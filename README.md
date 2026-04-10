# 山寨币现货异动狙击系统

基于 Python 的自动化交易框架，通过 WebSocket 实时监控币安全市场现货交易对，捕捉资金突然介入、突破盘整区间的动能爆发点，执行快速买入并利用移动止盈捕获波段利润。

## 功能特性

- **实时监控**: WebSocket 订阅全市场 miniTicker，毫秒级响应
- **成交量异动检测**: 单根放量 + 连续放量双重验证
- **多维度过滤**: 24h 成交额、价格动量、买卖价差、盘整期检查
- **大盘保护**: BTC 下跌时自动停止开仓
- **智能止盈止损**:
  - 4% 固定止损
  - 5 分钟内涨 3% 自动保本
  - 分级止盈 (+5% 出 30%, +10% 出 30%)
  - Trailing Stop 跟踪剩余仓位
- **模拟交易模式**: 可先模拟运行观察信号质量

## 系统架构

```
┌─────────────────┐
│ stream_manager  │  ← WebSocket 数据流，成交量异动初筛
└────────┬────────┘
         │ Signal
         ▼
┌─────────────────┐
│   validator     │  ← REST API 深度验证（盘整期、流动性）
└────────┬────────┘
         │ ValidationResult
         ▼
┌─────────────────┐
│ order_manager   │  ← 下单执行，持仓管理，止损止盈
└─────────────────┘
```

## 快速开始

### 1. 环境要求

- Python 3.8+
- 稳定的网络连接（需访问币安 API）

### 2. 安装依赖

```bash
cd shiit_spot_long
pip install -r requirements.txt
```

### 3. 配置 API 密钥

```bash
# 复制示例配置
cp .env.example .env

# 编辑 .env 文件，填入你的币安 API 密钥
BINANCE_API_KEY=your_api_key_here
BINANCE_API_SECRET=your_api_secret_here
```

> **注意**: API 密钥需要开启现货交易权限。如果只是模拟交易，可以不配置。

### 4. 运行程序

```bash
# 默认以模拟交易模式运行
python main.py
```

### 5. 切换实盘模式

编辑 `config/config.yaml`：

```yaml
mode:
  paper_trading: false  # 改为 false 开启实盘
```

## 配置说明

主要配置文件位于 `config/config.yaml`，关键参数说明：

### 触发阈值

```yaml
thresholds:
  vol_multiplier: 5.0        # 单根K线成交量需达到60分钟均量的5倍
  vol_continuous_multiplier: 3.0  # 连续放量阈值
  min_24h_vol: 10000000      # 最小24h成交额 (1000万 USDT)
  max_24h_vol: 100000000     # 最大24h成交额 (1亿 USDT)
  price_surge_1m: 0.015      # 1分钟最小涨幅 (1.5%)
  max_spread: 0.005          # 最大买卖价差 (0.5%)
```

### 风险管理

```yaml
risk:
  position_size: 500         # 每笔交易金额 (USDT)
  max_open_positions: 5      # 最大同时持仓数
  stop_loss_pct: 0.04        # 初始止损 (4%)
  breakeven_trigger: 0.03    # 保本止损触发涨幅 (3%)
  slippage_tolerance: 0.01   # 最大滑点容忍 (1%)
```

### 止盈配置

```yaml
take_profit:
  stage1_trigger: 0.05       # 第一阶段触发 (5%)
  stage1_portion: 0.30       # 第一阶段止盈比例 (30%)
  stage2_trigger: 0.10       # 第二阶段触发 (10%)
  stage2_portion: 0.30       # 第二阶段止盈比例 (30%)
```

### 黑名单

```yaml
market:
  blacklist:
    - "BTCUSDT"
    - "ETHUSDT"
    - "BNBUSDT"
    # ... 排除大盘币和稳定币
```

## 日志说明

日志文件位于 `logs/` 目录：

| 文件 | 内容 |
|-----|------|
| `trading.log` | 主日志，记录系统运行状态 |
| `signals.log` | 信号日志，记录所有触发的交易信号 |
| `errors.log` | 错误日志，仅记录错误信息 |

### 信号日志格式

```
[2024-01-15 14:32:15] SIGNAL | XYZUSDT      | Vol: 5.2x | Change: +2.30% | Break: YES | Cont: NO
[2024-01-15 14:32:16] ORDER  | XYZUSDT      | Action: BUY | Price: 1.234 | Qty: 405.5 | Slippage: 0.08%
[2024-01-15 14:45:30] EXIT   | XYZUSDT      | Type: TP1 | Price: 1.296 | Qty: 121.6 | PnL: +5.02%
```

## 项目结构

```
shiit_spot_long/
├── config/
│   └── config.yaml          # 配置文件
├── src/
│   ├── __init__.py
│   ├── config.py            # 配置加载器
│   ├── logger.py            # 日志系统
│   ├── utils.py             # 工具函数
│   ├── stream_manager.py    # WebSocket 数据流
│   ├── validator.py         # 信号验证
│   └── order_manager.py     # 订单管理
├── data/                    # 数据持久化
├── logs/                    # 日志文件
├── main.py                  # 主程序入口
├── requirements.txt         # 依赖
├── .env.example             # 环境变量示例
└── instruction.md           # 设计文档
```

## 信号触发逻辑

一个有效的买入信号需要满足以下条件：

1. **成交量突变**
   - 单根 K 线成交量 > 60 分钟均量 × 5
   - 或连续 2 根 K 线成交量 > 均量 × 3

2. **价格动量**
   - 1 分钟涨幅 > 1.5%
   - 阳线（收盘价 > 开盘价）

3. **过滤条件**
   - 24h 成交额在 1000 万 ~ 1 亿 USDT 之间
   - 不在黑名单中
   - 买卖价差 < 0.5%

4. **盘整期验证**
   - 过去 12 小时涨幅 < 10%

5. **大盘保护**
   - BTC 5 分钟跌幅 < 1%

## 注意事项

1. **风险提示**: 加密货币交易存在高风险，请谨慎使用，仅投入可承受损失的资金。

2. **API 限制**: 币安 API 有频率限制（1200 请求/分钟），系统已内置限频控制。

3. **网络要求**: 需要稳定的网络连接，WebSocket 断连会自动重连。

4. **首次运行**: 系统需要约 60 分钟积累历史数据才能正常检测异动。

5. **模拟测试**: 强烈建议先以模拟模式运行至少 24 小时，观察信号质量后再考虑实盘。

## 扩展功能 (规划中)

- [ ] 合约数据监控（轧空信号检测）
- [ ] 回测引擎
- [ ] Web 控制面板
- [ ] Telegram/微信通知

## License

MIT License
