# Licensing

Strathon ships under two open-source licenses, one per component:

| Component   | License       | Why                                                    |
|-------------|---------------|--------------------------------------------------------|
| `sdk/`      | Apache 2.0    | Explicit patent grant. Standard for client libraries.  |
| `receiver/` | MIT           | Maximum permissiveness. Trivial to self-host or embed. |

The full license texts are at `sdk/LICENSE` (Apache 2.0) and
`receiver/LICENSE` (MIT). The root `LICENSE` file is a pointer to the
split.

## Why two licenses?

**The SDK is what users import into their own code.** Apache 2.0 is the
modern industry default for client libraries — it gives users an explicit
patent grant (Section 3 of the license) that MIT doesn't address. If
Strathon ever holds patents related to agent observability or runtime
intervention, Apache 2.0 contractually allows downstream use without
risk. Legal teams at larger companies frequently auto-approve Apache 2.0
where they would block MIT specifically for this reason.

OpenTelemetry, LangChain, Kubernetes, and most other infrastructure
projects that get embedded into production codebases at scale ship under
Apache 2.0 for the same reasons.

**The receiver is what users self-host.** MIT keeps the self-hosting
story friction-free. You can run it, fork it, modify it, embed it into a
commercial product, redistribute it, all without NOTICE-file obligations
or attribution requirements beyond the copyright line. The receiver
exists to be deployed, and MIT is the most deploy-friendly license.

## What about commercial / enterprise features?

A future `ee/` directory (Enterprise Edition) will hold premium features
such as SSO, fine-grained RBAC, advanced policy primitives, and
multi-tenant isolation hardening. That code will ship under a commercial
license, separate from the open-source components above.

The split is by directory: `ee/` will be the only commercial part. The
SDK and receiver core stay open-source under their current licenses
indefinitely. We do not intend to relicense or paywall existing
functionality.

## Can I use Strathon commercially?

Yes. Both Apache 2.0 and MIT permit commercial use, including in closed-
source products. The licenses themselves contain the authoritative terms
— this section is a friendly summary, not a legal substitute.

## Can I fork it?

Yes. Both licenses permit forking and redistribution under the same
terms. If you fork the SDK and modify it, you must include the Apache
2.0 license text and (per Section 4(b)) note any modifications. If you
fork the receiver, you must include the MIT copyright notice.

## Contributions

By submitting a contribution, you agree that it can be released under
the license of the component you're contributing to: Apache 2.0 for SDK
contributions, MIT for receiver contributions. We use the inbound=outbound
model — no CLA, no DCO. Your existing copyright is preserved.

## Trademark

The name "Strathon" and any associated logos are not part of the
open-source license grant. Forks must use a different name unless you
have explicit written permission.

## Questions

For licensing questions not answered here, open an issue or email the
maintainers.
