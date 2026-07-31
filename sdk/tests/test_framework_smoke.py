"""Framework adapter smoke tests.

For each supported framework, if its package is installed, this verifies two
things: the adapter module imports, and `instrument()` actually attaches it (the
framework comes back in the instrumented list). That is enough to catch the
failure this guards against -- a framework release that renames a class or
changes a hook signature, which would make the adapter silently stop attaching
and leave a user believing calls are enforced when they are not.

These are deliberately lightweight: they import and attach, they do not run an
agent or a tool call (the full policy-enforcement path is covered by
test_policy_integration.py against LangChain). Their value is breadth -- every
adapter, checked against the framework version actually installed.

Run modes:
- Normal CI installs the floor versions from sdk/pyproject.toml, so this asserts
  the ">=floor" support claim is real.
- The weekly scheduled job installs the latest of each framework, so this
  catches upstream drift before a user hits it. There the same tests run; a
  failure there is a signal to look, not a release blocker.

A framework whose package is not installed is skipped, so the file is safe to
run in any environment (including the base SDK env with no extras).
"""

import importlib

import pytest

from strathon import Client
from strathon.instrumentation import SUPPORTED_FRAMEWORKS

# Maps the Strathon framework name to the import its adapter actually gates on --
# the import whose failure makes that adapter's instrument() return without
# attaching. This must mirror the real gate in each instrumentation module, not a
# merely-related package, or a case gives a false skip (framework present but we
# think it is absent) or a false failure. Verified against the adapters:
#   langchain and langgraph both attach through langchain_core's callback
#     system, so both gate on `langchain_core` (matching the strathon[langchain]
#     and strathon[langgraph] extras, which install langchain-core);
#   the rest gate on their own top-level package.
# A wrong entry that is too loose (claims a framework is present when the adapter
# cannot attach) fails loudly: instrument() does not attach, so
# test_adapter_attaches fails. A wrong entry that is too strict would instead
# skip silently, so the entries above are checked against the real adapter gates
# rather than guessed.
FRAMEWORK_IMPORT = {
    "anthropic": "anthropic",
    "autogen": "autogen_agentchat",
    "claude_agent": "claude_agent_sdk",
    "crewai": "crewai",
    "google_adk": "google.adk",
    "langchain": "langchain_core",
    "langgraph": "langchain_core",
    "openai": "openai",
    "openai_agents": "agents",
    "pydantic_ai": "pydantic_ai",
}


def _framework_installed(fw: str) -> bool:
    module = FRAMEWORK_IMPORT.get(fw)
    if module is None:
        return False
    try:
        importlib.import_module(module)
        return True
    except Exception:
        return False


def _client() -> Client:
    # No global tracer, no policy pull, and an in-memory exporter via the suite
    # conftest: the smoke needs a Client to attach to, not a live receiver.
    return Client(
        api_key="stra_smoke_test_key",
        set_global_tracer=False,
        enable_policies=False,
    )


def test_import_map_covers_every_supported_framework():
    # Guard the guard: if a new adapter is added to SUPPORTED_FRAMEWORKS, this
    # file must learn how to import its framework, or its smoke would silently
    # never run.
    missing = [fw for fw in SUPPORTED_FRAMEWORKS if fw not in FRAMEWORK_IMPORT]
    assert not missing, f"FRAMEWORK_IMPORT is missing entries for: {missing}"


@pytest.mark.parametrize("fw", SUPPORTED_FRAMEWORKS)
def test_adapter_module_imports(fw):
    if not _framework_installed(fw):
        pytest.skip(f"{fw} package not installed")
    # The adapter module itself must import against the installed framework --
    # this alone catches a top-level reference to a renamed framework symbol.
    importlib.import_module(f"strathon.instrumentation.{fw}")


@pytest.mark.parametrize("fw", SUPPORTED_FRAMEWORKS)
def test_adapter_attaches(fw):
    if not _framework_installed(fw):
        pytest.skip(f"{fw} package not installed")
    instrumented = instrument_one(fw)
    assert fw in instrumented, (
        f"instrument() did not attach {fw!r} even though its package is "
        f"installed; the adapter likely broke against the installed version."
    )


def instrument_one(fw: str):
    from strathon import instrument

    return instrument(_client(), frameworks=[fw])
