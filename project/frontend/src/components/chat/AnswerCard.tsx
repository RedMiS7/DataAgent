import { useMemo, useRef, useState } from "react";
import {
  CopyOutlined,
  DislikeOutlined,
  DownloadOutlined,
  ExclamationCircleOutlined,
  LikeOutlined,
  ReloadOutlined,
  StopOutlined,
} from "@ant-design/icons";
import { Tooltip } from "antd";
import { formatNumber } from "../../lib/chart";
import type { AssistantMessage } from "../../lib/conversations";
import { downloadCsv } from "../../lib/csv";
import { canGiveFeedback, failureNotice, sqlTabLabel } from "../../lib/failure";
import { groupResults } from "../../lib/plan";
import { usageDetail, usageLabel } from "../../lib/usage";
import { suggestsAnalysis } from "../../lib/modes";
import { parseAnalysisSummary, type SummaryPart } from "../../lib/summary";
import type { AnswerPayload, PlanStep, QueryResult, TableBlock } from "../../types";
import ChartView from "../ChartView";
import ResultTable from "../ResultTable";

type Tab = "chart" | "table" | "sql" | "metrics" | "retrieval";

const MAX_STATS = 4;

function isStatResult(table: TableBlock | null): table is TableBlock {
  return table !== null && table.rows.length === 1 && table.columns.length <= MAX_STATS;
}

function StatRow({ table }: { table: TableBlock }) {
  return (
    <div className="stat-row">
      {table.columns.map((name, index) => (
        <div className="stat" key={name}>
          <div className="stat-label">{name}</div>
          <div className="stat-value">{formatNumber(table.rows[0][index])}</div>
        </div>
      ))}
    </div>
  );
}

function RetrievalView({ answer }: { answer: AnswerPayload }) {
  const retrieval = answer.retrieval;
  const rows: [string, string][] = [
    ["字段", retrieval.schema_items.map((item) => `${item.table}.${item.column}`).join("、")],
    ["指标", retrieval.metrics.map((item) => item.name).join("、")],
    ["实体", retrieval.entities.map((item) => item.value).join("、")],
    ["案例", retrieval.cases.map((item) => item.question).join("；")],
  ];
  return (
    <dl className="detail-list">
      {rows.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>{value || "无"}</dd>
        </div>
      ))}
    </dl>
  );
}

function AnswerBody({ answer, onCopy }: { answer: AnswerPayload; onCopy: (text: string) => void }) {
  const { table, chart } = answer;
  const hasRows = Boolean(table?.rows.length);
  const hasChart = hasRows && !isStatResult(table) && chart !== null && chart.type !== "table";

  const tabs = useMemo(() => {
    const options: { label: string; value: Tab }[] = [];
    if (hasChart) options.push({ label: "图表", value: "chart" });
    if (hasRows && !isStatResult(table)) options.push({ label: "表格", value: "table" });
    if (answer.metrics.length) {
      options.push({ label: `口径 ${answer.metrics.length}`, value: "metrics" });
    }
    if (answer.sql) options.push({ label: sqlTabLabel(answer), value: "sql" });
    options.push({ label: "检索", value: "retrieval" });
    return options;
  }, [answer, hasChart, hasRows, table]);

  const [picked, setPicked] = useState<Tab | "none" | null>(null);
  const defaultTab: Tab | null = hasChart ? "chart" : hasRows && !isStatResult(table) ? "table" : null;
  const tab = picked === "none" ? null : picked ?? defaultTab;

  return (
    <>
      {isStatResult(table) && <StatRow table={table} />}
      <div className="answer-tabs" role="group" aria-label="查看详情">
        {tabs.map((option) => (
          <button
            key={option.value}
            type="button"
            aria-pressed={tab === option.value}
            className={tab === option.value ? "active" : undefined}
            onClick={() => setPicked(tab === option.value ? "none" : option.value)}
          >
            {option.label}
          </button>
        ))}
      </div>
      {tab && (
        <div className="answer-panel">
          {tab === "chart" && chart && table && <ChartView chart={chart} table={table} />}
          {tab === "table" && table && <ResultTable table={table} />}
          {tab === "sql" && (
            <div className="code-block">
              <button type="button" className="code-copy" onClick={() => onCopy(answer.sql)}>复制</button>
              <pre>{answer.sql}</pre>
            </div>
          )}
          {tab === "metrics" && (
            <dl className="detail-list">
              {answer.metrics.map((metric) => (
                <div key={metric.name}>
                  <dt>{metric.name}</dt>
                  <dd>
                    {metric.definition}
                    {metric.formula && <code className="formula">{metric.formula}</code>}
                  </dd>
                </div>
              ))}
            </dl>
          )}
          {tab === "retrieval" && <RetrievalView answer={answer} />}
        </div>
      )}
    </>
  );
}

function FailureView({ answer }: { answer: AnswerPayload }) {
  const notice = failureNotice(answer);
  if (!notice) return null;
  return (
    <div
      className={`notice failure-notice ${notice.tone === "error" ? "notice-error" : "notice-refused"}`}
      role={notice.tone === "error" ? "alert" : "status"}
    >
      <ExclamationCircleOutlined />
      <div>
        <div>{notice.message}</div>
        {notice.suggestion && <div className="failure-suggestion">{notice.suggestion}</div>}
      </div>
    </div>
  );
}

function SummaryText({ parts, onCite }: { parts: SummaryPart[]; onCite: (index: number) => void }) {
  return (
    <>
      {parts.map((part, position) =>
        typeof part === "string" ? (
          part
        ) : (
          <button
            type="button"
            className="cite"
            key={position}
            aria-label={`查看查询 ${part.query}`}
            onClick={() => onCite(part.query)}
          >
            查询{part.query}
          </button>
        ),
      )}
    </>
  );
}

function AnalysisSummaryView({ text, onCite }: { text: string; onCite: (index: number) => void }) {
  const { headline, points } = useMemo(() => parseAnalysisSummary(text), [text]);
  return (
    <>
      <h2 className="answer-headline"><SummaryText parts={headline} onCite={onCite} /></h2>
      {points.length > 0 && (
        <ul className="analysis-points">
          {points.map((parts, position) => (
            <li key={position}><SummaryText parts={parts} onCite={onCite} /></li>
          ))}
        </ul>
      )}
    </>
  );
}

function ResultButtons({ results, active, onPick }: {
  results: QueryResult[];
  active: QueryResult;
  onPick: (index: number) => void;
}) {
  return (
    <>
      {results.map((result) => (
        <button
          key={result.index}
          type="button"
          aria-pressed={result.index === active.index}
          className={result.index === active.index ? "active" : undefined}
          onClick={() => onPick(result.index)}
        >
          <span className="result-switch-index">查询{result.index}</span>
          {result.purpose}
        </button>
      ))}
    </>
  );
}

function ResultSwitch({ results, plan, active, onPick }: {
  results: QueryResult[];
  plan: PlanStep[] | undefined;
  active: QueryResult;
  onPick: (index: number) => void;
}) {
  const groups = groupResults(results, plan);
  if (groups.length === 1 && groups[0].step === null) {
    return (
      <div className="result-switch" role="group" aria-label="选择查询">
        <ResultButtons results={results} active={active} onPick={onPick} />
      </div>
    );
  }
  return (
    <div className="result-groups">
      {groups.map(({ step, results: items }) => {
        const label = step ? `第${step.id}步：${step.goal}` : "其他查询";
        return (
          <div className="result-group" key={step?.id ?? "rest"}>
            <div className="result-group-label">{label}</div>
            <div className="result-switch" role="group" aria-label={label}>
              <ResultButtons results={items} active={active} onPick={onPick} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

function Meta({ answer }: { answer: AnswerPayload }) {
  const range = answer.time_range;
  return (
    <div className="answer-meta">
      {answer.route && (
        <Tooltip title={answer.route.reason || undefined}>
          <span className={answer.route.source === "fallback" ? "warn" : undefined}>
            自动判断：{answer.route.chosen === "analysis" ? "分析" : "查询"}
          </span>
        </Tooltip>
      )}
      {range.label && (
        <span>{range.label}（{range.start} 至 {range.end_exclusive}，不含右端）</span>
      )}
      {answer.tables_used.length > 0 && <span>{answer.tables_used.join("、")}</span>}
      {answer.retries > 1 && <span>尝试 {answer.retries} 次</span>}
      {answer.model && <span>{answer.model}</span>}
      {answer.usage && usageLabel(answer.usage) && (
        <Tooltip title={usageDetail(answer.usage)}>
          <span>{usageLabel(answer.usage)}</span>
        </Tooltip>
      )}
      {answer.summary_source === "template" && answer.summary && <span>模板结论</span>}
      {answer.degraded && <span className="warn">检索已降级</span>}
    </div>
  );
}

interface Props {
  message: AssistantMessage;
  canRegenerate: boolean;
  onRegenerate: () => void;
  onReanalyze: () => void;
  onFeedback: (verdict: "correct" | "wrong") => void;
  onCopy: (text: string) => void;
}

export default function AnswerCard({
  message, canRegenerate, onRegenerate, onReanalyze, onFeedback, onCopy,
}: Props) {
  const { answer, error, state, verdict } = message;
  const refused = answer?.status === "refused";
  const analysis = answer?.mode === "analysis";
  const results = answer?.results ?? [];
  const [picked, setPicked] = useState<number | null>(null);
  const resultsRef = useRef<HTMLDivElement>(null);
  const activeResult = results.find((result) => result.index === picked) ?? results[0];
  const exportTable = activeResult?.table ?? answer?.table;

  const cite = (index: number) => {
    setPicked(index);
    resultsRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  };

  return (
    <div className="answer">
      {state === "stopped" && (
        <div className="notice"><StopOutlined /> 已停止生成</div>
      )}
      {state === "error" && (
        <div className="notice notice-error"><ExclamationCircleOutlined /> {error}</div>
      )}

      {answer && refused && (
        <div className="notice notice-refused"><StopOutlined /> {answer.summary}</div>
      )}
      {answer && !refused && (
        <>
          <FailureView answer={answer} />
          {answer.summary && (analysis ? (
            <AnalysisSummaryView text={answer.summary} onCite={cite} />
          ) : (
            <h2 className="answer-headline">{answer.summary}</h2>
          ))}
          {answer.rewritten_question && answer.rewritten_question !== answer.question && (
            <p className="answer-rewrite">理解为：{answer.rewritten_question}</p>
          )}
          <Meta answer={answer} />
          {answer.warnings?.map((warning) => (
            <div className="notice notice-refused answer-warning" key={warning}>
              <ExclamationCircleOutlined /> {warning}
            </div>
          ))}
          <div ref={resultsRef}>
            {activeResult && results.length > 1 && (
              <ResultSwitch
                results={results}
                plan={answer.plan}
                active={activeResult}
                onPick={setPicked}
              />
            )}
            <AnswerBody
              key={activeResult?.index ?? "answer"}
              answer={activeResult ? { ...answer, ...activeResult } : answer}
              onCopy={onCopy}
            />
          </div>
          {!analysis && canRegenerate && suggestsAnalysis(answer.question) && (
            <button type="button" className="reanalyze" onClick={onReanalyze}>
              用分析模式重新回答
            </button>
          )}
        </>
      )}

      {state !== "streaming" && (
        <div className="message-actions always">
          {answer?.summary && (
            <Tooltip title="复制结论">
              <button type="button" aria-label="复制结论" onClick={() => onCopy(answer.summary)}>
                <CopyOutlined />
              </button>
            </Tooltip>
          )}
          {canRegenerate && (
            <Tooltip title="重新生成">
              <button type="button" aria-label="重新生成" onClick={onRegenerate}>
                <ReloadOutlined />
              </button>
            </Tooltip>
          )}
          {exportTable && exportTable.rows.length > 0 && (
            <Tooltip title="导出 CSV">
              <button
                type="button"
                aria-label="导出 CSV"
                onClick={() => downloadCsv(exportTable, "dataagent-result.csv")}
              >
                <DownloadOutlined />
              </button>
            </Tooltip>
          )}
          {canGiveFeedback(answer) && (
            <>
              <Tooltip title="回答正确">
                <button
                  type="button"
                  aria-label="回答正确"
                  aria-pressed={verdict === "correct"}
                  className={verdict === "correct" ? "active" : undefined}
                  onClick={() => onFeedback("correct")}
                >
                  <LikeOutlined />
                </button>
              </Tooltip>
              <Tooltip title="回答错误">
                <button
                  type="button"
                  aria-label="回答错误"
                  aria-pressed={verdict === "wrong"}
                  className={verdict === "wrong" ? "active" : undefined}
                  onClick={() => onFeedback("wrong")}
                >
                  <DislikeOutlined />
                </button>
              </Tooltip>
            </>
          )}
        </div>
      )}
    </div>
  );
}
