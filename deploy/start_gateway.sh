#!/usr/bin/env bash
# ============================================================
# 启动 IB Gateway（Xvfb 无头模式 + IBC 自动登录）
# 由 ibgateway.service 调用，不建议手动运行
# ============================================================
set -uo pipefail

IBC_DIR="__IBC_DIR__"           # 由 setup.sh 替换
GATEWAY_DIR="__GATEWAY_DIR__"   # 由 setup.sh 替换
DISPLAY_NUM=":99"
LOG_FILE="/var/log/ibgateway.log"

# ── 清理函数：退出时杀掉 Xvfb ───────────────────────────────
cleanup() {
    echo "[$(date '+%F %T')] 清理 Xvfb $DISPLAY_NUM..." | tee -a "$LOG_FILE"
    pkill -f "Xvfb $DISPLAY_NUM" 2>/dev/null || true
}
trap cleanup EXIT

# ── 确保只有一个 Xvfb 实例 ──────────────────────────────────
pkill -f "Xvfb $DISPLAY_NUM" 2>/dev/null || true
sleep 1

# ── 启动虚拟显示器 ───────────────────────────────────────────
echo "[$(date '+%F %T')] 启动 Xvfb $DISPLAY_NUM..." | tee -a "$LOG_FILE"
Xvfb "$DISPLAY_NUM" -screen 0 1024x768x16 &
XVFB_PID=$!
sleep 2

if ! kill -0 "$XVFB_PID" 2>/dev/null; then
    echo "[$(date '+%F %T')] [ERROR] Xvfb 启动失败" | tee -a "$LOG_FILE"
    exit 1
fi
echo "[$(date '+%F %T')] Xvfb PID=$XVFB_PID" | tee -a "$LOG_FILE"

# ── 检测 IB Gateway 主版本号 ─────────────────────────────────
# install4j 安装后，版本号写在 .install4j/response.varfile 或可从目录名推断
detect_gateway_version() {
    # 方法1: 从 response.varfile 读取
    local varfile="$GATEWAY_DIR/.install4j/response.varfile"
    if [ -f "$varfile" ]; then
        local ver
        ver=$(grep -oE 'sys.version=[0-9]+\.[0-9]+' "$varfile" 2>/dev/null \
              | grep -oE '[0-9]+\.[0-9]+' | head -1)
        if [ -n "$ver" ]; then
            # 转换 10.30 -> 1030
            echo "$ver" | tr -d '.'
            return
        fi
    fi
    # 方法2: 从 ibgateway 脚本中提取
    if [ -f "$GATEWAY_DIR/ibgateway" ]; then
        local ver
        ver=$(grep -oE '[0-9]{4}' "$GATEWAY_DIR/ibgateway" | head -1)
        if [ -n "$ver" ]; then
            echo "$ver"
            return
        fi
    fi
    # 方法3: 默认值（若上述均失败请手动修改）
    echo "1030"
}

GATEWAY_VERSION=$(detect_gateway_version)
echo "[$(date '+%F %T')] IB Gateway 主版本: $GATEWAY_VERSION" | tee -a "$LOG_FILE"

# ── 启动 IB Gateway（via IBC）───────────────────────────────
export DISPLAY="$DISPLAY_NUM"

echo "[$(date '+%F %T')] 启动 IBC + IB Gateway..." | tee -a "$LOG_FILE"
exec "$IBC_DIR/scripts/ibcstart.sh" "$GATEWAY_VERSION" \
    --gateway \
    --tws-path="$GATEWAY_DIR" \
    --ibc-ini="$IBC_DIR/config.ini" \
    >> "$LOG_FILE" 2>&1
