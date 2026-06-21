# Troubleshooting & FAQ

Common issues and questions. If something here doesn't resolve your problem,
ask in [Discord](https://discord.gg/Ta9XRmh4H) or open a
[GitHub issue](https://github.com/strathon/strathon/issues).

## My policy isn't matching

The most common cause: a CEL match expression that references an attribute the
span doesn't have. Strathon's CEL evaluation **fails safe**; a missing-key
error is treated as "no match" (the call is allowed), so a typo'd attribute
name silently never matches. Check:

- The attribute name is exact: `gen_ai.tool.name`, not `tool.name`.
- The framework is instrumented (`instrument(client, frameworks=[...])`) and
  the call actually goes through it.
- The policy status is `enabled`, not `shadow` (shadow records the decision but
  does not enforce) and not `disabled`.

Run the policy against recent traffic in the dashboard's policy simulator to
see whether it matches.

## I created a policy but nothing gets blocked

Two usual reasons:

1. The policy is in **shadow** status. Shadow mode evaluates and records but
   does not block. Switch it to `enabled`.
2. The action is `log` or `alert`. Those are passive (server-side) and never
   affect the call. To stop a call you need `block`, `throttle`, or
   `require_approval` (or `steer` to substitute a result). Note that `steer`
   and `require_approval` behavior depends on the framework surface: on the
   synchronous callback surfaces (LangGraph, LangChain, Pydantic AI), `steer`
   is observe-only and `require_approval` fails closed (blocks). See the
   [approval support matrix](https://getstrathon.com/docs/intervention#approval-support).

See [Concepts → Actions](concepts.md) for which actions affect the call.

## What happens if the receiver is unreachable?

By default the SDK is **fail-open**: if it cannot reach the receiver to refresh
policy state, your agent keeps running on the last-known cached policies rather
than stalling. A brief outage does not break your agent.

For security-critical agents you can set `fail_closed=True`, which stops tool
calls when policy state cannot be verified within
`fail_closed_max_staleness_sec`. Choose deliberately: fail-open prioritizes
uptime, fail-closed prioritizes control. See the Reliability section of the
README.

## How do I test a policy without blocking real traffic?

Set the policy to **shadow** status. It will be evaluated against live traffic
and its decisions recorded (visible in traces and the audit log), but no call
is actually blocked. When you're confident, promote it to `enabled`.

## I invited a member but they don't appear in the list

Invitations show as **pending** rows in the members list until the person
registers. If you don't see a pending row right after inviting, refresh the
page. Once they register with the invited email, they're moved from pending to
an active member automatically.

## A new user registered but has no access

After the first user (who becomes owner), new registrations do not get project
access automatically; access is granted by invitation. An owner or admin
invites the email; when that person registers, they join with the invited role.
This prevents anyone who can reach the registration page from joining your
project.

## How do I rotate or revoke an API key?

In **Settings → API Keys**, use the actions menu on a key. **Rotate** issues a
new secret and invalidates the old one immediately; copy the new secret, it is
shown only once. **Revoke** disables the key without issuing a replacement. See
[API Keys](api_keys.md).

## Throttle decisions don't show up in my usual metrics

Throttle (and block, steer, and allow-list deny) decisions are enforced in the
SDK, not the receiver, so they aren't ordinary HTTP requests. Each decision is
recorded as an **intervention span** with attributes like
`strathon.policy.throttled = true`. Query intervention spans to count SDK-side
decisions. See [Runtime Intervention](intervention.md).

## Docker Compose fails or the dashboard won't build

For local development, running the receiver and dashboard directly (uvicorn +
`npm run dev`) is the simplest path and gives you hot reload. For container
deployment use the published combined image. See [Self-Hosting](self-hosting.md)
for the supported setups.

## Timestamps look wrong / show UTC

The dashboard renders all timestamps in your browser's local timezone
automatically. If a time looks off, check your machine's timezone setting:
there is no manual timezone selector to misconfigure.

## Is my own data export gated behind a paid plan?

No. Manual, on-demand export of your own data (policies, traces, spans,
approvals, agents, audit, budgets, compliance) is free in the open-source
build. Automated or scheduled streaming to a SIEM is planned for the
commercial enterprise edition (see [LICENSING.md](https://github.com/strathon/strathon/blob/main/LICENSING.md)).

## `zsh: no matches found: strathon[langgraph]`

zsh treats square brackets as glob characters. Quote the extra:

```bash
pip install "strathon[langgraph]"
```

## Where do I report a security issue?

Do not open a public issue for security reports. See `SECURITY.md` in the
repository for responsible disclosure instructions.
