import { ChevronDown, ChevronRight, ClipboardCheck } from "lucide-react";
import { useState } from "react";
import type { JobRun } from "../../api/types";
import { StatusPill, btnSecondary, card, duration } from "./shared";

const num = (o: Record<string, unknown>, k: string): number => (typeof o[k] === "number" ? (o[k] as number) : 0);

function Fig({ label, value, tone }: { label: string; value: number | string; tone?: "good" | "warn" }) {
  const color = tone === "good" ? "text-emerald-700" : tone === "warn" ? "text-amber-700" : "text-ink-100";
  return (
    <div className="rounded-md bg-ink-950 px-2.5 py-1.5">
      <div className="text-[10px] uppercase tracking-wide text-ink-400">{label}</div>
      <div className={`text-sm font-semibold ${color}`}>{value}</div>
    </div>
  );
}

export function RunCard({
  run,
  onCancel,
  onReview,
}: {
  run: JobRun;
  onCancel?: (run: JobRun) => void;
  onReview?: (runId: string) => void;
}) {
  const [showLog, setShowLog] = useState(false);
  const active = run.status === "queued" || run.status === "running";
  const result = run.result;
  const s = result?.summary ?? {};
  const pct = run.progress && run.progress.total ? Math.round((run.progress.done / run.progress.total) * 100) : null;

  return (
    <div className={card}>
      <div className="flex flex-wrap items-center gap-2">
        <div className="min-w-0 flex-1">
          <div className="truncate text-xs font-semibold text-ink-100">{run.title}</div>
          <div className="text-[11px] text-ink-400">
            started {new Date(run.started_at * 1000).toLocaleTimeString()} · {duration(run.started_at, run.finished_at)}
          </div>
        </div>
        <StatusPill status={run.cancel_requested && active ? "Cancelling" : run.status} />
        {active && onCancel && run.kind !== "mapping" && (
          <button onClick={() => onCancel(run)} disabled={run.cancel_requested} className={btnSecondary}>
            Cancel
          </button>
        )}
      </div>

      {pct !== null && (
        <div className="mt-2" aria-label={`Progress ${pct}%`}>
          <div className="h-1.5 overflow-hidden rounded-full bg-ink-800">
            <div className="h-full rounded-full bg-accent transition-all" style={{ width: `${pct}%` }} />
          </div>
          <div className="mt-0.5 text-[11px] text-ink-400">
            {run.progress?.done} of {run.progress?.total} chunks
          </div>
        </div>
      )}

      {run.steps.length > 0 && (
        <ol className="mt-3 space-y-1">
          {run.steps.map((st, i) => (
            <li key={`${st.key}-${i}`} className="flex flex-wrap items-start gap-2 text-xs">
              <span className="w-4 text-right text-ink-400">{i + 1}.</span>
              <span className="min-w-0 flex-1">
                <span className="text-ink-200">{st.title}</span>
                {st.message && <span className="ml-2 text-[11px] text-red-700">{st.message}</span>}
              </span>
              <StatusPill status={st.status} />
            </li>
          ))}
        </ol>
      )}

      {run.error && <div className="mt-2 rounded-md border border-red-300 bg-red-50 px-2.5 py-1.5 text-xs text-red-800">{run.error}</div>}

      {result && (
        <div className="mt-3 space-y-2">
          <div className="text-xs text-ink-200">
            {result.dry_run ? (
              <span className="font-semibold text-emerald-700">Dry run: nothing was written to Neo4j.</span>
            ) : (
              <span className="font-semibold text-amber-700">
                Written: {result.written?.created ?? 0} link(s) created, {result.written?.superseded ?? 0} superseded. Run id{" "}
                <code className="font-mono">{result.run_id}</code>
              </span>
            )}
          </div>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <Fig label="Chunks" value={num(s, "chunks_processed")} />
            <Fig label="Links to create" value={num(s, "to_create")} />
            <Fig label="Accepted" value={num(s, "to_create_accepted")} tone="good" />
            <Fig label="Need review" value={num(s, "to_create_proposed")} tone="warn" />
            <Fig label="Already mapped" value={num(s, "skipped_existing")} />
            <Fig label="Rejected before" value={num(s, "skipped_previously_rejected")} />
            <Fig label="Dropped by checks" value={num(s, "links_rejected_by_code")} />
            <Fig label="Chunks failed" value={num(s, "chunks_failed")} tone={num(s, "chunks_failed") ? "warn" : undefined} />
          </div>
          <div className="text-[11px] text-ink-400">
            tokens {Object.entries((s.tokens as Record<string, number> | undefined) ?? {}).map(([k, v]) => `${k} ${v}`).join(" · ") || "none"} · {String(s.seconds ?? "?")}s ·{" "}
            {s.independent_verification ? "verified by a different model" : "verified by the same model (not independent: links stay proposed)"}
          </div>
          <div className="break-all text-[11px] text-ink-400">report: <code className="font-mono">{result.report_path}</code></div>
          {onReview && (
            <button onClick={() => onReview(result.run_id)} className={btnSecondary}>
              <ClipboardCheck size={12} className="mr-1 inline" /> {result.dry_run ? "Review before writing" : "Review these links"}
            </button>
          )}
        </div>
      )}

      {run.log.length > 0 && (
        <div className="mt-2">
          <button onClick={() => setShowLog((o) => !o)} className="flex items-center gap-1 text-[11px] text-ink-400 hover:text-ink-100">
            {showLog ? <ChevronDown size={12} /> : <ChevronRight size={12} />} Log ({run.log.length})
          </button>
          {showLog && <pre className="mt-1 max-h-48 overflow-auto rounded-md bg-ink-950 p-2 font-mono text-[11px] leading-relaxed text-ink-300">{run.log.join("\n")}</pre>}
        </div>
      )}
    </div>
  );
}
