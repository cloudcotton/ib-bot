"""核心业务 REST API 路由。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from web.schemas import (
    CancelStaticStopRequest,
    CancelTakeProfitRequest,
    HealthResponse,
    ManualCloseRequest,
    OpenPositionRequest,
    SetReversalRequest,
    SetStaticStopRequest,
    SetTakeProfitRequest,
    UpdateNotifyRequest,
    UpdateStrategyRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


def _engine(request: Request):
    engine = request.app.state.engine
    if engine is None:
        raise HTTPException(503, "引擎未就绪")
    return engine


# ── 状态 ──────────────────────────────────────────────────────────────────


@router.get("/healthz", response_model=HealthResponse)
async def healthz(request: Request):
    e = _engine(request)
    return HealthResponse(connected=e.ib_client.is_connected, running=e._running)


@router.get("/status")
async def status(request: Request):
    """完整运行状态快照（前端每秒轮询）。"""
    return _engine(request).get_status()


# ── 开仓 ──────────────────────────────────────────────────────────────────


@router.post("/trade/open")
async def open_position(body: OpenPositionRequest, request: Request):
    """开仓（市价/限价），可同时设置止损价。

    止损价设置后，开仓成交时自动挂 IB 原生止损单（服务端执行）。
    """
    e = _engine(request)
    key = f"{body.symbol}@{body.exchange}"

    if body.direction not in ("long", "short"):
        raise HTTPException(400, "direction 必须是 'long' 或 'short'")
    if body.order_type not in ("market", "limit"):
        raise HTTPException(400, "order_type 必须是 'market' 或 'limit'")
    if body.order_type == "limit" and body.limit_price is None:
        raise HTTPException(400, "限价单必须提供 limit_price")
    if body.qty <= 0:
        raise HTTPException(400, "qty 必须大于 0")

    result = await e.open_position(
        key=key,
        direction=body.direction,
        qty=body.qty,
        order_type=body.order_type,
        limit_price=body.limit_price,
        stop_price=body.stop_price,
        take_profit_price=body.take_profit_price,
    )
    if not result.get("success"):
        raise HTTPException(400, result.get("error", "开仓失败"))
    return result


# ── 静态止损管理 ──────────────────────────────────────────────────────────


@router.post("/trade/set_stop")
async def set_stop(body: SetStaticStopRequest, request: Request):
    """设置/修改静态止损价（bot 端每 tick 检测，无论是否持仓均有效）。"""
    e = _engine(request)
    key = f"{body.symbol}@{body.exchange}"
    if body.long_stop is None and body.short_stop is None:
        raise HTTPException(400, "至少提供 long_stop 或 short_stop 之一")
    result = await e.set_static_stop(key, long_stop=body.long_stop, short_stop=body.short_stop)
    if not result.get("success"):
        raise HTTPException(400, result.get("error", "设置止损失败"))
    return result


@router.post("/trade/cancel_stop")
async def cancel_stop(body: CancelStaticStopRequest, request: Request):
    """撤销静态止损价（不平仓）。side: 'long' | 'short' | 'both'"""
    e = _engine(request)
    key = f"{body.symbol}@{body.exchange}"
    if body.side not in ("long", "short", "both"):
        raise HTTPException(400, "side 必须是 'long'、'short' 或 'both'")
    result = await e.cancel_static_stop(key, side=body.side)
    if not result.get("success"):
        raise HTTPException(400, result.get("error", "撤止损失败"))
    return result


# ── 止盈管理 ──────────────────────────────────────────────────────────────


@router.post("/trade/set_tp")
async def set_take_profit(body: SetTakeProfitRequest, request: Request):
    """为当前持仓设置或修改止盈价（重新挂 IB 限价止盈单）。"""
    e = _engine(request)
    key = f"{body.symbol}@{body.exchange}"
    result = await e.set_take_profit(key, body.take_profit_price)
    if not result.get("success"):
        raise HTTPException(400, result.get("error", "设置止盈失败"))
    return result


@router.post("/trade/cancel_tp")
async def cancel_take_profit(body: CancelTakeProfitRequest, request: Request):
    """撤销当前止盈单（不平仓）。"""
    e = _engine(request)
    key = f"{body.symbol}@{body.exchange}"
    result = await e.cancel_take_profit(key)
    if not result.get("success"):
        raise HTTPException(400, result.get("error", "撤止盈失败"))
    return result


# ── 平仓 ──────────────────────────────────────────────────────────────────


@router.post("/trade/close")
async def manual_close(body: ManualCloseRequest, request: Request):
    """手动平仓（市价或限价），同时自动撤销止损单。"""
    e = _engine(request)
    key = f"{body.symbol}@{body.exchange}"
    if body.order_type not in ("market", "limit"):
        raise HTTPException(400, "order_type 必须是 'market' 或 'limit'")
    if body.order_type == "limit" and body.limit_price is None:
        raise HTTPException(400, "限价平仓必须提供 limit_price")
    result = await e.manual_close(key, body.order_type, body.limit_price)
    if not result.get("success"):
        raise HTTPException(400, result.get("error", "平仓失败"))
    return result


# ── 参数热更新 ──────────────────────────────────────────────────────────────


@router.post("/params/reversal")
async def set_reversal(body: SetReversalRequest, request: Request):
    """设置/清除指定合约的抄底/摸顶目标价及开仓手数。"""
    e = _engine(request)
    key = f"{body.symbol}@{body.exchange}"
    result = await e.set_reversal_prices(
        key=key,
        buy_target=body.buy_target,
        sell_target=body.sell_target,
        qty=body.qty,
    )
    if not result.get("success"):
        raise HTTPException(400, result.get("error", "设置失败"))
    return result


@router.post("/params/strategy")
async def update_strategy(body: UpdateStrategyRequest, request: Request):
    e = _engine(request)
    await e.update_strategy_params(
        cooldown_sec=body.signal_cooldown_sec,
        signal_enabled=body.signal_enabled,
    )
    return {"success": True, "strategy": e.settings.strategy.model_dump()}


@router.post("/params/notify")
async def update_notify(body: UpdateNotifyRequest, request: Request):
    e = _engine(request)
    nc = e.settings.notify
    if body.enabled is not None:
        nc.enabled = body.enabled
    if body.telegram_bot_token is not None:
        nc.telegram_bot_token = body.telegram_bot_token
    if body.telegram_chat_id is not None:
        nc.telegram_chat_id = body.telegram_chat_id
    if body.min_interval_sec is not None:
        nc.min_interval_sec = body.min_interval_sec
    if e._notifier:
        e._notifier._token = nc.telegram_bot_token
        e._notifier._chat_id = nc.telegram_chat_id
        e._notifier._min_interval = nc.min_interval_sec
    return {"success": True}


@router.post("/notify/test")
async def test_notify(request: Request):
    e = _engine(request)
    if not e._notifier or not e._notifier.enabled:
        raise HTTPException(400, "Telegram 未配置")
    sent = e._notifier.send("🔔 IB Bot 测试通知")
    return {"sent": sent}
