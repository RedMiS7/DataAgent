# 评测打包进应用并入库，判分修正，多次运行和并发运行

日期：2026-11-09

对应优化计划 1 的第 3 步：1.3 评测打包进应用并入库，同时做 1.4 判分修正、1.5 多次运行和并发运行。

## 做了什么

**1.3 评测打包进应用并入库**

- 评测代码从 `eval/` 挪进后端，放在 `backend/app/evaluation/` 下：
  - `runner.py`：题目和判分，原来的 `eval/runner.py`；
  - `retrieval.py`：检索召回率评测，原来的 `eval/retrieval_eval.py`；
  - `report.py`：打印分数；
  - `run.py`：重复运行和并发运行；
  - `store.py`、`tables.py`：评测表和锁；
  - `__main__.py`：命令行。
- 每道题拆成一个独立任务（`Job`）。`evaluate_questions` 等旧函数保留下来，内部改为按顺序跑这些任务。
- 新命令 `python -m app.evaluation`：
  - `run`：跑一次评测；
  - `list`：评测列表；
  - `show <id> [--verbose]`：分组分数、不计分的题、失败题的 SQL 和结论；
  - `compare <a> <b>`：两次评测的逐题变化；
  - `export <id>`：导出 JSON。
- `eval/run_eval.py` 只保留为本机入口，参数和 `run` 相同，默认仍然写 `eval/results/latest.json`。
- 镜像里带上评测题：`eval/*.json` 复制到 `/eval`，`DB/DABench.csv` 复制到 `/DB`。`.dockerignore` 改为只排除 `eval/results`、`eval/run_eval.py` 和 `DB/data`。
- 新增两张表，`02_app_schema.sql` 和启动时都会建：
  - `eval_runs`：评测的模式、类别、选项、次数、并发数、状态、进度、模型、`schema_mode`、git 提交号、分组分数和错误；
  - `eval_case_results`：每道题每次一行，包括是否通过、是否计分、详情、失败现象、SQL、结论和来源、警告、`log_id`、token、耗时，以及完整结果的 JSON。
- 每题跑完立刻写入数据库。评测产生的问答记录照旧标为 `source=eval`。
- **同一实例同时只能跑一个评测。** 用 MySQL 的 `GET_LOCK` 实现：
  - 拿不到锁就拒绝运行，并提示正在运行的评测编号；
  - 拿到锁时，先把遗留的 `running` 记录标为 `failed`；
  - 进程退出，连接断开，锁就自动释放。
- **续跑：** `run --resume <id>` 跳过已经入库的（题，第几次），沿用原来的模式、类别和次数。按 Ctrl-C 中断时，评测记为 `cancelled`；进程被强行杀掉时，留下的 `running` 由下一次评测标为 `failed`，两种情况都可以续跑。
- **SQL 次数、失败次数、字段错误、describe/search 调用次数改为按 `log_id` 读存档**（`query_executions`、`call_traces`）。原来的 `ToolProbe` 靠临时替换 `tools` 模块的全局函数来计数，并发时计数会串到别的题上，已经删掉。
- **评测账号：** 在两张评测表上加 `SELECT, INSERT, UPDATE`，按表授权，其他权限不变。
  - `grant_eval_user` 先建好评测表再授权，因为 MySQL 只能给已经存在的表授权。没设密码时直接报错，不连数据库。
  - `import_dataset` 改为先建应用库的表，再给评测账号授权。

**1.4 判分修正**

- 数值比较：先比较误差范围（和原来一样），不在范围内时，把实际值按参考值的小数位四舍五入（四舍五入，不是银行家舍入）后再比较。
- 宽松比对作为主分数：参考 SQL 的每一列都要在答案里找到，允许多出列、列的顺序不同。严格比对的结果保存在 `strict_passed`，作为参考指标。少列仍然不通过，失败现象为"列数不同"。
- 没有判分依据的题：既没有参考 SQL、没有 `expect_from_previous`，也没有 `facts`，照常运行和入库，但不计分，原因记为"没有判分依据"（a01～a04 属于这类）。存疑题的原因记为"存疑题"。
- 分析题不再因为"数字未核对"判失败。
- `facts` 检查点（只做了判分逻辑，题目里还没有写检查点）：
  - `sql` 用评测账号执行，第一行的每个值都要出现在结论里。数字允许显示时的四舍五入，也认 `%`、`万`、`亿` 这些单位；
  - `text` 是结论里必须出现的词；
  - 有参考 SQL 的题，行比对和检查点都要通过。
- "意图和反问"这一项没有做：它依赖计划 2 的 2.2（结构化意图），放到第 6 步。

**1.5 多次运行和并发运行**

- `run --repeat N`：每组输出每一次的准确率、平均值、最低值、最高值，以及 N 次全部通过的题占比（pass^N）。
- `run --concurrency N`：默认 4，用线程池执行。多轮题整道作为一个任务，所以轮次顺序不会乱。
- `--no-plan` 要替换模块里的函数，改为在整次评测外面替换一次，避免多个线程交替替换时出错。
- 一道题出了异常，只记这一道失败（失败现象为"评测出错"），不会停掉整个评测。

**涉及的文件**

- 新增：`backend/app/evaluation/` 下的 `__init__.py`、`__main__.py`、`runner.py`（从 `eval/` 挪来）、`retrieval.py`（从 `eval/` 挪来）、`report.py`、`run.py`、`store.py`、`tables.py`
- 修改：
  - `eval/run_eval.py`
  - `backend/app/db/accounts.py`、`backend/app/db/engine.py`
  - `backend/app/bootstrap.py`、`backend/app/importer.py`、`backend/app/semantic/cli.py`
  - `data/initdb/02_app_schema.sql`
  - `backend/Dockerfile`、`.dockerignore`
  - `CLAUDE.md`
- 测试：
  - 改名：`test_eval_runner.py` 改为 `test_evaluation_runner.py`，`test_retrieval_eval.py` 改为 `test_evaluation_retrieval.py`；
  - 新增：`test_evaluation_scoring.py`、`test_evaluation_run.py`；
  - 修改：`test_db_accounts.py`、`test_bootstrap.py`、`test_planner.py`

## 为什么做

- 以前评测要把项目目录挂进容器才能跑，结果只有一个 JSON 文件，每次还会覆盖 `latest.json`。中途断了只能从头再跑，也看不到每道失败题的结论原文。
- 只跑一次看不出波动：上一次全量评测里，有几道题这次过、下次不过。
- 一次全量评测要 18 分钟，题之间又互不依赖，可以并发跑。
- 原来的判分有几处误判：
  - 答案多一列、小数位多一位就判失败；
  - 没有参考答案的题必定失败，拉低了分数；
  - 分析题里模型自己算出的数字被判为"未核对"。

## 有什么用

- 一条 `docker compose exec app python -m app.evaluation run ...` 就能跑评测，直接用实例里保存的 key 和配置，不用挂目录。
- 每次评测和每道题的结果都在数据库里：可以用 `list`、`show`、`compare` 查看和对比；可以从 `show` 给出的 `log_id` 用 `show_trace` 看完整过程；中断了可以续跑。
- 有了波动范围和 pass^N，能分清"改好了"和"碰巧过了"。
- 分数只反映真实的对错，多出的列和小数位不再算错，没有判分依据的题单独列出。

## 如何验证

**单元测试和 lint**
- 后端测试：`cd project/backend && PYTHONPATH=. ../../.venv/bin/python -m pytest -q`，538 个通过，1 个跳过。新增和改动的测试覆盖：
  - 每条判分规则的通过和不通过；
  - 没有判分依据的题不进分组分数；
  - 按存档统计 SQL 和工具调用；
  - 逐题入库和进度；
  - `--repeat 3` 的统计和 pass^N；
  - 续跑只补没跑完的题；
  - 一道题出错不影响别的题；
  - Ctrl-C 后记为 `cancelled`；
  - 并发 4 时多轮题的轮次顺序；
  - 锁被占时拒绝运行，拿到锁时把遗留的 `running` 标为失败；
  - 评测表授权和建表的顺序；
  - JSON 导出包含原来结果文件的全部字段；
  - 两个入口用同一套参数。
- lint：`cd project && ../.venv/bin/ruff check backend eval data`，全部通过。

**在容器里实际运行**（先 `docker compose --profile dabench up -d --build app app-dabench`）
- 授权：两个实例分别运行 `grant_eval_user`，各输出 4 条 GRANT（视图库读、应用库读、两张评测表的 `SELECT, INSERT, UPDATE`）。
- 评测账号的权限，在容器里逐项试过：

  | 操作 | 结果 |
  | --- | --- |
  | 读应用库 | 允许 |
  | 写 `eval_runs` | 允许 |
  | 读 `shop.orders` | 拒绝（1142） |
  | 改 `query_logs` | 拒绝（1142） |
  | 删 `eval_runs` 的行 | 拒绝（1142） |
  | 建表 | 拒绝（1142） |

- 小规模评测（评测 1）：4 道题、并发 4，用时 15 秒，结果逐题入库。`search_semantic` 的次数从存档里读出来了。
- 锁：评测 2 运行期间又启动一个评测，被拒绝，提示"评测 2 正在运行（dataagent）"。全量评测运行期间，本机入口 `eval/run_eval.py` 也被同样拒绝，说明本机入口能连上评测表。
- 续跑：评测 3 跑到 4/10 时用 SIGKILL 杀掉进程，状态停在 `running`。`run --resume 3` 只补跑了剩下的 6 道，结果 10/10；数据库里没有重复的（题，第几次）。
- 检索评测：`docker compose run ... python eval/run_eval.py --mode retrieval --limit 3 --output ...` 正常输出。第一次运行报了一次挂载目录里找不到 `run_eval.py`，原样重试就好了，没有查明原因。
- DABench 实例：`docker compose --profile dabench exec app-dabench python -m app.evaluation run --mode all` 能建评测、写表，但是 0 道题。`questions.json` 里没有 DABench 的题，DABench 的题也还没有接入问答评测，这和改动之前一样。

**全量评测（评测 4）**

命令：`docker compose exec -T -e APP_GIT_COMMIT=... app python -m app.evaluation run --mode all --repeat 3 --concurrency 4`。

- 模型 deepseek-chat，`schema_mode=agentic`，提交号记为 `a76d315abdac-dirty`（工作区有未提交的改动，提交号是手动传进去的）。
- 129 道题 × 3 次 = 387 条结果，没有"评测出错"的题，没有超过 60 秒的题，最长 42 秒。
- 用时 15 分 00 秒（23:44:40～23:59:40）：3 遍全量，平均每遍约 5 分钟。上一次单线程跑一遍用了 18 分 41 秒。
- 结果导出在 `eval/results/2026-11-09-eval-in-app.json`。

| 组 | 新规则（3 次） | 旧规则（同一次运行，近似） |
| --- | --- | --- |
| 电商 · 数据题 | 每次都是 58/60，平均 96.7%，每次都通过 96.7% | 50/64、53/64、51/64，平均 80.2%（78.1%～82.8%） |
| 电商 · 行为题 | 13/13 × 3 | 相同 |
| 电商 · 路由题 | 33/33 × 3 | 相同 |
| 电商 · 规则题 | 15/15 × 3 | 相同 |
| 电商 · 固定归因流程 | 4/4 × 3 | 相同 |

- **旧规则的算法：**用同一次运行的结果按旧规则重判：
  - 比对行的题用严格比对；题目写了 `match: loose` 的仍用宽松比对；
  - 分析题有"未能在查询结果中核对"的警告就算失败；
  - a01～a04 计分、算失败。

  原始行没有保存，所以旧规则这一列也用了新的四舍五入比较，是**近似值**，四舍五入单独带来的差别没有分出来。
- **新旧判法不同的题：**
  - m04、f03：多出列，3 次都不同；
  - m05、i04：各有 1 次多出列；
  - n03、n05、n06、n09、n11、n12、n13：数字未核对；
  - a01～a04：现在不计分。
- **新规则下仍然失败的两道，3 次都失败：**
  - i02（上个月退货率，数值不同）；
  - f05（上上个月卖得最差的商品，返回了 10 行，参考答案是 1 行）。

  这两道是回答本身的问题，不是这次改动造成的，这次没有处理。
- 数据题这次 3 次结果完全相同，所以波动范围为 0。只跑了这一次 3 遍，换个时间或者换模型时的波动还没测过。

**没有验证的**
- 在容器里手动按 Ctrl-C 中断：只有单元测试，没有真实操作过。容器里没有 `pkill`，实际测试的是强行杀进程以后续跑。
- 并发 4 时有没有触发模型的 429 限流：这次没有题出错，但是没有单独统计，要等 1.6 的后端指标。
- 不传 `APP_GIT_COMMIT` 时，容器里的提交号是空的。
- 镜像里的提交号、全新安装时由 `02_app_schema.sql` 建评测表：都没有单独测过。
- 评测 1～3 是验证用的小规模评测，留在了应用库里，没有删。
