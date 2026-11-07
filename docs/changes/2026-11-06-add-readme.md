# 新增启动说明

日期：2026-11-06

## 做了什么

新增根目录 `README.md`，只写环境要求、首次启动、再次启动和服务检查。

没有修改代码、配置或脚本。

## 为什么做

用户要求给项目加一份 README，内容只写怎么启动。放在根目录，打开仓库就能看到。

README 把“构建向量索引”写成单独一步，因为使用默认模型预设时，`scripts/init_data.sh` 会跳过建索引。

## 有什么用

新环境可以按文档启动项目，也能避免再次运行初始化脚本时重置模拟数据。

## 如何验证

- `bash -n scripts/init_data.sh`：语法检查通过。
- `docker compose config -q`：配置校验通过。
- 端口和环境变量已对照 `docker-compose.yml`、`.env.example` 和 `vite.config.ts`。
- `build_index` 和 `docker compose up -d --build app` 在之前的工作中实际运行成功。

以下内容没有验证：

- 没有在全新环境中从头走完整流程，也没有运行 `init_data.sh`，它会重置现有模拟数据。
- 编写时后端没有响应 `/healthz`，本次没有确认服务正在运行。
- Node.js 18 是按 Vite 5 的要求写的，本机实际使用的是 Node.js 24。
