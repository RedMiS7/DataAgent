# 评测账号

日期：2026-11-09

对应优化计划 1.2（`docs/优化方案/1-评测基础.md`）。

## 做了什么

评测改用自己的数据库账号 `dataagent_eval`。参考 SQL 由这个账号执行，结果返回全部行，不再截到 500 行。

| 账号 | 谁用 | 权限 |
| --- | --- | --- |
| `shop_ro` | 被测的问答（Agent 的查询），不变 | 视图库只读 |
| `dataagent_app` | 服务读写应用库、写问答日志，不变 | 见 `2026-11-09-app-db-account.md` |
| `dataagent_eval`（新增，`MYSQL_EVAL_USER`） | 评测执行参考 SQL、读语义层 | 每个实例的视图库和应用库：只有 SELECT。原始库（`shop`、`dabench_raw`）不授权 |

涉及文件：

- `backend/app/config.py`
  - 新增 `mysql_eval_user`（默认 `dataagent_eval`）和 `mysql_eval_password`（默认为空）；
  - 新增 `eval_business_url(db)` 和 `eval_app_url()`。
- `backend/app/db/accounts.py`
  - 新增 `EVAL_RIGHTS`、`eval_grant_statements` 和 `ensure_eval_user`；
  - 建账号、重设密码这段代码改由应用账号和评测账号共用（`_ensure_user`）。
- `backend/app/db/engine.py`
  - 新增 `eval_business_connection`，执行超时和 Agent 一样用 `sql_timeout_ms`；
  - 新增 `eval_app_connection`。
- `backend/app/safety/validator.py`：新增 `check_read_only`，只检查是不是单条查询语句，不限表、字段和行数。
- `backend/app/nl2sql/executor.py`：`_normalize` 改名为 `normalize_value`，评测用它把参考结果处理成和 Agent 结果一样的格式。
- `backend/app/semantic/cli.py`
  - 新增 `grant_eval_user` 命令，用管理账号授权当前实例的视图库和应用库，可以重复运行；
  - `import_dataset` 结束时，说明评测账号是否已授权。
- `backend/app/importer.py`：设置了 `MYSQL_EVAL_PASSWORD` 时，导入数据集会同时授权评测账号读新的视图库和应用库。
- `eval/runner.py`
  - `execute_reference` 改用评测账号：先 `check_read_only`，然后取出全部行；
  - 新增 `eval_account_problem`。
- `eval/run_eval.py`：`questions`、`retrieval`、`all` 三种模式在开始前检查评测账号的密码，没有设置时直接退出，并说明怎么设置。
- `eval/retrieval_eval.py`：读语义层改用 `eval_app_connection`。
- `data/initdb/03_grants.sql`：只加注释。这里不建评测账号，因为它的密码不进代码库。
- `.env.example`、`.env.dabench.example`：加了 `MYSQL_EVAL_USER`，`MYSQL_EVAL_PASSWORD` 写成空值。
- `CLAUDE.md`：评测账号的说明和授权命令。
- 测试：
  - `backend/tests/test_db_accounts.py` 新增 9 个；
  - `backend/tests/test_eval_runner.py` 新增 7 个（参数化计入）；
  - `backend/tests/test_retrieval_eval.py` 改为替换新的连接函数。

本机的 `project/.env` 加了 `MYSQL_EVAL_USER` 和一个随机生成的 `MYSQL_EVAL_PASSWORD`。这个文件在 `.gitignore` 里，不会提交。

## 为什么做

以前参考 SQL 用的是 Agent 的那套规则：只读账号、Agent 的 `sql_policy`、500 行上限。所以参考答案超过 500 行时会被截断，而且检查参考答案的规则和检查被测对象的规则是同一套。评测需要的权限和 Agent 不一样：要能读应用库里的问答记录，但不能碰原始库，也不能写。单独建一个账号，权限最清楚。

和计划原文不同的两处：

1. **评测表的写权限推迟到 1.3。** MySQL 只给已经存在的表单独授 INSERT、UPDATE 权限；要对还没建的表授权，必须连 CREATE 一起给，而计划要求评测账号不能建表。`eval_runs`、`eval_case_results` 在 1.3 建表后，在 `grant_eval_user` 里补上这两张表的写权限。
2. **新建数据库时不自动创建评测账号。** 计划要求密码只写在 `.env`，而初始化脚本 `03_grants.sql` 读不到 `.env`。所以改为设好密码后运行一次 `grant_eval_user`。

## 有什么用

- 参考答案不再被截断，判分的依据是完整结果。
- 参考 SQL 不受 Agent 的表和字段限制：评测题可以写 Agent 不允许用的写法，Agent 的规则变了也不会影响评测。
- 评测程序就算出错，也改不了任何数据，读不到原始库里的隐藏字段。
- 后面 1.3 把评测记录写进数据库时，账号已经准备好。

## 如何验证

- 后端测试：`cd project/backend && PYTHONPATH=. ../../.venv/bin/python -m pytest -q`，结果 498 passed，1 skipped。新增的用例覆盖：
  - 评测账号的连接地址；Agent 仍然用 `shop_ro`；
  - 授权语句只有视图库和应用库的 SELECT；导入数据集时不授权原始库；
  - 密码是绑定参数；密码为空、名字不合法时报错；
  - `grant_eval_user` 授权当前实例，输出里没有密码；没设密码时返回 1；
  - 设了密码才在导入时授权；
  - 评测连接用评测账号，并设置执行超时；
  - 参考 SQL 走评测连接，600 行全部返回，Decimal 转成 float；
  - DELETE、INSERT、多条语句和空 SQL 不会连数据库；WITH 和 UNION 可以执行；
  - 没设密码时提示 `MYSQL_EVAL_PASSWORD` 和 `grant_eval_user`；
  - `03_grants.sql` 的语句里没有评测账号。
- lint：`cd project && ../.venv/bin/ruff check backend eval data`，结果 All checks passed。
- 真实 MySQL：
  - 两个实例各运行了一次 `grant_eval_user`，`SHOW GRANTS` 只有 4 条 SELECT：`safe`、`dataagent`、`dabench`、`dataagent_dabench`。
  - 用评测账号逐项确认：
    - 能读 `safe.orders`（200000 行）、`dabench.car_leads`（17820 行）、`dataagent.query_logs`、`dataagent_dabench.semantic_tables`；
    - 读 `shop.orders`、`dabench_raw.car_leads` 都被拒绝（1142）；
    - 往视图库 INSERT、往应用库 DELETE、CREATE TABLE、DROP TABLE、ALTER TABLE 都被拒绝（1142）；GRANT 被拒绝（1044）。
  - 在容器里运行：
    - `CURRENT_USER()` 显示 Agent 的连接是 `shop_ro`，参考 SQL 和读应用库的连接是 `dataagent_eval`；
    - 用评测账号能读出 69 条语义字段；
    - `execute_reference` 执行 `LIMIT 1200` 的查询，返回 1200 行；执行 DELETE，报 SQLValidationError。
  - 在本机用 `MYSQL_EVAL_PASSWORD=` 运行 `eval/run_eval.py --mode questions --limit 1`：打印设置说明，退出码为 1，没有调用模型。

没有验证的：

- 没有跑完整评测，计划在整条链路做完后由用户手动评测。我查过现有 39 道电商题的参考 SQL，都不超过 500 行，所以去掉上限不会改变现在的分数。
- `--mode retrieval` 没有真跑，只在单元测试里替换了连接函数。
- 用 `import_dataset` 导入时授权评测账号，这一点只有单元测试，没有在真实环境重新导入。
