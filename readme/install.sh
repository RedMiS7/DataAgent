#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_DIR="$ROOT_DIR/project"
VENV_DIR="$ROOT_DIR/.venv"

info()  { printf '\033[32m[INFO]\033[0m %s\n' "$1"; }
warn()  { printf '\033[33m[WARN]\033[0m %s\n' "$1"; }
fail()  { printf '\033[31m[FAIL]\033[0m %s\n' "$1" >&2; exit 1; }

version_ge() {
  local current="${1#v}" required="$2"
  local IFS='.'
  local -a cur=($current) req=($required)
  local i
  for i in 0 1 2; do
    local c="${cur[$i]:-0}" r="${req[$i]:-0}"
    if (( c > r )); then return 0; fi
    if (( c < r )); then return 1; fi
  done
  return 0
}

info "检查运行环境"

command -v python3 >/dev/null 2>&1 || fail "未找到 python3，请安装 Python 3.11+"
PY_VER="$(python3 -c 'import sys;print(".".join(map(str,sys.version_info[:3])))')"
version_ge "$PY_VER" "3.11" || fail "Python 版本过低：$PY_VER，需要 3.11+"
info "Python $PY_VER"

if command -v node >/dev/null 2>&1; then
  NODE_VER="$(node -v)"
  version_ge "$NODE_VER" "20.0.0" || fail "Node 版本过低：$NODE_VER，需要 20+"
  info "Node $NODE_VER"
else
  warn "未找到 node，跳过前端依赖安装"
  NODE_VER=""
fi

if command -v docker >/dev/null 2>&1; then
  docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 不可用"
  info "Docker $(docker --version)"
else
  warn "未找到 docker，需自行准备 MySQL / Redis / Qdrant"
fi

info "创建 Python 虚拟环境：$VENV_DIR"
[ -d "$VENV_DIR" ] || python3 -m venv "$VENV_DIR"
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip >/dev/null

if [ -f "$PROJECT_DIR/backend/requirements.txt" ]; then
  info "安装后端依赖"
  pip install -r "$PROJECT_DIR/backend/requirements.txt"
else
  warn "未找到 backend/requirements.txt，跳过后端依赖安装"
fi

if [ -n "$NODE_VER" ] && [ -f "$PROJECT_DIR/frontend/package.json" ]; then
  info "安装前端依赖"
  (cd "$PROJECT_DIR/frontend" && npm ci)
elif [ -n "$NODE_VER" ]; then
  warn "未找到 frontend/package.json，跳过前端依赖安装"
fi

if [ ! -f "$PROJECT_DIR/.env" ] && [ -f "$PROJECT_DIR/.env.example" ]; then
  cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
  warn "已生成 project/.env，请填写大模型密钥与数据库口令后重新执行"
fi

if command -v docker >/dev/null 2>&1 && [ -f "$PROJECT_DIR/docker-compose.yml" ]; then
  info "启动中间件（MySQL / Qdrant）"
  (cd "$PROJECT_DIR" && docker compose up -d)
  info "等待中间件就绪…"
  sleep 5
  (cd "$PROJECT_DIR" && docker compose ps)
else
  warn "跳过中间件启动，请确认 MySQL / Qdrant 可用"
fi

cat <<'EOF'

安装完成。下一步：
  1. 编辑 project/.env 填写密钥
  2. 初始化数据与向量索引
  3. 启动后端与前端
EOF