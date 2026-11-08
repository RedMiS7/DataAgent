# 补充"追问沿用上一轮口径"的评测用例

日期：2026-11-08

## 做了什么

第 4 步（历史里带上每一轮的查询）的评测看不出收益：原有的多轮题在第 3 步就已经全部通过。这次补了专门的用例，并用"带 SQL"和"不带 SQL"两种历史做对比。

评测用例（`eval/questions.json`，新类别 `consistency`，6 题）：

每题第一轮都要求一个和默认口径不同的算法，追问只换时间，要求沿用第一轮的算法。

| 题 | 第一轮 | 追问 | 和默认口径的区别 |
| --- | --- | --- | --- |
| c01 | 上个月按退款申请时间算，各渠道的退款金额 | 那上上个月呢？ | 默认按完成时间 |
| c02 | 上个月各渠道的销售额，把运费也算进去 | 那上上个月呢？ | GMV 默认不含运费 |
| c03 | 上个月各省份一共有多少订单，已取消的也算上 | 那上上个月呢？ | 默认不含已取消 |
| c04 | 最近30天各类目的销量，只看直播带货渠道 | 那上个月呢？ | 多了渠道筛选 |
| c05 | 上个月各渠道因为质量问题退了多少钱 | 那上上个月呢？ | 多了退款原因筛选 |
| c06 | 上个月客单价按订单总金额（含运费）算，各渠道是多少 | 那上上个月呢？ | 默认按商品金额 |

评测代码：

- `eval/runner.py`
  - 用例可以写 `expect_from_previous`（旧日期到新日期的对照表）。参考答案不是写死的 SQL，而是第一轮模型自己写的 SQL 换成新日期：第一轮怎么算的，追问就该怎么算，不管第一轮选了哪种写法。第一轮失败或者 SQL 里没有要替换的日期时，这道题记为失败，并写明原因。
  - 新增 `shift_dates`（一次替换所有日期，避免九月换成八月后又被换一次）和 `previous_reference`。
  - `run_turns`、`case_history` 同时返回第一轮的回答；`reply_history` 可以不带 SQL。
  - 结果里新增 `reference_sql`，记录现场生成的参考答案；失败现象新增"口径和上一轮不同"。
- `eval/run_eval.py`
  - `--category`：只跑指定类别的问答题，可以写多次。
  - `--no-history-sql`：多轮题的历史里不带上一轮的 SQL，相当于第 4 步之前的前端。
  - 结果文件记录 `categories` 和 `history_sql`。

测试：`backend/tests/test_eval_runner.py` 新增 6 个测试，覆盖日期替换、参考答案的生成和失败原因、历史里带不带 SQL、用例格式。

## 为什么做

第 4 步的目的，是让追问沿用上一轮的口径。原来的用例只检查结果对不对，而且追问都按默认口径，带不带上一轮的 SQL 结果都一样，量不出差别。

## 有什么用

第 4 步的效果有了数据，以后改提示词或历史格式时，也能用这组题检查有没有退步。

对比结果（`multiturn` 6 题 + `consistency` 6 题，两种历史各跑 2 次，结果文件 `eval/results/consistency-{with,without}-sql-run{1,2}.json`）：

| | 带 SQL 第 1 次 | 带 SQL 第 2 次 | 不带 SQL 第 1 次 | 不带 SQL 第 2 次 |
| --- | --- | --- | --- | --- |
| consistency | 6/6 | 6/6 | 3/6 | 4/6 |
| multiturn | 6/6 | 6/6 | 6/6 | 6/6 |

不带 SQL 时失败的题：

- c01，两次都失败：第一轮按申请时间、不限状态；追问多加了 `r.status = 'completed'`。
- c03，两次都失败：第一轮按 `paid_at` 统计，追问改成了 `ordered_at`。
- c06，失败一次：列数和第一轮不同。

带 SQL 时，c01、c03 追问的 SQL 和第一轮只差日期。

## 如何验证

运行过的命令和结果：

- `cd project/backend && PYTHONPATH=. ../../.venv/bin/python -m pytest -q`：297 passed，1 skipped（集成测试）。
- `cd project && ../.venv/bin/ruff check backend eval data`：All checks passed。
- 对比评测，带 SQL 和不带 SQL 各跑 2 次，结果如上：

  ```bash
  docker compose run --rm --no-deps -T -v "$PWD":/work -w /work app python eval/run_eval.py --mode questions --category consistency --category multiturn --output <路径>
  docker compose run --rm --no-deps -T -v "$PWD":/work -w /work app python eval/run_eval.py --mode questions --category consistency --category multiturn --no-history-sql --output <路径>
  ```

需要说明的内容：

- 只有 6 道题，每种各跑 2 次，样本小。但 c01、c03 在不带 SQL 时两次都失败、带 SQL 时两次都通过，方向是一致的。
- 这种判定方式只能检查"追问和第一轮算法一致"，检查不了第一轮本身算得对不对。第一轮算错了，追问照着错算也会通过。
- 这次没有跑完整评测（`--mode all`），只跑了这两类题。
