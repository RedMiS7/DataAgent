# 修复追问答错和异常请求答非所问

日期：2026-11-05

## 做了什么

后端：

- `backend/app/agent/pipeline.py`：上一问只用于检索，发给模型的是当前问题；收紧追问判断；时间按当前问题解析，追问没说时间时继承上一个时间；关闭 AgentScope 运行时状态注入；提示词新增“只回答最后一个问题”和拒绝规则；处理 `refused` 结果。
- `backend/app/agent/tools.py`：保留所有成功查询；新增 `decline_request` 工具。
- `backend/app/agent/answer.py`：新增 `select_execution`，选择结论对应的查询；新增 `time_range_warning`，检查 SQL 日期和时间范围。
- `backend/app/nl2sql/time_parser.py`：新增 `is_default`，区分用户没说时间的情况。
- `backend/app/api/schemas.py`：`AnswerPayload` 新增 `warnings`。

前端：`src/types.ts`、`AnswerCard.tsx`、`ReasoningPanel.tsx`、`styles.css` 显示时间警告和拒绝步骤。

测试和评测集：

- 新增 `backend/tests/test_pipeline.py`，修改 `test_scope_models.py`。
- `eval/questions.json` 新增 `f05`，`eval/security_cases.json` 新增 `p06`。
- `eval/runner.py` 支持 `strict`：带这个标记的样例，模型用别的查询代替时算失败。

## 为什么做

用户实测了 3 轮对话：

- “上上个月哪个商品卖的最差”返回了 9 月销量榜。模型其实查对了，但系统把它和上一问拼接后，模型又重查了上一问，只保留最后一条 SQL，正确结论也因数字对不上被换成模板。页面还显示“上上个月”。
- `order_id = '1' OR '1'='1'` 被模型换成“最近 30 天订单数”返回，拒绝说明也被结论校验丢掉了。

对比重放显示，不拼接时全部答对，只关闭时间注入不够，所以选择了这个方案。

## 有什么用

追问和新问题不再互相干扰，页面的结论、表格和 SQL 指向同一次查询。不该执行的请求会明确说明原因，不再返回无关数字。日期不一致时有警告。

## 如何验证

- 后端测试：76 个通过，1 个模块默认跳过（集成测试需要 `RUN_INTEGRATION=1`）。
- `ruff check backend eval data`：通过。
- 前端：`npm test` 27 个通过，`npm run build` 通过。
- 真实 DeepSeek 模型重放，屏蔽了数据库写入：
  - 第二轮问题 3/3 答对（母婴热销款145号，63 件，已用只读账号核对）。
  - 第三轮问题 4/4 拒绝并说明原因，没有执行 SQL，其中 1 次带历史。
  - 真追问“那上上个月呢？”2/2 查到正确月份。
  - 普通问题 3 次都正常回答，没有被误拒。

以下内容没有验证：

- 按用户要求，没有运行 `eval/run_eval.py`，准确率没有新数据，`f05`、`p06` 也还没在评测里跑过。
- 重放次数少，不能代表稳定性。
- 拒绝规则只用这一种写法测过，其他说法可能漏过或误拒。
- 时间警告只有单元测试，没有在页面上触发过；同比类查询可能误报。
- `OR '1'='1'` 仍能通过 SQL 校验，靠的是模型拒绝和只读账号。
- 用户提到的其他 P0 缺陷（时间解析、字段上下文、结论质量等）不在本次范围。
