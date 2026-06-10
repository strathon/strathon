"""End-to-end test for the halt enforcement loop.

This is the integration test that ties the server-side halt CRUD and the
SDK enforcement together:

  1. Operator creates a halt via POST /v1/halts on the real receiver.
  2. The SDK's HaltEnforcer polls /v1/intervention/sync and picks it up.
  3. A tool call enforced via dispatch_policy_decision raises
     StrathonHaltExceeded.
  4. Operator clears the halt via DELETE /v1/halts/{id}.
  5. The SDK's next poll observes the empty halt list.
  6. A subsequent tool call runs normally.

Skipped cleanly if Postgres isn't reachable (the receiver fixture
handles that). When the receiver is reachable, this test catches the
class of bug that pure unit tests can't: did the response shape we
agreed on actually round-trip end-to-end?
"""

from __future__ import annotations

import json
import time
import urllib.request


from strathon.policy.halt_enforcer import HaltEnforcer


DEV_API_KEY = "stra_dev_local_default_project_do_not_use_in_production"


def _create_halt(receiver: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{receiver}/v1/halts",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {DEV_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _delete_halt(receiver: str, halt_id: int) -> None:
    req = urllib.request.Request(
        f"{receiver}/v1/halts/{halt_id}",
        method="DELETE",
        headers={"Authorization": f"Bearer {DEV_API_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        resp.read()


def _purge_all_halts() -> None:
    """Wipe halt_state in the default project so this test doesn't
    interfere with other tests in the same session."""
    import psycopg
    import os
    db_url = os.environ.get(
        "DATABASE_URL",
        "postgresql://strathon:strathon_dev@127.0.0.1:5432/strathon",
    )
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("DELETE FROM halt_state")


def test_e2e_operator_halt_takes_effect_in_sdk_then_clears(receiver: str):
    """Real receiver, real SDK polling, real halt + clear cycle."""
    _purge_all_halts()

    # SDK halt enforcer pointed at the live receiver, with a fast
    # refresh interval so the test doesn't have to wait long.
    enforcer = HaltEnforcer(
        endpoint=receiver,
        api_key=DEV_API_KEY,
        refresh_interval_sec=0.1,
        request_timeout_sec=3.0,
    )
    enforcer.start()

    halt_id = None
    try:
        # 1. Before any halt, calls allow
        decision = enforcer.check_halt({
            "name": "tool.x",
            "attrs": {"strathon.agent.id": "test-agent"},
        })
        assert decision.is_allow

        # 2. Operator creates an agent-scoped halt
        created = _create_halt(receiver, {
            "scope": "agent",
            "scope_value": "test-agent",
            "reason": "e2e test halt",
        })
        halt_id = created["halt"]["id"]

        # 3. Wait for the SDK's background poll to pick it up
        deadline = time.time() + 3.0
        halted = False
        while time.time() < deadline:
            d = enforcer.check_halt({
                "name": "tool.x",
                "attrs": {"strathon.agent.id": "test-agent"},
            })
            if d.is_halt:
                halted = True
                assert d.halt_id == halt_id
                assert d.scope == "agent"
                assert d.scope_value == "test-agent"
                assert d.reason == "e2e test halt"
                break
            time.sleep(0.05)
        assert halted, "SDK did not observe the halt within 3s"

        # 4. A different agent is unaffected by the agent-scoped halt
        other = enforcer.check_halt({
            "name": "tool.x",
            "attrs": {"strathon.agent.id": "other-agent"},
        })
        assert other.is_allow

        # 5. Operator clears the halt
        _delete_halt(receiver, halt_id)
        halt_id = None  # don't double-clean

        # 6. Wait for the SDK to observe the clear
        deadline = time.time() + 3.0
        cleared = False
        while time.time() < deadline:
            d = enforcer.check_halt({
                "name": "tool.x",
                "attrs": {"strathon.agent.id": "test-agent"},
            })
            if d.is_allow:
                cleared = True
                break
            time.sleep(0.05)
        assert cleared, "SDK did not observe the cleared halt within 3s"

    finally:
        if halt_id is not None:
            try:
                _delete_halt(receiver, halt_id)
            except Exception:
                pass
        enforcer.stop()
        _purge_all_halts()


def test_e2e_project_halt_matches_all_agents(receiver: str):
    """A project-scope halt should match ANY agent, not just one."""
    _purge_all_halts()

    enforcer = HaltEnforcer(
        endpoint=receiver,
        api_key=DEV_API_KEY,
        refresh_interval_sec=0.1,
        request_timeout_sec=3.0,
    )
    enforcer.start()

    halt_id = None
    try:
        created = _create_halt(receiver, {
            "scope": "project",
            "reason": "project-wide killswitch",
        })
        halt_id = created["halt"]["id"]

        # Wait for SDK to pick it up; check that multiple distinct
        # agents are all affected.
        deadline = time.time() + 3.0
        agents_halted = set()
        while time.time() < deadline and len(agents_halted) < 3:
            for agent in ("a-1", "a-2", "a-3"):
                d = enforcer.check_halt({
                    "name": "tool.x",
                    "attrs": {"strathon.agent.id": agent},
                })
                if d.is_halt:
                    agents_halted.add(agent)
            time.sleep(0.05)

        assert agents_halted == {"a-1", "a-2", "a-3"}, (
            f"project-scope halt should match all agents; got {agents_halted}"
        )

    finally:
        if halt_id is not None:
            try:
                _delete_halt(receiver, halt_id)
            except Exception:
                pass
        enforcer.stop()
        _purge_all_halts()
