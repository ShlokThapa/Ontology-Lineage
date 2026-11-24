import { AlertTriangle, CheckCircle2, ChevronDown, ChevronUp, Database, Rows3, ShieldAlert } from "lucide-react";
import { useMemo, useState } from "react";
import type { QualityIssue, QualityReport, QualityTable } from "../api/types";

const NAVY = "#1B2B4B";
const MUTED = "#6B7690";

const GRADE_COLOR: Record<string, string> = {
  Excellent: "#1E8E5A",
  Good: "#4E9F3D",
  Fair: "#E0A12F",
  "Needs attention": "#D9822B",
  Poor: "#C0392B",
};
const gradeColor = (grade: string) => GRADE_COLOR[grade] ?? "#9AA3B5";

/** Colour for a bare 0-100 score (dimension bars). */
function scoreColor(score: number | null): string {
  if (score === null) return "#C9CFDB";
  if (score >= 97) return GRADE_COLOR.Excellent;
  if (score >= 92) return GRADE_COLOR.Good;
  if (score >= 80) return GRADE_COLOR.Fair;
  if (score >= 60) return GRADE_COLOR["Needs attention"];
  return GRADE_COLOR.Poor;
}

const SEVERITY = {
  high: { label: "High", bg: "#FDECEA", fg: "#B3261E", bar: "#C0392B" },
  medium: { label: "Medium", bg: "#FFF1DC", fg: "#A85A07", bar: "#E0912F" },
  low: { label: "Low", bg: "#E8F0FB", fg: "#2F5D9F", bar: "#5B8BD0" },
} as const;

const fmt = (n: number) => n.toLocaleString("en-IN");
const shown = (score: number | null) => (score === null ? "n/a" : String(Math.round(score * 10) / 10));

function asOf(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

function Gauge({ score, grade }: { score: number | null; grade: string }) {
  const r = 52;
  const c = 2 * Math.PI * r;
  const color = gradeColor(grade);
  const filled = score === null ? 0 : Math.max(0, Math.min(100, score)) / 100;
  return (
    <div className="relative h-[132px] w-[132px] shrink-0" role="img" aria-label={`Overall score ${shown(score)} out of 100, ${grade}`}>
      <svg viewBox="0 0 132 132" className="h-full w-full -rotate-90">
        <circle cx="66" cy="66" r={r} fill="none" stroke="#E6EAF1" strokeWidth="11" />
        <circle
          cx="66" cy="66" r={r} fill="none" stroke={color} strokeWidth="11" strokeLinecap="round"
          strokeDasharray={`${c * filled} ${c}`}
        />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <div className="font-display text-[30px] font-bold leading-none" style={{ color: NAVY }}>
          {shown(score)}
        </div>
        <div className="mt-0.5 text-[10px] uppercase tracking-wide" style={{ color: MUTED }}>
          out of 100
        </div>
      </div>
    </div>
  );
}

function Stat({ label, value, sub, icon }: { label: string; value: string; sub?: string; icon: React.ReactNode }) {
  return (
    <div className="rounded-xl bg-[#F4F6FA] px-3 py-2.5">
      <div className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide" style={{ color: MUTED }}>
        {icon}
        {label}
      </div>
      <div className="mt-1 font-display text-xl font-bold leading-none" style={{ color: NAVY }}>
        {value}
      </div>
      {sub && <div className="mt-1 text-[11px]" style={{ color: MUTED }}>{sub}</div>}
    </div>
  );
}

function Bar({ value, color, height = 6 }: { value: number | null; color: string; height?: number }) {
  return (
    <div className="w-full overflow-hidden rounded-full bg-[#E6EAF1]" style={{ height }}>
      <div className="h-full rounded-full transition-all" style={{ width: `${value ?? 0}%`, background: color }} />
    </div>
  );
}

function TableRow({
  t,
  dims,
  selected,
  onSelect,
}: {
  t: QualityTable;
  dims: { key: string; label: string }[];
  selected: boolean;
  onSelect: () => void;
}) {
  const color = gradeColor(t.grade);
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={`w-full rounded-xl border px-3 py-2.5 text-left transition hover:border-[#0F7C86] focus:outline-none focus-visible:ring-2 focus-visible:ring-[#0F7C86] ${
        selected ? "border-[#0F7C86] bg-[#F0F9FA]" : "border-[#E3E7EF] bg-white"
      }`}
    >
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate text-[13px] font-semibold capitalize" style={{ color: NAVY }}>
            {t.label.startsWith("rows in") ? t.name : t.label}
          </div>
          <div className="truncate font-mono text-[10px]" style={{ color: MUTED }}>
            {t.name} · {fmt(t.rows)} rows · {t.columns} columns{t.sampled ? " · sampled" : ""}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {t.issues > 0 ? (
            <span className="rounded-full bg-[#FFF1DC] px-2 py-0.5 text-[10px] font-semibold text-[#A85A07]">
              {t.issues} {t.issues === 1 ? "finding" : "findings"}
            </span>
          ) : (
            <span className="flex items-center gap-1 rounded-full bg-[#E5F4EC] px-2 py-0.5 text-[10px] font-semibold text-[#1E8E5A]">
              <CheckCircle2 size={10} /> Clean
            </span>
          )}
          <span className="w-10 text-right font-display text-[15px] font-bold" style={{ color }}>
            {shown(t.score)}
          </span>
        </div>
      </div>
      <div className="mt-2 flex items-center gap-3">
        <div className="flex-1">
          <Bar value={t.score} color={color} height={5} />
        </div>
        <div className="flex gap-1" aria-hidden>
          {dims.map((d) => (
            <span
              key={d.key}
              title={`${d.label}: ${shown(t.dimensions[d.key] ?? null)}`}
              className="h-2.5 w-2.5 rounded-[3px]"
              style={{ background: scoreColor(t.dimensions[d.key] ?? null) }}
            />
          ))}
        </div>
      </div>
    </button>
  );
}

function Finding({ issue }: { issue: QualityIssue }) {
  const sev = SEVERITY[issue.severity];
  return (
    <li className="rounded-xl border border-[#E3E7EF] bg-white px-3 py-2.5">
      <div className="flex items-start gap-2.5">
        <span
          className="mt-0.5 shrink-0 rounded-md px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide"
          style={{ background: sev.bg, color: sev.fg }}
        >
          {sev.label}
        </span>
        <div className="min-w-0 flex-1">
          <div className="text-[13px] font-semibold leading-snug" style={{ color: NAVY }}>
            {issue.title}
          </div>
          {issue.detail && <div className="mt-0.5 text-[11px]" style={{ color: MUTED }}>{issue.detail}</div>}
          <div className="mt-1.5 flex items-center gap-2">
            <div className="w-28 shrink-0">
              <Bar value={issue.pct} color={sev.bar} height={4} />
            </div>
            <span className="text-[11px] font-semibold" style={{ color: sev.fg }}>
              {issue.failed} of {fmt(issue.checked)} ({issue.pct}%)
            </span>
            <span className="truncate font-mono text-[10px]" style={{ color: MUTED }}>
              {issue.table}
              {issue.column ? `.${issue.column}` : ""}
            </span>
          </div>
          {issue.examples.length > 0 && (
            <div className="mt-1.5 flex flex-wrap gap-1">
              {issue.examples.map((e) => (
                <span key={e} className="rounded bg-[#F4F6FA] px-1.5 py-0.5 font-mono text-[10px]" style={{ color: MUTED }}>
                  {e}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>
    </li>
  );
}

/**
 * The data quality scorecard, drawn from the report the server computed. Nothing here is calculated by the model:
 * the gauge, the five dimensions, the per-table scores and the findings all come straight from the data.
 */
export function QualityDashboard({ report }: { report: QualityReport }) {
  const [selected, setSelected] = useState<string | null>(null);
  const [all, setAll] = useState(false);
  const o = report.overall;
  const color = gradeColor(o.grade);
  const dims = report.dimensions.map((d) => ({ key: d.key, label: d.label }));

  const findings = useMemo(() => report.issues.filter((i) => !selected || i.table === selected), [report.issues, selected]);
  const visible = all ? findings : findings.slice(0, 5);
  const total = report.tables.length;

  return (
    <section className="mt-3 overflow-hidden rounded-2xl border border-[#DDE3EE] bg-[#FAFBFD]" aria-label={report.title}>
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-[#E3E7EF] bg-white px-4 py-2.5">
        <div className="flex items-center gap-2">
          <span className="flex h-7 w-7 items-center justify-center rounded-lg text-white" style={{ background: NAVY }}>
            <ShieldAlert size={15} />
          </span>
          <div>
            <h3 className="font-display text-sm font-bold leading-tight" style={{ color: NAVY }}>
              {report.title}
            </h3>
            <div className="text-[11px]" style={{ color: MUTED }}>
              {report.scope} · as of {asOf(report.as_of)}
            </div>
          </div>
        </div>
        <span className="rounded-full px-2.5 py-1 text-[11px] font-bold text-white" style={{ background: color }}>
          {o.grade}
        </span>
      </header>

      <div className="space-y-4 p-4">
        <div className="flex flex-col items-center gap-4 sm:flex-row">
          <Gauge score={o.score} grade={o.grade} />
          <div className="grid w-full flex-1 grid-cols-2 gap-2">
            <Stat label="Checks passed" value={`${o.passed}/${o.checks}`} sub="across every table" icon={<CheckCircle2 size={11} />} />
            <Stat
              label="Findings" value={String(o.issues)} icon={<AlertTriangle size={11} />}
              sub={`${o.high} high · ${o.medium} medium · ${o.low} low`}
            />
            <Stat label="Tables" value={String(o.tables)} sub={`${o.columns} columns`} icon={<Database size={11} />} />
            <Stat label="Records" value={fmt(o.rows)} sub="rows checked" icon={<Rows3 size={11} />} />
          </div>
        </div>

        <div>
          <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide" style={{ color: MUTED }}>
            How the data scores
          </div>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
            {report.dimensions.map((d) => (
              <div key={d.key} className="rounded-xl border border-[#E3E7EF] bg-white px-2.5 py-2" title={d.question}>
                <div className="text-[11px] font-semibold" style={{ color: NAVY }}>
                  {d.label}
                </div>
                <div className="font-display text-lg font-bold leading-tight" style={{ color: scoreColor(d.score) }}>
                  {shown(d.score)}
                </div>
                <Bar value={d.score} color={scoreColor(d.score)} height={4} />
                <div className="mt-1 text-[10px] leading-tight" style={{ color: MUTED }}>
                  {d.question}
                </div>
              </div>
            ))}
          </div>
        </div>

        {total > 1 && (
          <div>
            <div className="mb-1.5 flex items-baseline justify-between">
              <div className="text-[11px] font-semibold uppercase tracking-wide" style={{ color: MUTED }}>
                By table, weakest first
              </div>
              <div className="text-[10px]" style={{ color: MUTED }}>
                Click a table to see only its findings
              </div>
            </div>
            <div className="space-y-1.5">
              {report.tables.map((t) => (
                <TableRow
                  key={t.name}
                  t={t}
                  dims={dims}
                  selected={selected === t.name}
                  onSelect={() => {
                    setSelected((s) => (s === t.name ? null : t.name));
                    setAll(false);
                  }}
                />
              ))}
            </div>
          </div>
        )}

        <div>
          <div className="mb-1.5 flex items-baseline justify-between">
            <div className="text-[11px] font-semibold uppercase tracking-wide" style={{ color: MUTED }}>
              {selected ? `Findings for ${selected}` : "What to fix first"}
            </div>
            {selected && (
              <button onClick={() => setSelected(null)} className="text-[11px] font-semibold text-[#0F7C86] hover:underline">
                Show all tables
              </button>
            )}
          </div>
          {findings.length === 0 ? (
            <div className="flex items-center gap-2 rounded-xl border border-[#CFE8DA] bg-[#EFF8F3] px-3 py-3 text-[12px] font-semibold text-[#1E8E5A]">
              <CheckCircle2 size={15} /> No problems found{selected ? " in this table" : ""}.
            </div>
          ) : (
            <ul className="space-y-1.5">
              {visible.map((i, n) => (
                <Finding key={`${i.table}-${i.column}-${n}`} issue={i} />
              ))}
            </ul>
          )}
          {findings.length > 5 && (
            <button
              onClick={() => setAll((a) => !a)}
              className="mt-2 flex items-center gap-1 text-[11px] font-semibold text-[#0F7C86] hover:underline"
            >
              {all ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
              {all ? "Show fewer" : `Show all ${findings.length} findings`}
            </button>
          )}
        </div>

        <p className="text-[10px] leading-snug" style={{ color: MUTED }}>
          Each score is the share of checks and records that passed, worked out from the data itself. A high score does not
          hide serious findings: any high-severity finding keeps the grade at Fair or below.
          {Object.keys(report.not_read).length > 0 && ` Not checked: ${Object.keys(report.not_read).join(", ")}.`}
        </p>
      </div>
    </section>
  );
}
