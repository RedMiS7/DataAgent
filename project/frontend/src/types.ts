import type { AnswerMode, AskMode, RouteChoice } from "./lib/modes";

export type ChartType = "line" | "bar" | "pie" | "table";

export interface ChartSpec {
  type: ChartType;
  x_field: string;
  y_fields: string[];
  title: string;
}

export interface TableBlock {
  columns: string[];
  rows: unknown[][];
  truncated: boolean;
  row_count: number;
}

export interface MetricNote {
  name: string;
  definition: string;
  formula: string;
}

export interface RetrievalItem {
  table: string;
  column: string;
  score: number;
  source: string;
}

export interface MetricHitView {
  name: string;
  score: number;
  source: string;
}

export interface EntityHitView {
  type: string;
  value: string;
  source: string;
}

export interface RetrievalView {
  tables: string[];
  schema_items: RetrievalItem[];
  metrics: MetricHitView[];
  cases: { id: number; question: string; score: number }[];
  entities: EntityHitView[];
  degraded: boolean;
  degraded_reason: string;
}

/**
 * "partial": some queries ran but the answer is incomplete; "error": no query result;
 * "text": answered without querying, e.g. a metric definition.
 */
export type AnswerStatus = "ok" | "empty" | "partial" | "text" | "refused" | "blocked" | "error";

export type FailureKind =
  | "timeout"
  | "sql_failed"
  | "model_error"
  | "no_answer"
  | "step_failed"
  | "unexpected";

/** What went wrong in a partial or failed answer, in words for the user. */
export interface Failure {
  kind: FailureKind;
  message: string;
  suggestion: string;
}

export interface QueryResult {
  index: number;
  purpose: string;
  sql: string;
  table: TableBlock | null;
  chart: ChartSpec | null;
  /** The plan step that ran this query; null when analysis ran without a plan. */
  step_id?: number | null;
}

export type PlanStepStatus = "pending" | "running" | "done" | "failed";

export interface PlanStep {
  id: number;
  goal: string;
  origin: "plan" | "supplement";
  status: PlanStepStatus;
}

export interface HistoryMessage {
  role: "user" | "assistant";
  content: string;
  mode?: AnswerMode;
  /** How a reply ended; "stopped" means the user stopped it before an answer. */
  status?: AnswerStatus | "stopped";
}

export interface RouteInfo {
  requested: AskMode;
  chosen: RouteChoice;
  reason: string;
  source: "llm" | "fallback";
}

export interface CallUsage {
  model_calls: number;
  tool_calls: number;
  input_tokens: number;
  output_tokens: number;
  /** Summed over calls; steps running at once make it exceed the answer's duration. */
  model_ms: number;
}

export interface AnswerPayload {
  question: string;
  rewritten_question: string;
  sql: string;
  status: AnswerStatus;
  /** Set on partial and failed answers; missing on answers saved before it existed. */
  failure?: Failure | null;
  table: TableBlock | null;
  chart: ChartSpec | null;
  summary: string;
  summary_source: "model" | "template";
  metrics: MetricNote[];
  time_range: Record<string, string>;
  tables_used: string[];
  retrieval: RetrievalView;
  retries: number;
  log_id: number | null;
  degraded: boolean;
  warnings?: string[];
  /** Missing on answers saved before analysis mode existed. */
  mode?: AnswerMode;
  results?: QueryResult[];
  /** Set when the question was sent in auto mode. */
  route?: RouteInfo | null;
  /** The steps of a planned analysis; empty or missing otherwise. */
  plan?: PlanStep[];
  /** Missing on answers saved before call tracing. */
  usage?: CallUsage | null;
  /** Not sent by the backend yet; shown only when present. */
  model?: string;
}

export type SetupStepState = "pending" | "running" | "done" | "waiting" | "skipped" | "failed";

export interface SetupStep {
  key: string;
  label: string;
  state: SetupStepState;
  detail: string;
}

export interface SetupStatus {
  ready: boolean;
  llm_configured: boolean;
  embedding_configured: boolean;
  steps: SetupStep[];
  orders_done: number;
  orders_total: number;
  error: string;
}

export interface MetaInfo {
  as_of_date: string;
  sql_max_rows: number;
  sql_timeout_ms: number;
  llm_configured: boolean;
  embedding_configured: boolean;
  qdrant_available: boolean;
  business_db_available: boolean;
}

export interface CaseOut {
  id: number;
  question: string;
  sql_text: string;
  result_summary: string;
  status: string;
  created_at: string;
}

export interface AttributionFactor {
  dimension: string;
  name: string;
  current_refund: number;
  previous_refund: number;
  delta: number;
  share: number;
}

export interface AttributionResult {
  metric: string;
  current: Record<string, string>;
  previous: Record<string, string>;
  current_rate: number;
  previous_rate: number;
  delta_points: number;
  total_current_refund: number;
  total_previous_refund: number;
  summary: string;
  summary_source: string;
  factors: AttributionFactor[];
  sql: string[];
}

export type StreamEvent = "status" | "reasoning" | "plan" | "step" | "answer" | "error";

export interface StreamHandler {
  (event: StreamEvent, data: Record<string, unknown>): void;
}