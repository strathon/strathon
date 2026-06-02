"use client";
import { createContext, useContext, useEffect, useState, ReactNode } from "react";
import { api } from "./api-client";

interface User {
  email: string;
  display_name: string | null;
  role: string | null;
  project_id: string | null;
  project_name: string | null;
  mfa_enabled: boolean;
  force_password_change: boolean;
}

export interface ProjectMembership {
  id: string;
  name: string;
  slug: string;
  role: string;
}

interface UserCtx { user: User | null; projects: ProjectMembership[]; loading: boolean; refetch: () => void; receiverVersion: { version: string } | null; mode: "self-hosted" | "cloud"; }

const UserContext = createContext<UserCtx>({ user: null, projects: [], loading: true, refetch: () => {}, receiverVersion: null, mode: "self-hosted" });

export function UserProvider({ children, mode = "self-hosted" }: { children: ReactNode; mode?: "self-hosted" | "cloud" }) {
  const [user, setUser] = useState<User | null>(null);
  const [projects, setProjects] = useState<ProjectMembership[]>([]);
  const [loading, setLoading] = useState(true);
  const [receiverVersion, setReceiverVersion] = useState<{ version: string } | null>(null);

  const refetch = async () => {
    try {
      const data = await api.get("/api/auth/me");
      setUser(data?.user || null);
      setProjects(Array.isArray(data?.projects) ? data.projects : []);
    } catch {
      setUser(null);
      setProjects([]);
    } finally {
      setLoading(false);
    }
    try {
      const r = await fetch("/api/health").then(r => r.ok ? r.json() : null).catch(() => null);
      if (r?.version) setReceiverVersion({ version: r.version });
    } catch {}
  };

  useEffect(() => { refetch(); }, []);

  return <UserContext.Provider value={{ user, projects, loading, refetch, receiverVersion, mode }}>{children}</UserContext.Provider>;
}

export function useUser() { return useContext(UserContext); }
