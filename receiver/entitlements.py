"""Feature entitlements: the single place that maps a plan to the features
it unlocks.

This is deliberately a scaffold. Today every feature listed here is granted
in every mode, so nothing is gated — the open-source build is fully featured.
Its purpose is structural: when enterprise (``ee/``) features land, the gate
lives here and only here. Code asks ``has_entitlement("sso")`` rather than
scattering ``if settings.is_cloud`` / license checks across the codebase.

Design separates a binary feature map from the call sites: add a feature to
a plan by editing one map.

Plans:
- ``oss``        — self-hosted open-source (default). Fully featured today.
- ``ee``         — self-hosted with an enterprise license key.
- ``cloud``      — the hosted multi-tenant deployment.

When ee/ features are built, move them out of ``_ALWAYS_GRANTED`` into the
per-plan sets and have the relevant code path call ``has_entitlement``.
"""

from __future__ import annotations

from typing import Literal

from config import get_settings

Plan = Literal["oss", "ee", "cloud"]

# Known entitlement keys. Listing a feature here does not gate it; the plan
# maps below decide access. Keys are stable identifiers used by call sites.
ENTITLEMENTS = (
    "sso",                  # SSO/SAML/OIDC login (ee/cloud)
    "scim_provisioning",    # SCIM user provisioning (ee/cloud)
    "custom_roles",         # roles beyond the four fixed ones (ee/cloud)
    "siem_export",          # automated/scheduled export to a SIEM (ee/cloud)
    "ha_config",            # high-availability deployment configs (ee)
    "audit_log",            # tamper-evident audit log (all plans)
    "data_export",          # manual on-demand data export (all plans)
    "policy_engine",        # CEL policy enforcement (all plans)
    "framework_instrumentation",  # SDK framework adapters (all plans)
)

# Features every plan gets today. The open-source build is fully featured;
# the enterprise-only keys above are listed but not yet implemented, so they
# are intentionally absent here and from the per-plan sets until built.
_ALWAYS_GRANTED: frozenset[str] = frozenset(
    {
        "audit_log",
        "data_export",
        "policy_engine",
        "framework_instrumentation",
    }
)

# Per-plan additions on top of _ALWAYS_GRANTED. Empty today: the enterprise
# features (sso, scim, custom_roles, siem_export, ha_config) are not yet
# implemented, so no plan grants them. When built, add the key to the right
# plan(s) here — the only change needed to gate it.
_PLAN_GRANTS: dict[Plan, frozenset[str]] = {
    "oss": frozenset(),
    "ee": frozenset(),
    "cloud": frozenset(),
}


def current_plan() -> Plan:
    """Resolve the active plan from deployment mode.

    Self-host without a license is ``oss``; with a license key it is ``ee``;
    the hosted deployment is ``cloud``. License-key validation is an ee/
    concern and is not implemented in the open-source build, so self-host
    resolves to ``oss`` here.
    """
    settings = get_settings()
    if settings.is_cloud:
        return "cloud"
    return "oss"


def has_entitlement(feature: str, plan: Plan | None = None) -> bool:
    """Return True if the given (or current) plan unlocks ``feature``.

    Unknown feature keys return False (fail closed). Today every shipped
    feature is in ``_ALWAYS_GRANTED`` so this returns True for them on any
    plan; enterprise keys return False everywhere until implemented.
    """
    if feature not in ENTITLEMENTS:
        return False
    if feature in _ALWAYS_GRANTED:
        return True
    plan = plan or current_plan()
    return feature in _PLAN_GRANTS.get(plan, frozenset())
