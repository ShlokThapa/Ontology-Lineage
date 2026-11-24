import { ChevronRight, Sparkles } from "lucide-react";
import type { JobRun, JobsConfig } from "../../api/types";
import { busyKeys } from "./shared";
import { STAGES, type Stage } from "./stages";

const NAVY = "#1B2B4B";
const TONE = { teal: "#0F7C86", orange: "#D9822B" } as const;

/**
 * The whole process on one line, drawn after the project slide: numbered cards joined by arrows, an AI mark on the
 * stages that use the model. Each card is a button that jumps to that stage's buttons.
 */
export function FlowOverview({
  config,
  runs,
  active,
  onSelect,
}: {
  config: JobsConfig;
  runs: JobRun[];
  active: Stage["group"] | null;
  onSelect: (group: Stage["group"]) => void;
}) {
  const busy = busyKeys(runs);
  const running = (g: Stage["group"]): boolean =>
    config.jobs.some((j) => j.group === g && busy.has(j.key)) ||
    (g === "llm" && runs.some((r) => r.kind === "mapping" && (r.status === "queued" || r.status === "running")));
  const stepCount = (g: Stage["group"]) => config.jobs.filter((j) => j.group === g && !j.legacy).length;

  return (
    <section aria-label="How the data flows" className="mb-4">
      <div className="mb-2 flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
        <h2 className="font-display text-base font-bold" style={{ color: NAVY }}>
          How the data flows
        </h2>
        <p className="text-[11px] text-ink-400">
          Work from left to right. Click a box to jump to its buttons. <Sparkles size={11} className="mx-0.5 inline" style={{ color: TONE.orange }} /> marks the stage that uses AI.
        </p>
      </div>
      <ol className="flex flex-wrap items-stretch gap-y-2 lg:flex-nowrap">
        {STAGES.map((s, i) => {
          const Icon = s.icon;
          const title = s.group === "use" || s.group === "llm" ? s.short : (config.groups[s.group] ?? s.short);
          const selected = active === s.group;
          const live = running(s.group);
          return (
            <li key={s.group} className="flex min-w-[200px] flex-1 items-stretch">
              {i > 0 && (
                <span className="hidden shrink-0 items-center px-0.5 lg:flex" aria-hidden>
                  <ChevronRight size={26} strokeWidth={3} className="text-[#9AA3B5]" />
                </span>
              )}
              <button
                type="button"
                onClick={() => onSelect(s.group)}
                aria-current={selected ? "step" : undefined}
                className={`flex w-full flex-col rounded-2xl bg-[#EEF1F6] p-3.5 text-left transition hover:bg-[#E4E9F2] focus:outline-none focus-visible:ring-2 focus-visible:ring-accent ${
                  selected ? "ring-2 ring-accent" : ""
                }`}
              >
                <div className="flex items-start justify-between">
                  <span className="flex h-10 w-10 items-center justify-center rounded-full text-white" style={{ background: s.circle }}>
                    <Icon size={19} />
                  </span>
                  <span className="flex items-center gap-1 font-display text-2xl font-bold leading-none text-[#4B5568]">
                    {s.ai && <Sparkles size={14} style={{ color: TONE.orange }} aria-label="uses AI" />}
                    {String(s.n).padStart(2, "0")}
                  </span>
                </div>
                <div className="mt-2.5 font-display text-[15px] font-bold leading-tight" style={{ color: NAVY }}>
                  {title}
                </div>
                <div className="mt-0.5 text-[11px] font-semibold" style={{ color: TONE[s.whereTone] }}>
                  {s.where}
                </div>
                <ul className="mt-2 list-disc space-y-0.5 pl-4 text-[11px] leading-snug text-[#4B5568]">
                  {s.bullets.map((b) => (
                    <li key={b}>{b}</li>
                  ))}
                </ul>
                <div className="mt-auto pt-2 text-[10px] text-ink-400">
                  {live ? (
                    <span className="inline-flex items-center gap-1 font-semibold text-sky-700">
                      <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current" aria-hidden /> running now
                    </span>
                  ) : s.group === "use" ? (
                    "Open the chat"
                  ) : (
                    `${stepCount(s.group)} ${stepCount(s.group) === 1 ? "button" : "buttons"}`
                  )}
                </div>
              </button>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
