"""主线子系统（modules/themes.py）测试。

覆盖：
- 主线定义与成员的增删改查、幂等导入、replace 语义
- 强度的百分位定标：不饱和、能拉开差距
- 情绪面的经验贝叶斯收缩：小样本"零涨停"不再被打到地板
- 没有主线归属时退回行业兜底
"""

from __future__ import annotations

import pytest

from modules import themes as th


# ==================== 造数 ====================


def _write_klines(rows):
    """rows: (ts_code, trade_date, pct_chg, vol, amount, is_limit_up)"""
    from modules.database import get_connection

    with get_connection() as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO daily_kline
            (ts_code, trade_date, open, high, low, close, vol, amount, pct_chg, is_limit_up, is_limit_down)
            VALUES (?, ?, 10, 11, 9, 10, ?, ?, ?, ?, 0)
            """,
            [(c, d, v, a, p, lu) for c, d, p, v, a, lu in rows],
        )


DATES = ["20260803", "20260804", "20260805", "20260806", "20260807"]


def _make_group(prefix, count, pct_per_day, *, limit_days=0, vol=1000.0, start=0):
    """造一组行为一致的票：每天涨 pct_per_day，前 limit_days 天涨停。"""
    rows = []
    for i in range(count):
        code = f"{prefix}{start + i:04d}.SZ"
        for j, d in enumerate(DATES):
            rows.append((code, d, pct_per_day, vol, vol * 10, 1 if j < limit_days else 0))
    return rows


def _write_stock_basic(pairs):
    """pairs: (ts_code, industry)"""
    from modules.database import get_connection

    with get_connection() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO stock_basic (ts_code, name, industry) VALUES (?, ?, ?)",
            [(c, c, ind) for c, ind in pairs],
        )


# ==================== 主线定义与成员 ====================


def test_upsert_and_list_theme(temp_db):
    th.upsert_theme("商业航天", "卫星互联网产业链")
    th.upsert_theme("大金融")

    rows = th.list_themes()
    names = {r["name"] for r in rows}
    assert names == {"商业航天", "大金融"}
    assert next(r for r in rows if r["name"] == "商业航天")["description"] == "卫星互联网产业链"


def test_upsert_theme_is_idempotent_and_updates(temp_db):
    th.upsert_theme("算力", "旧说明")
    th.upsert_theme("算力", "新说明")
    rows = th.list_themes()
    assert len(rows) == 1
    assert rows[0]["description"] == "新说明"


def test_upsert_theme_rejects_blank_name(temp_db):
    with pytest.raises(ValueError):
        th.upsert_theme("   ")


def test_import_members_auto_creates_theme(temp_db):
    res = th.import_members(
        [{"theme": "商业航天", "ts_code": "600879.sh", "confidence": 0.9, "reason": "火箭"}],
        source="kimi-swarm",
    )
    assert res["imported"] == 1
    assert th.list_themes()[0]["name"] == "商业航天"
    # ts_code 统一大写
    assert th.get_theme_members("商业航天") == ["600879.SH"]


def test_import_members_skips_incomplete_records(temp_db):
    res = th.import_members([{"theme": "X"}, {"ts_code": "600000.SH"}, {"theme": "X", "ts_code": "600000.SH"}])
    assert res["imported"] == 1
    assert len(res["skipped"]) == 2


def test_import_members_clamps_confidence(temp_db):
    """外部判定器给出越界置信度时夹住而不是丢弃——宁可保守也别丢数据。"""
    th.import_members(
        [
            {"theme": "X", "ts_code": "600000.SH", "confidence": 5},
            {"theme": "X", "ts_code": "600001.SH", "confidence": -2},
            {"theme": "X", "ts_code": "600002.SH", "confidence": "不是数字"},
        ]
    )
    from modules.database import get_connection

    with get_connection() as conn:
        got = dict(conn.execute("SELECT ts_code, confidence FROM theme_members").fetchall())
    assert got["600000.SH"] == 1.0
    assert got["600001.SH"] == 0.0
    assert got["600002.SH"] == 1.0  # 非数值退回默认 1.0


def test_import_members_replace_drops_stale_members(temp_db):
    """外部判定器整体重跑时，退出主线的票必须消失，不能残留。"""
    th.import_members([{"theme": "X", "ts_code": "600000.SH"}, {"theme": "X", "ts_code": "600001.SH"}])
    th.import_members([{"theme": "X", "ts_code": "600001.SH"}], replace=True)
    assert th.get_theme_members("X") == ["600001.SH"]


def test_import_members_without_replace_upserts(temp_db):
    th.import_members([{"theme": "X", "ts_code": "600000.SH", "confidence": 0.5}])
    th.import_members([{"theme": "X", "ts_code": "600000.SH", "confidence": 0.9}])
    from modules.database import get_connection

    with get_connection() as conn:
        rows = conn.execute("SELECT ts_code, confidence FROM theme_members").fetchall()
    assert len(rows) == 1
    assert rows[0][1] == 0.9


def test_import_members_strict_mode_rejects_unknown_theme(temp_db):
    with pytest.raises(ValueError, match="未知主线"):
        th.import_members([{"theme": "没建过", "ts_code": "600000.SH"}], auto_create_theme=False)


def test_inactive_theme_excluded_from_ranking(temp_db):
    _write_klines(_make_group("00", 5, 3.0))
    th.import_members([{"theme": "T", "ts_code": f"00{i:04d}.SZ"} for i in range(5)])
    th.set_theme_active("T", False)
    res = th.rank_themes("20260807", persist=False)
    assert res["themes"] == []


def test_remove_theme_drops_members(temp_db):
    th.import_members([{"theme": "X", "ts_code": "600000.SH"}])
    assert th.remove_theme("X") is True
    assert th.get_theme_members("X") == []
    assert th.list_themes() == []


def test_get_stock_themes_orders_by_confidence(temp_db):
    th.import_members(
        [
            {"theme": "弱归属", "ts_code": "600000.SH", "confidence": 0.3},
            {"theme": "强归属", "ts_code": "600000.SH", "confidence": 0.95},
        ]
    )
    assert th.get_stock_themes("600000.SH") == ["强归属", "弱归属"]


# ==================== 强度计算 ====================


def test_rank_themes_orders_strong_above_weak(temp_db):
    """涨的组必须排在跌的组前面。"""
    _write_klines(_make_group("AA", 6, 3.0, limit_days=2))  # 强
    _write_klines(_make_group("BB", 6, -2.0, start=100))  # 弱
    # 参照系：一批中性行业
    for k in range(12):
        _write_klines(_make_group("CC", 10, 0.2, start=200 + k * 10))
        _write_stock_basic([(f"CC{200 + k * 10 + i:04d}.SZ", f"行业{k}") for i in range(10)])

    th.import_members([{"theme": "强主线", "ts_code": f"AA{i:04d}.SZ"} for i in range(6)])
    th.import_members([{"theme": "弱主线", "ts_code": f"BB{100 + i:04d}.SZ"} for i in range(6)])

    res = th.rank_themes("20260807", persist=False)
    names = [g.name for g in res["themes"]]
    assert names == ["强主线", "弱主线"]
    assert res["themes"][0].strength > res["themes"][1].strength
    assert res["themes"][0].rank == 1


def test_strength_does_not_saturate_in_a_bull_week(temp_db):
    """全市场普涨时强度不能全顶到 100——那是最初版本的致命缺陷。

    这里造 15 个都在涨的行业（涨幅各不相同），若用"上涨占比+加分"的绝对分
    公式，它们会全部被 clamp 到 100 而分不出先后。
    """
    for k in range(15):
        _write_klines(_make_group("DD", 10, 1.0 + k * 0.5, start=k * 10))
        _write_stock_basic([(f"DD{k * 10 + i:04d}.SZ", f"行业{k}") for i in range(10)])

    res = th.rank_themes("20260807", persist=False)
    strengths = [g.strength for g in res["industries"]]
    assert len(strengths) == 15
    assert max(strengths) <= 100.0
    # 至少要能区分出一半以上的不同档位，不能挤成一坨
    assert len(set(round(s, 2) for s in strengths)) >= 8
    # 涨得最多的行业必须排第一
    assert res["industries"][0].name == "行业14"


def test_sentiment_shrinkage_protects_small_groups(temp_db):
    """小样本组"零涨停"不该被判成情绪面垫底。

    涨停是稀有事件：6 只票 × 5 天只有 30 个样本，期望涨停数不足 1，
    拿到 0 是最可能的结果。不收缩就会系统性地把小主线打到地板。
    """
    # 参照系：20 个 30 只票的行业，其中大部分有零星涨停
    for k in range(20):
        limit = 1 if k % 2 == 0 else 0
        _write_klines(_make_group("EE", 30, 2.0, limit_days=limit, start=k * 30))
        _write_stock_basic([(f"EE{k * 30 + i:04d}.SZ", f"行业{k}") for i in range(30)])

    # 一条 6 只票、涨幅和大盘一致、但一个涨停都没有的主线
    _write_klines(_make_group("FF", 6, 2.0, start=5000))
    th.import_members([{"theme": "小主线", "ts_code": f"FF{5000 + i:04d}.SZ"} for i in range(6)])

    res = th.rank_themes("20260807", persist=False)
    small = res["themes"][0]
    assert small.member_count == 6
    assert small.limit_up_count == 0
    # 收缩后应贴近市场中位，而不是被打到地板（未收缩时这里会是个位数）
    assert small.sentiment_pct > 25, f"小样本组情绪面被误杀: {small.sentiment_pct}"


def test_group_below_min_members_is_dropped(temp_db):
    _write_klines(_make_group("GG", 2, 5.0))
    th.import_members([{"theme": "太小", "ts_code": f"GG{i:04d}.SZ"} for i in range(2)])
    res = th.rank_themes("20260807", persist=False)
    assert res["themes"] == []


def test_members_without_kline_are_ignored(temp_db):
    """外部判定器给的票可能已退市或库里没数据，不能因此崩掉。"""
    _write_klines(_make_group("HH", 4, 2.0))
    members = [{"theme": "T", "ts_code": f"HH{i:04d}.SZ"} for i in range(4)]
    members.append({"theme": "T", "ts_code": "999999.SZ"})  # 库里没有
    th.import_members(members)
    res = th.rank_themes("20260807", persist=False)
    assert res["themes"][0].member_count == 4


def test_rank_themes_persists_and_is_idempotent(temp_db):
    _write_klines(_make_group("II", 5, 2.0))
    th.import_members([{"theme": "T", "ts_code": f"II{i:04d}.SZ"} for i in range(5)])

    first = th.rank_themes("20260807")
    second = th.rank_themes("20260807")
    assert first["written"] == second["written"] >= 1

    from modules.database import get_connection

    with get_connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM theme_strength WHERE trade_date = '20260807' AND theme = 'T'"
        ).fetchone()[0]
    assert count == 1, "重跑必须覆盖而不是新增"


def test_rank_themes_with_no_kline_data_returns_reason(temp_db):
    res = th.rank_themes("20260807", persist=False)
    assert res["written"] == 0
    assert "没有任何日线数据" in res["reason"]


def test_lookback_window_respects_available_dates(temp_db):
    _write_klines(_make_group("JJ", 5, 1.0))
    res = th.rank_themes("20260805", lookback=3, persist=False)
    assert res["window"] == ["20260803", "20260804", "20260805"]


# ==================== 个股 → 主线强度 ====================


def test_get_stock_theme_strength_prefers_theme_over_industry(temp_db):
    _write_klines(_make_group("KK", 5, 2.0))
    _write_stock_basic([(f"KK{i:04d}.SZ", "某行业") for i in range(5)])
    th.import_members([{"theme": "真主线", "ts_code": f"KK{i:04d}.SZ"} for i in range(5)])
    th.rank_themes("20260807")

    got = th.get_stock_theme_strength("KK0000.SZ", "20260807")
    assert got["kind"] == "theme"
    assert got["theme"] == "真主线"


def test_get_stock_theme_strength_falls_back_to_industry(temp_db):
    """没有任何主线归属的票退回行业兜底，且 kind 如实标明。"""
    _write_klines(_make_group("LL", 5, 2.0))
    _write_stock_basic([(f"LL{i:04d}.SZ", "半导体") for i in range(5)])
    th.rank_themes("20260807")

    got = th.get_stock_theme_strength("LL0000.SZ", "20260807")
    assert got is not None
    assert got["kind"] == "industry"
    assert got["theme"] == "半导体"


def test_get_stock_theme_strength_returns_none_when_unknown(temp_db):
    _write_klines(_make_group("MM", 5, 2.0))
    th.rank_themes("20260807")
    assert th.get_stock_theme_strength("999999.SZ", "20260807") is None


def test_get_stock_theme_strength_picks_strongest_theme(temp_db):
    """一票多主线时取最强的那条。"""
    _write_klines(_make_group("NN", 5, 5.0, limit_days=3))  # 强
    _write_klines(_make_group("OO", 5, -3.0, start=100))  # 弱
    for k in range(10):
        _write_klines(_make_group("PP", 10, 0.5, start=1000 + k * 10))
        _write_stock_basic([(f"PP{1000 + k * 10 + i:04d}.SZ", f"行业{k}") for i in range(10)])

    shared = "NN0000.SZ"
    th.import_members([{"theme": "强", "ts_code": f"NN{i:04d}.SZ"} for i in range(5)])
    th.import_members(
        [{"theme": "弱", "ts_code": f"OO{100 + i:04d}.SZ"} for i in range(5)] + [{"theme": "弱", "ts_code": shared}]
    )
    th.rank_themes("20260807")

    got = th.get_stock_theme_strength(shared, "20260807")
    assert got["theme"] == "强"


# ==================== 渲染 ====================


def test_format_theme_ranking_hints_when_no_theme_imported(temp_db):
    _write_klines(_make_group("QQ", 5, 1.0))
    _write_stock_basic([(f"QQ{i:04d}.SZ", "某行业") for i in range(5)])
    text = th.format_theme_ranking(th.rank_themes("20260807", persist=False))
    assert "尚未导入任何主线成员" in text


def test_format_theme_ranking_lists_themes(temp_db):
    _write_klines(_make_group("RR", 5, 3.0))
    th.import_members([{"theme": "算力硬件", "ts_code": f"RR{i:04d}.SZ"} for i in range(5)])
    text = th.format_theme_ranking(th.rank_themes("20260807", persist=False))
    assert "算力硬件" in text
    assert "用户定义主线" in text


def test_get_stock_theme_strength_falls_back_to_recent_snapshot(temp_db):
    """个股数据日落后于主线排名日时，用最近一期快照而不是静默返回 None。

    静默返回 None 比用一期稍旧的强度更糟：主线分被清零后看起来像
    "这条主线中性"，而不是"没查到"。
    """
    _write_klines(_make_group("SS", 5, 3.0))
    th.import_members([{"theme": "T", "ts_code": f"SS{i:04d}.SZ"} for i in range(5)])
    th.rank_themes("20260807")

    got = th.get_stock_theme_strength("SS0000.SZ", "20260810")
    assert got is not None
    assert got["snapshot_date"] == "20260807"


def test_get_stock_theme_strength_ignores_future_snapshots(temp_db):
    """只能用不晚于目标日的快照——不能拿未来的主线强度做历史决策。"""
    _write_klines(_make_group("TT", 5, 3.0))
    th.import_members([{"theme": "T", "ts_code": f"TT{i:04d}.SZ"} for i in range(5)])
    th.rank_themes("20260807")

    assert th.get_stock_theme_strength("TT0000.SZ", "20260801") is None


def test_dropped_themes_are_reported_not_silent(temp_db):
    """成员太少的主线被丢弃时必须报出来——静默截断会让用户以为它在参与排名。"""
    _write_klines(_make_group("UU", 2, 5.0))
    _write_klines(_make_group("VV", 5, 1.0, start=100))
    th.import_members([{"theme": "太小", "ts_code": f"UU{i:04d}.SZ"} for i in range(2)])
    th.import_members([{"theme": "够大", "ts_code": f"VV{100 + i:04d}.SZ"} for i in range(5)])

    res = th.rank_themes("20260807", persist=False)
    assert [g.name for g in res["themes"]] == ["够大"]
    assert len(res["dropped_themes"]) == 1
    assert "太小" in res["dropped_themes"][0]
    assert "太小" in th.format_theme_ranking(res)


def test_dropped_themes_counts_only_members_with_kline(temp_db):
    """成员数按"有行情数据的"算：外部判定器给的退市票不该被计入。"""
    _write_klines(_make_group("WW", 2, 1.0))
    members = [{"theme": "T", "ts_code": f"WW{i:04d}.SZ"} for i in range(2)]
    members += [{"theme": "T", "ts_code": f"9999{i}.SZ"} for i in range(5)]
    th.import_members(members)

    res = th.rank_themes("20260807", persist=False)
    assert res["themes"] == []
    assert "有行情的成员 2 只" in res["dropped_themes"][0]
