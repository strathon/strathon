# PII redaction

Strathon redacts personally-identifiable information from span
attributes at ingest time, before spans land in Postgres and before
webhook payloads are assembled. Default-on for new projects.

## The model

Two layers, applied in order on every incoming span:

**Layer 1 — key-based actions.** Some attribute keys should never carry
their value into the trace store regardless of content. Authorization
headers are always credentials; `user.email` is always PII. Operators
declare these as `{attribute_key: action}` pairs and the redactor
applies them by name.

**Layer 2 — value-based pattern matching.** For free-text attributes
like `strathon.tool.args`, we don't know what's inside, so we scan the
value for known PII patterns (emails, credit cards, SSNs, API keys,
etc.) and apply the configured action per entity.

Both layers run after the policy evaluator has seen the unredacted
content, so a policy like
`attrs["strathon.tool.args"].contains("@competitor.com")` still fires
even though the email gets redacted to `[EMAIL_ADDRESS]` on
persistence. Match expressions work on raw content; storage is
sanitized. This property is the firewall semantic Strathon protects.

## Default entities

| Entity name      | What it catches | Validator |
|------------------|-----------------|-----------|
| `API_KEY`        | OpenAI (`sk-...`), Stripe (`sk_live_...`, `pk_live_...`), GitHub PAT (`ghp_...`, `github_pat_...`), Standard Webhooks (`whsec_...`), Slack (`xox*-...`), AWS access key (`AKIA...`), Google API key (`AIza...`), JWTs (`eyJ...`) | none |
| `EMAIL_ADDRESS`  | Standard `local@domain.tld` form, TLD 2+ chars | none |
| `CREDIT_CARD`    | 13-19 digit number, optionally hyphenated or spaced | Luhn check rejects ~90% of false positives |
| `US_SSN`         | `XXX-XX-XXXX` with hyphens | none |
| `PHONE_NUMBER`   | US-style: `(XXX) XXX-XXXX`, `XXX-XXX-XXXX`, `XXX.XXX.XXXX` | none |
| `IP_ADDRESS`     | IPv4 dotted-quad | none |

Order matters. `API_KEY` runs first because keys are catastrophic to
leak. `CREDIT_CARD` runs before `PHONE_NUMBER` so 16-digit card numbers
don't get misclassified. The entity names match Microsoft Presidio's
vocabulary so operators who later upgrade to a Presidio sidecar (a v2
plan) keep their existing strategy config working.

## Actions

| Action   | Effect                                          | Where it's valid |
|----------|-------------------------------------------------|------------------|
| `redact` | Replace match with `[ENTITY_NAME]`              | Layer 1, Layer 2 |
| `mask`   | Replace all but last 4 chars with `*`           | Layer 1, Layer 2 |
| `hash`   | SHA-256, write `[ENTITY:12_HEX_CHARS]`          | Layer 1, Layer 2 |
| `delete` | Drop the attribute entirely                     | Layer 1 only     |

`hash` is the action to pick when you still want to do analytics on
the value (count distinct, group-by) without retaining the cleartext.
Two runs against the same input produce the same hash, so joins work.

`delete` is only meaningful at the key level — you can't "delete part
of a value", you delete the whole attribute. At the value-pattern
level, redact / mask / hash are the choices.

## Configuration

Per-project columns on `project_settings`:

| Column                          | Type   | Default       | Purpose |
|---------------------------------|--------|---------------|---------|
| `pii_redaction_enabled`         | BOOL   | `true`        | Master switch |
| `pii_redaction_strategy`        | JSONB  | `{}`          | `{entity_name: action}` per-entity. Missing entries default to `redact`. |
| `pii_redaction_key_actions`     | JSONB  | `{}`          | `{attribute_key: action}` per-key. Empty = no key-level redaction. |
| `pii_attribute_allowlist`       | JSONB  | `[]`          | If non-empty, ONLY listed attribute keys survive. Deny-by-default mode. |
| `pii_redaction_patterns`        | JSONB  | `[]`          | Operator-supplied extra regexes. Either `[{"name": "...", "regex": "..."}]` or shorthand `["regex", ...]`. |

### Example: hash emails, mask credit cards, delete auth headers

```sql
UPDATE project_settings SET
    pii_redaction_strategy = '{"EMAIL_ADDRESS": "hash", "CREDIT_CARD": "mask"}'::jsonb,
    pii_redaction_key_actions = '{"http.request.header.authorization": "delete"}'::jsonb
WHERE project_id = '...';
```

After this:
- `alice@example.com` becomes `[EMAIL_ADDRESS:5d3b7c2a8e1f]` everywhere it appears in attribute values. Analytics that group by this hash get one bucket per real email.
- A credit card becomes `************4242`, keeping the last 4 for fraud-checking.
- The Authorization header attribute is dropped before the span hits Postgres.

### Example: allowlist mode (strongest privacy posture)

```sql
UPDATE project_settings SET
    pii_attribute_allowlist = '["http.method", "service.name", "gen_ai.tool.name", "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens"]'::jsonb
WHERE project_id = '...';
```

Every other attribute is dropped at ingest. This is the
compliance-grade setting: it doesn't matter what new attribute someone
adds in a future SDK version — if it's not on the list, it doesn't get
stored.

### Example: custom pattern for an internal ID

```sql
UPDATE project_settings SET
    pii_redaction_patterns = '[{"name": "INTERNAL_ACCT", "regex": "\\bACCT-[0-9]{8}\\b"}]'::jsonb
WHERE project_id = '...';
```

Custom patterns run after the defaults and use the same per-entity
action model. Add the entity name to `pii_redaction_strategy` if you
want a non-`redact` action (`{"INTERNAL_ACCT": "hash"}`).

## Disabling

```sql
UPDATE project_settings SET pii_redaction_enabled = false
WHERE project_id = '...';
```

Set the master switch off and ingest becomes a pure passthrough. Spans
land in Postgres with their raw content. Useful in tightly controlled
internal environments where the trace store itself is already a secure
zone and the redaction cost (small but non-zero) is unwanted.

## Interaction with policies

A subtle but critical property: **policy evaluation runs on the
unredacted span**. The redactor sees the attributes AFTER
`evaluate_for_span` has run, so the match-expression engine always
sees the original content.

This is why the property holds:

```
# Policy:
#   match_expression: attrs["strathon.tool.args"].contains("@competitor.com")
#   action: block

# Span ingested with: strathon.tool.args = "to alice@competitor.com"

# Sequence:
# 1. policy eval sees raw text, MATCHES
# 2. SDK has already enforced the block (this is server-side recording)
# 3. redactor rewrites attrs["strathon.tool.args"] = "to [EMAIL_ADDRESS]"
# 4. spans table row stores the redacted version
# 5. webhook payload (if alert policy) carries the redacted version
```

Without this ordering, the redactor would rewrite the email before
the match expression saw it and the firewall would silently break.

## Performance

For a 10 KB `strathon.tool.args` value with 6 default patterns, a
single scan completes well under 1 ms on modest hardware. Each span is
scanned once and patterns are compiled at module import. The
project's redaction config is loaded once per ingest batch (not per
span), so a thousand-span OTLP payload incurs one DB read.

If the redactor ever becomes a hot-path bottleneck (we don't expect it
to in v1), the right next step is a Presidio sidecar container; the
entity-name vocabulary is compatible, so existing strategy config
moves over without rewrite.

## What's not covered in v1

- **PERSON / LOCATION / ORGANIZATION**: these require NER (named-entity
  recognition), which means spaCy or a Hugging Face model. The image
  bloat (~500 MB for spaCy alone) isn't worth it for v1's regex-only
  baseline. The v2 plan is a Presidio sidecar that operators opt into.
- **IPv6**: deferred. The pattern is more complex and the false-positive
  rate higher; we'd rather skip it than emit noisy matches.
- **International phone numbers**: same reason. The shape varies too
  much by region for one regex to be useful without context.
- **Reversible tokenization**: the "anonymize → LLM → de-anonymize"
  proxy pattern used by tools like PII Shield. Strathon sits beside the
  LLM call, not in front of it, so reversible tokenization isn't the
  shape we need — by the time we see a span, the LLM has already seen
  the raw value.

The path to closing these gaps in v2 is documented in `docs/v2.md`
once that doc exists.
