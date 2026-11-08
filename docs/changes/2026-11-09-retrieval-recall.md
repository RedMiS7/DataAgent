# 检索召回率评测

日期：2026-11-09

## 做了什么

这是[问答流程优化方案](../问题记录/问答流程优化方案.md)的 8.1：新增一个只测检索的评测，看检索能不能把一道题需要的表找全。不调用大模型，只调用 embedding。

- `eval/retrieval_eval.py`（新增）
  - `expected_tables`：一道题需要的表，取参考 SQL 里读的表，加上手工标注的 `expected_tables`。
  - `retrieval_query`：和线上一样，用 `build_retrieval_query` 拼出检索语句，追问会带上前一个问题。
  - `evaluate_retrieval`：对每道需要查表的题跑检索，比较 4 种做法：向量检索（线上用的检索，向量为主、某一类没有结果时用关键词补）、只用关键词，以及两者各自加上关联补全（沿 `join_hint` 补中间表）。
  - 统计：找全的题数和比例、表召回率（需要的表里找到了多少）、平均返回几张表、这些表写成完整表结构约多少 token，以及每道漏表的题漏了哪张表。
- `eval/run_eval.py`：新增 `--mode retrieval`。不包含在 `--mode all` 里，调检索时单独跑。
- `eval/questions.json`：给没有参考 SQL、但需要查表的 25 道题手工标注 `expected_tables`：分析 15 题、归因 4 题、追问口径 6 题。标的是回答这道题最少需要的表。拒答和直接回答的题不需要查表，不参与统计。
- `backend/app/agent/tools.py`：新增 `retrieved_tables`，从检索结果里取出它指向的表（命中字段所在的表、命中指标的 `related_tables`、命中案例 SQL 里的表）。线上的 `schema_context` 和这个评测共用它，评测测的就是线上选表用的同一份逻辑。
- 测试：新增 `backend/tests/test_retrieval_eval.py`，7 个测试：从参考 SQL 和手工标注取表、每道需要查数的题都有表、标注的表都存在、追问的检索语句、打分和汇总、`retrieved_tables` 的顺序、用假检索跑通整个评测。`test_schema_context.py` 的一处断言改为不带库名的表名。

## 为什么做

第 8 步要把"给模型哪些表"交给检索，模型只能看到检索到的表。检索漏表，答案一定错。之前从没单独量过检索：线上把全部表都放进去，检索漏没漏表看不出来。先把召回率量出来，8.2 改检索时才能知道每处改动有没有用。

## 有什么用

- 几秒钟就能看出检索漏了哪道题的哪张表，8.2 可以改一处、跑一次。
- 找出了现在检索的具体问题（见下文），8.2 有了明确的目标。

## 如何验证

运行过的命令和结果：

- `cd project/backend && PYTHONPATH=. ../../.venv/bin/python -m pytest -q`：347 passed，1 skipped（集成测试）。
- `cd project && ../.venv/bin/ruff check backend eval data`：All checks passed。
- 在容器里对真实的向量库跑基线：`docker compose run --rm --no-deps -T -v "$PWD":/work -w /work app python eval/run_eval.py --mode retrieval --output eval/results/retrieval-baseline-2026-11-09.json`。向量检索可用，没有退化成关键词。64 道题：

  | 做法 | 找全 | 表召回率 | 平均表数 | 平均 token |
  | --- | --- | --- | --- | --- |
  | 向量检索（线上） | 54/64（84.4%） | 90.8% | 4.8 | 1517 |
  | 向量检索 + 关联补全 | 59/64（92.2%） | 95.4% | 5.9 | 1795 |
  | 关键词检索 | 55/64（85.9%） | 90.0% | 4.2 | 1328 |
  | 关键词检索 + 关联补全 | 58/64（90.6%） | 92.3% | 4.8 | 1461 |

发现的问题：

- **向量检索漏维度表**：问"各渠道"漏 `channels`（7 题），问"商品"漏 `products`（3 题），问"承运商"漏 `shipments`（1 题）。问"各渠道"时，向量命中的是 `orders.channel_id`，没有命中 `channels` 表本身。
- **向量检索多带了不需要的表**：64 题里有 44 题带上了 `ad_daily_stats`。在容器里看了 3 道题的检索结果：问"GMV"时，向量最接近的字段是 `ad_daily_stats.attributed_gmv`（广告归因成交金额，0.55 分）；问"订单数"时是 `attributed_orders`。字面意思接近，但不是用户要的表。
- **"向量 + 关联补全"修好的渠道题，有一部分是碰巧**：误选的 `ad_daily_stats` 在补全时，沿 `ad_campaigns → channels → orders` 这条路径补进了 `channels`，并不是检索真的找到了渠道表。所以 92.2% 偏高。
- **关键词检索漏的是另一类题**：没有明确关键词的分析题，比如"最近生意怎么样""老客复购情况怎么样"，什么都找不到。两种检索都漏的题只有 3 道（a04、c04、c06）。
- 对结果做了一个估算，没有改代码：向量和关键词的结果直接合并，找全 62/64；各自补全后再合并，64/64，但平均 6.4 张表，接近全部 10 张。

据此在方案的 8.2 里补了一条补全规则：**命中外键字段时，补上它指向的表**（命中 `orders.channel_id` 补 `channels`，命中 `refunds.product_id` 补 `products`）。按表重排用来去掉误选的 `ad_daily_stats` 这类表。

需要说明的内容：

- 25 道题的 `expected_tables` 是手工标的，标的是"最少需要的表"，带主观判断。比如"哪个渠道最值得加大投入"标了 `orders` 和 `channels`，没有标广告表。
- 只跑了一次。检索不调用大模型，结果是确定的；但向量库重建索引后可能会有变化。
- 平均 token 是按完整表结构估算的，估算方法和第 6 步相同（中文 1 字 1 token，英文 3 个字符 1 token），比实际偏多。
- 这次没有改检索本身，线上行为不变。
