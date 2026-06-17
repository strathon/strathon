# Egress Proxy

The egress proxy intercepts an agent's outbound HTTP traffic and enforces
Strathon policy on it. It runs as a [mitmproxy](https://mitmproxy.org) addon in
front of the agent process, so it catches network calls the agent makes
directly (raw HTTP, tools that aren't instrumented by the SDK, calls through
libraries you don't control).

```
agent process  ->  mitmproxy + Strathon addon  ->  internet
                   (credential scan + policy)
```

## How it fits

The egress proxy is one of Strathon's three enforcement layers. It governs an
agent's **raw outbound HTTP** — the network calls the in-process SDK can't see:
uninstrumented tools, direct HTTP, calls through libraries you don't control.
The SDK governs framework tool calls, the MCP gateway governs MCP-routed calls,
and the egress proxy is the network-layer catch-all underneath both.

Strathon's posture is that this layer is **recommended on** in any deployment you
can run it. The one difference from the other two layers: it cannot enable
itself. It needs a separate proxy process, HTTPS interception requires the agent
to trust mitmproxy's CA, and traffic has to be routed to it. So the model is:
set it up with one command, then verify it is actually in the traffic path (see
"Verifying the proxy is in the path" below) rather than assume it. In the
explicit-proxy mode shipped today it enforces on all traffic that honors the
proxy variables; network-level transparent interception that an agent cannot opt
out of is on the roadmap. In the meantime you can make the proxy mandatory
*as deployed* by isolating the agent on a network whose only route out is the
proxy — see [Locking egress](egress-locking.md).

It does two things on every request, and one on every response:

- **Request body credential scan.** If the outbound request body contains a
  secret matching the credential-pattern library (the same 70+ patterns used
  everywhere in Strathon), the request is blocked with a `403` and an
  `X-Strathon-Block-Reason: credential-leak` header. This stops an agent from
  exfiltrating a key it shouldn't have.
- **Policy evaluation.** The request is mapped to a span-shaped context
  (tool name `http.<method>`, the URL in attributes) and evaluated against the
  project's enabled policies. A matching `block` policy returns `403` with
  `X-Strathon-Block-Reason: policy`.
- **Response credential scan.** Response bodies are scanned and any matched
  secrets are redacted before reaching the agent, with an `X-Strathon-Redacted`
  header recording the count.

## Running it

Install mitmproxy (bundled in the `proxy` extra) and start the addon:

```bash
pip install "strathon[proxy]"      # or: pip install mitmproxy

mitmdump -s receiver/egress_proxy.py \
  --set strathon_url=http://localhost:4318 \
  --set strathon_key=$STRATHON_API_KEY
```

Then point the agent's process at the proxy:

```bash
export HTTP_PROXY=http://localhost:8080
export HTTPS_PROXY=http://localhost:8080
python my_agent.py
```

(For HTTPS interception the agent must trust mitmproxy's CA certificate; see
the mitmproxy docs for `mitmproxy-ca-cert.pem` setup.)

## Verifying the proxy is in the path

An egress proxy that is configured but not actually intercepting traffic is
worse than no proxy: it implies protection that isn't there. Because the proxy
relies on the agent honoring `HTTP_PROXY`/`HTTPS_PROXY`, a missing or unset
variable silently routes traffic around it.

So Strathon treats "in the path" as something to verify, not assume. On startup
the addon logs the listen address and the policy-pull target, and you can
confirm interception end to end by making a request that a policy is known to
block and checking for the `403` with `X-Strathon-Block-Reason`. Treat the proxy
as active only once you have seen it block or redact a known request; if you have
not, assume the agent's traffic is bypassing it and check the proxy-variable and
CA-trust setup. Do not rely on a configuration flag alone to tell you the proxy
is enforcing.

## How policy evaluation works (pull model)

The addon **pulls** the project's enabled policies from `GET /v1/policies`
(the same endpoint the SDK uses) and evaluates CEL **locally** on each request.
There is no per-request round-trip to the receiver, so request latency does not
depend on receiver availability and a slow receiver cannot stall agent traffic.
The policy set is pulled when the proxy starts (and whenever its mitmproxy
options change); to pick up policy edits, restart the proxy.

The policy match expression sees the request as:

```
attrs["strathon.tool.name"]  == "http.post"   # http.<method>, lowercased
attrs["strathon.http.url"]   == "https://..."  # the full request URL
```

So a policy like `attrs["strathon.tool.name"] == "http.post"` blocks all
outbound POSTs, and `attrs["strathon.http.url"].contains("evil.com")` blocks a
specific destination.

## Fail-closed on the policy path

If local policy evaluation raises (for example, the policy engine module is
unavailable in the proxy process), the request is **blocked**, not allowed. A
security control that allowed traffic when its evaluation failed would be a
bypass. Credential scanning runs independently and is unaffected.

## Deployment constraint

Because policy evaluation happens locally inside the mitmproxy process, that
process must be able to import Strathon's policy engine
(`policies.evaluate_for_span` and `credential_patterns`). Running the addon
from a checkout that has the `receiver/` package importable (as in the
`mitmdump -s receiver/egress_proxy.py` invocation above) satisfies this. If you
package the proxy separately, include the policy-engine module on its
`PYTHONPATH`. Credential scanning alone works without the receiver package only
if `credential_patterns` is importable.

## Egress proxy vs SDK vs MCP gateway

Three enforcement surfaces, used for different traffic:

- **SDK instrumentation**: in-process, at the tool-call boundary inside an
  agent framework. Can substitute tool results (full steer/throttle).
- **MCP gateway** (`/v1/mcp/proxy`): at the network boundary in front of an
  MCP server.
- **Egress proxy**: at the network boundary for arbitrary outbound HTTP the
  agent makes, regardless of framework or protocol.

Use the one(s) matching how your agent reaches the outside world; they compose.

## Related

- [Scope and limitations](scope.md): explicit vs transparent mode, honestly
- [MCP gateway](mcp.md): the same enforcement for MCP-routed tools
- [Runtime intervention](intervention.md): the policy language and actions
