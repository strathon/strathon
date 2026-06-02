"use client";

/**
 * Theme handling: light / dark / system.
 *
 * The stored preference is one of "light" | "dark" | "system". On first
 * visit (no stored preference) we default to "system", which resolves to
 * the OS setting via prefers-color-scheme — matching the user's device
 * rather than forcing dark. An explicit choice from the toggle or the
 * settings panel is persisted to localStorage and wins until changed.
 *
 * THEME_SCRIPT runs in <head> before first paint so the resolved theme is
 * applied without a flash of the wrong palette.
 */

export type ThemePref = "light" | "dark" | "system";
export type ResolvedTheme = "light" | "dark";

export const THEME_STORAGE_KEY = "strathon-theme";

export function getStoredTheme(): ThemePref {
  if (typeof window === "undefined") return "system";
  try {
    const v = localStorage.getItem(THEME_STORAGE_KEY);
    if (v === "light" || v === "dark" || v === "system") return v;
  } catch {}
  return "system";
}

export function systemTheme(): ResolvedTheme {
  if (typeof window === "undefined" || !window.matchMedia) return "dark";
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function resolveTheme(pref: ThemePref): ResolvedTheme {
  return pref === "system" ? systemTheme() : pref;
}

/** Reflect the given preference on <html data-theme>. */
export function applyTheme(pref: ThemePref): void {
  if (typeof document === "undefined") return;
  document.documentElement.dataset.theme = resolveTheme(pref);
}

/** Persist an explicit preference and apply it immediately. */
export function setTheme(pref: ThemePref): void {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, pref);
  } catch {}
  applyTheme(pref);
  window.dispatchEvent(new CustomEvent("strathon-theme-changed", { detail: pref }));
}

/**
 * Keep the page in sync with the OS when the preference is "system".
 * Returns a cleanup function. No-op when an explicit theme is chosen.
 */
export function watchSystemTheme(): () => void {
  if (typeof window === "undefined" || !window.matchMedia) return () => {};
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  const onChange = () => {
    if (getStoredTheme() === "system") applyTheme("system");
  };
  mq.addEventListener("change", onChange);
  return () => mq.removeEventListener("change", onChange);
}

/**
 * Inline script for the document head. Resolves the stored preference (or
 * the OS default) and sets data-theme before React hydrates, so there is
 * no flash of an incorrect theme on load.
 */
export const THEME_SCRIPT = `(function(){try{var k=localStorage.getItem("${THEME_STORAGE_KEY}");var p=(k==="light"||k==="dark"||k==="system")?k:"system";var dark=p==="dark"||(p==="system"&&window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches);document.documentElement.dataset.theme=dark?"dark":"light";}catch(e){document.documentElement.dataset.theme="dark";}})();`;
