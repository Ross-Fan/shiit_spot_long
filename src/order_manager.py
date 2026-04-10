"""
订单执行与持仓管理模块
负责下单、止损止盈和持仓状态管理
"""

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

import ccxt.async_support as ccxt

from .config import config
from .logger import get_logger, log_order, log_exit
from .validator import ValidationResult

logger = get_logger("order")


class PositionStatus(Enum):
    """持仓状态"""
    PENDING = "pending"      # 待成交
    OPEN = "open"            # 持仓中
    PARTIAL_EXIT = "partial" # 部分平仓
    CLOSED = "closed"        # 已平仓


class ExitType(Enum):
    """退出类型"""
    STOP_LOSS = "SL"
    TAKE_PROFIT_1 = "TP1"
    TAKE_PROFIT_2 = "TP2"
    TRAILING_STOP = "TRAIL"
    MANUAL = "MANUAL"


@dataclass
class Position:
    """持仓数据结构"""
    symbol: str
    entry_price: float
    quantity: float
    entry_time: float
    status: PositionStatus = PositionStatus.OPEN

    # 止损止盈
    stop_loss: float = 0.0
    initial_stop_loss: float = 0.0
    highest_price: float = 0.0

    # 分批止盈跟踪
    realized_pnl: float = 0.0
    remaining_qty: float = 0.0
    tp_stage: int = 0  # 止盈阶段 (0/1/2)

    # 订单ID
    entry_order_id: str = ""
    exit_order_ids: List[str] = field(default_factory=list)

    # 额外信息
    signal_data: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.remaining_qty == 0:
            self.remaining_qty = self.quantity
        if self.highest_price == 0:
            self.highest_price = self.entry_price

    @property
    def unrealized_pnl_pct(self) -> float:
        """未实现盈亏百分比"""
        if self.entry_price <= 0:
            return 0.0
        return (self.highest_price - self.entry_price) / self.entry_price

    def update_highest(self, price: float) -> None:
        """更新最高价"""
        if price > self.highest_price:
            self.highest_price = price

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'symbol': self.symbol,
            'entry_price': self.entry_price,
            'quantity': self.quantity,
            'entry_time': self.entry_time,
            'status': self.status.value,
            'stop_loss': self.stop_loss,
            'highest_price': self.highest_price,
            'realized_pnl': self.realized_pnl,
            'remaining_qty': self.remaining_qty,
            'tp_stage': self.tp_stage
        }


class OrderManager:
    """
    订单管理器
    负责执行买入、管理持仓、止损止盈
    """

    def __init__(self):
        self._exchange: Optional[ccxt.binance] = None
        self._positions: Dict[str, Position] = {}
        self._risk_config = config.risk
        self._tp_config = config.take_profit

        # 是否为模拟交易
        self._paper_trading = config.is_paper_trading

        # 持仓监控任务
        self._monitor_task: Optional[asyncio.Task] = None
        self._running = False

    @property
    def open_positions(self) -> List[Position]:
        """获取所有持仓"""
        return [p for p in self._positions.values() if p.status == PositionStatus.OPEN]

    @property
    def position_count(self) -> int:
        """当前持仓数量"""
        return len(self.open_positions)

    async def start(self) -> None:
        """启动订单管理器"""
        # 初始化交易所连接
        if not self._paper_trading:
            self._exchange = ccxt.binance({
                'apiKey': config.api_key,
                'secret': config.api_secret,
                'enableRateLimit': True,
                'options': {
                    'defaultType': 'spot'
                }
            })

            if config.is_testnet:
                self._exchange.set_sandbox_mode(True)

            # 测试连接
            try:
                await self._exchange.load_markets()
                logger.info("交易所连接成功")
            except Exception as e:
                logger.error(f"交易所连接失败: {e}")
                raise

        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_positions())

        mode = "模拟交易" if self._paper_trading else "实盘交易"
        logger.info(f"订单管理器已启动 ({mode})")

    async def stop(self) -> None:
        """停止订单管理器"""
        self._running = False

        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        if self._exchange:
            await self._exchange.close()
            self._exchange = None

        logger.info("订单管理器已停止")

    async def execute_buy(
        self,
        validation_result: ValidationResult,
        current_price: Optional[float] = None
    ) -> Optional[Position]:
        """
        执行买入

        Args:
            validation_result: 验证结果
            current_price: 当前价格（用于滑点检查）

        Returns:
            创建的持仓对象，失败返回 None
        """
        signal = validation_result.signal
        symbol = signal.symbol

        # 检查是否已有持仓
        if symbol in self._positions and self._positions[symbol].status == PositionStatus.OPEN:
            logger.warning(f"{symbol} 已有持仓，跳过")
            return None

        # 检查持仓数量限制
        max_positions = self._risk_config.get('max_open_positions', 5)
        if self.position_count >= max_positions:
            logger.warning(f"已达最大持仓数 {max_positions}，跳过 {symbol}")
            return None

        # 滑点检查
        trigger_price = signal.price
        if current_price is None:
            current_price = trigger_price

        slippage_tolerance = self._risk_config.get('slippage_tolerance', 0.01)
        slippage = (current_price - trigger_price) / trigger_price if trigger_price > 0 else 0

        if slippage > slippage_tolerance:
            logger.warning(f"{symbol} 滑点过大 {slippage:.2%}，取消买入")
            return None

        # 计算买入数量
        position_size = self._risk_config.get('position_size', 500)
        quantity = position_size / current_price

        # 执行买入
        order_id = ""
        actual_price = current_price
        actual_qty = quantity

        if self._paper_trading:
            # 模拟交易
            order_id = f"paper_{int(time.time() * 1000)}"
            logger.info(f"[模拟] {symbol} 买入 {actual_qty:.4f} @ {actual_price:.8g}")
        else:
            # 实盘交易
            try:
                order = await self._exchange.create_market_buy_order(
                    symbol,
                    None,
                    params={'quoteOrderQty': position_size}
                )
                order_id = order.get('id', '')
                actual_price = float(order.get('average', current_price))
                actual_qty = float(order.get('filled', quantity))
            except Exception as e:
                logger.error(f"{symbol} 下单失败: {e}")
                return None

        # 计算止损价
        stop_loss_pct = self._risk_config.get('stop_loss_pct', 0.04)
        stop_loss = actual_price * (1 - stop_loss_pct)

        # 创建持仓
        position = Position(
            symbol=symbol,
            entry_price=actual_price,
            quantity=actual_qty,
            entry_time=time.time(),
            stop_loss=stop_loss,
            initial_stop_loss=stop_loss,
            highest_price=actual_price,
            entry_order_id=order_id,
            signal_data=signal.to_dict()
        )

        self._positions[symbol] = position

        # 记录日志
        log_order(
            "BUY",
            symbol,
            actual_price,
            actual_qty,
            Slippage=f"{slippage:.2%}",
            SL=f"{stop_loss:.8g}"
        )

        return position

    async def _monitor_positions(self) -> None:
        """持仓监控循环"""
        while self._running:
            try:
                for symbol, position in list(self._positions.items()):
                    if position.status != PositionStatus.OPEN:
                        continue

                    # 获取当前价格
                    current_price = await self._get_current_price(symbol)
                    if current_price is None:
                        continue

                    # 更新最高价
                    position.update_highest(current_price)

                    # 检查止损
                    if current_price <= position.stop_loss:
                        await self._execute_exit(
                            position,
                            current_price,
                            ExitType.STOP_LOSS
                        )
                        continue

                    # 检查加速保护（5分钟内涨3%，止损上移至保本）
                    await self._check_breakeven(position, current_price)

                    # 检查分级止盈
                    await self._check_take_profit(position, current_price)

                    # 检查移动止盈
                    await self._check_trailing_stop(position, current_price)

            except Exception as e:
                logger.error(f"持仓监控异常: {e}")

            await asyncio.sleep(1)  # 每秒检查一次

    async def _check_breakeven(self, position: Position, current_price: float) -> None:
        """
        加速保护检查
        买入后5分钟内涨幅超过3%，止损上移至保本
        """
        if position.stop_loss > position.initial_stop_loss:
            # 已经上移过
            return

        # 检查时间
        elapsed = time.time() - position.entry_time
        if elapsed > 300:  # 5分钟
            return

        # 检查涨幅
        breakeven_trigger = self._risk_config.get('breakeven_trigger', 0.03)
        breakeven_level = self._risk_config.get('breakeven_level', 0.01)

        gain = (current_price - position.entry_price) / position.entry_price
        if gain >= breakeven_trigger:
            new_stop = position.entry_price * (1 + breakeven_level)
            position.stop_loss = new_stop
            logger.info(f"{position.symbol} 触发保本止损，SL 上移至 {new_stop:.8g}")

    async def _check_take_profit(self, position: Position, current_price: float) -> None:
        """
        分级止盈检查
        - 涨5%：止盈30%
        - 涨10%：止盈30%
        """
        gain = (current_price - position.entry_price) / position.entry_price

        # 第一阶段
        if position.tp_stage == 0:
            stage1_trigger = self._tp_config.get('stage1_trigger', 0.05)
            stage1_portion = self._tp_config.get('stage1_portion', 0.30)

            if gain >= stage1_trigger:
                exit_qty = position.quantity * stage1_portion
                await self._execute_partial_exit(
                    position,
                    current_price,
                    exit_qty,
                    ExitType.TAKE_PROFIT_1
                )
                position.tp_stage = 1

        # 第二阶段
        elif position.tp_stage == 1:
            stage2_trigger = self._tp_config.get('stage2_trigger', 0.10)
            stage2_portion = self._tp_config.get('stage2_portion', 0.30)

            if gain >= stage2_trigger:
                exit_qty = position.quantity * stage2_portion
                await self._execute_partial_exit(
                    position,
                    current_price,
                    exit_qty,
                    ExitType.TAKE_PROFIT_2
                )
                position.tp_stage = 2

    async def _check_trailing_stop(self, position: Position, current_price: float) -> None:
        """
        移动止盈检查
        使用 ATR 或固定比例跟踪
        """
        if position.tp_stage < 2:
            # 未到第三阶段
            return

        # 简化版：使用固定比例回撤
        # 从最高点回撤超过 5% 则平仓
        trailing_pct = 0.05

        drop_from_high = (position.highest_price - current_price) / position.highest_price
        if drop_from_high >= trailing_pct:
            await self._execute_exit(
                position,
                current_price,
                ExitType.TRAILING_STOP
            )

    async def _execute_partial_exit(
        self,
        position: Position,
        price: float,
        quantity: float,
        exit_type: ExitType
    ) -> None:
        """执行部分平仓"""
        symbol = position.symbol

        if self._paper_trading:
            logger.info(f"[模拟] {symbol} 部分平仓 {quantity:.4f} @ {price:.8g}")
        else:
            try:
                await self._exchange.create_market_sell_order(symbol, quantity)
            except Exception as e:
                logger.error(f"{symbol} 部分平仓失败: {e}")
                return

        # 计算盈亏
        pnl = (price - position.entry_price) * quantity
        pnl_pct = (price - position.entry_price) / position.entry_price

        # 更新持仓
        position.remaining_qty -= quantity
        position.realized_pnl += pnl

        if position.remaining_qty <= 0:
            position.status = PositionStatus.CLOSED
        else:
            position.status = PositionStatus.PARTIAL_EXIT

        # 记录日志
        log_exit(
            symbol,
            exit_type.value,
            price,
            quantity,
            pnl_pct,
            Remain=f"{position.remaining_qty:.4f}"
        )

    async def _execute_exit(
        self,
        position: Position,
        price: float,
        exit_type: ExitType
    ) -> None:
        """执行全部平仓"""
        symbol = position.symbol
        quantity = position.remaining_qty

        if self._paper_trading:
            logger.info(f"[模拟] {symbol} 全部平仓 {quantity:.4f} @ {price:.8g}")
        else:
            try:
                await self._exchange.create_market_sell_order(symbol, quantity)
            except Exception as e:
                logger.error(f"{symbol} 平仓失败: {e}")
                return

        # 计算盈亏
        pnl = (price - position.entry_price) * quantity
        pnl_pct = (price - position.entry_price) / position.entry_price

        # 更新持仓
        position.remaining_qty = 0
        position.realized_pnl += pnl
        position.status = PositionStatus.CLOSED

        # 记录日志
        log_exit(
            symbol,
            exit_type.value,
            price,
            quantity,
            pnl_pct,
            TotalPnL=f"{position.realized_pnl:.2f}"
        )

        # 从活跃持仓中移除
        # 保留记录用于统计，但标记为已关闭

    async def _get_current_price(self, symbol: str) -> Optional[float]:
        """获取当前价格"""
        if self._paper_trading:
            # 模拟模式下从 stream_manager 获取
            from .stream_manager import get_stream_manager
            sm = get_stream_manager()
            sd = sm.get_symbol_data(symbol)
            return sd.last_price if sd else None
        else:
            try:
                ticker = await self._exchange.fetch_ticker(symbol)
                return ticker.get('last')
            except Exception as e:
                logger.error(f"获取 {symbol} 价格失败: {e}")
                return None

    def get_position(self, symbol: str) -> Optional[Position]:
        """获取指定交易对的持仓"""
        return self._positions.get(symbol)

    def get_statistics(self) -> Dict[str, Any]:
        """获取交易统计"""
        closed = [p for p in self._positions.values() if p.status == PositionStatus.CLOSED]

        total_pnl = sum(p.realized_pnl for p in closed)
        win_count = sum(1 for p in closed if p.realized_pnl > 0)
        loss_count = sum(1 for p in closed if p.realized_pnl < 0)

        return {
            'total_trades': len(closed),
            'open_positions': self.position_count,
            'total_pnl': total_pnl,
            'win_count': win_count,
            'loss_count': loss_count,
            'win_rate': win_count / len(closed) if closed else 0
        }


# 单例
_order_manager: Optional[OrderManager] = None


def get_order_manager() -> OrderManager:
    """获取 OrderManager 单例"""
    global _order_manager
    if _order_manager is None:
        _order_manager = OrderManager()
    return _order_manager
