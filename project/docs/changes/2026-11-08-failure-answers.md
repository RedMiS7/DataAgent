# 出错时也给用户有用的信息

日期：2026-11-08

## 做了什么

这是[问答流程优化方案](../问题记录/问答流程优化方案.md)的第 1 步。

后端：

- `backend/app/api/schemas.py`：回答状态新增 `partial`（查到了一部分，但没能完整回答）；新增 `Failure`（`kind`、`message`、`suggestion`），放在 `AnswerPayload.failure`。`kind` 有 `timeout`、`sql_failed`、`model_error`、`no_answer`、`step_failed`、`unexpected`。
- `backend/app/agent/failure.py`（新增）：生成给用户看的失败说明和建议；把数据库报错翻译成大白话，例如 `Unknown column 'amount'` 写成"数据库里没有“amount”这个字段"；判断异常是不是来自模型服务（`openai`、`httpx` 等），不把代码异常说成模型出错。
- `backend/app/agent/pipeline.py`
  - 整体时限：查询模式 `ask_timeout_seconds`，分析模式 `analysis_timeout_seconds`，用 `asyncio.wait_for` 包住整个问答。
  - 新增 `_degrade`：出错时，有成功的查询就返回 `partial`，展示已有的查询结果，结论用模板生成；一条都没有就返回 `error`，带上时间范围、最后一次失败的 SQL、相关口径和失败说明。原来的"Agent 执行失败"分支会丢掉已经成功的查询，现在保留。
  - 模型没有调用 `run_sql`：SQL 都失败时说明试了几次、最后一次为什么失败（`sql_failed`）；一条 SQL 都没写时为 `no_answer`。
  - 分析有步骤没查到数据时，状态为 `partial`，说明缺了哪一步（`step_failed`）；查询次数用完时加一条警告。
  - `error` 状态没有 SQL 可以确定用到的表时，展示检索到的前 2 个指标口径。
  - 失败也通过 `answer` 事件发给前端，不再只发一行 `error` 事件；`error` 事件只剩接口层的异常。
  - 主 Agent 设置 `interruption_raise_cancelled_error=True`，否则 AgentScope 会吞掉取消，时限不会生效（见[AgentScope吞掉超时](../问题记录/AgentScope吞掉超时.md)）。
- `backend/app/agent/planner.py`：分析步骤的 Agent 同样设置 `interruption_raise_cancelled_error=True`。
- `backend/app/agent/tools.py`：`AgentRunContext` 新增 `failed_sql`，记录最后一条失败的 SQL。
- `backend/app/agent/scope_model.py`：把 `llm_timeout_seconds` 传给模型客户端（原来配置了但没有用上）。
- `backend/app/config.py`、`project/.env.example`：新增 `ASK_TIMEOUT_SECONDS=120`、`ANALYSIS_TIMEOUT_SECONDS=180`。分析时限参考了 2026-11-07 评测里最长的 81 秒。
- 模型单次输出上限从写死的 1024 改为配置项 `LLM_MAX_TOKENS`，默认 8192（deepseek-chat 的最大值），`scope_model.py` 的三种模型客户端都用它。原因见下文"如何验证"里 n11 的长 SQL 被截断。

前端：

- `frontend/src/types.ts`：新增 `partial` 状态和 `Failure`。
- `frontend/src/lib/failure.ts`（新增）：决定失败提示的样式和文字、什么时候显示"回答正确/错误"按钮、失败答案的 SQL 标签叫"失败的 SQL"。
- `frontend/src/components/chat/AnswerCard.tsx`、`src/styles.css`：`partial` 显示黄色提示条加已有结果，`error` 显示红色提示条、原因和建议，下面照常显示时间范围、口径、检索和失败的 SQL；失败的答案不显示反馈按钮；没有结论时不显示"模板结论"标签。

测试：

- 新增 `backend/tests/test_failure.py`，14 个测试。其中用一个会卡住的假模型，确认时限真的能打断 AgentScope；还覆盖了超时有结果和没结果、SQL 一直失败、模型服务报错、没配置模型、分析超时、报错翻译和异常分类。
- `backend/tests/test_planner.py`：有步骤失败的分析，期望状态从 `ok` 改为 `partial`。
- `backend/tests/test_pipeline.py`：查询次数用完时检查警告。
- 新增 `frontend/src/lib/__tests__/failure.test.ts`，6 个测试。

## 为什么做

原来出错时，前端只显示一行红字，例如"模型没有调用 run_sql 工具"。用户不知道系统怎么理解的问题、试过什么、下一步该怎么做。查了两条数据后模型超时，已经查到的数据也会被丢掉。查询模式没有整体时限，模型服务很慢时只能干等。

## 有什么用

- 超时、模型出错时，已经查到的数据照样展示，并说明缺了什么。
- 什么都没查到时，用户也能看到系统的理解（时间范围、口径）、失败的 SQL、用大白话说的原因和建议。比如问"GMV包不包括运费？"，现在会得到一张失败卡片，但口径页里就能看到"GMV 不含运费"。
- 一个问题最多等 2 分钟（分析 3 分钟），超过就给出已有的结果。

## 如何验证

运行过的命令和结果：

- `cd project/backend && PYTHONPATH=. ../../.venv/bin/python -m pytest -q`：261 passed，1 skipped（集成测试）；调大输出上限后 263 passed。
- `cd project && ../.venv/bin/ruff check backend eval data`：All checks passed。
- `cd project/frontend && npm test`：72 passed。
- `cd project/frontend && npm run build`（含 `tsc -b`）：构建成功，只有原本就有的 chunk 体积提示。
- 重建容器后在浏览器里问"GMV包不包括运费？"：显示红色提示条"模型没有查询数据，也没有给出可用的回答"和建议，下面有时间范围、"口径 2"和"检索"标签，口径里能看到 GMV 不含运费。当时发现"模板结论"标签在没有结论时也会显示，已修复，修复后跑过前端测试和构建，但没有再重建容器在浏览器里看。
- `partial` 卡片没有在真实页面里触发过，只有单元测试。
- 完整评测，和第 0 步的基线对比（结果在 `eval/results/step1-2026-11-08.json`）：

  | | 基线 | 第 1 步 |
  | --- | --- | --- |
  | 问答评测 | 43/64 | 44/64 |
  | 路由评测 | 27/33 | 27/33 |
  | 归因、安全 | 4/4、21/21 | 4/4、21/21 |
  | SQL 无报错的题 | 96.5% | 93.1% |
  | 字段不存在 | 2 次 | 4 次 |
  | `describe_table` | 38 次 | 35 次 |
  | 平均模型调用 / 输入 token | 5.3 / 14412 | 5.3 / 14542 |

  这一步没有改模型看到的内容，准确率和字段报错的差异属于随机波动：7 题结果翻转，其中 n11 是因为新规则，其余是随机的。
  - n11（"各类目最近表现怎么样"）有一步没查到数据，按新规则变成 `partial`，分析题的判定要求 `ok`，所以算失败。这是预期的行为变化。
  - 新发现：n11 那一步失败，是因为模型写的 SQL 很长，结尾被截断成 `GROUP BY c`、`ORDER BY GM`，数据库报字段不存在。`scope_model.py` 给模型设置了 `max_tokens=1024`，很可能是输出被截断了。部分"字段不存在"其实是截断造成的，不是猜错字段。经确认，已把上限改为配置项 `LLM_MAX_TOKENS`，默认 8192。改完后用真实的 deepseek-chat 调用了一次，`max_tokens=8192` 能正常返回；新增 2 个测试，后端共 263 passed。**没有重跑评测**，n11 这类长 SQL 是否不再截断，要等下一次评测确认。
  - d01、d02 仍是 `error`，d03 仍会重新查数。这些属于第 3 步。

没有验证的内容：

- 没有在真实环境里触发过超时（只在单元测试里用假模型验证）。
- `llm_timeout_seconds` 传给了模型客户端，但没有实测单次调用超时的表现。
