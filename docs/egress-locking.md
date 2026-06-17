# Locking egress: make the proxy the only way out

The [egress proxy](./egress.md) enforces policy on an agent's outbound HTTP. By
default it runs in **explicit proxy** mode: the agent honors the `HTTP_PROXY`
and `HTTPS_PROXY` environment variables and routes its traffic through Strathon.
That is defense-in-depth for a cooperating agent, but a process that ignores
those variables, or a compromised one that deliberately sets them aside, can
still open a socket straight to the internet.

This page closes that gap at the **network layer** instead of relying on the
agent's cooperation. The technique is ordinary Docker networking: put the agent
on a network that has no route to the internet, and give it exactly one reachable
neighbor that does — the proxy. Now "go through the proxy" is not a request to
the agent; it is the only path that exists.

This is a deployment recipe, not a Strathon feature. It composes the egress
proxy with container isolation you control. (Fully transparent, network-level
interception that needs no agent cooperation at all is on the roadmap; until it
ships, this recipe is how you get a mandatory egress boundary today.)

## The idea in one picture

```
            internal network (no internet route)
  ┌───────────────────────────────────────────────┐
  │   agent  ──HTTP_PROXY──▶  egress-proxy          │
  └───────────────────────────────────────────│────┘
                                               │ (also on the external network)
                                               ▼
                                          the internet
```

The agent sits on an `internal` network. Docker gives an `internal` network no
gateway to the host or the internet, so the agent **cannot** reach anything
outside it. The proxy is attached to *both* the internal network and a normal
(external) network, so it is the single bridge. Any request the agent makes
either goes through the proxy or goes nowhere.

## A proxy image

The published `ghcr.io/strathon/receiver` image ships the proxy addon
(`egress_proxy.py`) and the credential patterns it uses, but it does **not**
include mitmproxy. mitmproxy is an optional dependency, not part of the
receiver runtime. So the proxy runs as its own small image: mitmproxy plus those
two files. Create `egress/Dockerfile`:

```dockerfile
FROM python:3.12-slim
RUN pip install --no-cache-dir "mitmproxy>=11.0" httpx
WORKDIR /app
# The addon and the credential patterns it imports. Copy them from the
# receiver source (the addon does `from credential_patterns import PATTERNS`,
# so both must sit together on the working directory / import path).
COPY receiver/egress_proxy.py receiver/credential_patterns.py ./
EXPOSE 8080
ENTRYPOINT ["mitmdump", "--listen-host", "0.0.0.0", "--listen-port", "8080", \
            "-s", "/app/egress_proxy.py"]
```

The addon reads `STRATHON_EGRESS_RECEIVER_URL` and `STRATHON_API_KEY` from the
environment, so no extra `mitmdump --set` flags are needed.

## Compose recipe

This extends the standard `docker-compose.yml`. The receiver, dashboard, and
Postgres are unchanged; the new parts are the two networks, the `egress-proxy`
service, and the `agent` service that depends on it.

```yaml
networks:
  # Reachable from the host / internet as usual.
  edge:
  # No gateway: containers here cannot reach the internet. The only way out
  # is a container that is ALSO attached to `edge` — the proxy.
  caged:
    internal: true

services:
  receiver:
    image: ghcr.io/strathon/receiver:latest
    networks: [edge, caged]      # reachable by the proxy for policy pulls
    # ... rest of the receiver config from docker-compose.yml ...

  egress-proxy:
    build:
      context: .
      dockerfile: egress/Dockerfile
    container_name: strathon-egress
    restart: unless-stopped
    depends_on:
      receiver:
        condition: service_healthy
    networks:
      - edge        # can reach the internet (forwards allowed traffic)
      - caged       # can be reached by the agent
    environment:
      # Where the proxy pulls the project's policies from. Inside Compose the
      # receiver is reachable by service name.
      STRATHON_EGRESS_RECEIVER_URL: http://receiver:4318
      # A real project key with policies:read scope. Do NOT use the seeded
      # dev key for anything beyond a local trial.
      STRATHON_API_KEY: ${STRATHON_EGRESS_API_KEY:?set a real API key}
    # No `ports:` block. The proxy is not published to the host; only the
    # agent (on the caged network) needs to reach it.

  agent:
    build: ./your-agent          # your agent image
    container_name: my-agent
    restart: unless-stopped
    depends_on:
      egress-proxy:
        condition: service_started
    networks:
      - caged                    # ONLY the caged network — no internet route
    environment:
      # Route all HTTP(S) through the proxy. Because the agent is on an
      # internal-only network, this is belt AND suspenders: even if these were
      # unset, there is no other route off the caged network.
      HTTP_PROXY: http://egress-proxy:8080
      HTTPS_PROXY: http://egress-proxy:8080
      # Don't proxy in-cluster calls to the receiver (if your agent's SDK
      # ships spans to it); only external traffic should transit the proxy.
      NO_PROXY: receiver,localhost,127.0.0.1
      # Trust the proxy's CA so HTTPS interception works (see "HTTPS" below).
      REQUESTS_CA_BUNDLE: /usr/local/share/ca-certificates/mitmproxy-ca.crt
      SSL_CERT_FILE: /usr/local/share/ca-certificates/mitmproxy-ca.crt
```

Bring it up with a real key set:

```bash
export STRATHON_EGRESS_API_KEY=stra_your_real_project_key
docker compose up -d
```

## HTTPS interception

Almost all real egress is HTTPS, and an HTTPS request is encrypted end to end, so
the proxy can only inspect (and therefore enforce policy on) traffic it can
decrypt. mitmproxy does this by generating its own CA and re-signing TLS
connections on the fly, so the agent must **trust that CA**.

1. Generate the CA once and mount it into both the proxy and the agent. On first
   run mitmproxy writes its CA to `~/.mitmproxy/mitmproxy-ca-cert.pem`; bake that
   file into your agent image (or mount it) at the path the env vars above point
   to.
2. The `REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE` variables cover Python's `requests`
   and most SDKs. Other runtimes have their own trust store (Node uses
   `NODE_EXTRA_CA_CERTS`, the OS store is `/etc/ssl/certs`); set whichever your
   agent's HTTP client reads.

If the agent does **not** trust the CA, its HTTPS calls fail to connect rather
than leak, a fail-closed outcome. But you want them to succeed *and* be
inspected, which is what trusting the CA gives you.

## Verifying the cage actually holds

A locked egress you never tested is a locked egress you don't have. Confirm both
halves: that allowed traffic flows through the proxy, and that nothing escapes
around it.

```bash
# 1. The agent CANNOT reach the internet directly (no proxy). This must FAIL
#    (no route), proving the internal network has no gateway:
docker compose exec agent python -c \
  "import socket; socket.setdefaulttimeout(4); socket.create_connection(('1.1.1.1', 443))"
#    Expect: a timeout / "Network is unreachable". If it connects, the agent is
#    NOT caged — check that it is on `caged` only and `caged` has internal: true.

# 2. Through the proxy, a request to a domain blocked by one of your policies
#    returns 403 with the Strathon reason header:
docker compose exec agent sh -c \
  "curl -sS -x http://egress-proxy:8080 -o /dev/null -w '%{http_code}\n' https://<a-domain-you-block>/"
#    Expect: 403. Check the proxy logs for the matching policy.

# 3. Through the proxy, an allowed request succeeds (proves normal traffic works):
docker compose exec agent sh -c \
  "curl -sS -x http://egress-proxy:8080 -o /dev/null -w '%{http_code}\n' https://<an-allowed-domain>/"
#    Expect: 200.
```

Step 1 is the one that matters most: it proves the boundary is structural, not
cooperative. If step 1 connects, every policy you write is optional from the
agent's point of view.

## Notes and limits

- **Default-deny pairs well with this.** Set the project's
  `intervention_default_action` to `block` so a request that matches no policy
  is denied. Combined with the cage, the agent can reach only the destinations
  you have explicitly allowed, by a route it cannot avoid. See
  [intervention](./intervention.md).
- **DNS.** On an internal network Docker's embedded DNS still resolves names,
  but the agent can't reach external resolvers. The proxy resolves destination
  names itself, which is what you want: name resolution for allowed hosts
  happens on the proxy side.
- **Non-HTTP egress.** This recipe governs HTTP(S). If your threat model
  includes the agent opening raw non-HTTP sockets, the internal network still
  blocks them (no route), but they are dropped, not inspected. There is no
  policy decision or audit record for traffic the proxy never sees.
- **This is configuration, not a guarantee about your whole host.** It isolates
  the agent *container*. A misconfiguration elsewhere (publishing the agent on
  another network, a host-network container) can reintroduce a route. Re-run the
  step-1 check after any change to the compose networking.
