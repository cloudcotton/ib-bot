"""止损信号检测（纯函数，无副作用）。

策略：双K止损平仓
  平多信号: 当前价 ≤ Kn 运行低点 且 当前价 < min(K-1.low, K-2.low)
  平空信号: 当前价 ≥ Kn 运行高点 且 当前价 > max(K-1.high, K-2.high)

条件一 (price <= Kn.low)：
    IB 推送的当前 bar 中，close = 最新成交价，low = 运行低点。
    close <= low 等价于 close == low，即当前价刚刚创下本 K 线新低。
条件二 (price < min(K-1.low, K-2.low))：
    确保新低真正有效突破了前两根 K 线（K-1、K-2）的支撑区域。
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from .kline_manager import KlineBuffer


class Signal(str, Enum):
    CLOSE_LONG = "CLOSE_LONG"
    CLOSE_SHORT = "CLOSE_SHORT"


def check_signal(buffer: KlineBuffer, position: float) -> Optional[Signal]:
    """基于当前缓冲区和持仓方向，返回止损信号或 None。

    Args:
        buffer:   合约的 K 线缓冲区
        position: 持仓数量（>0 多头，<0 空头，0 平仓）

    Returns:
        Signal.CLOSE_LONG / CLOSE_SHORT，或 None
    """
    if position == 0:
        return None

    data = buffer.get_signal_data()
    if data is None:
        return None

    kn, k1, k2 = data
    price = kn.close  # 当前价 = 进行中 K 线的最新收盘价

    if position > 0:  # 多头 → 监控平多信号
        if price <= kn.low and price < min(k1.low, k2.low):
            return Signal.CLOSE_LONG

    elif position < 0:  # 空头 → 监控平空信号
        if price >= kn.high and price > max(k1.high, k2.high):
            return Signal.CLOSE_SHORT

    return None
