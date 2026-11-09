# DataAgent 功能现状与差距分析

更新日期：2026-11-05（已包含追问和拒绝执行的修复，修复后尚未重跑完整评测）

## 结论

DataAgent 已经具备完整的问数链路：用中文提问，系统生成并执行 SQL，返回表格、图表、结论和口径。安全层最扎实，20 条安全样例全部通过。

目前最大的短板是**答案还不够可信**：不算归因题，首次真实模型基线严格口径只有 23/36。追问答错月份的问题已修复，但修复后还没有重跑完整评测，自然语言查数不能算完成。所以下一步应该继续把答案做对、做透明，再加新功能。

和同类产品相比，DataAgent 最缺四类能力：

1. 模型切换的后端接入，目前界面已有但不生效。
2. 问题有歧义时能反问，或至少说明自己的假设。
3. 让使用者能持续改进答案的闭环：维护术语和口径、管理验证过的查询、查看使用记录、运行评测。
4. 主动推送：每日简报和异常提醒。

本文的优先级按“小电商卖家能放心用来查数”这个 MVP 目标判断，不沿用已删除的旧功能清单。

## 一、现有功能

状态说明：

- **可用**：已实现，并实际运行过。
- **部分可用**：主流程可用，但有明确缺口。
- **仅前端**：界面已完成，后端未接入。

### 智能问数

| 功能 | 状态 | 说明 |
| --- | --- | --- |
| 中文提问和流式状态 | 可用 | SSE 实时返回处理步骤 |
| Agent 生成并执行 SQL | 部分可用 | 基于 AgentScope，模型调用 `run_sql` 和 `search_semantic` 工具；准确率未达标，修复后待重新评测 |
| 上下文检索 | 可用 | Qdrant 向量检索，不可用时退回关键词匹配并标记降级 |
| 时间解析 | 部分可用 | 支持“上个月”“近 7 天”等；“最近一周”“最近三个月”“10月”“上季度”会被静默当成最近 30 天 |
| SQL 自动修正 | 部分可用 | 报错会反馈给模型重试；空结果不重试，也没有硬性的 3 次上限 |
| 多轮追问 | 部分可用 | 上一问只用于检索，模型只收到当前问题；追问没说时间时继承上一个时间。仍靠规则判断是否追问 |
| 实体值匹配 | 部分可用 | 商品、类目、渠道、省份支持精确和向量匹配，低分时不会反问 |
| 敏感问题拒答 | 部分可用 | 基于关键词，评测 4/4 通过，换说法可能漏过，但数据库层仍读不到敏感字段 |
| 拒绝执行异常请求 | 部分可用 | 模型可调用 `decline_request` 说明原因；`OR '1'='1'` 实测 4/4 被拒，其他说法未系统测试 |
| 时间一致性提示 | 可用 | SQL 日期和显示的时间范围不一致时给出警告 |
| 澄清反问 | 未实现 | 问题有歧义时不会反问 |

### 结果展示

| 功能 | 状态 | 说明 |
| --- | --- | --- |
| 一句话结论 | 部分可用 | 校验结论里的数字，不通过时改用模板，模板结论较生硬 |
| 结果表格 | 部分可用 | 分页和排序可用；因校验器已补 `LIMIT`，截断提示不会触发 |
| 图表 | 可用 | 折线、柱状、环形图；日期自动用折线，长标签用横向柱状图 |
| 单值结果 | 可用 | 大数字展示 |
| 口径说明 | 部分可用 | 能显示公式和定义，但常混入不相关指标 |
| 查看 SQL 和检索内容 | 可用 | 标签切换，SQL 可复制 |
| 导出 CSV | 可用 | 内容生成有单元测试，没在浏览器里实际下载 |

### 聊天界面

| 功能 | 状态 | 说明 |
| --- | --- | --- |
| 会话管理 | 可用 | 新建、搜索、分组、重命名、删除，保存在浏览器本地 |
| 思考过程 | 部分可用 | 显示流水线步骤和耗时，不是模型自身的推理文本 |
| 模型切换 | 仅前端 | 选择可保存，但回答仍用服务端默认模型 |
| 停止、重新生成、编辑重发 | 部分可用 | 前端可用；停止不会中断后端查询，重发不会清除后端历史 |
| 深浅色和手机适配 | 可用 | 375px 宽度实测无横向溢出 |

### 经营分析

| 功能 | 状态 | 说明 |
| --- | --- | --- |
| 退货率异动归因 | 可用 | 对比上一周期，按商品、渠道、物流下钻；只支持退货率一个指标 |

### 查询安全

| 功能 | 状态 | 说明 |
| --- | --- | --- |
| SQL 校验 | 可用 | sqlglot 解析，只放行单条查询，拦截危险函数和系统库；`SELECT @@version` 目前能通过 |
| 只读账号 | 可用 | 实测 `shop_ro` 读不到 `shop` 库 |
| 敏感字段保护 | 可用 | `safe` 视图不含手机号和地址字段 |
| 自动补 LIMIT | 可用 | 上限 500 行 |
| 查询超时 | 可用 | 数据库层限制 15 秒，未用慢查询实测 |
| 提示注入防护 | 可用 | 5 条样例通过，样例数量较少 |

### 语义层与知识库

| 功能 | 状态 | 说明 |
| --- | --- | --- |
| 语义内容 | 部分可用 | 10 张表、47 个字段、9 个指标，启动时自动同步，只能通过改 JSON 种子文件修改 |
| 向量索引 | 部分可用 | 索引为空或 embedding 模型变化时自动全量重建；审核通过不会自动同步 |
| 答案反馈 | 可用 | 标记正确后进入待审核列表 |
| 案例审核 | 部分可用 | 支持通过和驳回，不能修改内容 |

### 数据、评测与运行

| 功能 | 状态 | 说明 |
| --- | --- | --- |
| 业务库和模拟数据 | 可用 | 10 张表，20 万订单，固定随机种子，含大促和低 ROI 渠道；空库时自动生成，已有数据不会重置 |
| 查询日志 | 部分可用 | 记录问题、SQL、耗时和状态；token 用量写死为 0，使用预设模型时模型名为空，没有查看页面 |
| 评测 | 部分可用 | 40 道问答题、20 条安全样例；比对过严，归因题在问答评测中无法通过 |
| 模型适配层 | 可用 | DeepSeek、千问 embedding、OpenAI 兼容接口、离线脚本模型 |
| 网页填写 Key | 可用 | 首次设置页和“模型设置”，保存前校验，页面和接口都读不回 Key |
| 本地部署 | 可用 | `docker compose up -d --build` 一条命令启动，前端已打包进后端镜像 |
| 登录和权限 | 未实现 | 所有接口都没有鉴权 |

## 二、当前质量

以下是 2026-11-05 用 DeepSeek 和千问 `text-embedding-v3` 跑出的首次基线，运行时屏蔽了案例库。

| 指标 | 结果 |
| --- | --- |
| 问答（不含归因）严格口径 | 23/36，63.9% |
| 问答（不含归因）宽松口径 | 约 30/36，83.3% |
| 退货率归因 | 4/4 |
| 危险 SQL 拦截 | 15/15 |
| 提示注入 | 5/5 |
| 响应时间 | 成功问答平均约 4.2 秒，最长 7.7 秒 |

宽松口径指标准答案的列都能在结果里找到，例如模型多返回一列 ID 也算对。这是临时脚本的结果，不是正式指标。

基线之后已修复追问，见下方说明。这组数字是修复前的结果，确认的真实错误有 6 道：

- `f01`、`f02`（已修复）：追问时模型先查对，又补查上一问，系统只保留了最后一条 SQL。已改为只把上一问用于检索，关闭 AgentScope 运行时状态注入，并按结论选择对应查询。
- `q07`：取消订单没有 `paid_at`，模型仍按它统计，答成 0。
- `q08`：物流包裹应按 `shipped_at` 统计。
- `m08`：城市订单应使用订单城市，模型用了客户城市。
- `i02`：退货率只返回了分子和分母，没有算出比率。

测试状态：后端 89 个测试通过，集成测试默认跳过；前端 32 个测试通过，组件层暂无自动化测试。

## 三、参考产品

| 产品 | 类型 | 最值得参考的地方 |
| --- | --- | --- |
| [Databricks Genie](https://docs.databricks.com/genie/index.html) | 企业数据平台 | 指令和示例 SQL 调优、使用监控、最多 500 题的基准评测、按用户权限取数 |
| [Snowflake Cortex Analyst](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst) | 企业数据平台 | 验证过的查询、从高频问题生成待审核建议、起始问题、评测和优化建议 |
| [WrenAI](https://github.com/Canner/WrenAI) | 开源 GenBI | 版本化语义层、`instructions.md` 和 `queries.yml`、SQL 预校验 |
| [SQLBot](https://github.com/dataease/SQLBot/blob/main/docs/README.en.md) | 开源 ChatBI | 术语库、SQL 示例校准、工作空间隔离、网页嵌入和 MCP |
| [Shopify Sidekick](https://help.shopify.com/en/manual/shopify-admin/productivity-tools/sidekick) | 电商卖家助手 | 后台长任务、用代码做预测和对比、变更前确认、[Pulse 主动建议](https://help.shopify.com/en/manual/ai-powered-tools/sidekick/pulse) |
| [Triple Whale Moby](https://www.triplewhale.com/moby-ai) | 电商分析 | 按业务场景分工的专家，定时报告（厂商材料） |
| [ThoughtSpot Spotter](https://www.thoughtspot.com/blog/everyone-gets-an-ai-analyst) | 商业智能 | 回答“为什么”的多维变化分析、预测、异常监控（厂商材料） |
| [Julius AI](https://www.datacamp.com/th/tutorial/julius-ai-guide) | 轻量分析工具 | 上传文件分析，可复用的分析模板（第三方资料） |

[Vanna](https://github.com/vanna-ai/vanna) 的用户感知权限和流式组件设计也值得参考，但它的 GitHub 仓库页面显示已于 2026 年 3 月归档，不建议作为依赖。

### 能力对比

✅ 已有，🟡 部分具备，❌ 没有。其他产品一栏的“未确认”只表示在读到的资料里没有找到，不代表产品没有这项功能。

| 能力 | DataAgent | 同类产品情况 |
| --- | --- | --- |
| 自然语言查数 | ✅ | 全部具备 |
| 语义层维护 | 🟡 只有种子文件 | Genie、Cortex、WrenAI、SQLBot 都有 |
| 验证过的查询 | 🟡 有案例审核 | Cortex 的验证查询，Genie 的示例 SQL，WrenAI 的 `queries.yml` |
| 基准评测 | 🟡 只有脚本 | Genie、Cortex 产品内置 |
| 使用监控 | ❌ 只有日志表 | Genie、Cortex 产品内置 |
| 起始和推荐问题 | 🟡 只有 4 个固定示例 | Genie、Cortex 支持自动生成起始问题 |
| 澄清反问 | ❌ | 读到的官方资料里没有产品内置；Genie 靠作者写指令实现 |
| 用户级数据权限 | ❌ 当前是单店铺 | Genie、Cortex、SQLBot 具备，WrenAI 为商业版 |
| 异动归因 | 🟡 只有退货率 | Spotter 面向通用指标（厂商材料） |
| 看板 | ❌ | WrenAI 可由 Agent 生成看板 |
| 定时报告和提醒 | ❌ | Moby 自动化（厂商材料），Julius 资料说法不一 |
| 预测 | ❌ | Sidekick 用代码计算，Spotter（厂商材料） |
| 文件上传 | ❌ | Julius（第三方资料） |
| 确认后执行操作 | ❌ | Sidekick 变更前需确认 |

## 四、缺失功能和优先级

### P0：让答案可信

MVP 上线前必须解决。这些问题不修，用户拿到的数字可能是错的，而且看不出来。

| 功能 | 为什么重要 | 参考 | 建议做法 |
| --- | --- | --- | --- |
| 补全时间解析 | 常见说法被静默算成最近 30 天 | 无 | 支持中文数字、月份、季度、本周，解析不了时明确告知 |
| 假设透明和澄清反问 | 没说时间或指标有歧义时，系统在替用户猜 | [Genie](https://docs.databricks.com/aws/en/genie/best-practices) 用指令定义何时反问 | 先显示“默认按最近 30 天”，再增加 `clarify` 状态 |
| 补全上下文和口径规则 | 6 道真实错题中有 4 道属于这类问题 | [Genie](https://docs.databricks.com/aws/en/genie/best-practices) 的指令和 SQL 表达式 | 全部 47 个字段都给模型，写清时间、城市和比率的取法 |
| 接入模型切换和推理文本 | 界面已上线，但切换不生效 | [Genie](https://docs.databricks.com/genie/index.html) 等都由服务端配置 | 提供 `/api/models`，读取 `model`，发送 `reasoning` 事件，返回实际模型名 |
| 改善结论质量 | 模板结论生硬，无法给出经营判断 | 无 | 放宽不含数字结论的校验，要求模型使用中文别名 |
| 修正评测口径 | 现在的严格比对低估了准确率，无法指导优化 | [Cortex 评测](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst-evaluations) | 按列比对，归因题移出问答比对，增加多轮题 |
| 修复结果提示 | 用户不知道结果被截断，“重试次数”显示也不准确 | 无 | 用 `limit_applied` 判断截断，空结果重试，限制最大重试次数 |

### P1：形成改进闭环

MVP 跑通后优先做。这些功能让系统越用越准，也让卖家愿意每天打开。

| 功能 | 为什么重要 | 参考 | 建议做法 |
| --- | --- | --- | --- |
| 术语和口径维护页 | 现在改口径要改 JSON 再跑命令 | [SQLBot](https://github.com/dataease/SQLBot/blob/main/docs/README.en.md) 术语库，[Genie](https://docs.databricks.com/aws/en/genie/set-up) 知识存储 | 增加管理页，保存后增量更新索引 |
| 验证过的查询 | 案例通过后不会自动入索引，回答也看不出是否可靠 | [Cortex](https://docs.snowflake.com/en/user-guide/views-semantic/verified-query-repository) 验证查询 | 审核后同步索引，命中时显示“已验证”，支持编辑和下线 |
| 高频问题转案例 | 现在只靠用户主动点赞 | [Cortex 建议](https://docs.snowflake.com/en/user-guide/views-semantic/verified-query-suggestions) | 统计高频问题，生成待审核建议 |
| 推荐问题 | 用户常常不知道能问什么 | [Cortex 起始问题](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst/suggested-questions-feature) | 回答后给 2 到 3 个追问，首页用真实高频问题 |
| 每日简报和异常提醒 | 卖家更需要系统主动提醒，而不是自己想起来查 | [Moby](https://www.triplewhale.com/ai-agents) 定时任务，[Pulse](https://help.shopify.com/en/manual/ai-powered-tools/sidekick/pulse) 建议卡片 | 先在首页显示昨日核心指标和异常，再加邮件或微信推送 |
| 通用异动归因 | 目前只能分析退货率，而 GMV、ROI 同样常问 | [Spotter](https://www.thoughtspot.com/blog/everyone-gets-an-ai-analyst) 变化分析（厂商材料） | 按指标定义维度，把归因接入聊天入口 |
| 使用监控页 | 看不到失败问题，也没有 token 成本数据 | [Genie 监控](https://docs.databricks.com/aws/en/genie-agents/monitor)，[Cortex 观测](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst/admin-observability) | 补齐 token 和模型名，增加问题列表、成功率和差评视图 |
| 登录和管理员权限 | 审核和反馈接口没有鉴权，**公开部署前必须完成** | [Genie](https://docs.databricks.com/aws/en/genie/set-up) 权限分级 | 区分普通用户和管理员，管理页只给管理员 |
| 会话存到后端 | 换设备后历史丢失，前后端历史也会不一致 | [WrenAI](https://docs.getwren.ai/) 提供线程接口 | 增加会话接口，重发时同步删除后端历史 |

### P2：扩展能力

核心稳定后，根据用户反馈再决定。

| 功能 | 价值 | 参考 | 备注 |
| --- | --- | --- | --- |
| 看板和固定答案 | 常看的指标不用每次提问 | [WrenAI](https://github.com/Canner/WrenAI) | 依赖后端保存查询 |
| 预测 | 回答“下个月能卖多少”，辅助备货 | [Sidekick](https://help.shopify.com/en/manual/shopify-admin/productivity-tools/sidekick)，[Spotter](https://media.thoughtspot.com/pdf/ThoughtSpot-SpotMap-Slides-February-2026.pdf)（厂商材料） | 要明确展示不确定性 |
| 代码分析 | 处理 SQL 难以完成的分析 | [Sidekick](https://help.shopify.com/en/manual/shopify-admin/productivity-tools/sidekick) | 必须放进沙箱，不能接触 `.env` |
| 上传表格 | 分析平台外的数据，如成本表 | [Julius](https://www.datacamp.com/th/tutorial/julius-ai-guide)（第三方资料） | 上传文件要隔离，不能绕过安全层 |
| 分析模板 | 把每周复盘变成一键操作 | [Julius](https://www.eesel.ai/blog/julius-ai)（第三方资料） | 可与定时简报共用 |
| 分享和导出报告 | 把结果发给合伙人或会计 | [Genie](https://docs.databricks.com/aws/en/genie-agents/monitor) 对话共享 | 分享前要确认脱敏 |
| MCP 和嵌入 | 接入其他工具 | [SQLBot](https://github.com/dataease/SQLBot/blob/main/docs/README.en.md)，[WrenAI](https://github.com/Canner/WrenAI) | 需先完成鉴权 |
| 查询缓存 | 降低延迟和模型成本 | 无 | 当前响应时间达标，流量上来后再做，是否使用 Redis 待定 |

### 暂不建议做

| 功能 | 原因 |
| --- | --- |
| 确认后修改业务数据 | 与只读安全模型相反，并且需要对接真实电商平台 |
| 连接任意数据库、多店铺、多租户 | 当前面向单店铺模拟数据，用户级行权限也要到这时才需要 |
| 语音和屏幕共享 | 与查数核心价值关系不大 |
| 多专家 Agent 协作 | 单 Agent 的准确率都还没达标，过早增加复杂度 |
| 接入 AgentScope Web UI | 已决定继续使用自有前端；其内置 Agent 默认带 Bash 等工具，且没有真实鉴权 |

## 五、建议顺序

1. **修准确率：** 完成时间、上下文和结论相关的 P0 项，清理案例库里的 `SELECT 1` 测试数据。
2. **修评测并重跑：** 修正比对方式，用新基线确认收益。
3. **接入模型切换：** 让已上线的界面真正生效，并对比不同模型的准确率。
4. **建立闭环：** 优先做验证查询、口径维护页和使用监控。
5. **做主动价值：** 上线每日简报、异常提醒和通用归因。
6. **公开部署前：** 完成登录和管理员权限。

## 六、资料说明

- 现有功能来自 2026-11-05 对代码的阅读和实测。前端聊天界面的改动当时尚未提交。
- 同类产品的结论优先来自官方文档。[ThoughtSpot 文档](https://docs.thoughtspot.com/cloud/26.6.0.cl/spotter-agents)和 Triple Whale 帮助文章读取失败，这两家只能依据厂商公开材料，Julius 依据第三方资料。
- 产品功能变化很快，“未确认”只代表这次读到的页面里没有找到。
- Snowflake 官方已建议从 Cortex Analyst 过渡到 Cortex Agents，这里仍以 Cortex Analyst 的公开文档作为功能参考。
- WrenAI 的不少能力属于商业版，例如行列级权限、GenBI 界面和审计日志，开源版不一定具备。

其他参考资料：

- [Databricks Genie 最佳实践](https://docs.databricks.com/aws/en/genie/best-practices)
- [Databricks Genie 配置](https://docs.databricks.com/aws/en/genie/set-up)
- [Triple Whale Moby 2 发布稿](https://www.wboc.com/online_features/press_releases/triple-whale-unveils-the-ai-operating-system-for-ecommerce-with-the-launch-of-moby-2/article_8a2add9b-c561-5bf2-87f9-4156ea7d7791.html)
- [TechTarget 对 ThoughtSpot Spotter 的报道](https://techtarget.com/searchbusinessanalytics/news/366615693/ThoughtSpot-AI-agent-Spotter-enables-conversational-BI)
