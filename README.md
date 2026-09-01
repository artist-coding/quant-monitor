# quant-monitor

本地 A 股量化分析与监控项目。核心逻辑是把行情数据同步到本机 SQLite，再用本地代码完成指标计算、选股、回测、模拟和持仓诊断；LLM 只负责自然语言点评、问答和模拟器叙事。

本仓库只管理代码、配置模板和文档，不提交密钥、日志和本地数据库。

## 能做什么

- 单股分析：技术指标、战法信号、主力阶段、综合评分。
- 条件选股：按 B1、突破、量价、图形等策略扫描本地股票池。
- 回测：多策略融合回测与多股组合回测，用于验证战法有效性。
- 持仓与观察池：维护自选股、扫描买卖信号、生成诊断结果与预警推送。
- Web 看板：FastAPI 后端 + React 前端，作为可选界面。
- LLM 点评：读取结构化分析结果后生成中文解释，不参与指标计算。

## 数据逻辑

项目默认使用本地数据库：

```text
data/stock_data.db
```

这个文件不会进入 Git。换电脑时有两种方式：

1. 直接把 `data/stock_data.db` 单独拷过去。
2. 在新电脑重新配置 Token 后同步数据。

当前代码的核心计算路径是：

```text
Tushare/AkShare/其他数据源 -> SQLite -> indicators/strategies/screener/backtest/simulator -> CLI/API/LLM 点评
```

注意：历史库如果是未复权日线，后续增量同步也应保持同一口径，避免把未复权和前复权数据混在一起。

## 快速开始

```powershell
git clone https://github.com/artist-coding/quant-monitor.git
cd quant-monitor
python -m pip install -r requirements.txt
python -m pip install -e .
```

复制配置模板：

```powershell
Copy-Item .env.example .env
```

然后编辑 `.env`，至少确认这些字段：

```env
DATA_MODE=jnb
DB_PATH=data/stock_data.db
TUSHARE_TOKEN=你的Token
TUSHARE_API_URL=http://api.waditu.com/dataapi

LLM_BASE_URL=https://你的LLM地址/v1/chat/completions
LLM_API_KEY=你的LLMKey
LLM_MODEL=你的模型名
```

`.env` 已被 `.gitignore` 排除，不要提交。

## 常用命令

分析单只股票：

```powershell
python -m modules.cli analyze 000001.SZ --days 120
```

输出 JSON，方便其他程序调用：

```powershell
python -m modules.cli analyze 000001.SZ --days 120 --json
```

选股扫描：

```powershell
python -m modules.cli screen --strategy B1 --limit 20
```

查看数据同步状态：

```powershell
python -m modules.cli sync status
```

### 收盘后更新全 A 股日线

日常更新不需要逐只股票请求。项目会先通过交易日历判断当天是否开市，再按
`trade_date` 一次获取当天全市场日线并幂等写入 SQLite：

```powershell
python3 -m modules.cli sync market-daily
```

指定日期补录或输出 JSON：

```powershell
python3 -m modules.cli sync market-daily --date 20260717 --json
```

该入口同步的是 Tushare 官方 `daily` 未复权日线，字段包括开高低收、成交量、
成交额和涨跌幅；不会自动计算全市场技术指标。请不要在同一个数据库中混用
未复权日线与旧的逐股前复权同步结果。

### 选股流程（手动四步）

每日编排器与它的 systemd 定时任务已移除——选股改为手动触发，
四步各自独立、可单独重跑：

```bash
# 1. 录入当日活跃市值（选股的总开关，收盘后由用户提供）
python3 -m modules.cli amv add 20260810 --close 215000

# 2. 同步当日全市场日线（主线强度与建仓参考都从这里算）
python3 -m modules.cli sync market-daily

# 3. 刷新主线/行业强度排名
python3 -m modules.cli theme rank --lookback 5

# 4. 全市场扫描：活跃市值门槛 → B1 买点确认 → 主线/行业筛选
python3 -m modules.cli scan --save
```

#### 活跃市值：选股的总开关

能不能选股由**活跃市值多空区间**单独决定，规则如下（`p` 为当日涨幅）：

| 条件 | 结果 |
|---|---|
| `p < -2.3%` | 空头区间 → **完全不选股、不新建仓** |
| `p >= 4%` 或 `p + p昨 >= 4%` | 多头区间 → 可选股 |
| 其余 | 沿用前一日区间 |

两处容易写错、已用 8180 个交易日的官方标注反推验证（逐日 100% 吻合）：

- **空头触发优先于两日累计多头**：1993-03-08 涨 10.77% 进多头，次日跌 5.44%，
  两日累计仍有 +5.33%，但标注是空头。
- **涨幅必须由收盘价现算**：1993-10-11 显示 -2.30%（实为 -2.295082%，多头）
  与 1997-11-11 显示 -2.30%（实为 -2.302724%，空头）结论相反。
  所以 `amv add` 优先给 `--close`，`--pct` 只是备选。

历史数据用 `python3 -m modules.cli amv import <0AMV-YYMMDD-增强.csv>` 导入，
`amv verify` 可随时把重算结果与文件自带标注逐日比对。

**全市场宽度不再决定能否选股**，降级为建仓轻重的参考——同样是多头区间，
宽度 80 和宽度 55 该上的仓位不一样，扫描输出里会给一个仓位档位建议。

主线成员由外部判定器产出后导入，系统只负责排强弱，
格式见 [docs/theme-import-format.md](docs/theme-import-format.md)：

```bash
python3 -m modules.cli theme import themes.json --replace
```

### 历史回放：这套选股框架到底赚不赚钱

`zt replay` 把上面第 4 步的整条流水线在历史上逐日重跑一遍，统计每次选出来的票
在**之后 30 个交易日**的涨跌：

```bash
# 2024-09-24 起逐日回放，导出逐笔明细
python3 -m modules.cli replay --start 20240924 --workers 14 --csv replay.csv

# 加跑空头区间作对照，量化"活跃市值总开关"的贡献（约 2.4 倍耗时）
python3 -m modules.cli replay --start 20240924 --gate off --workers 14
```

口径：

- **逐日回放，不是每日买入**。多数交易日的产出是空的，这本身就是框架的一部分。
- **不接入外部主线判定器**。`theme_members` 为空时第二阶段全部落到行业兜底，
  回测看的就是"纯指标选股"的结果。
- **次日开盘买、第 30 个交易日收盘卖**。天数按全市场交易日历数，
  个股停牌不延长持有期；次日一字涨停买不进的单子会单独标出。
- **止损两种口径都给**：`路径止损` 是期间最低价触及 −20% 即记 −20%（强止损的
  真实行为），`只封底` 只截断最终收益率（字面读法，也是止损效果的乐观上界）。
- **自带基准**：同期全市场等权 30 日收益，选股收益减掉它才是超额。

第一次跑会先补算每个决策日的分组强度快照（缺快照会导致第二阶段全员落选，
且空得很像"框架没选出票"）；算过之后可加 `--skip-precompute`。

> 回放期间不要同时跑数据同步：多进程持续读会让写方拿不到 SQLite 的锁。

运行测试：

```powershell
python -m pytest tests/ -v
```

## Web 看板

后端：

```powershell
zt-web
```

前端：

```powershell
cd frontend
npm install
npm run dev
```

默认访问：

```text
http://localhost:5173
```

## 部署

长期在线的部署走 systemd 用户级单元：后端 8010、前端 4173，外加日线和活跃市值两条定时同步。

- `deploy/README.md` — 八份单元怎么装，每条 timer 为什么排在那个点，六个会静默坏掉的点。
- `deploy/NEW_SERVER.md` — **换一台服务器**要额外做什么：密钥和 1.8G 行情库怎么搬、
  单元里写死的绝对路径怎么改、Node 版本和时区这两道硬门槛、哪些东西两台机器不能同时跑。

## 目录结构

```text
api/                  FastAPI 后端
frontend/             React Web 看板
modules/              核心 Python 逻辑
modules/indicators/   技术指标和形态识别
modules/strategies/   策略信号（B1/B2/B3 买入、S1/S2/S3 等卖出）
modules/screener/     选股引擎
modules/data_sync/    数据同步
scripts/              数据维护和批处理脚本
tests/                自动化测试
docs/                 使用说明、变更记录和归档资料
data/                 本地数据库，不入 Git
logs/                 本地日志，不入 Git
```

## 归档资料

为了让主仓库更轻，历史研究和生成产物已经从首页移开：

- `docs/archive/README.legacy.md`：原始长 README，包含完整策略介绍和历史说明。
- `docs/archive/references/`：语料研究和观点提炼材料。
- `docs/archive/corpus/`：语料采集、转录、合并和 `SKILL.md` 质量检查脚本。
- `docs/archive/superpowers/`：历史功能规格和开发计划。

这些内容不属于日常运行路径，但对追溯策略来源、维护 `SKILL.md` 和理解旧功能设计仍有价值。

## Git 管理规则

不要提交：

- `.env`
- `data/stock_data.db`
- `data/`
- `logs/`
- `reports/`
- Python/Node 构建缓存

常规提交流程：

```powershell
git status
git add .
git commit -m "说明这次改了什么"
git push
```

## 免责声明

本项目用于个人研究、策略复盘和工程实验，不构成投资建议。任何交易决策都需要自行承担风险。
