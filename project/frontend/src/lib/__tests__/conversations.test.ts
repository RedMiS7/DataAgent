import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  answerQueries,
  buildHistory,
  createConversation,
  deriveTitle,
  groupConversations,
  loadActiveId,
  loadConversations,
  restoreConversations,
  saveActiveId,
  saveConversations,
  searchConversations,
  settleMessages,
  type ChatMessage,
  type Conversation,
} from "../conversations";
import { startThinking } from "../reasoning";

const NOW = new Date(2026, 10, 5, 12).getTime();
const DAY = 24 * 60 * 60 * 1000;

function conversation(title: string, updatedAt: number, text = title): Conversation {
  return {
    id: title,
    title,
    updatedAt,
    messages: [{ id: `${title}-u`, role: "user", text }],
  };
}

function fakeStorage(limit = Infinity) {
  const store = new Map<string, string>();
  return {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => {
      if (value.length > limit) throw new Error("quota");
      store.set(key, value);
    },
  };
}

describe("titles", () => {
  it("trims and shortens", () => {
    expect(deriveTitle("  上个月  GMV ")).toBe("上个月 GMV");
    expect(deriveTitle("字".repeat(30))).toBe(`${"字".repeat(24)}…`);
  });
});

describe("groupConversations", () => {
  it("buckets by calendar day, newest first", () => {
    const groups = groupConversations(
      [
        conversation("old", NOW - 30 * DAY),
        conversation("week", NOW - 3 * DAY),
        conversation("yesterday", NOW - DAY),
        conversation("earlier", NOW - 60_000),
        conversation("latest", NOW),
      ],
      NOW,
    );
    expect(groups.map((group) => group.label)).toEqual(["今天", "昨天", "近 7 天", "更早"]);
    expect(groups[0].items.map((item) => item.id)).toEqual(["latest", "earlier"]);
  });
});

describe("searchConversations", () => {
  it("matches titles and user messages", () => {
    const items = [conversation("销量", NOW), conversation("其他", NOW, "看看退货率")];
    expect(searchConversations(items, "退货").map((item) => item.id)).toEqual(["其他"]);
    expect(searchConversations(items, "  ")).toHaveLength(2);
  });
});

describe("persistence", () => {
  beforeEach(() => {
    vi.stubGlobal("localStorage", fakeStorage());
  });

  it("does not store empty conversations", () => {
    saveConversations([createConversation(NOW), conversation("有内容", NOW)]);
    expect(loadConversations(NOW).map((item) => item.id)).toEqual(["有内容"]);
  });

  it("drops the oldest conversations when storage is full", () => {
    vi.stubGlobal("localStorage", fakeStorage(400));
    const items = Array.from({ length: 8 }, (_, index) =>
      conversation(`c${index}`, NOW - index * 1000, "问题".repeat(10)));
    saveConversations(items);
    const kept = loadConversations(NOW);
    expect(kept.length).toBeGreaterThan(0);
    expect(kept.length).toBeLessThan(8);
    expect(kept[0].id).toBe("c0");
  });

  it("survives corrupted or blocked storage", () => {
    localStorage.setItem("dataagent.conversations", "{oops");
    expect(loadConversations(NOW)).toEqual([]);
    vi.stubGlobal("localStorage", {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    });
    expect(loadConversations(NOW)).toEqual([]);
    expect(() => saveConversations([conversation("a", NOW)])).not.toThrow();
  });

  it("does not restore a message as still streaming", () => {
    const settled = settleMessages(
      [{
        ...createConversation(NOW),
        messages: [{ id: "a", role: "assistant", state: "streaming", thinking: startThinking(NOW) }],
      }],
      NOW + 5,
    );
    expect(settled[0].messages[0]).toMatchObject({ state: "stopped" });
  });
});

describe("restoreConversations", () => {
  const fresh = conversation("新", NOW);
  const stored = [conversation("甲", NOW), conversation("乙", NOW)];

  it("reopens the last conversation without adding an empty one", () => {
    expect(restoreConversations(stored, "乙", fresh)).toEqual({ items: stored, activeId: "乙" });
  });

  it("starts a new conversation when the last one is gone", () => {
    expect(restoreConversations(stored, "已删除", fresh)).toEqual({
      items: [fresh, ...stored],
      activeId: "新",
    });
    expect(restoreConversations(stored, null, fresh).activeId).toBe("新");
  });
});

describe("active conversation id", () => {
  it("survives a reload", () => {
    vi.stubGlobal("localStorage", fakeStorage());
    saveActiveId("甲");
    expect(loadActiveId()).toBe("甲");
  });

  it("is simply missing when storage is blocked", () => {
    vi.stubGlobal("localStorage", {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    });
    expect(() => saveActiveId("甲")).not.toThrow();
    expect(loadActiveId()).toBeNull();
  });
});

describe("buildHistory", () => {
  const user = (text: string): ChatMessage => ({ id: text, role: "user", text });
  const done = (summary: string, mode?: "query" | "analysis"): ChatMessage => ({
    id: summary,
    role: "assistant",
    state: "done",
    thinking: startThinking(NOW),
    answer: { summary, mode } as never,
  });
  const stopped: ChatMessage = {
    id: "s",
    role: "assistant",
    state: "stopped",
    thinking: startThinking(NOW),
  };
  const failed: ChatMessage = {
    id: "e",
    role: "assistant",
    state: "error",
    error: "失败",
    thinking: startThinking(NOW),
  };

  it("is empty for a new conversation", () => {
    expect(buildHistory([])).toEqual([]);
  });

  it("keeps every question and finished answer with its mode", () => {
    expect(buildHistory([user("最近生意怎么样"), done("整体平稳 [查询1]", "analysis"),
                         user("上个月 GMV"), done("966.6 万", "query")])).toEqual([
      { role: "user", content: "最近生意怎么样" },
      { role: "assistant", content: "整体平稳 [查询1]", mode: "analysis" },
      { role: "user", content: "上个月 GMV" },
      { role: "assistant", content: "966.6 万", mode: "query" },
    ]);
  });

  it("keeps replies that stopped or failed, so no question is left open", () => {
    expect(buildHistory([user("甲"), stopped, user("乙"), failed])).toEqual([
      { role: "user", content: "甲" },
      { role: "assistant", content: "", status: "stopped" },
      { role: "user", content: "乙" },
      { role: "assistant", content: "失败", status: "error" },
    ]);
  });

  it("sends the reason of a failed answer and the status of every answer", () => {
    const failedAnswer: ChatMessage = {
      id: "f",
      role: "assistant",
      state: "done",
      thinking: startThinking(NOW),
      answer: {
        summary: "",
        mode: "query",
        status: "error",
        failure: { kind: "sql_failed", message: "SQL 试了 3 次都没有成功。", suggestion: "" },
      } as never,
    };
    const partial: ChatMessage = {
      id: "p",
      role: "assistant",
      state: "done",
      thinking: startThinking(NOW),
      answer: { summary: "共 3 行。", mode: "analysis", status: "partial" } as never,
    };
    expect(buildHistory([user("甲"), failedAnswer, user("乙"), partial])).toEqual([
      { role: "user", content: "甲" },
      { role: "assistant", content: "SQL 试了 3 次都没有成功。", mode: "query", status: "error" },
      { role: "user", content: "乙" },
      { role: "assistant", content: "共 3 行。", mode: "analysis", status: "partial" },
    ]);
  });

  it("sends the SQL and period of a query answer", () => {
    const answered: ChatMessage = {
      id: "q",
      role: "assistant",
      state: "done",
      thinking: startThinking(NOW),
      answer: {
        question: "上个月GMV是多少？",
        summary: "上个月 GMV 为 907.3 万元。",
        mode: "query",
        status: "ok",
        sql: "SELECT SUM(x) FROM safe.orders",
        results: [],
        time_range: { label: "上个月", start: "2026-09-01", end_exclusive: "2026-10-01" },
      } as never,
    };
    expect(buildHistory([user("上个月GMV是多少？"), answered])[1]).toEqual({
      role: "assistant",
      content: "上个月 GMV 为 907.3 万元。",
      mode: "query",
      status: "ok",
      queries: [{ purpose: "上个月GMV是多少？", sql: "SELECT SUM(x) FROM safe.orders" }],
      time_range: { label: "上个月", start: "2026-09-01", end_exclusive: "2026-10-01" },
    });
  });

  it("leaves out a reply that is still streaming", () => {
    const streaming: ChatMessage = {
      id: "w", role: "assistant", state: "streaming", thinking: startThinking(NOW),
    };
    expect(buildHistory([user("甲"), streaming])).toEqual([{ role: "user", content: "甲" }]);
  });

  it("cuts a reply to the length the backend accepts", () => {
    const long: ChatMessage = { ...failed, error: "错".repeat(5000) };
    expect(buildHistory([user("甲"), long])[1].content).toHaveLength(4000);
  });

  it("treats answers saved before modes existed as query answers", () => {
    expect(buildHistory([user("甲"), done("结论")])[1]).toEqual(
      { role: "assistant", content: "结论", mode: "query" });
  });
});

describe("answerQueries", () => {
  const base = { question: "为什么下降了？", sql: "", results: [] };

  it("lists the queries an analysis showed, with their purposes", () => {
    const answer = { ...base, status: "partial", mode: "analysis",
                     results: [{ purpose: "整体对比", sql: "SELECT 1" },
                               { purpose: "按渠道拆", sql: "SELECT 2" }] };
    expect(answerQueries(answer as never)).toEqual([
      { purpose: "整体对比", sql: "SELECT 1" },
      { purpose: "按渠道拆", sql: "SELECT 2" },
    ]);
  });

  it("leaves out SQL that did not run and answers without SQL", () => {
    expect(answerQueries({ ...base, status: "error", sql: "SELECT amount" } as never)).toEqual([]);
    expect(answerQueries({ ...base, status: "text" } as never)).toEqual([]);
    expect(answerQueries({ ...base, status: "refused" } as never)).toEqual([]);
  });

  it("keeps what the backend accepts", () => {
    const results = Array.from({ length: 12 }, (_, index) => (
      { purpose: "用途".repeat(150), sql: `SELECT ${index}` }));
    const queries = answerQueries({ ...base, status: "ok", results } as never);
    expect(queries).toHaveLength(10);
    expect(queries[0].purpose).toHaveLength(200);
  });
});
