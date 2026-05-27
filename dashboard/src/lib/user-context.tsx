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

interface UserCtx { user: User | null; loading: boolean; refetch: () => void; receiverVersion: { version: string } | null; mode: "self-hosted" | "cloud"; }

const UserContext = createContext<UserCtx>({ user: null, loading: true, refetch: () => {}, receiverVersion: null, mode: "self-hosted" });

export function UserProvider({ children, mode = "self-hosted" }: { children: ReactNode; mode?: "self-hosted" | "cloud" }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [receiverVersion, setReceiverVersion] = useState<{ version: string } | null>(null);

  const refetch = async () => {
    try {
      const data = await api.get("/api/auth/me");
      setUser(data?.user || null);
    } catch {
      setUser(null);
    } finally {
      setLoading(false);
    }
    try {
      const r = await fetch("/api/health").then(r => r.ok ? r.json() : null).catch(() => null);
      if (r?.version) setReceiverVersion({ version: r.version });
    } catch {}
  };

  useEffect(() => { refetch(); }, []);

  return <UserContext.Provider value={{ user, loading, refetch, receiverVersion, mode }}>{children}</UserContext.Provider>;
}

export function useUser() { return useContext(UserContext); }
