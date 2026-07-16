# 日线买卖评分与持仓生命周期系统设计方案

> 文档状态：Draft 0.9（买点确认门槛与独立事件回测基础版已落地，尚未完成样本外校准）
> 创建日期：2026-07-12
> 最近更新：2026-07-13
> 适用项目：zettaranc-skill-main
> 用途：指导“单只股票日线持仓过程量化 → 推广至自选池”的开发。

## 0. 当前实施状态

截至 2026-07-12，阶段 0～2 的基础引擎和阶段 4 的安全契约已经落地：

- 已修正白线，使其真正实现 `EMA(EMA(C,10),10)`。
- 已修正 MACD 截短序列与日K日期错位，包括背离窗口映射。
- 已修正模拟器读取量比、牛绳返回字段不一致的问题。
- 已建立严格 `as_of_date` 契约：未来、乱序、重复、非法日期均直接拒绝。
- 已建立日线派生特征、具名策略证据、连续评分、仓位阶梯和订单契约。
- 已建立只使用成交时点可知信息的开盘/收盘执行模型。
- 已建立单股日事件回放，并可用相同输入成对比较 `SAME_CLOSE_RESEARCH` 与 `NEXT_OPEN_STRICT`。
- 已建立量化证据快照、LLM 调整提案、确定性 guardrail 和失败时精确回退 `QUANT_ONLY` 的契约；尚未连接外部模型。
- 已将超上限持仓建模为不可作为目标的溢出档，LLM 不能取消强制减仓。
- 已要求已有持仓必须携带持久化止损，避免用当日低点向下重算止损。
- 已建立订单/成交标识、执行配置指纹、风险仓位计算、T+1 和持仓生命周期基础逻辑。
- 已建立信号价/原始成交价双价格数据契约、复权因子清单和公司行动日期字段；尚未接入回放主链。
- 已建立分批库存、FIFO、可卖日期和应收权益的第一版数据模型；仍有迁移、幂等和金额精度边界待收紧，尚不能视为生产账本。
- 已将连续买入分与“买点确认”分离：没有已匹配的入场 variant、有效信号日止损或无冲突条件时，即使上下文总分较高也不能生成 OPEN/ADD。
- 已将买入共振限制为明确的入场 variant 白名单，S1/S2/S3和未确认的假摔证据不能反向抬高买入分。
- 已提供独立买点事件研究 `zt daily-portfolio backtest-buy`：D日收盘评分、D+1原始开盘执行，输出1/3/5/10/20日结果、MFE/MAE、止损触达、固定期限R、分数分箱和逐variant统计。
- 已提供独立入口 `zt daily-portfolio score` 与 `zt daily-portfolio replay-pair`；它们不调用旧回测、模拟器或旧自选扫描逻辑。
- `tests/test_daily_*.py` 当前为 229 项通过。完整仓库仍有少量既有 Windows/统计兼容失败，与本模块无关。

当前开发顺序和状态如下：

| 里程碑 | 内容 | 当前状态 |
|---|---|---|
| M0 | 修正旧指标、拆分具名 B/S 策略证据 | 已完成基础版本 |
| M1 | 单股评分、D+1 开盘买入、双退出回放、状态机 | 已完成基础版本，正在收紧边界 |
| M2 | 安全 CLI、显式市场快照和交易所日历 | 已完成基础版本 |
| M3 | 买点事件研究、单股历史校准、walk-forward、冻结 v1 参数 | 事件研究基础版完成；真实双价校准未开始 |
| M4 | 结构化 LLM Provider、缓存、审计和多 Agent 对照 | 仅契约完成，未连接模型 |
| M5 | 自选池、真实持仓账本和组合风险层 | 未开始 |
| M6 | 每日任务、前端、人工确认和真实成交回录 | 未开始 |

这里的“已完成基础版本”只表示代码口径和测试骨架成立，不表示策略已经证明有效，更不表示可以自动下单。阶段 3 的样本外校准通过前，所有分数都只能视为研究初值。

### 0.1 2026-07-13 实施检查点

以下持仓复盘原则已经写入第11章；对应的完整周期复盘代码仍在后续阶段：

- 以完整 `holding_cycle` 复盘首次建仓、加仓、持有、减仓、退出和卖后路径。
- 将“当时决策过程是否正确”与“事后结果是否赚钱”分开，正常止损不自动归因为买点错误。
- 将“退出后继续上涨”与“当时是否应该退出”分开，顶背离后的合理减仓可以是 `JUSTIFIED_RISK_REDUCTION`，不自动算卖飞。
- 用扣费后的期望R、Profit Factor、盈亏比和风险判断策略有效性；五笔三止损两盈利仍可能是正期望，但不足以证明策略已统计验证。
- 对买入分、卖出分及B1/B2/B3逐项做严格模式消融、噪音检验和样本外稳定性分析，不因单独胜率低直接删除战法。

旧的 `trade_reviewer`、`review_generator`、`portfolio_diagnosis` 和 simulator 文案层不能直接复用：其中存在用当前分析解释历史交易、旧成交时点不一致以及自由文本缺少证据边界等问题。恢复开发时应新建独立、版本化的 `cycle-review` 契约，并严格分离：

```text
DECISION_TIME：当时可见证据、规则合规性、实际执行偏差
OUTCOME_TIME：已实现R、MFE/MAE、卖后5/10/20日反事实
LLM_REVIEW：只能解释上述冻结事实并提出下一版本研究假设
```

恢复开发后，买点阶段已经完成“确认门槛 + 独立事件研究 + CLI + 指纹 + 测试”的基础版。后续顺序调整为：

1. 接入双价格、公司行动账本和point-in-time市场/日历数据，使买点历史收益具备正式校准资格。
2. 对第一只股票运行买入分单调性、B1/B2/B3消融、权重敏感度和walk-forward，冻结第一个候选参数版本。
3. 实现完整持仓周期、决策轨迹、卖点、卖后反事实和结构化LLM复盘。
4. 完成跨股票验证后再推广到自选池；LLM不得改写历史分类、成交或已冻结权重，只能提出下一版本研究假设。

需要在研究配置中显式确定、但不妨碍先写数据契约的参数包括：卖后主要观察窗口和“显著上涨/有限回撤”阈值、单笔可承受风险、组合最大可接受回撤。任何默认值只能标记为研究初值。

当前单股验证入口：

```text
zt daily-portfolio score TS_CODE --as-of YYYYMMDD --market-file market.json --json

zt daily-portfolio backtest-buy TS_CODE \
  --start YYYYMMDD --end YYYYMMDD \
  --calendar-file calendar.json --exchange SSE \
  --market-file market.json --include-events --json

zt daily-portfolio replay-pair TS_CODE \
  --start YYYYMMDD --end YYYYMMDD \
  --exchange SSE --calendar-file calendar.json \
  --market-file market.json --json
```

`score` 必须使用该评分日的显式市场快照；`replay-pair` 还必须使用显式交易所日历，并包含回放结束日之后的下一交易日。`backtest-buy` 的日历要在结束日后覆盖至少两倍最大观察窗口，并会再次检查实际未来个股K线数量，避免把停牌造成的末端截尾静默排除。已有持仓文件必须给出整数股数、可卖股数、成本、实际仓位比例和持久化止损。缺少这些关键输入时命令直接失败，不生成看似有效的结果。

最小输入格式：

```json
{
  "schema_version": "market-snapshots-v1",
  "version": "market-context-research-v1",
  "source": "POINT_IN_TIME_SOURCE_DESCRIPTION",
  "snapshots": [
    {"trade_date": "20260105", "score": 55.0}
  ]
}
```

```json
{
  "schema_version": "exchange-calendar-v1",
  "exchange": "SSE",
  "source": "OFFICIAL_EXCHANGE_CALENDAR_SNAPSHOT",
  "dates": ["20260105", "20260106", "20260107"]
}
```

市场文件不能用事后信息生成，也不能为了让回测通过而批量填充静默中性分。买点CLI在双价格接入前只允许 SQLite RAW，并会在文本和JSON中声明该结果尚不具备多年正式收益解释资格。

### 0.1 M3 数据审计结论

本地 `data/stock_data.db` 已通过只读 `quick_check`，包含约 1021 万行、5531 个代码、2016-07-11～2026-07-10 共 2428 个交易日的原始日线。旧候选池中的 `600519.SH`、`000858.SZ`、`601318.SH` 等具备较长历史；若用户不另行指定，首只研究候选暂定为 `600519.SH`，随后必须用其他股票做跨股票验证。

但当前数据不能直接用于多年参数校准：

- SQLite 由 `pro.daily` 同步，保存未复权原始价；在线 Tushare 入口则使用 `qfq`，两者语义不一致。
- 本地没有 `adj_factor`、分红送转账本、指数行情和正式交易日历。
- 历史横截面只有当前上市股票的残缺基础信息，用它回算市场宽度会产生幸存者偏差。
- `watchlist`、持仓和交易记录均为空；旧 `stocks_final.json` 只能作为研究候选，不能冒充用户真实自选池或持仓。

因此 M3 先建立双价格数据契约：复权且 point-in-time 的信号价只用于指标，原始价用于成交、费用、涨跌停和现金账本，公司行动使用独立账本调整股数与现金。缺少任一数据来源或内容哈希时，多年校准必须失败关闭。当前已经新增 `price_data.py` 的价格口径、逐日双价对齐、内容哈希和公司行动来源契约；事件回放接入仍是下一开发项。

旧回测入口已经确认存在成交口径冲突，在完成统一事件引擎接入前，其收益结果只能视为旧实验结果：

| 旧入口 | 当前实际买入口径 | 问题 |
|---|---|---|
| `modules/simulator/simulator.py` | D日完整K线出信号后按D日开盘 | 明确同K线前视 |
| `modules/backtest.py` | D日完整K线出信号后按D日收盘 | 无法真实复现 |
| `modules/backtest_six_step.py` / `loop_engine.py` | D日完整K线出信号后按D日收盘 | 无法真实复现 |

因此后续不再分别维护三套成交循环；所有新验证必须通过 `modules/daily_portfolio/` 的统一评分、订单和事件模型。

## 1. 核心方向

本项目不做盘中实时行情采集，不做分钟级或高频策略。所有信号以已经走完并确认的日K为依据。

第一阶段只选择一只股票，完整量化其持仓生命周期：

```text
空仓
  ↓
出现买入评分
  ↓
下一交易日开盘建仓
  ↓
根据后续每日评分加仓或持有
  ↓
卖出评分升高后减仓或退出
  ↓
完成一轮持仓周期
```

单股逻辑验证稳定后，再将同一套评分和状态机推广到用户自选池中的所有股票。

## 2. 日线数据与决策时间定义

### 2.1 信号日

用 `D` 表示信号日。只有当D日走势已经结束后，D日的以下字段才是确认数据：

- `open[D]`
- `high[D]`
- `low[D]`
- `close[D]`
- `volume[D]`
- `amount[D]`

D日评分只能使用D日及以前的数据：

```text
available_data(D) = bars[0:D]
```

任何评分函数都不得读取D+1及之后的数据。

### 2.2 买入执行规则

买入采用严格的“收盘出信号、次日开盘成交”：

```text
D日走势结束
  ↓
使用D日及以前数据计算 buy_score[D]
  ↓
如果触发 OPEN 或 ADD
  ↓
生成待执行买单
  ↓
D+1交易日开盘成交
  ↓
成交价 = open[D+1]
```

核心约束：

- `signal_date = D`
- `execution_date = next_trading_day(D)`
- `execution_price = open[D+1]`
- 股数应按D+1实际开盘价重新计算，不能用D日收盘价反推后假装成交。
- 如果D+1停牌、开盘涨停无法买入或数据缺失，则订单不成交，并记录拒绝原因。
- 如果历史数据末尾没有D+1，信号只能记为待执行，不能计入成交。

### 2.3 卖出执行规则

用户指定的研究口径是：

```text
D日走势结束
  ↓
使用D日及以前数据计算 sell_score[D]
  ↓
如果触发 REDUCE 或 EXIT
  ↓
卖出价 = close[D]
```

即：

- `signal_date = D`
- `execution_date = D`
- `execution_price = close[D]`

### 2.4 同日收盘卖出的前视偏差说明

如果卖出信号使用了D日最终收盘价、最高价、最低价或全天成交量，那么必须等收盘后才能确认信号；此时无法再以D日收盘价真实成交。

因此，`D日完整K线出信号 + D日收盘价卖出` 是一种乐观研究假设，存在同K线前视偏差。它可以保留用于研究，但不能单独作为生产有效性证明。

系统必须同时提供两个退出模式：

| 模式 | 信号依据 | 成交时点 | 用途 |
|---|---|---|---|
| `SAME_CLOSE_RESEARCH` | D日及以前完整日K | D日收盘 | 用户指定研究口径，带前视偏差标记 |
| `NEXT_OPEN_STRICT` | D日及以前完整日K | D+1开盘 | 无同K线前视的严格验证口径 |

每次回测都必须同时输出两组结果。若两组结果差异过大，说明策略收益高度依赖无法真实获得的D日收盘成交价。

未来若接入14:50预估信号或收盘集合竞价，才可以新增更接近真实的 `CLOSE_AUCTION` 模式；该能力不属于当前阶段。

### 2.5 每日事件顺序

回测引擎必须严格按以下顺序处理每个交易日D：

```text
1. D日开盘
   - 执行D-1生成的待买订单
   - 严格退出模式下，执行D-1生成的待卖订单

2. D日持仓过程
   - 更新持仓市值、MFE、MAE
   - 不使用未来日数据

3. D日收盘
   - 使用截至D日的完整日K计算买入分和卖出分
   - SAME_CLOSE_RESEARCH模式可按D日收盘价模拟卖出
   - 生成D+1开盘待买订单
   - NEXT_OPEN_STRICT模式生成D+1开盘待卖订单

4. 保存D日评分、状态和事件
```

买入和卖出的事件顺序必须确定，不能因为代码循环顺序不同而改变结果。

## 3. 项目目标

### 3.1 第一目标：单股持仓生命周期

对一只股票完成：

- 每日买入分。
- 每日卖出分。
- 目标持仓分。
- 分批建仓。
- 分批加仓。
- 持有判断。
- 分批减仓。
- 全部退出。
- 交易成本和盈亏计算。
- 完整持仓周期回放。

### 3.2 第二目标：推广到自选池

在不修改单股评分逻辑的前提下，将引擎批量运行到所有自选股，再加入：

- 现金约束。
- 最大持仓数量。
- 单票最大仓位。
- 行业集中度。
- 组合总风险。
- 多个高分股票之间的机会排序。

### 3.3 非目标

当前阶段不做：

- 盘中实时行情轮询。
- 分钟线、Tick或盘口策略。
- 全市场每日扫描选股。
- 券商自动下单。
- LLM在没有结构化量化证据、风险护栏和审计记录的情况下直接决定买卖。
- 使用交互式编码代理作为无人值守的生产交易执行器。
- 集合竞价成交模型。
- 未经回测验证自动更新生产参数。

## 4. 设计原则

### 4.1 评分、回测和持仓维护同源

日线评分函数只能实现一次，并同时供以下场景调用：

- 单股历史回测。
- 持仓生命周期模拟。
- 每日收盘评分。
- 自选池批量维护。

禁止为每日维护另写一套简化版B1、B2或止损逻辑。

### 4.2 连续评分优先

系统核心输出三个连续分数：

- `buy_score`：新增仓位吸引力，0～100。
- `sell_score`：降低仓位必要性，0～100。
- `position_score`：目标持仓强度，0～100。

`OPEN/ADD/HOLD/REDUCE/EXIT` 是评分经过状态机和风险约束后的结果。

### 4.3 买入分和卖出分独立

买入分不能简单等于 `100 - 卖出分`。一只股票可能同时具有趋势机会和高位风险。

当两者同时较高时：

- 未持仓：不建仓，继续观察。
- 已持仓：禁止加仓，优先减仓或保持防守仓位。

### 4.4 风险优先

决策优先级：

```text
交易约束 > 硬退出 > 减仓 > 建仓/加仓 > 持有/观察
```

### 4.5 可追溯与可复现

每个评分和动作必须记录：

- 信号日。
- 实际使用的最后一根K线日期。
- 买入分、卖出分和目标持仓分。
- 每个分项贡献。
- 硬否决条件。
- 信号动作。
- 计划执行日和计划价格类型。
- 实际执行日、价格和数量。
- 执行模式。
- 策略版本和参数版本。

## 5. 日线评分模型

### 5.0 旧策略的兼容分层

仓库中原有的 B1/B2/B3/SB1/S1 并不是同一套定义。v0.1 不再用一个枚举名称掩盖多种含义，而是保存具名原始证据：

```text
b1.loose_3of4
b1.strict_oversold
b1.quality_confirmed
b2.knowledge_5bar
b2.legacy_5_14bar
b3.consensus_continuation
b3.pullback_reentry
super_b1.washout
sb1.false_break
sb1.reclaim_confirmation
s1.ugly_hat
s2.anchor_divergence
s3.failed_rebound
```

序列策略必须保留 `anchor_date`、`age_bars` 和 `anchor_variant`：

- B2 只能引用 D 日以前真正成立的 B1，不能用当前 D 日自己充当历史 B1。
- B3 只能引用此前真正成立的 B2，不能用“一根放量阳线”替代 B2。
- “超级B1洗盘”和“假摔反包”正式拆名。
- S1、出货五式、滴滴等证据保留原始子类型，不再先改名为 S1 后去重丢失。

正式进入买卖分的 variant 和阈值必须经过无前视回测选择；旧实现仅作为兼容证据来源，不自动获得生产资格。

### 5.1 买入分 `buy_score`

建议由以下维度构成：

```text
日线入场结构
+ 趋势质量
+ 量价质量
+ B1/B2/B3或其他战法共振
+ 沙漏/牛绳/麒麟阶段
+ 市场环境
- 风险惩罚
- 硬否决
```

第一版保留以下分项：

- `entry_structure_score`
- `trend_score`
- `volume_score`
- `resonance_score`
- `stage_score`
- `market_score`
- `risk_penalty`
- `hard_vetoes`

评分权重必须配置化，不能散落在多个模块中。

#### 5.1.1 连续评分不等于买点确认

`buy_score` 用于连续排序和后续权重校准；它不能单独授权开仓。v0.1 的买点确认必须同时满足：

```text
至少一个明确列入确认白名单的 entry variant 已 matched
+ buy_score 达到 OPEN 或 ADD 阈值
+ signal day 冻结止损有效且低于当日收盘
+ 无 hard veto / hard exit
+ 买入分与卖出分不构成高分冲突
= BuyPointStatus.CONFIRMED
```

否则分别输出 `NO_SETUP`、`CANDIDATE`、`CONFLICT` 或 `BLOCKED`。研究候选白名单与确认授权白名单必须分开：

```text
研究候选：所有具名B1/B2/B3、super_b1和sb1.reclaim
当前确认授权：b1.quality_confirmed、b2.knowledge_5bar、
              super_b1.washout、sb1.reclaim_confirmation
```

`b1.loose_3of4`、`b1.strict_oversold`、`b2.legacy_5_14bar`和两种B3首版只进入事件研究，即使匹配且总分较高也只能是 `CANDIDATE`，不能单独授权开仓。这样可以先验证用户担心的B3低胜率/低边际价值，再决定是否升级确认资格。S1/S2/S3、`sb1.false_break` 等风险/退出证据不得成为正向买入共振。

这里的 `CONFIRMED` 只表示“满足当前 v0.1 确定性规则”，不表示该 variant 已经通过统计验证。所有旧 variant 在walk-forward和锁定样本外通过前仍标记为研究初始化逻辑。

单日评分输出必须包含：

- 原始七项买入分、权重、加权贡献和风险扣分。
- 每个具名 variant 的 `matched/strength/details`。
- 序列策略的 `anchor_date/age_bars/anchor_variant`。
- 买点状态、主 variant、所有匹配 variant、阈值、信号日止损和估算风险比例。
- 策略版本、参数版本与参数指纹。

#### 5.1.2 买点独立回测

买入分先用固定窗口事件研究单独验收，避免尚未校准的卖出分污染结论：

```text
D日完整收盘数据打分
→ 下一交易所交易日D+1检查是否有个股原始开盘报价
→ 使用D+1原始开盘价并叠加买入滑点/费用
→ 从D+1开始观察1/3/5/10/20个个股交易bar
→ 输出固定窗口净收益、R、MFE、MAE和止损触达
```

固定窗口退出只是买点标签，不代表最终卖出策略；止损触达也只是路径标签，不等于已经模拟盘中止损成交。连续高分会另外生成非重叠的已确认样本，避免把同一轮行情重复当作独立交易。未来数据只用于 `OUTCOME_TIME` 标签，不能回写D日分数或确认状态。

每次事件研究至少输出：

- 所有评分日、入场候选数、确认数、实际可执行数和拒绝原因。
- 逐窗口样本数、胜率、平均/中位净收益、MFE、MAE、止损触达率、期望R和Profit Factor。
- 买入分分箱结果，以及每个B1/B2/B3具名variant的匹配→确认资格→策略选择→执行→完整标签漏斗。
- 逐variant同时给出共现统计、primary-exclusive统计和primary非重叠统计；这些仍不是正式消融或边际因果贡献。
- `INCONCLUSIVE/OVERLAPPING_DIAGNOSTIC/OBSERVED_NON_POSITIVE/RESEARCH_CANDIDATE` 证据状态。逐日重叠样本、分箱和多variant共现统计只能是诊断；只有非重叠样本才有资格进入正/负期望判断。样本达到数量阈值但期望R不为正时不能标成候选，基础版也不自动宣称策略已验证。
- 明确记录ST/可交易性、单价/双价、固定期限退出等研究假设。
- 完整记录评分参数、执行参数、事件研究参数、确认策略、feature版本、K线内容、日历和市场上下文指纹。

### 5.2 卖出分 `sell_score`

建议由以下维度构成：

```text
硬止损或止损距离
+ S1/S2/S3
+ 趋势破坏
+ 白线/黄线/BBI破位
+ 出货量价形态
+ 市场环境恶化
+ 当前仓位过热
+ 盈利保护需求
```

第一版保留以下分项：

- `stop_score`
- `exit_signal_score`
- `trend_break_score`
- `distribution_score`
- `market_risk_score`
- `position_heat_score`
- `profit_protection_score`
- `hard_exit_reasons`

### 5.3 目标持仓分 `position_score`

目标持仓分不应只用 `buy_score - sell_score`，而应遵守风险覆盖规则：

1. 硬退出触发：目标持仓分直接归零。
2. 卖出分超过清仓线：目标持仓分归零或最低保护值。
3. 卖出分超过减仓线：目标持仓分最多保留上一仓位阶梯。
4. 无高级风险时，再根据买入分决定目标仓位阶梯。

建议第一版仓位阶梯：

| 目标持仓分 | 占单票最大仓位的比例 |
|---:|---:|
| 0～19 | 0% |
| 20～39 | 25% |
| 40～59 | 50% |
| 60～79 | 75% |
| 80～100 | 100% |

这些阈值是待验证参数，不是最终结论。

## 6. 单股持仓生命周期

### 6.1 状态定义

| 状态 | 含义 |
|---|---|
| `FLAT` | 空仓 |
| `READY` | 日线结构允许建仓，但分数未到执行线 |
| `PENDING_BUY` | 已在D日生成买入信号，等待D+1开盘 |
| `BUILDING` | 已开始分批建仓，尚未达到目标仓位 |
| `HOLDING` | 当前仓位与目标仓位一致 |
| `PENDING_SELL` | 严格退出模式下等待D+1开盘卖出 |
| `REDUCING` | 已部分减仓 |
| `LOCKED` | 应卖出但受T+1、跌停或停牌限制 |
| `EXITED` | 本轮持仓周期结束 |

### 6.2 空仓和准备阶段

- 日线结构不允许或存在硬否决：保持 `FLAT`。
- 日线结构允许但买入分不足：进入 `READY`。
- 买入分达到建仓线且卖出分低于冲突线：生成D+1开盘买单，进入 `PENDING_BUY`。

### 6.3 建仓和加仓阶段

- D+1开盘成交后进入 `BUILDING`。
- 后续D日收盘买入分上升至更高仓位阶梯，可以继续生成D+1开盘加仓单。
- 买入分回落但卖出分不高时，不追价，维持已有仓位。
- 亏损本身不能成为加仓理由，必须出现新的独立有效信号。

### 6.4 持有阶段

- 当前仓位与目标阶梯一致：`HOLD`。
- 买入分增强且风险允许：`ADD`，D+1开盘执行。
- 卖出分达到减仓线：`REDUCE`。
- 卖出分达到退出线或硬退出触发：`EXIT`。

### 6.5 减仓和退出阶段

- `SAME_CLOSE_RESEARCH`：D日收盘价模拟减仓或退出。
- `NEXT_OPEN_STRICT`：D日生成卖单，D+1开盘执行。
- 受T+1、跌停或停牌限制时进入 `LOCKED`，记录“希望卖出但无法执行”。
- 全部卖出后进入 `EXITED`，完成本轮持仓周期。

## 7. 交易执行模型

### 7.1 买入成交

```text
买入信号：D日收盘确认
成交日期：D+1
成交价格：open[D+1]
```

买入数量：

```text
risk_budget = equity × risk_per_trade
risk_per_share = open[D+1] - stop_loss[D]
raw_shares = risk_budget / risk_per_share
```

之后再应用：

- 100股整数手。
- 单票最大仓位。
- 可用现金。
- 现金利用率。
- 涨停、停牌和ST限制。
- 手续费。

### 7.2 卖出成交

研究模式：

```text
卖出信号：D日收盘确认
成交日期：D
成交价格：close[D]
lookahead_flag = true
```

严格模式：

```text
卖出信号：D日收盘确认
成交日期：D+1
成交价格：open[D+1]
lookahead_flag = false
```

### 7.3 价格口径

- 开盘价和收盘价必须来自同一复权口径的数据集。
- 回测建议统一使用前复权行情，且不得混用未复权成本。
- 交易费用按实际成交金额计算。
- 估值和收益曲线在D日收盘使用 `close[D]` 标记市值。
- D+1跳空必须反映在真实买入或严格卖出成交价中。

### 7.4 A股约束

- 买入100股整数手。
- T+1：当日买入不可当日卖出。
- 停牌不成交。
- 涨停买入受限。
- 跌停卖出受限。
- 默认不允许ST股票，除非配置显式开启。

## 8. 数据模型

### 8.1 `daily_stock_scores`

每只股票每个交易日保存一份确认评分：

```text
id
trade_date
ts_code
buy_score
sell_score
position_score
target_position_pct
entry_structure_score
trend_score
volume_score
resonance_score
stage_score
market_score
risk_penalty
stop_score
exit_signal_score
trend_break_score
distribution_score
hard_vetoes_json
hard_exit_reasons_json
score_contributions_json
strategy_version
parameter_version
created_at
```

唯一索引：

```text
(trade_date, ts_code, strategy_version, parameter_version)
```

### 8.2 `pending_orders`

保存D日生成、等待D+1开盘执行的订单：

```text
id
signal_date
planned_execution_date
ts_code
side
action
target_position_pct
planned_quantity
price_type
status
reject_reason
score_id
strategy_version
created_at
executed_at
trade_record_id
```

`price_type` 第一版仅允许：

- `NEXT_OPEN`
- `SAME_CLOSE_RESEARCH`

### 8.3 `holding_cycles`

记录一轮完整持仓过程：

```text
id
ts_code
start_date
end_date
entry_mode
exit_mode
strategy_version
parameter_version
max_position_pct
average_cost
realized_pnl
total_return
max_drawdown
max_favorable_excursion
max_adverse_excursion
holding_days
status
created_at
updated_at
```

### 8.4 `position_events`

记录状态和目标仓位变化：

```text
id
holding_cycle_id
event_date
signal_date
execution_date
from_state
to_state
desired_action
executable_action
quantity
price
buy_score
sell_score
position_score
reason_json
score_id
pending_order_id
executed
trade_record_id
lookahead_flag
created_at
```

### 8.5 `strategy_versions`

所有评分权重、阈值和执行规则必须版本化：

```text
version
strategy_name
parameters_json
execution_model_json
backtest_start
backtest_end
in_sample_metrics_json
out_of_sample_metrics_json
is_production
activated_at
retired_at
notes
```

历史评分和交易不得因新版本启用而被改写。

## 9. 目标代码结构

当前代码结构：

```text
modules/daily_portfolio/
├── dates.py                 # 交易日期规范化和比较
├── contracts.py             # as-of 数据边界
├── models.py                # 分数、订单、持仓状态模型
├── bar_features.py          # OHLCV 派生字段统一富化
├── strategy_features.py     # 具名 variant 与序列锚点证据
├── evidence_adapter.py      # 原始证据映射到标准分项
├── score_engine.py          # 买入分和卖出分的唯一聚合器
├── buy_points.py            # 连续买入分到买点确认门槛
├── buy_backtest.py          # D+1开盘固定窗口买点事件研究
├── position_policy.py       # 分数到仓位阶梯和动作
├── execution.py             # 分数到待执行订单
├── execution_model.py       # D+1开盘及双退出成交模型
├── service.py               # 单股每日评分统一入口
├── replay.py                # 单股历史日事件回放
├── price_data.py            # 双价格和公司行动数据契约（待接主链）
├── inventory.py             # 分批库存模型（待收紧并接主链）
├── repository.py            # 数据库存取（待开发）
└── holding_state_machine.py # 持仓状态转换（持久化待开发）
```

核心评分接口：

```python
def score_daily_bar(
    ts_code: str,
    as_of_date: str,
    bars: list[DailyData],
    position: PositionState,
    market_context: MarketContext,
    config: DailyScoreConfig,
) -> DailyStockScore:
    ...
```

关键契约：

```text
max(bar.trade_date for bar in bars) <= as_of_date
```

仓位字段必须区分：

```text
target_ladder_ratio  = 占单票最大仓位的比例，例如 0.75
max_position_pct     = 单票在组合中的真实上限，例如 0.10
target_position_pct  = 两者乘积，例如 0.075
```

`max_position_pct` 目前必须显式传入，不采用旧代码中互相冲突的 10% / 20% / 30% 默认值。

执行接口：

```python
def process_trading_day(
    bar: DailyData,
    pending_orders: list[PendingOrder],
    position: PositionState,
    execution_config: ExecutionConfig,
) -> DayResult:
    ...
```

## 10. 每日评分结果示例

```json
{
  "ts_code": "600519.SH",
  "signal_date": "20260710",
  "buy_score": 78,
  "sell_score": 24,
  "position_score": 65,
  "current_position_pct": 0.00,
  "target_position_pct": 0.075,
  "desired_action": "OPEN",
  "execution_plan": {
    "execution_date": "20260713",
    "price_type": "NEXT_OPEN",
    "price": null
  },
  "stop_loss": 1420.0,
  "reasons": ["B1结构成立", "缩量回调", "牛绳未断"],
  "vetoes": [],
  "strategy_version": "daily-holding-v0.2"
}
```

卖出研究模式示例：

```json
{
  "ts_code": "600519.SH",
  "signal_date": "20260718",
  "buy_score": 18,
  "sell_score": 88,
  "desired_action": "EXIT",
  "execution_plan": {
    "execution_date": "20260718",
    "price_type": "SAME_CLOSE_RESEARCH",
    "price": 1512.5,
    "lookahead_flag": true
  }
}
```

## 11. 单股回测输出

每次回测至少输出：

- 总收益率。
- 年化收益率。
- 最大回撤。
- Sharpe Ratio。
- 胜率。
- 盈亏比。
- Profit Factor。
- 交易次数。
- 平均持仓天数。
- 最大连续亏损。
- 资金利用率。
- 各买入分区间的未来收益。
- 各卖出分区间的未来回撤。
- 每轮持仓周期的MFE和MAE。
- 分批建仓对成本的影响。
- 分批减仓对收益和回撤的影响。

必须并列展示：

```text
SAME_CLOSE_RESEARCH 结果
NEXT_OPEN_STRICT 结果
两者差异
```

重点观察：

- 收益差异。
- 最大回撤差异。
- 卖出滑点/跳空影响。
- 同日收盘退出带来的乐观偏差。

### 11.1 策略有效不等于每笔盈利

本系统不以“每笔都赚钱”或“胜率越高越好”作为策略目标。止损是预先定义的风险成本；在买点结构合理、执行符合计划、止损没有被擅自下移的前提下，一笔正常止损可以是完全正确的交易流程。

策略有效性的核心是长期净期望：

```text
expectancy_R
  = win_rate × average_win_R
  - loss_rate × average_loss_R

profit_factor
  = gross_profit / gross_loss
```

例如五笔交易中三笔各亏 `-1R`、两笔各赚 `+3R`：

```text
总结果 = 2 × 3R - 3 × 1R = +3R
单笔期望 = +0.6R
胜率 = 40%
```

这组盈亏结构是正的，不能因为三次止损就把策略判为无效。反过来，如果两笔盈利只有 `+1R`、三笔止损各 `-1R`，即使过程相似，总期望仍为负。

五笔交易只能说明原理，样本量不足以冻结策略。正式验收必须同时使用：

- 扣除费用和滑点后的 `NEXT_OPEN_STRICT` 样本外期望值。
- Profit Factor、平均盈利R、平均亏损R和盈亏比。
- 最大回撤、最大连续亏损、尾部损失和资金利用率。
- 完整持仓周期数及置信区间；样本不足时结论必须为 `INCONCLUSIVE`。
- 多个walk-forward窗口及至少一只额外股票的稳定性。
- 与现金基线、同风险买入持有和初始化参数的对照。

禁止只看胜率，也禁止因为严格模式表现不好而改用带前视的同收盘结果证明有效。

### 11.2 完整持仓周期复盘

每轮从首次建仓到最终清仓形成一个不可拆散的 `holding_cycle`。复盘必须重放整个过程，而不是只点评一张买入截图或最后一笔卖单：

```text
首次建仓
→ 后续加仓/未加仓
→ 每日 HOLD/REDUCE/EXIT 决策
→ 止损与失效条件变化
→ 分批减仓
→ 最终清仓
→ 清仓后固定观察窗口
```

每个周期至少保存：

- 所有评分、分项贡献、具名战法证据和硬约束。
- 每次目标仓位、实际订单、成交、拒绝、顺延和费用。
- 入场计划、原始止损、止损是否被违规放宽。
- 每日持仓、现金、成本、未实现盈亏、MFE和MAE。
- OPEN、ADD、HOLD、REDUCE、EXIT每次决策的当时可见证据。
- 清仓后5/10/20个交易日的价格路径和风险调整反事实。

确定性代码先将结果分类，LLM只能基于分类证据做原因分析。首版分类至少包括：

| 分类 | 含义 |
|---|---|
| `PROCESS_OK_OUTCOME_GOOD` | 决策过程符合规则且结果盈利 |
| `PROCESS_OK_OUTCOME_BAD` | 决策过程正确但出现计划内亏损/止损 |
| `ENTRY_SELECTION_ERROR` | 买入时证据质量不足、否决项被忽略或盈亏比不成立 |
| `ENTRY_EXECUTION_ERROR` | 次日跳空、追价、股数或费用使实际交易偏离计划 |
| `ADD_DECISION_ERROR` | 没有新证据却加仓、亏损摊平或在风险冲突下加仓 |
| `HOLDING_RISK_ERROR` | 持有中应减仓/退出却延误，或擅自下移止损 |
| `JUSTIFIED_RISK_REDUCTION` | 风险证据成立，减仓/卖出合理，即使之后继续上涨 |
| `PREMATURE_EXIT` | 风险证据不足且卖出后可执行上行显著、额外下行风险有限 |
| `MISSED_REDUCTION` | 已有明确风险证据但未保留防守动作 |

止损本身不等于买点错误。只有当入场时已存在可见的低质量证据、盈亏比不足、硬否决被忽略，或同类入场在样本外长期负期望时，才归因到买点选择。

### 11.3 “卖飞”必须做风险调整后的反事实判断

卖出后上涨不自动等于卖飞。判断需要同时比较：

- 卖出后5/10/20日的最大可执行上行 `post_exit_MFE`。
- 同期最大不利波动 `post_exit_MAE`。
- 原卖出日的顶背离、趋势破坏、出货、止损和仓位热度证据。
- 若继续持有，是否会突破当时允许的回撤、止损或组合风险。
- 全部卖出、只减仓、保留观察仓三种反事实路径的风险调整收益。

例如顶背离成立后减仓，随后股价继续上涨，但继续持有的风险收益并不优于“锁定大部分利润并保留小仓位”，应分类为 `JUSTIFIED_RISK_REDUCTION`，而不是简单记成卖飞。只有风险证据弱、后续上涨显著、额外下行风险有限且部分保留仓明显更优时，才可标记为 `PREMATURE_EXIT`。

反事实只用于复盘和下一版参数研究，不能回写当时历史决策，也不能让LLM假装当时已经知道未来路径。

### 11.4 权重、噪音与战法消融

买入分和卖出分的调整不以主观印象直接改权重，而要逐项分析：

- 每个分项对OPEN/ADD/REDUCE/EXIT的实际贡献分布。
- 移除该分项后的严格模式增量收益、回撤、换手和稳定性。
- 单独启用该分项时是否只提高样本内表现。
- 外部市场/新闻类证据是否在point-in-time条件下仍有增量价值。
- 权重上下移动5分后结果是否稳定，拒绝孤立最优点。
- 不同市场阶段和不同股票上的方向是否一致。

B1/B2/B3等具名变体必须分别统计：触发次数、可执行率、胜率、平均盈利R、平均亏损R、期望R、Profit Factor、MFE、MAE和对组合结果的边际贡献。B3胜率低不必然代表无效：若低胜率但平均盈利显著大于平均亏损，仍可能保留；若增量期望为负、样本外不稳定或只增加换手，应降权、改为辅助证据或移出生产评分。

参数搜索采用分阶段消融，不能一次把所有权重、阈值和战法条件做全笛卡尔积。Test和最终holdout结果不得反向生成同一轮的新候选。

## 12. 推广至自选池

单股引擎稳定后，再扩展现有 `watchlist`：

| 字段 | 说明 |
|---|---|
| `role` | `CANDIDATE/HOLDING/PAUSED/BLOCKED/ARCHIVED` |
| `strategy_profile` | 使用的策略版本 |
| `target_pct` | 当前目标仓位 |
| `max_pct` | 单票最大仓位 |
| `sector` | 行业分类 |
| `manual_stop` | 可选人工止损 |
| `priority` | 组合机会优先级 |

每个收盘日批量执行：

1. 同步自选池全部日线。
2. 重建真实持仓。
3. 计算市场环境。
4. 对每只股票调用相同的 `score_daily_bar()`。
5. 保存原始买入分、卖出分和目标持仓分。
6. 应用组合现金、行业和总风险约束。
7. 生成D+1开盘待买/待卖订单。
8. 输出自选池维护报告。

组合层只能修改最终允许仓位，不能篡改单股原始分数。

## 13. 组合风险规则

第一版开发默认值：

```yaml
portfolio:
  max_positions: 6
  max_single_position_pct: 0.15
  max_sector_position_pct: 0.30
  min_cash_pct: 0.20
  max_total_open_risk_pct: 0.05

risk:
  risk_per_trade: 0.01
  allow_st: false
  t1_lock: true
```

这些值必须通过回测调整。

当多只股票同时满足买入条件时，建议排序依据：

```text
风险调整后机会分
= buy_score
- sell_score惩罚
- 行业集中度惩罚
- 与现有持仓相关性惩罚
+ 现金利用效率
```

## 14. 防止日线信号抖动

- 买入触发线和取消线使用不同阈值。
- 普通加仓和减仓设置跨日冷却期。
- 最小仓位变化小于2个百分点时不调仓。
- 硬止损和高级退出不受冷却期限制。
- 同一股票同一信号日同一策略版本只能产生一份评分。
- 同一待执行订单不能在D+1重复成交。
- 当买入分和卖出分同时较高时禁止新增仓位。

## 15. LLM与多Agent决策层

### 15.1 定位

量化引擎负责计算事实和可复现测量，LLM/Agent负责：

- 阅读结构化量化证据。
- 从多个策略视角解释冲突。
- 结合有时间戳的基本面、新闻和用户交易约束做研究。
- 在允许的动作范围内给出最终建议。
- 生成决策理由、反方意见和失效条件。

LLM不能负责：

- 自己编造行情、财务或新闻。
- 改写D日信号和D+1成交的时间规则。
- 绕过T+1、涨跌停、停牌、现金和风险上限。
- 删除硬止损或高级退出信号。
- 无版本地修改评分权重。

### 15.2 三种运行模式

系统必须保留独立基线：

| 模式 | 说明 | 用途 |
|---|---|---|
| `QUANT_ONLY` | 只使用确定性评分和状态机 | 基准回测、生产降级 |
| `QUANT_LLM_OVERLAY` | LLM读取量化证据后调整一档仓位或否决新增仓位 | 首个LLM实验模式 |
| `MULTI_AGENT` | 多个研究Agent独立判断，由裁决Agent综合 | 后续高级模式 |

任何LLM或多Agent策略都必须和 `QUANT_ONLY` 同期对照，单独计算增量收益、增量回撤、成本和稳定性。

### 15.3 推荐的多Agent角色

首版不建议堆很多Agent，建议从四个角色开始：

#### 量化证据构建器（确定性代码）

- 调用同一套指标、战法、评分和仓位函数。
- 只返回结构化事实，不做自由发挥。
- 检查数据日期是否超过信号日。
- 该角色首版不调用LLM，避免让模型重新计算或改写量化事实。

#### 建仓/加仓Agent

- 重点审查B1/B2/B3、趋势、量价、沙漏、牛绳和阶段。
- 输出支持建仓/加仓的证据和失败条件。
- 必须主动寻找“为什么不该买”。

#### 减仓/风险Agent

- 重点审查止损、S1/S2/S3、趋势破坏、出货量价和市场风险。
- 对高买入分提出反方论证。
- 硬退出触发时拥有否决新增仓位的权力。

#### 决策裁决Agent

- 接收前三者的结构化输出。
- 输出唯一最终建议。
- 不直接读取未经工具验证的数值。
- 必须解释与纯量化结果是否一致；若不一致，说明具体覆盖原因。

多Agent编排可以使用支持工具、handoff、guardrail和trace的运行时；编码代理如 Codex 或 Claude Code 更适合开发、策略研究、回测审查和批量实验，不建议直接充当无人值守的生产交易进程。

### 15.4 Agent输入契约

Agent首版只能接收截至信号日D冻结的量化证据。新闻和基本面证据要等发布时间、系统获得时间和历史归档均可靠后再单独接入：

```json
{
  "schema_version": "quant-evidence-v1",
  "as_of_date": "20260710",
  "ts_code": "600519.SH",
  "strategy_version": "daily-holding-v0.2",
  "parameter_version": "buy-confirmation-initial-v0.2",
  "quant_decision": {
    "buy_score": 78.5,
    "sell_score": 21.0,
    "quant_action": "OPEN",
    "current_step": 0,
    "quant_target_step": 2,
    "hard_vetoes": [],
    "hard_exit_reasons": []
  },
  "position": {},
  "market_context": {
    "trade_date": "20260710",
    "score": 62,
    "version": "market-context-v0.1",
    "source_hash": "..."
  },
  "evidence_items": [
    {
      "ref_id": "strategy:b1.quality_confirmed",
      "kind": "STRATEGY",
      "observed_date": "20260710",
      "name": "b1.quality_confirmed",
      "value": true
    }
  ],
  "constraints": {
    "allowed_actions": ["OPEN", "WATCH", "BLOCK"],
    "max_position_step_adjustment": 1,
    "may_override_hard_exit": false,
    "may_override_hard_veto": false
  }
}
```

所有日期必须小于等于 `as_of_date`，且首版要求 `last_bar_date == as_of_date`。输入使用规范JSON计算 SHA-256；不得加入会使同一历史输入每次变化的生成时间字段。

未来接入基本面和新闻时，必须包含来源、事件发生时间、系统获得时间。历史回测时，不允许把D日以后发布或D日以后才被系统获得的信息提供给Agent。

### 15.5 Agent输出契约

Agent必须输出JSON Schema约束的结构：

```json
{
  "schema_version": "overlay-decision-v1",
  "position_step_adjustment": 0,
  "buy_disposition": "UNCHANGED",
  "confidence": 74,
  "quant_agreement": "AGREE",
  "reasons": [
    {
      "summary": "质量确认型B1成立",
      "evidence_refs": ["strategy:b1.quality_confirmed"]
    }
  ],
  "counter_arguments": ["若次日跳空过高则盈亏比下降"],
  "evidence_refs": ["strategy:b1.quality_confirmed"],
  "invalidation_conditions": ["白线下穿黄线"],
  "risk_flags": [],
  "requires_human_review": true
}
```

模型不得直接给最终动作或最终仓位，也不得修改量化分数。最终仓位和动作由确定性 guardrail 根据 `position_step_adjustment`、`buy_disposition`、原量化阶梯和硬约束推导。

首版建议将LLM权限限制为：

- 最多把目标仓位上调或下调一个阶梯。
- 可以把 `OPEN/ADD` 降级为 `WATCH/BLOCK`。
- 不得覆盖硬退出。
- 不得把被量化硬否决的股票升级为买入。
- 输出解析失败、超时或证据不足时自动回退 `QUANT_ONLY`。
- 回退必须精确保留原量化动作和目标仓位，不能擅自改成 `HOLD`。
- 所有 `evidence_refs` 必须能在输入中找到；禁止正则修补非法JSON。

### 15.6 LLM回测与可复现性

LLM具有非确定性，回测必须保存：

- 模型提供商和模型ID。
- Prompt版本。
- 工具输入输出哈希。
- 完整结构化输入。
- 原始输出和解析后输出。
- 温度、随机种子（若支持）和运行参数。
- Agent handoff、工具调用和护栏事件。
- 调用耗时、Token和成本。

相同历史日期的Agent决策优先从缓存读取，避免每次回测重新生成不同答案。

### 15.7 新增数据表

建议新增 `agent_runs`：

```text
id
run_date
as_of_date
ts_code
mode
agent_name
model_provider
model_id
prompt_version
input_hash
tool_evidence_hash
raw_output
structured_output_json
trace_id
latency_ms
token_usage_json
cost
success
error
created_at
```

建议新增 `agent_decisions`：

```text
id
score_id
agent_run_id
quant_action
final_action
quant_target_step
position_step_adjustment
buy_disposition
final_target_step
agreement
override_reason_json
requires_human_review
decision_version
created_at
```

### 15.8 现有LLM模块处理方式

| 模块 | 当前状态 | 后续处理 |
|---|---|---|
| `modules/llm_providers.py` | 通用文本生成，主要适配MiniMax；异常会被转成普通文本 | 仅保留文本用途；交易层另建会抛类型化异常的结构化Provider |
| `modules/commentary_service.py` | 生成自然语言点评 | 保留为解释层，不作为交易裁决 |
| `modules/intent_chat.py` | 对话路由 | 不进入核心交易路径 |
| `modules/self_optimizer/llm_judge.py` | 固定返回值占位 | 在真实评测、缓存和审计完成后再接入 |
| `modules/self_optimizer/scorer.py` | LLM评分仍是中性占位 | 暂不作为生产优化器 |

## 16. 初始化量化策略清单

### 16.1 首版核心：修正后直接进入评分框架

这些逻辑具备明确数值定义，适合作为初始化策略：

#### 基础指标

- MA5/10/20/60及均线排列。
- BBI `(MA3 + MA6 + MA12 + MA24) / 4`。
- KDJ和J值超买超卖。
- MACD DIF/DEA/柱、金叉死叉、零轴和背离。
- RSI、WR、布林带。
- 量比和量价配合。
- DMI/ADX。
- 52周位置和近期回撤。

#### 趋势结构

- 白线/黄线及金叉死叉。
- 牛绳状态。
- 多空市场环境分类。
- 价格相对BBI、白线、黄线的位置。

#### 买入信号

- B1：超卖和回调买点；必须先统一当前多个版本。
- B2：B1后的放量确认。
- B3：确认后的分歧转一致。
- 超级B1：放量震仓后的缩量企稳。

#### 卖出信号

- S1/S2/S3。
- 固定/结构止损。
- 白线死叉黄线。
- 跌破MA20或BBI。
- 四块砖翻绿和红砖减仓。
- 买盘枯竭。
- 绿肥红瘦。
- 阶梯放量下跌。
- 顶部大风车。

#### 图形和风险过滤

- 沙漏评分。
- 蜈蚣图硬过滤。
- 三波理论。
- 麒麟吸筹/拉升/派发/回落阶段。
- 主力出货五式。

#### 执行和风控

- D日收盘生成评分。
- D+1开盘买入。
- `SAME_CLOSE_RESEARCH`和 `NEXT_OPEN_STRICT` 双退出结果。
- 单笔风险仓位计算。
- 单票最大仓位、现金利用率和最大持仓数。
- T+1、涨跌停、停牌、ST和交易成本。

### 16.2 二级增强：先作为Agent证据，不直接控制仓位

以下形态可以输入Agent和报告，但首版不建议直接赋予高权重：

- 长安战法。
- 娜娜图形。
- 异动+地量地价。
- 平行重炮。
- 坑里起好货。
- 对称VA。
- 灾后重建。
- 跃跃欲试。
- 关键K和暴力K。
- 单针下20/30。
- 呼吸结构、黄金碗、双枪等复杂形态。

原因：它们多为启发式形态，条件重叠明显，需要逐项做独立增益、覆盖率和样本外稳定性测试。

### 16.3 暂不进入生产策略

- `portfolio_diagnosis._make_recommendation()`：与模拟器存在重复决策逻辑，只保留展示用途。
- `self_optimizer/llm_judge.py`：当前仍是stub。
- 未经数据工具验证的知识文档结论。
- 纯自然语言LLM买卖建议。
- 只在 `SAME_CLOSE_RESEARCH` 下有效、严格模式失效的信号。
- 依赖当前为空的资金流、财务或指标缓存字段的规则。

### 16.4 第一版买入分初始化权重

建议将总分限制在100分：

| 维度 | 初始权重 |
|---|---:|
| B1/B2/B3及核心入场结构 | 25 |
| 趋势与价格位置 | 20 |
| 量价确认 | 15 |
| 沙漏/牛绳/蜈蚣等图形质量 | 15 |
| 三波/麒麟阶段 | 10 |
| 市场环境 | 10 |
| 其他共振 | 5 |

风险项和硬否决不包含在正向100分中，而是单独扣分或直接禁止。

### 16.5 第一版卖出分初始化权重

| 维度 | 初始权重 |
|---|---:|
| 硬止损及止损距离 | 30 |
| S1/S2/S3 | 25 |
| 趋势破坏和双线死叉 | 20 |
| 出货量价形态 | 10 |
| 市场环境风险 | 5 |
| 仓位过热与盈利保护 | 10 |

硬止损、S2/S3等可以直接触发退出，不必等累计分达到普通阈值。

### 16.6 LLM初始化权限

第一版建议不要把LLM分数直接混入确定性100分。采用独立Overlay：

```text
quant_target_step = 量化目标仓位阶梯
agent_adjustment = -1 / 0 / +1
final_target_step = guardrail(quant_target_step + agent_adjustment)
```

其中：

- `+1` 仅在无硬否决且Agent证据充分时允许。
- `-1` 可用于降低风险。
- 硬退出始终覆盖Agent。
- 所有Agent覆盖必须单独统计增量价值。

## 17. 开发阶段

### 阶段0：修正现有日线量化

- 统一B1/B2/B3定义。
- 修正白线、MACD日期映射和量比字段问题。
- 排查现有模拟器前视偏差。
- 固定复权、费用和交易约束。
- 选择第一只开发股票。

验收：同一股票、日期和参数下，指标、策略、诊断和回测原始信号一致。

### 阶段1：单股日线评分

- 建立统一 `DailyStockScore`。
- 实现买入分、卖出分和分项贡献。
- 实现硬否决和硬退出。
- 保存 `daily_stock_scores`。

验收：每个交易日生成唯一、可解释、可复现的评分。

### 阶段2：执行时点与持仓状态机

- 实现D日收盘信号。
- 实现D+1开盘买入。
- 实现 `SAME_CLOSE_RESEARCH` 卖出。
- 实现 `NEXT_OPEN_STRICT` 对照卖出。
- 实现待执行订单和持仓生命周期。
- 正确计算T+1、费用、数量、成本和盈亏。

验收：同一组评分可以完整重放从空仓到清仓的多轮持仓周期。

### 阶段3：单股评分校准

- 先运行独立买点事件研究，隔离卖出分和仓位策略。
- 对所有匹配的入场候选计算D+1开盘后的1/3/5/10/20日结果。
- 输出买入分分箱、逐variant表现、MFE/MAE、止损触达、期望R和Profit Factor。
- 生成阈值确认样本的非重叠结果，连续高分不重复冒充独立交易。
- 回测不同评分权重和阈值。
- 分析买入分与未来收益的单调关系。
- 分析卖出分与未来回撤的单调关系。
- 比较两种卖出模式。
- 进行walk-forward样本外验证。
- 冻结第一套生产评分版本。

当前状态：买点事件研究的确定性计算、CLI和测试基础版已完成；双价格接入、置信区间、消融和真实walk-forward尚未完成。

验收：评分越高应在统计上对应更明确的未来收益或风险，具名variant具有可解释的边际价值，且锁定样本外表现稳定。

### 阶段4：LLM Overlay与多Agent实验

- 抽象结构化LLM Provider。
- 实现量化证据、建仓、风险和裁决Agent。
- 增加JSON Schema、工具白名单和交易护栏。
- 保存 `agent_runs`、trace和完整证据。
- 对同一历史区间并列运行 `QUANT_ONLY` 与 `QUANT_LLM_OVERLAY`。
- 只有Overlay样本外增益稳定后才启用多Agent模式。

验收：Agent输出可解析、可缓存、可审计；任何覆盖量化结果的动作都有证据，且不能突破硬风险约束。

### 阶段5：推广至自选池

- 扩展自选池和持仓数据模型。
- 批量运行单股评分引擎。
- 加入组合级风险约束。
- 生成下一交易日订单计划。

验收：池中股票单独运行与批量运行的原始评分完全一致。

### 阶段6：前端和复盘闭环

- 展示自选池每日买入分、卖出分和目标仓位。
- 展示单股完整持仓周期。
- 支持执行确认和真实成交录入。
- 统计建议动作后的表现。
- 分别统计纯量化、LLM Overlay和多Agent的增量表现。
- 建立策略版本切换流程。

验收：历史任何一天的评分、订单、成交和持仓变化均可追溯。

## 18. 测试要求

必须覆盖：

- 评分函数只能看到信号日及以前K线。
- D日买入信号只能在D+1开盘成交。
- 买入成交价严格等于D+1开盘价再叠加配置滑点。
- 数据末尾没有D+1时不能虚构成交。
- D+1涨停、停牌或资金不足时正确拒绝买入。
- 研究退出模式成交价为D日收盘并标记前视。
- 严格退出模式成交价为D+1开盘且不标记前视。
- 两个退出模式独立计算绩效。
- T+1阻止当日买入后同日卖出。
- 100股整数手、费用和现金扣减正确。
- 部分卖出后的剩余数量和成本正确。
- 买入分和卖出分的分项贡献可核对。
- 没有匹配入场variant时，高上下文买入分也不能生成OPEN/ADD。
- S1/S2/S3和未经确认的假摔证据不能增加买入共振。
- 单日评分输出逐variant匹配状态、强度、细节和序列锚点。
- 买点事件研究从D+1开盘开始计算，绝不能把D日高低价计入MFE/MAE。
- 1/3/5/10/20日窗口不足时标记截尾，不能填充0收益。
- 分数分箱和逐B1/B2/B3 variant报告能从同一事件明细复算。
- 三笔 `-1R`、两笔 `+3R` 得到40%胜率、`+0.6R`期望和2.0 Profit Factor。
- 连续确认信号的非重叠样本不会被当作多笔独立交易。
- 硬退出覆盖高买入分。
- 买卖分同时过高时禁止加仓。
- 状态机不允许非法跳转。
- 待执行订单不能重复成交。
- 相同输入、参数和版本得到相同结果。
- 策略版本更新不改写历史评分和交易。
- Agent只能读取信号日及以前的结构化证据。
- Agent输出不符合Schema时自动回退纯量化模式。
- Agent不得覆盖硬止损、T+1、涨跌停、停牌和仓位上限。
- 同一输入哈希优先命中缓存，不重复产生不一致的历史决策。
- Agent工具调用、handoff和最终裁决均有审计记录。
- `QUANT_ONLY`、`QUANT_LLM_OVERLAY` 和 `MULTI_AGENT` 绩效独立统计。

## 19. 已知风险

1. 旧 `simulator/backtest` 与新引擎的成交时点仍不一致；它们只能作为历史实验入口，不能用来证明 `daily_portfolio` 有效。
2. 用户指定的D日收盘卖出模式本身带有同K线前视偏差，可能高估止损效率并低估跳空损失；必须始终与严格次日开盘结果成对展示。
3. 当前涨跌停模型是按代码推断的研究模型，尚未覆盖创业板历史切换、北交所、新股无涨跌停期等逐日规则。
4. 当前费用模型使用固定费率和当前研究口径，尚未按历史日期版本化；长期回测不能宣称是精确成交账本。
5. 本地数据目前没有可靠的交易所交易日历和版本化市场环境快照，正式命令必须要求显式文件或可审计数据源，禁止静默回退中性 50 分。
6. 评分、订单、成交和 Agent 审计表尚未完成数据库持久化；目前的内存回放不能替代崩溃恢复和每日任务幂等性。
7. 第一版权重与阈值只是初始化参数，尚未完成单股校准、walk-forward 和样本外验证，存在明显过拟合风险。
8. 单只股票验证通过不代表推广到所有行业、市场状态和股票生命周期后仍然有效。
9. 自选池组合层尚未实现；现金、行业集中度、组合回撤、机会排序和多股票同时触发仍无统一裁决。
10. 每日实际仓位比例必须由股数、确认价格和组合权益重算；不能长期信任人工文件里的陈旧 `current_position_pct`。
11. 旧 LLM Provider 主要输出自然语言，不能接入交易裁决；新的 Overlay 当前只有结构化契约和 guardrail，尚未连接外部模型、缓存与审计存储。
12. `requires_human_review` 已进入最终决策契约；后续执行编排必须在未审批时阻止 LLM 增量动作，不能只把它当展示字段。
13. 当前 LLM Judge 和自优化 LLM 评分仍是 stub，不能作为真实优化证据。
14. LLM 模型、Prompt 或工具升级可能导致历史策略漂移，必须版本化并缓存；历史新闻或基本面数据还必须具备准确发布时间和系统获取时间。
15. 多 Agent 数量增加会提高成本、延迟和不一致性，不代表决策质量必然提高；在单 Agent Overlay 没有稳定样本外增益前不进入多 Agent 生产实验。
16. 买点事件研究尚未接入 `DualPriceSeries` 和公司行动权益结算；CLI因此暂时只允许 SQLite RAW，且明确标记“单价格研究”。除权分红区间或多年结果不能解释为正式历史成交收益。
17. 固定窗口退出、止损触达和逐日重叠事件只用于评价买点，不是最终卖出策略。只有非重叠样本可以进入正/负期望状态；variant共现/primary统计仍不能替代严格消融。
18. 当前逐日回测会为每个评分日重建历史特征，单股短区间可用，但多年和票池权重搜索前必须增加逐日预计算或增量缓存。

## 20. 第一版建议参数

以下仅为开发起点：

```yaml
score:
  open_buy_score: 75
  add_buy_score: 82
  reduce_sell_score: 70
  exit_sell_score: 85
  conflict_score: 70

position_ladder:
  levels: [0.00, 0.25, 0.50, 0.75, 1.00]

execution:
  entry_mode: NEXT_OPEN
  primary_exit_mode: SAME_CLOSE_RESEARCH
  validation_exit_mode: NEXT_OPEN_STRICT
  apply_commission: true
  apply_stamp_duty: true
  apply_transfer_fee: true
  apply_price_limit: true
  apply_halt_filter: true
  t1_lock: true

agent:
  mode: QUANT_ONLY
  max_position_step_adjustment: 1
  may_downgrade_buy: true
  may_override_hard_exit: false
  may_override_hard_veto: false
  require_structured_output: true
  require_evidence_refs: true
  fallback_to_quant_on_error: true
  cache_historical_decisions: true

rebalance:
  minimum_position_delta_pct: 0.02
  action_cooldown_days: 3
```

必须由配置版本统一控制，并同时用于回测、每日评分和自选池维护。

## 21. 最终验收

### 21.1 单股阶段

1. 选择任意历史区间和一只股票。
2. 系统逐日计算买入分、卖出分和目标持仓分。
3. 每日连续分与买点确认状态分开；没有明确入场结构不得开仓。
4. 独立买点事件研究按D+1原始开盘执行并输出固定窗口、MFE/MAE、R、分箱和variant结果。
5. D日买入信号在D+1开盘正确执行。
6. 卖出同时产生研究模式和严格模式结果。
7. 系统完整重放分批建仓、加仓、持有、减仓和清仓。
8. 每笔交易的信号日、执行日、价格、数量、费用和原因可追溯。
9. 每轮持仓周期可以计算完整绩效。
10. 无前视模式通过测试、双价格验收和样本外验证。

### 21.2 自选池阶段

1. 单股引擎不修改即可批量运行。
2. 每只股票每天保存一份唯一评分。
3. 未持仓股票获得 `OPEN/WATCH/BLOCK`。
4. 已持仓股票获得 `ADD/HOLD/REDUCE/EXIT`。
5. 系统给出D+1目标仓位和待执行订单。
6. 组合层正确处理现金、行业和总风险。
7. 多股票同时触发时给出风险调整后的优先顺序。
8. 历史评分、订单、执行和策略版本均可追溯。

### 21.3 LLM与多Agent阶段

1. Agent只能通过工具读取结构化、带日期的量化和研究证据。
2. Agent输出满足固定Schema并完整保存模型、Prompt和证据版本。
3. Agent错误或超时时系统自动回退 `QUANT_ONLY`。
4. Agent不能覆盖硬退出、交易约束和组合风险上限。
5. 同一历史输入可以从缓存重放相同Agent决策。
6. 纯量化、LLM Overlay和多Agent结果可独立回测比较。
7. 只有样本外增量价值稳定的Agent模式才允许成为生产候选。

只有当日线评分、回测、订单生成、执行模型和持仓状态机使用完全相同的代码与参数，且Agent决策具备完整证据、版本和审计记录时，系统才算真正闭环。
