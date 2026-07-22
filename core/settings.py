"""配置加载与持久化。

运行时所有模块通过 get_settings() 获取唯一的 Settings 单例。
支持通过 update() 热更新部分字段（写回 config.yaml）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, field_validator

load_dotenv()

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"

# ── Pydantic 模型 ──────────────────────────────────────────────────────────


class IBConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 1


class ContractConfig(BaseModel):
    symbol: str
    exchange: str
    currency: str
    timeframe: str = "5m"
    expiry: str = ""          # "" = 自动取主力合约；"202506" = 强制指定到期月
    roll_days: int = 7        # 到期前多少天自动切换到下一合约（仅 expiry="" 时有效）
    multiplier: float = 1.0   # 合约乘数（静态配置，优先于 IB 动态返回）
    min_tick: float = 0.01    # 最小价格变动单位（用于止损/止盈 Ticks 计算）
    use_rth: bool = False
    enabled: bool = True
    signal_enabled: Optional[bool] = None       # None = 使用全局默认
    ema_stop_enabled: Optional[bool] = None     # None = 使用全局默认
    ema_reversal_enabled: Optional[bool] = None # None = 使用全局默认
    ema_reversal_qty: Optional[float] = None    # None = 使用全局默认

    @field_validator("timeframe")
    @classmethod
    def _validate_tf(cls, v: str) -> str:
        valid = {"1m", "5m", "15m", "30m", "1h", "2h", "4h", "1d"}
        if v not in valid:
            raise ValueError(f"timeframe 必须是 {valid} 之一，当前值: {v!r}")
        return v

    @property
    def key(self) -> str:
        return f"{self.symbol}@{self.exchange}"


class StrategyConfig(BaseModel):
    signal_enabled: bool = True      # 双K止损信号总开关
    signal_cooldown_sec: int = 30
    ema_stop_enabled: bool = False   # 均线止损（EMA20/40/60）总开关
    ema_reversal_enabled: bool = False  # EMA反转开仓策略总开关
    ema_reversal_qty: float = 1.0       # EMA反转开仓默认手数


class NotifyConfig(BaseModel):
    enabled: bool = True
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    min_interval_sec: int = 10


class EquityLogConfig(BaseModel):
    enabled: bool = True
    record_time: str = "17:00"    # 每日记录时刻，"HH:MM"，与服务器本地时区一致


class Settings(BaseModel):
    ib: IBConfig = IBConfig()
    contracts: list[ContractConfig] = []
    strategy: StrategyConfig = StrategyConfig()
    notify: NotifyConfig = NotifyConfig()
    equity_log: EquityLogConfig = EquityLogConfig()

    # 账户（从 .env 读取）
    ib_account: str = ""

    @property
    def active_contracts(self) -> list[ContractConfig]:
        return [c for c in self.contracts if c.enabled]


# ── 加载 ──────────────────────────────────────────────────────────────────

def _load_raw() -> dict:
    if not _CONFIG_PATH.exists():
        return {}
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_settings() -> Settings:
    raw = _load_raw()

    # 环境变量覆盖 IB 连接参数
    ib_section = raw.get("ib", {})
    if os.getenv("IB_HOST"):
        ib_section["host"] = os.environ["IB_HOST"]
    if os.getenv("IB_PORT"):
        ib_section["port"] = int(os.environ["IB_PORT"])
    if os.getenv("IB_CLIENT_ID"):
        ib_section["client_id"] = int(os.environ["IB_CLIENT_ID"])
    raw["ib"] = ib_section

    # 环境变量覆盖通知参数
    notify_section = raw.get("notify", {})
    if os.getenv("TELEGRAM_BOT_TOKEN"):
        notify_section["telegram_bot_token"] = os.environ["TELEGRAM_BOT_TOKEN"]
    if os.getenv("TELEGRAM_CHAT_ID"):
        notify_section["telegram_chat_id"] = os.environ["TELEGRAM_CHAT_ID"]
    raw["notify"] = notify_section

    settings = Settings(**raw)
    settings.ib_account = os.getenv("IB_ACCOUNT", "")
    return settings


def save_settings(settings: Settings) -> None:
    """将当前 settings 写回 config.yaml（保留注释格式存在局限，直接覆盖）。"""
    raw = _load_raw()
    raw["ib"] = settings.ib.model_dump()
    raw["strategy"] = settings.strategy.model_dump()
    raw["notify"] = {
        k: v for k, v in settings.notify.model_dump().items()
        if k not in ("telegram_bot_token", "telegram_chat_id")  # 敏感项不写文件
    }
    # 持久化合约级策略开关（None 表示使用全局默认，不写入文件）
    contracts_raw = raw.get("contracts", [])
    for cfg in settings.contracts:
        for i, raw_c in enumerate(contracts_raw):
            if raw_c.get("symbol") == cfg.symbol and raw_c.get("exchange") == cfg.exchange:
                if cfg.signal_enabled is not None:
                    contracts_raw[i]["signal_enabled"] = cfg.signal_enabled
                else:
                    contracts_raw[i].pop("signal_enabled", None)
                if cfg.ema_stop_enabled is not None:
                    contracts_raw[i]["ema_stop_enabled"] = cfg.ema_stop_enabled
                else:
                    contracts_raw[i].pop("ema_stop_enabled", None)
                if cfg.ema_reversal_enabled is not None:
                    contracts_raw[i]["ema_reversal_enabled"] = cfg.ema_reversal_enabled
                else:
                    contracts_raw[i].pop("ema_reversal_enabled", None)
                if cfg.ema_reversal_qty is not None:
                    contracts_raw[i]["ema_reversal_qty"] = cfg.ema_reversal_qty
                else:
                    contracts_raw[i].pop("ema_reversal_qty", None)
                break
    raw["contracts"] = contracts_raw
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(raw, f, allow_unicode=True, sort_keys=False)


# ── 单例 ──────────────────────────────────────────────────────────────────

_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings
