# Licensing

Strathon is licensed under the **Apache License 2.0**, across all
open-source components: the SDK (`sdk/`), the receiver (`receiver/`), the
CLI (`cli/`), and everything else in this repository (dashboard, docs,
examples, tests).

The full license text is in [`LICENSE`](LICENSE) at the repository root and
in each shipped component (`sdk/LICENSE`, `receiver/LICENSE`,
`cli/LICENSE`). Attribution is in the `NOTICE` files.

## Why Apache 2.0?

Apache 2.0 is the modern default for infrastructure that gets embedded into
production systems (Kubernetes, OpenTelemetry, and most of the ecosystem
Strathon integrates with ship under it), for two practical reasons:

- **Explicit patent grant.** Section 3 gives every user a license to any
  patents the project's contributors hold that cover the code. Legal teams
  at larger companies frequently auto-approve Apache 2.0 where they would
  review MIT specifically for this reason.
- **One license, everywhere.** A single license across the SDK you import,
  the receiver you self-host, and the CLI you install means one review, one
  answer, no per-component analysis.

## What about commercial / enterprise features?

A future `ee/` directory (Enterprise Edition) will hold premium features
such as SSO, fine-grained RBAC, advanced policy primitives, and
multi-tenant isolation hardening. That code will ship under a commercial
license, separate from the open-source components above.

The split is by directory: `ee/` will be the only commercial part. The
SDK, receiver, and CLI stay open-source under Apache 2.0 indefinitely. We
do not intend to relicense or paywall existing functionality.

## Can I use Strathon commercially?

Yes. Apache 2.0 permits commercial use, including in closed-source
products. The license itself contains the authoritative terms — this page
is a friendly summary, not a legal substitute.

## Can I fork it?

Yes. Apache 2.0 permits forking and redistribution. If you redistribute,
Section 4 asks that you include the license text and the `NOTICE` file,
and note any files you modified.

## Contributions

By submitting a contribution, you agree that it is released under Apache
2.0, the license of the project. We use the inbound=outbound model — no
CLA, no DCO. Your existing copyright is preserved.

## Trademark

The name "Strathon" and any associated logos are not part of the
open-source license grant. Forks must use a different name unless you
have explicit written permission.

## Questions

For licensing questions not answered here, open an issue or email the
maintainers.
