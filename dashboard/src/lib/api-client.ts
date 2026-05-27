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
  if (res.status === 401) {
    if (typeof window !== "undefined") {
      window.location.href = "/login?expired=true";
    }
    throw new ApiError("Session expired", 401);
  }
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const msg = data?.error?.message || data?.detail || `HTTP ${res.status}`;
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
  const [loading, setLoading] = useState(!!path);

  const refetch = useCallback(async () => {
    if (!path) return;
    setLoading(true);
    setError(null);
    try {
      const result = await api.get(path, params);
      setData(result);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  }, [path, JSON.stringify(params), ...deps]);

  useEffect(() => { refetch(); }, [refetch]);
  return { data, error, loading, refetch };
}
