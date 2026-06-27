"""Per-tool steer enforcement and shared policy-decision dispatch.

This module owns two things:

1. **dispatch_policy_decision** — the single decision engine for what
   block / steer / allow means in terms of return-value-or-raise, plus
   the side effects (audit span emission, failure isolation). Used by
   every framework that wants Strathon policy enforcement on a tool
   call boundary.

2. **enforce_steer / disable_steer** — per-tool enrollment for
   Runnable-shaped frameworks (LangChain BaseTool, CrewAI structured
   tools), built on top of the shared dispatcher.

Why a shared dispatcher
=======================

Block + steer + allow has three behaviors and at least two side effects
(span emission, log isolation). Before this module existed, the CrewAI
patch had its own copy of the branching, the LangGraph callback had its
own copy, and we had to keep them coherent by hand. They drifted: the
CrewAI patch emitted intervention spans for audit; LangGraph and the
OpenAI Agents RunHooks path didn't. The receiver got blocks from
CrewAI in its policy_matches table but missed them from LangGraph.

dispatch_policy_decision is the single function that:

* Calls ``client.check_policy(span_context)``
* On exception, logs and returns the result of ``on_allow()`` — the
  contract every framework needs: a broken policy lookup never breaks
  the user's app
* On ``is_block``: emits an intervention span recording the block, then
  raises ``StrathonPolicyBlocked``. The user's tool body never runs.
* On ``is_steer``: emits an intervention span recording the steer plus
  the replacement string, then returns the replacement. The user's tool
  body never runs.
* On ``is_allow``: returns ``on_allow()`` — i.e., runs the real tool
  body and returns its value.

Two enrollment strategies
=========================

Per-tool (this module's enforce_steer):
    User opts each tool in. enforce_steer(tool, client) records the
    binding in a module-level registry keyed by id(tool); patches the
    tool's class invoke/ainvoke once (idempotent); the patched method
    consults the registry and dispatches through dispatch_policy_decision
    only for enrolled tools. Non-enrolled tools of the same class pass
    straight through.

    Use case: LangGraph / @tool decorators / any Runnable. Replacing a
    tool's return value via steer is a meaningful contract change, so
    we make the user opt each tool in explicitly.

Global (strathon.instrumentation.crewai._install_policy_patch):
    Patches ``CrewStructuredTool.invoke`` for the whole class. Every
    CrewAI tool in the process is automatically subject to policies as
    soon as ``instrument(client)`` is called. This is the historic
    CrewAI contract (since launch) and we keep it stable.

    The CrewAI patch's invoke wrapper calls into the same
    dispatch_policy_decision, so the decision behavior matches the
    per-tool path exactly.

OpenAI Agents is a third strategy entirely (Tool Guardrails API) and
lives in strathon.instrumentation.openai_agents. It doesn't use the
Runnable invoke surface at all, so the shared dispatcher doesn't apply
there. The OAI Agents guardrail does its own block/steer/allow
branching using the same PolicyDecision shape.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Callable, Dict, Mapping, Optional, Set, Type

from strathon.policy.types import (
    StrathonApprovalDenied,
    StrathonHaltExceeded,
    StrathonPolicyBlocked,
    StrathonPolicyThrottled,
)


logger = logging.getLogger("strathon.policy.steer")


# ===========================================================================
# Shared formatting helpers
# ===========================================================================

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
    """Best-effort JSON serialize for the strathon.tool.args attribute.

    JSON when possible (so CEL expressions can match on substrings
    inside dict-shaped inputs); ``str()`` fallback otherwise. Never raises.
    """
    try:
        return json.dumps(x, default=_safe_str)
    except Exception:
        return _safe_str(x)


def build_tool_span_attrs(
    tool: Any,
    input_value: Any,
    framework: Optional[str],
) -> Dict[str, Any]:
    """Build the attribute dict for a tool-boundary policy check.

    Public-internal: used by the enforce_steer class patch and by the
    CrewAI instrument-time global patch. Both produce attribute-shape
    parity so a policy authored against one framework matches the same
    way under the other.
    """
    tool_name = _safe_str(getattr(tool, "name", None) or "tool")
    attrs: Dict[str, Any] = {
        "gen_ai.tool.name": tool_name,
        "strathon.tool.name": tool_name,
        "strathon.tool.args": _truncate(_json_or_str(input_value), _MAX_ARG_LEN),
    }
    if framework:
        attrs["strathon.framework"] = framework
    return attrs


def _detect_framework(cls: Type[Any]) -> Optional[str]:
    """Best-effort framework label from a tool's class module."""
    module = getattr(cls, "__module__", "") or ""
    if module.startswith("langchain"):
        return "langchain"
    if module.startswith("crewai"):
        return "crewai"
    return None


# ===========================================================================
# Audit span emission
# ===========================================================================
#
# The receiver populates its policy_matches table when it ingests a span
# that has strathon.policy.* attributes. So the SDK needs to actually
# write a span for each block/steer decision — without this, blocks are
# invisible to the receiver (the tool span never gets opened because the
# body never ran).


def _emit_intervention_span(
    client: Any,
    *,
    span_name: str,
    attrs: Mapping[str, Any],
    decision_kind: str,  # 'blocked' or 'steered'
    decision: Any,
    replacement: Optional[str] = None,
) -> None:
    """Open and immediately close a span recording a block or steer.

    Best-effort: failures here are swallowed so observability bugs
    never break the user's tool. The receiver re-evaluates policies
    when it ingests the span and writes the policy_matches row, so
    losing the span just means losing one audit record — the tool's
    block/steer decision was still enforced locally.
    """
    try:
        from opentelemetry.trace import Status, StatusCode
    except Exception:  # pragma: no cover - otel must be importable in practice
        return

    try:
        tracer = client.tracer
    except Exception:
        return

    span_attrs = dict(attrs)
    span_attrs[f"strathon.policy.{decision_kind}"] = True
    if getattr(decision, "policy_id", None):
        span_attrs["strathon.policy.id"] = decision.policy_id
    if getattr(decision, "policy_name", None):
        span_attrs["strathon.policy.name"] = decision.policy_name
    if getattr(decision, "message", None):
        span_attrs["strathon.policy.message"] = decision.message
    # Halt-specific audit fields. Only populated when the decision
    # passed in is a HaltDecision; HaltDecision doesn't carry policy_*
    # fields, so the policy block above is a no-op for halts.
    if getattr(decision, "halt_id", None) is not None:
        span_attrs["strathon.halt.id"] = decision.halt_id
    if getattr(decision, "reason", None):
        span_attrs["strathon.halt.reason"] = decision.reason
    if getattr(decision, "scope", None):
        span_attrs["strathon.halt.scope"] = decision.scope
    if getattr(decision, "scope_value", None):
        span_attrs["strathon.halt.scope_value"] = decision.scope_value
    if replacement is not None:
        span_attrs["strathon.policy.replacement"] = _truncate(replacement, _MAX_ARG_LEN)

    try:
        span = tracer.start_span(name=span_name, attributes=span_attrs)
        try:
            if decision_kind in ("blocked", "approval_denied"):
                span.set_status(
                    Status(StatusCode.ERROR, decision.message or "policy blocked")
                )
            elif decision_kind == "halted":
                span.set_status(
                    Status(
                        StatusCode.ERROR,
                        getattr(decision, "reason", None) or "halted by operator",
                    )
                )
            else:
                span.set_status(Status(StatusCode.OK))
        finally:
            span.end()
    except Exception:
        logger.debug(
            "failed to emit intervention span %r", span_name, exc_info=True
        )


# ===========================================================================
# Shared dispatcher — the one decision engine
# ===========================================================================


def check_halt_or_raise(client: Any, span_name: str, attrs: Dict[str, Any]) -> None:
    """Shared operator-halt check for adapter pre-tool hooks.

    Every enforcement surface must honor operator kill-switches (the dashboard
    panic-stop), not just the surfaces that route through the dispatchers. The
    callback/hook-based adapters (autogen, google_adk, claude_agent,
    openai_agents RunHooks, langgraph, pydantic_ai) call this at the top of their
    pre-tool hook so halt behavior is identical everywhere and can't drift the
    way it did when each adapter hand-rolled its own enforcement.

    Raises StrathonHaltExceeded if an operator halt is active for this call.
    Fail-open isolation on lookup error (a bug in halt code must not break the
    user's tool), matching the dispatchers.
    """
    tool_name = attrs.get("strathon.tool.name") or attrs.get("gen_ai.tool.name") or "tool"
    try:
        halt_decision = client.check_halt({"name": span_name, "attrs": attrs})
    except Exception:
        logger.exception("halt check raised for %s; allowing tool", tool_name)
        return
    if halt_decision is None or not halt_decision.is_halt:
        return
    _emit_intervention_span(
        client, span_name=span_name, attrs=attrs,
        decision_kind="halted", decision=halt_decision,
    )
    scope_desc = (
        f"agent '{halt_decision.scope_value}'"
        if halt_decision.scope == "agent"
        else "project"
    )
    raise StrathonHaltExceeded(
        (
            f"Tool '{tool_name}' halted by Strathon "
            f"(halt #{halt_decision.halt_id}, {scope_desc}): "
            f"{halt_decision.reason or 'no reason given'}"
        ),
        halt_id=halt_decision.halt_id,
        scope=halt_decision.scope,
        scope_value=halt_decision.scope_value,
        reason=halt_decision.reason,
    )


def dispatch_policy_decision(
    client: Any,
    *,
    span_name: str,
    attrs: Dict[str, Any],
    on_allow: Callable[[], Any],
) -> Any:
    """Run policy evaluation and act on the result.

    The single decision engine used by every framework integration that
    enforces policies on a Runnable-style tool invoke. Both the per-tool
    enforce_steer path and CrewAI's global instrument-time patch call
    through here so their behavior is identical by construction.

    Parameters
    ----------
    client
        The Strathon Client whose ``check_policy`` produces the decision
        and whose ``tracer`` is used for the audit span.
    span_name
        OTel span name for the intervention record (e.g.
        ``"crewai.tool.send_email"`` or ``"tool.send_email"``).
    attrs
        Span attributes the CEL expression is evaluated against. Caller
        constructs these via ``build_tool_span_attrs``.
    on_allow
        Zero-arg callable that runs the real tool body. Called only on
        ``is_allow``. Its return value is returned to the caller as-is.

    Returns
    -------
    Any
        On ``is_allow``: whatever ``on_allow()`` returns.
        On ``is_steer``: the replacement string (decision.replacement or
        a generated fallback). The tool body never runs.

    Raises
    ------
    StrathonPolicyBlocked
        On ``is_block``. The tool body never runs.

    Failure isolation
    -----------------
    If ``client.check_policy`` itself raises, the exception is logged at
    error level and ``on_allow()`` is called. The user's tool keeps
    working regardless of bugs in policy code.
    """
    tool_name = attrs.get("strathon.tool.name") or attrs.get("gen_ai.tool.name") or "tool"

    # ---- Halt check first ----
    # Operator-imposed kill-switches override everything. If the agent
    # has been stopped by an operator, we don't run policy CEL or the
    # tool body. Same fail-open isolation as the policy check below:
    # any exception in the halt lookup logs and proceeds, so a bug in
    # halt code can't break the user's tool.
    try:
        halt_decision = client.check_halt({"name": span_name, "attrs": attrs})
    except Exception:
        logger.exception(
            "halt check raised for %s; allowing tool", tool_name,
        )
        halt_decision = None

    if halt_decision is not None and halt_decision.is_halt:
        _emit_intervention_span(
            client,
            span_name=span_name,
            attrs=attrs,
            decision_kind="halted",
            decision=halt_decision,
        )
        scope_desc = (
            f"agent '{halt_decision.scope_value}'"
            if halt_decision.scope == "agent"
            else "project"
        )
        raise StrathonHaltExceeded(
            (
                f"Tool '{tool_name}' halted by Strathon "
                f"(halt #{halt_decision.halt_id}, {scope_desc}): "
                f"{halt_decision.reason or 'no reason given'}"
            ),
            halt_id=halt_decision.halt_id,
            scope=halt_decision.scope,
            scope_value=halt_decision.scope_value,
            reason=halt_decision.reason,
        )

    try:
        decision = client.check_policy({"name": span_name, "attrs": attrs})
    except Exception:
        # Policy lookup failures must NEVER break the user's tool.
        logger.exception(
            "policy check raised for %s; allowing tool", tool_name
        )
        return on_allow()

    if decision.is_block:
        _emit_intervention_span(
            client,
            span_name=span_name,
            attrs=attrs,
            decision_kind="blocked",
            decision=decision,
        )
        raise StrathonPolicyBlocked(
            decision.message or f"Tool '{tool_name}' blocked by Strathon policy",
            policy_id=decision.policy_id,
            policy_name=decision.policy_name,
        )

    if decision.is_throttle:
        # Throttle: a matching policy's rate limit refused this specific
        # call. Audit-span it like a block so operators can see the
        # rejection in /v1/policy_matches; raise a distinct exception so
        # caller code that wants to backoff-and-retry can branch on type.
        _emit_intervention_span(
            client,
            span_name=span_name,
            attrs=attrs,
            decision_kind="throttled",
            decision=decision,
        )
        raise StrathonPolicyThrottled(
            decision.message
            or f"Tool '{tool_name}' rate-limited by Strathon policy",
            policy_id=decision.policy_id,
            policy_name=decision.policy_name,
            retry_after_seconds=decision.retry_after_seconds,
        )

    if decision.is_steer:
        # The agent receives our replacement string as if it were the
        # tool's real output, then self-corrects on its next turn.
        replacement = decision.replacement or (
            f"[Strathon: tool '{tool_name}' redirected by policy"
            + (f" '{decision.policy_name}'" if decision.policy_name else "")
            + "]"
        )
        _emit_intervention_span(
            client,
            span_name=span_name,
            attrs=attrs,
            decision_kind="steered",
            decision=decision,
            replacement=replacement,
        )
        return replacement

    if decision.is_require_approval:
        # Human-in-the-loop: create an approval on the receiver, then
        # block the tool call until an operator approves or denies.
        _emit_intervention_span(
            client,
            span_name=span_name,
            attrs=attrs,
            decision_kind="approval_requested",
            decision=decision,
        )
        from strathon.policy.approval import wait_for_approval
        # on_timeout configurable via action_config; default deny.
        on_timeout = "deny"
        try:
            wait_for_approval(
                client, decision,
                {"name": span_name, "attrs": attrs},
                on_timeout=on_timeout,
            )
        except StrathonApprovalDenied:
            raise
        except Exception:
            logger.exception(
                "approval workflow failed for %s; denying tool", tool_name
            )
            raise StrathonApprovalDenied(
                f"Approval workflow failed for tool '{tool_name}'",
                policy_id=decision.policy_id,
                policy_name=decision.policy_name,
                status="error",
            )
        # Approved — run the tool.
        return on_allow()

    # is_allow: run the original tool body. Any decision that is NOT a
    # recognized allow fails closed — a future action this SDK version doesn't
    # understand must never silently run the tool (forward-compat: today
    # check_policy only emits known actions, so this branch is unreachable, but
    # it keeps "unknown action -> allow" from ever being the default).
    if decision.is_allow:
        return on_allow()
    _emit_intervention_span(
        client, span_name=span_name, attrs=attrs,
        decision_kind="blocked", decision=decision,
    )
    raise StrathonPolicyBlocked(
        f"Tool '{tool_name}' blocked: unrecognized policy decision "
        f"'{getattr(decision, 'action', '?')}' (SDK cannot enforce it)",
        policy_id=getattr(decision, "policy_id", None),
        policy_name=getattr(decision, "policy_name", None),
    )


async def dispatch_policy_decision_async(
    client: Any,
    *,
    span_name: str,
    attrs: Dict[str, Any],
    on_allow: Callable[[], Any],
) -> Any:
    """Async sibling of ``dispatch_policy_decision``.

    Behaviorally identical to the sync dispatcher — halt check first, then
    block / throttle / steer / require_approval / allow — but for surfaces whose
    tool invoke is a coroutine (LangChain ``ainvoke``, the OpenAI Agents tool
    guardrail, and any other async pre-execution hook). Routing every async
    surface through this one function is what keeps async enforcement from
    drifting away from the sync engine the way it did before (require_approval
    and the halt check were silently missing on the hand-rolled async paths).

    ``on_allow`` may be sync or async; its result is awaited if awaitable, so the
    caller's real (possibly async) tool body runs only on allow/approval.

    Because this runs inside a coroutine, ``require_approval`` does REAL
    interactive approval via ``await_for_approval`` (suspends without blocking
    the event loop) rather than failing closed.
    """
    tool_name = attrs.get("strathon.tool.name") or attrs.get("gen_ai.tool.name") or "tool"

    async def _run_allow() -> Any:
        result = on_allow()
        if asyncio.iscoroutine(result):
            return await result
        return result

    # ---- Halt check first (operator kill-switch overrides everything) ----
    try:
        halt_decision = client.check_halt({"name": span_name, "attrs": attrs})
    except Exception:
        logger.exception("halt check raised for %s; allowing tool", tool_name)
        halt_decision = None

    if halt_decision is not None and halt_decision.is_halt:
        _emit_intervention_span(
            client,
            span_name=span_name,
            attrs=attrs,
            decision_kind="halted",
            decision=halt_decision,
        )
        scope_desc = (
            f"agent '{halt_decision.scope_value}'"
            if halt_decision.scope == "agent"
            else "project"
        )
        raise StrathonHaltExceeded(
            (
                f"Tool '{tool_name}' halted by Strathon "
                f"(halt #{halt_decision.halt_id}, {scope_desc}): "
                f"{halt_decision.reason or 'no reason given'}"
            ),
            halt_id=halt_decision.halt_id,
            scope=halt_decision.scope,
            scope_value=halt_decision.scope_value,
            reason=halt_decision.reason,
        )

    try:
        decision = client.check_policy({"name": span_name, "attrs": attrs})
    except Exception:
        logger.exception("policy check raised for %s; allowing tool", tool_name)
        return await _run_allow()

    if decision.is_block:
        _emit_intervention_span(
            client, span_name=span_name, attrs=attrs,
            decision_kind="blocked", decision=decision,
        )
        raise StrathonPolicyBlocked(
            decision.message or f"Tool '{tool_name}' blocked by Strathon policy",
            policy_id=decision.policy_id,
            policy_name=decision.policy_name,
        )

    if decision.is_throttle:
        _emit_intervention_span(
            client, span_name=span_name, attrs=attrs,
            decision_kind="throttled", decision=decision,
        )
        raise StrathonPolicyThrottled(
            decision.message or f"Tool '{tool_name}' rate-limited by Strathon policy",
            policy_id=decision.policy_id,
            policy_name=decision.policy_name,
            retry_after_seconds=decision.retry_after_seconds,
        )

    if decision.is_steer:
        replacement = decision.replacement or (
            f"[Strathon: tool '{tool_name}' redirected by policy"
            + (f" '{decision.policy_name}'" if decision.policy_name else "")
            + "]"
        )
        _emit_intervention_span(
            client, span_name=span_name, attrs=attrs,
            decision_kind="steered", decision=decision, replacement=replacement,
        )
        return replacement

    if decision.is_require_approval:
        _emit_intervention_span(
            client, span_name=span_name, attrs=attrs,
            decision_kind="approval_requested", decision=decision,
        )
        from strathon.policy.approval import await_for_approval
        try:
            await await_for_approval(
                client, decision,
                {"name": span_name, "attrs": attrs},
                on_timeout="deny",
            )
        except StrathonApprovalDenied:
            raise
        except Exception:
            logger.exception(
                "approval workflow failed for %s; denying tool", tool_name
            )
            raise StrathonApprovalDenied(
                f"Approval workflow failed for tool '{tool_name}'",
                policy_id=decision.policy_id,
                policy_name=decision.policy_name,
                status="error",
            )
        return await _run_allow()

    # is_allow: run the tool body. Any non-allow decision that reaches here is
    # an action this SDK version doesn't recognize — fail closed rather than
    # silently run the tool (matrix test asserts no unrecognized decision
    # reaches a live surface).
    if decision.is_allow:
        return await _run_allow()
    _emit_intervention_span(
        client, span_name=span_name, attrs=attrs,
        decision_kind="blocked", decision=decision,
    )
    raise StrathonPolicyBlocked(
        f"Tool '{tool_name}' blocked: unrecognized policy decision "
        f"'{getattr(decision, 'action', '?')}' (SDK cannot enforce it)",
        policy_id=getattr(decision, "policy_id", None),
        policy_name=getattr(decision, "policy_name", None),
    )


# ===========================================================================
# Per-tool enrollment (the enforce_steer surface)
# ===========================================================================
#
# Module-level state:
#
#   _PATCHED_CLASSES[cls] = (original_invoke, original_ainvoke_or_None)
#     -> the unpatched method objects, so we can call through when a tool
#        isn't enrolled and so we can uninstall cleanly in tests.
#
#   _ENROLLED_TOOLS[cls] = {id(tool), ...}
#     -> which specific tool instances should have policy enforcement
#        run for them. id() is fine: tools are long-lived; we only
#        need set membership.
#
#   _CLIENT_FOR[id(tool)] = client
#     -> per-tool client binding. Each enrolled tool remembers which
#        client should evaluate its policies. Allows multiple clients
#        in the same process without crosstalk.
#
# All three guarded by `_LOCK` since tools may be enrolled from different
# threads (e.g., app startup vs. first request handler).

_PATCHED_CLASSES: Dict[Type[Any], tuple[Callable[..., Any], Optional[Callable[..., Any]]]] = {}
_ENROLLED_TOOLS: Dict[Type[Any], Set[int]] = {}
_CLIENT_FOR: Dict[int, Any] = {}
_LOCK = threading.Lock()


def _install_class_patch(cls: Type[Any]) -> None:
    """Patch cls.invoke (and cls.ainvoke if present) once per class.

    Caller must hold _LOCK.
    """
    if cls in _PATCHED_CLASSES:
        return

    original_invoke = cls.invoke
    original_ainvoke = getattr(cls, "ainvoke", None)
    framework = _detect_framework(cls)

    def _patched_invoke(self, input, config=None, **kwargs):  # noqa: A002 - matches Runnable signature
        client = _CLIENT_FOR.get(id(self))
        if client is None or getattr(client, "_policy_enforcer", None) is None:
            return original_invoke(self, input, config, **kwargs)

        attrs = build_tool_span_attrs(self, input, framework)
        tool_name = attrs["strathon.tool.name"]
        span_name = f"tool.{tool_name}"

        def _on_allow():
            return original_invoke(self, input, config, **kwargs)

        return dispatch_policy_decision(
            client, span_name=span_name, attrs=attrs, on_allow=_on_allow,
        )

    if original_ainvoke is not None:
        async def _patched_ainvoke(self, input, config=None, **kwargs):  # noqa: A002
            client = _CLIENT_FOR.get(id(self))
            if client is None or getattr(client, "_policy_enforcer", None) is None:
                result = original_ainvoke(self, input, config, **kwargs)
                if asyncio.iscoroutine(result):
                    return await result
                return result

            attrs = build_tool_span_attrs(self, input, framework)
            tool_name = attrs["strathon.tool.name"]
            span_name = f"tool.{tool_name}"

            # Route through the async dispatcher so this surface enforces the
            # full action set — halt check, block, throttle, steer, AND real
            # interactive require_approval — identically to the sync path.
            return await dispatch_policy_decision_async(
                client,
                span_name=span_name,
                attrs=attrs,
                on_allow=lambda: original_ainvoke(self, input, config, **kwargs),
            )

        cls.ainvoke = _patched_ainvoke  # type: ignore[assignment]

    cls.invoke = _patched_invoke  # type: ignore[assignment]
    _PATCHED_CLASSES[cls] = (original_invoke, original_ainvoke)


def enforce_steer(tool: Any, client: Any) -> None:
    """Enroll a tool for Strathon block + steer enforcement.

    Block policies are also evaluated here (so a user who hasn't called
    ``instrument(client)`` still gets block on tools they explicitly
    enroll). The block path in the per-framework instrumentation and
    the block path here are coherent — both raise
    ``StrathonPolicyBlocked`` on a matched block policy, both with the
    same exception payload.

    Idempotent. Calling ``enforce_steer(tool, client)`` twice is
    harmless; the second call updates which client is bound to ``tool``.

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
    """Remove a tool from the enforcement registry. Inverse of enforce_steer.

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
    """Restore every patched class to its original invoke/ainvoke.

    Tests only. Production code never calls this. Without it, a test
    that patches BaseTool would pollute all subsequent tests in the
    same process, because pytest reuses the interpreter across tests.
    """
    with _LOCK:
        for cls, (original_invoke, original_ainvoke) in _PATCHED_CLASSES.items():
            cls.invoke = original_invoke  # type: ignore[assignment]
            if original_ainvoke is not None:
                cls.ainvoke = original_ainvoke  # type: ignore[assignment]
        _PATCHED_CLASSES.clear()
        _ENROLLED_TOOLS.clear()
        _CLIENT_FOR.clear()


__all__ = [
    "build_tool_span_attrs",
    "disable_steer",
    "dispatch_policy_decision",
    "enforce_steer",
]
