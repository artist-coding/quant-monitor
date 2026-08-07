# quant-monitor

本地 A 股量化分析与监控项目。核心逻辑是把行情数据同步到本机 SQLite，再用本地代码完成指标计算、选股、回测、模拟和持仓诊断；LLM 只负责自然语言点评、问答和模拟器叙事。

本仓库只管理代码、配置模板和文档，不提交密钥、日志和本地数据库。

## 能做什么

- 单股分析：技术指标、战法信号、主力阶段、综合评分。
- 条件选股：按 B1、突破、量价、图形等策略扫描本地股票池。
- 回测与模拟：支持策略回测、A 股 T+1/涨跌停/成本约束、ATR 仓位等模拟逻辑。
- 持仓与观察池：维护自选股、扫描风险信号、生成诊断结果。
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

Linux 服务器可使用仓库中的 systemd 配置，在每个工作日北京时间 16:30 执行；
节假日会由交易日历自动跳过：

```bash
sudo cp deploy/quant-monitor-daily-sync.service /etc/systemd/system/
sudo cp deploy/quant-monitor-daily-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now quant-monitor-daily-sync.timer
systemctl list-timers quant-monitor-daily-sync.timer
```

运行日志可通过下面的命令查看：

```bash
journalctl -u quant-monitor-daily-sync.service -n 100 --no-pager
```

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

## 目录结构

```text
api/                  FastAPI 后端
frontend/             React Web 看板
modules/              核心 Python 逻辑
modules/indicators/   技术指标和形态识别
modules/strategies/   策略信号
modules/screener/     选股引擎
modules/simulator/    端到端模拟器
modules/data_sync/    数据同步
knowledge/            LLM 点评参考知识
rules/                意图路由和非股票场景提示词
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
