# 按数据集加载评测和界面配置

日期：2026-11-10

## 做了什么

- `backend/app/agent/answer.py` 从列值识别日期、文本标签和数值度量。日期序列用折线，少量完整的非负分类数据用饼图，其他分类用柱状图。缺少合适的轴时显示表格。`frontend/src/lib/chart.ts` 继续兼容历史图表，保留有效的文本标识和缺失数值。
- `eval/questions.json` 把 15 道分析题的 `expected_dimensions` 改为维度名到匹配词的映射。`backend/app/evaluation/runner.py` 直接读取题内词表，旧自定义列表可以按维度原名匹配。
- `backend/app/datasets.py` 从 `data/*/manifest.json` 按业务库查找清单。新增 `data/ecommerce_demo/manifest.json`，扩充 `data/dabench/manifest.json`。清单的 `eval` 段配置相对路径，支持 JSON 题集或附核对记录的 DABench CSV。`backend/app/evaluation/run.py` 和 `__main__.py` 让评测任务和检索评测共享这一选题方式，`backend/Dockerfile` 将清单一起打包。
- `backend/app/config.py` 按显式覆盖、清单日期、当天的顺序取基准日期。电商沿用 2026-10-31，DABench 选用线索创建日期的上界 2025-10-28。更新 `docker-compose.yml`、`.env.example` 和 `.env.dabench.example`，DABench 的 Compose 实例用 `DABENCH_AS_OF_DATE` 单独覆盖。
- `backend/app/api/routes.py` 的 `/api/meta` 返回清单示例。`frontend/src/pages/ChatPage.tsx`、`components/chat/EmptyState.tsx`、`Composer.tsx` 和 `types.ts` 将示例用于空白页和输入提示。输入提示按查询或分析模式挑选示例。
- `backend/app/agent/pipeline.py` 合并通用隐私词和当前清单的附加词。字段级保护由业务视图继续执行，清单中的 `hidden` 字段仍不进入视图。
- 新增 `backend/tests/test_datasets.py`，调整图表、评测和规划相关测试，更新优化方案总览和计划 3 的状态。

## 为什么做

列名、示例和判分词原先带有电商业务假设，评测也通过代码表查找数据集。将这些配置放入清单和题集后，新增数据集只需提供自己的配置和题集。图表可以根据同样的结果结构识别贷款、库存等其他数据。

## 有什么用

切换业务库后，问答和页面使用该库的基准日期、示例和隐私词，评测只读取该库配置的题。没有清单的业务库仍可问答，日期取当天，示例为空。评测缺少题集配置时会给出错误信息。DABench 题沿用现有参考答案和检查点规则，缺少判分依据时照常记录并列为不计分。

## 如何验证

- `cd project/backend && PYTHONPATH=. ../../.venv/bin/python -m pytest -q` 通过 689 项，默认跳过 API 集成测试模块。以 `RUN_INTEGRATION=1` 单独运行 `tests/test_api_integration.py`，3 项通过。
- `../.venv/bin/ruff check backend eval data` 和 `git diff --check` 通过。`npm test` 通过 93 项，`npm run build` 成功，仍有包体积提示。
- 逐字段对照 78 道电商题。15 道分析题迁移后的匹配词与旧规则完全一致，其余题目字段相同。
- `docker build --quiet -f backend/Dockerfile .` 成功。在新镜像中分别检查两个数据集。电商加载 78 道题，DABench 加载 10 道题，`/api/meta` 各自返回正确的基准日期和示例。DABench 检查只加载题目和构建任务，未执行评测。
- 用 `docker compose --profile dabench config` 检查 DABench 日期覆盖，电商的日期配置独立。
- 电商实例小样本 `run --mode questions --category metric --limit 1 --concurrency 1` 运行编号 26。选中 i01，宽松和严格结果比对均通过。
- 验证使用临时容器和本地测试。未运行全量评测、DABench 服务评测或浏览器手动验收。
