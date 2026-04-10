#!/usr/bin/env python3
"""
山寨币现货异动狙击系统 - 主程序入口
"""

import asyncio
import signal
import sys
import time
from typing import Optional

from src.config import config
from src.logger import get_logger, setup_logger
from src.order_manager import OrderManager, get_order_manager
from src.stream_manager import StreamManager, get_stream_manager
from src.utils import Signal
from src.validator import SignalValidator, ValidationResult, get_validator

logger = get_logger("main")


class TradingBot:
    """交易机器人主类"""

    def __init__(self):
        self._stream_manager: Optional[StreamManager] = None
        self._validator: Optional[SignalValidator] = None
        self._order_manager: Optional[OrderManager] = None
        self._running = False
        self._start_time: float = 0

        # 信号队列
        self._signal_queue: asyncio.Queue = asyncio.Queue()

        # 信号计数
        self._signal_count: int = 0
        self._validated_count: int = 0

    async def start(self) -> None:
        """启动交易机器人"""
        logger.info("=" * 50)
        logger.info("山寨币现货异动狙击系统启动")
        logger.info("=" * 50)

        # 显示配置信息
        self._log_config()

        # 初始化组件
        self._stream_manager = get_stream_manager()
        self._validator = get_validator()
        self._order_manager = get_order_manager()

        # 设置回调
        self._stream_manager.add_signal_callback(self._on_signal)
        self._validator.add_validated_callback(self._on_validated)

        # 启动组件
        await self._validator.start()
        await self._order_manager.start()

        self._running = True
        self._start_time = time.time()

        # 启动任务
        tasks = [
            asyncio.create_task(self._stream_manager.start()),
            asyncio.create_task(self._process_signals()),
            asyncio.create_task(self._status_reporter()),
            asyncio.create_task(self._data_warmup_reporter())
        ]

        logger.info("系统启动完成，正在接收数据...")
        logger.info("提示: 系统需要约 10 分钟积累数据后才能开始检测异动")

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("收到停止信号")

    async def stop(self) -> None:
        """停止交易机器人"""
        logger.info("正在停止系统...")
        self._running = False

        if self._stream_manager:
            await self._stream_manager.stop()
        if self._validator:
            await self._validator.stop()
        if self._order_manager:
            await self._order_manager.stop()

        # 输出统计
        if self._order_manager:
            stats = self._order_manager.get_statistics()
            logger.info("=" * 50)
            logger.info("交易统计:")
            logger.info(f"  总交易数: {stats['total_trades']}")
            logger.info(f"  盈利次数: {stats['win_count']}")
            logger.info(f"  亏损次数: {stats['loss_count']}")
            logger.info(f"  胜率: {stats['win_rate']:.1%}")
            logger.info(f"  总盈亏: {stats['total_pnl']:.2f} USDT")
            logger.info("=" * 50)

        logger.info("系统已停止")

    def _log_config(self) -> None:
        """输出配置信息"""
        mode = "模拟交易" if config.is_paper_trading else "实盘交易"
        logger.info(f"运行模式: {mode}")
        logger.info(f"单笔金额: {config.risk.get('position_size', 500)} USDT")
        logger.info(f"最大持仓: {config.risk.get('max_open_positions', 5)}")
        logger.info(f"止损比例: {config.risk.get('stop_loss_pct', 0.04):.1%}")
        logger.info(f"成交量倍数: {config.thresholds.get('vol_multiplier', 5.0)}x")
        logger.info(f"黑名单币种: {len(config.blacklist)} 个")

    def _on_signal(self, signal: Signal) -> None:
        """处理初筛信号"""
        self._signal_count += 1
        try:
            self._signal_queue.put_nowait(signal)
        except asyncio.QueueFull:
            logger.warning("信号队列已满，丢弃信号")

    def _on_validated(self, result: ValidationResult) -> None:
        """处理验证通过的信号"""
        self._validated_count += 1
        asyncio.create_task(self._execute_trade(result))

    async def _process_signals(self) -> None:
        """信号处理循环"""
        while self._running:
            try:
                # 等待信号，带超时
                signal = await asyncio.wait_for(
                    self._signal_queue.get(),
                    timeout=1.0
                )

                # 检查大盘保护
                if not self._stream_manager.is_market_safe():
                    logger.debug(f"大盘保护生效，跳过 {signal.symbol}")
                    continue

                # 验证信号
                await self._validator.validate(signal)

            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"信号处理异常: {e}")

    async def _execute_trade(self, result: ValidationResult) -> None:
        """执行交易"""
        if not result.is_valid:
            return

        signal = result.signal

        # 获取当前价格
        current_price = await self._validator.get_current_price(signal.symbol)

        # 执行买入
        position = await self._order_manager.execute_buy(result, current_price)

        if position:
            logger.info(f"成功建仓 {signal.symbol}")

    async def _status_reporter(self) -> None:
        """定期输出状态报告"""
        # 等待数据预热完成后再开始定期报告
        await asyncio.sleep(600)  # 10分钟后开始

        while self._running:
            await asyncio.sleep(300)  # 每5分钟

            self._print_status()

    async def _data_warmup_reporter(self) -> None:
        """数据预热期间的状态报告"""
        # 报告时间点（从启动开始的秒数）
        report_times = [30, 60, 120, 300, 600]  # 30秒、1分钟、2分钟、5分钟、10分钟
        last_report = 0

        for target_time in report_times:
            wait_time = target_time - last_report
            await asyncio.sleep(wait_time)
            last_report = target_time

            if not self._running:
                return

            stats = self._stream_manager.get_statistics()
            elapsed = int(time.time() - self._start_time)
            elapsed_str = f"{elapsed // 60}分{elapsed % 60}秒"

            logger.info("-" * 40)
            logger.info(f"[数据预热] 运行时间: {elapsed_str}")
            logger.info(f"  接收币种数: {stats['total_symbols']}")
            logger.info(f"  数据就绪: {stats['ready_symbols']} (需>=10分钟数据)")
            logger.info(f"  符合条件: {stats['qualified_symbols']} (成交额在范围内)")
            logger.info(f"  平均数据量: {stats['avg_data_minutes']:.1f} 分钟")

            if stats['btc_price'] > 0:
                logger.info(f"  BTC 价格: ${stats['btc_price']:,.2f}")

            if stats['ready_symbols'] >= 50:
                logger.info("  ✓ 数据积累充足，系统已进入正常监控状态")

        # 预热完成，输出最终状态
        logger.info("=" * 50)
        logger.info("数据预热完成，系统进入正常运行状态")
        self._print_status()

    def _print_status(self) -> None:
        """打印当前状态"""
        if not self._stream_manager or not self._order_manager:
            return

        stats = self._stream_manager.get_statistics()
        elapsed = int(time.time() - self._start_time)
        hours, remainder = divmod(elapsed, 3600)
        minutes, seconds = divmod(remainder, 60)

        if hours > 0:
            elapsed_str = f"{hours}小时{minutes}分钟"
        else:
            elapsed_str = f"{minutes}分钟{seconds}秒"

        logger.info("=" * 50)
        logger.info(f"[系统状态] 运行时间: {elapsed_str}")
        logger.info(f"  监控币种: {stats['total_symbols']} | 数据就绪: {stats['ready_symbols']} | 符合条件: {stats['qualified_symbols']}")

        if stats['btc_price'] > 0:
            btc_change = stats['btc_change_5m']
            btc_status = "正常" if btc_change > -0.01 else "⚠️ 下跌"
            logger.info(f"  BTC: ${stats['btc_price']:,.2f} ({btc_change:+.2%} 5m) [{btc_status}]")

        logger.info(f"  信号统计: 初筛 {self._signal_count} | 验证通过 {self._validated_count}")

        positions = self._order_manager.open_positions
        if positions:
            logger.info(f"  当前持仓: {len(positions)} 个")
            for p in positions:
                pnl_pct = p.unrealized_pnl_pct
                logger.info(
                    f"    {p.symbol}: 入场 {p.entry_price:.8g}, "
                    f"最高 {p.highest_price:.8g}, "
                    f"盈亏 {pnl_pct:+.2%}"
                )
        else:
            logger.info("  当前持仓: 无")

        logger.info("=" * 50)


async def main():
    """主函数"""
    # 重新配置日志（确保使用最新配置）
    setup_logger()

    bot = TradingBot()

    # 设置信号处理
    loop = asyncio.get_event_loop()

    def handle_signal():
        asyncio.create_task(bot.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    try:
        await bot.start()
    except KeyboardInterrupt:
        pass
    finally:
        await bot.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n用户中断")
        sys.exit(0)
