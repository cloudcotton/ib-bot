"""止损信号检测（纯函数，无副作用）。

策略：双K止损平仓
  平多信号: Kn 运行低点 < min(K-1.low, K-2.low)
  平空信号: Kn 运行高点 > max(K-1.high, K-2.high)

判断逻辑（逐Tick检测，先检查后更新）：
  每笔 Tick 到达后，在 Kn.low/Kn.high 被更新之前先做比较：
    平多: tick_price < min(Kn.low_before, K-1.low, K-2.low)
    平空: tick_price > max(Kn.high_before, K-1.high, K-2.high)

  "先检查后更新"的含义：
    - Kn.low_before 是当前 K 线本 tick 之前的运行最低价
    - 只有当新 tick 真正突破"所有已知最低点"时才触发止损
    - 若价格曾经创新低后回升，回升过程中的 tick 不会再次触发：
        因为 Kn.low_before 已更新为历史最低，后续 tick > Kn.low_before
    - 抄底场景：K-1（抄底那根K线）的 low 就是阈值底线，
        开仓后只要价格不跌破该 low 就不触发——无需特判"开仓K线"
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from .kline_manager import Bar, KlineBuffer


class Signal(str, Enum):
    CLOSE_LONG = "CLOSE_LONG"
    CLOSE_SHORT = "CLOSE_SHORT"


def check_signal(
    buffer: KlineBuffer,
    position: float,
    tick_price: float,
) -> Optional[Signal]:
    """基于当前缓冲区、持仓方向和当前 tick 价格，返回止损信号或 None。

    必须在 TickBarBuilder.on_tick() 更新 Kn.low/Kn.high 之前调用，
    确保 buffer.current.low / high 反映的是本 tick 之前的极值。

    Args:
        buffer:     合约的 K 线缓冲区
        position:   持仓数量（>0 多头，<0 空头，0 无仓）
        tick_price: 当前 tick 成交价（即将成为新的 Kn.close）

    Returns:
        Signal.CLOSE_LONG / CLOSE_SHORT，或 None
    """
    if position == 0:
        return None

    data = buffer.get_signal_data()
    if data is None:
        return None

    kn, k1, k2 = data

    if position > 0:  # 多头 → 监控平多信号
        if tick_price < min(kn.low, k1.low, k2.low):
            return Signal.CLOSE_LONG

    elif position < 0:  # 空头 → 监控平空信号
        if tick_price > max(kn.high, k1.high, k2.high):
            return Signal.CLOSE_SHORT

    return None


def get_ema_zone(buffer: KlineBuffer, close_price: float) -> Optional[str]:
    """根据价格与 EMA20/40/60 的相对位置判断当前区间。

    Returns:
        'LONG'  — close > max(EMA20, EMA40, EMA60)
        'SHORT' — close < min(EMA20, EMA40, EMA60)
        None    — 横盘区间，或均线尚未就绪
    """
    ema20, ema40, ema60 = buffer.ema20, buffer.ema40, buffer.ema60
    if ema20 is None or ema40 is None or ema60 is None:
        return None
    if close_price > max(ema20, ema40, ema60):
        return "LONG"
    if close_price < min(ema20, ema40, ema60):
        return "SHORT"
    return None


def check_ema_signal(
    buffer: KlineBuffer,
    position: float,
    closed_bar: Bar,
) -> Optional[Signal]:
    """均线止损信号检测（K 线收盘时调用）。

    策略：K 线收盘价与 EMA20/40/60 的关系
      平空: close > max(EMA20, EMA40, EMA60)
      平多: close < min(EMA20, EMA40, EMA60)

    三条均线均可用时才触发，任一为 None 则跳过。

    Args:
        buffer:     合约的 K 线缓冲区（EMA 已在 bar 收盘时更新）
        position:   持仓数量（>0 多头，<0 空头，0 无仓）
        closed_bar: 刚刚收盘的 K 线

    Returns:
        Signal.CLOSE_LONG / CLOSE_SHORT，或 None
    """
    if position == 0:
        return None

    ema20, ema40, ema60 = buffer.ema20, buffer.ema40, buffer.ema60
    if ema20 is None or ema40 is None or ema60 is None:
        return None

    close = closed_bar.close

    if position < 0:  # 空头 → 收盘价高于所有均线则平空
        if close > max(ema20, ema40, ema60):
            return Signal.CLOSE_SHORT

    elif position > 0:  # 多头 → 收盘价低于所有均线则平多
        if close < min(ema20, ema40, ema60):
            return Signal.CLOSE_LONG

    return None
