"""
Zettaranc 技术分析模块包
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# ─── 全局一次性加载 .env（包首次 import 时执行）───────────────────────────────
# 优先读取环境变量指向的路径，其次查找项目根目录的 .env
_env_path = Path(os.getenv("ZETTARANC_ENV", Path(__file__).parent.parent / ".env"))
load_dotenv(_env_path, override=False)  # 已有的环境变量不被 .env 覆盖（保持测试 fixture 隔离能力）


# ─── 公开 API ────────────────────────────────────────────────────────────────
from .database import get_connection, get_db_path, init_database  # noqa: E402
from .tushare_client import TushareClient  # noqa: E402

# 交易记录模块（数据准备层）
from .trade_parser import TradeParser, ParseResult, format_trade_for_review  # noqa: E402
from .trade_manager import TradeManager, trade_manager  # noqa: E402

__all__ = [
    # 数据库
    "get_connection",
    "get_db_path",
    "init_database",
    # Tushare
    "TushareClient",
    # 交易记录（数据层）
    "TradeParser",
    "ParseResult",
    "format_trade_for_review",
    "TradeManager",
    "trade_manager",
]


def get_data_mode() -> str:
    """获取当前数据模式：jnb 或 websearch"""
    return os.getenv("DATA_MODE", "websearch")


def get_project_root() -> Path:
    """获取项目根目录（modules/ 的上一级）"""
    return Path(__file__).parent.parent
