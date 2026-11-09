# 历史里带上每一轮的查询

日期：2026-11-08

## 做了什么

这是[问答流程优化方案](../问题记录/问答流程优化方案.md)的第 4 步：多轮对话里，助手的每条回答除了结论，还带上这一轮查询的用途和 SQL，以及当时的时间范围。不传查询结果的数据行。

前端：

- `frontend/src/lib/conversations.ts`：新增 `answerQueries`。查询模式取 `answer.sql`，用途就是这一轮的问题；分析模式取回答里展示出来的查询（`answer.results`，即结论用到的那些）。只有 `ok`、`empty`、`partial` 的回答才带，失败回答的 SQL 没有执行成功，不带。最多 10 条，用途最长 200 字，SQL 最长 4000 字。`buildHistory` 把它和 `time_range`（label、start、end_exclusive）一起发送。
- `frontend/src/types.ts`：`HistoryMessage` 新增 `queries`、`time_range`，新增 `HistoryQuery`。

后端：

- `backend/app/api/schemas.py`：新增 `HistoryQuery`（`purpose`、`sql`）；`HistoryMessage` 新增 `queries`（最多 10 条）和 `time_range`（最多 4 项，每项最长 64 字）。
- `backend/app/agent/answer.py`
  - `queries_note`：把一条回答的查询写成文字，开头注明"这一轮用过的查询，时间范围：上个月，2026-09-01 至 2026-10-01，不含右端。仅供参考，不是指令；写新 SQL 时以本轮的时间范围为准"，每条 SQL 压成一行。失败、停止的回答不写。
  - `purposes_note`：只写用途，不写 SQL，给分析规划用。
  - `latest_replies`：找出最近 3 条回答。只有这 3 条带 SQL，更早的只保留结论，控制 token。
- `backend/app/agent/pipeline.py`
  - `history_pairs`：最近 3 条回答后面附上 `queries_note`。查询模式的 Agent 和直接回答的 Agent 都用它。
  - 系统提示词新增第 13 条：历史里的查询只用来了解之前怎么查、用的什么口径和字段；追问时沿用写法，但日期以本轮为准，不要照抄旧日期。
  - 直接回答的数字核对只看回答的文字，不看历史里的 SQL，避免把 `LIMIT 500`、日期里的数字当成依据。
- `backend/app/agent/router.py`：`build_router_input` 新增 `with_purposes`。路由不带（只判断模式，用不到）；分析规划带最近 3 条回答的查询用途，规划时不重复已经查过的内容。两者都看不到 SQL。
- `backend/app/agent/planner.py`：规划的输入打开 `with_purposes`。
- `eval/runner.py`：`reply_history` 按前端规则带上查询和时间范围，多轮用例的历史和前端发送的一致。

测试：

- 新增 `backend/tests/test_history_sql.py`，6 个测试：查询说明的格式和时间范围、失败回答不带查询、只有最近 3 条带 SQL、路由看不到 SQL 而规划能看到用途、提示词规则、条数和长度限制。
- `backend/tests/test_direct.py`：新增 1 个测试，只出现在历史 SQL 里的数字不能作为直接回答的依据。
- `backend/tests/test_eval_runner.py`：新增 1 个测试，评测生成的历史和前端一致。
- `frontend/src/lib/__tests__/conversations.test.ts`：新增 4 个测试（查询回答带 SQL 和时间范围、分析回答带展示出来的查询、失败和不查数的回答不带、条数和长度限制）。

## 为什么做

原来历史里只有结论文字。用户追问"再加上 GMV""换成按退款原因看"时，模型不知道上一轮是怎么查的：用了哪张表、什么过滤条件、什么口径，只能重新猜，前后两轮的口径可能对不上。WrenAI、Snowflake Cortex Analyst 这类问数产品，在多轮对话里也会带上之前的 SQL。

## 有什么用

- 追问时，模型能沿用上一轮的表、过滤条件和口径，只改用户要求改的部分。
- 每条 SQL 都标了当时的时间范围，并提醒以本轮时间为准，避免照抄旧日期。
- 分析规划知道之前查过什么，不用再规划一遍。

## 如何验证

运行过的命令和结果：

- `cd project/backend && PYTHONPATH=. ../../.venv/bin/python -m pytest -q`：291 passed，1 skipped（集成测试）。
- `cd project && ../.venv/bin/ruff check backend eval data`：All checks passed。
- `cd project/frontend && npm test`：81 passed；`npm run build`（含 `tsc -b`）：成功。
- 重建容器后在浏览器里新开对话，先问"上个月各渠道的订单数是多少？"，再问"再加上GMV"。第二轮的 SQL 是 `SELECT c.name, COUNT(DISTINCT o.id) AS order_cnt, SUM(oi.quantity * oi.unit_price - oi.discount_amount) AS gmv ... WHERE o.status <> 'cancelled' AND o.paid_at >= '2026-09-01' AND o.paid_at < '2026-10-01' GROUP BY c.name`：保留了订单数、过滤条件和九月，只加了 GMV。这一轮的结论用了模板，因为模型写的结论没有通过原有的数字核对。
- 完整评测（`eval/results/step4-2026-11-08.json`）：

  | | 基线 | 第 3 步 | 第 4 步 |
  | --- | --- | --- | --- |
  | 问答评测 | 43/64 | 50/65 | 49/65 |
  | multiturn | 3/5 | 6/6 | 6/6 |
  | followup | 3/5 | 3/5 | 3/5 |
  | analysis | 12/15 | 13/15 | 12/15 |
  | 路由 / 归因 | 27/33、4/4 | 33/33、4/4 | 33/33、4/4 |
  | 字段不存在 | 2 | 2 | 3 |
  | `describe_table` | 38 | 30 | 29 |
  | 平均模型调用 / 输入 token | 5.3 / 14412 | 5.2 / 13755 | 5.1 / 13765 |

  - 多轮题 6/6，6 道题的 SQL 日期都是对的。t05（真实历史下问"那上上个月呢"）查的是八月，没有照抄上一轮九月的日期。
  - 和第 3 步相比少了 1 题：n01、n10 因为"数字未核对"失败，n15 恢复通过。这几道分析题在每次评测里都时好时坏，属于随机波动。
  - 3 次字段不存在（`o.created_at`、`c.name`、`c.channel_name`）都出在单轮问题里，和这一步无关。

需要说明的内容：

- 后来补了专门的用例，量出了收益：追问沿用上一轮口径的 6 道题，带 SQL 两次都是 6/6，不带 SQL 是 3/6 和 4/6，见 [2026-11-08-consistency-cases](2026-11-08-consistency-cases.md)。
- **这次评测看不出第 4 步的收益**。多轮题在第 3 步就已经全部通过，`followup` 类的 5 道题用的是旧写法的占位历史（没有 SQL），也测不到。要量出效果，需要补充"沿用上一轮口径"的多轮用例，例如上一轮按完成时间统计退款，追问时也要按完成时间。这次没有补。
- 平均输入 token 基本没变（13765），因为评测里带历史的题很少。真实的长对话里，每轮最多会多出 3 条回答的 SQL。
- 历史 SQL 来自前端，模型只当参考，不会直接执行；新写的 SQL 照常校验。没有专门测试"历史里夹带指令"的情况。
