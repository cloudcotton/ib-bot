"""日志初始化：控制台 + 按天轮转文件，统一 UTC+8 时间戳。"""

import logging
import logging.handlers
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_HKT = ZoneInfo("Asia/Hong_Kong")


class _HKTFormatter(logging.Formatter):
    """在日志记录中使用 UTC+8 时间。"""

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=_HKT)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat(timespec="seconds")


def setup_logging(log_dir: str = "logs", level: int = logging.INFO) -> None:
    Path(log_dir).mkdir(exist_ok=True)

    fmt = _HKTFormatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    # 控制台
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)

    # 按天轮转文件
    fh = logging.handlers.TimedRotatingFileHandler(
        filename=f"{log_dir}/ib-bot.log",
        when="midnight",
        backupCount=30,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # 静默 ib_insync 的 DEBUG 噪音
    logging.getLogger("ib_insync").setLevel(logging.WARNING)

    # 过滤 /api/status 每秒轮询产生的 access 日志
    class _StatusFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return "GET /api/status" not in record.getMessage()

    logging.getLogger("uvicorn.access").addFilter(_StatusFilter())
