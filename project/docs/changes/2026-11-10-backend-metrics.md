# 后端指标：检索、embedding、重排和 SQL 的耗时与出错

日期：2026-11-10

对应优化计划 1 的第 4 步：1.6 后端指标。用这些指标判断要不要接入 Redis、Kafka。

## 做了什么

**记录后端调用**
- `backend/app/agent/trace.py`：
  - 调用记录增加 `error_type`（`timeout`、`429`、`5xx`、`other`）和 `input_hash`（输入规整空白后算 sha256，取前 16 位）。
  - 调用类型在 `model`、`tool` 之外，增加 `retrieval`、`embedding`、`rerank`、`sql`。
  - 新增 `timed()`：记录一段代码里的调用，失败时也记一条，再把异常抛出去；不传 trace 时什么都不记。
  - 新增 `error_type()`：顺着异常链判断错误类型。依据有三种：`TimeoutError` 或类名里带 Timeout；`status_code` 属性；报错文字里的 `HTTP 429`、`Error code: 503` 之类。
  - 模型调用失败时也记错误类型；模型返回 interrupted（超时被取消）时记为 `timeout`。
  - `usage.tool_calls` 改为只数工具调用。
- 接入的位置：
  - `backend/app/semantic/retriever.py`：回答前的语义检索记一条 `retrieval`，里面的 embedding 记一条 `embedding`。`retrieve` 和 `retrieve_with_connection` 都多了一个可选参数 `trace`。
  - `backend/app/semantic/table_search.py`：`TableSearcher` 多了一个可选参数 `trace`，记录 `search_semantic` 里的 embedding 和重排。重排失败、退回检索排序时，记一条失败的 `rerank`。
  - `backend/app/agent/tools.py`：`run_sql` 执行 SQL 时记一条 `sql`，`input_hash` 是 SQL 的哈希。旧的 `search_semantic` 工具检索时也把 trace 传进去。
  - `backend/app/agent/pipeline.py`：把每个问题的 trace 传给检索和表检索；统计 `_finalize` 里写 `query_logs`、`call_traces`、`query_executions` 的耗时，放在 `usage.log_write_ms` 里返回。
  - `backend/app/api/schemas.py`：`CallUsage` 增加 `log_write_ms`，默认 0。
- 建索引、检索召回率评测不传 trace，所以不记录。

**存档表**
- `call_traces` 增加 `error_type`、`input_hash` 两列。
- `query_executions` 增加 `sql_hash` 一列。
- 改动的文件：`data/initdb/02_app_schema.sql` 和 `backend/app/agent/session.py`。旧数据库启动时由 `ensure_log_tables` 自动补列（`LATER_COLUMNS`）。
- `show_trace` 会显示新的调用类型和错误类型，例如 `失败(429)`，最后再汇总一行后端调用。

**评测指标**（`backend/app/evaluation/runner.py`、`report.py`）
- 每道题按 `log_id` 从存档里读出：
  - 各类调用的耗时；
  - 失败次数，按"服务:类型"分；
  - embedding 或重排失败的次数，即降级次数；
  - 执行过的 SQL、embedding 输入、重排输入的哈希；
  - 写日志耗时。
- 每组的 `metrics.backend` 里有下面 6 项，`run` 和 `show` 都会打印，导出的 JSON 里也有：

  | 指标 | 算法 |
  | --- | --- |
  | 每题耗时拆分 | 模型、SQL、embedding、重排各自的平均耗时；其他 = 总耗时 − 这四项，最小记 0。各项相加超过总耗时的题数单独列出：分析模式里几个步骤同时跑，就会出现这种情况 |
  | 重复请求 | 同一组题里，相同的 SQL 哈希、embedding 输入、重排输入多出现的次数和总调用次数。被拦下、没有执行的 SQL 不算 |
  | 调用失败 | 按服务（模型、SQL、embedding、重排、检索）和类型统计 |
  | 降级 | embedding 失败后退回关键词检索的次数，加上重排失败后退回检索排序的次数 |
  | 长耗时 | 超过 60 秒的题数，以及分析题里最长的一题 |
  | 写日志耗时 | 平均值和最大值 |

**测试**
- 新增 `backend/tests/test_evaluation_backend.py`。
- 修改的测试文件：
  - `test_trace.py`、`test_table_search.py`、`test_agentic.py`、`test_session.py`、`test_evaluation_runner.py`；
  - 5 个测试文件里的假检索器补上 `trace` 参数。

## 为什么做

要不要接 Redis（缓存）或 Kafka（异步、削峰），要看数据：
- 时间花在哪一段；
- 相同的请求有多少；
- 并发时会不会被限流；
- 长任务有多少；
- 写日志会不会拖慢回答。

以前只记了模型和工具调用，检索、embedding、重排和 SQL 执行都是黑盒，失败也分不出是超时还是限流。

trace 作为参数传进去，没有挂在共用的检索器上，也没有用 ContextVar。这样每个问题只记自己的调用，没有 trace 的地方什么都不记。写日志的耗时没有写回数据库，因为要在写完之后再多做一次 UPDATE，所以放在回答的 `usage` 里，由评测读取。

## 有什么用

- 一次评测跑完，就能看到每组题的 6 项后端指标，直接回答这些问题：
  - 值不值得给 SQL、embedding、重排加缓存（看重复比例）；
  - 要不要给模型做全局限流（看 429 次数）；
  - 要不要做熔断（看降级次数）；
  - 长任务要不要改成异步（看长耗时题数）；
  - 日志要不要移出请求路径（看写日志耗时）。
- 排查单个问题时，`show_trace` 能看到每次检索、重排和 SQL 的耗时，以及失败的类型。

## 如何验证

- 后端测试：`cd project/backend && PYTHONPATH=. ../../.venv/bin/python -m pytest -q`，557 个通过，1 个跳过。新增的测试覆盖：
  - 8 种异常的分类，包括包装后的异常按原因判断；
  - `timed` 能记录成功和失败的调用，不传 trace 时什么都不记；
  - 表检索在有 trace 时记录 embedding 和重排，重排失败时记为 429；同一输入的哈希相同；
  - 用假模型跑一个问题：`run_sql` 执行的 SQL 记成一条 `sql`，哈希和执行的 SQL 一致，`usage.log_write_ms` 有值；
  - 新字段写进 `call_traces` 和 `query_executions`；旧表会按顺序补列，已经是新表的不动；
  - 按顺序执行的调用，耗时拆分加起来等于总耗时；同时执行的调用不会让"其他"变成负数；
  - 同一条 SQL 执行两次记 1 次重复，被拦下的不算；
  - 失败按服务和类型统计，降级次数和长耗时题数正确；
  - 每组的 `metrics.backend` 里有这 6 项指标。
- lint：`cd project && ../.venv/bin/ruff check backend eval data`，全部通过。
- 在电商实例（8000 端口）上实际运行，只跑了小样本，共 9 道题：
  - 重建：`docker compose up -d --build app`。服务启动后，旧库的 `call_traces` 补上了 `error_type`、`input_hash`，`query_executions` 补上了 `sql_hash`。
  - 评测 5：`run --mode questions --category metric --limit 4 --concurrency 1`，4 道题，3/4 通过（i02 数值不同，和之前一样）。
    - 后端指标：模型 4.4 秒，SQL 0.1，embedding 0.8，重排 1.0，其他 0.2，合计 6.5 秒；
    - 重复请求 0，调用失败无，降级 0；
    - 写日志平均 5 ms。
  - 评测 6：同样 4 道题，`--concurrency 4`。
    - 合计 7.2 秒，其中 embedding 1.4 秒，比并发 1 时多；
    - 调用失败无，没有出现 429；
    - 写日志平均 3 ms。
  - 评测 7：`run --mode analysis --limit 1 --concurrency 1`，1 道分析题，用时 28.8 秒。模型调用合计 43.6 秒，比总耗时长，因为几个步骤同时跑。"其他"记 0，输出里注明了"1 题的调用同时进行"。
  - 记录：`show_trace --log-id 1565` 能看到一次问答的检索、2 次 embedding、重排和 SQL 执行，每条都有耗时。库里 embedding、rerank、sql 的记录都有 `input_hash`，`query_executions.sql_hash` 是 16 位。
- **没有验证的**：
  - 真实的超时、429、5xx：这次没有遇到，只有单元测试；
  - 降级记录：这次没有发生；
  - 重复请求大于 0 的情况：小样本里没有重复的 SQL；
  - DABench 实例：按规则没有在它上面跑；
  - 全量评测以及并发 1 和并发 4 的全量对比：由用户来跑。小样本只能说明指标能正常输出，不能说明并发时会不会被限流。
