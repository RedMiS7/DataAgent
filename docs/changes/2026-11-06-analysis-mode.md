# 新增手动选择的分析模式

日期：2026-11-06

## 做了什么

输入框增加“查询 / 分析”切换，默认查询，选择保存在浏览器里。查询模式的提示词和流程不变，新逻辑只在分析模式生效。

后端：

- `backend/app/api/schemas.py`：`AskRequest` 新增 `mode`；`AnswerPayload` 新增 `mode` 和 `results`，保留 `table`、`chart`、`sql`。
- `backend/app/api/routes.py`：把 `mode` 传给问答流程。
- `backend/app/agent/pipeline.py`：分析模式使用单独的回答规则，给出上一周期的日期，步数上限 12，结论只展示被引用的查询；分析记录不写入 `query_logs.sql_text`，所以不进案例库。
- `backend/app/agent/tools.py`：`run_sql` 增加可选的 `purpose`；分析模式给查询编号，最多 10 条，每条显示为一个思考步骤。
- `backend/app/agent/answer.py`：新增 `normalize_citations`、`strip_citations`、`unverified_numbers`。

前端：

- `src/components/chat/Composer.tsx`、`src/pages/ChatPage.tsx`、`src/lib/modes.ts`：模式切换和保存。
- `src/components/chat/AnswerCard.tsx`、`src/lib/summary.ts`：判断加要点的结论、可点击的来源、查询切换，导出当前查询的 CSV。查询模式下问题像开放问题时，显示“用分析模式重新回答”。
- `src/components/chat/ReasoningPanel.tsx`：显示查询条数。
- `src/hooks/useChat.ts`、`src/api.ts`、`src/lib/conversations.ts`、`src/types.ts`、`src/styles.css`：配套改动。重新生成和编辑重发沿用这条问题当时的模式。

测试和评测：

- `backend/tests/test_pipeline.py`、`frontend/src/lib/__tests__/modes.test.ts`、`summary.test.ts`：补充测试。
- `eval/questions.json` 新增 5 个开放问题（`n01` 到 `n05`），`eval/runner.py` 支持 `mode` 和 `analysis` 类别。以后跑评测，问答总数会增加 5 条，和之前的结果比较时要注意。

## 为什么做

开放问题原来走查数流程：只答一句、只展示一条结果、校验失败时换成模板，所以页面上出现 `qty=42162、orders=16075` 这样的回答。

选择手动切换而不是自动判断，是因为简单问题不会被误判成分析，也不会变慢，只有用户选分析时才多花时间和 token。

## 有什么用

分析模式会先比较上一周期，再按维度拆开，给出判断、要点和建议。每条要点能点开看到对应的表、图和 SQL。模型结论不再被模板替换，核对不上的数字会明确标出来。

## 如何验证

- 后端测试：`PYTHONPATH=. ../../.venv/bin/python -m pytest -q`，116 个通过，1 个跳过。
- 后端 lint：`ruff check backend eval data` 通过。
- 前端：`npm test` 45 个通过，`npm run build` 通过。

真实模型重放（DeepSeek，不写库，不读案例库）：

| 模式 | 问题 | 结果 |
| --- | --- | --- |
| 分析 | 之前的 5 个开放问题 | 5 个都由模型写结论，没有退回模板；耗时 10.5 到 17.4 秒，执行 4 到 8 条查询 |
| 查询 | 2 个追问 | 都正常回答，耗时 3 到 4 秒 |
| 查询 | 3 个简单问题 | 都查出结果，其中 2 个退回模板，显示 `order_cnt为 16075。` 这样的英文列名 |
| 查询、分析 | 注入和改数据请求 | 3 次都拒绝 |

页面（http://localhost:8000）实测通过：模式切换、查询模式显示重答按钮、重答后切到分析、思考过程逐条显示查询、点击来源切换到对应查询、375px 宽度无横向溢出。分析记录的 `sql_text` 长度为 0，查询记录为 304。

发现的问题：

- 模型仍会自己算比例。5 个问题里有 3 个出现“未能核对”，例如 `6.3%`、`11.9%`。这些数字照常显示，只有提示。
- 回答经常超过 300 字或 4 条要点，提示词的限制没有强制执行。
- 查询模式的模板回答是已有问题，这次没有改。
- 重放时 stderr 有 `Event loop is closed`，来自 HTTP 客户端在事件循环关闭后释放，没有影响回答。没有处理。

没有验证的内容：

- 没有跑完整评测。
- 没有逐条用独立 SQL 核对分析结论里的数字是否正确，也没有评估判断和建议是否合理。
- 没有测试 10 条查询上限在真实模型下的表现，只有单元测试。
- 页面验证在问答日志里留下了 2 条测试记录（id 59、60）。
