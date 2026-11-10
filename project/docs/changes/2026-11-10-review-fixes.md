# 修复优化评审发现的问题

日期：2026-11-10

## 做了什么

- `.dockerignore` 和 `backend/Dockerfile` 仅将 `data/` 下的数据集清单、语义种子和应用库初始化 SQL 纳入镜像。评测使用 `eval/` 下的题集 JSON 和单独指定的 DABench CSV。
- `backend/app/analysis/metric_breakdown.py` 在平均值、最大值和最小值等非可加指标缺少一期数据时保留空值。可加指标在两期分组值分别与总体值对齐后才计算增量贡献占比。比率的分子和分母独立判断。
- `backend/app/agent/planner.py` 在总体查询返回有效数值且核对结果推翻提问前提时，跳过后续拆解和补查，使用总体证据汇总。`backend/app/api/schemas.py`、`frontend/src/types.ts` 和 `frontend/src/lib/plan.ts` 支持显示「已跳过」。
- `backend/app/api/schemas.py`、`backend/app/agent/answer.py`、`backend/app/evaluation/runner.py` 和 `frontend/src/lib/conversations.ts` 将历史 SQL 的单条预算设为 16,000 字符，每轮合计预算为 40,000 字符。预算内保留完整 SQL，超出预算时保留查询用途。
- `backend/app/nl2sql/time_parser.py` 和 `backend/app/agent/intent.py` 支持中文年月日、ISO 日期和两端都写明的日期区间。区间包含用户给出的最后一天。指定月份的「上月」和「同比」以该月为基准。
- 更新后端的拆解、规划、历史 SQL、日期、意图和评测测试，以及前端的历史和计划测试。

## 为什么做

镜像曾复制整个数据目录，可能带入未经业务视图保护的原始文件。缺少一期的非可加指标曾被补零，可能产生虚假的变化和占比。其余问题会把正常结束的分析标成部分失败、让追问使用残缺 SQL，或为明确给出的历史月份发起多余澄清。

## 有什么用

镜像包含应用和评测所需文件，原始数据仍通过导入流程处理。拆解可以区分缺少观测值和数值为零。分析保留推翻前提的证据，追问可复用较长查询的完整口径，历史月份及对比期可直接用于 SQL。

## 如何验证

- 在 `backend/` 运行 `PYTHONPATH=. ../../.venv/bin/python -m pytest -q`，777 项通过，默认跳过 API 集成测试模块。用 `RUN_INTEGRATION=1 LLM_PROVIDER=fake EMBEDDING_PROVIDER=fake` 单独运行 `tests/test_api_integration.py`，3 项通过。
- 在 `frontend/` 运行 `npm test`，95 项通过。`npm run build` 成功，Vite 报告有压缩后的分块超过 500 kB。`../.venv/bin/ruff check backend eval data` 和 `git diff --check` 通过。
- 通过 `docker build --quiet -f backend/Dockerfile .` 构建临时镜像。确认合成的 CSV、XLSX 和原始 JSON 均未进入镜像。镜像内可读取语义种子和应用库初始化 SQL，并加载 78 道电商题和 10 道 DABench 题。
- 用本地 MySQL 执行两期各自缺少一组的合成平均值拆解，确认该组缺失的期间值、增量和贡献占比为空。只读回放一条 5,014 字符的归档 SQL，确认整条 SQL 进入历史提示且可按 MySQL 方言解析。
- 读取 DABench 的两道实际题，核对同比和环比的开始日及结束日。DABench 验证限于题集加载和日期解析，未执行服务评测。
