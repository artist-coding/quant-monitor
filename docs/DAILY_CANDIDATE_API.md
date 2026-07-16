# 每日股票候选池 API

该接口用于在交易日收盘、日线数据入库完成后，批量调用同一套
`daily_portfolio` 单股评分引擎，并发布一份可追溯的 D+1 候选池。

接口只读取已经写入 SQLite 的确认日线，不负责从行情供应商拉取数据。
推荐任务顺序：

```text
同步当日日线 -> 校验同步结果 -> 刷新候选池 -> 查询/推送候选结果
```

## 安装和启动

```bash
pip install -e ".[api]"
zt-web
```

默认 API 前缀为 `http://127.0.0.1:8000/api/v1`。

## 刷新候选池

默认读取启用了提醒的 `watchlist` 股票：

```http
POST /api/v1/daily-candidates/refresh
Content-Type: application/json
```

```json
{
  "as_of_date": "20260714",
  "universe": "WATCHLIST",
  "lookback_bars": 180,
  "max_position_pct": 0.1,
  "minimum_buy_score": 60,
  "candidate_statuses": ["CANDIDATE", "CONFIRMED"],
  "top_n": 100,
  "minimum_market_coverage": 1000,
  "allow_partial": false
}
```

也可以显式指定股票：

```json
{
  "as_of_date": "20260714",
  "universe": "EXPLICIT",
  "ts_codes": ["600519.SH", "000001.SZ"],
  "market_context": {
    "score": 57.5,
    "version": "my-market-context-v1",
    "source_hash": "optional-source-fingerprint"
  }
}
```

没有传入 `market_context` 时，服务使用 `as_of_date` 当日全市场有效日线，
按照上涨家数加半数平盘家数的占比生成 0～100 市场宽度分，并对全部输入
生成 SHA-256 指纹。覆盖数量低于 `minimum_market_coverage` 时拒绝刷新。

默认 `allow_partial=false`。任意目标股票没有 `as_of_date` 当日 K 线时返回
HTTP 409，整批不发布，避免候选池混入过期数据。

## 查询候选池

查询最新发布结果：

```http
GET /api/v1/daily-candidates/
```

查询指定交易日：

```http
GET /api/v1/daily-candidates/?trade_date=20260714&limit=100
```

查询一次刷新批次及失败明细：

```http
GET /api/v1/daily-candidates/runs/{run_id}
```

## 持久化和幂等

- `daily_stock_scores`：每只股票、交易日、策略版本和参数指纹一份不可变评分。
- `daily_candidate_pool_runs`：刷新请求、市场快照、数量统计和最终状态。
- `daily_candidate_pool_items`：该批次实际发布的候选及排名。
- `daily_candidate_pool_issues`：缺数、过期或计算失败的逐股原因。

相同输入重复调用会产生新的刷新批次，但复用完全一致的日评分。若同一日期、
策略版本和参数指纹试图写入不同评分，服务会拒绝覆盖，防止历史结果被静默改写。

当前评分资格仍为 `UNVALIDATED_RESEARCH_RULE`，候选池用于研究排序和人工确认，
不代表明日上涨概率，也不直接提交交易订单。
