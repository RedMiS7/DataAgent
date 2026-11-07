# 记录每次问答的模型和工具调用

日期：2026-11-08

## 做了什么

每次问答都记录调用了几次模型和工具，每次调用由哪个 Agent 发起、从什么时候开始、用了多久、多少 token、是否出错。

### 记录方式

- 新增 `backend/app/agent/trace.py`：
  - `CallTrace` 保存一次问答的全部调用记录。
  - `TraceMiddleware` 实现 AgentScope 中间件的 `on_model_call` 和 `on_acting` 两个钩子。
- 挂上中间件的 Agent：主 Agent（`dataagent`）、路由（`router`）、规划（`planner`）、各步骤（`step_N`）、补查（`supplement`）、汇总（`summary`）。
- 每条记录包含：Agent 名、类型（model 或 tool）、模型名或工具名、相对问答开始的时间、耗时、输入和输出 token、是否成功、说明。
  - 模型调用的说明是它要求调用的工具，例如 `run_sql`。
  - 工具调用的说明是失败时的状态。
- 失败的模型调用也会记录。超时有两种表现，都记为失败：
  - 抛出异常的调用，说明是异常类型和信息。
  - 被取消的调用，AgentScope 的模型封装会把它变成一个 `finished_reason` 为 `interrupted` 的空回复，说明记为 `interrupted`。
- 分析步骤同时执行，记录按开始时间排序后保存。
- `query_logs.started` 这个开始时间改由 `CallTrace` 记录，`_finalize` 改为接收 trace，用它计算总耗时。

### 保存和查看

- `query_logs` 的 `prompt_tokens`、`completion_tokens` 原来一直写 0，现在写入本次问答所有模型调用的 token 合计。
- 新表 `call_traces`，每次调用一行，用 `log_id` 关联 `query_logs`。
  - 建表语句写在 `data/initdb/02_app_schema.sql`，新建数据库时创建。
  - 启动时“连接数据库”一步会执行同一条 `CREATE TABLE IF NOT EXISTS`，已有的数据库也会补上这张表。
  - 测试会检查两处语句一致。
- 后端日志“问答结束”一行加上模型调用次数和 token：`问答结束 status=ok 25986ms 尝试 4 次 模型 14 次 tokens=35144/2761`。“问答失败”一行加上模型调用次数。
- 新命令 `python -m app.semantic.cli show_trace --log-id <id>`：按开始时间列出一次问答的全部调用。
- 返回结果新增 `usage`：模型调用次数、工具调用次数、输入和输出 token、模型累计耗时。各步骤同时执行，累计耗时会超过问答总耗时。
- 前端答案下方显示“模型 14 次 · 37,905 tokens”，鼠标悬停显示输入、输出 token、模型累计耗时和工具次数。旧答案没有 `usage`，不显示。

### 评测

- `CaseResult` 新增 `model_calls`、`input_tokens`、`output_tokens`。
- `--mode analysis` 额外输出平均模型调用次数和平均 token。

### 涉及文件

- 后端：`backend/app/agent/trace.py`（新增）、`backend/app/agent/pipeline.py`、`backend/app/agent/planner.py`、`backend/app/agent/router.py`、`backend/app/agent/tools.py`、`backend/app/agent/session.py`、`backend/app/api/schemas.py`、`backend/app/bootstrap.py`、`backend/app/semantic/cli.py`
- 数据库：`data/initdb/02_app_schema.sql`
- 后端测试：`backend/tests/test_trace.py`（新增）、`backend/tests/test_planner.py`、`backend/tests/test_pipeline.py`、`backend/tests/test_bootstrap.py`
- 前端：`src/types.ts`、`src/lib/usage.ts`（新增）、`src/components/chat/AnswerCard.tsx`
- 前端测试：`src/lib/__tests__/usage.test.ts`（新增）
- 评测：`eval/runner.py`、`eval/run_eval.py`
- 文档：根目录 `CLAUDE.md` 补充 `show_trace` 命令；`踩坑记录.md` 和 `docs/问题记录/AgentScope吞掉超时.md` 补充模型封装把取消变成空回复的情况

## 为什么做

排查“分析模式为什么慢”“数字从哪条查询来”时，只能临时写脚本包住模型统计调用次数和耗时。没有记录，就说不清慢在哪、贵在哪，也没法比较改动前后的成本。

用 AgentScope 的中间件实现，不用改各模型的私有方法，所有 Agent 用同一种方式记录。逐次明细单独建表，方便按 Agent 汇总；总 token 写进 `query_logs` 原有的两列，不用改表。

## 有什么用

- 每次问答都能看到调用明细。上线后的第一次实测（“最近30天广告投放效果怎么样？”）直接看出：
  - 规划是最慢的单次调用（8.3 秒）。
  - 4 个步骤里有 2 个先调了一轮 `describe_table` 查字段，各多花一次模型调用。
- 回答下方能看到这次用了多少次模型和多少 token，方便估算成本。
- 评测能比较不同改动的调用次数和 token。

## 如何验证

运行过的命令和结果：

- `cd project/backend && PYTHONPATH=. ../../.venv/bin/python -m pytest -q`：234 passed，1 skipped（需要 `RUN_INTEGRATION=1` 的集成测试，这次没有运行）。新增和修改的测试覆盖：
  - 模型和工具调用都有记录，token 来自模型返回的 `usage`。
  - 模型调用出错时记为失败并继续抛出。
  - 路由超时记为失败（`interrupted`）。
  - 路由调用有记录。
  - 计划流程中规划、各步骤、补查、汇总都有记录，且按开始时间排序。
  - `query_logs` 的 token 合计正确，返回结果里的 `usage` 正确。
  - 启动时会确保 `call_traces` 存在。
  - 启动时的建表语句和 `02_app_schema.sql` 一致。
  - `show_trace` 的输出格式。
- `cd project && ../.venv/bin/ruff check backend eval data`：All checks passed。
- `cd project/frontend && npm test`：66 passed；`npm run build`：构建成功，只有原本就存在的 chunk 体积提示。
- 重建 Docker（`cd project && docker compose up -d --build`）：
  - 启动后已有的数据库里多了 `call_traces` 表，各启动步骤都是 done。
  - 在浏览器里用自动模式问“最近30天广告投放效果怎么样？”，答案下方显示“模型 14 次 · 37,905 tokens”。
  - `query_logs` 里这次的 `prompt_tokens`、`completion_tokens` 是 35144 和 2761。
  - `show_trace --log-id 220` 列出 20 条调用：模型 14 次（路由、规划、4 个步骤、补查、汇总），工具 6 次。
  - 后端日志的“问答结束”一行带有调用次数和 token。
  - 说明 DeepSeek 接口会返回 `usage`。
- `eval/run_eval.py --mode analysis --limit 2`：输出平均模型调用 13.5 次，平均 token 39069/4538。

未验证的内容：

- 鼠标悬停时的提示框没有在截图里确认显示。
- 只用 DeepSeek 确认过 `usage`，其他模型如果不返回，token 记为 0。
- 没有重跑完整的 `--mode all` 评测。
