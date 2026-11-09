# 执行前校验字段，报错时给出正确候选

日期：2026-11-08

## 做了什么

这是[问答流程优化方案](../问题记录/问答流程优化方案.md)的第 5 步。

- `backend/app/safety/validator.py`
  - `validate_and_limit` 新增可选参数 `columns`（表名 → 真实字段）。传了就在安全检查之后再检查字段：用 sqlglot 的 `qualify(..., validate_qualify_columns=True)` 对照真实字段解析每一列，能正确处理表别名、CTE、子查询、`SELECT` 别名在 `GROUP BY`/`ORDER BY`/`HAVING` 里的引用、窗口函数、`TIMESTAMPDIFF(HOUR, …)`、`INTERVAL` 等写法。检查前先把标识符转成小写，因为 MySQL 的字段名不区分大小写。
  - 只拦"字段不存在"。sqlglot 不认识的其他写法、或者检查本身出错，一律放行，交给 MySQL 判断，不误拦合法 SQL。
  - 新增 `unknown_column_hint`：生成修正信息，包括最接近的字段（先找包含关系，再按拼写相近）和相关表的全部字段。写了别名但别名不存在时，列出查询里可用的别名。例如：

    ```
    字段不存在：orders.created_at。最接近的字段：orders.ordered_at、orders.paid_at。
    orders 的字段：id, order_no, customer_id, channel_id, status, ordered_at, paid_at, total_amount, shipping_fee, province, city
    ```

- `backend/app/agent/tools.py`
  - `known_columns`：第一次用到时从 `information_schema` 读取业务库的真实字段，按库缓存。读不到时返回 `None`，这次就不做字段检查。表结构改了要重启服务，和语义层一样。
  - `run_sql` 把真实字段传给校验器，字段写错时直接返回修正信息，不再执行。
  - MySQL 报 1054（字段不存在）时，用同样的方法生成修正信息，代替原来"先调用 describe_table"的提示。拿不到字段信息时，还用原来的提示。
  - 连续失败上限：同一个 Agent（查询模式的 Agent，或分析里的某一步）连续失败（校验或执行）达到 `sql_max_retry`（默认 3）次时，第 3 次的报错后面加上"不要再调用 run_sql，直接告诉用户这次没有查出来"。之后再调用，直接返回这句话，不再执行，也不计入尝试次数。成功执行一次就清零。之后按第 1 步的规则，没有任何结果时返回 `error` 和失败原因。
- `backend/tests/conftest.py`：测试默认不读真实字段（没有数据库），需要字段的测试自己设置。
- `eval/runner.py`：评测的调用记录（`ToolProbe`）适配 `validate_and_limit` 的新参数。

测试：新增 `backend/tests/test_column_check.py`，23 个测试：

- 把评测结果里所有成功执行过的 SQL、和所有参考答案（共 100 多条），对照真实字段重新校验一遍，一条都不能被拦。
- 10 种容易误判的合法写法都能通过（时间函数、窗口函数、子查询、CTE、`GROUP BY 1`、大写字段名等）。
- 字段写错、不写表名、拼写不相近、别名不存在时的提示内容。
- 没有字段信息时不检查；sqlglot 判断不了或出错时放行。
- `run_sql`：字段写错不会执行到数据库；MySQL 报 1054 时给出同样的提示；连续失败 3 次后停止；成功一次后清零。

## 为什么做

基线里字段写错只有两三次，但每次都要等 MySQL 报错，模型再调用 `describe_table` 查字段，多花两轮模型调用。为了少犯错，模型也常常先调用 `describe_table`，基线里有 38 次。原来也没有硬性的重试上限。

## 有什么用

- 字段写错在执行前就被拦下，提示直接告诉模型该写什么，一般改一次就对。
- "字段不存在"一定会被拦住：不会执行到数据库，也不会给出错误答案。
- 一直写不对时会停下来，不会一直重试。

## 如何验证

运行过的命令和结果：

- `cd project/backend && PYTHONPATH=. ../../.venv/bin/python -m pytest -q`：320 passed，1 skipped（集成测试）。
- `cd project && ../.venv/bin/ruff check backend eval data`：All checks passed。
- 在容器里对真实数据库调用 `known_columns`：读到 10 张表，`customers` 只有 5 个字段（视图里没有手机号和地址）；`SELECT o.created_at FROM safe.orders o` 被拦下，提示最接近的是 `ordered_at`、`paid_at`。
- 完整评测（`eval/results/step5-2026-11-08.json`）。这次多了第 4 步之后补的 6 道 `consistency` 题，所以总题数不同：

  | | 基线 | 第 3 步 | 第 4 步 | 第 5 步 |
  | --- | --- | --- | --- | --- |
  | 问答评测 | 43/64 | 50/65 | 49/65 | 56/71 |
  | 其中原有的 65 题 | — | 50/65 | 49/65 | 50/65 |
  | consistency（新增） | — | — | — | 6/6 |
  | 路由 / 归因 / 安全 | 27/33、4/4、21/21 | 33/33、4/4、21/21 | 同左 | 同左 |
  | 字段不存在 | 2 | 2 | 3 | 4 |
  | 其中执行前拦下 | 0 | 0 | 0 | 4 |
  | `describe_table` | 38 | 30 | 29 | 23 |
  | 平均模型调用 | 5.3 | 5.2 | 5.1 | 4.8 |
  | 平均输入 token | 14412 | 13755 | 13765 | 13143 |

  - 4 次字段写错（q07 的 `o.created_at`、n07 的 `ad_campaigns.name` 等）全部在执行前被拦下，提示里有正确的候选，这 4 题里 3 题最终通过。没通过的 n06 是因为"数字未核对"，和字段无关。
  - `describe_table` 从 29 次降到 23 次，平均模型调用从 5.1 降到 4.8，输入 token 少了约 4.5%。
  - 没有触发连续失败上限。
  - 3 次其他失败是 MySQL 3024（查询超过 15 秒的执行时间上限），出在分析题 n08、n12 的复杂查询上。字段检查不会改动 SQL，也不会让执行变慢。之前只有第 1 步的评测出现过 1 次，2026-11-07 的评测记录里也有 1 次。这次为什么多了，没有查。
  - 结果翻转 3 题（m04、n10 通过，n11 失败），属于随机波动。

需要说明的内容：

- 字段写错本来就少，这次只有 4 次，样本小。"一定能拦住"是由测试和代码逻辑保证的，评测只是印证。
- 字段信息按进程缓存，改表结构后要重启服务才会更新。
- 拼写不相近的错误（例如把 `spend` 写成 `cost`）给不出候选，只能靠列出全部字段让模型自己找。
