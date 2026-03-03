#!/usr/bin/env bash
# ============================================================
# IB Bot 一键部署脚本（Ubuntu，git 工作流）
#
# 用法（VPS 上首次部署）:
#   git clone <repo> /opt/ib-bot
#   cd /opt/ib-bot
#   bash deploy/setup.sh
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"   # 即 git clone 的根目录

# ── 可修改变量 ────────────────────────────────────────────────
IBC_DIR="/opt/ibc"
GATEWAY_DIR="/opt/ibgateway"
IBC_VERSION="3.23.0"     # 见 https://github.com/IbcAlpha/IBC/releases
BOT_PORT="8001"

echo "=============================="
echo " IB Bot 部署开始"
echo " BOT_DIR = $BOT_DIR"
echo "=============================="

# ── 1. 系统依赖 ──────────────────────────────────────────────
echo ""
echo ">>> [1/4] 安装系统依赖..."
apt-get update -q || { echo "    警告: apt-get update 有仓库错误（已忽略），继续安装..."; true; }
apt-get install -y -q \
    xvfb \
    default-jre-headless \
    python3 \
    python3-venv \
    python3-pip \
    curl \
    unzip
echo "    完成"

# ── 2. 安装 IB Gateway ───────────────────────────────────────
echo ""
echo ">>> [2/4] 安装 IB Gateway..."
GATEWAY_INSTALLER="/tmp/ibgateway-installer.sh"
if [ ! -f "$GATEWAY_INSTALLER" ]; then
    echo "    下载中..."
    curl -fsSL \
        "https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone/ibgateway-stable-standalone-linux-x64.sh" \
        -o "$GATEWAY_INSTALLER"
fi
chmod +x "$GATEWAY_INSTALLER"
mkdir -p "$GATEWAY_DIR"
"$GATEWAY_INSTALLER" -q -dir "$GATEWAY_DIR" -Dinstall4j.createDesktopIcon=0 || true
echo "    已安装至 $GATEWAY_DIR"

# ── 3. 安装 IBC ──────────────────────────────────────────────
echo ""
echo ">>> [3/4] 安装 IBC v${IBC_VERSION}..."
IBC_ZIP="/tmp/ibc-${IBC_VERSION}.zip"
if [ ! -f "$IBC_ZIP" ]; then
    curl -fsSL \
        "https://github.com/IbcAlpha/IBC/releases/download/${IBC_VERSION}/IBC-${IBC_VERSION}.zip" \
        -o "$IBC_ZIP"
fi
mkdir -p "$IBC_DIR"
unzip -q -o "$IBC_ZIP" -d "$IBC_DIR"
find "$IBC_DIR" -name "*.sh" -exec chmod +x {} \;

# 仅首次复制配置模板
if [ ! -f "$IBC_DIR/config.ini" ]; then
    cp "$SCRIPT_DIR/ibc.ini" "$IBC_DIR/config.ini"
    echo "    !! 请编辑 $IBC_DIR/config.ini 填写 IB 账号密码 !!"
else
    echo "    已有 config.ini，跳过"
fi

# ── 4. Python 虚拟环境 ───────────────────────────────────────
echo ""
echo ">>> [4/4] 创建 Python 虚拟环境..."
python3 -m venv "$BOT_DIR/venv"
"$BOT_DIR/venv/bin/pip" install --upgrade pip -q
"$BOT_DIR/venv/bin/pip" install -r "$BOT_DIR/requirements.txt" -q
echo "    完成"

# ── 安装 systemd 服务 ────────────────────────────────────────
echo ""
echo ">>> 安装 systemd 服务..."

sed \
    -e "s|__BOT_DIR__|$BOT_DIR|g" \
    -e "s|__IBC_DIR__|$IBC_DIR|g" \
    -e "s|__GATEWAY_DIR__|$GATEWAY_DIR|g" \
    "$SCRIPT_DIR/ibgateway.service" > /etc/systemd/system/ibgateway.service

sed \
    -e "s|__BOT_DIR__|$BOT_DIR|g" \
    -e "s|__BOT_PORT__|$BOT_PORT|g" \
    "$SCRIPT_DIR/ib-bot.service" > /etc/systemd/system/ib-bot.service

cp "$SCRIPT_DIR/start_gateway.sh" /usr/local/bin/start_ibgateway.sh
chmod +x /usr/local/bin/start_ibgateway.sh
sed -i \
    -e "s|__IBC_DIR__|$IBC_DIR|g" \
    -e "s|__GATEWAY_DIR__|$GATEWAY_DIR|g" \
    /usr/local/bin/start_ibgateway.sh

systemctl daemon-reload
systemctl enable ibgateway ib-bot

# ── 完成 ─────────────────────────────────────────────────────
echo ""
echo "=============================="
echo " 部署完成！"
echo "=============================="
echo ""
echo "后续步骤:"
echo "  1. 填写 IB 账号:  nano $IBC_DIR/config.ini"
echo "  2. 启动 Gateway:  systemctl start ibgateway"
echo "  3. 等待登录:      sleep 40"
echo "  4. 启动 bot:      systemctl start ib-bot"
echo ""
echo "后续更新（VPS 上执行）:"
echo "  cd $BOT_DIR && git pull && systemctl restart ib-bot"
