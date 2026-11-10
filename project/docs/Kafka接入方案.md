# Kafka 接入方案

日期：2026-11-10
状态：待确认（尚未改代码）

相关文档：`docs/Redis接入方案.md`。两者互不依赖，可以分开做。

## 1. 能解决哪些问题

### 1.1 要解决的问题

| 现在的问题 | Kafka 怎么解决 | 用户或项目得到什么 |
|---|---|---|
| 想知道线上的通过率、拦截率、耗时、模型调用量，只能临时写 SQL 去查 `query_logs`、`query_executions`、`call_traces` | 每次问答发事件，消费服务持续聚合成按分钟的指标表 | 随时能看到 Agent 的运行效果，不用每次手写统计 |
| 评测只能说明评测集上的表现，线上真实提问的效果没有数据 | 把“回答完成”和“用户反馈”两类事件按 `log_id` 关联 | 得到线上的点赞率、点踩率，和评测结果互相印证 |
| 每加一种统计或告警，都要改问答主流程的代码 | 新需求加一个消费者组，读同一份事件 | 主流程只发事件，后面怎么用和它无关 |
| 统计口径改了，历史数据要重新算 | 事件在 Kafka 里保留 7 天，把消费位置拨回去重新消费 | 改口径后能重算最近的指标 |
| 指标的统计查询和问答存档读写同一批表 | 指标写到单独的表，由单独的服务计算 | 统计查询不和问答主流程抢数据库 |

### 1.2 Kafka 解决不了的问题

- **回答不会变快，也不会变准**。事件是在回答完成之后发的。
- **问答存档仍然同步写库**。`query_logs` 的 `id`（即 `log_id`）要随回答返回给前端，用户反馈、`show_trace` 和评测都靠它定位一次问答，所以存档不能改成异步。现在存档写库的耗时已经记录在回答的 `usage.log_write_ms` 里。

### 1.3 如实说明

按现在的流量，用一个定时任务直接对 `query_logs` 做聚合，也能得到同样的指标，而且更简单。本方案选 Kafka 的主要原因是练习一条完整的数据链路：埋点、传输、消费、去重、聚合。`docs/优化方案/README.md` 里写的是“先看后端指标再决定是否接入”，本方案是为了练手提前做，所以设计上保证它随时可以关掉（`KAFKA_ENABLED=false`），关掉后问答功能和现在完全一样。

### 1.4 能练到的知识点

- 分区、分区 key 和同一会话内的事件顺序
- 消费者组、再均衡、手动提交 offset
- 至少一次投递加幂等消费
- 重试、死信 topic
- 生产端 `acks`、幂等生产者、发送失败的处理
- 消费延迟（lag）监控、重置 offset 重放
- 双写问题（数据库和 Kafka 都要写）以及补发的处理方式

## 2. 现状

| 位置 | 现在的做法 |
|---|---|
| `backend/app/agent/pipeline.py:767` | 回答完成后，同步写 `query_logs`、`call_traces`、`query_executions`，拿到 `log_id` |
| `backend/app/api/routes.py:165` `/feedback` | 用户点赞或点踩，写 `feedback` 表（每个 `log_id` 一条） |
| `query_logs.source` | 区分提问来自网页还是评测 |
| 指标统计 | 没有。评测有自己的分数表，线上问答没有汇总数据 |

## 3. 总体设计

```
 问答主流程（Python）                       指标消费服务（Java，新）
┌─────────────────────────┐            ┌──────────────────────────────┐
│ 1. 同步写存档，拿到 log_id │            │ 批量消费 → 去重写 qa_events     │
│ 2. 发事件（不等结果）      │──▶ Kafka ──▶│ 失败重试 3 次 → 死信 topic      │
│ /feedback 写库后发事件    │  qa.events  │ 每分钟聚合 → qa_metrics_minute │
└─────────────────────────┘            └──────────────────────────────┘
        │ 发送失败：记日志和计数，不影响回答            │
        └──── 补发命令：按 log_id 范围从存档重新发事件 ──┘
```

- **Python 只负责发事件**。通过适配层 `backend/app/events/` 访问 Kafka，业务代码只依赖 `EventPublisher` 协议，单元测试用内存实现。
- **Java 负责消费**。独立的 Spring Boot 服务，目录 `project/metrics-consumer/`，在 docker compose 里是一个单独的容器。
- **存档表是事实来源，事件是它的副本**。事件丢了可以从存档补发，所以发送端不需要做到绝对不丢。

## 4. 事件设计

### 4.1 Topic

| Topic | 分区数 | key | 保留时间 | 用途 |
|---|---|---|---|---|
| `dataagent.qa.events` | 3 | `session_id` | 7 天 | 全部问答事件 |
| `dataagent.qa.events.dlt` | 1 | 同原消息 | 7 天 | 重试后仍然失败的消息 |

- 用 `session_id` 做 key：同一个会话的事件进同一个分区，消费时顺序和发生时一致。
- 关闭自动建 topic，由消费服务启动时创建，避免写错名字时悄悄多出一个 topic。
- 单节点部署，副本数只能是 1。这是练手环境的限制，生产环境应该是 3 副本。

### 4.2 事件格式

所有事件用同一个外层结构，JSON 编码：

```json
{
  "event_id": "dataagent_app:1024:answer",
  "event_type": "answer.completed",
  "occurred_at": "2026-11-10T08:30:00.123Z",
  "instance": "dataagent_app",
  "source": "web",
  "session_id": "s-8f3a…",
  "log_id": 1024,
  "payload": {}
}
```

- `event_id` 由“实例 + log_id + 事件类型 + 序号”拼成，**同一件事永远得到同一个 id**。这样重复发送、补发、重复消费都能靠它去重。
- `instance` 取应用库名（`MYSQL_APP_DB`），区分主实例和 DABench 实例。
- `source` 取 `query_logs.source`，指标里把网页提问和评测提问分开统计，评测不会把线上指标冲掉。
- 字段定义写成一份 JSON Schema，放在 `project/events/qa-event.schema.json`，Python 和 Java 的测试都用它校验，避免两边字段对不上。

### 4.3 事件类型

| 事件类型 | 什么时候发 | `event_id` 末段 | payload 主要字段 |
|---|---|---|---|
| `answer.completed` | 存档写完后 | `answer` | 状态、模式（查询/分析）、耗时、重试次数、返回行数、模型名、输入和输出 token、是否降级、警告条数 |
| `sql.executed` | 同上，每条尝试过的 SQL 一个 | `sql:{seq}` | 状态（成功/被拦截/执行失败）、耗时、行数、`sql_hash` |
| `call.finished` | 同上，每次模型、工具、检索调用一个 | `call:{seq}` | 类别、名称、耗时、是否成功、错误类型、token |
| `feedback.given` | `/feedback` 写库后 | `feedback` | 评价（correct/wrong） |

**事件里不放问题原文、SQL 原文和查询结果**。指标用不到它们，放进去会让业务数据多一个存放位置。需要看原文时用 `log_id` 回查存档。

## 5. 生产端（Python）

- **适配层**：`EventPublisher` 协议只有 `publish(events)` 和 `close()`。`KafkaPublisher` 用 `confluent-kafka`；`MemoryPublisher` 给单元测试用；没开 Kafka 时用空实现。
- **发送时机**：存档写库的事务提交之后再发。先发后写的话，写库失败会留下没有对应存档的事件。
- **不阻塞回答**：`produce()` 只把消息放进客户端的内存队列，由后台线程发送。队列满了就丢弃这一批事件，打 WARNING 日志并计数。
- **可靠性配置**：`acks=all`、`enable.idempotence=true`（网络重试不会产生重复消息）、`delivery.timeout.ms=30000`。发送结果在回调里处理，失败的记日志和计数。
- **双写问题**：写库成功但发事件失败时，指标会缺这次问答。解决办法是补发命令 `python -m app.semantic.cli republish_events --from-log-id A --to-log-id B`，从存档重新生成事件。因为 `event_id` 是确定的，重复补发不会重复计数。
  - 另一种做法是发件箱模式（事件和存档在同一个事务里写进一张表，再由后台任务转发到 Kafka），能保证不丢，但要多一张表和一个转发任务。指标允许有延迟，也能补发，所以先不用。
- **关闭服务时**：`flush()` 最多等 5 秒，把队列里的事件发完。
- **可观测**：`/api/meta` 增加 `kafka_enabled`、已发送数、发送失败数、丢弃数。

## 6. 消费端（Java）

- **技术选型**：Java 21、Spring Boot 3、Spring Kafka、Maven。数据库访问用 `JdbcTemplate`，表少、SQL 简单，不需要 ORM。
- **消费方式**：消费者组 `dataagent-metrics`，批量监听，一批最多 200 条。`enable.auto.commit=false`，一批全部写库成功后才提交 offset。
- **幂等**：`qa_events.event_id` 上有唯一键，用 `INSERT IGNORE` 写入。消息重复到达时，第二次写入不产生新行。**至少一次投递加幂等写入，效果等同于只处理一次**。
- **失败处理**：
  - 数据库暂时不可用等可恢复的错误：间隔 1 秒、2 秒、4 秒重试 3 次。
  - 重试后仍然失败，或消息格式不对（JSON 解析失败、缺必填字段）：转发到死信 topic，带上原 topic、分区、offset 和异常信息，然后继续消费后面的消息，不卡住整个分区。
- **聚合**：每分钟执行一次，重算最近 10 分钟每个分钟桶的指标，用 `INSERT … ON DUPLICATE KEY UPDATE` 覆盖写入。
  - 重算一个窗口而不是只算最新一分钟，是为了处理迟到的事件（例如补发的、重试后才写入的）。
  - 覆盖写入是幂等的，任务重复执行或服务重启都不会算错。
  - 超过 10 分钟才到的事件（主要是补发），由补发命令结束后调用消费服务的重算接口，按时间范围重算。
- **可观测**：Spring Boot Actuator 暴露健康检查和消费延迟；日志里每批打印条数、重复条数、耗时。

## 7. 数据表

两张新表都建在应用库里，写入 `data/initdb/02_app_schema.sql`，已有的数据库由管理命令补建。

**`qa_events`（事件明细）**

| 列 | 说明 |
|---|---|
| `event_id` | 唯一键，去重用 |
| `event_type`、`instance`、`source`、`session_id`、`log_id` | 取自事件外层 |
| `occurred_at` | 事件发生时间，聚合按它分桶 |
| `payload` | JSON |
| `kafka_partition`、`kafka_offset` | 排查问题时能对回 Kafka 里的位置 |
| `created_at` | 写入时间。它和 `occurred_at` 的差就是链路延迟 |

明细保留 30 天，由消费服务每天清理一次。

**`qa_metrics_minute`（分钟指标）**

主键：`instance` + `source` + `bucket_start`（分钟）。

| 指标 | 来自哪类事件 |
|---|---|
| 回答数、各状态的数量 | `answer.completed` |
| 一次通过数（重试次数为 0 且成功） | `answer.completed` |
| 平均耗时、P95 耗时 | `answer.completed`（MySQL 没有分位数函数，由 Java 取出该分钟的耗时后计算） |
| 输入 token、输出 token | `answer.completed` |
| SQL 执行数、被拦截数、执行失败数 | `sql.executed` |
| 外部调用数、失败数（按超时/429/5xx 分） | `call.finished` |
| 点赞数、点踩数 | `feedback.given` |

**数据库账号**：沿用项目现有的做法，新增一个只给消费服务用的账号 `dataagent_metrics`，只能读写这两张表，密码写在 `project/.env`，由管理命令 `grant_metrics_user` 授权。

## 8. 降级和容错

| 情况 | 表现 |
|---|---|
| `KAFKA_ENABLED=false`（默认） | 不创建生产者，问答和现在完全一样 |
| 启动时连不上 Kafka | 打 WARNING，按没开 Kafka 运行，`/api/meta` 显示 `kafka_enabled: false` |
| 运行中 Kafka 不可用 | 事件在客户端队列里等待，超时或队列满后丢弃并计数；问答不受影响；恢复后用补发命令补上 |
| 消费服务停了 | 事件留在 Kafka 里，服务恢复后从上次提交的 offset 继续，7 天内不丢 |
| 消费服务处理到一半崩溃 | offset 没提交，重启后这批消息重新消费，靠 `event_id` 去重 |
| 遇到无法处理的消息 | 进死信 topic，不影响后面的消息 |

## 9. 部署和配置

**docker-compose.yml** 新增两个服务，都放在 `kafka` profile 下，默认不启动：

```yaml
  kafka:
    image: apache/kafka:3.9.0
    profiles: ["kafka"]
    environment:
      KAFKA_NODE_ID: 1
      KAFKA_PROCESS_ROLES: broker,controller
      KAFKA_LISTENERS: PLAINTEXT://:9092,CONTROLLER://:9093
      KAFKA_ADVERTISED_LISTENERS: PLAINTEXT://kafka:9092
      KAFKA_CONTROLLER_LISTENER_NAMES: CONTROLLER
      KAFKA_LISTENER_SECURITY_PROTOCOL_MAP: CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT
      KAFKA_CONTROLLER_QUORUM_VOTERS: 1@kafka:9093
      KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR: 1
      KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR: 1
      KAFKA_TRANSACTION_STATE_LOG_MIN_ISR: 1
      KAFKA_AUTO_CREATE_TOPICS_ENABLE: "false"
      KAFKA_LOG_RETENTION_HOURS: 168
      KAFKA_HEAP_OPTS: "-Xmx384m -Xms384m"

  metrics-consumer:
    build: ./metrics-consumer
    profiles: ["kafka"]
    depends_on: [kafka, mysql]
```

- 用 KRaft 模式，不需要 ZooKeeper。
- 这段配置**还没有实际跑过**，镜像版本和环境变量以实现时跑通的为准。
- 内存：Kafka 加消费服务大约多占 1 到 1.5 GB，所以放在 profile 里，不用时不启动。

**新增配置**（写入 `config.py` 和 `.env.example`）：

```
KAFKA_ENABLED=false
KAFKA_BOOTSTRAP_SERVERS=kafka:9092
KAFKA_EVENTS_TOPIC=dataagent.qa.events
MYSQL_METRICS_USER=dataagent_metrics
MYSQL_METRICS_PASSWORD=
```

**依赖**：`backend/requirements.txt` 加 `confluent-kafka`。

**DABench 实例**：第一阶段不发事件（`KAFKA_ENABLED=false`）。事件里已经带了 `instance`，以后要接入时不用改事件格式。

## 10. 分三步做

每一步都能独立运行和测试，做完一步再决定要不要做下一步。

| 步骤 | 内容 | 练到什么 |
|---|---|---|
| 1. 跑通链路 | compose 加 Kafka；Python 只发 `answer.completed`；Java 消费后写 `qa_events` | 分区 key、消费者组、手动提交 offset |
| 2. 做可靠 | 补全另外三类事件；幂等去重、重试、死信 topic；生产端降级和计数；补发命令 | 至少一次加幂等、死信、双写问题 |
| 3. 做指标 | 分钟聚合、`qa_metrics_minute`、重算接口、清理任务 | 迟到数据、幂等聚合、offset 重放 |

## 11. 要改的文件

| 文件 | 改动 | 步骤 |
|---|---|---|
| `docker-compose.yml` | 新增 kafka、metrics-consumer 两个服务（`kafka` profile）；app 服务加 Kafka 相关环境变量 | 1 |
| `.env.example`、`backend/app/config.py` | 新增第 9 节列出的配置项 | 1 |
| `backend/requirements.txt` | 加 `confluent-kafka` | 1 |
| `events/qa-event.schema.json`（新） | 事件的 JSON Schema | 1 |
| `backend/app/events/__init__.py`、`base.py`、`kafka_publisher.py`、`memory_publisher.py`、`builders.py`（新） | `EventPublisher` 协议、两种实现、`build_publisher`、从存档记录生成事件的函数 | 1、2 |
| `backend/app/main.py` | lifespan 里创建和关闭发布器 | 1 |
| `backend/app/agent/pipeline.py` | 存档写完后发事件 | 1、2 |
| `backend/app/api/routes.py` | `/feedback` 写库后发事件；`/api/meta` 返回 Kafka 状态和发送计数 | 2 |
| `backend/app/semantic/cli.py` | 新增 `republish_events`、`grant_metrics_user` 命令 | 2 |
| `backend/app/db/accounts.py` | 新增指标账号的授权 | 1 |
| `data/initdb/02_app_schema.sql`、`data/initdb/03_grants.sql` | 新增两张表和指标账号 | 1、3 |
| `metrics-consumer/`（新，Java 项目） | `pom.xml`、`Dockerfile`、消费者、去重写入、错误处理和死信、聚合任务、重算接口、测试 | 1 到 3 |
| `backend/tests/test_events.py` 等（新） | 见第 12 节 | 1、2 |
| `CLAUDE.md` | 常用命令里加上启动 Kafka、消费服务的测试命令、补发和授权命令 | 1、2 |
| `docs/changes/2026-11-xx-kafka-events.md` | 做完后的改动记录 | 每步完成后 |

## 12. 测试和验证

**Python 单元测试**（不需要真 Kafka）：
- 从一次问答的存档记录生成事件：数量、类型、`event_id` 都符合预期；同样的输入两次生成的 `event_id` 相同。
- 生成的事件通过 JSON Schema 校验；事件里没有问题原文、SQL 原文和结果行。
- 存档写库失败时不发事件。
- 发布器抛异常或队列满时：回答正常返回，打了 WARNING，计数增加。
- `KAFKA_ENABLED=false` 时不创建生产者。

**Java 测试**：
- 单元测试：事件解析（含缺字段、未知事件类型）、指标计算（含 P95）。
- 集成测试（内嵌 Kafka 加 Testcontainers 的 MySQL，需要本机有 Docker）：
  - 同一条消息发两次，`qa_events` 只有一行。
  - 发一条格式错误的消息，它进了死信 topic，后面的正常消息照常入库。
  - 写库失败时 offset 不提交，恢复后消息被重新消费。
  - 聚合任务跑两次，结果相同；迟到的事件在下一次聚合后计入。
  - 用仓库里的 JSON Schema 校验测试用的事件样例。

**集成验证**：
- `docker compose --profile kafka up -d --build` 后提一个问题，`qa_events` 出现对应的行，一分钟内 `qa_metrics_minute` 出现对应的分钟桶。
- 点一次赞，指标里的点赞数加一。
- 停掉消费服务，提 3 个问题，再启动，3 次问答的事件全部入库，没有重复。
- 停掉 Kafka 再提问，问答正常，`/api/meta` 的失败数或丢弃数增加；恢复后跑补发命令，指标补齐。
- 对同一段 `log_id` 跑两次补发命令，指标不变。

**效果度量**（写进改动文档）：
- 开和关 Kafka 各跑一次评测，对比单题耗时，确认发事件没有拖慢问答。
- 评测并发为 4 时，链路延迟（`created_at` 减 `occurred_at`）的 P50 和 P95，以及消费延迟的最大值。
- 用 `qa_metrics_minute` 汇总出的回答数、成功数，和直接查 `query_logs` 得到的数字核对，两者必须一致。

## 13. 风险

| 风险 | 应对 |
|---|---|
| 发事件拖慢问答 | 只入内存队列，不等发送结果；用评测对比开关前后的耗时 |
| 事件丢失导致指标偏低 | 存档是事实来源；补发命令；定期用 `query_logs` 核对总数 |
| 重复消费导致指标偏高 | `event_id` 唯一键去重；聚合用覆盖写入 |
| Python 和 Java 的事件字段不一致 | 同一份 JSON Schema，两边的测试都校验 |
| 仓库多一种语言，维护成本上升 | 消费服务只做消费和聚合，不放业务逻辑；放在 profile 里，不影响默认的启动方式 |
| 本机内存不够 | Kafka 堆内存限制在 384MB；不用时不启动 `kafka` profile |
| 事件里带出业务数据 | 事件不放问题原文、SQL 原文和结果行，有测试保证 |
| 单节点 Kafka 磁盘损坏会丢数据 | 练手环境可以接受；丢了可以从存档补发 |

## 14. 后续阶段（不在本次范围内）

1. **让 Agent 查自己的指标**：给 `qa_metrics_minute` 建视图并写入语义层，就能用中文问“昨天的回答成功率是多少”。要先评估运行指标和业务数据放在一起会不会干扰选表。
2. **告警**：再加一个消费者组，失败率或外部调用错误率超过阈值时通知。
3. **流计算**：把定时聚合换成 Kafka Streams 或 Flink 的窗口聚合。
4. **业务数据流**：模拟订单和用户行为事件，经 Kafka 进业务库。它和现在按 `as_of_date` 固定数据的设计有冲突，改动大，需要单独设计。
