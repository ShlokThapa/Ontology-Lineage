import { create } from "zustand";
import type { Health, SchemaInfo } from "../api/types";

export type Page = "chat" | "query" | "bloom" | "lineage" | "impact" | "pii" | "pipelines" | "schema";
export const PAGES: Page[] = ["chat", "query", "bloom", "lineage", "impact", "pii", "pipelines", "schema"];

interface PendingQuery {
  text: string;
  run: boolean;
  nonce: number;
}

interface AppState {
  page: Page;
  pageParams: Record<string, string>;
  navigate: (page: Page, params?: Record<string, string>) => void;
  syncFromHash: () => void;

  pendingQuery: PendingQuery | null;
  /** Load Cypher into the Query page editor (and optionally run it). */
  openInQuery: (text: string, run?: boolean) => void;
  clearPendingQuery: () => void;

  health: Health | null;
  setHealth: (h: Health | null) => void;
  schema: SchemaInfo | null;
  setSchema: (s: SchemaInfo | null) => void;

  showShortcuts: boolean;
  setShowShortcuts: (v: boolean) => void;
}

function parseHash(): { page: Page; params: Record<string, string> } {
  const raw = window.location.hash.replace(/^#\/?/, "");
  const [path, qs] = raw.split("?");
  const page = (PAGES as string[]).includes(path) ? (path as Page) : "query";
  const params: Record<string, string> = {};
  new URLSearchParams(qs ?? "").forEach((v, k) => (params[k] = v));
  return { page, params };
}

function toHash(page: Page, params: Record<string, string>): string {
  const qs = new URLSearchParams(params).toString();
  return `#/${page}${qs ? `?${qs}` : ""}`;
}

const initial = parseHash();

export const useApp = create<AppState>((set) => ({
  page: initial.page,
  pageParams: initial.params,
  navigate: (page, params = {}) => {
    const hash = toHash(page, params);
    if (window.location.hash !== hash) window.history.pushState(null, "", hash);
    set({ page, pageParams: params });
  },
  syncFromHash: () => {
    const { page, params } = parseHash();
    set({ page, pageParams: params });
  },

  pendingQuery: null,
  openInQuery: (text, run = false) => {
    const hash = toHash("query", {});
    if (window.location.hash !== hash) window.history.pushState(null, "", hash);
    set({ page: "query", pageParams: {}, pendingQuery: { text, run, nonce: Date.now() } });
  },
  clearPendingQuery: () => set({ pendingQuery: null }),

  health: null,
  setHealth: (health) => set({ health }),
  schema: null,
  setSchema: (schema) => set({ schema }),

  showShortcuts: false,
  setShowShortcuts: (showShortcuts) => set({ showShortcuts }),
}));
