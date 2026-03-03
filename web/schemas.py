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


class ManualCloseRequest(BaseModel):
    symbol: str
    exchange: str
    order_type: str = "market"          # "market" | "limit"
    limit_price: Optional[float] = None


class SetStopRequest(BaseModel):
    symbol: str
    exchange: str
    stop_price: float


class CancelStopRequest(BaseModel):
    symbol: str
    exchange: str


class UpdateStrategyRequest(BaseModel):
    signal_cooldown_sec: Optional[int] = None
    signal_enabled: Optional[bool] = None


class UpdateNotifyRequest(BaseModel):
    enabled: Optional[bool] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    min_interval_sec: Optional[int] = None
