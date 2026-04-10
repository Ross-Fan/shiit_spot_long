"""
信号验证引擎
负责对初筛信号进行深度验证
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import aiohttp

from .config import config
from .logger import get_logger, log_signal
from .utils import RateLimiter, Signal

logger = get_logger("validator")


@dataclass
class ValidationResult:
    """验证结果"""
    is_valid: bool
    signal: Signal
    reason: str = ""
    consolidation_check: bool = False
    liquidity_check: bool = False
    breakout_confirmed: bool = False
    spread: float = 0.0
    extra: Dict[str, Any] = None

    def __post_init__(self):
        if self.extra is None:
            self.extra = {}


class SignalValidator:
    """
    信号验证器
    通过 REST API 获取 K 线数据验证信号有效性
    """

    # 币安 REST API 地址
    API_BASE_URL = "https://api.binance.com"

    def __init__(self):
        self._thresholds = config.thresholds
        self._rate_limiter = RateLimiter(
            rate=config.rate_limit.get('rest_requests_per_min', 600) / 60,
            capacity=20  # 允许短时突发
        )
        self._session: Optional[aiohttp.ClientSession] = None

        # 验证通过回调
        self._validated_callbacks: List[Callable[[ValidationResult], None]] = []

        # K线缓存
        self._kline_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_ttl = 60  # 缓存有效期（秒）

    def add_validated_callback(self, callback: Callable[[ValidationResult], None]) -> None:
        """添加验证通过回调"""
        self._validated_callbacks.append(callback)

    def _emit_validated(self, result: ValidationResult) -> None:
        """发送验证结果"""
        for callback in self._validated_callbacks:
            try:
                callback(result)
            except Exception as e:
                logger.error(f"验证回调执行失败: {e}")

    async def start(self) -> None:
        """启动验证器"""
        self._session = aiohttp.ClientSession()
        logger.info("信号验证器已启动")

    async def stop(self) -> None:
        """停止验证器"""
        if self._session:
            await self._session.close()
            self._session = None
        logger.info("信号验证器已停止")

    async def validate(self, signal: Signal) -> ValidationResult:
        """
        验证信号

        验证项目:
        1. 盘整期检查：过去 12 小时涨幅 < 10%
        2. 流动性检查：买卖价差 < 0.5%
        3. 突破确认：价格确实突破了 24h 高点

        Args:
            signal: 待验证的信号

        Returns:
            验证结果
        """
        symbol = signal.symbol

        try:
            # 并行执行验证
            consolidation_task = self._check_consolidation(symbol)
            liquidity_task = self._check_liquidity(symbol)

            results = await asyncio.gather(
                consolidation_task,
                liquidity_task,
                return_exceptions=True
            )

            consolidation_result = results[0]
            liquidity_result = results[1]

            # 处理盘整期检查结果
            if isinstance(consolidation_result, Exception):
                logger.warning(f"{symbol} 盘整期检查失败: {consolidation_result}")
                consolidation_ok = True  # 失败时默认通过
            else:
                consolidation_ok = consolidation_result

            # 处理流动性检查结果
            if isinstance(liquidity_result, Exception):
                logger.warning(f"{symbol} 流动性检查失败: {liquidity_result}")
                liquidity_ok = True
                spread = 0.0
            elif isinstance(liquidity_result, tuple):
                liquidity_ok, spread = liquidity_result
            else:
                liquidity_ok = True
                spread = 0.0

            # 突破确认
            breakout_confirmed = signal.is_breakout

            # 综合判断
            is_valid = consolidation_ok and liquidity_ok

            # 生成原因
            reasons = []
            if not consolidation_ok:
                reasons.append("盘整期检查失败")
            if not liquidity_ok:
                reasons.append(f"流动性不足(Spread={spread:.2%})")

            result = ValidationResult(
                is_valid=is_valid,
                signal=signal,
                reason="; ".join(reasons) if reasons else "验证通过",
                consolidation_check=consolidation_ok,
                liquidity_check=liquidity_ok,
                breakout_confirmed=breakout_confirmed,
                spread=spread
            )

            # 记录日志
            if is_valid:
                log_signal(
                    "VALID",
                    symbol,
                    Spread=f"{spread:.2%}",
                    Break="YES" if breakout_confirmed else "NO",
                    Vol=f"{signal.volume_ratio:.1f}x"
                )
                self._emit_validated(result)
            else:
                logger.debug(f"{symbol} 验证未通过: {result.reason}")

            return result

        except Exception as e:
            logger.error(f"{symbol} 验证异常: {e}")
            return ValidationResult(
                is_valid=False,
                signal=signal,
                reason=f"验证异常: {e}"
            )

    async def _check_consolidation(self, symbol: str) -> bool:
        """
        盘整期检查

        验证标准:
        - 过去 12 小时涨幅 < 10%
        - 确保买在启动初期而非末端

        Returns:
            True 表示处于盘整期（适合买入）
        """
        hours = self._thresholds.get('consolidation_hours', 12)
        max_change = self._thresholds.get('consolidation_max_change', 0.10)

        # 获取 K 线数据
        klines = await self._get_klines(symbol, interval='1h', limit=hours)
        if not klines or len(klines) < hours // 2:
            # 数据不足，默认通过
            return True

        # 计算期间最高价和最低价
        highs = [float(k[2]) for k in klines]  # index 2 = high
        lows = [float(k[3]) for k in klines]   # index 3 = low

        period_high = max(highs)
        period_low = min(lows)

        if period_low <= 0:
            return True

        # 计算涨幅
        price_range = (period_high - period_low) / period_low

        if price_range > max_change:
            logger.debug(f"{symbol} 近 {hours}h 波动 {price_range:.2%} 超过阈值")
            return False

        return True

    async def _check_liquidity(self, symbol: str) -> tuple:
        """
        流动性检查

        验证标准:
        - 买卖价差 < 0.5%

        Returns:
            (is_ok, spread)
        """
        max_spread = self._thresholds.get('max_spread', 0.005)

        # 获取订单簿
        orderbook = await self._get_orderbook(symbol, limit=5)
        if not orderbook:
            return True, 0.0

        bids = orderbook.get('bids', [])
        asks = orderbook.get('asks', [])

        if not bids or not asks:
            return True, 0.0

        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])

        if best_bid <= 0:
            return True, 0.0

        spread = (best_ask - best_bid) / best_bid

        if spread > max_spread:
            return False, spread

        return True, spread

    async def _get_klines(
        self,
        symbol: str,
        interval: str = '1h',
        limit: int = 12
    ) -> Optional[List]:
        """
        获取 K 线数据

        Args:
            symbol: 交易对
            interval: K 线间隔 (1m, 5m, 15m, 1h, 4h, 1d)
            limit: 数量

        Returns:
            K 线数据列表
        """
        cache_key = f"{symbol}_{interval}_{limit}"

        # 检查缓存
        if cache_key in self._kline_cache:
            cached = self._kline_cache[cache_key]
            if time.time() - cached['time'] < self._cache_ttl:
                return cached['data']

        # 限频
        await self._rate_limiter.acquire()

        url = f"{self.API_BASE_URL}/api/v3/klines"
        params = {
            'symbol': symbol,
            'interval': interval,
            'limit': limit
        }

        try:
            async with self._session.get(url, params=params, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # 缓存
                    self._kline_cache[cache_key] = {
                        'time': time.time(),
                        'data': data
                    }
                    return data
                else:
                    logger.warning(f"获取 K 线失败: {resp.status}")
                    return None
        except Exception as e:
            logger.error(f"获取 K 线异常: {e}")
            return None

    async def _get_orderbook(
        self,
        symbol: str,
        limit: int = 5
    ) -> Optional[Dict]:
        """
        获取订单簿

        Args:
            symbol: 交易对
            limit: 深度

        Returns:
            订单簿数据
        """
        cache_key = f"{symbol}_orderbook"

        # 检查缓存（订单簿缓存时间短）
        if cache_key in self._kline_cache:
            cached = self._kline_cache[cache_key]
            if time.time() - cached['time'] < 5:  # 5秒缓存
                return cached['data']

        # 限频
        await self._rate_limiter.acquire()

        url = f"{self.API_BASE_URL}/api/v3/depth"
        params = {
            'symbol': symbol,
            'limit': limit
        }

        try:
            async with self._session.get(url, params=params, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self._kline_cache[cache_key] = {
                        'time': time.time(),
                        'data': data
                    }
                    return data
                else:
                    logger.warning(f"获取订单簿失败: {resp.status}")
                    return None
        except Exception as e:
            logger.error(f"获取订单簿异常: {e}")
            return None

    async def get_current_price(self, symbol: str) -> Optional[float]:
        """
        获取当前价格

        Args:
            symbol: 交易对

        Returns:
            当前价格
        """
        await self._rate_limiter.acquire()

        url = f"{self.API_BASE_URL}/api/v3/ticker/price"
        params = {'symbol': symbol}

        try:
            async with self._session.get(url, params=params, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data.get('price', 0))
                return None
        except Exception as e:
            logger.error(f"获取价格异常: {e}")
            return None


# 单例
_validator: Optional[SignalValidator] = None


def get_validator() -> SignalValidator:
    """获取 SignalValidator 单例"""
    global _validator
    if _validator is None:
        _validator = SignalValidator()
    return _validator
