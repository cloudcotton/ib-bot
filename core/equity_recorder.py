"""账户净值定时记录器。

每天在指定时刻（默认 17:00 本地时间）将账户净值写入 CSV 文件，
便于追踪权益变化趋势。

记录文件：data/equity_log.csv
字段：date, time, net_liquidation, currency
"""

from __future__ import annotations

import asyncio
import csv
import logging
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adapters.ib_client import IBClient

logger = logging.getLogger(__name__)

_LOG_PATH = Path(__file__).parent.parent / "data" / "equity_log.csv"
_HEADER = ["date", "time", "net_liquidation", "currency"]


class EquityRecorder:
    """后台异步任务：每天定时记录账户净值到 CSV。"""

    def __init__(self, ib_client: IBClient, record_time: str = "17:00") -> None:
        self._ib = ib_client
        # record_time 格式 "HH:MM"，与服务器本地时区一致
        hour, minute = record_time.split(":")
        self._record_hour = int(hour)
        self._record_minute = int(minute)
        self._last_recorded: date | None = None
        self._task: asyncio.Task | None = None

        # 确保数据目录和文件头存在
        _LOG_PATH.parent.mkdir(exist_ok=True)
        if not _LOG_PATH.exists():
            with open(_LOG_PATH, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(_HEADER)
            logger.info(f"净值记录文件已创建: {_LOG_PATH}")

    # ── 生命周期 ───────────────────────────────────────────────────────────

    def start(self) -> None:
        """启动后台定时任务。"""
        self._task = asyncio.ensure_future(self._loop())
        logger.info(
            f"净值记录器已启动，每日 {self._record_hour:02d}:{self._record_minute:02d} 记录"
        )

    def stop(self) -> None:
        """停止后台定时任务。"""
        if self._task and not self._task.done():
            self._task.cancel()

    # ── 核心循环 ───────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        """每分钟唤醒一次，检查是否到达记录时刻。"""
        while True:
            try:
                await asyncio.sleep(60)
                now = datetime.now()
                if (
                    now.hour == self._record_hour
                    and now.minute == self._record_minute
                    and self._last_recorded != now.date()
                ):
                    self._record(now)
                    self._last_recorded = now.date()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"净值记录异常: {e}", exc_info=e)

    # ── 写入 ───────────────────────────────────────────────────────────────

    def _record(self, now: datetime) -> None:
        """读取当前账户净值并追加到 CSV。"""
        summary = self._ib.get_account_summary()
        net_liq = summary.get("NetLiquidation", {})
        if not net_liq:
            logger.warning("净值记录跳过：尚未收到 IB 账户数值推送")
            return

        # BASE = 所有币种折算后的合并总值，优先使用
        value = (
            net_liq.get("BASE")
            or net_liq.get("USD")
            or net_liq.get("HKD")
            or next(iter(net_liq.values()), None)
        )
        currency = next(
            (k for k in ("BASE", "USD", "HKD") if k in net_liq),
            next(iter(net_liq.keys()), ""),
        )

        if value is None:
            logger.warning("净值记录跳过：NetLiquidation 值为空")
            return

        with open(_LOG_PATH, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                now.strftime("%Y-%m-%d"),
                now.strftime("%H:%M:%S"),
                value,
                currency,
            ])
        logger.info(f"账户净值已记录: {value} {currency} → {_LOG_PATH.name}")
