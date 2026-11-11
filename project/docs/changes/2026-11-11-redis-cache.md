# 接入 Redis：结果缓存和提问限流

日期：2026-11-11

## 做了什么

按 `docs/Redis接入方案.md` 第 1.1 节的范围接入 Redis：

- **适配层**（新目录 `backend/app/cache/`）：`Cache` 协议有两种实现，`RedisCache` 和 `MemoryCache`。`RedisCache` 的读写超时是 0.2 秒、连接超时是 0.5 秒，出错时当作没命中并打 WARNING 日志，之后 30 秒内不再访问 Redis，避免每次调用都卡在超时上。`shared_redis`、`caching`、`rate_limit_store` 负责按配置取到对应的实例。
- **SQL 结果缓存**（`nl2sql/executor.py`）：key 由业务库、`as_of_date`、`sql_max_rows` 和合并空白后的 SQL 哈希组成，TTL 1 小时并加 0 到 10% 的随机抖动。查询失败的结果、超过 1MB 的结果、JSON 无法原样表示的结果都不缓存。缓存在安全校验之后才查，里面只放结果。`ExecutionResult` 新增 `cached` 字段，命中时 trace 里的 sql 调用标注"命中缓存"（`agent/trace.py` 的 `timed` 现在可以带 detail，`agent/tools.py`）。
- **Embedding 缓存**（`embedding/cached.py`）：key 由模型、维度和文本哈希组成，TTL 30 天，只把没命中的文本交给模型。向量按 float64 存储，缓存里取出的向量和模型返回的完全相同。网页保存 Key 时的校验（`bootstrap.validate_keys`）和 `check_models` 用 `build_embedder(..., cached=False)` 绕过缓存，否则错的 Key 也能通过校验。
- **Rerank 缓存**（`rerank/cached.py`）：key 由模型、`top_n`、问题和按顺序排列的候选文档组成，TTL 1 天。调用失败不缓存。
- **限流**（`cache/rate_limit.py`、`api/routes.py`）：`/ask`、`/ask/stream` 按 session 和客户端 IP 分别在一分钟的固定窗口里计数，任一项超过 `RATE_LIMIT_PER_MINUTE` 就返回 429，附带中文提示和 `Retry-After`。流式接口在建立 SSE 连接之前就判断。没有 Redis 时在本进程内存里计数，Redis 出错时放行。
- **命中统计**：`/api/meta` 新增 `redis_available`、`cache_enabled`、`cache_stats`（sql、emb、rerank 三类缓存各自的 hit 和 miss），统计按业务库分开。
- **清缓存命令**：`python -m app.semantic.cli clear_cache`，清除 sql、emb、rerank 缓存和统计数，限流计数不清。
- **部署和配置**：`docker-compose.yml` 新增 `redis:7.4-alpine`，内存上限 256MB，满了按 LRU 淘汰，关闭持久化，端口只绑定 127.0.0.1。`app` 和 `app-dabench` 都加上 `REDIS_URL`，并以 `service_started` 方式依赖 redis，Redis 不健康也不会挡住 app 启动。`config.py` 和 `.env.example` 新增 `REDIS_URL`（默认空）、`CACHE_ENABLED`、三个 TTL 和 `RATE_LIMIT_PER_MINUTE`。`requirements.txt` 加 `redis`，`requirements-dev.txt` 加 `fakeredis`。
- 测试：`backend/tests/test_cache.py`（新）。文档：根目录 `CLAUDE.md` 的常用命令。

和原方案的差别：

- `REDIS_URL` 默认为空，只在 compose 里设置。这样本机跑测试和脚本时不会去连 Redis。
- `execute_sql` 按 settings 自己取缓存，没有新增参数，调用方（`agent/tools.py`）和现有测试的写法不变。
- 原方案提到的 `analysis/attribution.py` 已经不调用 `execute_sql` 了，所以没有改。

## 为什么做

- `/ask` 原来没有任何限制，连点或者脚本就能刷光大模型额度。
- 多轮追问、重复提问和评测重跑时，同一条 SQL、同一段文本的 embedding、同一组 rerank 会反复执行。
- 业务代码只依赖 `Cache` 协议，所以单元测试不需要真的 Redis，Redis 挂了也只是变回"不缓存"。这符合 CLAUDE.md"外部服务走适配层"的要求。

## 有什么用

- 大模型调用量有了上限，一个会话或一个 IP 不能独占服务。
- 重复的查库、embedding 和 rerank 不再重复执行。缓存只影响快慢，不影响结果：SQL 每次仍由模型生成并经过校验，缓存里只放结果。
- `/api/meta` 的命中统计可以用来判断缓存值不值得，也能写进评测报告。

## 如何验证

已运行：

- `cd project/backend && PYTHONPATH=. ../../.venv/bin/python -m pytest -q`：834 passed，1 skipped。其中 `tests/test_cache.py` 有 27 个用例，覆盖以下内容：
  - MemoryCache 和 RedisCache（fakeredis）跑同一套读写、批量、计数、按前缀清除的用例，另外测了过期。
  - Redis 抛异常时读取当作没命中、写入不报错、计数返回 None 并打 WARNING，之后的调用直接跳过 Redis。
  - 同一条 SQL 第二次不查库；`as_of_date`、`max_rows` 或业务库变了就不命中；查询失败不缓存；结果超过上限不缓存；校验不通过的 SQL 不会查缓存；命中时 trace 标注"命中缓存"。
  - embedding 部分命中时只把没命中的文本交给模型，返回顺序和输入一致；换模型不共用缓存。
  - rerank 候选换了顺序、`top_n` 变了都不命中；调用失败不缓存。
  - 限流：第 21 次被拒，下一分钟恢复；换 session id 绕不过 IP 限制；上限设 0 或 Redis 出错时放行；`/api/ask` 超限返回 429 和 `Retry-After`；`/api/ask/stream` 在打开流之前返回 429。
- `cd project && ../.venv/bin/ruff check backend eval data`：All checks passed。
- `clear_cache`：没配置 `REDIS_URL` 时提示"没有配置 REDIS_URL"；Redis 连不上时提示"连不上 Redis"。两种情况都以返回码 1 退出。

**没有验证**：本机 Docker 没在运行，也没装 Redis，所以以下几项都还没跑过：
- 用 `docker compose up -d --build` 启动后，`/api/meta` 显示 `redis_available: true`。
- 同一个问题连问两次，第二次命中缓存。
- 停掉 redis 后问答照常。
- 按 `CACHE_ENABLED=false/true` 做评测，对比耗时和正确率。
