# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

永远使用中文回复。
本文件是 AI 编程助手在本项目工作时必须遵守的规则。规则冲突时，以用户在当前对话中的明确指示为准。
代码在 project文件夹中进行
## 项目概况

DataAgent 是面向小电商卖家和个体经营者的经营数据助手。用户用中文提问，系统生成并执行 SQL，返回表格、图表、一句话结论和口径说明。

## 必须遵守的工作流程

### 1. 修改代码前先询问用户

修改以下内容前，必须先说明计划，并等待用户明确同意：

- 源代码、测试代码和脚本
- 配置文件、依赖和部署文件
- 数据库结构、迁移脚本和种子数据
- 提示词和评测集

询问时说明：

- 要改哪些文件
- 打算怎么改，以及为什么这样改
- 可能带来的风险或影响

用户明确说“可以”“开始”“按这个改”等，才算同意。同意只对当次说明的范围有效；超出范围时，必须重新询问。

阅读代码、分析问题和运行只读命令不需要询问。

### 2. 较大的独立任务生成改动文档

完成较大、有独立性的任务后，在 `project/docs/changes/` 下新建一份文档，例如重构代码、新增功能。一次任务写一份，不必每个文件写一份。

小改动不写文档，例如修改说明文档、调整规则、改文案、修小问题。

文件名格式：`YYYY-MM-DD-简短说明.md`，例如 `2026-11-05-chat-ui-rewrite.md`。

文档使用以下模板：

```markdown
# 改动标题

日期：YYYY-MM-DD

## 做了什么

说明具体改动，并列出涉及的文件。

## 为什么做

说明要解决的问题，以及选择这个方案的原因。

## 有什么用

说明改动后的效果，以及用户或项目因此获得的价值。

## 如何验证

说明运行过的命令和结果。没有验证的内容要明确写出。
```

文档必须如实记录，不能把未运行的测试写成已通过。


## 常用命令

添加启动、测试、lint 和评测命令后，要在这里写明实际可运行的命令，不要写未验证的命令。

- 启动全部服务：`cd project && docker compose up -d --build`，然后打开 http://localhost:8000
- 数据库账号：服务运行时用应用账号 `dataagent_app`（`MYSQL_APP_USER`）读写应用库，用 `shop_ro` 查业务视图；root（`MYSQL_ADMIN_USER`）只给管理命令用。新建的数据库由 `data/initdb/03_grants.sql` 建好应用账号；已有的数据库或改了 `MYSQL_APP_PASSWORD` 后，先用管理账号补一次再重启服务：`cd project && docker compose run --rm --no-deps -T app python -m app.semantic.cli grant_app_user`；DABench 实例用 `docker compose --profile dabench run --rm --no-deps -T app-dabench python -m app.semantic.cli grant_app_user`
- 后端测试：`cd project/backend && PYTHONPATH=. ../../.venv/bin/python -m pytest -q`
- 后端 lint：`cd project && ../.venv/bin/ruff check backend eval data`
- 前端测试：`cd project/frontend && npm test`
- 前端构建：`cd project/frontend && npm run build`
- 模型连通检查：`cd project/backend && PYTHONPATH=. ../../.venv/bin/python -m app.semantic.cli check_models`
- 重建向量索引：`cd project/backend && PYTHONPATH=. ../../.venv/bin/python -m app.semantic.cli build_index`
- 查看一次问答的模型和工具调用：`cd project/backend && PYTHONPATH=. ../../.venv/bin/python -m app.semantic.cli show_trace --log-id <query_logs 的 id>`
- 评测：`cd project && ../.venv/bin/python eval/run_eval.py --mode all`，默认覆盖 `eval/results/latest.json`，可用 `--output` 改路径。按 `MYSQL_BUSINESS_DB` 只跑对应数据集的题，分数按"数据集/题型"分组，没有总分；`review.status=suspect` 的题照常运行但不计分
- 路由评测（只测自动模式的判断，需要大模型）：`cd project && ../.venv/bin/python eval/run_eval.py --mode routing --output <路径>`
- 分析评测（只跑分析题，统计维度覆盖和耗时，需要大模型）：`cd project && ../.venv/bin/python eval/run_eval.py --mode analysis --output <路径>`；加 `--no-plan` 用原来的单 Agent 流程对比
- 只跑部分类别的问答题：`--mode questions --category consistency --category multiturn`；加 `--no-history-sql` 时多轮题的历史不带上一轮的 SQL，用来对比
- 检索召回率评测（只测检索能不能找全一道题需要的表，不调用大模型，需要向量库）：`cd project && docker compose run --rm --no-deps -T -v "$PWD":/work -w /work app python eval/run_eval.py --mode retrieval --output <路径>`
- 导入数据集到 MySQL（按清单建库、建表、导数据、建视图、授权，例如 DABench；数据文件先放到清单写的目录）：`cd project && docker compose run --rm --no-deps -T -v "$PWD":/work -w /work/backend app python -m app.semantic.cli import_dataset --manifest ../data/dabench/manifest.json`
- 启动 DABench 实例（8001 端口，独立的业务库、应用库和向量集合，启动时自动同步语义层、建索引）：`cd project && docker compose --profile dabench up -d app-dabench`
- 用网页里保存的模型 key 跑评测（key 在 Docker 卷里，本机 `.env` 没有 key 时用这个）：先 `cd project && docker compose up -d --build`，再 `docker compose run --rm --no-deps -T -v "$PWD":/work -w /work app python eval/run_eval.py --mode all --output <路径>`

## 编码规范

- 优先写简单、清楚的代码，不为尚未需要的功能增加抽象。
- 修改范围只覆盖当前任务，不顺手重构无关代码。
- 做功能是要写Test，Test完一遍没问题后再告诉User
- Python 代码使用类型标注；对外接口使用明确的请求和响应模型。
- 外部服务通过适配层访问，便于替换和测试，包括大模型、向量库和 Redis。
- 错误信息要能定位问题，不能吞掉异常。

## 安全规则
- 不提交密钥和真实个人数据；配置示例写入 `.env.example`。

## Git

- 只有用户要求时才提交或推送。
- 不改写已有历史，不执行破坏性命令。
