"""
日志系统模块
使用 loguru 实现统一的日志格式和输出
"""

import sys
from pathlib import Path
from typing import Optional

from loguru import logger

from .config import config


def setup_logger(
    log_level: Optional[str] = None,
    log_dir: Optional[Path] = None
) -> None:
    """
    配置日志系统

    Args:
        log_level: 日志级别，默认从配置文件读取
        log_dir: 日志目录，默认为项目根目录下的 logs/
    """
    # 移除默认处理器
    logger.remove()

    # 获取配置
    logging_config = config.logging_config
    level = log_level or logging_config.get('level', 'INFO')
    console_output = logging_config.get('console', True)
    file_output = logging_config.get('file', True)
    rotation = logging_config.get('rotation', '10 MB')
    retention = logging_config.get('retention', '7 days')

    # 日志格式
    console_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
        "<level>{message}</level>"
    )

    file_format = (
        "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
        "{level: <8} | "
        "{name}:{function}:{line} | "
        "{message}"
    )

    # 交易信号专用格式
    signal_format = (
        "{time:YYYY-MM-DD HH:mm:ss} | "
        "{message}"
    )

    # 控制台输出
    if console_output:
        logger.add(
            sys.stderr,
            format=console_format,
            level=level,
            colorize=True,
            filter=lambda record: "signal" not in record["extra"]
        )

        # 信号日志单独格式（更简洁）
        logger.add(
            sys.stderr,
            format="<yellow>{time:HH:mm:ss}</yellow> | <level>{message}</level>",
            level="INFO",
            colorize=True,
            filter=lambda record: "signal" in record["extra"]
        )

    # 文件输出
    if file_output:
        if log_dir is None:
            log_dir = Path(__file__).parent.parent / 'logs'
        log_dir.mkdir(parents=True, exist_ok=True)

        # 主日志文件
        logger.add(
            log_dir / "trading.log",
            format=file_format,
            level=level,
            rotation=rotation,
            retention=retention,
            encoding='utf-8',
            filter=lambda record: "signal" not in record["extra"]
        )

        # 信号日志文件（独立记录所有交易信号）
        logger.add(
            log_dir / "signals.log",
            format=signal_format,
            level="INFO",
            rotation=rotation,
            retention=retention,
            encoding='utf-8',
            filter=lambda record: "signal" in record["extra"]
        )

        # 错误日志文件（单独记录错误）
        logger.add(
            log_dir / "errors.log",
            format=file_format,
            level="ERROR",
            rotation=rotation,
            retention=retention,
            encoding='utf-8'
        )


def get_logger(name: str = "shiit"):
    """
    获取带有模块名称的日志器

    Args:
        name: 模块名称

    Returns:
        配置好的 logger 实例
    """
    return logger.bind(name=name)


def log_signal(
    signal_type: str,
    symbol: str,
    **kwargs
) -> None:
    """
    记录交易信号

    Args:
        signal_type: 信号类型 (SIGNAL, ORDER, EXIT, etc.)
        symbol: 交易对
        **kwargs: 其他参数
    """
    # 格式化参数
    params = " | ".join([f"{k}: {v}" for k, v in kwargs.items()])
    message = f"{signal_type: <6} | {symbol: <12} | {params}"
    logger.bind(signal=True).info(message)


def log_order(
    action: str,
    symbol: str,
    price: float,
    quantity: float,
    **kwargs
) -> None:
    """
    记录订单信息

    Args:
        action: 操作类型 (BUY, SELL)
        symbol: 交易对
        price: 价格
        quantity: 数量
        **kwargs: 其他参数 (如 slippage, pnl 等)
    """
    params = {
        "Price": f"{price:.8g}",
        "Qty": f"{quantity:.4f}",
        **kwargs
    }
    log_signal("ORDER", symbol, Action=action, **params)


def log_exit(
    symbol: str,
    exit_type: str,
    price: float,
    quantity: float,
    pnl_pct: float,
    **kwargs
) -> None:
    """
    记录退出信息

    Args:
        symbol: 交易对
        exit_type: 退出类型 (TP1, TP2, SL, TRAILING)
        price: 退出价格
        quantity: 退出数量
        pnl_pct: 盈亏百分比
        **kwargs: 其他参数
    """
    params = {
        "Type": exit_type,
        "Price": f"{price:.8g}",
        "Qty": f"{quantity:.4f}",
        "PnL": f"{pnl_pct:+.2%}"
    }
    log_signal("EXIT", symbol, **params, **kwargs)


# 初始化日志系统
setup_logger()

# 导出
__all__ = [
    'logger',
    'setup_logger',
    'get_logger',
    'log_signal',
    'log_order',
    'log_exit'
]
