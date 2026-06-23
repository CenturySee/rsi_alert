#!/usr/bin/env bash
# RSI 预警 — 服务器端一键部署脚本（在小主机上运行，不是 Windows 本机）
#
# 前置：已把 rsi-alert 目录 scp 到服务器，例如 /opt/quant/rsi-alert
#       （opentdx 是公开 PyPI 包，无需另外拷贝，脚本会让 uv 从 PyPI 装）
# 然后：cd /opt/quant/rsi-alert/deploy && chmod +x deploy.sh && ./deploy.sh
set -euo pipefail

# ---- 可调参数 -------------------------------------------------------------
APP_USER="${APP_USER:-$(whoami)}"           # 运行服务的用户
PORT="${PORT:-8000}"                        # uvicorn 监听端口（仅 127.0.0.1）
SERVICE_NAME="rsi-alert"
# --------------------------------------------------------------------------

# deploy/ 的上一级就是项目根
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> 项目目录: $APP_DIR"
echo "==> 运行用户: $APP_USER  端口: 127.0.0.1:$PORT"

# 1. 清理从 Windows 带过来的无用产物
echo "==> 清理 venv / 缓存"
rm -rf "$APP_DIR/.venv" "$APP_DIR/__pycache__" "$APP_DIR/.pytest_cache"

# 2. 去掉本地 opentdx 源覆盖，改从 PyPI 安装（opentdx 是公开包）
echo "==> 移除 pyproject.toml 中的本地 opentdx 源覆盖"
if grep -q 'C:/xzq/projects/github/opentdx' "$APP_DIR/pyproject.toml"; then
  sed -i '/opentdx = { path = .*editable = true }/d' "$APP_DIR/pyproject.toml"
  # 本地源锁定的 uv.lock 已失效，删掉让 uv 从 PyPI 重新解析
  rm -f "$APP_DIR/uv.lock"
  echo "   已移除本地源，opentdx 将从 PyPI 安装"
else
  echo "   未发现本地源覆盖，跳过（可能已改过）"
fi

# 3. 安装 uv（若没有）
if ! command -v uv >/dev/null 2>&1; then
  echo "==> 安装 uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
# 让本会话能找到 uv
export PATH="$HOME/.local/bin:$PATH"
UV_BIN="$(command -v uv)"
echo "==> uv: $UV_BIN"

# 4. 同步依赖 + 跑测试
echo "==> uv sync"
( cd "$APP_DIR" && "$UV_BIN" sync )
echo "==> 跑测试（失败不阻断部署）"
( cd "$APP_DIR" && "$UV_BIN" run pytest -q ) || echo "   测试未通过，请检查（继续部署）"

# 5. 写 systemd 服务
echo "==> 写 systemd 服务 /etc/systemd/system/$SERVICE_NAME.service"
sudo tee "/etc/systemd/system/$SERVICE_NAME.service" >/dev/null <<EOF
[Unit]
Description=RSI Alert (FastAPI)
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$UV_BIN run uvicorn main:app --host 127.0.0.1 --port $PORT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# 6. 启动
echo "==> 启动服务"
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_NAME"
sleep 2
sudo systemctl --no-pager status "$SERVICE_NAME" || true

echo
echo "==> 本地自检"
if curl -fs "127.0.0.1:$PORT" >/dev/null; then
  echo "    OK：127.0.0.1:$PORT 已响应"
else
  echo "    !! 未响应，看日志：journalctl -u $SERVICE_NAME -n 50 --no-pager"
fi

echo
echo "==> app 部署完成。接下来配置 Cloudflare Tunnel（见 部署指南.md 第 3 步）。"
