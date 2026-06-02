"use client";

/**
 * Role-derived UI permissions.
 *
 * Mirrors the receiver's fixed role hierarchy (owner > admin > operator >
 * viewer) so the dashboard only offers write actions a role can actually
 * perform. The server enforces the real check on every request; this hook
 * exists so a viewer never sees a "Create key" button that would 403.
 *
 * Capability → role mapping (kept in step with receiver/rbac.py ROLE_SCOPES):
 *   api_keys:write           owner, admin
 *   policies:write           owner, admin, operator
 *   project_settings:write   owner, admin, operator
 *   member management        owner, admin            (stricter than the
 *                            backend's project_settings:write, so the UI
 *                            never surfaces team controls that confuse
 *                            operators)
 */

import { useUser } from "./user-context";

export interface Permissions {
  role: string | null;
  isOwner: boolean;
  isAdmin: boolean; // owner or admin
  canManageApiKeys: boolean;
  canWritePolicies: boolean;
  canEditSettings: boolean;
  canManageMembers: boolean;
  isReadOnly: boolean; // viewer
}

export function permissionsForRole(role: string | null): Permissions {
  const r = role || "";
  const isOwner = r === "owner";
  const isAdmin = r === "owner" || r === "admin";
  const canWrite = r === "owner" || r === "admin" || r === "operator";
  return {
    role,
    isOwner,
    isAdmin,
    canManageApiKeys: isAdmin,
    canWritePolicies: canWrite,
    canEditSettings: canWrite,
    canManageMembers: isAdmin,
    isReadOnly: r === "viewer",
  };
}

export function usePermissions(): Permissions {
  const { user } = useUser();
  return permissionsForRole(user?.role ?? null);
}
