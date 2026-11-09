# 对比类追问的时间解析修复

日期：2026-11-07

## 做了什么

- `project/backend/app/nl2sql/time_parser.py`
  - `TimeRange` 新增可选字段 `baseline`，表示对比基准。`as_dict()` 的输出不变。
  - `parse_time_range` 先识别对比基准，再用剩下的文字解析本期。支持的写法：
    - “比/相比/相较/较 + 时间词”，比如“比上上个月高”
    - “和/跟/与/同 + 时间词 + 比/相比/对比”，比如“和去年相比”
    - “占比”不算对比。
  - 新增 `following_period(baseline, as_of)`：取紧接在基准之后的同单位周期，结束日不超过截止日。整月的基准按月推（上个月推出本月至今，去年推出今年至今），其他基准按天数推。
- `project/backend/app/agent/pipeline.py`
  - `resolve_time_range`：问题里只写了对比基准时，即使不算追问，也从历史继承最近一次出现的本期；普通追问会连带继承上一轮的基准；历史里没有可继承的时间时，用 `following_period` 推出本期。
  - `_system_prompt`：有基准时写“对比期：X，[start, end)”，查询模式和分析模式都写；没有基准时，分析模式写“对比期（上一个同长度周期）”。分析规则里的“上一个同长度周期”改成“对比期”。
  - `_user_content` 和 `time_parsed` 状态消息带上对比期的日期。
- 测试：`project/backend/tests/test_time_parser.py`、`project/backend/tests/test_pipeline.py`。
- 第二轮改动还涉及 `project/backend/app/analysis/attribution.py`，详见文末的补充一节。

## 为什么做

as_of_date 为 2026-10-31 时，先问“上个月GMV是多少？”，再追问“为什么比上上个月高？”，会出现两个问题：

1. 原来的解析器只认出问题里的“上上个月”，把 8 月当成了本期。
2. 这句话不算追问，所以不会继承上一轮的 9 月。

分析模式随后拿 8 月和 7 月对比，而用户想要的是 9 月和 8 月对比。“上个月比上上个月高多少”这类问题也有同样的错误。

## 有什么用

- 对比类问题能分清本期和对比基准，追问时本期会沿用上一轮的时间。
- 模型在系统提示词和用户消息里都能拿到两个时间段的准确日期。
- 状态消息会显示对比期，用户可以核对。

## 如何验证

- `cd project/backend && PYTHONPATH=. ../../.venv/bin/python -m pytest -q`：179 passed，1 skipped。
- `cd project && ../.venv/bin/ruff check backend eval data`：All checks passed。
- 新增测试覆盖了上面的复现场景，本期为 [2026-09-01, 2026-10-01)，对比期为 [2026-08-01, 2026-09-01)，测试走真实的 Agent 循环，模型用脚本代替。

未验证：

- 没有在浏览器里接入真实的大模型重新跑这个场景。
- 没有跑 eval。

## 补充：默认对比期按自然月推，归因页使用对比基准

同一天的第二轮改动修了两个老问题：

1. 句子里没写对比基准时，分析模式的“上一个同长度周期”按天数往前推。本期是 9 月（30 天）时，对比期是 [08-02, 09-01)，少了 8/1。
2. 退货率归因页（`/api/attribution/return-rate`）用自己的 `previous_period`，同样按天数推，而且不看问题里写的基准，“比去年高”仍拿 8 月对比。

改动：

- `time_parser.py` 新增 `previous_period(current)`。整月区间（起止都是 1 号）按整月往前推，比如 9 月推出 8 月整月、去年推出前年；其他区间按天数推。`following_period` 也改用同一个整月判断。label 写成“起始日 至 最后一天”。
- `pipeline._system_prompt`：没有基准时，分析模式改用 `previous_period`，提示词写“对比期（上一个周期）”。
- `analysis/attribution.py`：删掉旧的按天数 `previous_period`，改成 `current.baseline or previous_period(current)`。对比期的 label 从“08-02 至 09-01”这种写法改成“08-01 至 08-31”；前端显示的是 start 和 end_exclusive，不受影响。
- 测试：`test_time_parser.py` 覆盖 `previous_period` 的整月、去年、最近 N 天和上周；`test_pipeline.py` 覆盖分析模式的提示词，以及归因页用基准和按整月推对比期这两种情况。数据库和模型都用替身。

验证：后端测试 188 passed、1 skipped；ruff 全部通过。没有连真实数据库跑归因页，也没有跑 eval。

## 仍未处理

- “今年至今”“本月至今”这类“至今”区间，对比期是往前推同样的月数或天数，不是“去年同期”或“上月同期”。比如截止日是 10-31 时，今年至今的对比期是 [2025-03-01, 2026-01-01)。要改成“同期”对比，需要单独设计。
