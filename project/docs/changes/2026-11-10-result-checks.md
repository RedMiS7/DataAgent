# 查询结果合理性检查

日期：2026-11-10

## 做了什么

- 新增 `backend/app/agent/result_checks.py`，根据 SQL 投影检查整体份额、非负件数和金额。份额按 SQL 表达式的倍率使用 0～1 或 0～100% 的范围。明确的查数意图返回零行时，提示核对时间和筛选。
- `backend/app/agent/tools.py` 将检查接入 `run_sql` 成功后的返回值。对可加总且完整的分组结果，保留相同的来源、关联和筛选条件，去掉最外层分组和排序后执行合计查询。每轮最多一次，计入查询预算，SQL 执行限时最多 2 秒。补查使用原只读权限和安全校验，单独记入查询档案和 SQL 追踪。
- `backend/app/agent/pipeline.py` 按最终采用的查询选取结果告警。`planner.py` 把每条查询自己的告警传给分析汇总。补查失败时保留原查询结果，失败原因可在存档中复查。
- 新增 `backend/tests/test_result_checks.py`，补充 `test_pipeline.py`、`test_planner.py`，更新优化方案总览和计划 2 的进度。

## 为什么做

SQL 通过只读校验并成功执行后，结果仍可能有口径问题。工具返回中直接提示可辨识的异常，模型可以在形成结论前复查。原查询和补查分开记录，便于定位数据变化或筛选口径问题。

## 有什么用

范围检查只适用于可识别的份额和非负度量。ROI、增长率、净额等有符号或无统一上限的指标按原口径展示，0 和分母为零时的空值继续保留。分组合计只核对可加总的列，前 N 名、分页、截断、分组筛选和去重计数等跳过相应核对。两条查询之间可能有数据更新，因此合计差异作为提示。模型修正后，回答只展示最终采用的查询的结果告警。

## 如何验证

- `cd project/backend && PYTHONPATH=. ../../.venv/bin/python -m pytest -q` 通过 730 项，默认跳过 API 集成模块。以 `RUN_INTEGRATION=1` 单独运行 `tests/test_api_integration.py`，3 项通过。
- 测试覆盖正常和异常比率、负数与有符号度量、零行与 0 或空值、可加总和不可加总的结果、补查失败、并发去重、预算，以及修正后移除旧告警。
- `../.venv/bin/ruff check backend eval data`、`git diff --check` 和 `docker build --quiet -f backend/Dockerfile .` 通过。
- 用新镜像连接电商 MySQL 做只读核对。8 个渠道的订单数和金额加总与独立总数一致。另执行对照查询，超界份额与零行有提示，相同数值的 ROI 结果正常返回。
- 电商小样本 `run --mode questions --category multi_table --limit 1 --concurrency 1` 只跑 m01。评测编号 27，宽松和严格比对均通过。问答日志 1632 存有一条主查询和一条合计核对 SQL，两条均执行成功。
- 验证使用临时容器。未运行全量评测或 DABench 服务评测。
