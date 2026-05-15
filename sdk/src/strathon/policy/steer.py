"""Per-tool steer enforcement for Runnable-shaped tools.

Background
==========

Strathon's enforcement model has two halves:

  * **block** — zero-code-change. The per-framework instrumentation
    (e.g. ``strathon.instrumentation.langgraph.instrument``) hooks the
    framework's tool callback and raises ``StrathonPolicyBlocked`` before
    the tool body runs. The user just calls ``instrument(client)`` once
    and every matched tool call is blocked.

  * **steer** — explicit opt-in. Replacing a tool's return value is a
    bigger contract change than refusing to call it, so we make the user
    opt each tool in. The user calls ``enforce_steer(my_tool, client)``
    and that tool's ``invoke``/``ainvoke`` will, on a steer match, return
    the policy's replacement string in place of the real output.

Why this shape (and not a per-instance attribute override)
----------------------------------------------------------

LangChain and CrewAI tools are Pydantic models. Pydantic v2 forbids
arbitrary per-instance attribute assignment (``ValueError: "Tool" object
has no field "invoke"`` on ``tool.invoke = wrapped``), which makes the
naive "wrap one tool's method" approach unworkable.

Two viable patterns remain:

  1. Return a wrapper that subclasses ``BaseTool``. The user has to
     reassign their variable and the wrapper has to mirror enough
     surface area (``args_schema``, ``name``, ``description``) for the
     framework to treat it identically to the original.
  2. Class-level patch with a per-tool registry. Patch
     ``ClassOfTool.invoke`` once; the patched method checks "is THIS
     particular tool instance enrolled for steer?" via a module-level
     registry keyed by class.

We use pattern 2:

  * The user's tool object is unchanged. ``enforce_steer(tool, client)``
    returns ``None`` and only mutates a registry. The user passes the
    same object to their agent.
  * One patch per class, installed lazily on first ``enforce_steer``
    call for that class. Idempotent: subsequent calls only retarget the
    active client and add to the registry.
  * Block path is unaffected — the per-framework instrumentation runs
    its own check upstream, independent of this registry. A tool can be
    both block-enrolled (automatically, via ``instrument``) and
    steer-enrolled (explicitly, via ``enforce_steer``).

This is the same shape ``strathon.instrumentation.crewai`` already uses
for its enforcement patch, generalized to any class with an
``invoke``/``ainvoke`` Runnable-style signature.

Supported tool surfaces
-----------------------

Any tool whose class exposes:

  * ``invoke(self, input, config=None, **kwargs) -> Any``  (required)
  * ``ainvoke(self, input, config=None, **kwargs) -> Awaitable[Any]``
    (optional; patched if present)
  * ``self.name`` for span attribution (defaulted to ``"tool"`` if absent)

This covers ``langchain_core.tools.BaseTool`` (and everything decorated
with ``@tool``) and ``crewai.tools.structured_tool.CrewStructuredTool``.

OpenAI Agents SDK is not Runnable-shaped — its tools are dataclasses
with an ``on_invoke_tool`` callable, and the runner orchestrates
guardrails separately. For that framework see
``strathon.instrumentation.openai_agents.attach_strathon_guardrails``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Callable, Dict, Set, Type

from strathon.policy.types import StrathonPolicyBlocked


logger = logging.getLogger("strathon.policy.steer")


# Module-level state:
#
#   _PATCHED_CLASSES[cls] = (original_invoke, original_ainvoke_or_None)
#     -> the unpatched method objects, so we can call through when a tool
#        isn't enrolled and so we can uninstall cleanly in tests.
#
#   _ENROLLED_TOOLS[cls] = {id(tool), ...}
#     -> which specific tool instances should have policy enforcement run
#        for them. id() is fine here because Pydantic models are
#        long-lived, and we only need set membership.
#
#   _CLIENT_FOR[id(tool)] = client
#     -> per-tool client binding. Each enrolled tool remembers which
#        client should evaluate its policies. Allows multiple clients in
#        the same process without crosstalk.
#
# All three are guarded by `_LOCK` because tools may be enrolled from
# different threads (e.g., on app startup vs. on first request).

_PATCHED_CLASSES: Dict[Type[Any], tuple[Callable[..., Any], Callable[..., Any] | None]] = {}
_ENROLLED_TOOLS: Dict[Type[Any], Set[int]] = {}
_CLIENT_FOR: Dict[int, Any] = {}
_LOCK = threading.Lock()


_MAX_ARG_LEN = 1500


def _safe_str(x: Any) -> str:
    """str() that never raises. Important inside hot patched paths."""
    try:
        return str(x)
    except Exception:
        return "<unstringable>"


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 3] + "..."


def _json_or_str(x: Any) -> str:
    """Best-effort serialize for the strathon.tool.args attribute.

    JSON when possible (so CEL expressions can look for substrings
    inside dict-shaped inputs), str() fallback otherwise. Never raises.
    """
    try:
        return json.dumps(x, default=_safe_str)
    except Exception:
        return _safe_str(x)


def _build_attrs(tool_obj: Any, input_value: Any, framework_hint: str | None) -> Dict[str, Any]:
    """Build the attribute dict the CEL expression matches against.

    Mirrors the shape produced by the existing per-framework callback
    handlers so policies authored for one path port to the other.
    """
    tool_name = _safe_str(getattr(tool_obj, "name", None) or "tool")
    attrs: Dict[str, Any] = {
        "gen_ai.tool.name": tool_name,
        "strathon.tool.name": tool_name,
        "strathon.tool.args": _truncate(_json_or_str(input_value), _MAX_ARG_LEN),
    }
    if framework_hint:
        attrs["strathon.framework"] = framework_hint
    return attrs


def _detect_framework(cls: Type[Any]) -> str | None:
    """Best-effort framework label for the strathon.framework attribute."""
    module = getattr(cls, "__module__", "") or ""
    if module.startswith("langchain"):
        return "langchain"
    if module.startswith("crewai"):
        return "crewai"
    return None


def _check_and_dispatch(
    self: Any,
    input_value: Any,
    original_call: Callable[..., Any],
    *call_args,
    **call_kwargs,
) -> Any:
    """Shared block/steer/allow decision logic for the patched invoke path.

    `original_call` is a zero-arg-after-self callable that runs the real
    tool body. We call it on `is_allow`. We never call it on `is_block`
    (raise) or `is_steer` (return replacement).
    """
    cls = type(self)
    client = _CLIENT_FOR.get(id(self))
    if client is None:
        # Tool was patched at the class level (because some other tool
        # was enrolled) but this particular instance isn't enrolled.
        # Straight through to the original.
        return original_call(*call_args, **call_kwargs)

    enforcer = getattr(client, "_policy_enforcer", None)
    if enforcer is None:
        # Client exists but has policies disabled. Allow.
        return original_call(*call_args, **call_kwargs)

    attrs = _build_attrs(self, input_value, _detect_framework(cls))
    tool_name = attrs["strathon.tool.name"]

    try:
        decision = client.check_policy({
            "name": f"tool.{tool_name}",
            "attrs": attrs,
        })
    except Exception:
        # Policy lookup failures must NEVER break the user's tool.
        logger.exception("steer policy check raised; allowing tool %s", tool_name)
        return original_call(*call_args, **call_kwargs)

    if decision.is_block:
        raise StrathonPolicyBlocked(
            decision.message or f"Tool '{tool_name}' blocked by Strathon policy",
            policy_id=decision.policy_id,
            policy_name=decision.policy_name,
        )

    if decision.is_steer:
        # The agent receives our replacement string as if it were the
        # tool's real output, then self-corrects on its next turn.
        replacement = decision.replacement or (
            f"[Strathon: tool '{tool_name}' redirected by policy"
            + (f" '{decision.policy_name}'" if decision.policy_name else "")
            + "]"
        )
        return replacement

    # is_allow: run the original body.
    return original_call(*call_args, **call_kwargs)


def _install_class_patch(cls: Type[Any]) -> None:
    """Patch cls.invoke (and cls.ainvoke if present) once per class.

    Caller must hold _LOCK.
    """
    if cls in _PATCHED_CLASSES:
        return

    original_invoke = cls.invoke
    original_ainvoke = getattr(cls, "ainvoke", None)

    def _patched_invoke(self, input, config=None, **kwargs):  # noqa: A002 - matches Runnable signature
        def _call_original():
            return original_invoke(self, input, config, **kwargs)
        return _check_and_dispatch(self, input, _call_original)

    if original_ainvoke is not None:
        async def _patched_ainvoke(self, input, config=None, **kwargs):  # noqa: A002
            # We cannot block on the original here because the original
            # may itself be async (e.g., in langchain-core, ainvoke
            # defaults to calling invoke in a thread). We wrap it in a
            # coroutine-returning closure and await the dispatched
            # result, which may be the closure's awaited value (allow),
            # a sync string (steer), or an exception (block).
            async def _call_original_async():
                result = original_ainvoke(self, input, config, **kwargs)
                if asyncio.iscoroutine(result):
                    return await result
                return result

            # _check_and_dispatch is sync; we drive it manually for the
            # async path so block/steer branches don't fight with the
            # event loop.
            cls_ = type(self)
            client = _CLIENT_FOR.get(id(self))
            if client is None or getattr(client, "_policy_enforcer", None) is None:
                return await _call_original_async()

            attrs = _build_attrs(self, input, _detect_framework(cls_))
            tool_name = attrs["strathon.tool.name"]
            try:
                decision = client.check_policy({
                    "name": f"tool.{tool_name}",
                    "attrs": attrs,
                })
            except Exception:
                logger.exception("steer policy check raised; allowing tool %s", tool_name)
                return await _call_original_async()

            if decision.is_block:
                raise StrathonPolicyBlocked(
                    decision.message or f"Tool '{tool_name}' blocked by Strathon policy",
                    policy_id=decision.policy_id,
                    policy_name=decision.policy_name,
                )
            if decision.is_steer:
                return decision.replacement or (
                    f"[Strathon: tool '{tool_name}' redirected by policy"
                    + (f" '{decision.policy_name}'" if decision.policy_name else "")
                    + "]"
                )
            return await _call_original_async()

        cls.ainvoke = _patched_ainvoke  # type: ignore[assignment]

    cls.invoke = _patched_invoke  # type: ignore[assignment]
    _PATCHED_CLASSES[cls] = (original_invoke, original_ainvoke)


def enforce_steer(tool: Any, client: Any) -> None:
    """Enroll a tool for Strathon block + steer enforcement.

    Block policies are also evaluated here (so a user who hasn't called
    ``instrument(client)`` still gets block on tools they explicitly
    enroll). The block path in the per-framework instrumentation and the
    block path here are coherent — both raise ``StrathonPolicyBlocked``
    on a matched block policy, both with the same exception payload.

    Idempotent. Calling ``enforce_steer(tool, client)`` twice is harmless;
    the second call updates which client is bound to ``tool``.

    Parameters
    ----------
    tool : Any
        A LangChain ``BaseTool`` (anything ``@tool``-decorated), a
        CrewAI structured tool, or any other object whose class has
        ``invoke(self, input, config=None, **kwargs) -> Any``.
    client : strathon.Client
        The Strathon client whose ``_policy_enforcer`` provides the
        decision. If the client has no enforcer, the patch is still
        installed but acts as a pass-through for this tool.

    Raises
    ------
    TypeError
        If ``tool``'s class doesn't have an ``invoke`` method (the
        Runnable-style hook we patch).
    """
    cls = type(tool)
    if not hasattr(cls, "invoke") or not callable(cls.invoke):
        raise TypeError(
            f"enforce_steer expects a tool with an invoke() method; "
            f"got {cls.__module__}.{cls.__qualname__}, which has none. "
            f"For OpenAI Agents SDK tools, use "
            f"strathon.instrumentation.openai_agents.attach_strathon_guardrails."
        )

    with _LOCK:
        _install_class_patch(cls)
        _ENROLLED_TOOLS.setdefault(cls, set()).add(id(tool))
        _CLIENT_FOR[id(tool)] = client

    logger.info(
        "Strathon steer enforcement enrolled on tool %r (%s.%s)",
        getattr(tool, "name", "tool"),
        cls.__module__,
        cls.__qualname__,
    )


def disable_steer(tool: Any) -> None:
    """Remove a tool from the enforcement registry. Inverse of ``enforce_steer``.

    Does not uninstall the class-level patch — that's intentional,
    because other tools of the same class may still be enrolled. With
    no tools enrolled the patch becomes a one-dict-lookup no-op, which
    is cheap enough that we don't bother undoing it.

    Idempotent.
    """
    cls = type(tool)
    with _LOCK:
        enrolled = _ENROLLED_TOOLS.get(cls)
        if enrolled is not None:
            enrolled.discard(id(tool))
        _CLIENT_FOR.pop(id(tool), None)


def _uninstall_all_for_testing() -> None:
    """Restore every patched class to its original invoke/ainvoke. Tests only.

    Production code never calls this. Without it, a test that patches
    BaseTool would pollute all subsequent tests in the same process,
    because pytest reuses the interpreter across tests.
    """
    with _LOCK:
        for cls, (original_invoke, original_ainvoke) in _PATCHED_CLASSES.items():
            cls.invoke = original_invoke  # type: ignore[assignment]
            if original_ainvoke is not None:
                cls.ainvoke = original_ainvoke  # type: ignore[assignment]
        _PATCHED_CLASSES.clear()
        _ENROLLED_TOOLS.clear()
        _CLIENT_FOR.clear()


__all__ = ["enforce_steer", "disable_steer"]
