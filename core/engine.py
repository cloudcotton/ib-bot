"""交易引擎：协调合约监控、信号检测、下单执行。

功能：
  1. 止损监控   — 双K止损，自动市价平仓
  2. 手动开仓   — 市价/限价，开仓后自动挂 IB 原生止损单
  3. 止损管理   — 设置/修改止损价（IB 服务端止损单）
  4. 手动平仓   — 市价平仓 + 自动撤销止损单

止损双层保护：
  - IB 原生止损单（STP）：服务端执行，Bot 断线后仍有效
  - 双K止损逻辑：Bot 运行时作为辅助退出信号
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

from adapters.ib_client import IBClient
from core.equity_recorder import EquityRecorder
from core.kline_manager import Bar, KlineBuffer, TickBarBuilder, _PERIOD_SECONDS
from core.notifier import TelegramNotifier
from core.settings import ContractConfig, Settings, save_settings
from core.strategy import Signal, check_signal

logger = logging.getLogger(__name__)

_RECONNECT_DELAYS = [5, 10, 30, 60, 120, 300]


def _safe_ensure_future(coro, label: str = "") -> asyncio.Task:
    """asyncio.ensure_future 的安全包装：后台协程若抛出未捕获异常，
    通过 done_callback 记录完整 traceback，防止异常被静默吞没。"""
    task = asyncio.ensure_future(coro)

    def _on_done(t: asyncio.Task) -> None:
        if not t.cancelled() and (exc := t.exception()):
            logger.error(f"后台任务异常 [{label}]", exc_info=exc)

    task.add_done_callback(_on_done)
    return task


# ── ContractMonitor ────────────────────────────────────────────────────────


class ContractMonitor:
    """监控单个合约：K 线缓冲 + 持仓状态 + 止损单追踪。"""

    def __init__(
        self,
        cfg: ContractConfig,
        ib_contract,
        ib_client: IBClient,
        notifier: Optional[TelegramNotifier],
        cooldown_sec: int,
    ) -> None:
        self.cfg = cfg
        self.contract = ib_contract
        self._ib = ib_client
        self._notifier = notifier
        self._cooldown = cooldown_sec

        self.buffer = KlineBuffer()
        self.ticker = None                  # ib_insync Ticker（逐Tick订阅）
        self._tick_builder: Optional[TickBarBuilder] = None
        self._tick_callback: Optional[object] = None   # 注册到 updateEvent 的引用
        self._initialized: bool = False     # 历史K线是否已预热
        self._signal_enabled: bool = True   # 双K止损信号开关

        # 持仓状态
        self._position: float = 0.0
        self._entry_price: Optional[float] = None    # 开仓均价（来自 IB avgCost）

        # IB 止损单追踪
        self._stop_trade = None                       # ib_insync Trade 对象
        self._stop_price: Optional[float] = None      # 当前止损价

        # 平仓执行状态
        self._in_flight: bool = False
        self._close_lock = asyncio.Lock()        # 防止异步重入
        self._last_signal_time: dict[str, datetime] = {}
        self._last_signal: Optional[str] = None

        # 开仓当根 K 线时间戳：用于区分"开仓前历史极值"与"开仓后新极值"
        # 开仓当根 K 线用 close 触发止损；后续 K 线用 kn.low/kn.high 触发
        self._entry_bar_time: Optional[str] = None

        # 抄底摸顶目标价（None 表示未设置）
        self._buy_target: Optional[float] = None   # 抄底价：低点触及后收盘回升则买入
        self._sell_target: Optional[float] = None  # 摸顶价：高点触及后收盘回落则卖出
        self._reversal_qty: float = 1.0            # 抄底/摸顶开仓手数

    # ── 持仓更新（IBClient 回调）──────────────────────────────────────────

    def update_position(self, qty: float, avg_cost: float = 0.0) -> None:
        old = self._position
        self._position = qty
        if qty != 0 and avg_cost:
            # IB 期货的 avgCost = 成交价 × 合约乘数，需除以乘数还原为价格
            # 乘数取自静态配置，避免 IB 接口瞬时返回异常导致均价失真
            mult = self.cfg.multiplier or 1.0
            self._entry_price = avg_cost / mult
            # 新开仓（从无仓到有仓）：记录开仓时所在 K 线，避免该 K 线已有的
            # 历史极值（开仓前发生）被误判为止损信号
            if old == 0:
                self._entry_bar_time = (
                    self.buffer.current.time if self.buffer.current else None
                )
                logger.debug(f"[{self.cfg.key}] 开仓K线时间: {self._entry_bar_time}")
        elif qty == 0:
            self._entry_price = None
            self._stop_price = None
            self._entry_bar_time = None
            if self._stop_trade is not None:
                # 持仓归零（IB 强平或保证金不足触发），主动撤销残留止损单。
                # 若止损单尚未触发，孤立订单可能在后续行情中意外反向开仓。
                # _cancel_stop_order 异步执行，由它负责将 _stop_trade 置 None。
                _safe_ensure_future(self._cancel_stop_order(),
                                    label=f"{self.cfg.key} 强平后撤止损")
            else:
                self._stop_trade = None
        if old != qty:
            logger.info(f"[{self.cfg.key}] 持仓: {old} → {qty}  均价={avg_cost:.4f}")

    # ── 逐 Tick 回调 ──────────────────────────────────────────────────────

    def on_ticker_update(self, ticker) -> None:
        """ticker.updateEvent 回调：处理本批次所有新成交 Tick。

        ib_insync 在每个 TCP 数据包处理完后触发一次此回调，
        ticker.tickByTicks 包含该批次内所有新到的 TickByTickAllLast。
        """
        if not self._initialized or self._tick_builder is None:
            return
        ticks = ticker.tickByTicks
        if not ticks:
            return
        for t in ticks:
            price = float(t.price)
            size = float(t.size)
            if price <= 0:
                continue
            try:
                has_new_bar = self._tick_builder.on_tick(price, size, t.time)
            except Exception as e:
                logger.error(f"[{self.cfg.key}] Tick 处理异常: {e}")
                continue
            # K 线收盘时检查抄底/摸顶开仓信号
            if has_new_bar and self.buffer.completed:
                self._check_reversal_entry(self.buffer.completed[-1])
            # 每 tick 都检测止损信号
            self._check_and_fire()

    def _check_and_fire(self) -> None:
        """双K止损检测（辅助退出信号）。"""
        if not self._signal_enabled:
            return
        signal = check_signal(self.buffer, self._position, self._entry_bar_time)
        if signal is None:
            return
        now = datetime.now()
        last = self._last_signal_time.get(signal.value)
        if last and (now - last).total_seconds() < self._cooldown:
            return
        if self._in_flight:
            return
        # 在同步段立即置位，防止后续 Tick 回调在 _execute_close 启动前再次进入
        self._in_flight = True
        self._last_signal_time[signal.value] = now
        self._last_signal = signal.value
        logger.info(f"[{self.cfg.key}] 双K止损信号: {signal.value} 持仓={self._position}")
        _safe_ensure_future(self._execute_close(signal, reason="双K止损"),
                            label=f"{self.cfg.key} _execute_close")

    def _check_reversal_entry(self, bar: Bar) -> None:
        """K 线收盘时检查抄底/摸顶开仓信号。
        仅当双K止损启用且当前无持仓时触发。
        - 抄底：bar.low < buy_target  且  bar.close >= buy_target → 买入
        - 摸顶：bar.high > sell_target 且  bar.close <= sell_target → 卖出
        价格一旦突破目标价（不论是否开仓），目标价立即清零。
        若同一根K线同时满足抄底和摸顶条件，两个目标价均清零，但均不开仓（信号矛盾）。
        """
        if not self._signal_enabled:
            return
        if self._position != 0:
            return

        # 第一步：检测各目标价是否被突破，先统一清零（不论收盘如何）
        buy_triggered  = False   # 低点突破了抄底价
        sell_triggered = False   # 高点突破了摸顶价
        buy_target     = None
        sell_target    = None

        if self._buy_target is not None and bar.low < self._buy_target:
            buy_target = self._buy_target
            self._buy_target = None   # 低点突破后立即清零
            buy_triggered = True

        if self._sell_target is not None and bar.high > self._sell_target:
            sell_target = self._sell_target
            self._sell_target = None  # 高点突破后立即清零
            sell_triggered = True

        # 第二步：若两个目标价在同一根K线内同时被突破，信号矛盾，均不开仓
        if buy_triggered and sell_triggered:
            logger.warning(
                f"[{self.cfg.key}] 抄底价 {buy_target:.4f} 与摸顶价 {sell_target:.4f} "
                f"在同一K线同时被突破（low={bar.low:.4f} high={bar.high:.4f} "
                f"close={bar.close:.4f}），信号矛盾，两者均清零不开仓"
            )
            return

        # 第三步：单独触发时，检查收盘价是否满足开仓条件
        if buy_triggered:
            if bar.close >= buy_target:
                logger.info(
                    f"[{self.cfg.key}] 抄底信号: low={bar.low:.4f} < {buy_target:.4f}, "
                    f"close={bar.close:.4f} >= {buy_target:.4f} → 买入 {self._reversal_qty} 手"
                )
                _safe_ensure_future(
                    self._execute_open("long", buy_target),
                    label=f"{self.cfg.key} _execute_open(long)",
                )
            else:
                logger.info(
                    f"[{self.cfg.key}] 抄底价 {buy_target:.4f} 被突破，收盘 {bar.close:.4f} 未回 → 清零不开仓"
                )

        if sell_triggered:
            if bar.close <= sell_target:
                logger.info(
                    f"[{self.cfg.key}] 摸顶信号: high={bar.high:.4f} > {sell_target:.4f}, "
                    f"close={bar.close:.4f} <= {sell_target:.4f} → 卖出 {self._reversal_qty} 手"
                )
                _safe_ensure_future(
                    self._execute_open("short", sell_target),
                    label=f"{self.cfg.key} _execute_open(short)",
                )
            else:
                logger.info(
                    f"[{self.cfg.key}] 摸顶价 {sell_target:.4f} 被突破，收盘 {sell_target:.4f} 未回 → 清零不开仓"
                )

    async def _execute_open(self, direction: str, trigger_price: float) -> None:
        """市价开仓（抄底/摸顶），双K止损启用时自动跟踪止损。"""
        if self._position != 0:
            logger.warning(f"[{self.cfg.key}] 抄底/摸顶开仓跳过：已有持仓 {self._position}")
            return
        label = "抄底" if direction == "long" else "摸顶"
        try:
            trade = self._ib.open_position(
                contract=self.contract,
                direction=direction,
                qty=self._reversal_qty,
                order_type="market",
            )
            logger.info(
                f"[{self.cfg.key}] {label} 开仓指令已发送: {direction} {self._reversal_qty} 手 "
                f"触发价={trigger_price:.4f} orderId={trade.order.orderId}"
            )
            if self._notifier:
                side = "多" if direction == "long" else "空"
                self._notifier.send(
                    f"🎯 <b>{label}开仓</b>\n"
                    f"合约: <code>{self.cfg.symbol}@{self.cfg.exchange}</code>\n"
                    f"方向: {side}  手数: {self._reversal_qty}\n"
                    f"触发价: {trigger_price:.4f}"
                )
        except Exception as e:
            logger.error(f"[{self.cfg.key}] {label} 开仓异常: {e}")

    async def _execute_close(self, signal: Signal, reason: str = "") -> None:
        # Lock 确保即使多个协程同时被调度，平仓逻辑也绝对串行执行
        if self._close_lock.locked():
            logger.debug(f"[{self.cfg.key}] 平仓已在进行中，丢弃重复信号")
            self._in_flight = False
            return
        async with self._close_lock:
            pos_qty = self._position
            try:
                # 先撤销 IB 止损单（避免重复平仓）
                await self._cancel_stop_order()

                success = await self._ib.close_position(self.contract, pos_qty)
                if success and self._notifier:
                    price = self.buffer.current.close if self.buffer.current else 0.0
                    self._notifier.notify_close(
                        symbol=self.cfg.symbol,
                        exchange=self.cfg.exchange,
                        direction=signal.value,
                        qty=abs(pos_qty),
                        price=price,
                    )
            except Exception as e:
                logger.error(f"[{self.cfg.key}] 执行平仓异常: {e}")
            finally:
                self._in_flight = False

    # ── 止损单操作 ────────────────────────────────────────────────────────

    async def _cancel_stop_order(self) -> None:
        if self._stop_trade is not None:
            try:
                self._ib.cancel_order(self._stop_trade)
            except Exception as e:
                logger.warning(f"[{self.cfg.key}] 撤止损单异常: {e}")
            self._stop_trade = None
            self._stop_price = None

    # ── 状态快照 ───────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        buf = self.buffer
        current = buf.current
        k1 = buf.completed[-1] if len(buf.completed) >= 2 else None
        k2 = buf.completed[-2] if len(buf.completed) >= 2 else None
        pos = self._position
        cp = current.close if current else None

        # 浮动盈亏（点数）
        pnl_pts: Optional[float] = None
        if pos != 0 and cp and self._entry_price:
            pnl_pts = (cp - self._entry_price) * (1 if pos > 0 else -1) * abs(pos)

        return {
            "symbol": self.cfg.symbol,
            "exchange": self.cfg.exchange,
            "currency": self.cfg.currency,
            "timeframe": self.cfg.timeframe,
            "expiry": self.contract.lastTradeDateOrContractMonth,
            "position": pos,
            "position_side": "long" if pos > 0 else ("short" if pos < 0 else "flat"),
            "entry_price": self._entry_price,
            "current_price": cp,
            "pnl_pts": round(pnl_pts, 4) if pnl_pts is not None else None,
            "stop_price": self._stop_price,
            "klines_ready": buf.ready,
            "in_flight": self._in_flight,
            "last_signal": self._last_signal,
            "last_signal_time": (
                self._last_signal_time[self._last_signal].isoformat()
                if self._last_signal and self._last_signal in self._last_signal_time
                else None
            ),
            "ema20": round(buf.ema20, 4) if buf.ema20 is not None else None,
            "current_bar": current.to_dict() if current else None,
            "k1": k1.to_dict() if k1 else None,
            "k2": k2.to_dict() if k2 else None,
            "bars_buffered": len(buf.completed),
            "buy_target": self._buy_target,
            "sell_target": self._sell_target,
            "reversal_qty": self._reversal_qty,
        }


# ── TradingEngine ──────────────────────────────────────────────────────────


class TradingEngine:
    """顶层引擎：管理 IB 连接、合约监控、开仓/止损操作。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.ib_client = IBClient(
            host=settings.ib.host,
            port=settings.ib.port,
            client_id=settings.ib.client_id,
            account=settings.ib_account,
        )
        self._monitors: dict[str, ContractMonitor] = {}
        self._notifier: Optional[TelegramNotifier] = None
        self._equity_recorder: Optional[EquityRecorder] = None
        self._running = False
        self._stopping = False
        self._reconnect_task: Optional[asyncio.Task] = None
        self._reconnect_attempt = 0
        self._resubscribe_task: Optional[asyncio.Task] = None  # 10182 触发的定向重订阅

    # ── 生命周期 ───────────────────────────────────────────────────────────

    async def start(self) -> None:
        logger.info("引擎启动中…")
        nc = self.settings.notify
        if nc.enabled:
            from core.notifier import init_notifier
            self._notifier = init_notifier(
                nc.telegram_bot_token, nc.telegram_chat_id, nc.min_interval_sec
            )

        await self.ib_client.connect()
        if self._notifier:
            self._notifier.notify_connected(self.settings.ib.host, self.settings.ib.port)

        self.ib_client.register_position_callback(self._on_position_update)
        self.ib_client.ib.disconnectedEvent += self._on_disconnected
        self.ib_client.ib.errorEvent       += self._on_ib_error

        for cfg in self.settings.active_contracts:
            await self._init_monitor(cfg)

        # 启动净值定时记录器
        elc = self.settings.equity_log
        if elc.enabled:
            self._equity_recorder = EquityRecorder(self.ib_client, elc.record_time)
            self._equity_recorder.start()

        self._running = True
        logger.info(f"引擎就绪，监控 {len(self._monitors)} 个合约")

    async def stop(self) -> None:
        logger.info("引擎停止中…")
        self._stopping = True
        self._running = False
        if self._equity_recorder:
            self._equity_recorder.stop()
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
        for monitor in self._monitors.values():
            if monitor.ticker is not None:
                try:
                    self.ib_client.cancel_ticks(monitor.ticker, monitor._tick_callback)
                except Exception:
                    pass
        await self.ib_client.disconnect()
        logger.info("引擎已停止")

    # ── 历史 K 线加载（内部辅助）──────────────────────────────────────────

    @staticmethod
    def _fill_buffer_from_history(buffer: KlineBuffer, hist: list) -> None:
        """将历史 K 线装入缓冲区。

        IB 返回的最后一根 K 线是当前正在成形的 K 线（partial bar），
        它尚未完成，不能放入 buffer.completed，否则会导致 K-1/K-2 显示偏移。
        正确做法：
          - hist[:-1] → buffer.completed（已完成 K 线，供 K-1/K-2 计算）
          - hist[-1]  → buffer.current（当前 K 线的历史种子，让 ready 立即为 True）

        TickBarBuilder 收到首个 Tick 时会覆盖 buffer.current，
        历史种子仅起到"填充 ready 状态"和"保留已知极值"的作用。
        """
        if not hist:
            return
        # 已完成 K 线：排除最后一根（partial），最多取 24 根（EMA20 需 20 根种子）
        for b in hist[:-1][-24:]:
            buffer.add_completed(Bar(
                time=str(b.date), open=float(b.open), high=float(b.high),
                low=float(b.low), close=float(b.close), volume=float(b.volume or 0),
            ))
        # 当前 K 线：用最后一根历史 K 线作为种子，确保 ready 在首 Tick 到达前即为 True
        b = hist[-1]
        buffer.update_current(Bar(
            time=str(b.date), open=float(b.open), high=float(b.high),
            low=float(b.low), close=float(b.close), volume=float(b.volume or 0),
        ))

    # ── 合约初始化 ─────────────────────────────────────────────────────────

    async def _init_monitor(self, cfg: ContractConfig) -> None:
        logger.info(f"初始化合约监控: {cfg.key}")
        try:
            ib_contract = await self.ib_client.resolve_contract(
                symbol=cfg.symbol, exchange=cfg.exchange,
                currency=cfg.currency, expiry=cfg.expiry,
            )
        except Exception as e:
            logger.error(f"合约解析失败 {cfg.key}: {e}")
            return

        monitor = ContractMonitor(
            cfg=cfg, ib_contract=ib_contract, ib_client=self.ib_client,
            notifier=self._notifier,
            cooldown_sec=self.settings.strategy.signal_cooldown_sec,
        )
        qty = self.ib_client.get_position(ib_contract.conId)
        avg_cost = self.ib_client.get_avg_cost(ib_contract.conId)
        monitor.update_position(qty, avg_cost)

        # ── 拉取历史 K 线，初始化缓冲区 ──
        try:
            hist = await self.ib_client.fetch_historical_bars(
                contract=ib_contract, timeframe=cfg.timeframe, use_rth=cfg.use_rth,
            )
            self._fill_buffer_from_history(monitor.buffer, hist)
            logger.info(
                f"[{cfg.key}] 历史K线预热完成"
                f"（已完成 {len(monitor.buffer.completed)} 根，"
                f"ready={monitor.buffer.ready}，ema20={monitor.buffer.ema20}）"
            )
        except Exception as e:
            logger.error(f"[{cfg.key}] 历史K线拉取失败，将在首批Tick到来后逐步建立缓冲: {e}")

        # ── 创建 Tick 合成器并订阅实时成交流 ──
        monitor._tick_builder = TickBarBuilder(
            timeframe=cfg.timeframe, buffer=monitor.buffer,
        )
        monitor._initialized = True

        monitor._tick_callback = monitor.on_ticker_update
        ticker = self.ib_client.subscribe_ticks(
            contract=ib_contract, callback=monitor._tick_callback,
        )
        monitor.ticker = ticker
        self._monitors[cfg.key] = monitor
        logger.info(f"合约监控已启动（逐Tick模式）: {cfg.key} conId={ib_contract.conId}")

    # ── 开仓 ───────────────────────────────────────────────────────────────

    async def open_position(
        self,
        key: str,
        direction: str,
        qty: float,
        order_type: str,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
    ) -> dict:
        """开仓，可同时设置止损价（开仓成交后自动挂 IB 止损单）。"""
        monitor = self._monitors.get(key)
        if not monitor:
            return {"success": False, "error": f"合约 {key!r} 不在监控列表中"}
        if monitor._position != 0:
            return {"success": False, "error": "已有持仓，请先平仓"}
        if not self.ib_client.is_connected:
            return {"success": False, "error": "IB 未连接"}

        try:
            trade = self.ib_client.open_position(
                contract=monitor.contract,
                direction=direction,
                qty=qty,
                order_type=order_type,
                limit_price=limit_price,
            )
        except Exception as e:
            return {"success": False, "error": str(e)}

        # 成交后挂止损单
        if stop_price is not None:
            _stop_price = stop_price
            _direction = direction
            _qty = qty

            def on_entry_filled(t):
                fill = t.orderStatus.avgFillPrice
                logger.info(f"[{key}] 开仓成交: {_direction} {_qty} @ {fill}")
                _safe_ensure_future(
                    self._submit_stop(monitor, _direction, _qty, _stop_price),
                    label=f"{key} _submit_stop",
                )

            trade.filledEvent += on_entry_filled

        return {
            "success": True,
            "order_id": trade.order.orderId,
            "order_type": order_type,
            "direction": direction,
            "qty": qty,
            "stop_price": stop_price,
        }

    async def _submit_stop(
        self, monitor: ContractMonitor,
        direction: str, qty: float, stop_price: float,
    ) -> None:
        """开仓成交后提交 IB 止损单，并监听止损成交事件。"""
        try:
            # 若已有旧止损单先撤掉
            await monitor._cancel_stop_order()

            stop_trade = self.ib_client.place_stop_order(
                contract=monitor.contract,
                position_direction=direction,
                qty=qty,
                stop_price=stop_price,
            )
            monitor._stop_trade = stop_trade
            monitor._stop_price = stop_price
            logger.info(f"[{monitor.cfg.key}] IB 止损单已提交 @ {stop_price}")

            # 止损单成交时通知
            def on_stop_filled(t):
                fill = t.orderStatus.avgFillPrice
                logger.info(f"[{monitor.cfg.key}] IB 止损单触发 @ {fill}")
                monitor._stop_trade = None
                monitor._stop_price = None
                if self._notifier:
                    self._notifier.send(
                        f"🛑 <b>止损触发（IB 执行）</b>\n"
                        f"合约: <code>{monitor.cfg.symbol}@{monitor.cfg.exchange}</code>\n"
                        f"成交价: {fill}"
                    )

            stop_trade.filledEvent += on_stop_filled

        except Exception as e:
            logger.error(f"[{monitor.cfg.key}] 提交止损单失败: {e}")

    # ── 止损价管理 ─────────────────────────────────────────────────────────

    async def set_stop_loss(self, key: str, stop_price: float) -> dict:
        """为当前持仓设置或修改止损价（重新挂 IB 止损单）。"""
        monitor = self._monitors.get(key)
        if not monitor:
            return {"success": False, "error": f"合约 {key!r} 不在监控列表中"}
        if monitor._position == 0:
            return {"success": False, "error": "当前无持仓"}

        direction = "long" if monitor._position > 0 else "short"
        qty = abs(monitor._position)

        await self._submit_stop(monitor, direction, qty, stop_price)
        return {"success": True, "stop_price": stop_price}

    async def cancel_stop_loss(self, key: str) -> dict:
        """撤销当前止损单。"""
        monitor = self._monitors.get(key)
        if not monitor:
            return {"success": False, "error": f"合约 {key!r} 不在监控列表中"}
        await monitor._cancel_stop_order()
        return {"success": True}

    # ── 手动平仓 ───────────────────────────────────────────────────────────

    async def manual_close(
        self, key: str,
        order_type: str = "market",
        limit_price: Optional[float] = None,
    ) -> dict:
        monitor = self._monitors.get(key)
        if not monitor:
            return {"success": False, "error": f"合约 {key!r} 不在监控列表中"}
        if monitor._position == 0:
            return {"success": False, "error": "当前无持仓"}

        # 先撤止损单，再平仓
        await monitor._cancel_stop_order()
        success = await self.ib_client.close_position(
            monitor.contract, monitor._position, order_type, limit_price
        )
        return {"success": success}

    # ── 断线重连 ───────────────────────────────────────────────────────────

    def _on_disconnected(self) -> None:
        if self._stopping:
            return
        logger.warning("IB 连接断开，准备自动重连…")
        self._running = False
        if self._notifier:
            self._notifier.notify_disconnected()
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = _safe_ensure_future(
                self._reconnect_loop(), label="_reconnect_loop"
            )

    def _on_ib_error(self, reqId: int, errorCode: int, errorString: str, contract) -> None:
        """监听 IB 错误码，记录日志并对关键错误做自动恢复。

        常见信息级错误码（非故障，可忽略）：
          2104/2106/2158 — Market data farm connected/disconnected（正常连接状态变化）
          2119           — Market data farm is connecting

        常见 Tick 订阅错误码：
          321  — Error validating request: subscription already in progress
                 原因：程序重启时旧 clientId 的订阅在 IB 侧尚存活，
                 新订阅请求被拒，该合约不再收到 tick 数据。
                 处理：触发对该合约的重新订阅（cancelTickByTickData + re-subscribe）。
          354  — Requested market data is not subscribed（缺少市场数据订阅权限）
          10187 — Failed to request tick-by-tick data

        10182 — Failed to request live updates (disconnected)：
            数据农场断开重连后推送流被强制掐断，触发全量重订阅。
        """
        # 信息级状态通知（无需处理）
        _INFO_CODES = {2104, 2106, 2107, 2108, 2119, 2158}
        if errorCode in _INFO_CODES:
            logger.debug(f"IB 状态通知 ({errorCode}): {errorString}")
            return

        # 记录所有非信息级错误，方便排查（如 tick 订阅失败）
        symbol = contract.symbol if contract else "—"
        logger.warning(
            f"IB 错误 reqId={reqId} code={errorCode} contract={symbol}: {errorString}"
        )

        # 321: Tick 订阅被 IB 拒绝（旧订阅残留），对该合约触发重新订阅
        if errorCode == 321 and self._running and not self._stopping:
            for key, monitor in self._monitors.items():
                if (monitor.ticker is not None
                        and hasattr(monitor.ticker, 'reqId')
                        and monitor.ticker.reqId == reqId):
                    logger.warning(
                        f"[{key}] Tick 订阅被拒（321），1 秒后重新订阅…"
                    )
                    _safe_ensure_future(
                        self._resubscribe_one(key), label=f"{key} resubscribe(321)"
                    )
                    break

        # 10182: 数据农场断线，全量重订阅
        if errorCode == 10182 and self._running and not self._stopping:
            if self._resubscribe_task is None or self._resubscribe_task.done():
                logger.warning(
                    f"K线推送流断开 (10182 reqId={reqId})，自动重新订阅所有合约…"
                )
                self._resubscribe_task = _safe_ensure_future(
                    self._resubscribe_all(), label="_resubscribe_all(10182)"
                )

    async def _reconnect_loop(self) -> None:
        self._reconnect_attempt = 0
        while not self._stopping:
            delay = _RECONNECT_DELAYS[min(self._reconnect_attempt, len(_RECONNECT_DELAYS) - 1)]
            logger.info(f"等待 {delay}s 后发起第 {self._reconnect_attempt + 1} 次重连…")
            await asyncio.sleep(delay)
            if self._stopping:
                break
            try:
                await self.ib_client.connect()
            except Exception as e:
                logger.warning(f"第 {self._reconnect_attempt + 1} 次重连失败: {e}")
                self._reconnect_attempt += 1
                continue
            try:
                await self._resubscribe_all()
            except Exception as e:
                logger.error(f"重新订阅失败: {e}")
                self._reconnect_attempt += 1
                continue
            self._running = True
            logger.info(f"重连成功（共尝试 {self._reconnect_attempt + 1} 次）")
            if self._notifier:
                self._notifier.notify_connected(self.settings.ib.host, self.settings.ib.port)
            break

    async def _resubscribe_all(self) -> None:
        await self.ib_client._fetch_initial_positions()
        for key, monitor in self._monitors.items():
            # 取消旧 Tick 订阅
            if monitor.ticker is not None:
                try:
                    self.ib_client.cancel_ticks(monitor.ticker, monitor._tick_callback)
                except Exception:
                    pass
                monitor.ticker = None
                monitor._tick_callback = None

            # 清空 K 线缓冲与 Tick 合成器
            monitor.buffer.reset()
            monitor._initialized = False
            if monitor._tick_builder is not None:
                monitor._tick_builder.reset()

            try:
                # 重拉历史 K 线
                hist = await self.ib_client.fetch_historical_bars(
                    contract=monitor.contract,
                    timeframe=monitor.cfg.timeframe,
                    use_rth=monitor.cfg.use_rth,
                )
                self._fill_buffer_from_history(monitor.buffer, hist)

                # 重建 Tick 合成器
                monitor._tick_builder = TickBarBuilder(
                    timeframe=monitor.cfg.timeframe, buffer=monitor.buffer,
                )
                monitor._initialized = True

                # 重新订阅 Tick
                monitor._tick_callback = monitor.on_ticker_update
                ticker = self.ib_client.subscribe_ticks(
                    contract=monitor.contract, callback=monitor._tick_callback,
                )
                monitor.ticker = ticker

                # 刷新持仓状态
                qty = self.ib_client.get_position(monitor.contract.conId)
                avg_cost = self.ib_client.get_avg_cost(monitor.contract.conId)
                monitor.update_position(qty, avg_cost)
                logger.info(f"重新订阅成功（逐Tick）: {key}")
            except Exception as e:
                logger.error(f"重新订阅失败 {key}: {e}")

    async def _resubscribe_one(self, key: str) -> None:
        """对单个合约重新订阅 Tick（用于 321 错误恢复）。"""
        await asyncio.sleep(1)   # 稍等片刻，让 IB 侧旧订阅超时
        monitor = self._monitors.get(key)
        if monitor is None or not self._running:
            return
        # 取消旧订阅
        if monitor.ticker is not None:
            try:
                self.ib_client.cancel_ticks(monitor.ticker, monitor._tick_callback)
            except Exception:
                pass
            monitor.ticker = None
        # 重新订阅
        try:
            monitor._tick_callback = monitor.on_ticker_update
            ticker = self.ib_client.subscribe_ticks(
                contract=monitor.contract, callback=monitor._tick_callback,
            )
            monitor.ticker = ticker
            logger.info(f"[{key}] Tick 重新订阅成功")
        except Exception as e:
            logger.error(f"[{key}] Tick 重新订阅失败: {e}")

    # ── 持仓变动回调 ───────────────────────────────────────────────────────

    def _on_position_update(self, con_id: int, qty: float, avg_cost: float) -> None:
        for monitor in self._monitors.values():
            if monitor.contract.conId == con_id:
                monitor.update_position(qty, avg_cost)
                break

    # ── 状态快照 ───────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        reconnecting = (
            self._reconnect_task is not None and not self._reconnect_task.done()
        )
        return {
            "connected": self.ib_client.is_connected,
            "running": self._running,
            "reconnecting": reconnecting,
            "reconnect_attempt": self._reconnect_attempt if reconnecting else 0,
            "signal_enabled": self.settings.strategy.signal_enabled,
            "account": self.ib_client.get_account_summary(),
            "contracts": [m.get_status() for m in self._monitors.values()],
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }

    # ── 参数热更新 ─────────────────────────────────────────────────────────

    async def set_reversal_prices(
        self,
        key: str,
        buy_target: Optional[float] = None,
        sell_target: Optional[float] = None,
        qty: Optional[float] = None,
    ) -> dict:
        """设置/清除指定合约的抄底/摸顶目标价。
        传 0 表示清零，传正数表示设置，传 None 表示不变。
        """
        monitor = self._monitors.get(key)
        if not monitor:
            return {"success": False, "error": f"合约 {key!r} 不在监控列表中"}
        if buy_target is not None:
            monitor._buy_target = None if buy_target == 0 else buy_target
        if sell_target is not None:
            monitor._sell_target = None if sell_target == 0 else sell_target
        if qty is not None and qty > 0:
            monitor._reversal_qty = qty
        logger.info(
            f"[{key}] 抄底/摸顶目标价更新: buy={monitor._buy_target} "
            f"sell={monitor._sell_target} qty={monitor._reversal_qty}"
        )
        return {
            "success": True,
            "buy_target": monitor._buy_target,
            "sell_target": monitor._sell_target,
            "reversal_qty": monitor._reversal_qty,
        }

    async def update_strategy_params(
        self,
        cooldown_sec: Optional[int] = None,
        signal_enabled: Optional[bool] = None,
    ) -> None:
        if cooldown_sec is not None:
            self.settings.strategy.signal_cooldown_sec = cooldown_sec
            for m in self._monitors.values():
                m._cooldown = cooldown_sec
        if signal_enabled is not None:
            self.settings.strategy.signal_enabled = signal_enabled
            for m in self._monitors.values():
                m._signal_enabled = signal_enabled
            save_settings(self.settings)
            logger.info(f"双K止损已{'启用' if signal_enabled else '暂停'}")
