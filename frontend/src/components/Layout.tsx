import {
  CheckCircle2,
  Database,
  GitFork,
  Info,
  Keyboard,
  MessageSquare,
  Play,
  Network,
  RefreshCw,
  ShieldAlert,
  Sparkles,
  SquareTerminal,
  Workflow,
  X,
  XCircle,
  AlertTriangle,
  Flower2,
} from "lucide-react";
import { EXPLORER_MENU_LABEL } from "../bloom/constants";
import { useEffect, useState } from "react";
import { api } from "../api/client";
import { type Page, useApp } from "../store/app";
import { toast, useToasts } from "../store/toasts";

const NAV: { page: Page; label: string; icon: React.ReactNode }[] = [
  { page: "jobs", label: "Run jobs", icon: <Play size={20} /> },
  { page: "query", label: "Query", icon: <SquareTerminal size={20} /> },
  { page: "bloom", label: EXPLORER_MENU_LABEL, icon: <Flower2 size={20} /> },
  { page: "lineage", label: "Lineage Explorer", icon: <GitFork size={20} /> },
  { page: "impact", label: "Impact Analysis", icon: <Network size={20} /> },
  { page: "pii", label: "PII Tracker", icon: <ShieldAlert size={20} /> },
  { page: "pipelines", label: "Pipelines", icon: <Workflow size={20} /> },
  { page: "schema", label: "Schema", icon: <Database size={20} /> },
];

export function Sidebar() {
  const page = useApp((s) => s.page);
  const navigate = useApp((s) => s.navigate);
  const setShowShortcuts = useApp((s) => s.setShowShortcuts);
  return (
    <nav className="flex w-14 shrink-0 flex-col items-center gap-1 bg-gradient-to-b from-[#001689] to-[#00106a] py-3">
      <div className="mb-3 flex h-8 w-8 items-center justify-center rounded-lg bg-white/15 text-white" title="Lineage Studio">
        <GitFork size={18} />
      </div>
      {NAV.map((n) => (
        <button
          key={n.page}
          onClick={() => navigate(n.page)}
          title={n.label}
          aria-label={n.label}
          className={`group relative flex h-10 w-10 items-center justify-center rounded-lg transition ${
            page === n.page
              ? "bg-white text-[#001689] shadow-soft"
              : "text-white/70 hover:bg-white/15 hover:text-white"
          }`}
        >
          {n.icon}
          <span className="pointer-events-none absolute left-12 z-50 whitespace-nowrap rounded bg-[#001689] px-2 py-1 text-xs text-white opacity-0 shadow group-hover:opacity-100">
            {n.label}
          </span>
        </button>
      ))}
      <div className="flex-1" />
      <button
        onClick={() => setShowShortcuts(true)}
        title="Keyboard shortcuts (?)"
        className="flex h-10 w-10 items-center justify-center rounded-lg text-white/70 hover:bg-white/15 hover:text-white"
      >
        <Keyboard size={18} />
      </button>
    </nav>
  );
}

/** The chatbot button on the right edge of every page. Clicking it opens the chat full screen. */
export function ChatLauncher() {
  const chatOpen = useApp((s) => s.chatOpen);
  const setChatOpen = useApp((s) => s.setChatOpen);
  if (chatOpen) return null;
  return (
    <button
      onClick={() => setChatOpen(true)}
      aria-label="Open the AI chat"
      title="Ask the AI about your lineage and policies"
      className="group fixed bottom-6 right-6 z-[70] flex items-center gap-2 rounded-full bg-gradient-to-br from-[#001689] to-[#0099A8] py-3 pl-4 pr-5 text-sm font-semibold text-white shadow-xl ring-4 ring-white/70 transition hover:scale-105 focus:outline-none focus-visible:ring-accent"
    >
      <MessageSquare size={20} />
      Ask AI
      <Sparkles size={14} className="text-amber-300 transition group-hover:rotate-12" aria-hidden />
    </button>
  );
}

export function TopBar() {
  const health = useApp((s) => s.health);
  const setHealth = useApp((s) => s.setHealth);
  const setSchema = useApp((s) => s.setSchema);
  const [busy, setBusy] = useState(false);

  const refresh = async (reconnect = false) => {
    setBusy(true);
    try {
      const h = reconnect ? await api.reconnect() : await api.health();
      setHealth(h);
      if (h.connected) api.schema().then(setSchema).catch(() => undefined);
      if (reconnect) {
        if (h.connected) toast.success("Connected", `${h.host} / ${h.database}`);
        else toast.error("Still not connected", h.error ?? undefined);
      }
    } catch (e) {
      setHealth(null);
      if (reconnect) toast.error("Cannot reach the API", (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    void refresh();
    const t = setInterval(() => void refresh(), 60_000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const snapshot = health?.mode === "snapshot";
  const connected = !!health?.connected;
  return (
    <header className="relative flex h-14 shrink-0 items-center gap-4 border-b border-ink-700 bg-white px-4 text-xs">
      <img src="/mastek-logo.png" alt="Mastek - Trust. Value. Velocity" className="h-9 w-auto select-none" draggable={false} />
      <span className="h-7 w-px bg-ink-600" />
      <span className="font-display text-lg font-bold leading-none tracking-tight text-[#001689]">Lineage Studio</span>
      <span className="h-4 w-px bg-ink-700" />
      <span className="flex items-center gap-2" title={health?.error ?? undefined}>
        <span
          className={`inline-block h-2.5 w-2.5 rounded-full ${
            snapshot ? "bg-amber-400" : connected ? "bg-emerald-400 shadow-[0_0_6px] shadow-emerald-500" : "bg-red-500"
          }`}
        />
        {health === null ? (
          <span className="text-red-700">API unreachable</span>
        ) : snapshot ? (
          <span className="text-amber-700">Snapshot mode (offline data)</span>
        ) : connected ? (
          <span>
            Connected <span className="text-ink-400">to</span> <span className="font-mono">{health.host}</span>
          </span>
        ) : (
          <span className="text-red-700">Disconnected{health.error ? `: ${health.error}` : ""}</span>
        )}
      </span>
      {health && !snapshot && (
        <>
          <span className="text-ink-400">
            Database <span className="font-mono text-ink-200">{health.database}</span>
          </span>
          <span className="text-ink-400">
            User <span className="font-mono text-ink-200">{health.user ?? "-"}</span>
          </span>
          {health.server_version && (
            <span className="text-ink-400">
              Neo4j <span className="text-ink-200">{health.server_version}</span>
            </span>
          )}
        </>
      )}
      <div className="flex-1" />
      <a href="/docs" target="_blank" rel="noreferrer" className="text-ink-400 hover:text-ink-100">
        API docs
      </a>
      <button
        onClick={() => void refresh(true)}
        disabled={busy}
        className="flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 font-semibold text-white hover:bg-[#0099A8] disabled:opacity-50"
      >
        <RefreshCw size={13} className={busy ? "animate-spin" : ""} /> Reconnect
      </button>
      <span className="absolute inset-x-0 bottom-0 h-[3px] bg-mastek-strip" aria-hidden />
    </header>
  );
}

export function Toasts() {
  const toasts = useToasts((s) => s.toasts);
  const dismiss = useToasts((s) => s.dismiss);
  return (
    <div className="pointer-events-none fixed bottom-24 right-4 z-[100] flex w-80 flex-col gap-2">
      {toasts.map((t) => {
        const icon =
          t.kind === "success" ? (
            <CheckCircle2 size={16} className="text-emerald-600" />
          ) : t.kind === "error" ? (
            <XCircle size={16} className="text-red-600" />
          ) : t.kind === "warning" ? (
            <AlertTriangle size={16} className="text-amber-600" />
          ) : (
            <Info size={16} className="text-accent" />
          );
        return (
          <div
            key={t.id}
            role="status"
            className="pointer-events-auto flex items-start gap-2 rounded-xl border border-ink-700 bg-white px-3 py-2 text-xs shadow-soft"
          >
            <span className="mt-0.5">{icon}</span>
            <div className="min-w-0 flex-1">
              <div className="font-semibold">{t.title}</div>
              {t.message && <div className="mt-0.5 break-words text-ink-300">{t.message}</div>}
            </div>
            <button onClick={() => dismiss(t.id)} className="text-ink-400 hover:text-ink-100">
              <X size={14} />
            </button>
          </div>
        );
      })}
    </div>
  );
}

const SHORTCUTS: [string, string][] = [
  ["Ctrl / ⌘ + Enter", "Run the query in the editor"],
  ["Esc", "Clear the editor"],
  ["↑ / ↓", "Previous / next query from history (when the editor is empty or the cursor is on the first / last line)"],
  ["Ctrl + Space", "Autocomplete labels, relationship types, properties, keywords"],
  [":param name => value", "Set a query parameter (use $name in queries)"],
  [":params", "Show parameters; :params {…} replaces them"],
  [":clear", "Remove all unpinned result frames"],
  ["Double-click node", "Expand its neighbours"],
  ["Right-click node", "Expand, dismiss, show lineage, impact analysis"],
  ["?", "Show this help"],
];

export function ShortcutsHelp() {
  const open = useApp((s) => s.showShortcuts);
  const setOpen = useApp((s) => s.setShowShortcuts);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement;
      const typing = el.closest("input, textarea, [contenteditable=true], .cm-editor");
      if (e.key === "?" && !typing) {
        e.preventDefault();
        setOpen(true);
      } else if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [setOpen]);

  if (!open) return null;
  return (
    <div className="fixed inset-0 z-[90] flex items-center justify-center bg-[#001689]/30" onClick={() => setOpen(false)}>
      <div
        className="w-[560px] max-w-[92vw] rounded-2xl border border-ink-700 bg-white p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-3 flex items-center justify-between">
          <h2 className="flex items-center gap-2 text-sm font-semibold">
            <Keyboard size={16} /> Keyboard shortcuts
          </h2>
          <button onClick={() => setOpen(false)} className="text-ink-400 hover:text-ink-100">
            <X size={16} />
          </button>
        </div>
        <table className="w-full text-xs">
          <tbody>
            {SHORTCUTS.map(([k, d]) => (
              <tr key={k} className="border-t border-ink-800">
                <td className="whitespace-nowrap py-1.5 pr-4">
                  <kbd className="rounded border border-ink-600 bg-ink-800 px-1.5 py-0.5 font-mono text-[11px]">{k}</kbd>
                </td>
                <td className="py-1.5 text-ink-300">{d}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
