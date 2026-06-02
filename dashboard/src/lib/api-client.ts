"use client";
import { useState, useEffect, useCallback } from "react";

class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function handleResponse(res: Response) {
  const data = await res.json().catch(() => null);
  // Pull a human-readable message out of the response. FastAPI returns
  // `detail` as a plain string for normal errors but as an array of
  // {loc,msg,type} objects for 422 validation errors — flatten those to
  // their messages instead of rendering "[object Object]".
  const extractMsg = (): string | null => {
    if (data?.error?.message) return data.error.message;
    const d = data?.detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) {
      const msgs = d.map((e) => (typeof e === "string" ? e : e?.msg)).filter(Boolean);
      if (msgs.length) return msgs.join(", ");
    }
    return null;
  };

  if (res.status === 401) {
    const msg = extractMsg();
    // A 401 from the login endpoint means bad credentials, not an expired
    // session — surface the real message and don't redirect.
    if (res.url.includes("/api/auth/login")) {
      throw new ApiError(msg || "Incorrect email or password", 401);
    }
    if (typeof window !== "undefined") {
      window.location.href = "/login?expired=true";
    }
    throw new ApiError(msg || "Session expired", 401);
  }
  if (!res.ok) {
    const msg = extractMsg() || `HTTP ${res.status}`;
    throw new ApiError(msg, res.status);
  }
  return data;
}

export const api = {
  async get(path: string, params?: Record<string, string>) {
    const url = new URL(path, window.location.origin);
    if (params) Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, v));
    return handleResponse(await fetch(url.toString(), { credentials: "same-origin" }));
  },
  async post(path: string, body?: unknown) {
    return handleResponse(await fetch(path, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    }));
  },
  async patch(path: string, body?: unknown) {
    return handleResponse(await fetch(path, {
      method: "PATCH", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    }));
  },
  async del(path: string) {
    return handleResponse(await fetch(path, { method: "DELETE", credentials: "same-origin" }));
  },
};

export function useApi<T = unknown>(
  path: string | null,
  params?: Record<string, string>,
  deps: unknown[] = []
) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<number | null>(null);
  const [loading, setLoading] = useState(!!path);

  const refetch = useCallback(async () => {
    if (!path) return;
    setLoading(true);
    setError(null);
    setStatus(null);
    try {
      const result = await api.get(path, params);
      setData(result);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unknown error");
      setStatus(e instanceof ApiError ? e.status : null);
    } finally {
      setLoading(false);
    }
  }, [path, JSON.stringify(params), ...deps]);

  useEffect(() => { refetch(); }, [refetch]);
  return { data, error, status, loading, refetch };
}
