import { useCallback, useState } from "react";
import { ApiError, api } from "../api/client";
import type { JobRun } from "../api/types";
import { ErrorPanel, PageHeader, Segmented, Skeleton } from "../components/ui";
import { useAsync } from "../lib/useAsync";
import { toast } from "../store/toasts";
import { FabricPanel } from "./jobs/FabricPanel";
import { MappingPanel } from "./jobs/MappingPanel";
import { RunCard } from "./jobs/RunCard";
import { FlowOverview } from "./jobs/FlowOverview";
import { useRuns } from "./jobs/shared";
import type { Stage } from "./jobs/stages";
import { useApp } from "../store/app";

type Tab = "fabric" | "mapping";

export function JobsPage() {
  const setChatOpen = useApp((s) => s.setChatOpen);
  const config = useAsync(() => api.jobs.config(), [], { toastErrors: false });
  const { runs, refresh, add } = useRuns();
  const [tab, setTab] = useState<Tab>("fabric");
  const [reviewRunId, setReviewRunId] = useState("");

  const started = useCallback(
    (run: JobRun) => {
      add(run);
      void refresh();
    },
    [add, refresh],
  );

  const cancel = useCallback(
    async (run: JobRun) => {
      try {
        add(await api.jobs.cancel(run.id));
        toast.info("Cancel requested", "Fabric stops the current step, then the chain ends.");
      } catch (e) {
        toast.error(e instanceof ApiError ? e.code : "Error", e instanceof ApiError ? e.message : String(e));
      }
    },
    [add],
  );

  const fabricRuns = runs.filter((r) => r.kind !== "mapping");

  const select = (group: Stage["group"]) => {
    if (group === "use") return setChatOpen(true);
    if (group === "llm") {
      setTab("mapping");
      return window.scrollTo?.({ top: 0 });
    }
    setTab("fabric");
    // the section only exists once the Fabric tab has rendered
    setTimeout(() => document.getElementById(`stage-${group}`)?.scrollIntoView({ behavior: "smooth", block: "start" }), 60);
  };

  return (
    <div className="flex h-full flex-col overflow-auto p-5">
      <PageHeader
        title="Run jobs"
        subtitle="Run each stage of the data flow with a button: copy the data, extract the lineage, prepare the policy documents, then let AI link the policies to the data. This is a proof of concept with no login, so anyone who can open this page can run these."
      />
      {config.error && <ErrorPanel code={config.error.code} message={config.error.message} />}
      {config.loading && !config.data && <Skeleton className="h-40" />}
      {config.data && (
        <>
          <FlowOverview config={config.data} runs={runs} active={tab === "mapping" ? "llm" : null} onSelect={select} />
          <div className="mb-4">
            <Segmented<Tab>
              value={tab}
              onChange={setTab}
              options={[
                { value: "fabric", label: "Stages 1 to 3: Fabric pipelines and notebooks" },
                { value: "mapping", label: "Stage 4: Policy document mapping" },
              ]}
            />
          </div>
          {tab === "fabric" ? (
            <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,420px)]">
              <FabricPanel config={config.data} runs={runs} onStarted={started} onOpenMapping={() => select("llm")} />
              <div className="space-y-2">
                <h2 className="text-sm font-semibold text-ink-100">Runs</h2>
                {fabricRuns.length === 0 && <p className="text-xs text-ink-400">Nothing started yet. Runs started here appear below and update while they run.</p>}
                {fabricRuns.slice(0, 8).map((r) => (
                  <RunCard key={r.id} run={r} onCancel={cancel} />
                ))}
              </div>
            </div>
          ) : (
            <MappingPanel
              config={config.data}
              runs={runs}
              onStarted={started}
              onCancel={cancel}
              reviewRunId={reviewRunId}
              setReviewRunId={setReviewRunId}
            />
          )}
        </>
      )}
    </div>
  );
}
