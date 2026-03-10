"""止损信号检测（纯函数，无副作用）。

策略：双K止损平仓
  平多信号: Kn 运行低点 < min(K-1.low, K-2.low)
  平空信号: Kn 运行高点 > max(K-1.high, K-2.high)

判断依据是当前 K 线的运行极值（low/high），而非 close：
  - IB 的 bar 更新是批量的，close 是批次内最后一笔价格。
    若价格在批次内短暂突破止损阈值后回升，close > low，
    用 close 检测会漏掉这次突破。
  - kn.low 记录的是 K 线开始至今的最低价，一旦价格触及过
    阈值以下，kn.low 就会永久反映这一事实，直到 K 线收盘。
  - 因此只需检测 kn.low < min(K-1.low, K-2.low) 即可，
    无需同时要求 close == low。
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from .kline_manager import KlineBuffer


class Signal(str, Enum):
    CLOSE_LONG = "CLOSE_LONG"
    CLOSE_SHORT = "CLOSE_SHORT"


def check_signal(
    buffer: KlineBuffer,
    position: float,
    entry_bar_time: Optional[str] = None,
) -> Optional[Signal]:
    """基于当前缓冲区和持仓方向，返回止损信号或 None。

    Args:
        buffer:         合约的 K 线缓冲区
        position:       持仓数量（>0 多头，<0 空头，0 平仓）
        entry_bar_time: 开仓时所在 K 线的 time 字段。
                        开仓当根 K 线用 close 判断，避免开仓前已存在的极值误触发；
                        后续 K 线用 kn.low/kn.high 判断，能捕获批次内短暂突破。

    Returns:
        Signal.CLOSE_LONG / CLOSE_SHORT，或 None
    """
    if position == 0:
        return None

    data = buffer.get_signal_data()
    if data is None:
        return None

    kn, k1, k2 = data

    # 开仓当根 K 线：kn.low/kn.high 包含开仓前的价格运动，不能用来触发止损；
    # 改用 kn.close（当前价）比较，避免追溯开仓前历史极值。
    # 后续 K 线：kn.low/kn.high 只反映开仓后的行情，可直接与阈值比较。
    is_entry_bar = (entry_bar_time is not None and kn.time == entry_bar_time)

    if position > 0:  # 多头 → 监控平多信号
        ref = kn.close if is_entry_bar else kn.low
        if ref < min(k1.low, k2.low):
            return Signal.CLOSE_LONG

    elif position < 0:  # 空头 → 监控平空信号
        ref = kn.close if is_entry_bar else kn.high
        if ref > max(k1.high, k2.high):
            return Signal.CLOSE_SHORT

    return None
