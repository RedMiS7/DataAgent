# 评测按数据集和题型分开计分

日期：2026-11-09

## 做了什么

**题目文件**

- `eval/questions.json`、`eval/routing_cases.json`、`eval/security_cases.json`：每道题加上 `dataset`（现在都是 `ecommerce_demo`）和 `kind`：
  - `data`：查数据并比对结果（单表、多表、口径、追问、多轮、一致性、分析、归因）；
  - `behavior`：拒答、直接回答、提示词注入；
  - `routing`：路由题；
  - `rule`：危险 SQL。
- 路由题的 id 从 `r01`～`r33` 改成 `rt01`～`rt33`，原来和拒答题的 `r01`～`r04` 重复。问答题的 id 没改，和以前的结果文件还能按 id 对照。
- 新增 `eval/dabench_reviews.json`：DABench 10 道题逐题的核对结论（`ok`、`weak_premise`、`suspect`）和核对用的数字。题目原文仍只在 `DB/DABench.csv`，不复制。第 101 题标为 `suspect`（按题目自己的定义，河南 8 月成单是上升，不是下降），第 73 题标为 `weak_premise`。

**评测代码**

- `eval/runner.py`
  - `CaseResult` 增加 `dataset`、`kind`、`scored`。
  - `EvalReport` 不再有全部题的总分，`as_dict()` 输出 `groups`（按"数据集/题型"分组，每组有题数、通过数、准确率、按类别统计、失败的 id、过程指标）和 `unscored`（存疑题的 id）。同一个类别名在不同题型里不会再合并，例如数据题的 `followup` 和路由题的 `followup`。
  - 新增 `dataset_for`（按 `MYSQL_BUSINESS_DB` 找到对应的数据集，找不到就报错）、`select_cases`（只取这个数据集的题，题目没标 `dataset` 就报错）、`case_meta`（`review.status` 为 `suspect` 的题不计分）、`load_dabench_cases`（读 `DB/DABench.csv` 并合上核对记录，有题没核对就报错）。
  - `evaluate_attribution`（写死的退货率归因流程）的结果 id 改成 `fa-a01` 这样，题型记为 `fixed_attribution`，单独成组。原来同样 4 道题在问答链路和这个流程里各算一次分。
  - `evaluate_security` 增加 `dataset` 参数，只跑这个数据集的安全题。
- `eval/run_eval.py`：启动时打印当前业务库和数据集，问答、路由、检索、安全各模式都只取当前数据集的题；按组打印分数；存疑题单独列在最后；结果文件增加 `dataset` 字段。
- `backend/tests/test_eval_runner.py`：更新原来按总分断言的测试，新增 7 个测试：分组计分、存疑题不计分、按业务库选题、未标注时报错、所有题的 id 全局唯一且都有合法的 `dataset` 和 `kind`、DABench 核对记录、固定归因流程单独成组。

DABench 的题这次只是能读出来、带上核对结论，**还没有评测入口**：根因判分要等评测方案第 3、4 步（原子因素、`facts`、评委）。

## 为什么做

原来 `--mode all` 把所有结果合成一个总分。最近一次 `latest.json` 是 91/107（85.1%），其中路由题、危险 SQL、提示词注入、拒答、固定归因流程这 55 道几乎全对，只看电商数据题是 36/52（69.2%）。总分掩盖了数据题的真实水平。另外，电商的题跑在 DABench 实例上必然全错，原来也会混进分数里。

选择"按数据集和题型分组、不出总分"，而不是给总分加权：不同类型的题测的是不同的东西，加权的权重没有依据。

## 有什么用

- 报告第一眼看到的是当前数据集数据题的准确率，不再被规则题抬高。
- 在 DABench 实例（`MYSQL_BUSINESS_DB=dabench`）上跑评测时不会误跑电商的题；连到不认识的业务库时直接报错。
- 前提和数据不符的题照常运行、照常记录，但不影响分数，复核后再决定怎么处理。

## 如何验证

- `cd project/backend && PYTHONPATH=. ../../.venv/bin/python -m pytest -q`：454 passed，1 skipped。
- `cd project && ../.venv/bin/ruff check backend eval data`：All checks passed。
- 在容器里跑 `eval/run_eval.py --mode security`：
  - 默认业务库 `safe`：打印"只跑数据集 ecommerce_demo 的评测题"，规则题 15/15、行为题（注入）6/6，两组分开显示；
  - `MYSQL_BUSINESS_DB=dabench`：打印"当前数据集没有这类题"，没有误跑电商的题；
  - `MYSQL_BUSINESS_DB=other`：报错"业务库 other 没有对应的评测题"。
- **没有跑**带问答和路由的完整评测（`--mode all`），所以没有新格式下的电商数据题实测分数；69.2% 是按旧结果文件拆出来的。
