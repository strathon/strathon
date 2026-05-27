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

interface UserCtx { user: User | null; loading: boolean; refetch: () => void; }

const UserContext = createContext<UserCtx>({ user: null, loading: true, refetch: () => {} });

export function UserProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  const refetch = async () => {
    try {
      const data = await api.get("/api/auth/me");
      setUser(data?.user || null);
    } catch {
      setUser(null);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { refetch(); }, []);

  return <UserContext.Provider value={{ user, loading, refetch }}>{children}</UserContext.Provider>;
}

export function useUser() { return useContext(UserContext); }
