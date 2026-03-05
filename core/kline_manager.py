"""K 线缓冲区管理。

KlineBuffer  — 单个合约的 K 线缓冲：最多 25 根已完成 + 1 根进行中 + EMA20
KlineManager — 所有合约缓冲的字典管理器

completed deque(maxlen=25):
    [0]  = 最早保留的已完成 K 线
    [-2] = K-2（倒数第 2 根已完成）
    [-1] = K-1（最新已完成）
current = Kn（正在成形，每 tick 更新）

EMA20（20 周期指数移动平均）：
  - 前 20 根 K 线用 SMA 作为种子值
  - 后续每根已完成 K 线按标准 EMA 公式递推：
      EMA_t = close_t × k + EMA_{t-1} × (1 - k)，k = 2 / (20 + 1)
  - 未满 20 根时 ema20 为 None（不可用）

信号触发条件（双K止损）：
  平多: current.close ≤ current.low  AND  current.close < min(K-1.low, K-2.low)
  平空: current.close ≥ current.high AND  current.close > max(K-1.high, K-2.high)
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional

_EMA_PERIOD = 20
_EMA_K = 2.0 / (_EMA_PERIOD + 1)   # EMA 平滑系数 ≈ 0.0952


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
    """维护单个 (合约, 周期) 的 K 线滚动缓冲，含 EMA20 实时计算。"""

    def __init__(self) -> None:
        self.completed: deque[Bar] = deque(maxlen=25)  # 保留最近 25 根（EMA20 需 20 根种子）
        self.current: Optional[Bar] = None
        self.ema20: Optional[float] = None             # 20 周期 EMA，不足 20 根时为 None

    # ── 写入接口 ───────────────────────────────────────────────────────────

    def add_completed(self, bar: Bar) -> None:
        """追加一根已完成的 K 线，并递推更新 EMA20。"""
        self.completed.append(bar)
        self._update_ema(bar.close)

    def _update_ema(self, close: float) -> None:
        """EMA20 递推计算：
        - 前 20 根满员时以 SMA 作为初始种子
        - 之后每根按 EMA_t = close × k + EMA_{t-1} × (1-k) 递推
        """
        if self.ema20 is None:
            if len(self.completed) >= _EMA_PERIOD:
                # 用最新 20 根收盘价的简单均值初始化 EMA
                seed_closes = [b.close for b in list(self.completed)[-_EMA_PERIOD:]]
                self.ema20 = sum(seed_closes) / _EMA_PERIOD
        else:
            self.ema20 = close * _EMA_K + self.ema20 * (1 - _EMA_K)

    def update_current(self, bar: Bar) -> None:
        """更新正在成形的 K 线（每个 tick）。"""
        self.current = bar

    def reset(self) -> None:
        """断线重连后清空缓冲，重新填充。"""
        self.completed.clear()
        self.current = None
        self.ema20 = None

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
