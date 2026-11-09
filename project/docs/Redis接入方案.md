# Redis 接入方案

日期：2026-11-09
状态：待确认（尚未改代码）

## 1. 目标

1. **减少重复计算**：同样的 SQL 不重复查库，同样的文本不重复调 embedding 和 rerank 接口。
2. **控制成本**：按会话和 IP 限流，防止连点或脚本刷掉大模型额度。
3. **为后续做准备**：多实例共享状态、分布式锁、Kafka 异步任务的进度存储，都会用到这一层 Redis。

**不做的事**：本阶段不缓存大模型生成的 SQL 和最终回答，也不做“相似问题复用答案”。原因见第 7 节。

## 2. 现状

| 位置 | 现在的做法 | 问题 |
|---|---|---|
| `backend/app/nl2sql/executor.py` `execute_sql` | 每次都查 MySQL | 同一条 SQL 在多轮追问、重复提问、评测重跑时会被反复执行 |
| `backend/app/semantic/retriever.py:125`、`backend/app/semantic/table_search.py:214` | 每次检索都调用 `embedder.embed([问题])` | 同一个问题可能在一次问答里被向量化不止一次；重复提问时每次都要调用 API |
| `backend/app/semantic/table_search.py:275` | 每次检索都调用 `reranker.rerank(...)` | 同样的问题和候选表，重排结果是一样的 |
| `backend/app/agent/tools.py:56` `_columns_cache` | 进程内的 dict | 只在单个进程内有效，暂时够用，本阶段不动 |
| `backend/app/api/routes.py` `/ask`、`/ask/stream` | 没有任何限制 | 一个会话可以无限次触发大模型调用 |

## 3. 总体设计

```
            ┌──────────── backend/app/cache/ ────────────┐
 调用方 ───▶ │ Cache 协议：get / set / get_many / set_many / incr │
            │   ├─ RedisCache   （生产，redis-py）               │
            │   └─ MemoryCache  （单元测试、没有 Redis 时）       │
            └────────────────────────────────────────────┘
                   ▲               ▲               ▲               ▲
          execute_sql      CachedEmbedder   CachedReranker     RateLimiter
          （SQL 结果）      （向量）          （重排结果）        （限流）
```

- 按 CLAUDE.md 的要求，Redis 通过适配层访问。业务代码只依赖 `Cache` 协议，单元测试用 `MemoryCache`，不需要真的 Redis。
- 写法和现在的 `vector/` 一样：`build_cache(settings)` 启动时先 ping，连不上就返回 `None`，系统按“没有缓存”继续运行。
- embedding 和 rerank 用**装饰器**包一层，`CachedEmbedder` 本身也实现 `EmbeddingProvider` 协议，所以调用方（retriever、table_search、indexer）一行都不用改。

## 4. 各缓存项

### 4.1 SQL 结果缓存（收益最大）

- **key**：`da:v1:sql:{业务库名}:{as_of_date}:{max_rows}:{sha256(规范化后的 SQL)}`
  - 规范化：取 `validate_and_limit` 之后的 `safe_sql.sql`，合并连续空白。
  - 放 `as_of_date`：业务数据按这个日期固定，日期一变 key 就跟着变，旧缓存自然失效。
  - 放 `max_rows`：结果会按它截断，配置改了不能复用旧结果。
- **value**：JSON 格式的 `{columns, rows, truncated}`。`_normalize` 已经把 Decimal 和日期转成了 JSON 能表示的类型。
- **TTL**：默认 3600 秒，再加 0 到 10% 的随机抖动，避免大量 key 同时过期（雪崩）。
- **不缓存的情况**：执行失败的 SQL；序列化后超过 1MB 的结果。
- **顺序**：先校验（`validate_and_limit`），再查缓存，最后执行。**缓存里只放结果，不放 SQL**，所以缓存永远不会绕过安全校验。这一点吸取了 Vanna 的教训：它曾经把客户端传来的 SQL 存进缓存，之后跳过校验直接执行。
- **可观测**：`ExecutionResult` 增加 `cached: bool` 字段，trace 和日志都写上“命中缓存”，前端展示的耗时也能区分出来。
- **调用点**：`agent/tools.py` 的 `run_sql`，以及 `analysis/attribution.py:156`。

### 4.2 Embedding 缓存

- **key**：`da:v1:emb:{模型名}:{维度}:{sha256(文本)}`
- **value**：`numpy.float32` 数组的二进制，1024 维大约 4KB。
- **TTL**：30 天。同一个模型对同一段文本算出的向量不会变；换了模型，key 也就变了。
- **批量**：`CachedEmbedder.embed(texts)` 先用 `MGET` 批量取，只把没命中的文本交给真实模型，再用 pipeline 批量写回。这样 `build_index` 重建索引时也能复用之前算过的向量。

### 4.3 Rerank 缓存

- **key**：`da:v1:rerank:{模型名}:{top_n}:{sha256(query + "\x1f" + 各候选文档用 "\x1e" 拼接)}`
- **value**：`[{index, score}]` 的 JSON。
- **TTL**：1 天。候选表文档来自语义层，语义层一改，文档内容不同，key 也就变了。

### 4.4 限流

- **算法**：固定窗口计数，`INCR da:v1:rl:{维度}:{值}:{当前分钟}`，第一次计数时 `EXPIRE 60`。
- **维度**：同时按 `session_id` 和客户端 IP 计数，任一维度超过上限都拒绝。`session_id` 是前端生成的，用户可以随意换，所以只按它计数不够。
- **配置**：`RATE_LIMIT_PER_MINUTE=20`，设为 0 关闭。
- **超限返回**：`/ask` 返回 HTTP 429，带上中文提示；`/ask/stream` 在建立 SSE 连接之前就返回 429。
- **只限制会调用大模型的接口**：`/ask`、`/ask/stream`、`/attribution/return-rate`。

### 4.5 命中统计

- 每次查缓存时 `INCR da:v1:stats:{sql|emb|rerank}:{hit|miss}`。
- `/api/meta` 增加两个字段：`redis_available`，以及各类缓存的命中数和未命中数。评测和面试时可以直接拿这些数字说话。

## 5. 降级和容错

- Redis 客户端设置 `socket_timeout=0.2s`、`socket_connect_timeout=0.5s`，**Redis 慢了或挂了，都不能拖慢一次问答**。
- 所有 Redis 异常在适配层捕获，打一条 WARNING 日志（带 key 的前缀，不带 value），然后当作没命中处理。限流在 Redis 不可用时放行，同样打日志。
- 启动时连不上 Redis，就当作没配置缓存，`/api/meta` 显示 `redis_available: false`。

## 6. 部署和配置

**docker-compose.yml** 新增服务：

```yaml
  redis:
    image: redis:7.4-alpine
    container_name: dataagent-redis
    command: ["redis-server", "--maxmemory", "256mb", "--maxmemory-policy", "allkeys-lru",
              "--save", "", "--appendonly", "no"]
    ports:
      - "6379:6379"
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      retries: 10
```

- `allkeys-lru`：内存满了，淘汰最久没用的 key。数据全是缓存，丢了也能重新算，所以关闭持久化。
- `app` 服务加上 `REDIS_URL: redis://redis:6379/0`，在 `depends_on` 里加上 redis。

**新增配置**（写入 `config.py` 和 `.env.example`）：

```
REDIS_URL=redis://127.0.0.1:6379/0
# 关闭后所有缓存都不生效，评测量“无缓存耗时”时用。
CACHE_ENABLED=true
SQL_CACHE_TTL_SECONDS=3600
EMBEDDING_CACHE_TTL_SECONDS=2592000
RERANK_CACHE_TTL_SECONDS=86400
RATE_LIMIT_PER_MINUTE=20
```

**依赖**：`requirements.txt` 加 `redis>=5.0,<6.0`；`requirements-dev.txt` 加 `fakeredis>=2.20,<3.0`，用来测 `RedisCache` 本身的代码。

## 7. 为什么不缓存回答，也不做语义缓存

- “上个月退款率”和“上个月退货率”向量非常接近，口径却不同；“华东上个月销售额”和“华南上个月销售额”只差一个筛选条件。语义缓存一旦命中错，用户拿到的就是**错误的数字**，这比慢更糟。
- 多轮追问依赖上下文，同一句“那上个月呢”在不同会话里意思不同，按问题文本做 key 不安全。
- 缓存 SQL 结果，就算是同一个问题，大模型照样生成 SQL，只是省掉查库这一步，**正确性不受影响**。
- 如果以后要缓存“完全相同的问题”，前提是：没有历史对话、同一个 `as_of_date`、同一种模式，并且在回答里标注“来自缓存”。这一项放在后续阶段评估。

## 8. 要改的文件

| 文件 | 改动 |
|---|---|
| `docker-compose.yml` | 新增 redis 服务；app 服务加 `REDIS_URL` 和依赖 |
| `.env.example`、`backend/app/config.py` | 新增第 6 节列出的配置项 |
| `backend/requirements.txt`、`requirements-dev.txt` | 加 `redis`、`fakeredis` |
| `backend/app/cache/__init__.py`、`base.py`、`redis_cache.py`、`memory_cache.py`（新） | `Cache` 协议、两种实现、`build_cache` |
| `backend/app/cache/rate_limit.py`（新） | 固定窗口限流 |
| `backend/app/nl2sql/executor.py` | `execute_sql` 增加可选参数 `cache`；`ExecutionResult` 增加 `cached` 字段 |
| `backend/app/agent/tools.py` | `AgentRunContext` 增加 `cache` 字段，`run_sql` 把它传给 `execute_sql`，日志标注命中 |
| `backend/app/agent/pipeline.py` | `AskPipeline` 接收 cache，放进 `AgentRunContext` |
| `backend/app/analysis/attribution.py` | 调用 `execute_sql` 时传入 cache |
| `backend/app/embedding/cached.py`（新）、`embedding/__init__.py` | `CachedEmbedder`，`build_embedder` 返回包装后的对象 |
| `backend/app/rerank/cached.py`（新）、`rerank/__init__.py` | `CachedReranker` |
| `backend/app/main.py` | lifespan 里调用 `build_cache`，挂到 `app.state.cache` |
| `backend/app/api/routes.py` | 限流；`/api/meta` 返回 Redis 状态和命中统计；保存 Key 后重建 embedder 时也要包上缓存 |
| `backend/app/semantic/cli.py` | 新增 `clear_cache` 命令，用于业务数据变化后手动清缓存 |
| `backend/tests/test_cache.py` 等（新） | 见第 9 节 |
| `CLAUDE.md` | 常用命令里加上 `clear_cache` |
| `docs/changes/2026-11-xx-redis-cache.md` | 做完后的改动记录 |

## 9. 测试和验证

**单元测试**（不需要真 Redis）：
- `MemoryCache` 和 `RedisCache`（用 fakeredis）跑同一套用例：读写、过期、批量读写、自增。
- Redis 抛异常时：`get` 返回没命中、`set` 不报错、限流放行，并且都打了 WARNING 日志。
- SQL 缓存：同一条 SQL 第二次不查库（用 mock 计数）；`as_of_date` 或 `max_rows` 变了就不命中；执行失败不写缓存；超过 1MB 不写缓存；**校验不通过的 SQL 不会去查缓存**。
- `CachedEmbedder`：5 条文本里有 2 条命中，只把 3 条交给底层模型，返回的顺序和输入一致。
- `CachedReranker`：候选文档换了顺序就不命中。
- 限流：第 21 次请求返回 429；跨过分钟窗口后恢复；`RATE_LIMIT_PER_MINUTE=0` 时不限流。

**集成验证**：
- `docker compose up -d --build` 之后访问 `/api/meta`，看到 `redis_available: true`。
- 同一个问题连问两次，第二次日志里出现“命中缓存”，`/api/meta` 的命中数增加。
- `docker compose stop redis` 之后再提问，问答正常，日志出现降级警告。

**效果度量**（写进改动文档，作为简历数据）：
- 设 `CACHE_ENABLED=false` 跑一遍 `eval/run_eval.py --mode all`，得到无缓存的基线。
- 设 `CACHE_ENABLED=true` 连跑两遍，对比第二遍的：SQL 和 embedding 命中率、单题耗时的 P50 和 P95、embedding 和 rerank 的 API 调用次数。
- 正确率必须和基线一致。缓存只改变“快不快”，不能改变“对不对”。

## 10. 风险

| 风险 | 应对 |
|---|---|
| 业务数据变了，缓存还是旧的 | key 里带 `as_of_date`；TTL 1 小时；提供 `clear_cache` 命令 |
| 评测的耗时数据被缓存“美化” | 评测默认要说明是否开了缓存；基线用 `CACHE_ENABLED=false` |
| Redis 不可用 | 超时很短 + 捕获异常 + 当作没命中，问答不受影响 |
| 结果集太大，占满内存 | 超过 1MB 不缓存；`maxmemory 256mb` + LRU 淘汰 |
| 缓存里有敏感数据 | 业务查询本来就只连脱敏后的 `safe` 库；Key 和密码绝不写入缓存；生产环境 Redis 不对外暴露端口，并设置密码 |
| 限流误伤（多人共用一个出口 IP） | 上限可配置，可以关闭；超限的提示里说明原因和等待时间 |

## 11. 后续阶段（不在本次范围内）

1. **会话上下文**：启用 MySQL 的 `conversations`、`messages` 表，最近 N 轮对话放 Redis，前端不用再每次带全部历史。这一项要改前端，单独评估。
2. **分布式锁**：bootstrap 和重建索引用 `SET NX EX` 加锁，等到多实例部署时再做。
3. **异步任务进度**：接入 Kafka 之后，worker 把每一步进度写进 Redis Stream，前端断开后可以续上。
4. **完全相同问题的回答缓存**：满足第 7 节的前提后再评估。
