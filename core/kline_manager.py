"""K 线缓冲区管理。

KlineBuffer  — 单个合约的 K 线缓冲：2 根已完成 + 1 根进行中
KlineManager — 所有合约缓冲的字典管理器

设计与 okx-exit-bot 保持一致：
  completed deque(maxlen=2):
      [0] = K-2（较早已完成）
      [1] = K-1（最新已完成）
  current = Kn（正在成形，每 tick 更新）

信号触发条件（双K止损）：
  平多: current.close ≤ current.low  AND  current.close < min(K-1.low, K-2.low)
  平空: current.close ≥ current.high AND  current.close > max(K-1.high, K-2.high)
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional


@dataclass
class Bar:
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: float

    def to_dict(self) -> dict:
        return {
            "time": self.time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


class KlineBuffer:
    """维护单个 (合约, 周期) 的 K 线滚动缓冲。"""

    def __init__(self) -> None:
        self.completed: deque[Bar] = deque(maxlen=2)
        self.current: Optional[Bar] = None

    # ── 写入接口 ───────────────────────────────────────────────────────────

    def add_completed(self, bar: Bar) -> None:
        """追加一根已完成的 K 线（K-1 变为新的 K-1，旧 K-1 变为 K-2）。"""
        self.completed.append(bar)

    def update_current(self, bar: Bar) -> None:
        """更新正在成形的 K 线（每个 tick）。"""
        self.current = bar

    def reset(self) -> None:
        """断线重连后清空缓冲，重新填充。"""
        self.completed.clear()
        self.current = None

    # ── 读取接口 ───────────────────────────────────────────────────────────

    @property
    def ready(self) -> bool:
        """缓冲区就绪：至少 2 根已完成 K 线 + 1 根进行中。"""
        return len(self.completed) >= 2 and self.current is not None

    def get_signal_data(self) -> Optional[tuple[Bar, Bar, Bar]]:
        """返回 (Kn, K-1, K-2)，未就绪时返回 None。"""
        if not self.ready:
            return None
        k2, k1 = self.completed[0], self.completed[1]
        return self.current, k1, k2


class KlineManager:
    """所有合约 KlineBuffer 的集中管理器。"""

    def __init__(self) -> None:
        self._buffers: dict[str, KlineBuffer] = {}

    def get_or_create(self, key: str) -> KlineBuffer:
        if key not in self._buffers:
            self._buffers[key] = KlineBuffer()
        return self._buffers[key]

    def get(self, key: str) -> Optional[KlineBuffer]:
        return self._buffers.get(key)

    def reset(self, key: str) -> None:
        buf = self._buffers.get(key)
        if buf:
            buf.reset()
