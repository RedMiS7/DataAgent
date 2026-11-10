# 外部服务重试与降级记录

日期：2026-11-10

## 做了什么

- 新增 `backend/app/service_calls.py`，给模型、embedding 和重排统一处理超时、429 和 5xx。首次失败后最多重试两次，退避分别为 0.25 秒和 0.5 秒。当前问答的剩余时间用于限制单次请求和退避，取消会继续向外传递。
- `backend/app/agent/pipeline.py` 从问答开始计时，涵盖前置检索和路由。自动模式在路由前用两种模式中较短的预算，确定模式后从同一起点计算剩余时间。`agent/router.py`、`agent/scope_model.py` 让路由、问答、直接回答、重写和模型探测都在对应事件循环关闭前关闭客户端。
- `backend/app/agent/scope_model.py` 关闭 AgentScope 和 OpenAI 客户端的内置重试。百炼 embedding 保留原生文本和多模态请求格式，使用可取消的异步 HTTP 请求。`embedding/__init__.py`、`embedding/scope_embedder.py` 为每次向量请求建立独立客户端，结束时取消未完成的批次并关闭连接。`rerank/dashscope.py` 用同样的重试机制并在请求结束后关闭 HTTP 客户端。
- `backend/app/semantic/retriever.py`、`semantic/table_search.py` 在实际回退时记录原因。向量检索失败时改用关键词，重排失败时沿用原检索排序。`agent/trace.py` 分别记录重试和回退，`pipeline.py` 把回退提示加入回答的 `degraded` 和 `warnings`，`evaluation/runner.py` 统计失败尝试和实际降级。
- `backend/app/main.py` 移除过滤异步客户端清理异常的日志规则。新增 `backend/tests/test_service_calls.py`，调整模型、问答、表检索和评测指标测试，更新优化方案进度。

## 为什么做

AgentScope 和 OpenAI 客户端各自重试会叠加请求。百炼 SDK 的同步传输也会在连接掉线时自行重发。统一重试后，服务请求有确定的次数上限，等待和单次请求也都受问答剩余时间约束。此前异步客户端的清理可能晚于所在事件循环的结束，显式关闭可将清理放回正确的事件循环。

## 有什么用

临时故障后，服务有机会在当前问题的时间预算内恢复。失败尝试会保留在调用存档里，实际使用备用检索路径时才会生成降级提示。已关闭重排等主动配置继续按原检索顺序运行。每次向量请求用独立客户端，因此多个问答线程可以安全地共用 embedding 适配器。

## 如何验证

- `cd project/backend && PYTHONPATH=. ../../.venv/bin/python -m pytest -q` 通过 747 项，默认跳过 API 集成模块。以 `RUN_INTEGRATION=1` 单独运行 `tests/test_api_integration.py`，3 项通过。
- 单测覆盖超时、429、503、401、退避次数、剩余预算、取消、同一事件循环内关闭客户端、并发向量请求、向量和重排回退，以及重试成功后的评测统计。
- `../.venv/bin/ruff check backend eval data`、`git diff --check` 和 `docker build --quiet -f backend/Dockerfile .` 通过。
- 正常电商小样本评测编号 28，`--mode questions --category multi_table --limit 1 --concurrency 1` 只跑 m01。宽松和严格比对均通过，未记录服务失败或降级。
- 故障注入电商小样本评测编号 29，`--mode questions --category metric --limit 1 --concurrency 1` 只跑 i01。临时重排端点连续返回 503，实际收到 3 次请求。宽松和严格比对均通过，评测统计 3 次重排失败、1 次降级。日志 1636 记录了两次重试和一次降级，回答警告已入库。
- 两次小样本的标准输出和错误输出都检查过，未出现 `Event loop is closed`、`AsyncClient.aclose` 或未处理任务异常。验证使用临时镜像，未运行全量或 DABench 服务评测。
