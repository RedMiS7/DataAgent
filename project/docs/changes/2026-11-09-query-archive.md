# 回答过程存档

日期：2026-11-09

对应优化计划 1.1（`docs/优化方案/1-评测基础.md`）。

## 做了什么

每次问答除了原来的日志和调用记录，还存下用户看到的结论，以及每一条试过的 SQL。

1. `query_logs` 新增 4 个字段：
   - `summary`：结论原文；
   - `summary_source`：model 或 template，没有结论时为空；
   - `warnings`：回答里的警告，JSON 数组；
   - `source`：web 或 eval。
2. 新表 `query_executions`，每条试过的 SQL 存一行：
   - 字段：`seq`、`query_no`、`purpose`、`sql_text`、`status`、`row_count`、`error`、`duration_ms`、`rows_preview`；
   - `status` 分三种：`ok` 是执行成功；`blocked` 是被安全校验拦下，或用了没检索过的表；`failed` 是数据库报错；
   - `query_no` 是结论里 `[查询n]` 的编号，只有成功的查询才有；
   - `rows_preview` 存列名和最多 20 行结果。
3. `run_sql` 每试一次 SQL 都记一条，记在 `AgentRunContext.query_records` 里。`_finalize` 写日志时一并入库。查询、分析、降级、拒答、直接回答几种结束方式都会写入。
4. 已有的数据库在启动时自动补上 `query_executions` 表和 `query_logs` 的新字段：`session.ensure_log_tables` 取代原来的 `ensure_trace_table`。
5. `AskPipeline` 新增参数 `source`，默认 `web`。评测入口 `eval/run_eval.py` 传 `eval`。
6. `show_trace` 先打印问题、状态、来源、结论原文和警告，再逐条打印查询：编号、状态、耗时、行数、目的、SQL、错误和前 5 行。最后照旧打印模型和工具调用。

涉及文件：

- `data/initdb/02_app_schema.sql`
- `backend/app/agent/session.py`
- `backend/app/agent/tools.py`
- `backend/app/agent/pipeline.py`
- `backend/app/bootstrap.py`
- `backend/app/semantic/cli.py`
- `eval/run_eval.py`
- `CLAUDE.md`：`show_trace` 命令的说明
- 测试：
  - 新增 `backend/tests/test_session.py`；
  - 修改 `test_pipeline.py`、`test_failure.py`；
  - 调整夹具：`test_agentic.py`、`test_direct.py`、`test_planner.py`、`test_bootstrap.py`。

## 为什么做

以前复查一道答错的题，只能看到 SQL 和调用顺序，看不到用户实际看到的结论，也看不到中间查过什么。分析模式的日志里连 SQL 都没有，被拦下和执行失败的 SQL 更是只留在服务日志里。后面要做的评测入库（1.3）、判分修正（1.4）和后端指标（1.6），都要靠这份记录。

失败的 SQL 也存下来，因为复查时最常要回答的问题是模型在哪一步走错了。`query_no` 用来把结论里的 `[查询n]` 对上具体哪一行记录。

## 有什么用

- 用一个 `log_id` 就能看到这次问答的全过程：结论、每条 SQL 的结果或失败原因、模型和工具调用。
- 用 `source` 可以把评测产生的问答和真实用户的问答分开。

## 保留期限

`rows_preview` 存的是 `safe` 视图里的业务数据，隐藏列不在视图里，所以不会存进来。它和对应的 `query_logs` 记录保留一样久，目前不自动清理。需要清理时，用管理账号执行：

```sql
DELETE FROM query_executions WHERE created_at < NOW() - INTERVAL 90 DAY;
```

## 如何验证

- 后端测试：`cd project/backend && PYTHONPATH=. ../../.venv/bin/python -m pytest -q`，结果 482 passed，1 skipped。新增的用例：
  - 查询模式：结论、来源、警告入库；被拦下的 SQL 记为 blocked，成功的记为 ok，编号为 1，带预览行。
  - 分析模式：两条查询按编号入库；结论里没有引用的查询也照样入库；未核对数字的警告入库。
  - 失败降级：执行失败的 SQL 记为 failed，并带数据库的报错；没有结论时 `summary_source` 为空。
  - 评测入口：`source` 记为 eval。
  - 补表：旧库按顺序补 4 个字段；字段齐全的库不执行 ALTER；代码里的建表语句和 `02_app_schema.sql` 一致。
  - 存储：预览最多 20 行；没有列的记录不存预览；`show_trace` 的输出格式。
- lint：`cd project && ../.venv/bin/ruff check backend eval data`，结果 All checks passed。
- 真实 MySQL：
  - 改动前，`dataagent.query_logs` 没有新字段，也没有 `query_executions` 表。
  - `docker compose up -d --build app` 启动成功，新字段和新表都已补上。
  - 查询模式问"上个月各渠道的订单数"，log_id=1274。`show_trace` 显示：结论（model）、来源 web、1 条查询（[查询1]，8 行，存档里有前 5 行和"另有 3 行"）。
  - 分析模式问"上个月GMV和前一个月比怎么样，主要变化在哪些类目"，log_id=1275。`show_trace` 显示：结论原文、1 条警告（从 JSON 字段正确读回）、6 条查询，编号 1～6。

没有验证的：

- 真实环境下没有触发 blocked 和 failed，这两种只在单元测试里验证过。
- 没有在真实环境跑评测，`source=eval` 只在单元测试里验证过。
- DABench 实例（`app-dabench`）没有重建。它的应用库 `dataagent_dabench` 下次重建启动时才会补表。
