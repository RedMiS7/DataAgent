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

export interface AnswerPayload {
  question: string;
  rewritten_question: string;
  sql: string;
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

export type StreamEvent = "status" | "answer" | "error";

export interface StreamHandler {
  (event: StreamEvent, data: Record<string, unknown>): void;
}