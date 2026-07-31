"use client";

/**
 * Small, shared table helpers: server-side sort state and per-table column
 * visibility. These are deliberately thin -- each list page keeps its own
 * hand-written <table>; these just supply the sort state, the clickable
 * header, and the column show/hide menu, so we don't grow a generic DataTable.
 */

import { useState, useEffect, useCallback } from "react";
import { Icons } from "@/components/icons";
import { Dropdown } from "@/components/ui";

export interface SortState {
  /** Active sort key (matches the receiver's allowlist), or null for default. */
  key: string | null;
  dir: "asc" | "desc";
}

/**
 * Sort state for a list table. `param` is the string to send as ?sort=,
 * or undefined when no sort is active (server uses its default order).
 * Clicking the active column flips direction; clicking a new column selects
 * it descending first (most tables want "biggest first" on first click).
 */
export function useTableSort(initial: SortState = { key: null, dir: "desc" }) {
  const [sort, setSort] = useState<SortState>(initial);
  const toggle = useCallback((key: string) => {
    setSort((s) =>
      s.key === key
        ? { key, dir: s.dir === "desc" ? "asc" : "desc" }
        : { key, dir: "desc" },
    );
  }, []);
  const param = sort.key ? `${sort.key}:${sort.dir}` : undefined;
  return { sort, toggle, param };
}

/** A clickable table header cell that shows the current sort direction. */
export function SortableTh({
  label,
  sortKey,
  sort,
  onSort,
  align = "left",
}: {
  label: string;
  sortKey: string;
  sort: SortState;
  onSort: (key: string) => void;
  align?: "left" | "right";
}) {
  const active = sort.key === sortKey;
  return (
    <th
      onClick={() => onSort(sortKey)}
      className="sortable-th"
      aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}
      style={{ cursor: "pointer", userSelect: "none", textAlign: align, whiteSpace: "nowrap" }}
    >
      <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
        {label}
        {active ? (
          sort.dir === "asc" ? <Icons.ArrowUp size={12} /> : <Icons.ArrowDown size={12} />
        ) : (
          <Icons.ChevronsUpDown size={12} style={{ opacity: 0.35 }} />
        )}
      </span>
    </th>
  );
}

/**
 * Per-table column visibility, persisted in localStorage under `storageKey`.
 * `columns` is the ordered list of { id, label }; `always` lists ids that
 * cannot be hidden (e.g. the primary identifier column).
 */
export function useColumnVisibility(
  storageKey: string,
  columns: { id: string; label: string }[],
  always: string[] = [],
) {
  const allIds = columns.map((c) => c.id);
  const [hidden, setHidden] = useState<Set<string>>(new Set());

  // Load persisted preference once on mount.
  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(storageKey);
      if (raw) {
        const arr = JSON.parse(raw) as string[];
        // eslint-disable-next-line react-hooks/set-state-in-effect -- loads persisted column visibility from localStorage on mount
        setHidden(new Set(arr.filter((id) => allIds.includes(id) && !always.includes(id))));
      }
    } catch {
      /* ignore malformed/absent storage */
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [storageKey]);

  const persist = useCallback(
    (next: Set<string>) => {
      setHidden(next);
      try {
        window.localStorage.setItem(storageKey, JSON.stringify([...next]));
      } catch {
        /* storage may be unavailable; visibility still works for the session */
      }
    },
    [storageKey],
  );

  const toggle = useCallback(
    (id: string) => {
      if (always.includes(id)) return;
      persist(
        (() => {
          const next = new Set(hidden);
          if (next.has(id)) next.delete(id);
          else next.add(id);
          return next;
        })(),
      );
    },
    [hidden, always, persist],
  );

  const isVisible = useCallback((id: string) => !hidden.has(id), [hidden]);

  const menu = (
    <Dropdown
      align="right"
      width={200}
      trigger={({ toggle: t }) => (
        <button className="btn ghost sm" onClick={t} aria-label="Show or hide columns">
          <Icons.Settings size={14} /> Columns
        </button>
      )}
      items={columns.map((c) => ({
        icon: isVisible(c.id) ? <Icons.Check size={14} /> : <span style={{ width: 14, display: "inline-block" }} />,
        label: c.label + (always.includes(c.id) ? "  (required)" : ""),
        onClick: () => toggle(c.id),
      }))}
    />
  );

  return { isVisible, toggle, menu };
}
