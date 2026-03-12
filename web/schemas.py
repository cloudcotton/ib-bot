"""API 请求 / 响应的 Pydantic 模型。"""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel


class HealthResponse(BaseModel):
    connected: bool
    running: bool


class OpenPositionRequest(BaseModel):
    symbol: str
    exchange: str
    direction: str          # "long" | "short"
    qty: float
    order_type: str = "market"   # "market" | "limit"
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    take_profit_price: Optional[float] = None


class ManualCloseRequest(BaseModel):
    symbol: str
    exchange: str
    order_type: str = "market"          # "market" | "limit"
    limit_price: Optional[float] = None


class SetStaticStopRequest(BaseModel):
    symbol: str
    exchange: str
    long_stop: Optional[float] = None    # None=不变, 0=清除, >0=设置多头止损
    short_stop: Optional[float] = None   # None=不变, 0=清除, >0=设置空头止损


class CancelStaticStopRequest(BaseModel):
    symbol: str
    exchange: str
    side: str = "both"   # "long" | "short" | "both"



class UpdateStrategyRequest(BaseModel):
    signal_cooldown_sec: Optional[int] = None
    signal_enabled: Optional[bool] = None


class SetReversalRequest(BaseModel):
    symbol: str
    exchange: str
    buy_target: Optional[float] = None   # None=不变, 0=清空, >0=设置抄底价
    sell_target: Optional[float] = None  # None=不变, 0=清空, >0=设置摸顶价
    qty: Optional[float] = None          # None=不变, >0=更新开仓手数


class UpdateNotifyRequest(BaseModel):
    enabled: Optional[bool] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    min_interval_sec: Optional[int] = None
