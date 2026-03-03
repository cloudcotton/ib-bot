# OKX Exit Bot — 项目文档

## 目录

1. [项目概述](#1-项目概述)
2. [功能说明](#2-功能说明)
3. [架构设计](#3-架构设计)
4. [核心策略逻辑](#4-核心策略逻辑)
5. [模块说明](#5-模块说明)
6. [配置说明](#6-配置说明)
7. [部署指南](#7-部署指南)
8. [注意事项与已知限制](#8-注意事项与已知限制)

---

## 1. 项目概述

**OKX Exit Bot** 是一个基于 K 线技术分析的自动平仓机器人，部署于 OKX 合约交易平台。

程序通过 WebSocket 订阅实时 K 线行情，依据「三根 K 线突破」策略，在价格突破前两根历史 K 线的高低点时自动平仓，并通过 Telegram 发送通知。

**核心能力：**

- 同时监控最多 5 个合约，每个合约可独立配置 K 线周期
- 支持双向持仓（long_short）和单向持仓（net）两种模式
- WebSocket 断线自动重连，重连后重新拉取历史数据填充缓冲区
- 可选 Telegram 平仓通知，未配置时静默跳过
- 支持模拟盘 / 实盘切换
- 日志同时输出到控制台和 `bot.log` 文件（时间戳统一为 UTC+8）

---

## 2. 功能说明

### 2.1 平仓信号

| 信号 | 触发条件 |
|------|----------|
| **平多** | 当前价格 ≤ Kn.low **且** 当前价格 < min(K-1.low, K-2.low) |
| **平空** | 当前价格 ≥ Kn.high **且** 当前价格 > max(K-1.high, K-2.high) |

- **Kn**：当前正在成形的 K 线（未完成）
- **K-1**：最近一根已完成 K 线
- **K-2**：再前一根已完成 K 线

### 2.2 防重复触发机制

两层保护防止同一信号被重复执行：

1. **防重入锁**：`_in_flight` 集合，key 为 `(inst_id, signal_type)`。同一合约同一方向的平仓请求正在处理中时，后续信号直接跳过。
2. **冷却计时**：`_signal_cooldown` 字典，同一 `(inst_id, signal)` 组合触发后，30 秒内不再重复调用，避免因 OKX API 频率限制被封。

### 2.3 Telegram 通知

平仓成功后，通过 Telegram Bot 发送消息，包含：合约名称、平仓方向、可用手数、时间。

未配置 `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` 时，通知模块静默跳过，不影响主流程。

---

## 3. 架构设计

### 3.1 整体数据流

```
OKX WebSocket (business 端点)
        │
        │  每次 tick 推送 K 线原始数据
        ▼
   OKXWebSocketClient._handle_message()
        │
        │  调用回调
        ▼
   on_candle(channel, inst_id, raw)       ← main.py 工厂函数生成
        │
        ├─ KlineManager.update()          ← 更新 K 线缓冲区
        │
        ├─ check_signal()                 ← 策略判断
        │
        └─ execute_close()                ← 查询持仓 → 平仓 → Telegram 通知
                │
                ├─ OKXRestClient.get_positions()
                ├─ OKXRestClient.close_position()
                └─ TelegramNotifier.send()
```

### 3.2 启动初始化流程

```
main()
  │
  ├─ load_config()                      读取 config.yaml + .env
  │
  ├─ OKXRestClient / KlineManager / TelegramNotifier 实例化
  │
  ├─ init_kline_buffers()               REST API 拉取历史 K 线（每个合约 5 根）
  │   └─ 过滤 confirm==1 的已完成 K 线，填充 completed deque
  │
  └─ OKXWebSocketClient.run()           永久运行，断线自动重连
      └─ 重连时 on_reconnect()           清空旧缓冲区，重新调用 init_kline_buffers()
```

### 3.3 模块依赖关系

```
main.py
  ├── config.py          配置加载
  ├── kline_manager.py   K 线缓冲区（被 main.py 和 strategy.py 共同使用）
  ├── strategy.py        信号生成（只读 kline_manager）
  ├── okx_rest.py        REST API 调用
  ├── ws_client.py       WebSocket 客户端
  └── notifier.py        Telegram 通知
```

---

## 4. 核心策略逻辑

### 4.1 设计意图

该策略本质是「三根 K 线突破确认」：

- **条件一**（`price <= Kn.low`）：确保当前 tick 正在创下成形 K 线的新低，而非价格在已有下影线内正常波动后已回升。OKX WebSocket 推送中，成形 K 线的 `close` 字段就是当前最新价，当 `close == low` 时即说明此刻正在触及新低。
- **条件二**（`price < min(K-1.low, K-2.low)`）：确保这个新低真正有效突破了前两根已完成 K 线的支撑区域，而不仅仅是 Kn 内部的波动。

平空逻辑完全对称（高点突破 → 平空）。

### 4.2 K 线缓冲区（KlineBuffer）

```
completed: deque(maxlen=2)
    [0] = K-2  (较早的已完成 K 线)
    [1] = K-1  (最新的已完成 K 线)

current: Kline 或 None
    = Kn (正在成形的 K 线，每个 tick 更新)
```

**状态转换规则：**

- WebSocket 推送 `confirm == "0"`：更新 `current`（Kn）
- WebSocket 推送 `confirm == "1"`：将完成 K 线追加到 `completed`，`current` 置 None
- `ready()` 条件：`len(completed) >= 2`，即至少有 K-1 和 K-2 才能开始信号判断

**Kn 缺失的边界情况：**

当一根 K 线恰好完成（`current` 被置 None）后，下一根 Kn 还未开始推送时，`get_signal_data()` 以当前价格作为临时 `kn_low = kn_high`，此时条件一和条件二不会同时成立，不会误触发。

---

## 5. 模块说明

### 5.1 `config.py` — 配置加载

- 读取 `config.yaml` 中的合约列表和持仓模式
- 读取 `.env` 中的 API 密钥和 Telegram 配置
- 将用户友好的 `timeframe`（如 `60m`、`1h`、`1H`）统一映射到 OKX API 参数（`bar` 和 `channel`）
- 最多支持 5 个合约，超出时抛出 `ValueError`

**timeframe 支持列表：**

| 用户配置 | OKX bar | WebSocket channel |
|---------|---------|-------------------|
| 5m      | 5m      | candle5m          |
| 15m     | 15m     | candle15m         |
| 30m     | 30m     | candle30m         |
| 60m / 1h / 1H | 1H | candle1H       |
| 4h / 4H | 4H     | candle4H          |
| 1d / 1D | 1D     | candle1D          |

### 5.2 `kline_manager.py` — K 线缓冲区

- `KlineBuffer`：单个 `(合约, 周期)` 的缓冲区，维护 completed deque 和 current
- `KlineManager`：字典 `{(inst_id, channel): KlineBuffer}`，统一管理所有合约

### 5.3 `strategy.py` — 信号逻辑

纯函数，无副作用，只读取 `KlineManager` 的数据，返回 `CLOSE_LONG`、`CLOSE_SHORT` 或 `None`。

### 5.4 `okx_rest.py` — REST API

- **鉴权**：HMAC-SHA256 签名，消息格式 = `timestamp + method + path + body`，Base64 编码
- **模拟盘**：请求头添加 `x-simulated-trading: 1`
- **接口清单：**
  - `GET /api/v5/account/positions`（需鉴权）：查询当前持仓
  - `POST /api/v5/trade/close-position`（需鉴权）：平仓
  - `GET /api/v5/market/candles`（公开）：获取历史 K 线（初始化用）

> **注意**：OKX 历史 K 线接口返回顺序为「最新在前」，代码中已做 `reversed()` 转换为时间正序。

### 5.5 `ws_client.py` — WebSocket 客户端

- 连接端点：`wss://ws.okx.com:8443/ws/v5/business`（**business** 端点，支持 1H 及以上周期；public 端点不支持）
- 心跳：每 20 秒发送一次 ping（OKX 要求 ≤30 秒）
- 重连策略：指数退避，延迟序列为 `[3, 5, 10, 30, 60]` 秒，连接成功后重置计数
- 重连后调用 `on_reconnect` 回调，触发缓冲区重建

### 5.6 `notifier.py` — Telegram 通知

- 使用 Telegram Bot API 的 `sendMessage` 接口，支持 HTML 格式
- 请求超时 10 秒，失败时只打印 WARNING，不影响主流程
- `enabled` 属性：`bot_token` 和 `chat_id` 同时非空时为 `True`

---

## 6. 配置说明

### 6.1 `config.yaml`

```yaml
contracts:
  - instId: "BTC-USDT-SWAP"    # OKX 合约 ID，永续合约以 -SWAP 结尾
    timeframe: "15m"            # K 线周期

pos_side_mode: "long_short"    # 双向: long_short | 单向: net
```

> 最多配置 5 个合约。

### 6.2 `.env`

```env
# OKX API（必填）
OKX_API_KEY=your_api_key
OKX_SECRET_KEY=your_secret_key
OKX_PASSPHRASE=your_passphrase
OKX_DEMO=false              # true = 模拟盘，false = 实盘

# Telegram（选填，不填则跳过通知）
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

---

## 7. 部署指南

### 7.1 快速启动（Ubuntu）

```bash
# 1. 克隆仓库到服务器
git clone <repo-url> ~/okx-exit-bot
cd ~/okx-exit-bot

# 2. 配置密钥
cp .env.example .env
nano .env          # 填写 API 密钥

# 3. 修改合约配置
nano config.yaml

# 4. 启动（自动创建 venv 并安装依赖）
bash start.sh
```

### 7.2 Systemd 服务（推荐生产环境）

```bash
# 修改服务文件中的用户名和路径
nano okx-exit-bot.service

# 安装服务
sudo cp okx-exit-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable okx-exit-bot   # 开机自启
sudo systemctl start okx-exit-bot

# 常用命令
sudo systemctl status okx-exit-bot
sudo journalctl -u okx-exit-bot -f   # 实时日志
```

### 7.3 依赖环境

- Python 3.10+
- 依赖包：`websockets`、`aiohttp`、`python-dotenv`、`pyyaml`

---

## 8. 注意事项与已知限制

### 8.1 策略层面

- **无开仓功能**：本程序只负责平仓，不会主动建立持仓。需配合外部开仓策略使用。
- **冷却 30 秒**：同一合约同一方向触发后，30 秒内不会再次执行平仓。若价格在 30 秒内持续满足条件，不会重复平仓（通常符合预期）。
- **`ready()` 前不触发**：启动初始化若历史 K 线拉取失败（接口异常或无数据），该合约的缓冲区不会 ready，信号检查直接跳过，不会误操作。

### 8.2 API 层面

- **WebSocket 端点**：必须使用 **business** 端点，`candle1H`、`candle4H`、`candle1D` 等高周期频道在 public 端点无效，会返回错误事件。
- **API 频率限制**：平仓接口有频率限制，冷却机制可避免短时间大量调用，但极端情况下仍需关注 OKX 的错误返回。
- **持仓模式一致性**：`pos_side_mode` 必须与 OKX 账户的实际持仓模式一致。双向模式下使用 `posSide=long/short`，单向模式下使用 `posSide=net`，若不匹配平仓请求会失败。

### 8.3 运行层面

- **合约数量上限**：最多 5 个合约，由 `config.py` 在加载时强制检查。
- **重连时缓冲区清空**：WebSocket 重连后会清空所有缓冲区并重新拉取历史 K 线，重连期间不会错过已完成的 K 线（通过 REST API 补回）。
- **日志文件**：`bot.log` 与程序同目录，长期运行需定期清理或配置日志轮转（logrotate）。
- **模拟盘与实盘**：通过 `.env` 中 `OKX_DEMO=true/false` 切换，模拟盘请求头携带 `x-simulated-trading: 1`，使用同一套代码无需修改。
