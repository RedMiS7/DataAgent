# 简化启动流程

日期：2026-11-06

## 做了什么

把启动简化成一条命令 `docker compose up -d --build`，API Key 改到网页填写，数据库初始化自动完成。

后端：

- 新增 `backend/app/bootstrap.py`：后台依次连接数据库、同步语义层、准备模拟数据、建立索引，并提供进度；校验 Key。
- `data/generate.py` 移到 `backend/app/demo_data.py`：改用 `Settings`，全部数据在一个事务里写入，已有订单时拒绝生成，去掉 `TRUNCATE`。
- `backend/app/config.py`：读写本地 Key 文件 `.env.local`，网页保存的 Key 优先于 `.env`；数据库密码增加默认值。
- `backend/app/api/routes.py`、`schemas.py`：新增 `/api/setup/status`、`/api/setup/keys`、`/api/setup/retry`。
- `backend/app/main.py`：启动时运行初始化，托管前端页面。
- `backend/app/agent/scope_model.py`、`semantic/cli.py`：共用模型探测函数。

部署：

- `backend/Dockerfile`：多阶段构建，把前端打包进镜像，并带上语义种子文件。
- `docker-compose.yml`：`.env` 改为可选，数据库密码有默认值，新增 `app_config` 数据卷。
- 新增 `.dockerignore`，`.env.example` 增加说明。
- 删除 `scripts/init_data.sh`。

前端：

- 新增 `pages/SetupPage.tsx`、`pages/SettingsPage.tsx`、`components/KeysForm.tsx`、`hooks/useSetup.ts`、`lib/setup.ts`。
- 修改 `App.tsx`、`api.ts`、`types.ts`、`Sidebar.tsx`、`styles.css`。

文档和测试：更新 `README.md`、`CLAUDE.md`、差距分析文档；新增 `backend/tests/test_bootstrap.py`、`frontend/src/lib/__tests__/setup.test.ts`。

## 为什么做

用户要求简化流程：API Key 放到前端填写，数据库初始化不要单独运行命令。这个项目只在本地使用，所以没有修改端口绑定，也没有增加登录。

Key 保存到本地配置文件，不放数据库。这样和原来的 `.env` 安全级别相同，数据库备份里不会带上 Key，命令行工具也能读取。文件内容是明文，权限为 600。

数据只在订单表为空时生成，并且改成单个事务，这样不会清空已有数据，失败时也不会留下半份数据。

## 有什么用

新环境只需要安装 Docker，运行一条命令，打开网页填两个 Key 就能开始提问，不再需要手动创建 `.env`，也不用运行初始化脚本或 npm。

## 如何验证

- 后端测试：89 个通过，1 个模块默认跳过（集成测试需要 `RUN_INTEGRATION=1`）。
- `ruff check backend eval`：通过。`docker compose config -q`：通过。
- 前端：`npm test` 32 个通过，`npm run build` 通过。
- 从零验证：复制项目到临时目录，去掉 `.env`，使用全新数据卷和其他端口。
  - 构建加启动约 148 秒，没有 `.env` 也能启动。
  - 没填 Key 时自动完成建表、语义同步和 20 万订单生成，约 30 秒完成，索引步骤等待 Key。
  - 无效 Key 返回 400，不保存，错误里没有 Key。
  - 通过接口提交真实 Key 后约 14 秒就绪，索引自动建好；页面自动进入聊天，真实提问得到正确结论。
  - 接口和容器日志里没有 Key，文件权限为 600。
  - 重建容器后 Key 还在，数据和索引没有重建。
- 现有环境升级：`docker compose up -d --build` 后订单 200000、日志 57、案例 6 都没变，索引判定为最新。
- 本地 `uvicorn` 启动：初始化状态正常就绪。

以下内容没有验证：

- 真实 Key 是通过接口提交的，没有在浏览器输入框里实际输入并保存。表单错误提示是用模拟的 400 响应验证的。
- 初始化失败时的页面和“重新准备”按钮只有单元测试，没有在浏览器里看过。
- 全新环境的 `python3 -m venv` 和 `npm run dev` 这次没有重新运行。
- 按用户要求，没有运行 `eval/run_eval.py`。
- `readme/install.sh` 没有修改，内容仍是旧流程。
