# 重写前端聊天界面

日期：2026-11-05

## 做了什么

把 `project/frontend` 的问数页改成常见的聊天应用形态，这次只改前端，没有改后端。

- **布局：** 左侧会话栏（新建、搜索、按日期分组、重命名、删除）加功能导航，取代原来的顶部标签页；手机宽度下侧栏变成抽屉。
- **模型切换：** 输入框内的模型选择器，选择保存在本机。
- **思考过程：** 每条回答上方的折叠面板，实时展示处理步骤和耗时，结束后自动折叠。
- **对话操作：** 停止生成、重新生成、编辑问题后重发、复制、回到底部、中文输入法回车保护。
- **回答展示：** 宋体结论、单行结果用大数字展示，图表、表格、口径、SQL、检索用标签切换，支持导出 CSV。拒答和错误有独立样式。
- **主题：** 账本风格的浅色和深色模式，跟随系统，可手动切换。
- **图表：** 配色通过 dataviz 校验脚本；日期标签自动用折线；长标签用横向柱状图；跳过 ID 列和文本列。

新增文件：

- `src/lib/`：`chart.ts`、`conversations.ts`、`csv.ts`、`models.ts`、`reasoning.ts`、`sse.ts`，以及 `__tests__/` 下的 6 个测试文件
- `src/hooks/`：`useChat.ts`、`useTheme.ts`
- `src/components/Sidebar.tsx`
- `src/components/chat/`：`AnswerCard.tsx`、`Composer.tsx`、`EmptyState.tsx`、`ModelPicker.tsx`、`ReasoningPanel.tsx`、`UserBubble.tsx`

修改文件：`src/App.tsx`、`src/api.ts`、`src/main.tsx`、`src/styles.css`、`src/types.ts`、`src/components/ChartView.tsx`、`src/components/ResultTable.tsx`、`src/pages/ChatPage.tsx`、`src/pages/AttributionPage.tsx`、`package.json`、`package-lock.json`。

新增依赖：`@ant-design/icons`，开发依赖 `vitest`。新增脚本 `npm test`。

另外在根目录 `CLAUDE.md` 的“常用命令”里写入了本次实际运行过的命令。

用户指定的 `frontend-design` skill 在本机和 skill 库中都不存在，这次没有使用。

## 为什么做

用户决定继续使用现有前端，不接 AgentScope 自带 UI，并希望具备模型切换、查看思考过程等常见聊天功能。后端暂时没有的能力要求先留空，所以界面和数据结构先做好，接口之后再接。

## 有什么用

用户可以保留多个对话、随时中断或重试，并看到系统每一步在做什么。之前切换标签后图表缩成 100px 的问题已消除，宽度实测保持 734px。

后端以后要接入的内容：

| 能力 | 前端已预留 | 后端需要提供 |
| --- | --- | --- |
| 模型列表 | 请求 `GET /api/models`，失败时用内置列表 | 返回模型列表 |
| 模型切换 | 请求体已带 `model` 字段 | 读取并使用该字段，目前被忽略 |
| 模型推理文本 | 处理 SSE `reasoning` 事件（字段 `delta`） | 发送该事件 |
| 回答所用模型 | 显示 `AnswerPayload.model` | 返回该字段 |
| 会话历史 | 目前存在浏览器 localStorage | 如需跨设备，提供会话接口 |

已知限制：

- 在后端接入之前，切换模型不影响实际回答，菜单里已如实提示。
- 停止生成只断开浏览器连接，后端本次查询仍会跑完。
- 重新生成和编辑重发不会清除后端已保存的历史，追问改写可能拿到旧问题。
- 思考过程目前是流水线步骤，不是模型自身的推理文本。

## 如何验证

- `npm test`：6 个测试文件、27 个测试全部通过。
- `npm run build`：类型检查和构建通过。
- 浏览器实测：用临时服务连接真实后端路由和真实 DeepSeek 模型，屏蔽了数据库写入，测试脚本放在临时目录。
  - 已通过：实时思考过程、趋势折线图、单值结果、追问、拒答、模型选择和保存、停止生成、重新生成、编辑重发、重命名、删除、刷新后恢复、切换页面后图表宽度、浅色和深色模式、375px 宽度无横向溢出、侧栏抽屉。
  - 控制台唯一的报错是 `/api/models` 的 404，属于预期行为。

没有验证的内容：

- 答案反馈按钮只验证了显示，没有实际点击提交。
- CSV 下载只测试了内容生成，没有在浏览器里实际下载。
- 案例审核页只跟随新布局调整，没有逐项回归。
- 没有在 Docker 部署的后端和 `npm run dev` 组合下测试。
- 没有在真实中文输入法下手动测试回车保护。
- 组件层没有自动化测试，单元测试覆盖的是 `src/lib` 的逻辑。
