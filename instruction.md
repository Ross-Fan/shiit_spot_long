# 山寨币现货异动狙击系统设计 v2.1

## 1. 系统定义

本系统是一个基于 Python 的自动化交易框架，通过 WebSocket 实时监控币安（Binance）全市场现货交易对，捕捉资金突然介入、突破盘整区间的动能爆发点，执行快速买入并利用移动止盈捕获波段利润。

**扩展能力**：系统预留合约数据监控接口，可通过监控资金费率、持仓量等合约指标，捕捉轧空行情机会（Phase 5+ 实现）。

---

## 2. 核心量化逻辑

### 2.1 启动监测指标 (Trigger Indicators)

#### 成交量突变 (Volume Spike)
- **基础条件**: `Current_1m_Vol > Average_Vol_Last_60m * Multiplier`
- **连续性验证**: 要求连续 2 根 1m K 线成交量 > 均量 × 3（防止单根脉冲式假信号）
- **建议参数**: `Multiplier = 5.0`（单根触发阈值），`Continuous_Multiplier = 3.0`（连续验证阈值）

#### 价格动量 (Price Momentum)
- **基础条件**: `1m_Change > 1.5%` 且 `Close > Open`（实体阳线）
- **价格确认**: 第二根放量 K 线的 `Close > 第一根 K 线的 High`（价格站稳确认）

#### 突破验证 (Breakout Confirmation)
- **基础条件**: `Current_Price > Max(High_Price_Last_24h)`
- **突破幅度过滤**: `Current_Price > 24h_High * 1.005`（突破 0.5% 以上，过滤假突破）
- **可选增强**: 等待回踩确认（突破后回落但不破前高再入场）

### 2.2 过滤条件 (Filtering)

#### 活跃度过滤
- 最小交易额: `24h_Volume > 10,000,000 USDT`（确保流动性）
- 最大交易额: `24h_Volume < 100,000,000 USDT`（大盘币难暴涨）

#### 板块过滤
- 排除知名大盘币: BTC, ETH, SOL, BNB 等
- 排除稳定币: USDT, USDC, DAI, TUSD 等
- 排除锚定币: PAXG, WBTC 等

#### 流动性过滤
- 买卖价差: `Bid_Ask_Spread < 0.5%`（防止滑点过大）
- 订单簿深度: 买一卖一各 > 10,000 USDT（可选）

#### 盘整期验证
- **波动检查**: 过去 12 小时涨幅 < 10%（确保买在启动初期）
- **波动率检查**: 过去 12 小时 ATR 处于近期低位（真正的盘整而非高位震荡）
- **目的**: 确保介入点是突破盘整，而非追高

#### 大盘保护 (Market Hedge)
- 若 `BTC_5m_Change < -1%`，全局停止开仓
- 若 `BTC_1h_Change < -3%`，考虑减仓或暂停系统

### 2.3 合约联动分析 [扩展功能 - Phase 5+]

> **背景**: 山寨币暴涨常见于"轧空行情"（Short Squeeze）。当空头过度拥挤时，少量资金拉升即可触发爆仓连锁，产生火箭式上涨。监控合约数据可提前发现这类机会。

#### 现货与合约的三种联动模式

| 模式 | 特征 | 信号价值 |
|-----|------|---------|
| **现货为因** | 现货成交量先启动，合约跟进 | 中等 - 原有策略已覆盖 |
| **轧空拉升** | 负费率 + OI 骤降 + 价格飙升 | 极高 - 山寨币暴涨主战场 |
| **正基差驱动** | 合约溢价扩大，预示现货需求 | 高 - 先行指标 |

#### 轧空监控指标

```python
FuturesData = {
    "XYZUSDT": {
        "funding_rate": -0.015,      # 资金费率 (负=空头付费给多头)
        "open_interest": 5000000,    # 持仓量 (USDT)
        "oi_change_1h": -0.15,       # 1小时OI变化率
        "basis": 0.003,              # 基差 = (合约价-现货价)/现货价
    }
}
```

#### 信号增强规则

| 条件 | 解读 | 动作 |
|-----|------|-----|
| `funding_rate < -0.01%` | 空头拥挤 | 现货信号优先级 +1 |
| `funding_rate < -0.05%` | 极度看空，燃料充足 | 信号优先级 +2 |
| `OI 下降 > 10%` + 价格上涨 | 空头正在爆仓 | 快速通道，跳过连续验证 |
| `basis > 0.5%` | 合约溢价过高 | 警惕回调，降低仓位 |

#### 轧空猎手模式（独立触发）

当满足以下条件时，可独立触发买入信号（不依赖现货成交量连续验证）：
1. `funding_rate < -0.03%`（极度负费率）
2. `open_interest` 处于近期高位（燃料充足）
3. 现货出现任何向上催化（`1m_Change > 1%`）

**注意**: 轧空行情速度极快，等待连续验证会错过最佳入场点。

---

## 3. 风险管理与退出策略

### 3.1 仓位管理
- 单笔交易金额: 500 USDT（固定额度）
- 最大同时持仓: 5 个
- 总风险敞口: 2,500 USDT

### 3.2 止损策略
- **初始止损**: `Stop_Loss = Buy_Price * 0.96`（4% 固定止损）
- **加速保护**: 买入后 5 分钟内涨幅 > 3%，止损上移至 `Buy_Price * 1.01`（保本+1%）

### 3.3 止盈策略（分级退出）

| 涨幅阶段 | 操作 | 剩余仓位 |
|---------|------|---------|
| +5% | 止盈 30% 仓位 | 70% |
| +10% | 止盈 30% 仓位 | 40% |
| 剩余 40% | ATR Trailing Stop | 动态跟踪 |

**Trailing Stop 参数**:
- 使用 15min ATR × 2 作为回撤容忍度
- 或使用 15min MA10 作为动态支撑线
- 收盘价跌破则清仓

### 3.4 滑点控制
- 下单前检查: 当前市价与触发价偏离 > 1% 则取消执行
- 市价单保护: 使用带 `quoteOrderQty` 的市价单控制买入金额

---

## 4. 模块化设计

### 模块 A: 实时流数据中心 (stream_manager.py)

**职责**:
1. 订阅 `!miniTicker@arr` 获取全市场价格与成交额
2. 维护内存字典 `Price_History`，存储每个币种过去 60 分钟的 1m 数据
3. 每秒扫描，筛选符合 Volume Spike 的币种
4. 断连自动重连（币安 WS 24 小时强制断开）

**数据结构**:
```python
Price_History = {
    "XYZUSDT": {
        "volumes": deque(maxlen=60),      # 60 分钟滚动窗口
        "prices": deque(maxlen=60),       # 收盘价
        "highs": deque(maxlen=1440),      # 24 小时高点（1440 分钟）
        "last_update": timestamp
    }
}
```

### 模块 B: 信号验证引擎 (validator.py)

**职责**:
1. 接收 stream_manager 的初筛信号
2. 通过 REST API 获取 1h/4h K 线进行深度验证
3. 执行盘整期检查、突破有效性验证
4. 检查流动性（获取订单簿 Bid/Ask）

**验证流程**:
```
初筛信号 → 限频控制 → K线获取 → 盘整验证 → 流动性检查 → 输出有效信号
```

**限频控制**:
- 使用令牌桶或滑动窗口限制 API 调用频率
- 币安限制: 1200 requests/min（建议控制在 600/min 以内）

### 模块 C: 执行与持仓管理 (order_manager.py)

**职责**:
1. 执行买入订单（带滑点检查）
2. 管理持仓列表与状态
3. 监控止损止盈条件
4. 执行分级止盈和 Trailing Stop

**持仓数据结构**:
```python
Position = {
    "symbol": "XYZUSDT",
    "entry_price": 1.234,
    "quantity": 405.5,
    "entry_time": timestamp,
    "stop_loss": 1.185,           # 当前止损价
    "highest_price": 1.234,       # 持仓期间最高价
    "realized_pnl": 0,            # 已实现盈亏（分批止盈）
    "remaining_qty": 405.5,       # 剩余持仓量
    "tp_stage": 0                 # 止盈阶段 (0/1/2)
}
```

### 模块 D: 日志与监控 (logger.py)

**职责**:
1. 统一日志格式，支持回溯分析
2. 记录每次信号触发的详细指标
3. 异常监控与报警

**日志内容**:
```
[2024-01-15 14:32:15] SIGNAL | XYZUSDT | Vol: 523% | 1m_Chg: 2.3% | 24h_Break: YES | Spread: 0.12%
[2024-01-15 14:32:16] ORDER  | XYZUSDT | BUY | Price: 1.234 | Qty: 405.5 | Slippage: 0.08%
[2024-01-15 14:45:30] EXIT   | XYZUSDT | TP1 | Price: 1.296 | PnL: +5.02% | Qty: 121.6
```

### 模块 E: 合约数据监控 (futures_monitor.py) [扩展功能 - Phase 5+]

**职责**:
1. 订阅合约市场 WebSocket 获取标记价格与资金费率
2. 定时获取持仓量 (Open Interest) 数据
3. 计算基差、费率变化等衍生指标
4. 识别轧空机会并输出增强信号

**数据源**:
```yaml
websocket:
  - "wss://fstream.binance.com/ws/!markPrice@arr"  # 标记价格 + 费率

rest_api:
  - "GET /fapi/v1/openInterest"      # 单币种持仓量
  - "GET /fapi/v1/fundingRate"       # 历史费率
  - "GET /fapi/v1/premiumIndex"      # 溢价指数
```

**数据结构**:
```python
FuturesData = {
    "XYZUSDT": {
        "funding_rate": -0.0015,
        "next_funding_time": timestamp,
        "open_interest": 5000000,
        "oi_history": deque(maxlen=60),   # 1小时OI历史
        "mark_price": 1.235,
        "index_price": 1.233,
        "basis": 0.0016,
        "last_update": timestamp
    }
}
```

**与现有模块的集成**:
```
┌─────────────────┐     ┌─────────────────┐
│ stream_manager  │     │ futures_monitor │
│ (现货数据)       │     │ (合约数据)       │
└────────┬────────┘     └────────┬────────┘
         │                       │
         └───────────┬───────────┘
                     ▼
            ┌────────────────┐
            │   validator    │  ← 合并分析，信号增强
            └────────┬───────┘
                     ▼
            ┌────────────────┐
            │ order_manager  │
            └────────────────┘
```

---

## 5. 配置文件规范 (config.yaml)

```yaml
# 市场配置
market:
  exchange: "binance"
  quote_currency: "USDT"
  blacklist:
    - "BTCUSDT"
    - "ETHUSDT"
    - "BNBUSDT"
    - "SOLUSDT"
    - "USDCUSDT"
    - "DAIUSDT"
    - "TUSDUSDT"
    - "PAXGUSDT"
    - "WBTCUSDT"

# 触发阈值
thresholds:
  vol_multiplier: 5.0              # 单根K线成交量倍数
  vol_continuous_multiplier: 3.0   # 连续验证倍数
  min_24h_vol: 10000000            # 最小日交易额 (USDT)
  max_24h_vol: 100000000           # 最大日交易额 (USDT)
  price_surge_1m: 0.015            # 1分钟最小涨幅 (1.5%)
  breakout_margin: 0.005           # 突破幅度要求 (0.5%)
  max_spread: 0.005                # 最大买卖价差 (0.5%)
  consolidation_hours: 12          # 盘整期检查时长
  consolidation_max_change: 0.10   # 盘整期最大涨幅 (10%)

# 风险管理
risk:
  position_size: 500               # 每笔交易金额 (USDT)
  max_open_positions: 5            # 最大同时持仓数
  stop_loss_pct: 0.04              # 初始止损 (4%)
  breakeven_trigger: 0.03          # 保本止损触发涨幅 (3%)
  breakeven_level: 0.01            # 保本止损位置 (1%)
  slippage_tolerance: 0.01         # 最大滑点容忍 (1%)

# 止盈配置
take_profit:
  stage1_trigger: 0.05             # 第一阶段触发 (5%)
  stage1_portion: 0.30             # 第一阶段止盈比例 (30%)
  stage2_trigger: 0.10             # 第二阶段触发 (10%)
  stage2_portion: 0.30             # 第二阶段止盈比例 (30%)
  trailing_atr_multiplier: 2.0     # Trailing Stop ATR 倍数

# 大盘保护
market_protection:
  btc_5m_threshold: -0.01          # BTC 5分钟跌幅阈值
  btc_1h_threshold: -0.03          # BTC 1小时跌幅阈值

# API 限频
rate_limit:
  rest_requests_per_min: 600       # REST API 每分钟请求数上限
  ws_reconnect_delay: 5            # WebSocket 重连延迟 (秒)

# 运行模式
mode:
  paper_trading: true              # 模拟交易模式 (true=不实际下单)
  backtest: false                  # 回测模式

# ============================================
# 合约联动配置 [扩展功能 - Phase 5+]
# ============================================
futures:
  enabled: false                   # 是否启用合约监控

  # 轧空信号阈值
  squeeze_detection:
    funding_rate_warning: -0.0001  # 费率警告阈值 (-0.01%)
    funding_rate_critical: -0.0005 # 费率极端阈值 (-0.05%)
    oi_drop_threshold: 0.10        # OI下降触发阈值 (10%)

  # 信号增强权重
  signal_boost:
    funding_warning: 1             # 费率警告时优先级 +1
    funding_critical: 2            # 费率极端时优先级 +2

  # 轧空猎手模式
  squeeze_hunter:
    enabled: false                 # 是否启用独立轧空触发
    min_funding_rate: -0.0003      # 触发最低费率 (-0.03%)
    min_price_change: 0.01         # 触发最低涨幅 (1%)

  # 风险控制
  risk_adjustment:
    high_basis_threshold: 0.005    # 高基差警告阈值 (0.5%)
    position_reduction: 0.5        # 高基差时仓位缩减比例
```

---

## 6. 实现路径

### Phase 0: 项目脚手架 [当前阶段]
- [ ] 项目目录结构搭建
- [ ] 配置文件加载器 (config.py)
- [ ] 统一日志系统 (logger.py)
- [ ] 公共工具函数 (utils.py)
- [ ] 环境变量与密钥管理

### Phase 1: 数据层 (stream_manager.py) [当前阶段]
- [ ] WebSocket 连接管理（含自动重连）
- [ ] 全市场 miniTicker 订阅与解析
- [ ] 60 分钟滚动窗口数据维护
- [ ] 成交量异动初筛逻辑
- [ ] 数据持久化（程序重启恢复）

### Phase 2: 验证层 (validator.py) [当前阶段]
- [ ] REST API 封装（含限频控制）
- [ ] K 线数据获取与缓存
- [ ] 盘整期验证逻辑
- [ ] 突破有效性验证
- [ ] 流动性检查（订单簿）

### Phase 3: 执行层 (order_manager.py) [当前阶段]
- [ ] 订单执行（市价单 + 滑点检查）
- [ ] 持仓状态管理
- [ ] 止损监控与执行
- [ ] 分级止盈逻辑
- [ ] Trailing Stop 实现

### Phase 4: 整合与优化 [当前阶段]
- [ ] 主控程序 (main.py) 串联各模块
- [ ] 模拟交易模式
- [ ] 完善日志与异常监控
- [ ] 性能优化与压力测试

### Phase 5: 合约联动监控 [扩展阶段]
- [ ] 合约 WebSocket 数据订阅 (futures_monitor.py)
- [ ] 资金费率监控与历史记录
- [ ] 持仓量 (OI) 追踪与变化计算
- [ ] 基差计算与异常检测
- [ ] 轧空信号识别逻辑
- [ ] 与 validator 集成，实现信号增强
- [ ] 轧空猎手模式（独立触发）

### Phase 6: 回测与验证 [扩展阶段]
- [ ] 历史数据获取与存储
- [ ] 回测引擎开发
- [ ] 策略参数优化
- [ ] 合约数据回测支持

---

## 7. 技术注意事项

### 并发与限频
- 币安 REST API: 1200 req/min，建议控制在 600/min
- 使用异步编程 (asyncio) 提高效率
- 实现令牌桶限频器

### WebSocket 稳定性
- 币安 WS 24 小时强制断开，需自动重连
- 心跳检测，超时自动重连
- 数据跳帧检测（时间戳校验）

### 数据一致性
- 内存数据定期持久化到本地文件
- 程序启动时加载历史数据
- 异常退出时保存当前状态

### 异常处理
- API 报错重试机制（指数退避）
- 网络异常容错
- 订单状态异步确认

### 合约数据注意事项 [Phase 5+]
- 现货与合约 WebSocket 需分别连接（不同域名）
- 部分山寨币没有合约，需做好兼容处理
- 资金费率每 8 小时结算一次，注意结算时间点
- OI 数据通过 REST API 获取，需控制频率

---

## 8. 项目目录结构

```
shiit_spot_long/
├── config/
│   └── config.yaml          # 配置文件
├── src/
│   ├── __init__.py
│   ├── config.py            # 配置加载器
│   ├── logger.py            # 日志系统
│   ├── utils.py             # 工具函数
│   ├── stream_manager.py    # 现货数据流管理
│   ├── validator.py         # 信号验证
│   ├── order_manager.py     # 订单管理
│   └── futures_monitor.py   # [Phase 5+] 合约数据监控
├── data/
│   ├── price_history.json   # 现货历史数据
│   └── futures_data.json    # [Phase 5+] 合约历史数据
├── logs/
│   └── trading.log          # 交易日志
├── tests/
│   └── ...                  # 单元测试
├── main.py                  # 主程序入口
├── requirements.txt         # 依赖
└── README.md
```

---

## 9. 依赖库

```
ccxt>=4.0.0          # 交易所 API
websockets>=12.0     # WebSocket 客户端
aiohttp>=3.9.0       # 异步 HTTP
pandas>=2.0.0        # 数据处理
numpy>=1.24.0        # 数值计算
pyyaml>=6.0          # 配置文件解析
python-dotenv>=1.0.0 # 环境变量管理
loguru>=0.7.0        # 日志库
```

---

## 10. 版本记录

| 版本 | 日期 | 变更内容 |
|-----|------|---------|
| v1.0 | - | 初始设计 |
| v2.0 | - | 增加连续验证、分级止盈、详细配置 |
| v2.1 | - | 增加合约联动分析扩展设计（Phase 5+） |
