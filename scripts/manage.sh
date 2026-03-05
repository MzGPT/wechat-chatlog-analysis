#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="$ROOT_DIR/.venv"
REQ_FILE="$ROOT_DIR/requirements.txt"
ENV_FILE="$ROOT_DIR/.env"
PID_FILE="$ROOT_DIR/.uvicorn.pid"
LOG_FILE="$ROOT_DIR/uvicorn.log"

APP_IMPORT="app.main:app"

color() { printf "\033[%sm%s\033[0m\n" "$1" "$2"; }
info() { color "36" "$1"; }
ok() { color "32" "$1"; }
warn() { color "33" "$1"; }
err() { color "31" "$1"; }

ensure_env() {
  if [[ ! -f "$ENV_FILE" ]]; then
    warn "未找到 .env，使用 .env.example 作为模板"
    cp "$ROOT_DIR/.env.example" "$ENV_FILE" || true
  fi
}

ensure_venv() {
  # 项目移动目录后，旧的 venv shebang 可能指向旧路径导致 "bad interpreter"
  if [[ -d "$VENV_DIR" ]]; then
    if [[ ! -x "$VENV_DIR/bin/python" && ! -x "$VENV_DIR/bin/python3" ]]; then
      warn "检测到虚拟环境已损坏（缺少 python 可执行文件），将重建: $VENV_DIR"
      rm -rf "$VENV_DIR"
    else
      # pip 可能存在但 shebang 已失效；用 python -m pip 做一次自检
      if ! ("$VENV_DIR/bin/python" -m pip --version >/dev/null 2>&1 || "$VENV_DIR/bin/python3" -m pip --version >/dev/null 2>&1); then
        warn "检测到虚拟环境 pip 不可用（可能路径已变更），将重建: $VENV_DIR"
        rm -rf "$VENV_DIR"
      fi
    fi
  fi
  if [[ ! -d "$VENV_DIR" ]]; then
    info "创建虚拟环境: $VENV_DIR"
    python3 -m venv "$VENV_DIR"
  fi
  info "升级 pip/setuptools/wheel"
  "$VENV_DIR/bin/pip" install -U pip setuptools wheel >/dev/null
  info "安装依赖: $REQ_FILE"
  "$VENV_DIR/bin/pip" install -r "$REQ_FILE"
}

# 在网络受限或本地已装好的场景，允许跳过安装
maybe_ensure_venv() {
  if [[ -d "$VENV_DIR" && "${NO_INSTALL:-}" == "1" ]]; then
    warn "跳过依赖安装 (NO_INSTALL=1)"
    return 0
  fi
  ensure_venv
}

export_env() {
  # 将 .env 中的变量导出到当前 shell
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
}

is_running() {
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid=$(cat "$PID_FILE" || true)
    if [[ -n "${pid}" ]] && ps -p "$pid" >/dev/null 2>&1; then
      return 0
    fi
  fi
  return 1
}

start_bg() {
  ensure_env
  maybe_ensure_venv
  export_env
  if is_running; then
    ok "服务已在运行 (PID: $(cat "$PID_FILE"))"
    exit 0
  fi
  local host port
  host=${HOST:-127.0.0.1}
  port=${PORT:-8000}
  info "以后台方式启动: http://$host:$port"
  cd "$ROOT_DIR"
  local pybin
  pybin="$VENV_DIR/bin/python"
  if [[ ! -x "$pybin" ]]; then
    pybin="$VENV_DIR/bin/python3"
  fi
  nohup "$pybin" -m uvicorn "$APP_IMPORT" --host "$host" --port "$port" >"$LOG_FILE" 2>&1 < /dev/null &
  echo $! > "$PID_FILE"
  sleep 1
  if ps -p "$(cat "$PID_FILE")" >/dev/null 2>&1; then
    ok "已启动 (PID: $(cat "$PID_FILE"))，日志: $LOG_FILE"
  else
    err "启动失败，请查看日志: $LOG_FILE"
    tail -n 80 "$LOG_FILE" || true
    return 1
  fi
}

start_fg() {
  ensure_env
  maybe_ensure_venv
  export_env
  local host port
  host=${HOST:-127.0.0.1}
  port=${PORT:-8000}
  info "以前台方式启动: Ctrl+C 退出"
  cd "$ROOT_DIR"
  "$VENV_DIR/bin/uvicorn" "$APP_IMPORT" --host "$host" --port "$port"
}

stop_svc() {
  local host port
  host=${HOST:-127.0.0.1}
  port=${PORT:-8000}
  if is_running; then
    local pid
    pid=$(cat "$PID_FILE")
    info "停止服务 (PID: $pid)"
    kill "$pid" || true
    sleep 1
    if ps -p "$pid" >/dev/null 2>&1; then
      warn "进程未退出，发送 SIGKILL"
      kill -9 "$pid" || true
    fi
    rm -f "$PID_FILE"
    ok "已停止"
    return 0
  fi
  # fallback: 通过端口查找
  local pid2
  pid2=$(lsof -nPiTCP:$port -sTCP:LISTEN -t 2>/dev/null || true)
  if [[ -n "$pid2" ]]; then
    warn "发现占用端口 $port 的进程 (PID: $pid2)，尝试结束"
    kill "$pid2" || true
    sleep 1
    if ps -p "$pid2" >/dev/null 2>&1; then
      warn "发送 SIGKILL"
      kill -9 "$pid2" || true
    fi
    ok "已释放端口 $port"
  else
    warn "服务未在运行"
  fi
}

status_svc() {
  if is_running; then
    ok "运行中 (PID: $(cat "$PID_FILE"))"
  else
    warn "未运行"
  fi
}

logs_svc() {
  local follow=${1:-}
  [[ -f "$LOG_FILE" ]] || { warn "暂无日志"; return 0; }
  if [[ "$follow" == "-f" ]]; then
    tail -n 200 -f "$LOG_FILE"
  else
    tail -n 200 "$LOG_FILE"
  fi
}

sync_once() {
  export_env || true
  local port
  port=${PORT:-8000}
  info "触发一次拉取同步: /api/sync/chatlog"
  curl -fsS -X POST "http://127.0.0.1:$port/api/sync/chatlog" || true
  echo
}

sync_full() {
  export_env || true
  local port days
  port=${PORT:-8000}
  days=${1:-30}
  info "触发全量近${days}天: /api/sync/chatlog/full"
  curl -fsS -X POST "http://127.0.0.1:$port/api/sync/chatlog/full?days=$days" || true
  echo
}

usage() {
  cat <<USAGE
用法: bash scripts/manage.sh <命令>

命令：
  install        创建虚拟环境并安装依赖
  start          后台启动服务（nohup + PID 文件）
  run            前台启动服务（阻塞，Ctrl+C 退出）
  dev            前台启动（--reload 热重载，建议开发环境使用；可配合 NO_INSTALL=1）
  stop           停止后台服务
  status         查看服务状态
  logs [-f]      查看日志（-f 持续跟随）
  sync           触发一次从 chatlog 拉取增量
  emailsync [id] 同步邮箱（可选账户ID，省略则同步全部已启用账户）

示例：
  bash scripts/manage.sh install
  bash scripts/manage.sh start
  bash scripts/manage.sh status
  bash scripts/manage.sh logs -f
  bash scripts/manage.sh sync
  bash scripts/manage.sh emailsync 1
  bash scripts/manage.sh stop
USAGE
}

cmd=${1:-}
case "$cmd" in
  install) ensure_env; ensure_venv ;;
  start) start_bg ;;
  run) start_fg ;;
  dev) ensure_env; maybe_ensure_venv; export_env; cd "$ROOT_DIR"; "$VENV_DIR/bin/uvicorn" "$APP_IMPORT" --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}" --reload ;;
  stop) stop_svc ;;
  status) status_svc ;;
  logs) shift || true; logs_svc "${1:-}" ;;
  restart) stop_svc; start_bg ;;
  sync) sync_once ;;
  emailsync)
    export_env || true
    id=${2:-}
    port=${PORT:-8000}
    if [[ -n "$id" ]]; then
      info "同步邮箱账户 #$id"
      curl -fsS -X POST "http://127.0.0.1:$port/api/email/accounts/$id/sync" || true
      echo
    else
      info "同步全部邮箱账户（逐个尝试）"
      ids=$(curl -fsS "http://127.0.0.1:$port/api/email/accounts" | python3 - <<'PY'
import sys, json
data = json.load(sys.stdin)
print(" ".join(str(i.get('id')) for i in data))
PY
      )
      for i in $ids; do
        curl -fsS -X POST "http://127.0.0.1:$port/api/email/accounts/$i/sync" || true
        echo
      done
    fi
    ;;
  syncfull) shift || true; sync_full "${1:-30}" ;;
  *) usage ;;
esac
