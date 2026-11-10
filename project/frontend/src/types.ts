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
 * "text": answered without querying, e.g. a metric definition; "clarify": the question
 * was ambiguous or lacked something, and the reply asks the user instead of answering.
 */
export type AnswerStatus =
  | "ok" | "empty" | "partial" | "text" | "refused" | "blocked" | "error" | "clarify";

/** A question asked back to the user, with answers to pick from. */
export interface Clarify {
  question: string;
  options: string[];
  /** 1 for the first question asked about a user question, 2 for the second. */
  round: number;
}

/** The query rows and columns a point rests on. */
export interface Evidence {
  /** The query's number, as QueryResult.index and the [查询n] marks give it. */
  query: number;
  /** Row numbers from 0; empty means every row. */
  rows: number[];
  /** Column names; empty means every column. */
  columns: string[];
  /** The cited cells, cut from the query result by the backend. */
  table: TableBlock | null;
}

export interface ConclusionPoint {
  claim: string;
  evidence: Evidence[];
}

/** The model's conclusion as a structure: what the answer's summary text is made from. */
export interface Conclusion {
  judgement: string;
  points: ConclusionPoint[];
  advice: string;
  /** False when some evidence was still wrong after a rewrite and was left out. */
  checked: boolean;
}

/** How the question was understood. */
export interface IntentInfo {
  /** One line for the user, e.g. "时间：上个月；指标：销售额". */
  text: string;
  /** What was filled in without asking, e.g. the default period. */
  assumptions: string[];
  /** The structure; sent back with the conversation. */
  data: Record<string, unknown>;
}

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
  /** The queries a reply rests on, so a follow-up can count the same way. */
  queries?: HistoryQuery[];
  /** The period the reply covered, as the answer's time_range gives it. */
  time_range?: Record<string, string>;
  /** How the reply understood its question, so a follow-up can build on it. */
  intent?: Record<string, unknown>;
}

export interface HistoryQuery {
  purpose: string;
  sql: string;
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
  /** Missing when the intent could not be read, and on answers saved before it existed. */
  intent?: IntentInfo | null;
  /** Set when status is "clarify". */
  clarify?: Clarify | null;
  /** Missing when no conclusion could be read, and on answers saved before it existed. */
  conclusion?: Conclusion | null;
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