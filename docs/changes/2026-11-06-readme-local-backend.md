# README 补充本地运行后端

日期：2026-11-06

## 做了什么

在根目录 `README.md` 新增“本地运行后端”一节：MySQL 和 Qdrant 继续用 Docker，后端用 `uvicorn --reload` 在本机运行。

没有修改代码、配置或脚本。

## 为什么做

用户指出 README 只有 Docker 部署方式，并选择补充本地运行后端。完全不用 Docker 的方式这次不做。

## 有什么用

开发后端时不用每次重建镜像，改代码后服务会自动重启。

## 如何验证

按 README 的命令实际运行：

- `docker compose stop app` 和 `docker compose up -d mysql qdrant`：成功，MySQL 健康检查通过。
- `uvicorn app.main:app --reload --port 8000`：成功启动。
- `/healthz` 返回 `"status":"ok"` 和 `"business_db":true`。
- `/api/meta` 显示大模型、embedding、Qdrant 和业务库都可用。
- `/api/cases` 正常返回 6 条案例。

以下内容没有验证：

- 没有实际修改代码来触发自动重启。
- 这次没有通过本地后端完成真实问答，也没有和前端联调。
- 没有在全新环境中验证。
