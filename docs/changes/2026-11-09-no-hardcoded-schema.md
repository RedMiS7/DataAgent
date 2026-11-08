# 去掉写死的表名、库名、方言和业务规则

日期：2026-11-09

## 做了什么

这是[问答流程优化方案](../问题记录/问答流程优化方案.md)的第 7 步：校验器和提示词里不再写死这个库的表名、库名、SQL 方言和业务口径，改成从配置和数据库本身读取，为换库做准备。

- `backend/app/safety/validator.py`
  - 删掉写死的 `ALLOWED_TABLES` 和 `ALLOWED_SCHEMA = "safe"`，新增 `SQLPolicy`（库名、方言、可查的表和字段）。`validate_and_limit(sql, max_rows, policy)` 按它检查：库名必须是业务库，表必须是业务库里只读账号能看到的表，字段必须存在（第 5 步的字段校验）。
  - 解析、字段校验、输出 SQL 都用 `policy.dialect`，不再固定 `mysql`。`unknown_column_hint` 新增 `dialect` 参数。
  - 读不到真实的表和字段时（`columns` 为 None），只检查库名，不检查表名和字段，记一条警告，由只读账号的权限兜底。这和第 5 步"读不到字段就不做字段校验"一致。
  - 系统库黑名单（`mysql`、`information_schema` 等）保留。
- `backend/app/agent/tools.py`：新增 `sql_policy(settings)`，用业务库名、`sql_dialect` 和 `known_columns` 生成规则。`run_sql`、归因分析（`analysis/attribution.py`）、评测的参考答案和安全评测（`eval/runner.py`）都用它，四处的检查完全一致。`run_sql` 的参数说明去掉了"MySQL""safe 库"。
- `backend/app/config.py`、`.env.example`：新增 `sql_dialect`，默认 `mysql`，按 sqlglot 的名字写。
- `backend/app/agent/pipeline.py`：系统提示词
  - 第 2 条：从列出 10 个表名，改为"只能查询业务库里的表，表名照【表结构】的写法写，带上库名；没列出的表先调用 describe_table 确认"。
  - 第 3 条：去掉 `shop 库`，改为"不要访问其他库或系统库"。
  - 第 5 条：从写死的 `paid_at`、`completed_at`、`status <> 'cancelled'`，改为"按【指标口径】的公式和【表结构】的字段说明执行"。这些口径原本就写在指标公式和字段说明里，例如 GMV 的公式带 `o.status <> 'cancelled' AND o.paid_at`，`paid_at` 的说明是"成交类指标按此字段归属时间"。另外补了一句"用户明确指定了口径时，按用户的口径查，这不算绕过规则"，原因见下面的验证。
  - 规则后面新加一行"数据库：MySQL，SQL 按 MySQL 的语法写。业务库名：safe。"，按配置生成，放在表结构前面，仍是固定前缀，不影响缓存。
  - 记录口径说明时找用到的表，原来用写死 `safe.` 的正则，改用 `table_names(sql, 业务库名)`。
- `backend/app/agent/planner.py`：删掉写死的 `TABLES`，改为"可以查的数据见参考资料里的【表摘要】"。规划的输入本来就带全部表的摘要。

测试：

- 新增 `backend/tests/business_schema.py`：从初始化 SQL 读出视图的字段，生成测试用的 `SQLPolicy`，`test_validator.py` 和 `test_column_check.py` 共用。
- `test_validator.py`：新增 3 个测试，可查的表来自规则；读不到表时只检查库名，系统库和其他库照样拦；库名和方言换成 `public` / `postgres` 时按新的检查和输出。
- `test_schema_context.py`：新增 3 个测试，系统提示词、分析提示词和规划提示词里不出现任何表名、字段名、库名（`orders`、`paid_at`、`cancelled`、`shop`、`safe` 等）；第 5 条规则；数据库说明按配置生成。
- `test_pipeline.py`：两个日志测试按内容找警告，不受新警告影响。

没改的写死内容（不在这一步的范围）：

- `analysis/attribution.py` 的归因 SQL：针对退货率写死的固定分析，换库时要重写。
- `semantic/repository.py` 的 `fetch_entity_values`：从哪几张表读实体值（商品名、类目、渠道、省份）是写死的。
- `semantic_tables`、`schema_items`、`metrics` 的种子数据：本来就是这个库的语义层，换库要换数据，不用改代码。
- `MYSQL_*` 的配置名和 `pymysql` 驱动：换成其他数据库还要改连接层。

## 为什么做

原来接一个新库要改代码里的白名单、提示词里的表名和口径、规划里的表列表，几处很容易漏掉，漏掉就会拦住合法的表或给出错误的口径。第 6 步以后，表结构已经从数据库读、整表放进提示词，这些写死的内容就重复了。

## 有什么用

- 换库时，可查的表跟着只读账号能看到的表走，不用改白名单。
- 换方言时改 `SQL_DIALECT`，校验和提示词一起变。
- 业务口径只在指标口径和字段说明里维护一份，不会和提示词里的写法打架。

## 如何验证

运行过的命令和结果：

- `cd project/backend && PYTHONPATH=. ../../.venv/bin/python -m pytest -q`：340 passed，1 skipped（集成测试）。
- `cd project && ../.venv/bin/ruff check backend eval data`：All checks passed。
- `cd project/frontend && npm test`：81 passed。前端代码没有改动。
- 完整评测跑了两次，`eval/results/step7-2026-11-08.json` 是第二次（最终代码）的结果：

  | | 第 6 步 | 第 7 步第一次 | 第 7 步第二次（最终） |
  | --- | --- | --- | --- |
  | 问答评测 | 57/71 | 59/71 | 59/71 |
  | consistency | 6/6 | 5/6 | 6/6 |
  | analysis | 14/15 | 15/15 | 14/15 |
  | 路由 / 归因 / 安全 | 33/33、4/4、21/21 | 同左 | 同左 |
  | SQL 失败 / 字段不存在 | 0 / 0 | 0 / 0 | 4 / 2 |
  | `describe_table` | 0 | 0 | 0 |
  | 平均输入 token / 缓存命中率 | 17513 / — | 17561 / 77.8% | 17698 / 79.4% |

  - 安全评测 21/21，包括"未授权表 `safe.unknown_table`"：可查的表从数据库读出来以后，照样能拦住。
  - **第一次评测里 c06 被拒绝了**：第一轮"上个月客单价按订单总金额（含运费）算，各渠道是多少？"，模型调用了 `decline_request`（调用记录 log_id 1034）。新的第 5 条说口径按【指标口径】执行，用户指定的口径和指标里的客单价不一样，模型把它当成了绕过规则。在第 5 条补了"用户明确指定了口径时，按用户的口径查，这不算绕过规则"。修改前在容器里重跑这一句 6 次，都没有拒绝，说明是偶发的；修改后重跑 10 次，也都没有拒绝；第二次完整评测里 c06 通过。偶发问题，这些次数不能证明已经根除。
  - 第二次评测的 4 次 SQL 失败都和这一步无关：
    - a04（归因题）2 次被字段校验拦下：SQL 写了 `cur.gmv`，但 `gmv` 在子查询 `curg` 里，执行到数据库也会报错，拦得对。不过提示写的是"查询里没有叫 cur 的表或别名"，这句话不准确：`cur` 是子查询的别名，第 5 步的提示只认得真实表的别名。这是第 5 步提示信息的问题，这次没改。
    - n05 1 次 MySQL 1055（`GROUP BY` 不合规），n08 1 次 MySQL 3024（查询超时）。
  - 两次评测之间的题目翻转（m04、i03、n11 通过，n05 失败等）属于随机波动，没有一题是表选错或口径被换掉。

需要说明的内容：

- 只在现在的 MySQL 库上跑过评测。换成 PostgreSQL 等其他方言，只有校验器的单元测试，没有接过真实的库；连接层还是 MySQL。
- 读不到表和字段时，校验退化成只检查库名，比原来的写死白名单宽松。这种情况下查询通常也执行不了，只读账号的权限也只开放业务库，但这一层检查确实变弱了。
