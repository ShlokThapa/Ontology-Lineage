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

export interface ChatResponse {
  answer: string;
  graph: ChatGraph | null;
  citations: ChatCitation[];
  steps: ChatStep[];
  tokens: number;
  model: string;
}

export interface ChatStatus {
  configured: boolean;
  model: string | null;
  policy_search: boolean;
  mode: "live" | "snapshot";
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
