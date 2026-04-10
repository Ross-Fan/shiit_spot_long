"""
工具函数模块
提供通用的辅助函数
"""

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from .config import config


@dataclass
class SymbolData:
    """单个交易对的数据结构"""
    symbol: str
    volumes: Deque[float] = field(default_factory=lambda: deque(maxlen=60))
    prices: Deque[float] = field(default_factory=lambda: deque(maxlen=60))
    highs: Deque[float] = field(default_factory=lambda: deque(maxlen=1440))
    lows: Deque[float] = field(default_factory=lambda: deque(maxlen=1440))
    last_price: float = 0.0
    last_volume: float = 0.0
    quote_volume_24h: float = 0.0
    price_change_24h: float = 0.0
    last_update: float = 0.0

    def avg_volume(self, periods: int = 60) -> float:
        """计算平均成交量"""
        if not self.volumes:
            return 0.0
        volumes = list(self.volumes)[-periods:]
        return sum(volumes) / len(volumes) if volumes else 0.0

    def high_24h(self) -> float:
        """获取24小时最高价"""
        return max(self.highs) if self.highs else 0.0

    def low_24h(self) -> float:
        """获取24小时最低价"""
        return min(self.lows) if self.lows else float('inf')

    def price_change_1m(self) -> float:
        """计算1分钟价格变化率"""
        if len(self.prices) < 2:
            return 0.0
        prev_price = self.prices[-2] if len(self.prices) >= 2 else self.prices[-1]
        if prev_price == 0:
            return 0.0
        return (self.last_price - prev_price) / prev_price

    def is_bullish_candle(self) -> bool:
        """判断是否为阳线（当前价 > 前一分钟收盘价）"""
        if len(self.prices) < 2:
            return False
        return self.last_price > self.prices[-2]


@dataclass
class Signal:
    """交易信号数据结构"""
    symbol: str
    signal_type: str  # VOLUME_SPIKE, BREAKOUT, SQUEEZE
    timestamp: float
    price: float
    volume_ratio: float = 0.0  # 当前成交量 / 平均成交量
    price_change_1m: float = 0.0
    is_breakout: bool = False
    priority: int = 0  # 信号优先级
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'symbol': self.symbol,
            'signal_type': self.signal_type,
            'timestamp': self.timestamp,
            'price': self.price,
            'volume_ratio': self.volume_ratio,
            'price_change_1m': self.price_change_1m,
            'is_breakout': self.is_breakout,
            'priority': self.priority,
            'extra': self.extra
        }


class RateLimiter:
    """
    令牌桶限频器
    用于控制 API 请求频率
    """

    def __init__(self, rate: float, capacity: Optional[float] = None):
        """
        Args:
            rate: 每秒生成的令牌数
            capacity: 桶容量，默认等于 rate
        """
        self.rate = rate
        self.capacity = capacity or rate
        self.tokens = self.capacity
        self.last_update = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        """
        获取令牌，如果令牌不足则等待

        Args:
            tokens: 需要的令牌数
        """
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self.last_update
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                self.last_update = now

                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return

                # 计算需要等待的时间
                wait_time = (tokens - self.tokens) / self.rate
                await asyncio.sleep(wait_time)

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """
        尝试获取令牌，不等待

        Args:
            tokens: 需要的令牌数

        Returns:
            是否成功获取
        """
        now = time.monotonic()
        elapsed = now - self.last_update
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last_update = now

        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False


class DataPersistence:
    """数据持久化管理"""

    def __init__(self, data_dir: Optional[Path] = None):
        if data_dir is None:
            data_dir = Path(__file__).parent.parent / 'data'
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def save(self, filename: str, data: Dict[str, Any]) -> None:
        """保存数据到 JSON 文件"""
        filepath = self.data_dir / filename
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)

    def load(self, filename: str) -> Optional[Dict[str, Any]]:
        """从 JSON 文件加载数据"""
        filepath = self.data_dir / filename
        if not filepath.exists():
            return None
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return None

    def save_symbol_data(self, symbols: Dict[str, SymbolData]) -> None:
        """保存交易对数据"""
        data = {}
        for symbol, sd in symbols.items():
            data[symbol] = {
                'volumes': list(sd.volumes),
                'prices': list(sd.prices),
                'highs': list(sd.highs),
                'lows': list(sd.lows),
                'last_price': sd.last_price,
                'last_volume': sd.last_volume,
                'quote_volume_24h': sd.quote_volume_24h,
                'price_change_24h': sd.price_change_24h,
                'last_update': sd.last_update
            }
        self.save('price_history.json', data)

    def load_symbol_data(self) -> Dict[str, SymbolData]:
        """加载交易对数据"""
        data = self.load('price_history.json')
        if not data:
            return {}

        symbols = {}
        window_minutes = config.data_settings.get('history_window_minutes', 60)
        high_window = config.data_settings.get('high_window_minutes', 1440)

        for symbol, d in data.items():
            sd = SymbolData(symbol=symbol)
            sd.volumes = deque(d.get('volumes', []), maxlen=window_minutes)
            sd.prices = deque(d.get('prices', []), maxlen=window_minutes)
            sd.highs = deque(d.get('highs', []), maxlen=high_window)
            sd.lows = deque(d.get('lows', []), maxlen=high_window)
            sd.last_price = d.get('last_price', 0.0)
            sd.last_volume = d.get('last_volume', 0.0)
            sd.quote_volume_24h = d.get('quote_volume_24h', 0.0)
            sd.price_change_24h = d.get('price_change_24h', 0.0)
            sd.last_update = d.get('last_update', 0.0)
            symbols[symbol] = sd

        return symbols


def format_price(price: float, precision: int = 8) -> str:
    """格式化价格显示"""
    return f"{price:.{precision}g}"


def format_quantity(quantity: float, precision: int = 4) -> str:
    """格式化数量显示"""
    return f"{quantity:.{precision}f}"


def format_percent(value: float) -> str:
    """格式化百分比显示"""
    return f"{value:+.2%}"


def timestamp_to_datetime(ts: float) -> datetime:
    """时间戳转 datetime"""
    return datetime.fromtimestamp(ts)


def datetime_to_timestamp(dt: datetime) -> float:
    """datetime 转时间戳"""
    return dt.timestamp()


def is_usdt_pair(symbol: str) -> bool:
    """判断是否为 USDT 交易对"""
    return symbol.endswith('USDT')


def filter_symbol(symbol: str, blacklist: Optional[List[str]] = None) -> bool:
    """
    过滤交易对

    Args:
        symbol: 交易对名称
        blacklist: 黑名单

    Returns:
        True 表示应该排除，False 表示保留
    """
    if blacklist is None:
        blacklist = config.blacklist

    # 检查黑名单
    if symbol in blacklist:
        return True

    # 只保留 USDT 交易对
    if not is_usdt_pair(symbol):
        return True

    return False


# 导出
__all__ = [
    'SymbolData',
    'Signal',
    'RateLimiter',
    'DataPersistence',
    'format_price',
    'format_quantity',
    'format_percent',
    'timestamp_to_datetime',
    'datetime_to_timestamp',
    'is_usdt_pair',
    'filter_symbol'
]
