import { AlertTriangle, CheckCircle2, CircleSlash, RefreshCw } from "lucide-react";
import { useMemo, useRef, useState, type ReactNode } from "react";
import { ApiError, api } from "../../api/client";
import type { ChainInfo, JobInfo, JobParam, JobRun, JobsConfig } from "../../api/types";
import { useAsync } from "../../lib/useAsync";
import { toast } from "../../store/toasts";
import { ConfirmDialog, btnPrimary, btnSecondary, busyKeys, card, inputCls } from "./shared";
import { STAGE_BY_GROUP, type Stage } from "./stages";

type Values = Record<string, Record<string, string | number | boolean>>;
interface Pending {
  title: string;
  body: ReactNode;
  confirmLabel: string;
  danger: boolean;
  action: () => Promise<void>;
}

const defaultsOf = (job: JobInfo): Record<string, string | number | boolean> =>
  Object.fromEntries(job.params.map((p) => [p.name, p.default ?? (p.type === "bool" ? false : p.type === "int" ? 0 : "")]));

function ParamInput({ p, value, onChange }: { p: JobParam; value: string | number | boolean; onChange: (v: string | number | boolean) => void }) {
  if (p.type === "bool")
    return (
      <label className="flex items-center gap-1.5 text-xs text-ink-200">
        <input type="checkbox" checked={value === true} onChange={(e) => onChange(e.target.checked)} /> {p.label}
      </label>
    );
  return (
    <label className="flex flex-col gap-0.5 text-[11px] text-ink-400">
      {p.label}
      {p.type === "select" ? (
        <select className={inputCls} value={String(value)} onChange={(e) => onChange(e.target.value)}>
          {p.options.map((o) => (
            <option key={o}>{o}</option>
          ))}
        </select>
      ) : (
        <input
          className={`${inputCls} w-40`}
          type={p.type === "int" ? "number" : "text"}
          min={p.type === "int" ? 0 : undefined}
          value={String(value)}
          onChange={(e) => onChange(p.type === "int" ? Math.max(0, Number(e.target.value) || 0) : e.target.value)}
        />
      )}
    </label>
  );
}

export function FabricPanel({
  config,
  runs,
  onStarted,
  onOpenMapping,
}: {
  config: JobsConfig;
  runs: JobRun[];
  onStarted: (run: JobRun) => void;
  onOpenMapping: () => void;
}) {
  const force = useRef(false);
  const [nonce, setNonce] = useState(0);
  const status = useAsync(
    () => {
      const refresh = force.current;
      force.current = false;
      return api.jobs.fabricStatus(refresh);
    },
    [nonce],
    { toastErrors: false },
  );
  const [values, setValues] = useState<Values>({});
  const [chainWrite, setChainWrite] = useState<Record<string, boolean>>({});
  const [pending, setPending] = useState<Pending | null>(null);
  const busy = useMemo(() => busyKeys(runs), [runs]);
  const jobsByKey = useMemo(() => new Map(config.jobs.map((j) => [j.key, j])), [config.jobs]);

  const st = status.data;
  const ready = !!st?.configured && !st.error;
  const found = (key: string): boolean | undefined => (st && ready ? st.items[key]?.found : undefined);
  const valuesOf = (job: JobInfo) => ({ ...defaultsOf(job), ...(values[job.key] ?? {}) });

  const fail = (e: unknown) => {
    const err = e instanceof ApiError ? e : null;
    toast.error(err?.code ?? "Error", err?.message ?? String(e));
  };

  const confirmThen = (p: Omit<Pending, "danger"> & { danger?: boolean }) => setPending({ danger: false, ...p });

  const startJob = (job: JobInfo) => {
    const params = job.params.length ? valuesOf(job) : {};
    const writes = params.WRITE === true;
    const overwrite = writes && params.MODE === "overwrite";
    const action = async () => {
      try {
        const run = await api.jobs.runJob(job.key, params, true);
        toast.success("Started", job.title);
        onStarted(run);
      } catch (e) {
        fail(e);
      }
    };
    if (!job.confirm && !writes) return void action();
    confirmThen({
      title: `Run ${job.title}?`,
      confirmLabel: "Run it",
      danger: !!job.confirm || writes,
      action,
      body: (
        <>
          {job.confirm && <p>{job.confirm}</p>}
          {writes && (
            <p>
              <strong>Write to Neo4j is on</strong> ({String(params.MODE)} mode): the notebook will create GOVERNS links
              {overwrite ? " and mark the existing links of the chosen chunks as superseded." : "."}
            </p>
          )}
          <p>It runs in Fabric and takes as long as the notebook or pipeline does.</p>
        </>
      ),
    });
  };

  const startChain = (chain: ChainInfo) => {
    const steps = chain.steps.map((k) => jobsByKey.get(k)).filter((j): j is JobInfo => !!j);
    const writes = chain.steps.includes("llm_mapping_nb") && chainWrite[chain.key] === true;
    const overrides: Record<string, Record<string, unknown>> = chain.steps.includes("llm_mapping_nb") ? { llm_mapping_nb: { WRITE: writes } } : {};
    const warnings = steps.filter((j) => j.confirm);
    const action = async () => {
      try {
        const run = await api.jobs.runChain(chain.key, overrides, true);
        toast.success("Started", chain.title);
        onStarted(run);
      } catch (e) {
        fail(e);
      }
    };
    if (!warnings.length && !writes) return void action();
    confirmThen({
      title: `Run ${chain.title}?`,
      confirmLabel: "Run the chain",
      danger: true,
      action,
      body: (
        <>
          <p>The steps run one after another and the chain stops at the first one that fails:</p>
          <ol className="list-decimal pl-5">
            {steps.map((j) => (
              <li key={j.key}>{j.title}</li>
            ))}
          </ol>
          {warnings.map((j) => (
            <p key={j.key}>
              <strong>{j.title}:</strong> {j.confirm}
            </p>
          ))}
          {writes && (
            <p>
              <strong>The LLM mapping step will write to Neo4j</strong> (append mode).
            </p>
          )}
        </>
      ),
    });
  };

  const jobRow = (job: JobInfo) => {
    const f = found(job.key);
    const running = busy.has(job.key);
    const disabled = !ready || f === false || running;
    const why = !ready ? "Fabric is not configured" : f === false ? `Not found in the workspace (looked for ${job.names.join(", ")})` : running ? "Already running" : "";
    const v = valuesOf(job);
    return (
      <div key={job.key} className="rounded-lg border border-ink-700 p-3">
        <div className="flex flex-wrap items-start gap-2">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2 text-xs font-semibold text-ink-100">
              {job.title}
              {job.legacy && <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-semibold text-amber-800">retired</span>}
              {f === true && (
                <span className="flex items-center gap-1 text-[10px] font-normal text-emerald-700">
                  <CheckCircle2 size={11} /> {st?.items[job.key]?.display_name}
                </span>
              )}
              {f === false && (
                <span className="flex items-center gap-1 text-[10px] font-normal text-ink-400" title={`Looked for: ${job.names.join(", ")}`}>
                  <CircleSlash size={11} /> not in the workspace
                </span>
              )}
            </div>
            <div className="mt-0.5 text-[11px] text-ink-400">{job.description}</div>
          </div>
          <button onClick={() => startJob(job)} disabled={disabled} title={why} className={btnPrimary}>
            Run
          </button>
        </div>
        {job.params.length > 0 && (
          <details className="mt-2">
            <summary className="cursor-pointer text-[11px] text-ink-400 hover:text-ink-100">Parameters</summary>
            <div className="mt-2 flex flex-wrap items-end gap-3">
              {job.params.map((p) => (
                <ParamInput
                  key={p.name}
                  p={p}
                  value={v[p.name]}
                  onChange={(nv) => setValues((all) => ({ ...all, [job.key]: { ...valuesOf(job), [p.name]: nv } }))}
                />
              ))}
            </div>
          </details>
        )}
      </div>
    );
  };

  const chainCard = (chain: ChainInfo) => {
    const missing = chain.steps.filter((k) => found(k) === false);
    const running = chain.steps.some((k) => busy.has(k)) || busy.has(chain.key);
    const disabled = !ready || missing.length > 0 || running;
    const why = !ready
      ? "Fabric is not configured"
      : missing.length
        ? `Missing in the workspace: ${missing.map((k) => jobsByKey.get(k)?.title ?? k).join(", ")}`
        : running
          ? "A step is already running"
          : "";
    return (
      <div key={chain.key} className="rounded-lg border border-accent/30 bg-accent/5 p-3">
        <div className="flex flex-wrap items-start gap-2">
          <div className="min-w-0 flex-1">
            <div className="text-xs font-semibold text-ink-100">{chain.title}</div>
            {chain.description && <div className="text-[11px] text-ink-400">{chain.description}</div>}
            <div className="mt-1.5 flex flex-wrap items-center gap-1 text-[11px]">
              {chain.steps.map((k, i) => (
                <span key={k} className="flex items-center gap-1">
                  {i > 0 && <span className="text-ink-500">→</span>}
                  <span className={`rounded px-1.5 py-0.5 ${found(k) === false ? "bg-ink-800 text-ink-500 line-through" : "bg-white text-ink-200 ring-1 ring-ink-700"}`}>
                    {jobsByKey.get(k)?.title.replace(/ \(.*\)$/, "") ?? k}
                  </span>
                </span>
              ))}
            </div>
            {chain.steps.includes("llm_mapping_nb") && (
              <label className="mt-1.5 flex items-center gap-1.5 text-[11px] text-ink-300">
                <input type="checkbox" checked={chainWrite[chain.key] === true} onChange={(e) => setChainWrite((w) => ({ ...w, [chain.key]: e.target.checked }))} />
                Write to Neo4j in the mapping step (off = dry run)
              </label>
            )}
          </div>
          <button onClick={() => startChain(chain)} disabled={disabled} title={why} className={btnPrimary}>
            Run in order
          </button>
        </div>
      </div>
    );
  };

  // Stage 4 (the AI mapping) lives in its own tab, which is where it is reviewed and saved, so it is not repeated
  // here as a second, different set of buttons: a hand-off card stands in its place, in the flow's order.
  // Set B (notebooks that were designed but never imported) is left out: it only ever showed "not in the workspace".
  // It is still in the registry and the API, so importing those notebooks later and un-hiding it is a one-line change.
  const HIDDEN = new Set(["llm", "setb"]);
  const groups = Object.entries(config.groups).filter(
    ([g]) => !HIDDEN.has(g) && (config.jobs.some((j) => j.group === g) || config.chains.some((c) => c.group === g)),
  );
  const stage4 = STAGE_BY_GROUP.get("llm");
  const handOff = stage4 && (
    <section key="llm-handoff" className={`${card} flex flex-wrap items-center gap-3`}>
      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full font-display text-base font-bold text-white" style={{ background: stage4.circle }} aria-hidden>
        {stage4.n}
      </span>
      <div className="min-w-0 flex-1">
        <h2 className="font-display text-base font-bold text-[#1B2B4B]">Stage {stage4.n}: {stage4.short}</h2>
        <p className="text-xs text-ink-300">{config.group_notes.llm} It has its own tab, because you review the result before anything is saved.</p>
      </div>
      <button onClick={onOpenMapping} className={btnPrimary}>
        Open stage {stage4.n}
      </button>
    </section>
  );

  return (
    <div className="space-y-4">
      <div className={card}>
        <div className="flex flex-wrap items-center gap-3 text-xs">
          <div className="min-w-0 flex-1">
            <div className="font-semibold text-ink-100">
              Fabric workspace <span className="font-mono text-ink-300">{st?.workspace ?? config.fabric.workspace ?? "not set"}</span>
            </div>
            <div className="text-[11px] text-ink-400">
              Sign-in: {st?.auth_mode === "service_principal" ? "service principal" : st?.auth_mode === "token" ? "pasted token" : "none"}
              {st?.token_expires_in != null && (
                <span className={st.token_expires_in < 600 ? "ml-1 font-semibold text-amber-700" : "ml-1"}>
                  · token {st.token_expires_in > 0 ? `expires in ${Math.round(st.token_expires_in / 60)} min` : "has expired"}
                </span>
              )}
            </div>
          </div>
          <button
            onClick={() => {
              force.current = true;
              setNonce((n) => n + 1);
            }}
            disabled={status.loading}
            className={btnSecondary}
          >
            <RefreshCw size={12} className={`mr-1 inline ${status.loading ? "animate-spin" : ""}`} /> Check the workspace
          </button>
        </div>
        {st && st.problems.length > 0 && (
          <div className="mt-3 flex items-start gap-2 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900">
            <AlertTriangle size={14} className="mt-0.5 shrink-0" />
            <div className="space-y-1">
              {st.problems.map((p) => (
                <p key={p}>{p}</p>
              ))}
              <p>Add them to <code className="font-mono">.env</code> and restart the backend. Buttons stay disabled until Fabric is configured.</p>
            </div>
          </div>
        )}
        {st?.error && <div className="mt-3 rounded-lg border border-red-300 bg-red-50 px-3 py-2 text-xs text-red-800">{st.error}</div>}
      </div>

      {groups.map(([g, title]) => {
        const chains = config.chains.filter((c) => c.group === g);
        const jobs = config.jobs.filter((j) => j.group === g && !j.legacy); // the retired step 5 is a no-op: not worth a button
        const body = (
          <div className="space-y-2">
            {chains.map(chainCard)}
            {jobs.map(jobRow)}
          </div>
        );
        const stage = STAGE_BY_GROUP.get(g as Stage["group"]);
        // a group the flow has no stage for still gets a plain section rather than vanishing
        return (
          <section key={g} id={`stage-${g}`} className={`${card} scroll-mt-4`}>
            <div className="mb-3 flex items-start gap-3">
              {stage && (
                <span
                  className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full font-display text-base font-bold text-white"
                  style={{ background: stage.circle }}
                  aria-hidden
                >
                  {stage.n}
                </span>
              )}
              <div className="min-w-0">
                <h2 className="font-display text-base font-bold text-[#1B2B4B]">
                  {stage ? `Stage ${stage.n}: ${title}` : title}
                </h2>
                {config.group_notes[g] && <p className="text-xs text-ink-300">{config.group_notes[g]}</p>}
              </div>
            </div>
            {body}
          </section>
        );
      })}
      {handOff}

      {pending && (
        <ConfirmDialog
          title={pending.title}
          confirmLabel={pending.confirmLabel}
          danger={pending.danger}
          onCancel={() => setPending(null)}
          onConfirm={() => {
            const go = pending.action;
            setPending(null);
            void go();
          }}
        >
          {pending.body}
        </ConfirmDialog>
      )}
    </div>
  );
}
