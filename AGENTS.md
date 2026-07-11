<!-- From: /Users/chenlei/005_skill/skills/zettaranc-skill/AGENTS.md -->
# zettaranc-skill · Agent 指南

> 本文件面向 AI 编程 Agent。阅读前请确认你已通读本文件，再操作代码或文档。

---

## 项目概述

本项目是一个 **AI Skill（思维框架蒸馏包）+ 真实数据量化工具** 的混合体。

核心目标：将 B 站 UP 主 / 前阳光私募冠军基金经理 zettaranc（万千）的投资思维框架、决策启发式和表达 DNA，封装为可供 Claude Code / Cursor 等 AI 工具调用的 Skill 文件（`SKILL.md`），同时提供基于真实 Tushare 行情数据的 Python 数据层支撑。

- **核心交付物**：`SKILL.md`（可直接被 AI 工具加载的角色扮演协议）
- **数据层**：Python 模块 + SQLite 数据库 + Tushare API（JNB 模式）
- **Web 看板**：`api/`（FastAPI 后端）+ `frontend/`（React + Vite + Tailwind 前端），可选
- **语料基础**：约 467 篇直播/付费课整理文章（~200 万字）+ 13 个 ztalk 视频 transcript（~12.7 万字）+ 9 篇交易心理系列（~3.3 万字）+ 后续新增文章
- **许可证**：MIT
- **版本**：`docs/CHANGELOG.md` 最新版本为 **v3.6.0**；`pyproject.toml` 中的 `version` 字段已同步为 `3.6.0`。

### 双模式架构

| 模式 | 环境变量 | 说明 |
|------|---------|------|
| **JNB 模式** | `DATA_MODE=jnb` | 接入 Tushare 真实行情，具备实时数据查询、技术指标计算、战法识别能力 |
| **普通小万** | `DATA_MODE=websearch` | 纯 LLM 对话，不走任何外部数据接口 |

### 架构分层

```
Python 数据层（modules/）              LLM 角色层（SKILL.md）
├─ datasource.py         统一数据源协议      ├─ 角色扮演规则
├─ tushare_client.py     Tushare API 封装    ├─ Agentic Protocol（编排/分流逻辑）
├─ bridge_client.py      tushare-data-bridge ├─ 9 个核心心智模型
│                         HTTP 客户端        ├─ 决策启发式
├─ database.py           SQLite 管理         ├─ 表达 DNA
├─ data_sync.py          兼容 shim          └─ 诚实边界
├─ data_sync/            数据同步子包
│   ├─ rate_limiter.py     120次/分限流器
│   ├─ indicator_cache.py  指标缓存写入
│   ├─ fetcher.py          Tushare/Bridge 抓取
│   ├─ syncer.py           增量/全量同步器
│   ├─ cli.py              子命令入口
│   └─ __main__.py         python -m 入口
├─ indicators/           60+ 技术指标
│   ├─ core.py           基础/数学/核心指标
│   ├─ price_patterns/   价格形态识别子包
│   │   ├─ base.py          形态识别基类
│   │   ├─ brick.py         砖形图
│   │   ├─ bull_rope.py     牛绳
│   │   ├─ complex_patterns 复杂形态（蜈蚣图等）
│   │   ├─ key_candles.py   关键 K
│   │   ├─ sandglass.py     沙漏
│   │   └─ screener_helper  选股辅助
│   ├─ volume_patterns.py 量价信号
│   ├─ wave_theory.py    三波理论识别
│   ├─ kirin_detector.py 麒麟会四阶段
│   └─ data_layer.py     数据接入/缓存/可视化
├─ screener.py           兼容 shim
├─ screener/             选股评分子包（含蜈蚣图/沙漏/牛绳过滤）
│   ├─ models.py           数据模型
│   ├─ data.py             数据接入
│   ├─ criteria.py         筛选条件（曼城/B1/趋势/量价/风险）
│   ├─ scoring.py          多维度评分
│   ├─ engine.py           引擎主入口
│   ├─ market.py           市场环境权重
│   ├─ format.py           输出格式化
│   ├─ workflow.py         选股工作流
│   └─ cli.py              子命令入口
├─ simulator/            少女/少妇模拟器（v3.4-v3.6）
│   ├─ simulator.py          主入口
│   ├─ market_context.py     市场环境判定
│   ├─ signal_filter.py      信号过滤（simple / resonance 双模式）
│   ├─ position_sizer.py     ATR 动态仓位
│   ├─ execution_engine.py   撮合执行引擎
│   ├─ execution_constraints A 股约束（T+1/涨跌停/ST/停牌）
│   ├─ cost_model.py         真实成本模型
│   ├─ slippage_model.py     动态滑点
│   ├─ exit_manager.py       止盈止损管理
│   ├─ metrics.py            绩效指标
│   ├─ strategy_adapter.py   战法信号标准化
│   ├─ resonance_scorer.py   多战法共振评分
│   ├─ environment_weights   环境权重动态调整
│   ├─ param_space.py        参数空间与网格生成
│   ├─ walk_forward.py       滚动窗口 OOS 验证
│   └─ optimizer_report.py   walk-forward 报告输出
├─ strategies/           30+ 战法识别（5 子模块）
├─ backtest.py           策略组合回测
├─ backtest_six_step.py  少妇战法六步闭环
├─ loop_engine.py        六步闭环状态机
├─ portfolio_diagnosis.py 持股检查
├─ watchlist.py          自选股观察池
├─ cli.py / cli_commands.py 命令行工具
├─ trade_parser.py       口语化输入解析
├─ trade_manager.py      交易记录 CRUD
├─ trade_reviewer.py     交割单数据准备层
├─ intent_router.py      意图路由
├─ intent_chat.py        LLM 聊天接口
├─ knowledge_retriever.py RAG 知识检索
├─ llm_providers.py      LLM 提供者抽象
├─ setup_wizard.py       初始化配置向导
├─ report.py             Z 哥量化评估报告
├─ commentary_service.py 点评服务
├─ review_generator.py   复盘生成
├─ monitor.py / notifier.py 自选股监控与推送
├─ tracking_manager.py / tracking_syncer.py 自我改进跟踪池
├─ improvement_logger.py / harness_updater.py 改进日志与 Harness 更新
└─ self_optimizer/       Darwin 自优化管线
    ├─ param_registry.py 参数注册表
    ├─ mutator.py        参数变异
    ├─ scorer.py / backtest_scorer.py 评分器
    ├─ llm_judge.py      LLM 裁判
    ├─ reflex_blacklist.py 反射黑名单
    └─ phase1_baseline.py / phase2_hillclimb.py / phase3_report.py 三阶段管线

knowledge/（知识文件，29 篇交易体系）
├─ trading-core.md       短线交易核心
├─ indicators.md         技术指标
├─ sell-discipline.md    卖出纪律
├─ position-management.md 仓位管理
├─ market-macro.md       宏观判断
├─ stock-glossary.md     个股黑话
├─ trend-lines.md        趋势线
├─ exit-strategies.md    逃顶体系
├─ key-candles.md        关键K
├─ advanced-patterns.md  高级战法
├─ portfolio-management.md 组合配置
├─ trading-psychology.md 交易心理
├─ breathing-theory.md   呼吸理论
├─ three-best-principles.md 三最原则
├─ iron-butterfly.md     铁蝴蝶识别
├─ four-rhythms.md       四大节奏
├─ six-tracks-2026.md    2026 赛道
├─ life-decision.md      人生决策框架
├─ career-development.md 职业发展框架
├─ business-judgment.md  创业/商业判断框架
├─ heuristics.md         决策启发式
├─ framework-extraction.md 框架萃取方法
├─ workflow.md           回答工作流 SOP
├─ harness.md            Harness 六大部分
├─ improvement-system.md 改进系统闭环
└─ ... 其他研究与数据字典文件

rules/
├─ intent_rules.yaml     意图匹配规则
├─ career_prompt.md      职业决策框架
└─ life_prompt.md        人生决策框架
```

**关键设计原则**：Python 层只负责 **数据准备**，所有点评、分析话术由 LLM 用 Z 哥角色生成，避免“AI 味”。宿主通过 CLI `--json` 或 Web API 获取结构化数据。

**自优化双管线说明**：
- `self_optimizer/`（Darwin 管线）：LLM 驱动的参数自优化，通过变异 + 评分 + 反射黑名单迭代策略参数组合。
- `simulator/walk_forward`：滚动窗口样本内训练 + 样本外验证的参数寻优。
- 两者**互补** —— Darwin 做探索性优化（发现新参数组合），walk-forward 做验证性优化（防止过拟合）。典型流程：Darwin 产出候选参数集 → walk-forward 验证其样本外稳定性 → 通过者写回 `param_registry`。

---

## 技术栈与运行时架构

### 核心技术栈

| 层级 | 技术 |
|------|------|
| 数据管道 | Python 3.10+（标准库 + `sqlite3`、`pathlib`、`dataclasses`、`enum`） |
| 外部数据 | `tushare`（Pro API，支持中转 URL）、`pandas`、`requests` |
| 环境配置 | `python-dotenv`（`.env` 文件） |
| 数据库 | SQLite（本地文件，15 张核心表 + 4 张自我改进跟踪表） |
| 接口协议 | CLI（`zt` 入口）、可选 FastAPI Web 服务（`zt-web`） |
| 前端看板 | React 19 + Vite 8 + TypeScript 6 + Tailwind CSS 4 + ECharts 6 |
| 状态管理 | Zustand + TanStack React Query |
| 测试框架 | `pytest`（892 用例 passed，11 skipped） |
| 代码质量 | `ruff`（lint + format）、`mypy`、pre-commit |
| 视频下载 | `yt-dlp`（语料采集，可选） |
| 语音转写 | `faster-whisper`（语料采集，可选） |
| 文档格式 | Markdown（全部文档与语料） |
| 版本控制 | Git |

### 关键配置文件

| 文件 | 作用 |
|------|------|
| `pyproject.toml` | 包定义、`zt` 等命令入口、pytest/ruff/mypy 配置、可选依赖分组 |
| `requirements.txt` | 核心 Python 依赖（含 `pyyaml`、`httpx`） |
| `.env.example` | 环境变量模板（.env 不入库） |
| `frontend/package.json` | 前端依赖与脚本 |
| `frontend/vite.config.ts` | Vite 配置（端口 5173，代理 `/api` 到 localhost:8000） |
| `.editorconfig` | 编辑器格式统一配置 |
| `.pre-commit-config.yaml` | 提交前 ruff、部分 mypy、SKILL.md 质量门 |
| `.github/workflows/test.yml` | CI：测试、lint、质量门、真实数据回归、pre-commit |
| `.github/workflows/e2e-cron.yml` | 每周一真实数据回归 cron |

### 环境变量说明（`.env.example`）

```ini
DATA_MODE=jnb                       # jnb(真实数据) 或 websearch(纯对话)
TUSHARE_TOKEN=你的56位token
TUSHARE_API_URL=                    # 中转 API 地址（JNB 模式必填）
DATA_DIR=data
DB_PATH=data/stock_data.db
LLM_API_KEY=***                     # 可选，LLM 回答生成
# KB_ENABLED=true                   # 可选，向量知识库
IM_PUSH_WEBHOOK=                    # 可选，飞书 webhook
```

> v2.1.1 之后，所有 Tushare URL 均从环境变量读取，代码中不再硬编码任何内部域名。

---

## 项目结构与模块划分

```
zettaranc-skill/
├── SKILL.md                    # 核心 Skill 文件（LLM 角色扮演协议）
├── README.md                   # 面向人类用户的项目介绍
├── AGENTS.md                   # 本文件（AI Agent 开发指南）
├── LICENSE                     # MIT
├── pyproject.toml              # 包定义 + 命令入口
├── .env / .env.example         # 本地配置（.env 不入库）
├── .gitignore                  # Git 忽略规则
├── .editorconfig               # 编辑器格式统一配置
├── requirements.txt            # Python 依赖
├── data/                       # 本地 SQLite 数据库与报告（不入库）
│   ├── stock_data.db           # 主数据库
│   └── reports/                # 自动生成的监控/评估报告
├── docs/                       # 项目说明文档
│   ├── CHANGELOG.md            # 版本变更日志（当前 v3.6.0）
│   ├── TODO.md                 # 待办与路线图
│   ├── CONTRIBUTING.md         # 贡献指南
│   ├── USER_GUIDE.md           # 详细使用手册
│   ├── CONFIG_GUIDE.md         # 配置指南
│   └── intent-router-design.md # 意图路由设计文档
├── modules/                    # Python 数据层与业务逻辑
│   ├── datasource.py           # 统一数据源协议（DataSource Protocol + TushareDataSource / BridgeDataSource / SqliteDataSource / CompositeDataSource + get_datasource() 工厂）
│   ├── database.py             # SQLite 15+ 张表、事务上下文、CRUD 助手
│   ├── data_sync.py            # 向后兼容 shim → 实际逻辑在 modules/data_sync/
│   ├── data_sync/              # 数据同步子包（增量/全量，限流 120 次/分）
│   │   ├── rate_limiter.py
│   │   ├── indicator_cache.py
│   │   ├── fetcher.py
│   │   ├── syncer.py
│   │   ├── cli.py
│   │   └── __main__.py
│   ├── tushare_client.py       # Tushare Pro API 封装
│   ├── bridge_client.py        # tushare-data-bridge HTTP 客户端
│   ├── indicators/             # 60+ 技术指标（6 子模块）
│   ├── strategies/             # 30+ 战法识别引擎（5 子模块）
│   ├── screener.py             # 向后兼容 shim → 实际逻辑在 modules/screener/
│   ├── screener/               # 选股评分子包（含蜈蚣图/沙漏/牛绳过滤）
│   │   ├── models.py
│   │   ├── data.py
│   │   ├── criteria.py
│   │   ├── scoring.py
│   │   ├── engine.py
│   │   ├── market.py
│   │   ├── format.py
│   │   ├── workflow.py
│   │   └── cli.py
│   ├── simulator/              # 少女/少妇模拟器（v3.4-v3.6）：A 股真实约束、成本模型、动态滑点、ATR 仓位、战法共振评分、Walk-forward 参数寻优
│   ├── backtest.py             # 多策略融合/组合回测
│   ├── backtest_six_step.py    # 少妇战法六步闭环回测
│   ├── loop_engine.py          # 六步闭环状态机
│   ├── portfolio_diagnosis.py  # 持股检查端到端
│   ├── watchlist.py            # 自选股观察池
│   ├── cli.py / cli_commands.py # 命令行统一入口
│   ├── trade_parser.py         # 口语化/JSON/CSV 交易输入解析
│   ├── trade_manager.py        # 交易记录 CRUD、持仓计算、盈亏统计
│   ├── trade_reviewer.py       # 交割单数据准备层
│   ├── intent_router.py        # YAML 规则意图路由
│   ├── intent_chat.py          # LLM 聊天接口
│   ├── knowledge_retriever.py  # RAG 知识检索适配器
│   ├── llm_providers.py        # LLM 提供者抽象
│   ├── setup_wizard.py         # 初始化向导
│   ├── report.py               # Z 哥量化评估报告
│   ├── commentary_service.py   # 点评服务
│   ├── review_generator.py     # 复盘生成
│   ├── monitor.py / notifier.py # 自选股监控与预警推送
│   ├── tracking_manager.py / tracking_syncer.py # 自我改进跟踪池
│   ├── improvement_logger.py / harness_updater.py
│   └── self_optimizer/         # Darwin 自优化管线
├── api/                        # FastAPI REST API（可选）
│   ├── main.py                 # 服务入口
│   ├── config.py               # pydantic-settings 配置
│   ├── routes/                 # 路由层
│   ├── services/               # 业务服务层
│   ├── models/                 # Pydantic 请求/响应模型
│   └── utils/                  # 序列化等工具
├── frontend/                   # React 前端看板（可选）
│   ├── src/                    # 页面与组件
│   ├── package.json
│   └── vite.config.ts
├── knowledge/                  # 29 篇交易体系知识文档
├── tests/                      # pytest 测试（52 个文件，892 passed / 11 skipped）
├── scripts/                    # 薄壳工具脚本（业务逻辑在 modules/）
│   ├── _common.py
│   ├── sync_watchlist.py
│   ├── sync_and_compute.py
│   ├── batch_compute_indicators.py
│   ├── generate_report.py
│   └── e2e_data_integrity.py
├── corpus/                     # 语料采集与质检工具
│   ├── quality_check.py        # SKILL.md 12 项质量检查
│   ├── batch_download_bilibili.py
│   ├── batch_transcribe.py
│   ├── srt_to_transcript.py
│   └── merge_research.py
└── references/
    └── research/               # 11 份调研提炼文件
        ├── 01-writings.md
        ├── 02-conversations.md
        ├── 03-expression-dna.md
        ├── 04-external-views.md
        ├── 05-decisions.md
        ├── 06-timeline.md
        └── 07-11-*.md
```

**注意**：`references/sources/` 下的原始语料因版权和体积原因 **不提交到 Git**。仓库中只保留调研提炼文件、`SKILL.md` 与转写文本。

---

## 数据库架构

`modules/database.py` 初始化以下表：

| 表名 | 用途 | 关键字段 |
|------|------|---------|
| `stock_basic` | 股票基本信息 | ts_code, name, industry, market, list_date |
| `daily_kline` | 日线 K 线 | open, high, low, close, vol, amount, pct_chg |
| `indicator_cache` | 技术指标缓存（每日快照） | KDJ/MACD/BBI/MA/RSI/WR/布林带/双线/砖形图/DMI/量比/信号 |
| `moneyflow` | 资金流向 | 大小单买卖金额、净流入 |
| `financial_data` | 财务报表 | revenue, net_profit, total_assets, pe, pb, ps |
| `trade_signals` | 交易信号记录 | signal_type, signal_score, signal_price |
| `trade_records` | 随堂测试/交易记录 | action, price, quantity, reason, signal_type, zg_review |
| `sync_log` | 数据同步日志 | data_type, last_date, status |
| `watchlist` | 自选股观察池 | ts_code, name, tags, add_date, alert_enabled |
| `tushare_indicator_cache` | Tushare 官方指标（diff 验证） | macd_dif, rsi_6, kdj_k, boll_mid 等 |
| `llm_response_log` | LLM 响应耗时日志 | ts_code, request_date, model, response_time_ms, success |
| `tracking_pool_self` | 自我改进跟踪池 | ts_code, add_date, status, strategy_tags |
| `tracking_records_self` | 跟踪记录表 | 行情 + 指标 + 信号每日快照 |
| `monthly_reviews_self` | 月度复盘表 | review_month, monthly_return, max_drawdown 等 |
| `strategy_performance_self` | 策略表现统计表 | strategy_name, review_month, accuracy_rate, sharpe_ratio |

每张表均建立合适的复合索引（如 `ts_code + trade_date DESC`）。

---

## 构建、测试与常用命令

### 安装依赖

```bash
# 核心 Python 依赖
pip install -r requirements.txt
# 或安装为本地可编辑包（推荐，会注册 zt 命令）
pip install -e .

# 语料处理可选依赖
pip install -e ".[corpus]"

# 开发测试依赖
pip install -e ".[dev]"
```

安装后可使用 `zt` 命令：

```bash
zt analyze 600487.SH
zt screen --strategy B1 --limit 20
zt watchlist scan
zt backtest shaofu 600487.SH --days 250
```

### 运行测试

```bash
# 全部测试（验证结果：892 passed, 11 skipped）
python -m pytest tests/ -v

# 单文件测试
python -m pytest tests/test_indicators.py -v

# 慢速端到端测试（默认不跑）
python -m pytest tests/ -m slow -v
```

> `test_indicators_realdata.py` 等真实数据测试会在无 `TUSHARE_TOKEN` 时自动 skip。

### 数据库初始化与数据同步

```bash
# 初始化数据库（创建 15+ 张表）
python -m modules.database

# 同步股票基本信息（全量 5525 只）
python -m modules.data_sync sync

# 同步单只股票 K 线 + 指标缓存
python -m modules.data_sync sync --ts_code 600487.SH --days 365 --indicators

# 查看同步状态
python -m modules.data_sync status

# 同步 Tushare 官方指标（diff 验证）
python -m modules.data_sync stk-factor --ts_code 600487.SH --days 365
```

### CLI 主要命令

```bash
zt analyze <ts_code> [--days N] [--json]          # 分析单只股票
zt screen --strategy <策略> [--limit N] [--json]   # 批量选股
zt score <ts_code> [--json]                        # 综合评分
zt diagnose <ts_code> [--days N] [--json]          # 持仓诊断
zt watchlist add <ts_code> --tags <标签>           # 添加自选股
zt watchlist list                                  # 查看观察池
zt watchlist scan [--json]                         # 批量扫描信号
zt backtest shaofu <ts_code> [--days N] [--json]   # 少妇战法回测
zt backtest multi <ts_code> [--days N] [--json]    # 多策略融合回测
zt backtest portfolio <c1,c2,...> [--days N]       # 组合回测
zt simulate [codes] --days N --capital N --max-positions N --risk R --score S --signals N --json  # 交易模拟器
zt simulate [codes] --strategy-mode resonance --strategy-lookback N --min-resonance-score S --json  # 战法共振模式
zt simulate [codes] --walk-forward --wf-train-days N --wf-test-days N --wf-objective calmar --json  # Walk-forward 寻优
zt trade add "口语化交易描述"                       # 记录交易
zt trade list / review / stats                     # 交易记录管理
zt daily [--json]                                  # 每日五步工作流
zt monitor [--json] [--no-push]                    # 自选股主动监控
zt self-optimize ...                               # Darwin 自优化
zt sync init/sync/status/stk-factor                # 数据同步
```

### 启动 Web 看板

> `api/` 依赖 `fastapi`、`uvicorn`、`pydantic-settings`，当前未写入 `requirements.txt`，运行前请单独安装：

```bash
pip install fastapi uvicorn pydantic-settings

# 启动后端
zt-web
# 或 python -m api.main

# 启动前端（另开终端）
cd frontend
npm install
npm run dev        # 默认 http://localhost:5173
npm run build      # 生产构建
npm run lint       # ESLint 检查
```

### 质量检查

```bash
# 验证 SKILL.md 是否通过 12 项质量标准
python corpus/quality_check.py SKILL.md

# strict 模式（任一不通过则 exit 1）
python corpus/quality_check.py SKILL.md --strict
```

### 语料采集脚本

| 脚本 | 用法 | 说明 |
|------|------|------|
| `corpus/batch_download_bilibili.py` | `python corpus/batch_download_bilibili.py` | 下载 B 站 ztalk 音频 |
| `corpus/batch_transcribe.py` | `python corpus/batch_transcribe.py` | 音频转写文本 |
| `corpus/srt_to_transcript.py` | `python corpus/srt_to_transcript.py input.srt` | 字幕清洗为纯文本 |
| `corpus/merge_research.py` | `python corpus/merge_research.py` | 合并调研结果 |

**路径约定**：部分脚本使用硬编码相对路径，请在项目根目录执行，并注意 `references/sources/` 中的原始语料不入库。

---

## 代码风格与开发规范

### 通用规范

- 所有脚本文件头包含 `#!/usr/bin/env python3`
- 使用 **中文** 编写文档字符串和注释
- 使用标准库为主，避免引入不必要的第三方依赖
- 每个模块文件末尾包含 `if __name__ == "__main__":` 命令行入口

### 编辑器配置（`.editorconfig`）

| 文件类型 | 缩进 | 大小 |
|---------|------|------|
| `*.py` | space | 4 |
| `*.sh` | space | 4 |
| `*.md` | space | 2（不裁剪行尾空格） |
| `*.json` | space | 2 |
| 全部 | UTF-8 | LF 换行 |

### Python 模块规范

- **数据库路径**：统一从 `os.getenv("DB_PATH", "data/stock_data.db")` 读取，支持相对路径和绝对路径
- **环境变量加载**：统一由 `modules/__init__.py` 在包首次 import 时一次性加载 `.env`；各子模块不再重复加载
- **模块间 DB 路径解析**：`modules/*.py` 使用 `Path(__file__).parent.parent`（项目根目录）；`modules/indicators/*.py` 使用 `Path(__file__).parent.parent.parent`
- **限流控制**：所有 Tushare API 调用必须带 `_rate_limit()`，控制 120 次/分钟
- **事务管理**：数据库操作统一使用 `get_connection()` 上下文管理器（自动 commit/rollback，默认 WAL 模式）
- **错误处理**：API 调用用 try/except 包裹，记录 error log，返回空 DataFrame/None 而非抛异常中断
- **包安装**：使用 `pip install -e .` 安装后，可通过 `zt` 命令或 `python -m modules.cli` 调用

### Lint / Format / Type（`pyproject.toml` 配置）

- **ruff**：`line-length = 120`，`target-version = py310`，扩展排除 `data/`、`logs/`、`knowledge/`
  - lint 选择：`F, E, W, UP`
  - 忽略：`E501, F401, F403`
  - 测试文件额外忽略 `F811`
  - format：`quote-style = "double"`，`indent-style = "space"`
- **mypy**：`ignore_missing_imports = true`，仅对关键路径做类型检查
- **pre-commit**：每次 commit 自动跑 ruff、部分 mypy、SKILL.md 12 项质量门、merge/yaml/行尾空白检查

### 版本规则

采用语义化版本，但含义针对本项目定制：

| 位 | 含义 | 示例 |
|----|------|------|
| MAJOR | 心智模型级别的重构 | v1.3.0：将 6 个心智模型重组为 5 个 |
| MINOR | 新增战术/启发式/语料/模块 | v2.0.0：新增 Tushare 数据层和 8 个 Python 模块 |
| PATCH | 排版修正、安全修复、数字更新 | v2.1.1：移除 URL 硬编码 |

---

## 测试策略

### 测试架构

- **框架**：pytest
- **配置**：`pyproject.toml` 中 `testpaths = ["tests"]`，默认 `-v --tb=short`
- **标记**：`@pytest.mark.slow` 用于慢速端到端测试（如 self_optimizer 多轮），默认不跑
- **Fixture**：`tests/conftest.py` 提供
  - `mock_env_for_tests`：自动将环境变量 mock 到临时目录
  - `temp_db`：初始化好的临时数据库
  - `db_conn`：数据库连接
- **数据工厂**：`make_kline_row()`、`make_daily_data()`、`generate_uptrend_klines()`、`generate_downtrend_klines()`、`generate_b1_scenario()` 等
- **数据库隔离**：所有测试使用临时 SQLite 文件，互不干扰

### 测试覆盖范围（当前 52 个测试文件）

| 测试文件 | 覆盖范围 |
|---------|---------|
| `test_database.py` | 路径解析、连接上下文、事务回滚、表初始化、幂等性 |
| `test_indicators.py` | 60+ 指标计算（MA/EMA/KDJ/MACD/背离/BBI/RSI/WR/布林带/量比/双线/单针/砖形图/B1B2/呼吸结构/SB1/沙漏/牛绳/蜈蚣图等） |
| `test_strategies.py` | B1/B2/B3/SB1/长安/四分之三阴量/娜娜/异动地量/出货五式等 |
| `test_screener.py` / `test_screener_p3.py` | 选股评分、P3 指标接入评分 |
| `test_backtest.py` / `test_loop_engine.py` / `test_backtest_six_step.py` | 回测框架与六步闭环 |
| `test_portfolio_diagnosis.py` | 持股检查、防卖飞、出货信号、战法匹配 |
| `test_watchlist.py` | 观察池增删改查、批量扫描 |
| `test_wave_theory.py` | 三波理论识别 |
| `test_kirin_detector.py` | 麒麟会四阶段 |
| `test_cli_screen.py` / `test_cli_subparser.py` | CLI 子命令分发与参数解析 |
| `test_data_e2e.py` / `test_data_sync_extensions.py` | 数据层端到端与同步扩展 |
| `test_trade_manager.py` / `test_trade_parser.py` | 交易记录 CRUD、口语化解析 |
| `test_intent_router.py` | 意图路由规则匹配 |
| `test_quality_check.py` | SKILL.md 12 项质量检查 |
| `test_rate_limiter.py` | 120次/分钟限流器 |
| `test_bridge_client.py` | tushare-data-bridge HTTP 客户端与降级网关 |
| `test_monitor.py` / `test_notifier.py` | 自选股监控与推送 |
| `test_tracking_system.py` | 自我改进跟踪池 |
| `test_self_optimizer_*.py` / `test_param_registry.py` / `test_mutator.py` / `test_scorer.py` / `test_break_signal.py` / `test_reflex_blacklist.py` / `test_backtest_scorer.py` | Darwin 自优化管线 |
| `test_indicators_realdata.py` | 真实 Tushare 数据指标回归（无 token 时 skip） |

### 运行预期

```bash
$ python -m pytest tests/ -v
# 验证结果：892 passed, 11 skipped
```

---

## 文件修改优先级

1. **`SKILL.md`** —— 直接影响 Skill 表现，任何改动都需语料支撑
2. **`knowledge/*.md`** —— 知识文档，补充新语料或修正旧发现时更新
3. **`modules/*.py`** —— 数据层代码改动需同步更新测试
4. **`references/research/*.md`** —— 调研档案，新增语料源时更新
5. **`README.md` / `docs/CHANGELOG.md`** —— 项目对外文档，版本发布时同步更新
6. **`api/` / `frontend/`** —— Web 看板，仅在交互层需要改进时修改
7. **`scripts/`** —— 工具脚本，仅在数据管道或检查逻辑需要改进时修改

---

## 内容修改原则

1. **最小改动原则**：只改确实不准确的部分
2. **有依据**：任何改动都需要语料支撑，不能凭印象。优先来源：
   - zettaranc 本人直接产出（视频、直播、付费课、雪球专栏）
   - 权威媒体报道（澎湃新闻等）
   - 证券业协会公示资料
   - **不应作为主要依据**：知乎回答、非本人微信公众号、股吧/雪球帖子（除本人账号外）
3. **保持角色一致性**：修改后的回答仍需符合 zettaranc 的表达 DNA

### 风格验证清单

修改 `SKILL.md` 后，用以下问题自检：

- [ ] 是否用「我」而非「Z 哥认为...」？
- [ ] 是否包含职业背书开场？
- [ ] 是否分 1/2/3/4 点拆解？
- [ ] 是否用了具体数字或案例？
- [ ] 是否以金句或反问收尾？
- [ ] 是否避免跳出角色的表述？
- [ ] 交易建议是否包含具体的进场/止损/止盈规则？

---

## 安全与合规考虑

1. **免责声明**：`SKILL.md` 和 `README.md` 均包含明确免责声明——**不构成任何投资建议**。
2. **版权边界**：原始语料不提交到仓库。仓库中只保留粉丝整理的 Markdown 提炼文件和转写文本。
3. **敏感信息**：Tushare Token、API URL、LLM API Key、飞书 webhook 通过 `.env` 文件管理，**绝不硬编码**；`.env` 已加入 `.gitignore`。
4. **信息偏差标注**：`SKILL.md` 的「诚实边界」一节明确标注了公开表达与真实想法的差异。
5. **高风险动作**：Skill 不会代下单、转账或处理内幕信息；给出买卖建议时必须附加免责声明。
6. **语料截止期**：信息截止到调研时间（2026-04-18 及后续更新）。

---

## 常见任务速查

| 任务 | 操作 |
|------|------|
| 更新心智模型或交易规则 | 先查 `references/research/01-writings.md` 和 `05-decisions.md` → 修改 `SKILL.md` 与对应 `knowledge/*.md` → 运行 `corpus/quality_check.py SKILL.md` |
| 补充新语料 | 将新文章放入 `references/sources/articles/` → 更新对应 `references/research/*.md` → **不要**将原始语料加入 git |
| 新增 B 站视频 transcript | `python corpus/batch_download_bilibili.py && python corpus/batch_transcribe.py` |
| 发布新版本 | 更新 `SKILL.md` → 更新 `docs/CHANGELOG.md` → 同步 `README.md` 版本 badge → 考虑同步 `pyproject.toml` 版本号 → 打 git tag |
| 验证风格一致性 | 对照「风格验证清单」逐项检查 |
| 修复数据层 bug | 修改 `modules/*.py` → 补充/更新 `tests/test_*.py` → `pytest tests/ -v` |
| 接入新 Tushare 接口 | 修改 `modules/tushare_client.py` 或 `modules/data_sync.py` → 确认 `modules/database.py` 表结构支持 → 补充保存逻辑与测试 |
| 初始化全新环境 | `cp .env.example .env` → 填入 Token → `python -m modules.database` → `python -m modules.data_sync sync` → `pytest tests/ -v` |
| 运行 Web 看板 | 安装 `fastapi uvicorn pydantic-settings` → `zt-web` → `cd frontend && npm install && npm run dev` |
| 跑 Darwin 自优化 | `zt self-optimize --target trading --rounds 3`（默认 dry_run） |

---

## 外部依赖安装

```bash
# Python 依赖
pip install -r requirements.txt
pip install -e .

# Web API 依赖（运行 zt-web 时需要）
pip install fastapi uvicorn pydantic-settings

# 前端依赖
cd frontend && npm install

# yt-dlp 可能需要 ffmpeg（处理音频）
# macOS: brew install ffmpeg
```

**注意**：`faster-whisper` 的 base 模型首次运行时会自动下载到本地缓存（约 150MB）。

---

> Love and Share 🖤
