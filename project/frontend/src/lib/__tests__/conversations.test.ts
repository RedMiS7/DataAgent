import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  createConversation,
  deriveTitle,
  groupConversations,
  lastMessageId,
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

describe("lastMessageId", () => {
  const user: ChatMessage = { id: "u", role: "user", text: "问题" };
  const assistant = (id: string, messageId?: number | null): ChatMessage => ({
    id,
    role: "assistant",
    state: "done",
    thinking: startThinking(NOW),
    answer: { message_id: messageId } as never,
  });
  const stopped: ChatMessage = {
    id: "s",
    role: "assistant",
    state: "stopped",
    thinking: startThinking(NOW),
  };

  it("is 0 when nothing is kept", () => {
    expect(lastMessageId([])).toBe(0);
    expect(lastMessageId([user, stopped])).toBe(0);
  });

  it("uses the last answer, skipping turns without one", () => {
    expect(lastMessageId([user, assistant("a", 7), user, assistant("b", 9)])).toBe(9);
    expect(lastMessageId([user, assistant("a", 7), user, stopped])).toBe(7);
  });

  it("is unknown for answers saved before message ids existed", () => {
    expect(lastMessageId([user, assistant("a")])).toBeUndefined();
    expect(lastMessageId([user, assistant("a", null)])).toBeUndefined();
  });
});
