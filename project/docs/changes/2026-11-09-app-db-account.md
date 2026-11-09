# 应用库连接改用专用账号，不再用 root

日期：2026-11-09

## 做了什么

服务运行时连接数据库不再使用 root。新增应用账号 `dataagent_app`，root 只留给管理命令。

| 账号 | 谁用 | 权限 |
| --- | --- | --- |
| `shop_ro`（`MYSQL_RO_USER`） | Agent 查业务数据，不变 | 各数据集视图库只读 |
| `dataagent_app`（`MYSQL_APP_USER`，新增） | 服务运行时读写应用库；首次启动生成演示订单 | 应用库：SELECT、INSERT、UPDATE、DELETE、CREATE、ALTER、INDEX；`shop` 库：SELECT、INSERT（只有开了演示数据的实例才授权） |
| root（`MYSQL_ADMIN_USER`） | 只给 `import_dataset`、`grant_app_user` 这类管理命令用 | 全部 |

应用账号能建表和加字段，是因为启动时要给旧数据库补 `call_traces` 表和 `is_entity` 字段。它不能删表，也不能给别的账号授权。

涉及的文件：
- `backend/app/config.py`：新增 `mysql_app_user` 和 `mysql_app_password`；`app_db_url` 改用应用账号。`mysql_admin_*` 只用于 `admin_server_url`。
- `backend/app/db/accounts.py`（新增）：应用账号的权限定义。`ensure_app_user` 负责建账号或重设密码，然后授权，可以重复执行。密码作为绑定参数传入，不拼进 SQL；账号名和库名只允许小写字母、数字和下划线。
- `backend/app/db/engine.py`：`ping` 增加 `raise_denied` 参数。遇到账号被拒（MySQL 1044、1045）时直接抛出 `DatabaseAccessDenied`，不再当作"暂时连不上"去等待。
- `backend/app/bootstrap.py`：启动时如果应用账号登录不了，立即报错，并写明要先运行 `grant_app_user`。以前会空等 120 秒，然后报"连不上 MySQL"。
- `backend/app/demo_data.py`：生成演示数据改用应用账号；库名提取为常量 `DEMO_DATABASE`。
- `backend/app/importer.py`：`import_dataset` 导入数据集时，同时给应用账号授权这个数据集的应用库（例如 `dataagent_dabench`）。
- `backend/app/semantic/cli.py`：
  - 新增 `grant_app_user` 命令：用管理账号建好应用账号，并授权当前实例的应用库；开了演示数据时，再授权 `shop` 库。
  - `import_dataset` 现在把账号名不合法、密码为空也作为导入失败报告。
- `data/initdb/03_grants.sql`：新建的数据库在初始化时直接创建应用账号，权限和代码里一致。
- `.env.example`、`.env.dabench.example`：增加 `MYSQL_APP_USER` 和 `MYSQL_APP_PASSWORD`，注明 `MYSQL_ADMIN_*` 只给管理命令用。
- `docker-compose.yml`：`app` 和 `app-dabench` 两个服务传入 `MYSQL_APP_PASSWORD`。
- `backend/tests/test_db_accounts.py`（新增），以及 `backend/tests/test_bootstrap.py`：离线替身的 `ping` 改为接受新参数。
- `CLAUDE.md`：写明各个账号的分工，以及已有数据库要先运行的 `grant_app_user` 命令。

## 为什么做

`.env.example` 里 `MYSQL_ADMIN_USER=root`，服务的应用库连接和演示数据生成用的都是 root。服务运行时只需要读写应用库，但拿着的是能删任何库、能给任何账号授权的权限。一旦服务被利用，或者代码有缺陷，影响范围就是整个 MySQL。

做法上，保留现有的 `MYSQL_ADMIN_*` 变量给管理命令使用，另加一个最小权限的运行时账号，而不是把 `MYSQL_ADMIN_USER` 直接改成普通账号。原因是导入数据集要建库、删表、授权，确实需要高权限。两类用途分开，各自的权限都清楚。

## 有什么用

- 服务运行时连接 MySQL，用的是只能操作自己应用库的账号。即使出问题，也删不了业务库、改不了权限。
- 应用账号配置错误时，启动页面立即提示该运行哪条命令，不用再空等两分钟去猜原因。
- 导入新数据集后，对应的实例可以直接用应用账号启动，不需要手动授权。
- 为优化方案里的评测专用账号（C1）提供了现成的写法：权限定义、可重复执行的授权命令、测试方式都可以照用。

## 如何验证

- `cd project/backend && PYTHONPATH=. ../../.venv/bin/python -m pytest -q`：469 passed，1 skipped（改动前是 454 passed，新增 15 个测试）。
- `cd project && ../.venv/bin/ruff check backend eval data`：All checks passed。
- `docker compose config --quiet`：通过。
- 本机迁移（已有的 MySQL 数据卷，初始化脚本不会再执行）：
  1. `docker compose --profile dabench build app app-dabench`：构建成功。
  2. `docker compose run --rm --no-deps -T app python -m app.semantic.cli grant_app_user`：授权 `dataagent` 和 `shop`。
  3. `docker compose --profile dabench run --rm --no-deps -T app-dabench python -m app.semantic.cli grant_app_user`：授权 `dataagent_dabench`，这个实例关了演示数据，所以没有授权 `shop`。
  4. 用 root 执行 `SHOW GRANTS FOR dataagent_app@'%'`：权限和上表一致，没有全局权限，只有 `USAGE`。
  5. `docker compose --profile dabench up -d app app-dabench` 重启后，两个实例的初始化步骤全部完成（database、semantic、demo_data、index）。
  6. 8000 端口问"上个月的GMV是多少？"，状态 ok，结果 9,072,501.5，问答记录写入 `dataagent.query_logs`（id 1273）。8001 端口问"2023年2月的销售额是多少？"，状态 ok，写入 `dataagent_dabench.query_logs`。
  7. 查 `information_schema.processlist`：两个应用容器（172.19.0.4、172.19.0.5）只用 `dataagent_app` 连应用库、用 `shop_ro` 连业务视图。root 只出现在我自己用来核对的本地会话里。

**没有验证的内容**
- 全新安装：数据卷为空、由 `03_grants.sql` 创建账号的流程，没有在本机实际跑过，只有测试核对了脚本里的权限和代码一致。
- 演示数据的写入：本机 `shop` 已经有数据，启动时只执行了查询（`has_orders`），没有实际用应用账号插入演示订单。

**遗留问题**
- `.env` 会整个注入应用容器，所以 root 密码仍然出现在容器的环境变量里，只是服务不再使用。要彻底拿掉，需要把 root 密码拆到只给 MySQL 和管理命令使用的单独文件里。
- 默认密码 `dataagent_app_pwd` 写在初始化脚本里，和 `shop_ro` 一样，只适合本机开发。正式部署时要把脚本和 `.env` 一起改。
