// Types mirroring the FastAPI responses.

export type Json = null | boolean | number | string | Json[] | { [k: string]: Json };
export type Props = Record<string, unknown>;

// ---- raw query results (POST /api/query) -------------------------------------

export interface NeoNode {
  $type: "node";
  elementId: string;
  labels: string[];
  properties: Props;
}

export interface NeoRelationship {
  $type: "relationship";
  elementId: string;
  type: string;
  startNodeElementId: string;
  endNodeElementId: string;
  properties: Props;
}

export interface NeoPath {
  $type: "path";
  start: NeoNode;
  end: NeoNode;
  segments: { start: NeoNode; relationship: NeoRelationship; end: NeoNode }[];
  length: number;
}

export interface NeoGraph {
  nodes: NeoNode[];
  relationships: NeoRelationship[];
}

export interface QueryNotification {
  code: string;
  title: string;
  description: string;
  severity: string;
  category: string;
  position: { offset: number; line: number; column: number } | null;
}

export interface QuerySummary {
  available_after_ms: number | null;
  consumed_after_ms: number | null;
  wall_ms?: number;
  query_type: string | null;
  counters: Record<string, number>;
  notifications: QueryNotification[];
  database?: string | null;
  server?: string | null;
}

export interface QueryResponse {
  keys: string[];
  records: Record<string, unknown>[];
  graph: NeoGraph;
  summary: QuerySummary;
  truncated: boolean;
}

// ---- lineage pages ------------------------------------------------------------

export interface ViewNode {
  id: string;
  labels: string[];
  label: string;
  caption: string;
  subtitle?: string | null;
  properties: Props;
  start?: boolean;
  pii?: boolean;
}

export interface ViewEdge {
  id: string;
  source: string;
  target: string;
  type: string;
  label: string;
  properties: Props;
  reversed?: boolean;
}

export interface GraphData {
  nodes: ViewNode[];
  edges: ViewEdge[];
}

export interface PageResponse<Row = Record<string, unknown>, Summary = Record<string, unknown>>
  extends GraphData {
  rows?: Row[];
  summary?: Summary;
  start?: ViewNode;
  cypher: string;
}

export interface SearchRow {
  id: string;
  label: string;
  name: string;
  caption: string;
  subtitle?: string | null;
  platform?: string | null;
}

export type Direction = "upstream" | "downstream" | "both";
export type Level = "column" | "table";

export interface ImpactRow {
  depth: number;
  source_table: string | null;
  source_column: string;
  target_table: string | null;
  target_column: string;
  target_id: string;
  target_platform: string | null;
  transformation: string | null;
  expression: string | null;
  pipeline: string | null;
  activity: string | null;
}

export interface ImpactSummary {
  tables: number;
  columns: number;
  pipelines: number;
  table_names: string[];
  pipeline_names: string[];
  transformations: Record<string, number>;
  sentence: string;
}

export interface PiiRow {
  source_id: string;
  source_table: string | null;
  source_column: string;
  classification: string;
  comment: string | null;
  treatment: string | null;
  target_id: string | null;
  target_table: string | null;
  target_column: string | null;
  target_platform: string | null;
  target_classification?: string | null;
  pipeline: string | null;
  activity: string | null;
  expression: string | null;
  status: "OK" | "VIOLATION" | "NOT MOVED";
}

export interface PiiSummary {
  pii_columns: number;
  masked_flows: number;
  flows: number;
  violations: number;
  not_moved: number;
  ok: number;
}

export interface PipelineRow {
  pipeline: string;
  pipeline_id: string;
  activity: string;
  activity_id: string;
  activity_type: string | null;
  reads_from: string[];
  writes_to: string[];
  next: string[];
  query: string | null;
  pre_copy_script: string | null;
}

export interface PipelineSummary {
  pipelines: number;
  activities: number;
  sources: number;
  targets: number;
}

// ---- status / schema ----------------------------------------------------------

/** One turn of the conversation sent to the chatbot. */
export interface ChatTurn {
  role: "user" | "assistant";
  content: string;
}

export interface ChatCitation {
  chunk_id: string;
  doc_id: string | null;
  doc_title: string | null;
  section_path: string | null;
  heading: string | null;
}

/** A tool the chatbot ran to find its answer. */
export interface ChatStep {
  tool: string;
  arguments: Record<string, unknown>;
  summary: string;
  error: boolean;
}

/** A graph fetched from Neo4j for the chat to draw. Present only when the user asked for one. */
export interface ChatGraph extends GraphData {
  kind: "lineage" | "impact" | "policies" | "pii" | "pipelines" | "neighbors" | "ontology";
  title: string;
  cypher: string | null;
  truncated: boolean;
}

export interface QualityDimension {
  key: string;
  label: string;
  question: string;
  score: number | null;
  checked: number;
  failed: number;
  weight: number;
}

export interface QualityTable {
  name: string;
  label: string;
  rows: number;
  columns: number;
  score: number | null;
  grade: string;
  dimensions: Record<string, number | null>;
  issues: number;
  sampled: boolean;
  latest: string | null;
  key: string[] | null;
}

export interface QualityIssue {
  severity: "high" | "medium" | "low";
  dimension: string;
  table: string;
  column: string | null;
  title: string;
  detail: string;
  failed: number;
  checked: number;
  pct: number;
  examples: string[];
}

/** The data quality scorecard the chat shows under an answer about the quality of the data. */
export interface QualityReport {
  title: string;
  scope: string;
  as_of: string;
  overall: {
    score: number | null;
    grade: string;
    tables: number;
    rows: number;
    columns: number;
    checks: number;
    passed: number;
    issues: number;
    high: number;
    medium: number;
    low: number;
  };
  dimensions: QualityDimension[];
  tables: QualityTable[];
  issues: QualityIssue[];
  not_read: Record<string, string>;
}

export interface ChatResponse {
  answer: string;
  graph: ChatGraph | null;
  dashboard?: QualityReport | null;
  citations: ChatCitation[];
  steps: ChatStep[];
  tokens: number;
  model: string;
  /** True when this is an answer prepared in advance; `cached_at` says when. */
  cached?: boolean;
  cached_at?: string | null;
}

export interface ChatSuggestion {
  id: string;
  label: string;
  question: string;
  /** An answer for it was prepared in advance, so it comes back at once. */
  saved: boolean;
}

export interface ChatStatus {
  configured: boolean;
  model: string | null;
  policy_search: boolean;
  mode: "live" | "snapshot";
  suggestions?: ChatSuggestion[];
}

export interface Health {
  mode: "live" | "snapshot";
  connected: boolean;
  host: string;
  database: string;
  user: string | null;
  server_version: string | null;
  server_edition: string | null;
  error: string | null;
  max_rows: number;
  query_timeout_seconds: number;
}

export interface CountItem {
  name: string;
  count: number;
}

export interface SchemaInfo {
  labels: CountItem[];
  relationship_types: CountItem[];
  property_keys: CountItem[];
}

export interface ApiErrorBody {
  error: { code: string; message: string };
}

// ---------------------------------------------------------------------------------------------------------
// Jobs page: Fabric triggers and the policy-mapping job
// ---------------------------------------------------------------------------------------------------------

export interface JobParam {
  name: string;
  label: string;
  type: "text" | "bool" | "int" | "select";
  default: string | number | boolean | null;
  options: string[];
}

export interface JobInfo {
  key: string;
  title: string;
  description: string;
  item_type: "Notebook" | "DataPipeline";
  group: string;
  confirm: string;
  legacy: boolean;
  names: string[];
  params: JobParam[];
}

export interface ChainInfo {
  key: string;
  title: string;
  description: string;
  group: string;
  steps: string[];
}

export interface JobsConfig {
  groups: Record<string, string>;
  /** One plain sentence per group, for a reader who is not technical. */
  group_notes: Record<string, string>;
  jobs: JobInfo[];
  chains: ChainInfo[];
  fabric: { configured: boolean; auth_mode: "service_principal" | "token" | "none"; workspace: string | null; param_style: string };
  mapping: { write_enabled: boolean; live: boolean };
}

export interface FabricItemState {
  found: boolean;
  item_id: string | null;
  display_name: string | null;
  looked_for: string[];
}

export interface FabricStatus {
  configured: boolean;
  auth_mode: "service_principal" | "token" | "none";
  token_expires_in: number | null;
  workspace: string | null;
  items: Record<string, FabricItemState>;
  error: string | null;
  problems: string[];
}

export interface JobStep {
  key: string;
  title: string;
  status: string;
  message: string;
  item_id: string | null;
  instance_id: string | null;
  started_at: number | null;
  finished_at: number | null;
}

export interface MappingResult {
  run_id: string;
  summary: Record<string, unknown>;
  report_path: string;
  written: { created: number; superseded: number } | null;
  failed_chunks: string[];
  dry_run: boolean;
}

export interface JobRun {
  id: string;
  kind: "fabric" | "chain" | "mapping";
  key: string;
  title: string;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  started_at: number;
  finished_at: number | null;
  error: string | null;
  progress: { done: number; total: number } | null;
  result: MappingResult | null;
  log: string[];
  cancel_requested: boolean;
  steps: JobStep[];
}

export interface MappingCheck {
  catalogue: { tables: number; columns: number; categories: string[]; hash: string };
  chunks: { total: number; documents: number; with_live_link: number; without_link: number };
  existing_links: number;
  models: { slot: number; role: string; model: string | null }[];
  independent_verifier: boolean;
}

export interface MappingRunOptions {
  mode: "append" | "overwrite";
  write: boolean;
  confirm_overwrite: boolean;
  only_unlinked: boolean;
  no_auto_accept: boolean;
  accept_self_verified: boolean;
  docs: string[];
  limit: number | null;
  workers: number;
  use_cache: boolean;
}

export interface MappingLink {
  chunk_id: string;
  section: string;
  asset_id: string;
  asset: string;
  status: string;
  confidence: string;
  relation: string;
  evidence: string;
  reason: string;
  category: string;
  note: string;
}

export interface MappingPastRun {
  run_id: string;
  mode: string;
  started_at: string;
  created: number;
  superseded: number;
  models: Record<string, string | null>;
  links_by_status: Record<string, number>;
}

/** A saved run report: a dry run lives here until someone writes some of it. */
export interface MappingReportInfo {
  run_id: string;
  created: number;
  mode: string | null;
  chunks: number;
  links: number;
  written: boolean;
}

export interface MappingReportLink {
  chunk_id: string;
  doc_title: string;
  section_path: string;
  asset: string;
  asset_id: string;
  relation: string;
  confidence: string;
  status: string;
  basis: string;
  category: string | null;
  evidence: string;
  reason: string;
  note: string;
}

export interface MappingReport {
  run_id: string;
  written: boolean;
  summary: { mode?: string; chunks_processed?: number; to_create?: number; independent_verification?: boolean };
  links: MappingReportLink[];
}

export interface ApplyResult {
  run_id: string;
  created: number;
  accepted: number;
  rejected: number;
  skipped: { chunk_id: string; ref: string; reason: string }[];
}
