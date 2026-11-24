import type {
  Direction,
  GraphData,
  Health,
  ChatResponse,
  ChatStatus,
  ChatTurn,
  FabricStatus,
  ImpactRow,
  JobRun,
  JobsConfig,
  MappingCheck,
  MappingLink,
  ApplyResult,
  MappingPastRun,
  MappingReport,
  MappingReportInfo,
  MappingRunOptions,
  ImpactSummary,
  Level,
  NeoGraph,
  PageResponse,
  PiiRow,
  PiiSummary,
  PipelineRow,
  PipelineSummary,
  QueryResponse,
  SchemaInfo,
  SearchRow,
} from "./types";

/** Error carrying the Neo4j / API error code. */
export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

const BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";

type QueryValue = string | number | boolean | undefined | null;

export async function request<T>(
  path: string,
  init: RequestInit & { query?: Record<string, QueryValue> } = {},
): Promise<T> {
  const { query, ...rest } = init;
  let url = `${BASE}${path}`;
  if (query) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) if (v !== undefined && v !== null) qs.set(k, String(v));
    const s = qs.toString();
    if (s) url += `?${s}`;
  }
  let res: Response;
  try {
    res = await fetch(url, {
      ...rest,
      headers: { "Content-Type": "application/json", ...(rest.headers ?? {}) },
    });
  } catch (e) {
    if ((e as Error)?.name === "AbortError") throw e; // cancelled on purpose
    throw new ApiError(0, "Network", "Cannot reach the Lineage Studio API. Is the backend running?");
  }
  const text = await res.text();
  let body: unknown = undefined;
  try {
    body = text ? JSON.parse(text) : undefined;
  } catch {
    /* non-JSON body */
  }
  if (!res.ok) {
    const err = (body as { error?: { code?: string; message?: string } } | undefined)?.error;
    throw new ApiError(res.status, err?.code ?? `HTTP ${res.status}`, err?.message ?? (text || res.statusText));
  }
  return body as T;
}

export const api = {
  health: () => request<Health>("/api/health"),
  reconnect: () => request<Health>("/api/reconnect", { method: "POST" }),
  schema: () => request<SchemaInfo>("/api/schema"),
  schemaGraph: () => request<PageResponse>("/api/schema/graph"),

  query: (query: string, params: Record<string, unknown> = {}, signal?: AbortSignal) =>
    request<QueryResponse>("/api/query", { method: "POST", body: JSON.stringify({ query, params }), signal }),

  /** 1-hop neighbourhood by Neo4j elementId (Query page). */
  neighbors: (elementId: string) =>
    request<NeoGraph>("/api/neighbors", { query: { id: elementId, format: "graph" } }),

  /** 1-hop neighbourhood by asset id (lineage pages). */
  assetNeighbors: (id: string) =>
    request<GraphData & { cypher: string }>("/api/neighbors", { query: { id, format: "lineage" } }),

  search: (q: string, limit = 25) => request<{ rows: SearchRow[] }>("/api/search", { query: { q, limit } }),

  lineage: (id: string, direction: Direction, depth: number, level: Level, policies = true) =>
    request<PageResponse>("/api/lineage", { query: { id, direction, depth, level, policies } }),

  impact: (id: string, depth: number, level: Level, policies = true) =>
    request<PageResponse<ImpactRow, ImpactSummary>>("/api/impact", { query: { id, depth, level, policies } }),

  pii: () => request<PageResponse<PiiRow, PiiSummary>>("/api/pii"),

  pipelines: () => request<PageResponse<PipelineRow, PipelineSummary>>("/api/pipelines"),

  chatStatus: () => request<ChatStatus>("/api/chat/status"),

  chat: (messages: ChatTurn[], signal?: AbortSignal) =>
    request<ChatResponse>("/api/chat", { method: "POST", body: JSON.stringify({ messages }), signal }),

  /** The Jobs page: Fabric triggers and the policy-mapping job. */
  jobs: {
    config: () => request<JobsConfig>("/api/jobs/config"),
    fabricStatus: (refresh = false) => request<FabricStatus>("/api/jobs/fabric/status", { query: { refresh } }),
    runJob: (job: string, parameters: Record<string, unknown>, confirm: boolean) =>
      request<JobRun>("/api/jobs/fabric/run", { method: "POST", body: JSON.stringify({ job, parameters, confirm }) }),
    runChain: (chain: string, overrides: Record<string, Record<string, unknown>>, confirm: boolean) =>
      request<JobRun>("/api/jobs/fabric/chain", { method: "POST", body: JSON.stringify({ chain, overrides, confirm }) }),
    runs: () => request<JobRun[]>("/api/jobs/runs"),
    cancel: (id: string) => request<JobRun>(`/api/jobs/runs/${id}/cancel`, { method: "POST" }),
    mappingCheck: () => request<MappingCheck>("/api/jobs/mapping/check", { method: "POST" }),
    mappingRun: (opts: MappingRunOptions) => request<JobRun>("/api/jobs/mapping/run", { method: "POST", body: JSON.stringify(opts) }),
    mappingRuns: () => request<MappingPastRun[]>("/api/jobs/mapping/runs"),
    mappingReports: () => request<MappingReportInfo[]>("/api/jobs/mapping/reports"),
    mappingReport: (runId: string) => request<MappingReport>("/api/jobs/mapping/report", { query: { run_id: runId } }),
    applyReport: (runId: string, accept: { chunk_id: string; asset_id: string }[], reject: { chunk_id: string; asset_id: string }[]) =>
      request<ApplyResult>("/api/jobs/mapping/apply", { method: "POST", body: JSON.stringify({ run_id: runId, accept, reject }) }),
    mappingLinks: (runId: string, status?: string) => request<MappingLink[]>("/api/jobs/mapping/links", { query: { run_id: runId, status } }),
    decide: (runId: string, status: "accepted" | "rejected", decisions: { chunk_id: string; asset_id: string }[]) =>
      request<{ changed: number }>("/api/jobs/mapping/decide", { method: "POST", body: JSON.stringify({ run_id: runId, status, decisions }) }),
    review: (runId: string, status: "accepted" | "rejected", minConfidence: "high" | "medium") =>
      request<{ changed: number }>("/api/jobs/mapping/review", { method: "POST", body: JSON.stringify({ run_id: runId, status, min_confidence: minConfidence }) }),
    restore: (runId: string) =>
      request<{ retracted: number; restored: number }>("/api/jobs/mapping/restore", { method: "POST", body: JSON.stringify({ run_id: runId }) }),
  },

  concepts: (includeTables: boolean) =>
    request<PageResponse>("/api/concepts", { query: { include_tables: includeTables } }),
};
