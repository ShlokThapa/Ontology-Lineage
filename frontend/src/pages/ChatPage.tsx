import { AlertTriangle, ChevronDown, ChevronRight, FileText, Network, RotateCcw, Send, Sparkles, Square, Wrench } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import type { ChatCitation, ChatGraph, ChatStatus, ChatStep, ChatTurn } from "../api/types";
import { GraphView } from "../components/GraphView";
import { ErrorPanel } from "../components/ui";
import { Markdown } from "../lib/markdown";
import { useApp } from "../store/app";

interface Item {
  id: number;
  role: "user" | "assistant";
  content: string;
  citations?: ChatCitation[];
  steps?: ChatStep[];
  graph?: ChatGraph | null;
  model?: string;
}

const STARTERS = [
  "Which columns hold PII, and is any of it moved to Fabric unmasked?",
  "What does the loan policy say about risk classification?",
  "What is affected if I change banking.loans?",
  "Which policies govern customer data?",
  "Which pipelines write to the Ontology_Lineage lakehouse?",
  "Show me a graph of the lineage of fact.loan_portfolio.risk_bucket",
];

const TOOL_LABEL: Record<string, string> = {
  search_policies: "Searched policies",
  get_policy_section: "Read a policy section",
  get_policies_for_asset: "Looked up policies for",
  list_policy_documents: "Listed policy documents",
  find_assets: "Searched assets",
  describe_table: "Described table",
  get_lineage: "Traced lineage of",
  get_impact: "Checked impact of",
  list_pii_columns: "Listed PII columns",
  list_pipelines: "Listed pipelines",
};

// The server keeps no conversation, so the browser sends it each time; cap what goes over the wire.
const MAX_HISTORY = 30;

function stepArgs(args: Record<string, unknown>): string {
  return Object.values(args)
    .filter((v) => v !== undefined && v !== null && v !== "")
    .map(String)
    .join(", ");
}

function sectionLabel(c: ChatCitation): string {
  const where = c.section_path ?? c.heading;
  return [c.doc_title ?? c.doc_id, where].filter(Boolean).join(" › ");
}

export function ChatPage() {
  const [items, setItems] = useState<Item[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<{ code: string; message: string } | null>(null);
  const [status, setStatus] = useState<ChatStatus | null>(null);
  const nextId = useRef(1);
  const abortRef = useRef<AbortController | null>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const refreshStatus = useCallback(() => {
    api.chatStatus().then(setStatus).catch(() => setStatus(null));
  }, []);
  useEffect(refreshStatus, [refreshStatus]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [items, loading, error]);

  // Grow the box with its content, up to a limit.
  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    if (!input) {
      // Empty: let the CSS minimum apply (measuring an empty box can catch the placeholder mid-layout).
      el.style.height = "";
      return;
    }
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [input]);

  const run = useCallback(async (history: ChatTurn[]) => {
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setLoading(true);
    setError(null);
    try {
      const res = await api.chat(history.slice(-MAX_HISTORY), ctrl.signal);
      setItems((prev) => [
        ...prev,
        { id: nextId.current++, role: "assistant", content: res.answer, graph: res.graph, citations: res.citations, steps: res.steps, model: res.model },
      ]);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      const err = e as ApiError;
      setError({ code: err.code ?? "Error", message: err.message ?? "Something went wrong." });
      if (err.code === "LineageStudio.ChatNotConfigured") refreshStatus();
    } finally {
      setLoading(false);
      abortRef.current = null;
    }
  }, [refreshStatus]);

  const turns = (list: Item[]): ChatTurn[] => list.map((i) => ({ role: i.role, content: i.content }));

  const send = (text: string) => {
    const content = text.trim();
    if (!content || loading) return;
    const next = [...items, { id: nextId.current++, role: "user" as const, content }];
    setItems(next);
    setInput("");
    void run(turns(next));
  };

  const stop = () => abortRef.current?.abort();
  const reset = () => {
    abortRef.current?.abort();
    setItems([]);
    setError(null);
    setInput("");
    inputRef.current?.focus();
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      send(input);
    }
  };

  const lastIsUser = items.length > 0 && items[items.length - 1].role === "user";
  const notConfigured = status !== null && !status.configured;

  return (
    <div className="flex h-full flex-col bg-ink-950">
      <div className="flex shrink-0 items-center justify-between border-b border-ink-700 bg-white px-5 py-2.5">
        <div>
          <h1 className="font-display text-base font-bold leading-tight text-accent">Ask Lineage Studio</h1>
          <p className="text-[11px] text-ink-400">
            Answers come from your lineage graph and policy documents, read-only.
            {status?.model && <span className="ml-1 font-mono">Model: {status.model}</span>}
          </p>
        </div>
        <button
          onClick={reset}
          disabled={items.length === 0 && !input}
          className="flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs text-ink-300 hover:bg-ink-700 disabled:opacity-40"
        >
          <RotateCcw size={13} /> New chat
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto" aria-live="polite">
        <div className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-5">
          {notConfigured && (
            <Notice>
              The chatbot is not configured. Add <code className="font-mono">AZURE_OPEN_AI_KEY</code>,{" "}
              <code className="font-mono">AZURE_OPEN_AI_ENDPOINT</code> and{" "}
              <code className="font-mono">AZURE_OPEN_AI_MODEL_NAME</code> to <code className="font-mono">.env</code> and restart the backend.
            </Notice>
          )}
          {status?.configured && !status.policy_search && (
            <Notice>
              {status.mode === "snapshot"
                ? "Snapshot mode has no policy documents, so policy questions can't be answered. Lineage, PII and pipeline questions work."
                : "Policy search is unavailable: the local embedding model is not installed. Run scripts/fetch_embedding_model.py, then reload. Lineage and PII questions still work."}
            </Notice>
          )}

          {items.length === 0 && !loading && (
            <div className="flex flex-col items-center gap-4 py-10 text-center">
              <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-accent text-white shadow-soft">
                <Sparkles size={22} />
              </div>
              <div>
                <div className="text-sm font-semibold text-ink-100">What would you like to know?</div>
                <div className="mt-1 text-xs text-ink-400">Lineage, impact, PII handling, pipelines, and what your policies say.</div>
              </div>
              <div className="flex max-w-xl flex-wrap justify-center gap-2">
                {STARTERS.map((q) => (
                  <button
                    key={q}
                    onClick={() => send(q)}
                    disabled={notConfigured}
                    className="rounded-full border border-ink-600 bg-white px-3 py-1.5 text-left text-xs text-ink-200 shadow-soft transition hover:border-accent hover:text-accent disabled:opacity-50"
                  >
                    {q}
                  </button>
                ))}
              </div>
            </div>
          )}

          {items.map((m) => (m.role === "user" ? <UserBubble key={m.id} text={m.content} /> : <AnswerCard key={m.id} item={m} />))}

          {loading && (
            <div className="flex items-center gap-2 text-xs text-ink-400" role="status">
              <span className="flex gap-1" aria-hidden>
                {[0, 1, 2].map((d) => (
                  <span key={d} className="h-1.5 w-1.5 animate-bounce rounded-full bg-accent" style={{ animationDelay: `${d * 140}ms` }} />
                ))}
              </span>
              Looking through the graph…
            </div>
          )}

          {error && (
            <div className="space-y-2">
              <ErrorPanel code={error.code} message={error.message} />
              {lastIsUser && (
                <button
                  onClick={() => void run(turns(items))}
                  className="flex items-center gap-1.5 rounded-md border border-ink-600 bg-white px-2.5 py-1.5 text-xs text-ink-200 hover:border-accent hover:text-accent"
                >
                  <RotateCcw size={12} /> Try again
                </button>
              )}
            </div>
          )}
          <div ref={endRef} />
        </div>
      </div>

      <div className="shrink-0 border-t border-ink-700 bg-white px-4 py-3">
        <div className="mx-auto flex max-w-3xl items-end gap-2">
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKeyDown}
            rows={1}
            maxLength={8000}
            aria-label="Your question"
            placeholder="Ask about lineage, PII, pipelines or policies…  (Enter to send, Shift+Enter for a new line)"
            className="max-h-40 min-h-[40px] flex-1 resize-none rounded-xl border border-ink-600 bg-white px-3 py-2 text-[13px] text-ink-100 outline-none placeholder:text-ink-500 focus:border-accent focus:ring-2 focus:ring-accent/20"
          />
          {loading ? (
            <button onClick={stop} title="Stop" aria-label="Stop" className="flex h-10 w-10 items-center justify-center rounded-xl bg-ink-700 text-ink-200 hover:bg-ink-600">
              <Square size={15} fill="currentColor" />
            </button>
          ) : (
            <button
              onClick={() => send(input)}
              disabled={!input.trim() || notConfigured}
              title="Send"
              aria-label="Send"
              className="flex h-10 w-10 items-center justify-center rounded-xl bg-accent text-white hover:bg-[#0099A8] disabled:opacity-40"
            >
              <Send size={16} />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function Notice({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex items-start gap-2 rounded-xl border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900">
      <AlertTriangle size={14} className="mt-0.5 shrink-0" />
      <div>{children}</div>
    </div>
  );
}

function UserBubble({ text }: { text: string }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[85%] whitespace-pre-wrap break-words rounded-2xl rounded-br-md bg-accent px-3.5 py-2 text-[13px] text-white shadow-soft">
        {text}
      </div>
    </div>
  );
}

// Policy and neighbourhood graphs are clusters; lineage-style graphs read left to right.
function GraphBlock({ graph }: { graph: ChatGraph }) {
  const openInQuery = useApp((s) => s.openInQuery);
  const boxRef = useRef<HTMLDivElement>(null);

  // A graph inside a scrolling chat must not swallow the mouse wheel: plain scrolling moves the chat, and
  // zooming needs Ctrl (a trackpad pinch also arrives as Ctrl + wheel). A native capture listener is the only
  // way to stop the graph library's own wheel handler, which React's synthetic events cannot reach.
  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey && !e.metaKey) e.stopPropagation();
    };
    el.addEventListener("wheel", onWheel, { capture: true });
    return () => el.removeEventListener("wheel", onWheel, { capture: true });
  }, []);

  const layout = ["policies", "neighbors", "ontology"].includes(graph.kind) ? "force" : "hierarchical";
  return (
    <div className="mt-3 border-t border-ink-700 pt-2">
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-1.5 text-[11px] font-semibold text-ink-200">
          <Network size={13} className="shrink-0 text-accent" />
          <span className="truncate">{graph.title}</span>
          <span className="shrink-0 font-normal text-ink-400">
            {graph.nodes.length} nodes · {graph.edges.length} edges
          </span>
        </div>
        {graph.cypher && (
          <button
            onClick={() => openInQuery(graph.cypher as string)}
            className="shrink-0 rounded px-2 py-0.5 text-[11px] text-accent hover:bg-ink-800"
            title="Load the Cypher behind this graph into the Query page"
          >
            Open in Query
          </button>
        )}
      </div>
      {graph.truncated && (
        <div className="mb-1 text-[11px] text-amber-700">Large graph: showing the first {graph.nodes.length} nodes.</div>
      )}
      <div ref={boxRef}>
        <GraphView data={graph} layout={layout} nodeShape="box" filenameBase="chat-graph" className="h-[380px]" />
      </div>
      <div className="mt-1 text-[10px] text-ink-400">Drag to pan · Ctrl + scroll to zoom · double-click a node to expand</div>
    </div>
  );
}

function AnswerCard({ item }: { item: Item }) {
  const [open, setOpen] = useState(false);
  const steps = item.steps ?? [];
  const citations = item.citations ?? [];
  return (
    <div className="flex">
      <div className="min-w-0 max-w-full flex-1 rounded-2xl rounded-bl-md border border-ink-700 bg-white px-4 py-3 shadow-soft">
        <Markdown text={item.content} />

        {item.graph && <GraphBlock graph={item.graph} />}

        {citations.length > 0 && (
          <div className="mt-3 border-t border-ink-700 pt-2">
            <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-ink-400">Policy sections consulted</div>
            <div className="flex flex-wrap gap-1.5">
              {citations.map((c) => (
                <span
                  key={c.chunk_id}
                  title={c.chunk_id}
                  className="flex items-center gap-1 rounded-md bg-ink-800 px-2 py-1 text-[11px] text-ink-200"
                >
                  <FileText size={11} className="shrink-0 text-accent" />
                  {sectionLabel(c)}
                </span>
              ))}
            </div>
          </div>
        )}

        {steps.length > 0 && (
          <div className="mt-2">
            <button onClick={() => setOpen((o) => !o)} className="flex items-center gap-1 text-[11px] text-ink-400 hover:text-ink-100">
              {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
              <Wrench size={11} /> How this was found ({steps.length} {steps.length === 1 ? "step" : "steps"})
            </button>
            {open && (
              <ul className="mt-1.5 space-y-1 text-[11px] text-ink-300">
                {steps.map((s, i) => (
                  <li key={i} className="flex gap-1.5">
                    <span className="text-ink-500">{i + 1}.</span>
                    <span>
                      {TOOL_LABEL[s.tool] ?? s.tool}
                      {stepArgs(s.arguments) && <span className="ml-1 font-mono text-ink-100">{stepArgs(s.arguments)}</span>}
                      <span className={`ml-1.5 ${s.error ? "text-red-700" : "text-ink-400"}`}>
                        → {s.summary}
                      </span>
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
