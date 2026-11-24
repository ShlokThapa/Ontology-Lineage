import { X } from "lucide-react";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { api } from "../../api/client";
import type { JobRun } from "../../api/types";

export const btnPrimary =
  "rounded-md bg-accent px-3 py-1.5 text-xs font-semibold text-white hover:bg-[#0099A8] disabled:cursor-not-allowed disabled:opacity-50";
export const btnSecondary =
  "rounded-md border border-ink-600 bg-white px-3 py-1.5 text-xs text-ink-200 hover:border-accent hover:text-accent disabled:cursor-not-allowed disabled:opacity-50";
export const btnDanger =
  "rounded-md bg-red-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-red-700 disabled:cursor-not-allowed disabled:opacity-50";
export const card = "rounded-xl border border-ink-700 bg-white p-4 shadow-soft";
export const inputCls =
  "rounded-md border border-ink-600 bg-white px-2 py-1 text-xs text-ink-100 outline-none focus:border-accent focus:ring-2 focus:ring-accent/20";

const TONES: Record<string, string> = {
  succeeded: "bg-emerald-100 text-emerald-800",
  Completed: "bg-emerald-100 text-emerald-800",
  running: "bg-sky-100 text-sky-800",
  queued: "bg-sky-100 text-sky-800",
  InProgress: "bg-sky-100 text-sky-800",
  NotStarted: "bg-sky-100 text-sky-800",
  resolving: "bg-sky-100 text-sky-800",
  failed: "bg-red-100 text-red-800",
  Failed: "bg-red-100 text-red-800",
  TimedOut: "bg-red-100 text-red-800",
  Deduped: "bg-red-100 text-red-800",
  cancelled: "bg-amber-100 text-amber-800",
  Cancelled: "bg-amber-100 text-amber-800",
  Cancelling: "bg-amber-100 text-amber-800",
};

export function StatusPill({ status }: { status: string }) {
  const live = status === "running" || status === "InProgress" || status === "resolving";
  return (
    <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold ${TONES[status] ?? "bg-ink-800 text-ink-300"}`}>
      {live && <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current" aria-hidden />}
      {status}
    </span>
  );
}

export function ConfirmDialog({
  title,
  children,
  confirmLabel,
  danger,
  onConfirm,
  onCancel,
}: {
  title: string;
  children: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onCancel();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);
  return (
    <div className="fixed inset-0 z-[95] flex items-center justify-center bg-[#001689]/30" onClick={onCancel}>
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="w-[460px] max-w-[92vw] rounded-2xl border border-ink-700 bg-white p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold">{title}</h2>
          <button onClick={onCancel} aria-label="Close" className="text-ink-400 hover:text-ink-100">
            <X size={16} />
          </button>
        </div>
        <div className="space-y-2 text-xs text-ink-300">{children}</div>
        <div className="mt-4 flex justify-end gap-2">
          <button onClick={onCancel} className={btnSecondary}>
            Cancel
          </button>
          <button onClick={onConfirm} className={danger ? btnDanger : btnPrimary} autoFocus>
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

export function clip(text: string, n: number): string {
  return text.length > n ? `${text.slice(0, n - 1)}…` : text;
}

export function duration(start: number | null, end: number | null): string {
  if (!start) return "";
  const s = Math.max(0, Math.round((end ?? Date.now() / 1000) - start));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

/** The runs started from this page, polled while any is active. The server forgets them when it restarts. */
export function useRuns() {
  const [runs, setRuns] = useState<JobRun[]>([]);
  const refresh = useCallback(async () => {
    try {
      setRuns(await api.jobs.runs());
    } catch {
      /* the panels show their own errors */
    }
  }, []);
  useEffect(() => void refresh(), [refresh]);
  const active = runs.some((r) => r.status === "queued" || r.status === "running");
  useEffect(() => {
    const t = setInterval(() => void refresh(), active ? 2500 : 15000);
    return () => clearInterval(t);
  }, [active, refresh]);
  const add = useCallback((run: JobRun) => setRuns((prev) => [run, ...prev.filter((r) => r.id !== run.id)]), []);
  return { runs, refresh, add, active };
}

/** Job keys a queued or running run still has to execute (mirrors the server's guard against double starts). */
export function busyKeys(runs: JobRun[]): Set<string> {
  const out = new Set<string>();
  for (const r of runs) {
    if (r.status !== "queued" && r.status !== "running") continue;
    out.add(r.key);
    for (const s of r.steps) if (["pending", "resolving", "NotStarted", "InProgress"].includes(s.status)) out.add(s.key);
  }
  return out;
}
