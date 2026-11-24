import { Play, Search } from "lucide-react";
import { useState, type ReactNode } from "react";
import { ApiError, api } from "../../api/client";
import type { JobRun, JobsConfig, MappingCheck, MappingRunOptions } from "../../api/types";
import { toast } from "../../store/toasts";
import { ReviewSection } from "./ReviewSection";
import { RunCard } from "./RunCard";
import { ConfirmDialog, btnDanger, btnPrimary, btnSecondary, card, inputCls } from "./shared";

const DEFAULTS: MappingRunOptions = {
  mode: "append", write: false, confirm_overwrite: false, only_unlinked: false, no_auto_accept: false,
  accept_self_verified: false, docs: [], limit: 10, workers: 4, use_cache: true,
};

function Check2({ label, checked, onChange, hint }: { label: string; checked: boolean; onChange: (v: boolean) => void; hint?: string }) {
  return (
    <label className="flex items-start gap-1.5 text-xs text-ink-200" title={hint}>
      <input type="checkbox" className="mt-0.5" checked={checked} onChange={(e) => onChange(e.target.checked)} /> {label}
    </label>
  );
}

export function MappingPanel({
  config,
  runs,
  onStarted,
  onCancel,
  reviewRunId,
  setReviewRunId,
}: {
  config: JobsConfig;
  runs: JobRun[];
  onStarted: (run: JobRun) => void;
  onCancel: (run: JobRun) => void;
  reviewRunId: string;
  setReviewRunId: (id: string) => void;
}) {
  const live = config.mapping.live;
  const canWrite = config.mapping.write_enabled;
  const [opts, setOpts] = useState<MappingRunOptions>(DEFAULTS);
  const [docsText, setDocsText] = useState("");
  const [check, setCheck] = useState<MappingCheck | null>(null);
  const [checking, setChecking] = useState(false);
  const [confirm, setConfirm] = useState<{ title: string; body: ReactNode; label: string; action: () => Promise<void> } | null>(null);
  const mappingRuns = runs.filter((r) => r.kind === "mapping");
  const active = mappingRuns.some((r) => r.status === "queued" || r.status === "running");
  const set = <K extends keyof MappingRunOptions>(k: K, v: MappingRunOptions[K]) => setOpts((o) => ({ ...o, [k]: v }));

  const fail = (e: unknown) => {
    const err = e instanceof ApiError ? e : null;
    toast.error(err?.code ?? "Error", err?.message ?? String(e));
  };

  const runCheck = async () => {
    setChecking(true);
    try {
      setCheck(await api.jobs.mappingCheck());
    } catch (e) {
      fail(e);
    } finally {
      setChecking(false);
    }
  };

  const start = async (write: boolean) => {
    const body: MappingRunOptions = {
      ...opts, write, confirm_overwrite: write && opts.mode === "overwrite",
      docs: docsText.split(",").map((d) => d.trim()).filter(Boolean), limit: opts.limit && opts.limit > 0 ? opts.limit : null,
    };
    try {
      const run = await api.jobs.mappingRun(body);
      toast.success(write ? "Mapping started (writes)" : "Dry run started");
      onStarted(run);
    } catch (e) {
      fail(e);
    }
  };

  const askWrite = () =>
    setConfirm({
      title: opts.mode === "overwrite" ? "Overwrite links in Neo4j?" : "Write links to Neo4j?",
      label: opts.mode === "overwrite" ? "Overwrite" : "Write",
      action: () => start(true),
      body: (
        <>
          <p>This runs the mapping and then <strong>writes the result to Neo4j</strong>.</p>
          {opts.mode === "append" ? (
            <p>Append only adds links that do not exist yet. Existing links are not touched, and links someone rejected are never added again.</p>
          ) : (
            <p>
              <strong>Overwrite replaces the live links of the chosen chunks.</strong> The old links are kept as <em>superseded</em>, not deleted,
              and Undo brings them back.
            </p>
          )}
          <p>It calls the model for every chunk, so it costs tokens. Tip: do a dry run first and look at the numbers.</p>
        </>
      ),
    });

  return (
    <div className="space-y-4">
      {!live && (
        <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900">
          The mapping job needs the live Neo4j database, and the app is in snapshot mode.
        </div>
      )}
      {live && !canWrite && (
        <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900">
          Writing from the app is switched off (<code className="font-mono">MAPPING_UI_WRITE=false</code>). Check and dry runs still work.
        </div>
      )}

      <section className="rounded-xl border border-[#E0912F]/40 bg-[#FFF8EE] p-4" aria-label="What this page does">
        <h2 className="font-display text-base font-bold text-[#1B2B4B]">What this page does</h2>
        <p className="mt-1 text-sm text-ink-200">
          Your policies say things like <em>"customer ID numbers must be masked"</em>. This page uses AI to read each policy section and decide{" "}
          <strong>which tables and columns it applies to</strong>, so the chat can show the rule next to the data it governs.
        </p>
        <ol className="mt-3 grid gap-2 text-xs text-ink-200 sm:grid-cols-3">
          {[
            ["1. Check", "See what is there. Nothing runs."],
            ["2. Try it", "AI proposes links. Nothing is saved yet."],
            ["3. Review and save", "You accept or reject each link. Only accepted links are saved, and you can undo."],
          ].map(([t, d]) => (
            <li key={t} className="rounded-lg bg-white/80 px-3 py-2 ring-1 ring-[#E0912F]/20">
              <div className="font-semibold text-[#1B2B4B]">{t}</div>
              <div className="text-[11px] text-ink-300">{d}</div>
            </li>
          ))}
        </ol>
        <p className="mt-2 text-[11px] text-ink-400">Safe by default: trying it never changes the graph, and existing links are never deleted.</p>
      </section>

      <section className={card}>
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="flex-1 text-sm font-semibold text-ink-100">1. Check</h2>
          <button onClick={() => void runCheck()} disabled={!live || checking} className={btnSecondary}>
            <Search size={12} className="mr-1 inline" /> {checking ? "Checking…" : "Check what would run"}
          </button>
        </div>
        <p className="mt-1 text-[11px] text-ink-400">Reads Neo4j only. No model is called and nothing is written.</p>
        {check && (
          <div className="mt-3 space-y-2 text-xs text-ink-200">
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              {[
                ["Tables / views", check.catalogue.tables],
                ["Columns", check.catalogue.columns],
                ["Policy chunks", check.chunks.total],
                ["Existing links", check.existing_links],
                ["Chunks with a link", check.chunks.with_live_link],
                ["Chunks without", check.chunks.without_link],
                ["Documents", check.chunks.documents],
              ].map(([k, v]) => (
                <div key={String(k)} className="rounded-md bg-ink-950 px-2.5 py-1.5">
                  <div className="text-[10px] uppercase tracking-wide text-ink-400">{k}</div>
                  <div className="text-sm font-semibold text-ink-100">{v}</div>
                </div>
              ))}
            </div>
            <ul className="space-y-0.5 text-[11px] text-ink-300">
              {check.models.map((m) => (
                <li key={m.slot}>
                  Model {m.slot} ({m.role}): <span className="font-mono">{m.model ?? (m.slot > 1 ? "not set, model 1 does this too" : "NOT CONFIGURED")}</span>
                </li>
              ))}
            </ul>
            {!check.independent_verifier && (
              <p className="text-[11px] text-amber-800">
                One model maps and verifies, which is not independent, so its links stay <em>proposed</em> for a person unless you tick "Accept self-verified".
              </p>
            )}
          </div>
        )}
      </section>

      <section className={card}>
        <h2 className="text-sm font-semibold text-ink-100">2. Try it</h2>
        <div className="mt-2 flex flex-wrap items-end gap-4">
          <label className="flex flex-col gap-0.5 text-[11px] text-ink-400">
            Mode
            <select className={inputCls} value={opts.mode} onChange={(e) => set("mode", e.target.value as "append" | "overwrite")}>
              <option value="append">append: add what is missing</option>
              <option value="overwrite">overwrite: replace (old kept as superseded)</option>
            </select>
          </label>
          <label className="flex flex-col gap-0.5 text-[11px] text-ink-400">
            Documents (words, comma-separated; empty = all)
            <input className={`${inputCls} w-56`} value={docsText} onChange={(e) => setDocsText(e.target.value)} placeholder="e.g. Customer Data" />
          </label>
          <label className="flex flex-col gap-0.5 text-[11px] text-ink-400">
            First N chunks (empty = all)
            <input
              className={`${inputCls} w-28`} type="number" min={1} value={opts.limit ?? ""}
              onChange={(e) => set("limit", e.target.value ? Math.max(1, Number(e.target.value)) : null)}
            />
          </label>
          <label className="flex flex-col gap-0.5 text-[11px] text-ink-400">
            Parallel calls
            <input className={`${inputCls} w-20`} type="number" min={1} max={16} value={opts.workers} onChange={(e) => set("workers", Math.min(16, Math.max(1, Number(e.target.value) || 1)))} />
          </label>
        </div>
        <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1.5">
          <Check2 label="Only chunks with no link yet" checked={opts.only_unlinked} onChange={(v) => set("only_unlinked", v)} hint="Skips chunks that already have a live link: fewer model calls." />
          <Check2 label="Accept self-verified links" checked={opts.accept_self_verified} onChange={(v) => set("accept_self_verified", v)} hint="When one model maps and verifies, let its high-confidence links be accepted instead of held for review." />
          <Check2 label="Never auto-accept (all need review)" checked={opts.no_auto_accept} onChange={(v) => set("no_auto_accept", v)} />
          <Check2 label="Use the cache" checked={opts.use_cache} onChange={(v) => set("use_cache", v)} hint="Reuses results for chunks whose text, catalogue and prompt are unchanged." />
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button onClick={() => void start(false)} disabled={!live || active} className={btnPrimary}>
            <Play size={12} className="mr-1 inline" /> Dry run (writes nothing)
          </button>
          {canWrite && (
            <button onClick={askWrite} disabled={!live || active} className={btnDanger}>
              Run and write to Neo4j
            </button>
          )}
          {active && <span className="text-[11px] text-ink-400">A mapping run is in progress.</span>}
        </div>
      </section>

      {mappingRuns.length > 0 && (
        <section className="space-y-2">
          <h2 className="text-sm font-semibold text-ink-100">Runs</h2>
          {mappingRuns.slice(0, 3).map((r) => (
            <RunCard key={r.id} run={r} onCancel={onCancel} onReview={(id) => { setReviewRunId(id); setTimeout(() => document.getElementById("review-section")?.scrollIntoView({ behavior: "smooth", block: "start" }), 50); }} />
          ))}
        </section>
      )}

      <ReviewSection live={live} canWrite={canWrite} runId={reviewRunId} setRunId={setReviewRunId} refreshKey={mappingRuns.filter((r) => r.status === "succeeded").length} />

      {confirm && (
        <ConfirmDialog
          title={confirm.title} confirmLabel={confirm.label} danger
          onCancel={() => setConfirm(null)}
          onConfirm={() => {
            const go = confirm.action;
            setConfirm(null);
            void go();
          }}
        >
          {confirm.body}
        </ConfirmDialog>
      )}
    </div>
  );
}
