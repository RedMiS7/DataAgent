# 新增字段查看工具和后端日志

日期：2026-11-06

## 做了什么

字段查看：

- `backend/app/agent/tools.py`：新增 `describe_table`，只在分析模式注册。字段不存在时，分析模式的报错会提示模型先查字段。
- `backend/app/semantic/repository.py`：新增 `fetch_table_columns`，读取 `safe` 库的真实字段。
- `backend/app/agent/pipeline.py`：分析提示词要求 SQL 里的字段必须见过，否则先查；步数上限从 12 调到 14。

后端日志：

- `backend/app/main.py`：配置日志格式和级别，只让本项目的日志跟随 `LOG_LEVEL`，并过滤模型客户端关闭时的 `Event loop is closed`。
- `backend/app/config.py`、`.env.example`：新增 `LOG_LEVEL`，默认 `INFO`。
- `backend/app/agent/tools.py`、`pipeline.py`、`session.py`、`backend/app/api/routes.py`：记录问答开始和结束、历史回退、每条 SQL 的成功、失败和拦截、结论校验问题、异常堆栈。日志用对话 id 后 6 位标记。
- `docker-compose.yml`：app 服务增加 `TZ: Asia/Shanghai`，日志时间和 MySQL 一致。这行不在原方案内，是实现时发现容器用 UTC 后补的。

其他：

- `backend/app/agent/tools.py`：修复“SQL 执行失败”重复显示。
- `backend/tests/test_pipeline.py`：补充测试。
- 新增 `docs/问题记录/模型猜错字段名.md`，更新 `踩坑记录.md`。

## 为什么做

模型看不到真实字段时会猜字段名，导致 SQL 报 1054。模型能自己恢复，但失败信息没有写到任何地方，Docker 里只有 `200 OK`，还被无关堆栈盖住。

字段用工具按需查看，而不是全部放进提示词，这是用户选择的渐进式加载方案。方案比较见问题文档。

## 有什么用

分析模式可以按需查看真实字段，减少猜错。查看后端问题时，可以用 `docker compose logs -f app` 看到每次问答的过程和错误。

## 如何验证

- 后端测试：`PYTHONPATH=. ../../.venv/bin/python -m pytest -q`，134 个通过，1 个跳过。
- 后端 lint：`ruff check backend eval data` 通过。
- 前端：`npm test` 52 个通过。这次没有改前端代码，也没有重新运行 `npm run build`，前端由 Docker 镜像构建打包。

真实模型重放（DeepSeek，不写库）：

| 内容 | 结果 |
| --- | --- |
| 第一版提示词，9 次分析 | 3 次字段报错，都恢复；耗时 10.9 到 20.3 秒 |
| 收紧规则后，9 次分析 | 0 次字段报错，4 次提前查字段；耗时 9.2 到 16.3 秒 |
| 分析模式注入请求（第一版提示词） | 拒绝 |
| 查询模式 2 个简单问题（第一版提示词） | 都查出结果，一个仍显示模板回答 |

Docker 实测：

- 成功、拒绝和失败的日志都能看到。
- `SELECT nope FROM safe.orders` 显示 WARNING，带 MySQL 错误和完整 SQL，随后显示 ERROR。
- 容器时间显示 CST，`Event loop is closed` 在 Docker 实测时为 0 条。

没有验证的内容：

- 没有跑完整评测。
- 收紧规则后没有重放注入请求和查询模式。查询模式的提示词没有改动。
- 没有在 Docker 里验证 `LOG_LEVEL=DEBUG`，只有单元测试。
- 真实的 Agent 异常堆栈只有单元测试。
- 验证在问答日志里留下了 `logcheck-a1b2c3`、`logcheck-d4e5f6` 两条测试对话。
