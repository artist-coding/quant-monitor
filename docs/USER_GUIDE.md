# zettaranc-skill 使用手册 & 操作手册

> 版本：v2.6.0 | 更新日期：2026-05-30
> 
> 面向用户：想使用此项目做量化分析的人

---

## 目录

1. [项目简介](#1-项目简介)
2. [快速开始](#2-快速开始)
3. [环境配置](#3-环境配置)
4. [数据库初始化与数据同步](#4-数据库初始化与数据同步)
5. [核心功能：股票分析](#5-核心功能股票分析)
6. [核心功能：选股扫描](#6-核心功能选股扫描)
7. [核心功能：自选股管理](#7-核心功能自选股管理)
8. [核心功能：持仓诊断](#8-核心功能持仓诊断)
9. [核心功能：策略回测](#9-核心功能策略回测)
10. [核心功能：随堂交易记录](#10-核心功能随堂交易记录)
11. [SKILL.md：Z哥角色扮演](#11-skillmdz哥角色扮演)
12. [知识文档索引](#12-知识文档索引)
13. [Python API 调用](#13-python-api-调用)
14. [测试与质量检查](#14-测试与质量检查)
15. [日常操作流程](#15-日常操作流程)
16. [常见问题](#16-常见问题)
17. [数据库结构说明](#17-数据库结构说明)
18. [技术指标体系](#18-技术指标体系)
19. [战法体系速查](#19-战法体系速查)

---

## 1 项目简介

zettaranc-skill 是一个**AI 思维框架蒸馏包 + 真实数据量化工具**的混合系统。

核心目标：将 B 站 UP 主 / 前阳光私募冠军基金经理 zettaranc（万千）的投资思维框架、决策启发式和表达 DNA，封装为可供 AI 工具（Claude Code / Cursor / Hermes Agent）调用的 Skill 文件（`SKILL.md`），同时提供基于真实 Tushare 行情数据的 Python 量化分析层。

### 1.1 双模式架构

| 模式 | 环境变量 | 说明 |
|------|---------|------|
| **JNB 模式** | `DATA_MODE=jnb` | 接入 Tushare 真实行情，60+ 技术指标实时计算，30+ 战法自动识别，选股、回测、持仓诊断全开 |
| **普通小万** | `DATA_MODE=websearch` | 纯 LLM 对话模式，不走任何外部数据接口，只聊框架和逻辑 |

### 1.2 架构分层

```
Tushare API（真实行情）
    ↓
data_sync.py（数据同步：日线/资金流/财报/指标）
    ↓
SQLite（本地缓存：8 张核心表，26 万+ 条数据）
    ↓
indicators/（60+ 技术指标计算：KDJ/MACD/BBI/RSI/WR/布林带/DMI/双线/砖形图...）
    ↓
strategies.py（30+ 战法识别：B1/B2/B3/SB1/长安战法/出货五式/三波理论/麒麟会...）
    ↓
screener.py（选股评分：曼城评分体系、趋势/量价/风险三维度）
    ↓
portfolio_diagnosis.py（持股诊断：防卖飞评分、出货信号、止损/止盈）
    ↓
backtest.py（策略组合回测：胜率/夏普/最大回撤）
    ↓
SKILL.md（LLM 角色层：Z 哥视角点评、多轮问诊、表达 DNA）
```

### 1.3 技术栈

| 层级 | 技术 |
|------|------|
| 数据管道 | Python 3.14（标准库 + sqlite3 + pathlib + dataclasses + enum） |
| 外部数据 | tushare Pro API（中转 https://tt.xiaodefa.cn） |
| 数据库 | SQLite（本地文件，8 张表 + 索引） |
| 数据处理 | pandas（Tushare 依赖） |
| 环境配置 | python-dotenv（.env 文件） |
| 测试框架 | pytest（261 用例） |
| 版本控制 | Git |

---

## 2 快速开始

### 2.1 安装

```bash
git clone https://github.com/lululu811/zettaranc-skill.git
cd zettaranc-skill
pip install -r requirements.txt
```

安装完成后会注册 `zt` 命令（快捷入口）。如不安装，也可以 `python -m modules.cli` 调用。

### 2.2 配置

```bash
cp .env.example .env
```

编辑 `.env` 文件：

```ini
# 数据模式: jnb(走Tushare API) 或 websearch(走网络搜索)
DATA_MODE=jnb

# Tushare API 配置
TUSHARE_TOKEN=你的56位token
# Tushare 中转 API 地址
TUSHARE_API_URL=https://tt.xiaodefa.cn

# 数据库配置
DATA_DIR=data
DB_PATH=data/stock_data.db
```

> **Token 获取**：前往 https://tushare.pro/user/token 复制你的 56 位 token。
> 
> **中转 API**：本项目使用 `https://tt.xiaodefa.cn` 中转服务，无需高级积分，限流 120 次/分钟。

### 2.3 验证安装

```bash
# 测试连通性
python -c "from modules.setup_wizard import test_jnb_connection; import os; print(test_jnb_connection(os.environ['TUSHARE_TOKEN']))"

# 预期输出: True
```

---

## 3 环境配置

### 3.1 环境变量详解

| 变量名 | 必填 | 默认值 | 说明 |
|--------|------|--------|------|
| `DATA_MODE` | 否 | `websearch` | `jnb` = 真实行情模式，`websearch` = 纯对话模式 |
| `TUSHARE_TOKEN` | 是（jnb 模式） | 无 | 56 位 Tushare Token |
| `TUSHARE_API_URL` | 是（jnb 模式） | 无 | 中转 API 地址，如 `https://tt.xiaodefa.cn` |
| `DB_PATH` | 否 | `data/stock_data.db` | 数据库路径，支持绝对/相对路径 |
| `DATA_DIR` | 否 | `data` | 数据目录 |

### 3.2 模式切换

```bash
# 切换到 websearch 模式（纯对话，不需要 Token）
python -c "from modules.setup_wizard import write_env_file; write_env_file(mode='websearch')"

# 切换回 JNB 模式（需 Token）
python -c "from modules.setup_wizard import write_env_file; write_env_file(token='你的token', mode='jnb')"
```

---

## 4 数据库初始化与数据同步

### 4.1 初始化数据库（只需做一次）

```bash
python -m modules.database
```

创建 8 张表：
- `stock_basic`：股票基本信息
- `daily_kline`：日线 K 线
- `indicator_cache`：技术指标缓存
- `moneyflow`：资金流向
- `financial_data`：财务报表
- `trade_signals`：交易信号
- `trade_records`：交易记录
- `watchlist`：自选股观察池

### 4.2 同步股票基本信息

```bash
python -m modules.data_sync sync
```

同步全量 5500+ 只 A 股基本信息，以及所有股票的 2 年日线数据。

> ⚠️ 全量同步需要较长时间（约 50 分钟），因为要对每只股票调用 Tushare API。

### 4.3 同步单只股票

```bash
# 同步单只股票日线（最近 365 天）
python -m modules.data_sync sync --ts_code 600487.SH --days 365

# 同步并计算指标缓存
python -m modules.data_sync sync --ts_code 600487.SH --days 365 --indicators
```

### 4.4 查看同步状态

```bash
python -m modules.data_sync status
```

输出示例：
```
==================================================
数据库: /path/to/data/stock_data.db
股票数量: 5525
K线数据: 25591
--------------------------------------------------
同步状态:
  stock_basic: 20260530 (success)
  daily_kline: 20260529 (success)
  moneyflow: 20260529 (success)
```

### 4.5 同步 Tushare 官方指标（用于验证）

```bash
python -m modules.data_sync stk-factor --ts_code 600487.SH --days 365
```

同步 Tushare 官方计算的 stk_factor 指标，用于与本项目自研指标做 diff 验证。

### 4.6 增量同步

系统自动检测最后同步日期，只拉取新增数据。如果 2 天内已同步过，会自动跳过。

---

## 5 核心功能：股票分析

### 5.1 CLI 调用

```bash
# 分析单只股票（默认 120 天）
python -m modules.cli analyze 600487.SH

# 指定分析天数
python -m modules.cli analyze 600487.SH --days 60
```

### 5.2 分析内容

分析结果包括：
1. **基础信息**：股票名称、代码、最新价、涨跌幅
2. **技术指标**：KDJ（K/D/J 值）、MACD（DIF/DEA/柱）、BBI、MA5/10/20/60、RSI、WR、布林带、DMI、量比
3. **价格形态**：双线战法（白线/黄线位置）、单针下 20、砖形图趋势、双枪信号
4. **量价信号**：防卖飞评分、出货信号、北斗/缩量/假阴真阳等
5. **战法识别**：B1/B2/B3/SB1 买点、S1/S2/S3 卖点、长安战法、娜娜图形等
6. **三波理论**：建仓波/拉升波/冲刺波识别
7. **麒麟会**：四阶段（吸筹/拉升/派发/回落）

### 5.3 Python API

```python
from modules.indicators import analyze_stock

result = analyze_stock("600487.SH", days=60)
print(f"J 值: {result.j:.1f}")
print(f"MACD DIF: {result.dif:.2f}")
print(f"信号: {result.signal}")
print(f"是否 B1: {result.is_b1}")
print(f"卖分: {result.sell_score}")
```

---

## 6 核心功能：选股扫描

### 6.1 CLI 调用

```bash
# B1 选股扫描（默认 25 只）
python -m modules.cli screen --strategy B1 --limit 20

# 完美图形扫描
python -m modules.cli screen --strategy 完美图形 --limit 10

# 超级 B1
python -m modules.cli screen --strategy 超级B1 --limit 10

# 建仓波选股
python -m modules.cli screen --strategy 建仓波 --limit 20

# 吸筹阶段
python -m modules.cli screen --strategy 吸筹 --limit 20

# 安全标的
python -m modules.cli screen --strategy 安全 --limit 20

# 全市场扫描（无策略限制）
python -m modules.cli screen
```

### 6.2 支持的筛选策略

| 策略 | 说明 | 适用场景 |
|------|------|---------|
| `B1` | J 值超卖 + N 型结构 + 缩量回调 | 左侧抄底 |
| `B2` | B1 后放量长阳确认 | 右侧追买 |
| `完美图形` | 趋势/量价/风险综合评分高 | 综合优选 |
| `超级B1` | 强 B1 信号，置信度更高 | 高确定性左侧 |
| `建仓波` | 三波理论识别为建仓阶段 | 趋势早期 |
| `吸筹` | 麒麟会四阶段为吸筹期 | 主力布局期 |
| `安全` | 低风险评分标的 | 保守型 |

### 6.3 评分体系

曼城评分体系包含三个维度：
- **趋势评分**（0-100）：均线排列、双线位置、MACD 趋势
- **量价评分**（0-100）：量价配合、量比异动、资金流
- **风险评分**（0-100）：波动率、回撤幅度、形态完整性

---

## 7 核心功能：自选股管理

### 7.1 添加自选股

```bash
# 添加单只股票
python -m modules.cli watchlist add 600487.SH

# 添加带标签
python -m modules.cli watchlist add 600487.SH --tags 通信设备,5G,波段
python -m modules.cli watchlist add 600036.SH --tags 银行,价值
```

### 7.2 查看自选股

```bash
# 查看所有
python -m modules.cli watchlist list

# 按标签筛选
python -m modules.cli watchlist list --tags 银行
```

### 7.3 批量扫描信号

```bash
python -m modules.cli watchlist scan
```

对观察池中所有股票进行批量战法识别，输出每只股票的当前信号。

### 7.4 移除自选股

```bash
python -m modules.cli watchlist remove 600487.SH
```

### 7.5 Python API

```python
from modules.watchlist import WatchList

wl = WatchList()
wl.add("600487.SH", tags=["通信", "5G"])
wl.add("600036.SH", tags=["银行"])
wl.list_all()
wl.list_by_tag("银行")
wl.scan_all()  # 批量扫描
wl.remove("600487.SH")
```

---

## 8 核心功能：持仓诊断

### 8.1 CLI 调用

```bash
# 诊断单只股票
python -m modules.cli diagnose 600487.SH

# 指定诊断天数
python -m modules.cli diagnose 600487.SH --days 100
```

### 8.2 诊断内容

持仓诊断报告包含：
1. **当前状态**：趋势判断（多头/震荡/空头/MACD 一票否决）
2. **防卖飞评分**：1-5 分制，5 分 = 让利润飞
3. **出货信号**：出货五式扫描，S1/S2/S3 逃顶识别
4. **战法匹配**：当前是否在 B1/B2/B3 可买区间
5. **止损/止盈位**：基于战法计算的具体价位
6. **麒麟会阶段**：主力处于哪一阶段
7. **风险等级**：LOW / MEDIUM / HIGH / CRITICAL
8. **操作建议**：文字版诊断建议

### 8.3 诊断示例输出

```
平安银行(000001.SZ)
  当前状态: MACD一票否决，不宜买入
  操作建议: S1逃顶信号出现，建议减仓或清仓
  止损位: 10.52
  目标价: 12.48
  风险等级: CRITICAL

贵州茅台(600519.SH)
  当前状态: 震荡整理
  操作建议: 防卖飞评分5/5，持股让利润飞
  止损位: 1273.30
  目标价: 1509.58
  风险等级: LOW
```

---

## 9 核心功能：策略回测

### 9.1 Python API

```python
from modules.backtest import backtest_multi_strategy, backtest_portfolio

# 单股票多策略融合回测
result = backtest_multi_strategy(
    ts_code="600487.SH",
    days=120,
    strategies=["b1", "b2"],
    position_pct=0.3  # 单信号 30% 仓位
)
print(f"胜率: {result.win_rate:.1%}")
print(f"夏普比率: {result.sharpe_ratio:.2f}")
print(f"最大回撤: {result.max_drawdown:.1%}")

# 多股票组合回测
portfolio_result = backtest_portfolio(
    ts_codes=["600487.SH", "000001.SZ", "600519.SH"],
    days=120,
    max_weight=0.4  # 单股上限 40%
)
```

### 9.2 回测指标

- **胜率**：盈利交易 / 总交易
- **平均收益**：单笔交易平均收益率
- **夏普比率**：风险调整后收益
- **最大回撤**：资金曲线最大跌幅
- **总收益**：回测期间累计收益率
- **交易次数**：信号触发次数

---

## 10 核心功能：随堂交易记录

### 10.1 录入交易记录

```python
from modules.trade_manager import TradeManager

tm = TradeManager()

# 记录买入
tm.add_trade(
    ts_code="600487.SH",
    trade_date="20260528",
    action="BUY",
    price=22.81,
    quantity=1000,
    reason="B1信号触发，J值超卖",
    signal_type="B1"
)

# 记录卖出
tm.add_trade(
    ts_code="600487.SH",
    trade_date="20260529",
    action="SELL",
    price=23.50,
    quantity=500,
    reason="浮盈过半，减半仓位",
    signal_type="SELL"
)
```

### 10.2 查看持仓

```python
# 当前持仓
positions = tm.get_positions()
for p in positions:
    print(f"{p.ts_code}: {p.quantity}股, 成本{p.avg_price}")

# 盈亏统计
pnl = tm.calculate_pnl()
print(f"总盈亏: {pnl['total_pnl']}")
print(f"胜率: {pnl['win_rate']:.1%}")
```

### 10.3 随堂测试复盘

```python
from modules.trade_reviewer import TradeReviewer

reviewer = TradeReviewer()
context = reviewer.prepare_review_context("600487.SH")
# 准备交割单数据 + K线 + 指标 + 战法信号，供 LLM 用 Z哥角色点评
```

---

## 11 SKILL.md：Z哥角色扮演

### 11.1 什么是 SKILL.md

`SKILL.md` 是项目的核心 AI 角色扮演协议。当 AI Agent 加载此文件后，会以 zettaranc（Z 哥）的身份回应投资相关问题。

### 11.2 触发方式

当用户提到以下关键词时触发：
- 「用 Z 哥的视角」
- 「Z 哥会怎么看」
- 「万千模式」
- 「zettaranc perspective」
- 「切换到 Z 哥」
- 「如果 Z 哥会怎么做」

### 11.3 角色特征

1. **第一人称**：用「我」而非「Z 哥认为...」
2. **表达节奏**：分 1/2/3/4 点拆解，用具体数字或案例
3. **职业背书**：必要时提及私募基金管理经验
4. **金句收尾**：以金句或反问收尾
5. **诚实边界**：对不确定的问题用 Z 哥会有的犹豫方式犹豫

### 11.4 工作流

1. **问题分类**：需要事实的问题 → 先研究再回答；纯框架问题 → 直接用心智模型
2. **个股问诊**：多轮问诊（周期 → 状态 → 仓位 → 诊断），不可一句回答
3. **数据支撑**：遇到需要事实支撑的问题，先跑数据再回答

### 11.5 6 个核心心智模型

详见 `SKILL.md` 文件，包括：
- 择时永远第一
- B1/B2/B3 买点体系
- 出货五式逃顶
- 仓位管理铁律
- 交易心理防线
- 少妇模拟器思维

### 11.6 30 条决策启发式

详见 `SKILL.md`，涵盖买入/卖出/持股/风控等各类决策场景的具体规则。

---

## 12 知识文档索引

`knowledge/` 目录下包含 14 篇交易体系文档，是量化代码的语料基础：

| 文件 | 核心内容 |
|------|---------|
| `trading-core.md` | 四层交易结构、少妇战法 SOP、B1/B2/B3、量比战法 |
| `indicators.md` | MACD 一票否决、筹码理论、麒麟会、三波理论 |
| `sell-discipline.md` | 防卖飞 V1.4、出货五式、S1/S2/S3 逃顶 |
| `position-management.md` | 仓位铁律、三层防火墙 |
| `market-macro.md` | 周期思维、逆向操作、四年周期 |
| `portfolio-management.md` | 新曼城 4231、ETF 躺平、ABC 建仓 |
| `trading-psychology.md` | 交易免疫系统、斗牛士心法、散户魔咒 |
| `stock-glossary.md` | 60+ 个股黑话/代号 |
| `trend-lines.md` | 双线战法、三道防线、牛绳理论 |
| `exit-strategies.md` | S1/S2/S3 逃顶、摸顶税 |
| `key-candles.md` | 关键 K 理论、6 种趋势转换 |
| `advanced-patterns.md` | 长安战法、平行重炮、对称 VA |
| `data_dictionary.md` | 输入数据字典（DailyBar/MoneyFlow/Financial） |
| `signal_dictionary.md` | 输出信号字典 |

---

## 13 Python API 调用

### 13.1 分析单只股票

```python
from modules.indicators import analyze_stock

result = analyze_stock("600487.SH", days=60)
# result 是 IndicatorResult dataclass
print(f"J={result.j}, DIF={result.dif}")
print(f"B1={result.is_b1}, B2={result.is_b2}")
print(f"卖分={result.sell_score}, 信号={result.signal}")
```

### 13.2 战法识别

```python
from modules.strategies import detect_all_strategies

signals = detect_all_strategies("600487.SH", days=60)
for s in signals:
    print(f"{s.trade_date}: {s.strategy} 置信度={s.confidence} 操作={s.action}")
```

### 13.3 选股评分

```python
from modules.screener import screen_stocks

results = screen_stocks(criteria="b1", max_stocks=50, use_parallel=False)
for r in sorted(results, key=lambda x: x.score, reverse=True)[:10]:
    print(f"{r.ts_code}({r.name}): 总分={r.score}")
```

### 13.4 持股诊断

```python
from modules.portfolio_diagnosis import diagnose_stock, format_report

report = diagnose_stock("600487.SH", days=100)
print(format_report(report))
```

### 13.5 策略回测

```python
from modules.backtest import backtest_multi_strategy

result = backtest_multi_strategy(
    ts_code="600487.SH",
    days=120,
    strategies=["b1", "b2"],
    position_pct=0.3
)
print(f"胜率: {result.win_rate:.1%}")
```

### 13.6 交易记录 CRUD

```python
from modules.trade_manager import TradeManager

tm = TradeManager()
tm.add_trade(ts_code="600487.SH", trade_date="20260528",
             action="BUY", price=22.81, quantity=1000,
             reason="B1信号", signal_type="B1")
positions = tm.get_positions()
pnl = tm.calculate_pnl()
```

### 13.7 获取 K 线数据

```python
from modules.indicators import get_kline_data

klines = get_kline_data("600487.SH", days=60)
for k in klines[-5:]:
    print(f"{k.trade_date}: 开{k.open} 高{k.high} 低{k.low} 收{k.close} 量{k.vol}")
```

### 13.8 获取实时行情

```python
from modules.indicators import get_realtime_data

data = get_realtime_data("600487.SH")
print(f"最新价: {data.close}, 涨跌: {data.pct_chg}%")
```

---

## 14 测试与质量检查

### 14.1 运行测试

```bash
# 全部测试（预期：261 passed, 1 skipped）
python -m pytest tests/ -v

# 单文件测试
python -m pytest tests/test_indicators.py -v
python -m pytest tests/test_strategies.py -v
python -m pytest tests/test_screener.py -v
```

### 14.2 测试覆盖范围

| 测试文件 | 覆盖内容 | 用例数 |
|---------|---------|--------|
| `test_database.py` | 数据库连接、事务、表增删、幂等性 | ~15 |
| `test_indicators.py` | 56+ 指标计算（MA/EMA/KDJ/MACD/布林带/砖形图/DMI...） | ~56 |
| `test_strategies.py` | B1/B2/B3/SB1/长安/娜娜/异动地量/全量检测 | ~15 |
| `test_screener.py` | 评分模型、趋势/量价/风险评分 | ~15 |
| `test_setup_wizard.py` | 环境检测、模式切换 | ~8 |
| `test_exam_rules.py` | 考试规则验证（B1/砖形图/单针） | ~25 |
| `test_trade_manager.py` | 交易记录 CRUD、盈亏计算 | ~5 |
| `test_portfolio_diagnosis.py` | 持股检查、防卖飞、战法匹配 | ~10 |
| `test_watchlist.py` | 观察池增删改查、批量扫描 | ~4 |
| `test_wave_theory.py` | 三波理论识别 | ~12 |
| `test_kirin_detector.py` | 麒麟会四阶段 | ~15 |

### 14.3 SKILL.md 质量检查

```bash
python scripts/quality_check.py SKILL.md
```

8 项维度自动检查：触发条件、角色扮演规则、工作流完整性、心智模型、启发式数量、表达 DNA、诚实边界、格式规范。

---

## 15 日常操作流程

### 15.1 每日五步工作流

```bash
# Step 1: 更新数据（增量同步）
python -m modules.data_sync sync --ts_code 600487.SH --days 1

# Step 2: 查看观察池信号
python -m modules.cli watchlist scan

# Step 3: B1 选股扫描
python -m modules.cli screen --strategy B1 --limit 20

# Step 4: 诊断持仓
python -m modules.cli diagnose 600487.SH

# Step 5: 分析感兴趣的股票
python -m modules.cli analyze 000001.SZ
python -m modules.cli analyze 600519.SH
```

### 15.2 每周维护

```bash
# 更新股票基本信息（变动不频繁，每周一次）
python -m modules.data_sync sync

# 同步资金流数据
python -c "
from modules.data_sync import DataSyncer
from datetime import datetime, timedelta
syncer = DataSyncer()
for d in range(5):
    date = (datetime.now() - timedelta(days=d)).strftime('%Y%m%d')
    syncer.sync_moneyflow('000001.SZ', date)
"

# 运行全量测试
python -m pytest tests/ -v
```

### 15.3 每月维护

```bash
# 同步 Tushare 官方指标（diff 验证用）
python -m modules.data_sync stk-factor --ts_code 600487.SH --days 30

# 同步财务数据
python -c "
from modules.scripts import sync_missing_data
# 运行财务数据补充脚本
"

# 检查数据库状态
python -m modules.data_sync status
```

---

## 16 常见问题

### 16.1 连通性问题

**Q: 测试连通性返回 False / 报 ProxyError**

A: 检查以下几点：
1. `.env` 中 `TUSHARE_TOKEN` 是否为 56 位有效 token
2. `TUSHARE_API_URL` 是否已配置为 `https://tt.xiaodefa.cn`
3. 中转服务是否正在维护（访问 https://tt.xiaodefa.cn/youxiaoqi 查询）
4. 网络是否正常（ping tt.xiaodefa.cn）

**Q: 报 "No module named 'dotenv'"**

A: 确保使用的是安装了依赖的 Python 版本：
```bash
# 不要用系统默认 python（3.9）
python3 -m pip install -r requirements.txt
# 或用完整路径
/opt/homebrew/bin/python3 -m pip install -r requirements.txt
```

### 16.2 数据同步问题

**Q: 同步进度卡住了**

A: Tushare 限流 120 次/分钟，全量 5500 只股票约需 50 分钟。这是正常现象。

**Q: 偶发超时怎么办**

A: 中转 API 偶尔返回 Read timeout 或维护中，增量同步会自动跳过，下次同步时补上。

**Q: 数据库文件在哪**

A: 默认在 `data/stock_data.db`（项目根目录下的 data 文件夹）。路径从 `DB_PATH` 环境变量读取。

### 16.3 指标计算问题

**Q: 某只股票分析返回空结果**

A: 检查该股票是否有 K 线数据：
```python
from modules.indicators import get_kline_data
klines = get_kline_data("XXXXXX.SH", days=60)
print(len(klines))  # 如果为 0，说明没有数据
```

**Q: 战法识别返回 0 个信号**

A: 可能原因：
1. K 线数据不足（至少需要 120 天才能识别 B1）
2. 当前确实没有战法信号触发（正常现象）
3. 检查 `strategies.py` 的 DB 路径是否正确（已修复为 `parent.parent`）

### 16.4 SKILL.md 问题

**Q: AI 没有用 Z 哥角色回答**

A: 确保：
1. 使用了 `zettaranc-perspective` skill 触发条件
2. 对话中包含了触发关键词
3. AI 工具已加载 `SKILL.md`

**Q: 首次对话没有引导选择模式**

A: SKILL.md 设计了首次激活时的模式检查流程，首次使用时会自动引导选择 JNB 或 websearch 模式。

---

## 17 数据库结构说明

### 17.1 8 张核心表

#### stock_basic（股票基本信息）

| 字段 | 类型 | 说明 |
|------|------|------|
| `ts_code` | TEXT PK | 股票代码（如 600487.SH） |
| `name` | TEXT | 股票名称 |
| `area` | TEXT | 地区 |
| `industry` | TEXT | 行业 |
| `market` | TEXT | 市场类型 |
| `list_date` | TEXT | 上市日期 |
| `is_hs` | TEXT | 是否沪/深股通 |

#### daily_kline（日线 K 线）

| 字段 | 类型 | 说明 |
|------|------|------|
| `ts_code` | TEXT | 股票代码 |
| `trade_date` | TEXT | 交易日期（YYYYMMDD） |
| `open` | REAL | 开盘价 |
| `high` | REAL | 最高价 |
| `low` | REAL | 最低价 |
| `close` | REAL | 收盘价 |
| `vol` | REAL | 成交量 |
| `amount` | REAL | 成交额 |
| `pct_chg` | REAL | 涨跌幅(%) |
| `is_limit_up` | INTEGER | 是否涨停 |
| `is_limit_down` | INTEGER | 是否跌停 |

#### indicator_cache（技术指标缓存）

每日快照，包含 60+ 指标的每日计算结果：KDJ、MACD、BBI、MA、RSI、WR、布林带、双线、砖形图、DMI、量比、信号等 60+ 列。

#### moneyflow（资金流向）

| 字段 | 类型 | 说明 |
|------|------|------|
| `buy_sm_amount` | REAL | 小单买入额 |
| `buy_md_amount` | REAL | 中单买入额 |
| `buy_lg_amount` | REAL | 大单买入额 |
| `buy_elg_amount` | REAL | 特大单买入额 |
| `sell_sm_amount` | REAL | 小单卖出额 |
| `net_mf` | REAL | 净流入 |

#### financial_data（财务报表）

| 字段 | 类型 | 说明 |
|------|------|------|
| `revenue` | REAL | 营业收入 |
| `net_profit` | REAL | 净利润 |
| `total_assets` | REAL | 总资产 |
| `total_liab` | REAL | 总负债 |
| `equity` | REAL | 股东权益 |
| `pe` | REAL | 市盈率 |
| `pb` | REAL | 市净率 |
| `ps` | REAL | 市销率 |

#### watchlist（自选股观察池）

| 字段 | 类型 | 说明 |
|------|------|------|
| `ts_code` | TEXT PK | 股票代码 |
| `name` | TEXT | 股票名称 |
| `tags` | TEXT | 标签（逗号分隔） |
| `add_date` | TEXT | 添加日期 |
| `notes` | TEXT | 备注 |

#### trade_records（交易记录）

记录随堂测试/模拟交易：买卖价格、数量、原因、信号类型、Z哥点评。

#### sync_log（同步日志）

记录每次数据同步的类型、时间、状态。

### 17.2 索引设计

每张表均建立复合索引，关键字段以 `ts_code + trade_date DESC` 排序。

---

## 18 技术指标体系

### 18.1 基础指标（通达信标准）

| 指标 | 参数 | 说明 |
|------|------|------|
| MA | 5/10/20/60 | 移动平均线 |
| EMA | 5/10/20/60 | 指数移动平均 |
| SMA | 通达信递推 | 简单移动平均 |

### 18.2 经典指标

| 指标 | 参数 | 说明 |
|------|------|------|
| KDJ | 9,3,3 递推 | 随机指标 |
| MACD | 12,26,9 递推 | 指数平滑异同移动平均 |
| RSI | 6/12/24 递推 SMA | 相对强弱指标 |
| WR | 5/10 | 威廉指标 |
| BBI | 4 参数 | 多空指标 |
| 布林带 | 20,2 | 上/中/下轨 + 宽度 + 位置 |
| DMI | 14 | +DI/-DI/ADX |

### 18.3 特色指标

| 指标 | 说明 |
|------|------|
| 双线战法 | 白线 EMA(EMA(C,10),10) + 黄线 4参数 BBI |
| 单针下 20/30 | 探底信号 |
| 砖形图 | 递推计算，与通达信一致 |
| 量比 | 当日成交量/过去5日平均量 |
| 防卖飞评分 | 5 分制自动化 |

### 18.4 量价信号

- 北斗信号
- 缩量信号
- 假阴真阳
- 放量阴线
- 异动地量

---

## 19 战法体系速查

### 19.1 买入战法

| 战法 | 触发条件 | 适用场景 |
|------|---------|---------|
| **B1** | J ≤ -10 + N型结构 + 缩量回调 | 左侧抄底 |
| **B2** | B1 后涨幅 ≥ 4% + 放量 + J < 55 + 无上影线 | 右侧确认 |
| **B3** | B2 后十字星/小阴线 + 平开一致 | 趋势延续 |
| **SB1** | 超级 B1，强信号版本 | 高确定性 |
| **长安战法** | B1 + 放量长阳 + 缩半量 | 经典反转 |
| **四分之三阴量** | 真假突破判断 | 突破确认 |
| **娜娜图形** | 特定形态识别 | 趋势转折 |
| **坑里起好货** | 底部坑形态 | 低位布局 |
| **平行重炮** | 平行上涨形态 | 强势追涨 |
| **对称 VA** | 时间+空间对称 | 对称反转 |

### 19.2 卖出/逃顶战法

| 战法 | 触发条件 |
|------|---------|
| **S1** | 高位放量阴线 |
| **S2** | 挑前高 + MACD 顶背离 |
| **S3** | 反抽巨量下沿 |
| **出货五式** | 加速天量大阴/次高巨量长阴/阶梯放量下跌/双头巨阴/绿肥红瘦 |
| **滴滴战法** | 高位连续两根阴线下台阶，第二根收盘 < 第一根最低价 |
| **砖形图止损** | 红砖翻绿 |

### 19.3 趋势/阶段战法

| 战法 | 说明 |
|------|------|
| **三波理论** | 建仓波（25-50%无涨停）→ 拉升波（快速脱离有涨停）→ 冲刺波（最后主升） |
| **麒麟会四阶段** | 吸筹 → 拉升 → 派发 → 回落（评分制识别） |
| **双线战法** | 白线在黄线上 = 多头，交叉 = 金叉/死叉 |
| **牛绳理论** | 双线战法的抽象：白线牵牛，跌破 = 牛绳断 |

### 19.4 选股体系

- **曼城评分**：趋势/量价/风险三维综合评分
- **完美图形**：特定形态加分
- **B1 扫描**：全市场 B1 信号筛选

---

## 附录 A：文件修改优先级

| 优先级 | 文件 | 说明 |
|--------|------|------|
| 1 | `SKILL.md` | 直接影响 Skill 表现，任何改动需语料支撑 |
| 2 | `modules/*.py` | 数据层代码，改动需同步更新测试 |
| 3 | `knowledge/*.md` | 知识文档，补充新语料或修正发现时更新 |
| 4 | `references/research/*.md` | 调研档案，新增语料源时更新 |
| 5 | `README.md` / `CHANGELOG.md` | 版本发布时同步更新 |
| 6 | `scripts/` | 仅在数据管道或检查逻辑需要改进时修改 |

## 附录 B：版本规范

遵循语义化版本：

| 位 | 含义 | 示例 |
|----|------|------|
| MAJOR | 心智模型级别重构 | v1.3.0：将 6 个心智模型重组为 5 个 |
| MINOR | 新增战术/启发式/语料/模块 | v2.0.0：新增 Tushare 数据层和 8 个 Python 模块 |
| PATCH | 排版修正、安全修复、数字更新 | v2.1.1：移除 URL 硬编码 |

## 附录 C：开发规范

- 所有脚本使用 Python 3.14
- 中文注释和文档字符串
- 编辑器使用 `.editorconfig` 配置（Python 4 空格缩进）
- 数据库路径统一从 `DB_PATH` 环境变量读取
- 所有 Tushare API 调用必须带 `_rate_limit()`
- 错误处理返回空 DataFrame/None 而非抛异常

## 附录 D：安全与合规

1. 此项目**不构成任何投资建议**，金融市场风险极高
2. Tushare Token 通过 `.env` 管理，绝不硬编码
3. 语料截止期：2026-04-18 及后续更新
4. 信息截止标注在 `SKILL.md` 的「诚实边界」一节

---

> 心中有牛熊，唯有纪律坚。
> 
> Love and Share 🖤
