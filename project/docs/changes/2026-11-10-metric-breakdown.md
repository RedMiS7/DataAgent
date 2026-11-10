# 通用指标变化拆解

日期：2026-11-10

## 做了什么

- `backend/app/analysis/metric_breakdown.py` 新增通用拆解查询，支持聚合表达式、表关联、日期字段、筛选和维度。默认使用当前问题解析出的本期和对比期，也支持显式传入两期。比率的分子和分母各自聚合，再按维度值合并。
- `backend/app/agent/tools.py` 将拆解接入分析工具，沿用只读 SQL 校验、表可见性检查、查询次数上限、调用追踪和存档。拆解的结果有查询编号，可作为结论依据。
- `backend/app/agent/planner.py`、`pipeline.py` 让规划器为归因题安排先核对总体、再按维度拆解。维度步骤可以并行，并能看到总体结果。汇总按同向贡献解释变化，适用时结合历史波动对照。分子和分母各有贡献占比，各组的比率变化用百分点表示。
- 删除 `backend/app/analysis/attribution.py` 和 `api/routes.py` 中的固定退货率接口，清理 `analysis/__init__.py` 的导出。移除 `frontend/src/pages/AttributionPage.tsx`，并清理 `App.tsx`、`components/Sidebar.tsx`、`api.ts`、`types.ts` 和 `styles.css` 中的专用入口、类型和样式。
- `backend/app/evaluation/runner.py`、`run.py` 和 `__main__.py` 移除独立固定归因评测模式。历史结果仍可查看。续跑旧的混合评测时，已完成的固定归因题仍计入进度和报告。
- 新增 `backend/tests/test_metric_breakdown.py`，调整 `test_planner.py`、`test_evaluation_run.py`、`test_evaluation_runner.py`、`test_api_integration.py` 和 `test_pipeline.py` 中受影响的用例。更新优化方案总览和计划 3 的进度。

## 为什么做

旧拆解流程绑定了电商退货率以及商品、渠道、承运商三个维度。让模型提供聚合口径，程序生成并执行只读查询，同一个工具就能处理其他数据集和题目临时定义的指标。比率两侧独立聚合，可以避免直接关联分子、分母明细造成重复计数。

## 有什么用

在聊天中可以直接问指标变化的原因，并展开查看结论引用的查询结果和 SQL。维度按与总体变化同向的贡献优先展示。比率以分子、分母贡献中较高的一侧排序，并保留两侧的具体数值。分母为零时比率留空。总体增量为零或分组无法加总到总体时，相应的贡献占比留空。分析提示词只在贡献可用且分组未截断时使用 30% 规则。

## 如何验证

- 后端测试 `cd project/backend && PYTHONPATH=. ../../.venv/bin/python -m pytest -q`，676 个通过，默认跳过 API 集成测试模块。覆盖本期和对比期、DABench 贷款样例与手写 SQL、零分母、贡献排序、越权查询、截断标记、并发查询预算、查询存档和结论引用。
- API 集成测试 `cd project/backend && PYTHONPATH=. RUN_INTEGRATION=1 ../../.venv/bin/python -m pytest -q tests/test_api_integration.py`，3 个通过。覆盖健康检查、问答反馈和旧归因路由移除。
- `../.venv/bin/ruff check backend eval data` 和 `git diff --check` 通过。`npm test` 通过 90 个前端测试，`npm run build` 成功，构建输出了包体积提示。
- 电商 MySQL 使用只读账号对比 2026 年 9 月和 8 月。商品、渠道、承运商各有 10、8、5 个旧拆解项，两期退款额逐项相符，两期总体退款额和成交额相符。
- 电商小样本评测编号 25 只跑了 a01。该题按现有配置走查询模式，因没有参考 SQL 或检查点而不计分。另用电商实例的配置和数据库运行一次真实模型的分析问答，日志编号 1628。模型先查两期总体退货率，再调用一次 `metric_breakdown` 拆渠道，返回 `ok`，结论引用了拆解查询。其他普通 SQL 有一次数据库报错和一次关联计数拦截，模型修正后完成。
- 未运行全量评测、DABench 服务评测或浏览器手动验收。30% 阈值尚未用异动注入题校准。
