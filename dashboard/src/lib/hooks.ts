"use client";
import { useState, useEffect } from "react";

/** Flips true → false after `ms`. Demonstrates skeleton states on mount. */
export function useFakeLoad(ms = 400) {
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    const t = setTimeout(() => setLoading(false), ms);
    return () => clearTimeout(t);
  }, [ms]);
  return loading;
}
