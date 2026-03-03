# okx_ws — 项目总结文档

> 更新日期：2026-03-03

---

## 一、项目概述

**okx_ws** 是基于 OKX WebSocket 的永续合约自动交易机器人，核心策略为"前K高/低突破开仓"（Breakout）。本项目是早期 REST 轮询版 okx-bot 的全面改造版，以 WebSocket 事件驱动替代轮询，并内置了 FastAPI Web 控制台供实时监控与参数热更新。

**运行方式**

```bash
pip install -r requirements.txt
cp .env.example .env   # 填写 OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
# 打开 http://127.0.0.1:8000/ 进入 Web 控制台
```

---

## 二、技术栈

| 层次 | 技术 |
|------|------|
| Web 框架 | FastAPI + uvicorn |
| WebSocket 客户端 | websocket-client |
| OKX REST 调用 | requests（自签名） |
| 配置与验证 | PyYAML + pydantic v2 |
| 前端 | 原生 HTML / JS / CSS（无框架） |
| Python 版本 | 3.11+（使用 zoneinfo） |

---

## 三、项目结构

```
okx_ws/
├── app/                    # FastAPI 应用入口与 API 路由
│   ├── main.py             # 应用工厂、依赖注入、启动顺序
│   ├── api.py              # 核心业务 REST 接口
│   ├── manual.py           # 手动交易路由
│   └── debug.py            # 调试接口（WS 快照等）
├── core/                   # 核心业务逻辑
│   ├── strategy.py         # BreakoutBot 策略主类（~2500 行）
│   ├── risk.py             # 风控中心 RiskCenter
│   ├── notifier.py         # 通知系统（Telegram / webhook）
│   ├── settings.py         # 配置加载与持久化
│   ├── logging.py          # 日志初始化（按天轮转）
│   └── utils.py            # 工具函数（period_alias、round_tick 等）
├── adapters/
│   └── okx_client.py       # OKX REST V5 适配器（签名、下单、查询）
├── md/                     # 行情数据层
│   ├── ws.py               # WebSocket 行情订阅器（tickers + trades）
│   ├── agg.py              # K线聚合器（逐笔成交 → OHLC）
│   ├── rest.py             # REST 历史行情（EMA 预热用）
│   └── base.py             # 行情接口基类
├── web/
│   └── schemas.py          # API 参数 Pydantic 模型（UIParams）
├── webui/                  # 静态前端
│   ├── index.html          # 单页控制台
│   ├── app.js              # 状态轮询、表单交互、手动交易
│   └── styles.css          # 样式
├── config.yaml             # 运行参数（所有可热更新项）
├── .env.example            # API 密钥模板
└── requirements.txt        # Python 依赖
```

---

## 四、核心模块详解

### 4.1 行情层（md/）

**WSMarketData**（`md/ws.py`）
- 订阅 OKX 公共 WebSocket，同时拿 `tickers`（最优价）和 `trades`（逐笔成交）
- 内置断线自动重连（3 秒重试循环）
- 将逐笔成交交给 `CandleAggregator` 聚合

**CandleAggregator**（`md/agg.py`）
- 纯内存 K 线聚合，支持任意周期（1m / 5m / 15m / 1H / 4H / 1D …）
- `on_trade()` 逐笔更新当前 bucket；跨 bucket 时自动 emit 收盘回调
- `flush_due()` 由独立线程每 0.5 秒扫一次，防止末尾 bucket 因无成交卡住
- 提供 `live_ohlc()` 供策略随时读取当前未收盘 K 线的实时 OHLC

### 4.2 OKX 适配器（adapters/okx_client.py）

- 封装 OKX REST V5 签名（HMAC-SHA256 + Base64）
- 支持模拟盘（`x-simulated-trading: 1`）
- 主要方法：
  - `place_market / place_limit / place_post_only` — 市价/限价/PostOnly 下单
  - `order_algo` — 计划委托（TP/SL）
  - `cancel_algo / cancel_order` — 撤单
  - `get_positions / get_account_balance` — 持仓与账户查询
  - `set_leverage` — 启动时设置杠杆
  - `amend_order` — 追价改单

### 4.3 策略核心（core/strategy.py — BreakoutBot）

#### 事件驱动流程

```
WS trades → CandleAggregator → on_candle_close() → 策略逻辑
                               ↗
WS tickers → on_ticker() → 实时价格更新
```

#### 开仓逻辑

1. 每根 K 线收盘时记录 `prev_high` / `prev_low`（前K高低）
2. 下一根 K 线的 ticker 价格突破上述阈值时触发开仓
3. **single_shot** 模式：每根 K 线只触发一次方向，防止重复入场

#### Maker 追价机制

```
PostOnly 限价单
    → 超时未成交 → 改价追单（最多 N 次）
    → 仍未成交 → IOC 限价单（offset 几个 tick）
    → 仍未成交 → 市价兜底
```

#### 止盈止损管理

- **自动开仓**：同时下 SL + TP1 + TP2 三个计划委托
- **手动开仓**：只下 SL（手动控制平仓价）
- **TP1 成交后**：SL 自动移至保本价，TP2 继续挂单
- **SL 触发后**：撤销所有剩余计划委托，清理持仓状态

#### EMA 趋势过滤

- 配置多周期 EMA（默认 20/40/60）
- 启动时通过 REST 拉取历史 K 线预热 EMA 初值
- 运行中每根 K 线增量更新
- `trend_entry_mode` 可设为 both / long / short / none，限制只做顺势方向

#### HHLL 手动止损模式（manual_sl_mode: hhll）

- 运行态维护双向止损价 `manual_stop_long_px` / `manual_stop_short_px`
- 通过 Web UI 或 API 动态设置，进程重启后失效

### 4.4 风控中心（core/risk.py — RiskCenter）

| 参数 | 说明 |
|------|------|
| `daily_max_loss_usdt` | 当日累计亏损达到阈值后锁定，不再开仓 |
| `daily_profit_target_usdt` | 当日盈利达到目标后锁定（守住利润） |
| `daily_max_trades` | 当日最大开仓次数上限 |
| `cooldown_sec` | 每次平仓后冷却 N 秒 |
| `max_position_duration_sec` | 单笔最长持仓时间，到期强平 |
| `equity_tp_usdt` | 账户权益达到目标时全平并锁定 |
| `equity_sl_usdt` | 账户权益跌至阈值时全平并锁定 |

- 每日 00:00 自动重置（基于本地日期）
- 所有限制通过 `can_open_trade()` 统一检查

### 4.5 通知系统（core/notifier.py）

- 支持 Telegram Bot（`sendMessage` API）
- 支持通用 Webhook（JSON POST）
- `time_windows` 配置发送时间窗口（例如只在 09:00–23:59 发送）
- `min_interval_sec` 限制最小发送间隔，防止消息风暴
- 主要通知事件：开仓、平仓、K 线收盘摘要、风控锁定、权益触发

### 4.6 Web API（app/api.py）

| 端点 | 说明 |
|------|------|
| `GET /api/status` | 获取完整运行状态快照（每秒轮询） |
| `GET /api/healthz` | 健康检查（WS 是否有 ticker） |
| `POST /api/trade/open` | 手动开仓（指定方向和手数） |
| `POST /api/trade/flatten` | 手动全平 |
| `POST /api/trade/set_state` | 切换交易状态（flat / armed 等） |
| `POST /api/params/update` | 参数热更新（写 config.yaml + 同步运行态） |
| `POST /api/manual/open_long_limit` | 手动限价开多 |
| `POST /api/manual/open_short_limit` | 手动限价开空 |
| `POST /api/manual/close_long_limit` | 手动限价平多 |
| `POST /api/manual/close_short_limit` | 手动限价平空 |
| `POST /api/manual/set_stops` | 设置手动止损价 |
| `POST /api/notify/test` | 发送测试通知 |

### 4.7 Web 控制台（webui/）

- 纯原生 JS，每秒 `GET /api/status` 轮询更新页面
- 展示：实时价格 / 前K高低 / 当前持仓 / EMA 值 / 风控状态 / 权益
- 表单支持所有可热更新参数，提交即生效无需重启
- 手动交易面板：限价开/平多空、设置止损价

---

## 五、配置参数速查（config.yaml）

```yaml
instrument: ETH-USDT-SWAP    # 合约品种
period: 5m                   # K线周期（1m/5m/15m/1H/4H/1D）
leverage: 20                 # 杠杆倍数
position_mode: long_short    # 持仓模式（net / long_short）
is_simulated: false          # 是否模拟盘
auto_trade: true             # 自动交易开关
open_size: '50'              # 开仓手数
stop_x: 1.0                  # SL 倍数（x × 前K振幅）
tp_y: 1.0                    # TP 倍数（y × 前K振幅）
single_shot: true            # 每K只触发一次

# 限价单参数
limit_post_only: true
limit_offset_ticks: 1
limit_fill_timeout_ms: 2000
limit_max_requotes: 3

# EMA 趋势
trend_exit_enabled: false
trend_entry_mode: none        # both / long / short / none
trend_ema_periods: [20, 40, 60]

# 风控
risk:
  enabled: true
  daily_max_loss_usdt: 1000.0
  daily_profit_target_usdt: 600000.0
  daily_max_trades: 30
  cooldown_sec: 0
  max_position_duration_sec: 0
  equity_tp_usdt: 0.0
  equity_sl_usdt: 0.0

# 通知
notify:
  enabled: true
  method: telegram
  telegram_bot_token: ""
  telegram_chat_id: ""
  min_interval_sec: 10
  time_windows:
    - "09:00-23:59"
```

---

## 六、开发迭代历程

| 阶段 | 主要工作 |
|------|---------|
| 初始版本 | REST 轮询版改写为 WS 事件驱动，搭建 FastAPI + 静态前端框架 |
| 行情层完善 | 实现 CandleAggregator（逐笔→OHLC），支持多周期 |
| 策略完善 | Maker 追价、IOC 回退、TP1 移保本、SL 联动清理 |
| 风控 | RiskCenter 接入（日亏损/盈利/次数/冷却/持仓时长） |
| EMA 趋势 | 历史预热 + 增量滚动，trend_entry_mode 过滤 |
| 手动交易 | 限价手动开/平仓、HHLL 手动止损 |
| 通知系统 | Telegram / Webhook，时间窗口过滤，最小间隔防刷屏 |
| 权益风控 | 实时轮询账户权益，equity_tp / equity_sl 触发全平锁定 |
| K 线通知 | 每根 K 线收盘后发送摘要通知 |
| 周期扩展 | period_alias 支持小时、日线别名 |
| 代码清理 | 删除死代码、修复 Bug、补全运行态同步 |

---

## 七、已知约束与注意事项

1. **PnL 估算非结算数据**：`risk_realized_pnl` 基于内部记录，非 OKX 官方已结算盈亏，仅用于风控统计参考。

2. **K 线聚合依赖逐笔成交**：若 OKX trades 推送出现延迟或断流，K 线收盘触发可能滞后；`flush_due()` 提供了时间兜底（每 0.5 秒扫描）。

3. **单进程设计**：策略使用 `threading.Lock` 保护共享状态，不支持多实例并发操作同一账户。

4. **权益轮询**：账户权益通过 REST 轮询（默认每 3 秒），非实时推送，存在秒级延迟。

5. **模拟盘切换**：`is_simulated` 写在 `config.yaml`，切换需重启服务。

6. **手动止损仅内存生效**：`manual_stop_long_px` / `manual_stop_short_px` 不持久化，进程重启后需重新设置。
