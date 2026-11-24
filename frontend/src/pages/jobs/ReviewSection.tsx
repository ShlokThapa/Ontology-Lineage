import { Check, RotateCcw, Undo2, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { ApiError, api } from "../../api/client";
import type { MappingLink, MappingReport, MappingReportLink } from "../../api/types";
import { useAsync } from "../../lib/useAsync";
import { toast } from "../../store/toasts";
import { ConfirmDialog, StatusPill, btnDanger, btnSecondary, card, clip, inputCls } from "./shared";

type Pick = "accept" | "reject" | "skip";
type Confirm = { title: string; body: ReactNode; label: string; danger?: boolean; action: () => Promise<void> };

const keyOf = (l: { chunk_id: string; asset_id: string }) => `${l.chunk_id}|${l.asset_id}`;
const fail = (e: unknown) => toast.error(e instanceof ApiError ? e.code : "Error", e instanceof ApiError ? e.message : String(e));

function Category({ value }: { value: string | null }) {
  if (!value) return null;
  return (
    <span className="ml-1 rounded bg-sky-100 px-1 text-[10px] text-sky-800" title="Linked because the policy speaks about this whole category">
      {value}
    </span>
  );
}

/** Review: first the dry runs that are NOT in Neo4j yet (choose what to write), then the runs that are (accept / reject / undo). */
export function ReviewSection({
  live,
  canWrite,
  runId,
  setRunId,
  refreshKey,
}: {
  live: boolean;
  canWrite: boolean;
  runId: string;
  setRunId: (id: string) => void;
  refreshKey: number;
}) {
  const [nonce, setNonce] = useState(0);
  const reports = useAsync(() => (live ? api.jobs.mappingReports() : Promise.resolve([])), [live, refreshKey, nonce], { toastErrors: false });
  const history = useAsync(() => (live ? api.jobs.mappingRuns() : Promise.resolve([])), [live, refreshKey, nonce], { toastErrors: false });
  const [confirm, setConfirm] = useState<Confirm | null>(null);

  const drafts = (reports.data ?? []).filter((r) => !r.written);
  const isDraft = drafts.some((r) => r.run_id === runId);
  const reload = () => setNonce((n) => n + 1);

  return (
    <section id="review-section" className={card}>
      <h2 className="text-sm font-semibold text-ink-100">3. Review and save</h2>
      <div className="mt-2 flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-0.5 text-[11px] text-ink-400">
          Run
          <select className={`${inputCls} w-96 max-w-full`} value={runId} onChange={(e) => setRunId(e.target.value)} disabled={!live}>
            <option value="">choose a run…</option>
            {runId && !drafts.some((d) => d.run_id === runId) && !history.data?.some((h) => h.run_id === runId) && <option value={runId}>{runId}</option>}
            {drafts.length > 0 && (
              <optgroup label="Dry runs: not in Neo4j yet">
                {drafts.map((r) => (
                  <option key={r.run_id} value={r.run_id}>
                    {new Date(r.created * 1000).toLocaleString()} · {r.links} links to review · {r.chunks} chunks
                  </option>
                ))}
              </optgroup>
            )}
            {(history.data ?? []).length > 0 && (
              <optgroup label="Written to Neo4j">
                {(history.data ?? []).map((h) => (
                  <option key={h.run_id} value={h.run_id}>
                    {h.run_id} · {h.mode} · {Object.entries(h.links_by_status).map(([k, v]) => `${v} ${k}`).join(", ") || "no links"}
                  </option>
                ))}
              </optgroup>
            )}
          </select>
        </label>
        <button onClick={reload} className={btnSecondary}>
          <RotateCcw size={12} className="mr-1 inline" /> Refresh list
        </button>
      </div>

      {!runId && (
        <p className="mt-2 text-[11px] text-ink-400">
          Choose a dry run to look at what it proposes <strong>before</strong> anything goes into Neo4j, then write only the links you pick. Runs that were
          already written appear under "Written to Neo4j", where you can accept, reject or undo.
        </p>
      )}
      {runId && isDraft && <DraftReview runId={runId} canWrite={canWrite} setConfirm={setConfirm} onWritten={reload} />}
      {runId && !isDraft && <WrittenReview runId={runId} live={live} canWrite={canWrite} setConfirm={setConfirm} onChanged={reload} />}

      {confirm && (
        <ConfirmDialog
          title={confirm.title}
          confirmLabel={confirm.label}
          danger={confirm.danger ?? true}
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
    </section>
  );
}

/** A dry run: nothing is in Neo4j yet. Choose Accept / Reject / Skip for each link, then write only those. */
function DraftReview({ runId, canWrite, setConfirm, onWritten }: { runId: string; canWrite: boolean; setConfirm: (c: Confirm) => void; onWritten: () => void }) {
  const report = useAsync<MappingReport>(() => api.jobs.mappingReport(runId), [runId], { toastErrors: false });
  const [picks, setPicks] = useState<Record<string, Pick>>({});
  const [busy, setBusy] = useState(false);
  const links = useMemo(() => report.data?.links ?? [], [report.data]);

  // Start from a sensible suggestion: high-confidence links accepted, everything else left out. Every one can be changed.
  useEffect(() => {
    setPicks(Object.fromEntries(links.map((l) => [keyOf(l), l.confidence === "high" ? "accept" : "skip"])));
  }, [links]);

  const n = useMemo(() => {
    const c = { accept: 0, reject: 0, skip: 0 };
    for (const l of links) c[picks[keyOf(l)] ?? "skip"]++;
    return c;
  }, [links, picks]);
  const setAll = (pick: Pick, only?: (l: MappingReportLink) => boolean) =>
    setPicks((p) => ({ ...p, ...Object.fromEntries(links.filter((l) => !only || only(l)).map((l) => [keyOf(l), pick])) }));

  const write = () => {
    const chosen = (want: Pick) => links.filter((l) => picks[keyOf(l)] === want).map((l) => ({ chunk_id: l.chunk_id, asset_id: l.asset_id }));
    setConfirm({
      title: `Write ${n.accept + n.reject} link(s) to Neo4j?`,
      label: "Write them",
      action: async () => {
        setBusy(true);
        try {
          const r = await api.jobs.applyReport(runId, chosen("accept"), chosen("reject"));
          toast.success("Written to Neo4j", `${r.accepted} accepted, ${r.rejected} rejected${r.skipped.length ? `, ${r.skipped.length} skipped (already in the graph)` : ""}`);
          onWritten();
        } catch (e) {
          fail(e);
        } finally {
          setBusy(false);
        }
      },
      body: (
        <>
          <p>
            <strong>{n.accept}</strong> link(s) will be written as <em>accepted</em> and start showing in the app and the chatbot.
          </p>
          {n.reject > 0 && (
            <p>
              <strong>{n.reject}</strong> will be written as <em>rejected</em>: kept as a record, never shown, and never proposed again.
            </p>
          )}
          <p>
            <strong>{n.skip}</strong> will not be written at all. Your existing links are not touched.
          </p>
        </>
      ),
    });
  };

  if (report.loading && !report.data) return <p className="mt-3 text-xs text-ink-400">Loading the report…</p>;
  if (report.error) return <p className="mt-3 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-xs text-red-800">{report.error.message}</p>;

  return (
    <div className="mt-3">
      <div className="rounded-md border border-sky-300 bg-sky-50 px-3 py-2 text-xs text-sky-900">
        This is a dry run. <strong>Nothing is in Neo4j yet.</strong> Choose what happens to each link, then write only those. The evidence is the exact quote from the policy.
        {report.data?.summary.independent_verification === false && " One model proposed and checked these, so none was accepted automatically."}
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px]">
        <span className="text-ink-400">Set many at once:</span>
        <button className={btnSecondary} onClick={() => setAll("accept", (l) => l.confidence === "high")}>Accept all high-confidence</button>
        <button className={btnSecondary} onClick={() => setAll("accept")}>Accept all</button>
        <button className={btnSecondary} onClick={() => setAll("reject", (l) => l.confidence !== "high")}>Reject all medium</button>
        <button className={btnSecondary} onClick={() => setAll("skip")}>Skip all</button>
      </div>
      <div className="mt-2 max-h-[420px] overflow-auto rounded-lg border border-ink-700">
        <table className="w-full border-collapse text-left text-[11px]">
          <thead className="sticky top-0 bg-ink-800 text-ink-300">
            <tr>
              <th className="px-2 py-1.5">Policy section</th>
              <th className="px-2 py-1.5">Asset</th>
              <th className="px-2 py-1.5">Relation</th>
              <th className="px-2 py-1.5">Conf.</th>
              <th className="px-2 py-1.5">Evidence (quoted from the policy)</th>
              <th className="px-2 py-1.5">Do this</th>
            </tr>
          </thead>
          <tbody>
            {links.map((l) => {
              const pick = picks[keyOf(l)] ?? "skip";
              return (
                <tr key={keyOf(l)} className={`border-t border-ink-700 align-top ${pick === "accept" ? "bg-emerald-50/50" : pick === "reject" ? "bg-red-50/50" : ""}`}>
                  <td className="max-w-[190px] px-2 py-1.5 text-ink-300" title={`${l.doc_title} › ${l.section_path}`}>{clip(l.section_path ?? "", 60)}</td>
                  <td className="px-2 py-1.5 font-mono text-ink-100">{l.asset}<Category value={l.category} /></td>
                  <td className="px-2 py-1.5">{l.relation}</td>
                  <td className="px-2 py-1.5">{l.confidence}</td>
                  <td className="max-w-[320px] px-2 py-1.5 text-ink-300" title={`${l.evidence}\n\n${l.reason}\n${l.note ?? ""}`}>{clip(l.evidence ?? "", 110)}</td>
                  <td className="px-2 py-1.5">
                    <select
                      aria-label={`What to do with ${l.asset}`}
                      className={inputCls}
                      value={pick}
                      disabled={!canWrite}
                      onChange={(e) => setPicks((p) => ({ ...p, [keyOf(l)]: e.target.value as Pick }))}
                    >
                      <option value="accept">Accept</option>
                      <option value="reject">Reject</option>
                      <option value="skip">Skip (don't write)</option>
                    </select>
                  </td>
                </tr>
              );
            })}
            {!links.length && <tr><td colSpan={6} className="px-3 py-6 text-center text-ink-400">This dry run has no links to review.</td></tr>}
          </tbody>
        </table>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <button onClick={write} disabled={!canWrite || busy || n.accept + n.reject === 0} className={btnDanger}>
          Write {n.accept + n.reject} link(s) to Neo4j
        </button>
        <span className="text-[11px] text-ink-400">
          {n.accept} accept · {n.reject} reject · {n.skip} skip
          {!canWrite && " · writing is switched off (MAPPING_UI_WRITE=false)"}
        </span>
      </div>
    </div>
  );
}

/** A run that is already in Neo4j: accept or reject its links, in bulk or one by one, or undo the whole run. */
function WrittenReview({ runId, live, canWrite, setConfirm, onChanged }: { runId: string; live: boolean; canWrite: boolean; setConfirm: (c: Confirm) => void; onChanged: () => void }) {
  const [filter, setFilter] = useState<"proposed" | "accepted" | "rejected" | "all">("proposed");
  const [links, setLinks] = useState<MappingLink[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    if (!runId || !live) return setLinks([]);
    setLoading(true);
    try {
      setLinks(await api.jobs.mappingLinks(runId));
    } catch (e) {
      fail(e);
      setLinks([]);
    } finally {
      setLoading(false);
    }
  }, [runId, live]);
  useEffect(() => void load(), [load]);

  const shown = useMemo(() => links.filter((l) => filter === "all" || l.status === filter), [links, filter]);
  const counts = useMemo(() => links.reduce<Record<string, number>>((a, l) => ({ ...a, [l.status]: (a[l.status] ?? 0) + 1 }), {}), [links]);

  const act = async <T,>(fn: () => Promise<T>, done: (r: T) => string) => {
    setBusy(true);
    try {
      toast.success(done(await fn()));
      await load();
      onChanged();
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  };

  const decide = (l: MappingLink, status: "accepted" | "rejected") =>
    void act(() => api.jobs.decide(runId, status, [{ chunk_id: l.chunk_id, asset_id: l.asset_id }]), () => `${status === "accepted" ? "Accepted" : "Rejected"} ${l.asset}`);

  const bulk = (status: "accepted" | "rejected", level: "high" | "medium") =>
    setConfirm({
      title: `${status === "accepted" ? "Accept" : "Reject"} ${level === "high" ? "high-confidence " : ""}proposed links?`,
      label: status === "accepted" ? "Accept" : "Reject",
      action: () => act(() => api.jobs.review(runId, status, level), (r: { changed: number }) => `${r.changed} link(s) ${status}`),
      body: (
        <p>
          This changes the status of this run's <em>proposed</em> links{level === "high" ? " that have high confidence" : ""}.
          {status === "rejected" && " A rejection is remembered: later runs never add that link again."}
        </p>
      ),
    });

  const undo = () =>
    setConfirm({
      title: "Undo this run?",
      label: "Undo the run",
      action: () => act(() => api.jobs.restore(runId), (r: { retracted: number; restored: number }) => `${r.retracted} link(s) retracted, ${r.restored} restored`),
      body: <p>The links this run created become <em>retracted</em> (hidden, kept for the record), and any links it superseded become live again.</p>,
    });

  return (
    <div className="mt-3">
      <div className="flex flex-wrap items-center gap-2">
        <button onClick={() => void load()} disabled={loading} className={btnSecondary}>
          <RotateCcw size={12} className="mr-1 inline" /> Reload
        </button>
        {canWrite && (
          <>
            <button onClick={() => bulk("accepted", "high")} disabled={busy || !counts.proposed} className={btnSecondary}>Accept all high-confidence</button>
            <button onClick={() => bulk("accepted", "medium")} disabled={busy || !counts.proposed} className={btnSecondary}>Accept all proposed</button>
            <button onClick={() => bulk("rejected", "medium")} disabled={busy || !counts.proposed} className={btnSecondary}>Reject all proposed</button>
            <button onClick={undo} disabled={busy} className={btnSecondary}><Undo2 size={12} className="mr-1 inline" /> Undo this run</button>
          </>
        )}
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-1.5 text-[11px]">
        {(["proposed", "accepted", "rejected", "all"] as const).map((f) => (
          <button key={f} onClick={() => setFilter(f)} className={`rounded-full px-2.5 py-1 ${filter === f ? "bg-accent text-white" : "bg-ink-800 text-ink-300 hover:bg-ink-700"}`}>
            {f} {f === "all" ? `(${links.length})` : `(${counts[f] ?? 0})`}
          </button>
        ))}
        {Object.entries(counts).filter(([k]) => !["proposed", "accepted", "rejected"].includes(k)).map(([k, v]) => (
          <span key={k} className="text-ink-400">{v} {k}</span>
        ))}
      </div>
      <div className="mt-2 max-h-[420px] overflow-auto rounded-lg border border-ink-700">
        <table className="w-full border-collapse text-left text-[11px]">
          <thead className="sticky top-0 bg-ink-800 text-ink-300">
            <tr>
              <th className="px-2 py-1.5">Policy section</th>
              <th className="px-2 py-1.5">Asset</th>
              <th className="px-2 py-1.5">Relation</th>
              <th className="px-2 py-1.5">Conf.</th>
              <th className="px-2 py-1.5">Evidence (quoted from the policy)</th>
              <th className="px-2 py-1.5">Status</th>
              {canWrite && <th className="px-2 py-1.5" />}
            </tr>
          </thead>
          <tbody>
            {shown.map((l) => (
              <tr key={keyOf(l)} className="border-t border-ink-700 align-top">
                <td className="max-w-[200px] px-2 py-1.5 text-ink-300" title={l.section}>{clip(l.section ?? "", 60)}</td>
                <td className="px-2 py-1.5 font-mono text-ink-100">{l.asset}<Category value={l.category || null} /></td>
                <td className="px-2 py-1.5">{l.relation}</td>
                <td className="px-2 py-1.5">{l.confidence}</td>
                <td className="max-w-[320px] px-2 py-1.5 text-ink-300" title={`${l.evidence}\n\n${l.reason}\n${l.note ?? ""}`}>{clip(l.evidence ?? "", 110)}</td>
                <td className="px-2 py-1.5"><StatusPill status={l.status} /></td>
                {canWrite && (
                  <td className="whitespace-nowrap px-2 py-1.5">
                    <button disabled={busy || l.status === "accepted"} onClick={() => decide(l, "accepted")} title="Accept" aria-label={`Accept ${l.asset}`} className="mr-1 rounded p-1 text-emerald-700 hover:bg-emerald-50 disabled:opacity-30"><Check size={13} /></button>
                    <button disabled={busy || l.status === "rejected"} onClick={() => decide(l, "rejected")} title="Reject" aria-label={`Reject ${l.asset}`} className="rounded p-1 text-red-700 hover:bg-red-50 disabled:opacity-30"><X size={13} /></button>
                  </td>
                )}
              </tr>
            ))}
            {!shown.length && <tr><td colSpan={canWrite ? 7 : 6} className="px-3 py-6 text-center text-ink-400">{loading ? "Loading…" : "No links with this status in this run."}</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
