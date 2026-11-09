# 按表把全部字段放进提示词

日期：2026-11-08

## 做了什么

这是[问答流程优化方案](../问题记录/问答流程优化方案.md)的第 6 步：提示词里不再只放检索命中的几个字段，而是按表放全部字段（字段名、类型、说明、同义词、关联关系）。所有表加起来不超过预算时放全部表，超过时只放这个问题需要的表。

- `backend/app/semantic/schema_context.py`（新增）
  - `build_schema_context`：所有表的估算 token 不超过 `schema_token_budget` 时放全部表；超过时按需要程度排列选中的表，沿关联关系补上中间表，能放几张放几张（至少放一张）。其余的表只列表名和摘要，注明"要用时先调用 describe_table"。表内不裁剪字段。
  - `join_graph`、`connect`：从字段说明里的"关联 safe.x.y"建出表之间的关联图，用最短路径补中间表。例如选中 orders 和 categories，会补上 products 和 order_items。
  - `estimate_tokens`：从宽估算，中文 1 字算 1 个 token，英文 3 个字符算 1 个 token。
  - `render_table`：表结构的写法，和 `describe_table` 的输出共用。
- `backend/app/agent/tools.py`
  - `table_columns`：从 `information_schema` 读真实字段和类型，按库缓存。`known_columns`（第 5 步的字段校验）改为从它取字段名，两处共用一份缓存。
  - `schema_context`：确定这个问题需要的表，顺序是检索命中的字段所在的表、命中指标的 `related_tables`、命中案例 SQL 用到的表、历史 SQL 用到的表。读不到真实字段时返回 `None`。
  - `describe_tables` 改用 `render_table`，输出不变。
- `backend/app/agent/pipeline.py`
  - 检索之后生成表结构（`_schema`）。历史表取最近 3 条回答的 SQL，最新的在前（`_history_tables`）。读不到时记日志，退回原来的做法，只放检索到的字段。
  - 系统提示词的顺序改为：工作规则、表结构、时间范围、检索到的口径和案例。不变的内容放在前面，方便命中模型的前缀缓存。有表结构时，检索结果里的【表摘要】和【字段】不再重复放。
  - 第 11 条规则改为："【表结构】里列出的表包含全部字段，没列出的字段就不存在，不用再调用 describe_table 查看。要用没有列出全部字段的表时，先调用 describe_table。"
  - 分析模式的每一步也用同样的表结构。
- `backend/app/semantic/models.py`：`to_prompt_context` 新增 `include_schema`；退回原来的做法时，【字段】标题注明"只列出一部分，没见过的字段先调用 describe_table"。
- `backend/app/config.py`、`.env.example`：新增 `schema_token_budget`，默认 32000（deepseek-chat 上下文 64K 的一半左右）。
- `backend/tests/conftest.py`：测试默认读不到真实字段。
- 统计缓存命中：
  - `backend/app/agent/trace.py`：每次模型调用记下从模型前缀缓存读取的输入 token（AgentScope 的 DeepSeek 适配层已经把 `prompt_cache_hit_tokens` 读进 `ChatUsage.cache_input_tokens`），`usage()` 新增 `cached_input_tokens`。
  - `backend/app/api/schemas.py`：`CallUsage` 新增 `cached_input_tokens`。
  - `eval/runner.py`、`eval/run_eval.py`：每题记录缓存命中的 token，指标新增 `cached_input_tokens_avg` 和 `cache_hit_rate`（全部输入 token 里缓存命中的比例），并打印出来。
  - 数据库里的调用记录（`call_traces`）和前端没有加这一项。

规划（`planner.py`）和直接回答的提示词没有改，还是用原来的检索结果。第 2 条规则（写死的表名）和第 5 条规则（业务口径）留给第 7 步。

测试：`test_trace.py`、`test_eval_runner.py` 补上缓存 token 的断言。新增 `backend/tests/test_schema_context.py`，14 个测试：

- 按种子数据和视图的真实字段生成表结构：10 张表全部放进去，每个字段都有类型，包括检索不到的关联键；约 2.7k token。
- 关联图和补中间表；超过预算时只放需要的表，最需要的在前，预算很小时至少放一张；不认识的表跳过。
- 需要的表的来源和顺序（检索、指标、案例、历史）；历史表只取最近 3 条回答，最新的在前。
- 提示词顺序、有表结构时不重复放检索字段、退回原来做法时的写法、第 11 条规则、读表结构失败时退回。

## 为什么做

原来提示词里只有检索命中的 6 个左右字段，第 11 条规则说"字段只列出一部分，没见过先调用 describe_table"。关联键、时间字段、状态字段在问题里通常不会出现，最容易检索不到，模型只能先调用 `describe_table` 或者猜。第 5 步的评测里 `describe_table` 还有 23 次，字段写错 4 次。

现在只有 10 张表、69 个字段，全部放进去大约 2.7k token，比裁剪的风险小得多。以后换成大库，超过预算会自动切到只放需要的表，不用改代码。

## 有什么用

- 模型写 SQL 前就能看到要用的表的全部字段，不用先查，也不用猜。
- 追问时，上一轮 SQL 用到的表一定在上下文里。
- 库变大以后，预算会自动控制提示词的长度。

## 如何验证

运行过的命令和结果：

- `cd project/backend && PYTHONPATH=. ../../.venv/bin/python -m pytest -q`：334 passed，1 skipped（集成测试）。
- `cd project && ../.venv/bin/ruff check backend eval data`：All checks passed。
- 重建容器后，对真实数据库生成表结构：10 张表全部放进去，估算 2728 token；`customers` 只有视图里的 5 个字段，没有手机号和地址。
- 完整评测（`eval/results/step6-2026-11-08.json`）：

  | | 基线 | 第 5 步 | 第 6 步 |
  | --- | --- | --- | --- |
  | 问答评测 | 43/64 | 56/71 | 57/71 |
  | 宽松比对 | — | 40/49 | 41/49 |
  | 路由 / 归因 / 安全 | 27/33、4/4、21/21 | 33/33、4/4、21/21 | 同左 |
  | SQL 条数 / 失败 | 136 / — | 150 / 7 | 153 / 0 |
  | 字段不存在 | 2 | 4 | 0 |
  | `describe_table` | 38 | 23 | 0 |
  | 平均模型调用 | 5.3 | 4.8 | 4.5 |
  | 平均输入 token | 14412 | 13143 | 17513 |
  | 平均输出 token | — | 1128 | 1077 |

  - SQL 一条都没有失败，字段写错 0 次，`describe_table` 0 次。第 5 步的 7 次失败里有 3 次是 MySQL 3024（查询超时），这次没有出现。这一点可能有随机因素，没有查原因。
  - 结果翻转 5 题：q08、n01、n06 通过；m04、i03 失败。m04、i03 的 SQL 是对的，只是多输出了 GMV 和花费两列，宽松比对通过，属于评分偏严。
  - 剩下没通过的 14 题：4 题归因题没有参考答案；列数不同 6 题、数值不同 2 题、行数不同 1 题；n11 数字未核对。没有一题是字段或表选错。

需要说明的内容：

- **输入 token 增加了约 33%**（每题平均 13143 → 17513）。原因是每次模型调用都带着完整的表结构，每次多约 1.1k token。
- **缓存命中**：加上统计后又跑了一次问答评测（`--mode questions`，`eval/results/step6-cache-2026-11-08.json`）：

  | | 数值 |
  | --- | --- |
  | 平均输入 token | 17396 |
  | 其中缓存命中 | 14670 |
  | 命中率 | 84.3% |
  | 平均没有命中的输入 token | 2725 |

  - 按类别：查询类题（单表、多表、指标、追问、多轮、consistency）命中率 93% 到 95%；分析题 79.5%（平均 51149 token，多个步骤并行，各自的对话不同）；直接回答 77.8%（只有一次模型调用，平均 1756 token）。
  - 表结构放在规则后面、时间范围前面，同一个问题里的多次模型调用、不同问题之间都能共用这段前缀，所以多出来的 token 大部分按缓存价计费。
  - 第 5 步的评测没有统计缓存，无法直接比较两步没有命中的 token。
  - 这次问答 55/71（上一次 57/71）。翻转的题：q08、n06、n12 失败，n11 通过。n06、n12 是"数字未核对"，每次时好时坏；q08 按支付时间而不是发货时间统计包裹，是"字段存在但用错了"，第 5 步也失败过。SQL 失败、字段写错、`describe_table` 仍然都是 0。
- 只跑了一次评测，准确率的变化（56 → 57）在随机波动范围内。能确定的收益是 SQL 失败、字段写错、`describe_table` 都降到了 0。
- "超过预算只放需要的表"这条路只有单元测试，现在的库走不到，没有用真实的大库验证过。
- 状态这类字段的取值目前写在字段说明里，没有自动从数据库读取。
- 表结构的真实字段按进程缓存，改表结构后要重启服务。
