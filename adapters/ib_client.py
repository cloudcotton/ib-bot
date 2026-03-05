"""IB API 适配器（基于 ib_insync）。

职责：
  - 连接 / 断开 TWS / IB Gateway
  - 合约自动寻找近月（reqContractDetails）
  - 订阅历史 K 线 + 实时更新（keepUpToDate=True）
  - 查询 / 订阅持仓变动（含均价）
  - 开仓（市价 / 限价）、止损单、撤单、平仓
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Callable, Optional

from ib_insync import (
    IB, BarDataList, Future, LimitOrder, MarketOrder,
    Position, StopOrder, util,
)

logger = logging.getLogger(__name__)

# ── IB barSizeSetting 映射 ────────────────────────────────────────────────

_BAR_SIZE: dict[str, str] = {
    "1m":  "1 min",
    "5m":  "5 mins",
    "15m": "15 mins",
    "30m": "30 mins",
    "1h":  "1 hour",
    "2h":  "2 hours",
    "4h":  "4 hours",
    "1d":  "1 day",
}

_DURATION: dict[str, str] = {
    "1m":  "1 D",
    "5m":  "2 D",
    "15m": "3 D",
    "30m": "5 D",
    "1h":  "5 D",
    "2h":  "10 D",
    "4h":  "20 D",
    "1d":  "1 M",
}


class IBClient:
    """轻量级 ib_insync 封装，供 TradingEngine 使用。"""

    def __init__(self, host: str, port: int, client_id: int, account: str = "") -> None:
        self._host = host
        self._port = port
        self._client_id = client_id
        self._account = account
        self.ib = IB()
        self._connected = False

        # 持仓缓存: conId -> (quantity, avg_cost)
        self._positions: dict[int, float] = {}
        self._avg_costs: dict[int, float] = {}

        # 持仓变动回调: (conId, qty, avg_cost)
        self._position_callbacks: list[Callable[[int, float, float], None]] = []

        # 账户数值缓存: "TAG/CURRENCY" -> value_str
        # 关注字段: NetLiquidation(总净值) / UnrealizedPnL(持仓浮盈) /
        #           RealizedPnL(当前session已实现) / AvailableFunds(可用资金)
        self._account_data: dict[str, str] = {}
        self.ib.accountValueEvent  += self._on_account_value   # 注册一次，重连后持续有效
        self.ib.disconnectedEvent  += self._on_ib_disconnected  # 断线时清空缓存

    # ── 连接管理 ──────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """连接到 TWS / IB Gateway（支持重连调用，幂等）。"""
        if self.ib.isConnected():
            logger.debug("IB 已连接，跳过")
            return

        logger.info(f"连接 IB: {self._host}:{self._port} clientId={self._client_id}")
        await self.ib.connectAsync(
            self._host,
            self._port,
            clientId=self._client_id,
            readonly=False,
        )
        self._connected = True
        logger.info("IB 连接成功")

        self.ib.positionEvent += self._on_position_event
        await self._fetch_initial_positions()

        # 订阅账户数值实时推送（重连后需重新订阅）
        # ib_insync 已隐藏 subscribe 参数，只传账户号即可
        self.ib.reqAccountUpdates(self._account or "")
        logger.info("已订阅账户数值更新")

    async def disconnect(self) -> None:
        if self._connected:
            self.ib.disconnect()
            self._connected = False
            logger.info("IB 已断开")

    @property
    def is_connected(self) -> bool:
        return self._connected and self.ib.isConnected()

    # ── 合约解析 ──────────────────────────────────────────────────────────

    async def resolve_contract(
        self,
        symbol: str,
        exchange: str,
        currency: str,
        expiry: str = "",
    ):
        """解析期货合约，expiry 为空时自动取近月合约。"""
        if expiry:
            contract = Future(symbol, expiry, exchange, currency=currency)
            qualified = await self.ib.qualifyContractsAsync(contract)
            if not qualified:
                raise ValueError(f"合约解析失败: {symbol} {expiry} {exchange}")
            return qualified[0]

        template = Future(symbol, "", exchange, currency=currency)
        details = await self.ib.reqContractDetailsAsync(template)
        if not details:
            raise ValueError(f"找不到合约详情: {symbol}@{exchange}")

        today = datetime.now().strftime("%Y%m%d")
        active = [
            d for d in details
            if d.contract.lastTradeDateOrContractMonth >= today
        ]
        if not active:
            active = details
        active.sort(key=lambda d: d.contract.lastTradeDateOrContractMonth)
        contract = active[0].contract
        logger.info(f"近月合约: {symbol}@{exchange} → 到期={contract.lastTradeDateOrContractMonth}")
        return contract

    # ── K 线订阅 ──────────────────────────────────────────────────────────

    def subscribe_bars(
        self,
        contract,
        timeframe: str,
        use_rth: bool,
        callback: Callable[[BarDataList, bool], None],
    ) -> BarDataList:
        bar_size = _BAR_SIZE.get(timeframe, "5 mins")
        duration = _DURATION.get(timeframe, "2 D")
        logger.info(f"订阅 K 线: {contract.symbol} {bar_size} duration={duration} useRTH={use_rth}")
        bars = self.ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow="TRADES",
            useRTH=use_rth,
            formatDate=1,
            keepUpToDate=True,
        )
        bars.updateEvent += callback
        return bars

    def cancel_bars(self, bars: BarDataList) -> None:
        self.ib.cancelHistoricalData(bars)

    # ── 持仓管理 ──────────────────────────────────────────────────────────

    def register_position_callback(self, cb: Callable[[int, float, float], None]) -> None:
        """注册持仓变动回调，参数为 (conId, quantity, avg_cost)。"""
        self._position_callbacks.append(cb)

    def get_position(self, con_id: int) -> float:
        return self._positions.get(con_id, 0.0)

    def get_avg_cost(self, con_id: int) -> float:
        return self._avg_costs.get(con_id, 0.0)

    async def _fetch_initial_positions(self) -> None:
        positions: list[Position] = await self.ib.reqPositionsAsync()
        self._positions.clear()
        self._avg_costs.clear()
        for p in positions:
            if self._account and p.account != self._account:
                continue
            self._positions[p.contract.conId] = p.position
            self._avg_costs[p.contract.conId] = p.avgCost
        logger.info(f"初始持仓: {self._positions}")

    def _on_position_event(self, position: Position) -> None:
        if self._account and position.account != self._account:
            return
        con_id = position.contract.conId
        qty = position.position
        avg_cost = position.avgCost
        self._positions[con_id] = qty
        self._avg_costs[con_id] = avg_cost
        logger.debug(f"持仓更新: conId={con_id} qty={qty} avgCost={avg_cost}")
        for cb in self._position_callbacks:
            cb(con_id, qty, avg_cost)

    # ── 账户数值 ──────────────────────────────────────────────────────────

    def _on_account_value(self, val) -> None:
        """IB 推送账户数值时缓存关键字段。"""
        if self._account and val.account != self._account:
            return
        if val.tag in ("NetLiquidation", "UnrealizedPnL", "RealizedPnL", "AvailableFunds"):
            self._account_data[f"{val.tag}/{val.currency}"] = val.value

    def _on_ib_disconnected(self) -> None:
        """IB 断线时立即清空账户数值缓存，防止前端在重连期间显示过期数据。"""
        self._account_data.clear()
        logger.debug("IB 断线：账户数值缓存已清空")

    def get_account_summary(self) -> dict:
        """返回账户关键数值快照，格式：{tag: {currency: value_str}}。

        示例：
            {
              "NetLiquidation":  {"USD": "500000.00"},
              "UnrealizedPnL":   {"USD": "1234.50"},
              "AvailableFunds":  {"USD": "450000.00"},
            }
        空字典表示尚未收到 IB 推送（连接后约 1~2 秒到达）。
        """
        result: dict[str, dict[str, str]] = {}
        for key, value in self._account_data.items():
            tag, currency = key.split("/", 1)
            result.setdefault(tag, {})[currency] = value
        return result

    # ── 开仓下单 ──────────────────────────────────────────────────────────

    def open_position(
        self,
        contract,
        direction: str,      # "long" | "short"
        qty: float,
        order_type: str,     # "market" | "limit"
        limit_price: Optional[float] = None,
    ):
        """提交开仓订单，返回 ib_insync Trade 对象（可监听 filledEvent）。"""
        action = "BUY" if direction == "long" else "SELL"

        if order_type == "market":
            order = MarketOrder(action, qty)
        elif order_type == "limit":
            if limit_price is None:
                raise ValueError("限价单必须提供 limit_price")
            order = LimitOrder(action, qty, limit_price)
        else:
            raise ValueError(f"不支持的订单类型: {order_type!r}")

        logger.info(f"开仓下单: {contract.symbol} {action} {qty} {order_type}"
                    + (f" @ {limit_price}" if limit_price else ""))
        return self.ib.placeOrder(contract, order)

    # ── 止损单管理 ────────────────────────────────────────────────────────

    def place_stop_order(
        self,
        contract,
        position_direction: str,   # "long" | "short"（决定止损方向）
        qty: float,
        stop_price: float,
    ):
        """提交 IB 原生止损单（止损市价单 STP）。

        多头止损 → SELL STP；空头止损 → BUY STP。
        IB 服务端执行，即使 Bot 断线仍然有效。
        """
        action = "SELL" if position_direction == "long" else "BUY"
        order = StopOrder(action, qty, stop_price)
        logger.info(f"止损单: {contract.symbol} {action} {qty} STP @ {stop_price}")
        return self.ib.placeOrder(contract, order)

    def cancel_order(self, trade) -> None:
        """撤销指定订单（传入 Trade 对象）。"""
        try:
            self.ib.cancelOrder(trade.order)
            logger.info(f"撤单: orderId={trade.order.orderId}")
        except Exception as e:
            logger.warning(f"撤单失败: {e}")

    def modify_stop_price(self, trade, new_stop_price: float) -> None:
        """修改止损单价格（原地改价，IB 会更新现有止损单）。"""
        trade.order.auxPrice = new_stop_price
        self.ib.placeOrder(trade.contract, trade.order)
        logger.info(f"修改止损价: orderId={trade.order.orderId} → {new_stop_price}")

    # ── 市价平仓 ──────────────────────────────────────────────────────────

    async def close_position(
        self,
        contract,
        position_qty: float,
        order_type: str = "market",
        limit_price: Optional[float] = None,
    ) -> bool:
        """平掉当前持仓（市价或限价）。"""
        if position_qty == 0:
            logger.warning(f"close_position 调用时持仓为 0，跳过: {contract.symbol}")
            return False

        action = "SELL" if position_qty > 0 else "BUY"
        qty = abs(position_qty)

        if order_type == "limit":
            if limit_price is None:
                raise ValueError("限价平仓必须提供 limit_price")
            order = LimitOrder(action, qty, limit_price)
            logger.info(f"下单平仓: {contract.symbol} {action} {qty} @ LIMIT {limit_price}")
            try:
                self.ib.placeOrder(contract, order)
                return True
            except Exception as e:
                logger.error(f"平仓下单异常: {contract.symbol}: {e}")
                return False
        else:
            order = MarketOrder(action, qty)
            logger.info(f"下单平仓: {contract.symbol} {action} {qty} @ MARKET")
            try:
                trade = self.ib.placeOrder(contract, order)

                # 用 Future + 事件回调代替轮询，响应更及时
                loop = asyncio.get_running_loop()
                fut: asyncio.Future[bool] = loop.create_future()

                def _on_filled(t):
                    if not fut.done():
                        fut.set_result(True)

                def _on_cancelled(t):
                    if not fut.done():
                        fut.set_result(False)

                trade.filledEvent += _on_filled
                trade.cancelledEvent += _on_cancelled

                try:
                    success = await asyncio.wait_for(fut, timeout=30.0)
                    if success:
                        logger.info(
                            f"平仓完成: {contract.symbol} avgFill={trade.orderStatus.avgFillPrice}"
                        )
                    else:
                        logger.warning(f"平仓订单被取消: {contract.symbol}")
                    return success
                except asyncio.TimeoutError:
                    logger.warning(f"平仓订单 30s 内未成交: {contract.symbol}")
                    return False
                finally:
                    trade.filledEvent -= _on_filled
                    trade.cancelledEvent -= _on_cancelled

            except Exception as e:
                logger.error(f"平仓下单异常: {contract.symbol}: {e}")
                return False
