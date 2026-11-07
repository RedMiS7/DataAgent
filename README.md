# DataAgent

## 启动

需要先安装并打开 Docker。

```bash
cd project
docker compose up -d --build
```

打开 <http://localhost:8000>。

第一次使用时，在页面上填写 DeepSeek 和阿里云百炼的 API Key。数据库、模拟数据和检索索引会自动准备，完成后直接进入问答页面。之后想修改 Key，到侧栏的“模型设置”。

需要空闲端口：`3306`、`6333`、`6334`、`8000`。

## 本地运行后端

开发时可以不用后端容器，改代码后自动重启。MySQL 和 Qdrant 仍由 Docker 提供。

首次运行前创建虚拟环境：

```bash
python3 -m venv .venv
.venv/bin/pip install -r project/backend/requirements.txt
```

启动后端：

```bash
cd project
docker compose stop app
docker compose up -d mysql qdrant
cd backend
PYTHONPATH=. ../../.venv/bin/uvicorn app.main:app --reload --port 8000
```

新开一个终端，启动前端：

```bash
cd project/frontend
npm install
npm run dev
```

打开 <http://localhost:5173>。需要 Python 3.12 和 Node.js 18 或更高版本。
