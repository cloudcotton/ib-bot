"""Telegram 通知模块。

未配置 token / chat_id 时静默跳过，不影响主流程。
min_interval_sec 限制最小发送间隔，防止消息风暴。
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        min_interval_sec: int = 10,
    ) -> None:
        self._token = bot_token.strip()
        self._chat_id = chat_id.strip()
        self._min_interval = min_interval_sec
        self._last_sent: float = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._chat_id)

    def send(self, text: str) -> bool:
        """发送 Telegram 消息。返回是否成功发送（False = 未配置或被限流）。"""
        if not self.enabled:
            return False

        now = time.time()
        if now - self._last_sent < self._min_interval:
            logger.debug("Telegram 消息被限流，跳过")
            return False

        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        try:
            resp = httpx.post(
                url,
                json={"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            resp.raise_for_status()
            self._last_sent = now
            return True
        except Exception as e:
            logger.warning(f"Telegram 发送失败: {e}")
            return False

    # ── 预定义消息模板 ──────────────────────────────────────────────────────

    def notify_close(
        self,
        symbol: str,
        exchange: str,
        direction: str,
        qty: float,
        price: float,
    ) -> None:
        emoji = "🔴" if direction == "CLOSE_LONG" else "🟢"
        action = "平多" if direction == "CLOSE_LONG" else "平空"
        text = (
            f"{emoji} <b>止损平仓</b>\n"
            f"合约: <code>{symbol}@{exchange}</code>\n"
            f"方向: {action}\n"
            f"数量: {qty}\n"
            f"触发价: {price}"
        )
        self.send(text)

    def notify_connected(self, host: str, port: int) -> None:
        self.send(f"✅ IB 连接成功 <code>{host}:{port}</code>")

    def notify_disconnected(self) -> None:
        self.send("⚠️ IB 连接断开，正在重连…")

    def notify_error(self, msg: str) -> None:
        self.send(f"❌ 错误: {msg}")


# ── 全局单例（由 engine 初始化后赋值）──────────────────────────────────────
_notifier: Optional[TelegramNotifier] = None


def get_notifier() -> Optional[TelegramNotifier]:
    return _notifier


def init_notifier(token: str, chat_id: str, min_interval: int) -> TelegramNotifier:
    global _notifier
    _notifier = TelegramNotifier(token, chat_id, min_interval)
    return _notifier
