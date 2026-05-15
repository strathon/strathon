"""Cross-framework parity integration test.

The central claim of Strathon is "same CEL policy, three frameworks, no code
changes." This test proves that claim against a live receiver:

    1. Install one CEL policy via REST
    2. For each framework (langgraph, crewai, openai_agents):
        a. Initialize a Strathon Client; let it pull the policy via /v1/policies
        b. Instrument the framework
        c. Trigger a tool call matching the policy
        d. Assert StrathonPolicyBlocked was raised
        e. Assert the SDK's blocked-span made it to the DB with
           strathon.framework set to the right value

The three frameworks use three different block mechanisms internally
(LangChain on_tool_start raise, CrewAI invoke patch, OAI Runner wrap +
RunHooks). If any of those drifts in attribute naming or evaluation
semantics, this test catches it.
"""

from __future__ import annotations

import asyncio
import json
import time
from urllib.request import Request, urlopen

import psycopg
import pytest

from strathon import Client
from strathon.policy import StrathonPolicyBlocked


DEV_API_KEY = "stra_dev_local_default_project_do_not_use_in_production"

# Single CEL policy used for all three framework runs. Note that the
# expression only references attribute names that all three integrations
# emit: strathon.tool.args and gen_ai.tool.name. This is exactly the
# cross-framework standardization we shipped earlier — if it ever breaks,
# this test fails.
PARITY_POLICY_NAME = "parity_test_block_competitor_email"
PARITY_POLICY_BODY = {
    "name": PARITY_POLICY_NAME,
    "description": "Parity test: block emails to @competitor.com from any framework",
    "match_expression": (
        'attrs["gen_ai.tool.name"] == "send_email" && '
        'attrs["strathon.tool.args"].contains("@competitor.com")'
    ),
    "action": "block",
    "action_config": {"message": "Cannot email a competitor (parity test policy)."},
    "priority": 500,
}


# ---- REST helpers (authenticated) ----


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {DEV_API_KEY}"}


def _post(url: str, payload: dict) -> dict:
    req = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **_auth_headers()},
        method="POST",
    )
    with urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(url: str) -> dict:
    req = Request(url, headers=_auth_headers(), method="GET")
    with urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _delete(url: str) -> None:
    req = Request(url, headers=_auth_headers(), method="DELETE")
    try:
        with urlopen(req, timeout=10):
            pass
    except Exception:
        pass


# ---- Fixtures ----


@pytest.fixture
def parity_policy(receiver):
    """Install the parity policy fresh. Tears down on exit so each test
    function starts from a clean slate (and we don't pollute the user's
    local policy table).
    """
    existing = _get(f"{receiver}/v1/policies").get("policies", [])
    for p in existing:
        if p["name"] == PARITY_POLICY_NAME:
            _delete(f"{receiver}/v1/policies/{p['id']}")

    policy = _post(f"{receiver}/v1/policies", PARITY_POLICY_BODY)

    yield policy

    _delete(f"{receiver}/v1/policies/{policy['id']}")


@pytest.fixture
def strathon_client(receiver):
    """A fresh Client per test. Each gets its own enforcer and instrumentation
    so framework patches in one test don't leak into the next.
    """
    client = Client(
        api_key=DEV_API_KEY,
        endpoint=receiver,
        service_name="parity-integration-test",
        environment="test",
    )
    # Wait a tick for the enforcer's background fetch to land
    time.sleep(0.5)
    yield client
    client.flush(timeout_millis=5000)
    client.shutdown()


@pytest.fixture
def db_conn(database_url):
    """Direct Postgres connection for asserting on persisted state."""
    url = database_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = psycopg.connect(url)
    yield conn
    conn.close()


# ---- Per-framework exercise helpers ----


def _exercise_langgraph(client: Client):
    """Trigger a tool call through the LangGraph callback path.

    Note: LangChain (unlike CrewAI and OAI Agents SDK) has no global
    "register a callback handler" mechanism. The Strathon handler must be
    passed via config={"callbacks": [handler]} on each invocation. This is
    the single piece of code the user has to write that's framework-specific;
    in real apps it's usually wired once at the agent/graph level rather
    than per-tool.

    Returns the StrathonPolicyBlocked exception that should have been raised.
    """
    from langchain_core.tools import tool
    from strathon.instrumentation.langgraph import instrument

    handler = instrument(client)

    @tool
    def send_email(to: str, subject: str, body: str) -> str:
        """Send an email."""
        return f"sent to {to}"

    try:
        send_email.invoke(
            {
                "to": "sales@competitor.com",
                "subject": "Hi",
                "body": "let's chat",
            },
            config={"callbacks": [handler]},
        )
    except StrathonPolicyBlocked as exc:
        return exc
    return None


def _exercise_crewai(client: Client):
    from crewai.tools import BaseTool
    from pydantic import BaseModel
    from strathon.instrumentation.crewai import instrument

    instrument(client)

    class EmailInput(BaseModel):
        to: str = ""
        subject: str = ""
        body: str = ""

    class SendEmailTool(BaseTool):
        name: str = "send_email"
        description: str = "Send an email to the given recipient."
        args_schema: type = EmailInput

        def _run(self, to: str = "", subject: str = "", body: str = "") -> str:
            return f"sent to {to}"

    tool = SendEmailTool()
    structured = tool.to_structured_tool()

    try:
        structured.invoke({
            "to": "sales@competitor.com",
            "subject": "Hi",
            "body": "let's chat",
        })
    except StrathonPolicyBlocked as exc:
        return exc
    return None


def _exercise_openai_agents(client: Client):
    """Exercise the OAI Agents block path by invoking on_tool_start directly.

    Driving Runner.run would need a real OpenAI API call. We call the same
    hook the Runner wrapper builds; this is the line that actually decides
    whether the tool runs.
    """
    from agents import function_tool
    from strathon.instrumentation.openai_agents import (
        instrument,
        _build_strathon_run_hooks,
    )

    instrument(client)

    @function_tool
    def send_email(to: str, subject: str, body: str) -> str:
        """Send an email."""
        return f"sent to {to}"

    class _ToolContext:
        def __init__(self, args):
            self.tool_arguments = args

    hooks = _build_strathon_run_hooks(client, user_hooks=None)
    ctx = _ToolContext({
        "to": "sales@competitor.com",
        "subject": "Hi",
        "body": "let's chat",
    })

    try:
        asyncio.run(hooks.on_tool_start(ctx, agent=None, tool=send_email))
    except StrathonPolicyBlocked as exc:
        return exc
    return None


FRAMEWORK_EXERCISERS = {
    "langgraph": (_exercise_langgraph, "langgraph"),
    "crewai":    (_exercise_crewai, "crewai"),
    "openai_agents": (_exercise_openai_agents, "agents"),
}


# ---- The parity test ----


@pytest.mark.parametrize("framework_key", list(FRAMEWORK_EXERCISERS.keys()))
def test_one_policy_blocks_across_all_frameworks(
    framework_key, parity_policy, strathon_client, db_conn,
):
    """One CEL policy. Three frameworks. All three must block identically."""
    exercise, expected_framework_attr = FRAMEWORK_EXERCISERS[framework_key]

    # The SDK enforcer should have picked up the policy by now
    enforcer = strathon_client.policy_enforcer
    assert enforcer is not None, "client has no policy enforcer"
    policy_names = [p.name for p in enforcer.policies]
    assert PARITY_POLICY_NAME in policy_names, (
        f"enforcer didn't load the parity policy. Loaded: {policy_names}"
    )

    # ---- Exercise: must raise StrathonPolicyBlocked ----
    exc = exercise(strathon_client)
    assert exc is not None, (
        f"{framework_key}: expected StrathonPolicyBlocked, got nothing — "
        f"the tool ran without being blocked"
    )
    assert exc.policy_name == PARITY_POLICY_NAME, (
        f"{framework_key}: blocked by wrong policy {exc.policy_name!r}"
    )
    assert "competitor" in (exc.message or "").lower(), (
        f"{framework_key}: block message didn't carry the policy message: {exc.message!r}"
    )

    # ---- Flush so the blocked span reaches the receiver ----
    strathon_client.flush(timeout_millis=5000)
    # Best-effort wait for the receiver to ingest + record the audit row
    time.sleep(1.5)

    # ---- Assert: policy_matches row exists for this framework ----
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT m.action, m.action_outcome, p.name
            FROM policy_matches m
            JOIN policies p ON p.id = m.policy_id
            WHERE p.name = %s
              AND m.matched_at > NOW() - INTERVAL '60 seconds'
            ORDER BY m.matched_at DESC
            LIMIT 1
            """,
            (PARITY_POLICY_NAME,),
        )
        row = cur.fetchone()

    assert row is not None, (
        f"{framework_key}: no policy_matches row recorded for {PARITY_POLICY_NAME}"
    )
    action, outcome, name = row
    assert action == "block", f"{framework_key}: action={action!r}, expected 'block'"
    assert outcome == "block_recorded", (
        f"{framework_key}: outcome={outcome!r}, expected 'block_recorded'"
    )

    # ---- Assert: a span was persisted with strathon.framework set correctly ----
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT attributes->>'strathon.framework' AS framework,
                   attributes->>'strathon.policy.name' AS policy_name,
                   status_code
            FROM spans
            WHERE (attributes ? 'strathon.policy.blocked'
                   OR attributes->>'strathon.policy.matched_actions' LIKE '%%block%%')
              AND start_time_unix_nano > (EXTRACT(EPOCH FROM NOW() - INTERVAL '60 seconds') * 1e9)::bigint
            ORDER BY start_time_unix_nano DESC
            LIMIT 5
            """
        )
        spans = cur.fetchall()

    matching = [
        s for s in spans
        if s[0] == expected_framework_attr and s[1] == PARITY_POLICY_NAME
    ]
    assert matching, (
        f"{framework_key}: no span with strathon.framework={expected_framework_attr!r} "
        f"AND strathon.policy.name={PARITY_POLICY_NAME!r}. Recent spans: {spans}"
    )


def test_parity_policy_can_be_fetched_via_rest(receiver, parity_policy):
    """Sanity: the policy fixture installed cleanly and is readable.

    Mostly catches receiver-side regressions in the policy CRUD path that
    would make the framework tests fail for unrelated reasons.
    """
    policies = _get(f"{receiver}/v1/policies").get("policies", [])
    names = [p["name"] for p in policies]
    assert PARITY_POLICY_NAME in names


# ---- applies_to: live end-to-end through the receiver ------------------
#
# Verifies the dot-segment-path match semantic in production-shape: a
# policy with applies_to filters out spans whose name doesn't align on
# segment boundaries. Two spans go in (one matching, one not), one
# policy_matches row comes out.


def test_applies_to_filters_at_ingest(receiver, db_conn):
    """A policy with applies_to=['langgraph.tool'] must record exactly one
    policy_matches row when given two spans — one whose name starts with
    'langgraph.tool', one whose name doesn't.
    """
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    policy_name = "applies_to_segment_filter_test"

    # Clean up any prior run before installing
    existing = _get(f"{receiver}/v1/policies").get("policies", [])
    for p in existing:
        if p["name"] == policy_name:
            _delete(f"{receiver}/v1/policies/{p['id']}")

    policy = _post(
        f"{receiver}/v1/policies",
        {
            "name": policy_name,
            "description": "Verify applies_to dot-segment-path filtering",
            "match_expression": "true",  # match any attrs; applies_to does the scoping
            "action": "alert",
            "action_config": {"webhook_url": "http://127.0.0.1:1/never"},
            "applies_to": ["langgraph.tool"],
        },
    )

    try:
        # Send one span matching the filter and one that doesn't. We
        # build our own provider + processor and use it directly rather
        # than touching the global TracerProvider — other tests in the
        # session may have already installed one, and the warning we'd
        # get for overriding it actually swallows the span export.
        provider = TracerProvider()
        exporter = OTLPSpanExporter(
            endpoint=f"{receiver}/v1/traces", headers=_auth_headers(),
        )
        processor = BatchSpanProcessor(exporter)
        provider.add_span_processor(processor)
        tracer = provider.get_tracer("applies_to_test")

        with tracer.start_as_current_span("langgraph.tool.send_email") as s:
            s.set_attribute("gen_ai.tool.name", "send_email")
        with tracer.start_as_current_span("langgraph.llm.chat") as s:
            s.set_attribute("gen_ai.system", "openai")

        # Flush via the processor we own (provider.shutdown() works too,
        # but is symmetric only when we own the provider; force_flush is
        # the explicit path).
        processor.force_flush(timeout_millis=5000)
        processor.shutdown()

        # Give the receiver a moment to ingest + evaluate
        deadline = time.time() + 10
        rows: list = []
        while time.time() < deadline:
            with db_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT s.name
                    FROM policy_matches m
                    JOIN spans s ON s.span_id = m.span_id
                    WHERE m.policy_id = %s
                    ORDER BY m.matched_at
                    """,
                    (policy["id"],),
                )
                rows = cur.fetchall()
            if rows:
                break
            time.sleep(0.5)

        # Exactly one row, and it must be the tool span.
        assert len(rows) == 1, (
            f"expected exactly 1 policy_matches row under applies_to=['langgraph.tool'], "
            f"got {len(rows)}: {rows}"
        )
        assert rows[0][0] == "langgraph.tool.send_email", (
            f"unexpected matching span name: {rows[0][0]!r}"
        )
    finally:
        _delete(f"{receiver}/v1/policies/{policy['id']}")
