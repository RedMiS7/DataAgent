#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="$(cd "$PROJECT_DIR/.." && pwd)"
VENV="$REPO_DIR/.venv"

info() { printf '[INFO] %s\n' "$1"; }
warn() { printf '[WARN] %s\n' "$1"; }
fail() { printf '[FAIL] %s\n' "$1" >&2; exit 1; }

cd "$PROJECT_DIR"

if [ ! -f .env ]; then
  cp .env.example .env
  warn "已生成 project/.env，请填写大模型和数据库口令后重新执行"
fi

command -v docker >/dev/null || fail "未找到 docker"
command -v python3 >/dev/null || fail "未找到 python3"

info "启动 MySQL 与 Qdrant"
docker compose up -d mysql qdrant

info "等待 MySQL 健康检查通过"
for _ in $(seq 1 60); do
  status="$(docker inspect -f '{{.State.Health.Status}}' dataagent-mysql 2>/dev/null || echo unknown)"
  [ "$status" = "healthy" ] && break
  sleep 2
done
[ "$status" = "healthy" ] || fail "MySQL 未就绪，请查看 docker compose logs mysql"

if [ ! -d "$VENV" ]; then
  info "创建虚拟环境"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q -r backend/requirements.txt
fi

info "生成模拟数据"
"$VENV/bin/python" data/generate.py --orders "${ORDERS:-200000}"

info "导入语义层"
(cd backend && PYTHONPATH=. "$VENV/bin/python" -m app.semantic.cli init_semantic)

set -a
# shellcheck disable=SC1091
source .env
set +a

if [ "${EMBEDDING_PROVIDER:-}" = "fake" ] || { [ -n "${EMBEDDING_ENDPOINT:-}" ] && [ -n "${EMBEDDING_MODEL:-}" ]; }; then
  info "构建向量索引"
  (cd backend && PYTHONPATH=. "$VENV/bin/python" -m app.semantic.cli build_index)
else
  warn "未配置 EMBEDDING_ENDPOINT/EMBEDDING_MODEL，跳过向量索引；"
  warn "问答会退回应用库关键词匹配，仍可运行。"
  warn "如需离线构建测试索引：EMBEDDING_PROVIDER=fake bash scripts/init_data.sh"
fi

info "完成。启动后端：docker compose up -d app"
info "前端：cd frontend && npm install && npm run dev  -> http://localhost:5173"