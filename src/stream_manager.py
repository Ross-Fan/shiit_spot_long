"""
实时流数据中心
负责 WebSocket 连接管理、数据订阅和成交量异动检测
"""

import asyncio
import json
import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional, Set

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from .config import config
from .logger import get_logger, log_signal
from .utils import DataPersistence, Signal, SymbolData, filter_symbol

logger = get_logger("stream")


class StreamManager:
    """
    实时数据流管理器
    订阅币安全市场 miniTicker，维护价格历史并检测成交量异动
    """

    # 币安 WebSocket 地址
    WS_BASE_URL = "wss://stream.binance.com:9443/ws"
    WS_STREAM = "!miniTicker@arr"

    def __init__(self):
        # 数据存储
        self.symbols: Dict[str, SymbolData] = {}

        # 配置
        self._blacklist: Set[str] = set(config.blacklist)
        self._thresholds = config.thresholds
        self._data_settings = config.data_settings

        # WebSocket 连接
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._reconnect_delay = config.rate_limit.get('ws_reconnect_delay', 5)

        # 回调函数
        self._signal_callbacks: List[Callable[[Signal], None]] = []

        # 数据持久化
        self._persistence = DataPersistence()
        self._last_persistence_time = 0
        self._persistence_interval = self._data_settings.get('persistence_interval', 300)

        # 分钟数据聚合
        self._current_minute: int = 0
        self._minute_volumes: Dict[str, float] = {}  # 当前分钟累计成交量
        self._minute_high: Dict[str, float] = {}  # 当前分钟最高价
        self._minute_low: Dict[str, float] = {}  # 当前分钟最低价

        # 连续放量追踪
        self._volume_spike_count: Dict[str, int] = {}  # 连续放量计数
        self._last_spike_minute: Dict[str, int] = {}  # 上次放量的分钟

        # BTC 数据（用于大盘保护）
        self._btc_prices: deque = deque(maxlen=60)  # 60分钟价格
        self._btc_last_price: float = 0.0

    def add_signal_callback(self, callback: Callable[[Signal], None]) -> None:
        """添加信号回调函数"""
        self._signal_callbacks.append(callback)

    def remove_signal_callback(self, callback: Callable[[Signal], None]) -> None:
        """移除信号回调函数"""
        if callback in self._signal_callbacks:
            self._signal_callbacks.remove(callback)

    def _emit_signal(self, signal: Signal) -> None:
        """发送信号到所有回调"""
        for callback in self._signal_callbacks:
            try:
                callback(signal)
            except Exception as e:
                logger.error(f"信号回调执行失败: {e}")

    async def start(self) -> None:
        """启动数据流"""
        self._running = True

        # 加载历史数据
        self._load_history()

        logger.info("启动 WebSocket 数据流...")

        while self._running:
            try:
                await self._connect_and_listen()
            except Exception as e:
                logger.error(f"WebSocket 连接异常: {e}")

            if self._running:
                logger.info(f"{self._reconnect_delay} 秒后重连...")
                await asyncio.sleep(self._reconnect_delay)

    async def stop(self) -> None:
        """停止数据流"""
        self._running = False

        if self._ws:
            await self._ws.close()
            self._ws = None

        # 保存数据
        self._save_history()
        logger.info("数据流已停止")

    async def _connect_and_listen(self) -> None:
        """连接 WebSocket 并监听数据"""
        url = f"{self.WS_BASE_URL}/{self.WS_STREAM}"

        async with websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5
        ) as ws:
            self._ws = ws
            logger.info(f"WebSocket 已连接: {url}")

            async for message in ws:
                if not self._running:
                    break

                try:
                    data = json.loads(message)
                    await self._process_tickers(data)
                except json.JSONDecodeError as e:
                    logger.warning(f"JSON 解析失败: {e}")
                except Exception as e:
                    logger.error(f"数据处理异常: {e}")

    async def _process_tickers(self, tickers: List[Dict[str, Any]]) -> None:
        """
        处理 miniTicker 数据

        miniTicker 字段说明:
        - s: 交易对
        - c: 最新价格
        - o: 24小时开盘价
        - h: 24小时最高价
        - l: 24小时最低价
        - v: 24小时成交量（base asset）
        - q: 24小时成交额（quote asset, 即 USDT）
        """
        current_time = time.time()
        current_minute = int(current_time // 60)

        # 检查是否进入新的分钟
        if current_minute != self._current_minute:
            await self._on_minute_close(current_minute)
            self._current_minute = current_minute

        for ticker in tickers:
            symbol = ticker.get('s', '')

            # 过滤交易对
            if filter_symbol(symbol, self._blacklist):
                continue

            # 获取或创建 SymbolData
            if symbol not in self.symbols:
                self.symbols[symbol] = SymbolData(symbol=symbol)

            sd = self.symbols[symbol]

            # 更新数据
            price = float(ticker.get('c', 0))
            quote_volume_24h = float(ticker.get('q', 0))
            high_24h = float(ticker.get('h', 0))
            low_24h = float(ticker.get('l', 0))

            # 计算增量成交额（当前分钟）
            if sd.quote_volume_24h > 0 and quote_volume_24h > sd.quote_volume_24h:
                volume_delta = quote_volume_24h - sd.quote_volume_24h
                self._minute_volumes[symbol] = self._minute_volumes.get(symbol, 0) + volume_delta

            # 更新分钟高低价
            if symbol not in self._minute_high or price > self._minute_high[symbol]:
                self._minute_high[symbol] = price
            if symbol not in self._minute_low or price < self._minute_low[symbol]:
                self._minute_low[symbol] = price

            # 更新 SymbolData
            sd.last_price = price
            sd.quote_volume_24h = quote_volume_24h
            sd.last_update = current_time

            # 更新 BTC 数据
            if symbol == 'BTCUSDT':
                self._btc_last_price = price

        # 定期持久化
        if current_time - self._last_persistence_time > self._persistence_interval:
            self._save_history()
            self._last_persistence_time = current_time

    async def _on_minute_close(self, new_minute: int) -> None:
        """
        分钟结束时的处理
        - 更新滚动窗口数据
        - 检测成交量异动
        """
        if self._current_minute == 0:
            # 首次初始化
            return

        # 更新 BTC 价格历史
        if self._btc_last_price > 0:
            self._btc_prices.append(self._btc_last_price)

        # 处理每个交易对
        for symbol, volume in self._minute_volumes.items():
            if symbol not in self.symbols:
                continue

            sd = self.symbols[symbol]

            # 更新滚动窗口
            sd.volumes.append(volume)
            sd.prices.append(sd.last_price)

            if symbol in self._minute_high:
                sd.highs.append(self._minute_high[symbol])
            if symbol in self._minute_low:
                sd.lows.append(self._minute_low[symbol])

            sd.last_volume = volume

            # 检测成交量异动
            await self._check_volume_spike(symbol, sd, new_minute)

        # 清空分钟数据
        self._minute_volumes.clear()
        self._minute_high.clear()
        self._minute_low.clear()

    async def _check_volume_spike(
        self,
        symbol: str,
        sd: SymbolData,
        current_minute: int
    ) -> None:
        """
        检测成交量异动

        触发条件:
        1. 当前成交量 > 平均成交量 × vol_multiplier
        2. 连续 2 根 K 线成交量 > 平均成交量 × vol_continuous_multiplier
        3. 1分钟涨幅 > price_surge_1m
        4. 阳线（Close > Open）
        """
        # 需要足够的历史数据
        if len(sd.volumes) < 10:
            return

        avg_volume = sd.avg_volume()
        if avg_volume <= 0:
            return

        current_volume = sd.last_volume
        volume_ratio = current_volume / avg_volume

        # 获取阈值
        vol_multiplier = self._thresholds.get('vol_multiplier', 5.0)
        vol_continuous_multiplier = self._thresholds.get('vol_continuous_multiplier', 3.0)
        price_surge_threshold = self._thresholds.get('price_surge_1m', 0.015)
        min_24h_vol = self._thresholds.get('min_24h_vol', 10_000_000)
        max_24h_vol = self._thresholds.get('max_24h_vol', 100_000_000)

        # 24小时成交额过滤
        if sd.quote_volume_24h < min_24h_vol or sd.quote_volume_24h > max_24h_vol:
            return

        # 检查单根放量
        is_single_spike = volume_ratio >= vol_multiplier

        # 检查连续放量
        is_continuous = False
        last_spike_minute = self._last_spike_minute.get(symbol, 0)

        if volume_ratio >= vol_continuous_multiplier:
            # 检查是否与上一根连续
            if current_minute - last_spike_minute == 1:
                self._volume_spike_count[symbol] = self._volume_spike_count.get(symbol, 0) + 1
                if self._volume_spike_count[symbol] >= 2:
                    is_continuous = True
            else:
                self._volume_spike_count[symbol] = 1

            self._last_spike_minute[symbol] = current_minute
        else:
            # 重置计数
            if current_minute - last_spike_minute > 1:
                self._volume_spike_count[symbol] = 0

        # 如果既没有单根放量也没有连续放量，返回
        if not is_single_spike and not is_continuous:
            return

        # 检查价格动量
        price_change = sd.price_change_1m()
        if price_change < price_surge_threshold:
            return

        # 检查是否为阳线
        if not sd.is_bullish_candle():
            return

        # 检查是否突破24小时高点
        high_24h = sd.high_24h()
        is_breakout = sd.last_price > high_24h * (1 + self._thresholds.get('breakout_margin', 0.005))

        # 生成信号
        signal = Signal(
            symbol=symbol,
            signal_type="VOLUME_SPIKE",
            timestamp=time.time(),
            price=sd.last_price,
            volume_ratio=volume_ratio,
            price_change_1m=price_change,
            is_breakout=is_breakout,
            priority=2 if is_continuous else 1,
            extra={
                'avg_volume': avg_volume,
                'current_volume': current_volume,
                'quote_volume_24h': sd.quote_volume_24h,
                'high_24h': high_24h,
                'is_continuous': is_continuous
            }
        )

        # 记录日志
        log_signal(
            "SIGNAL",
            symbol,
            Vol=f"{volume_ratio:.1f}x",
            Change=f"{price_change:+.2%}",
            Break="YES" if is_breakout else "NO",
            Cont="YES" if is_continuous else "NO"
        )

        # 发送信号
        self._emit_signal(signal)

    def get_btc_change(self, minutes: int = 5) -> float:
        """
        获取 BTC 近 N 分钟的涨跌幅

        Args:
            minutes: 分钟数

        Returns:
            涨跌幅（小数形式）
        """
        if len(self._btc_prices) < minutes or self._btc_last_price <= 0:
            return 0.0

        old_price = self._btc_prices[-minutes]
        if old_price <= 0:
            return 0.0

        return (self._btc_last_price - old_price) / old_price

    def is_market_safe(self) -> bool:
        """
        检查大盘是否安全（BTC 未大跌）

        Returns:
            True 表示安全，False 表示应停止开仓
        """
        btc_5m_threshold = config.market_protection.get('btc_5m_threshold', -0.01)
        btc_change = self.get_btc_change(5)

        if btc_change < btc_5m_threshold:
            logger.warning(f"大盘保护触发: BTC 5分钟跌幅 {btc_change:.2%}")
            return False

        return True

    def get_symbol_data(self, symbol: str) -> Optional[SymbolData]:
        """获取交易对数据"""
        return self.symbols.get(symbol)

    def get_statistics(self) -> Dict[str, Any]:
        """
        获取数据流统计信息

        Returns:
            统计信息字典
        """
        total_symbols = len(self.symbols)

        # 统计有足够历史数据的币种（可以进行异动检测）
        ready_symbols = sum(1 for sd in self.symbols.values() if len(sd.volumes) >= 10)

        # 统计符合成交额条件的币种
        min_24h_vol = self._thresholds.get('min_24h_vol', 10_000_000)
        max_24h_vol = self._thresholds.get('max_24h_vol', 100_000_000)
        qualified_symbols = sum(
            1 for sd in self.symbols.values()
            if min_24h_vol <= sd.quote_volume_24h <= max_24h_vol
        )

        # BTC 价格
        btc_price = self._btc_last_price
        btc_change_5m = self.get_btc_change(5)

        # 数据完整度（有多少分钟的数据）
        avg_data_minutes = 0
        if self.symbols:
            avg_data_minutes = sum(len(sd.volumes) for sd in self.symbols.values()) / len(self.symbols)

        return {
            'total_symbols': total_symbols,
            'ready_symbols': ready_symbols,
            'qualified_symbols': qualified_symbols,
            'btc_price': btc_price,
            'btc_change_5m': btc_change_5m,
            'avg_data_minutes': avg_data_minutes,
            'is_connected': self._ws is not None and self._ws.open if self._ws else False
        }

    def _load_history(self) -> None:
        """加载历史数据"""
        try:
            self.symbols = self._persistence.load_symbol_data()
            if self.symbols:
                logger.info(f"已加载 {len(self.symbols)} 个交易对的历史数据")
        except Exception as e:
            logger.warning(f"加载历史数据失败: {e}")
            self.symbols = {}

    def _save_history(self) -> None:
        """保存历史数据"""
        try:
            self._persistence.save_symbol_data(self.symbols)
            logger.debug(f"已保存 {len(self.symbols)} 个交易对的数据")
        except Exception as e:
            logger.error(f"保存历史数据失败: {e}")


# 单例
_stream_manager: Optional[StreamManager] = None


def get_stream_manager() -> StreamManager:
    """获取 StreamManager 单例"""
    global _stream_manager
    if _stream_manager is None:
        _stream_manager = StreamManager()
    return _stream_manager
