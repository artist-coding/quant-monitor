"""活跃市值多空区间（modules/amv.py）测试。

区间规则是选股的总开关，判错一天就会在该做的时候不做、不该做的时候做，
所以这里把规则的每个边界都单独钉住，另加一条对官方标注的全量回归。
"""

from __future__ import annotations

import pathlib

import pytest

from modules import amv
from modules.amv import BEAR_THRESHOLD, BULL_THRESHOLD, REGIME_BEAR, REGIME_BULL, classify
from modules.database import get_connection


# ==================== 状态机（纯函数，不碰库）====================


def test_single_day_gain_enters_bull():
    assert classify([None, 4.0])[-1] == REGIME_BULL
    assert classify([None, 5.5])[-1] == REGIME_BULL


def test_gain_just_below_threshold_does_not_enter_bull():
    """4% 是含等号的下限，3.99% 不触发。"""
    assert classify([None, 3.99])[-1] == ""


def test_two_day_cumulative_enters_bull():
    """「连续 1 天或两天」是累计口径：单日不够，两日加起来够也算。"""
    assert classify([None, 2.5, 1.6])[-1] == REGIME_BULL  # 累计 4.1
    assert classify([None, 2.0, 1.5])[-1] == ""  # 累计 3.5，不够


def test_two_day_window_does_not_extend_to_three():
    """只看相邻两日，不能三天慢慢累积。"""
    assert classify([None, 1.5, 1.5, 1.5])[-1] == ""


def test_single_day_drop_enters_bear():
    assert classify([None, 5.0, -2.31])[-1] == REGIME_BEAR


def test_drop_exactly_at_threshold_does_not_enter_bear():
    """规则是「跌幅**超过** 2.3%」——-2.3% 整不触发。

    这不是抠字眼：官方标注里 1993-10-11 与 2016-09-26 的涨幅恰好是
    -2.295%/-2.297%（显示都是 -2.30%），标注保持多头。
    """
    assert classify([None, 5.0, BEAR_THRESHOLD])[-1] == REGIME_BULL


def test_bear_takes_precedence_over_two_day_cumulative():
    """单日暴跌压过两日累计涨幅——顺序反了会有 18 天判错。

    原型：1993-03-08 涨 10.77% 进多头，次日跌 5.44%，两日累计仍有 +5.33%，
    官方标注是空头。
    """
    assert classify([None, 10.77, -5.44])[-1] == REGIME_BEAR


def test_regime_persists_until_opposite_trigger():
    """既不触发多头也不触发空头时沿用前一日。"""
    seq = classify([None, 6.0, 0.5, -1.0, 1.2, -2.0])
    assert seq[-1] == REGIME_BULL
    seq = classify([None, -3.0, 0.5, 1.0, -1.5])
    assert seq[-1] == REGIME_BEAR


def test_state_is_undefined_before_first_trigger():
    assert classify([None, 0.5, -1.0, 1.2]) == ["", "", "", ""]


def test_initial_state_can_be_supplied():
    assert classify([0.5, -1.0], initial=REGIME_BULL) == [REGIME_BULL, REGIME_BULL]


# ==================== 库操作 ====================


def _write(rows):
    """rows: (trade_date, close)"""
    from modules.database import get_connection

    with get_connection() as conn:
        conn.executemany("INSERT OR REPLACE INTO amv_daily (trade_date, close) VALUES (?, ?)", rows)
    amv.recompute_regimes()


def test_recompute_derives_pct_from_close(temp_db):
    _write([("20260801", 100.0), ("20260804", 105.0)])
    day = amv.get_day("20260804")
    assert day.pct_chg == pytest.approx(5.0)
    assert day.regime == REGIME_BULL


def test_pct_precision_matters_at_the_bear_boundary(temp_db):
    """-2.295% 与 -2.303% 显示都是 -2.30%，但区间结论相反。

    这就是为什么必须存收盘价、由它现算涨幅，而不能存四舍五入过的涨幅。
    """
    _write([("20260801", 100.0), ("20260804", 106.0), ("20260805", 106.0 * (1 - 0.02295))])
    assert amv.get_day("20260805").regime == REGIME_BULL

    _write([("20260806", 106.0 * (1 - 0.02295) * (1 - 0.02303))])
    assert amv.get_day("20260806").regime == REGIME_BEAR


def test_add_daily_with_close(temp_db):
    _write([("20260801", 100.0)])
    day = amv.add_daily("2026-08-04", close=106.0)
    assert day.trade_date == "20260804"
    assert day.regime == REGIME_BULL
    assert day.can_select is True


def test_add_daily_with_pct_backs_out_close(temp_db):
    _write([("20260801", 100.0)])
    day = amv.add_daily("20260804", pct_chg=6.0)
    assert day.close == pytest.approx(106.0)
    assert day.regime == REGIME_BULL


def test_add_daily_with_pct_needs_a_previous_close(temp_db):
    with pytest.raises(ValueError, match="无法反推收盘价"):
        amv.add_daily("20260804", pct_chg=6.0)


def test_add_daily_requires_a_value(temp_db):
    with pytest.raises(ValueError):
        amv.add_daily("20260804")


def test_add_daily_is_idempotent(temp_db):
    _write([("20260801", 100.0)])
    amv.add_daily("20260804", close=106.0)
    amv.add_daily("20260804", close=107.0)
    from modules.database import get_connection

    with get_connection() as conn:
        rows = conn.execute("SELECT close FROM amv_daily WHERE trade_date = '20260804'").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == pytest.approx(107.0)


def test_date_formats_are_normalised(temp_db):
    _write([("20260801", 100.0)])
    amv.add_daily("2026/08/04", close=106.0)
    assert amv.get_day("20260804") is not None


def test_get_regime_falls_back_to_most_recent(temp_db):
    """活跃市值由人工录入，可能比行情库晚——向前回退而不是返回空。"""
    _write([("20260801", 100.0), ("20260804", 106.0)])
    day = amv.get_regime("20260820")
    assert day.trade_date == "20260804"
    assert day.can_select is True


def test_get_regime_returns_none_before_all_data(temp_db):
    _write([("20260801", 100.0), ("20260804", 106.0)])
    assert amv.get_regime("20260101") is None


def test_bear_regime_blocks_selection(temp_db):
    _write([("20260801", 100.0), ("20260804", 106.0), ("20260805", 106.0 * 0.96)])
    day = amv.get_regime("20260805")
    assert day.regime == REGIME_BEAR
    assert day.can_select is False


def test_regime_segments(temp_db):
    _write([("20260801", 100.0), ("20260804", 106.0), ("20260805", 106.5), ("20260806", 100.0)])
    segs = amv.regime_segments()
    assert [s["regime"] for s in segs] == [REGIME_BULL, REGIME_BEAR]
    assert segs[0]["days"] == 2
    assert segs[0]["start"] == "20260804" and segs[0]["end"] == "20260805"


def test_import_history_round_trip(temp_db, tmp_path):
    csv_path = tmp_path / "amv.csv"
    csv_path.write_text(
        "date,open,high,low,close,volume,amount,涨幅,振幅,区间\n"
        "2026-08-01,100,101,99,100.00,1,1,,,\n"
        "2026-08-04,105,107,104,106.00,1,1,6.00%,3.00%,多头区间\n"
        "2026-08-05,106,106,101,101.76,1,1,-4.00%,4.00%,空头区间\n",
        encoding="utf-8-sig",
    )
    res = amv.import_history(csv_path)
    assert res["imported"] == 3
    assert res["start"] == "20260801" and res["end"] == "20260805"

    v = amv.verify_against_imported()
    assert v["total"] == 2
    assert v["mismatches"] == []
    assert v["accuracy"] == 100.0


def test_import_rejects_empty_file(temp_db, tmp_path):
    p = tmp_path / "empty.csv"
    p.write_text("date,close\n", encoding="utf-8")
    with pytest.raises(ValueError, match="没有解析出任何有效行情行"):
        amv.import_history(p)


def test_import_missing_file(temp_db):
    with pytest.raises(FileNotFoundError):
        amv.import_history("/nonexistent/amv.csv")


# ==================== 对官方标注的全量回归 ====================

_REAL = pathlib.Path(__file__).parent.parent / "data" / "amv" / "0AMV-260807-增强.csv"


@pytest.mark.skipif(not _REAL.exists(), reason="本地没有活跃市值原始数据（data/amv/ 不入库）")
def test_matches_official_labels_exactly(temp_db):
    """1993-01-04 ~ 2026-08-07 共 8180 个有标注交易日，必须逐日 100% 吻合。

    规则是用户给的，这条测试锁住我的实现没有跑偏。任何一处改动
    （阈值含不含等号、空头是否优先、涨幅用不用收盘价现算）都会让它变红。
    """
    amv.import_history(_REAL)
    v = amv.verify_against_imported()
    assert v["total"] > 8000, f"标注样本只有 {v['total']} 条，数据可能不完整"
    assert v["mismatches"] == [], f"有 {len(v['mismatches'])} 天判错，前 3 条: {v['mismatches'][:3]}"
    assert v["accuracy"] == 100.0


def test_format_status_reports_gate(temp_db):
    _write([("20260801", 100.0), ("20260804", 106.0)])
    text = amv.format_amv_status(amv.get_regime(), amv.regime_segments())
    assert "多头区间" in text and "可选股" in text
    assert str(BULL_THRESHOLD) in text


def test_format_status_when_empty(temp_db):
    assert "活跃市值库为空" in amv.format_amv_status(None)


# ==================== 容器格式（csv / xlsx / zip）====================
#
# 活跃市值的原始表由用户从行情终端导出后放百度网盘，格式不受我们控制。
# 这几条钉住的是"换一种导出方式也别静默少导数据"——少导的表现是
# 区间往前回退，看日志一切正常。

_ROWS = [
    ("2026-08-01", 100.0, ""),
    ("2026-08-04", 106.0, "多头区间"),
    ("2026-08-05", 101.76, "空头区间"),
]


def _csv_text(header: str = "date,close,区间") -> str:
    body = "\n".join(f"{d},{c},{r}" for d, c, r in _ROWS)
    return f"{header}\n{body}\n"


def test_import_accepts_chinese_headers(temp_db, tmp_path):
    """列名是中文的导出同样要认，否则换个导出器就一行都进不来。"""
    p = tmp_path / "cn.csv"
    p.write_text(_csv_text("日期,收盘,区间"), encoding="utf-8-sig")
    res = amv.import_history(p)
    assert res["imported"] == 3
    assert amv.get_regime().regime == REGIME_BEAR


def test_import_accepts_gbk_csv(temp_db, tmp_path):
    """GBK 编码的 CSV。

    原来固定按 utf-8-sig + errors='replace' 解，GBK 文件不会报错，
    而是表头变成乱码 → 一列都匹配不上 → 报"没有解析出任何有效行情行"，
    错误信息指向文件为空，实际是编码猜错。
    """
    p = tmp_path / "gbk.csv"
    p.write_bytes(_csv_text("日期,收盘,区间").encode("gbk"))
    res = amv.import_history(p)
    assert res["imported"] == 3


def test_import_xlsx_with_datetime_cells(temp_db, tmp_path):
    """xlsx 的日期列 openpyxl 会还原成 datetime 对象。

    str(datetime) 是 '2026-08-01 00:00:00'，_norm_date 会数出 14 位数字
    然后抛「无法解析日期」——错误信息指向格式非法，实际是类型没转。
    """
    openpyxl = pytest.importorskip("openpyxl")
    import datetime as _dt

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["日期", "收盘", "区间"])
    for d, c, r in _ROWS:
        y, m, day = (int(x) for x in d.split("-"))
        ws.append([_dt.datetime(y, m, day), c, r])
    p = tmp_path / "amv.xlsx"
    wb.save(p)

    res = amv.import_history(p)
    assert res["imported"] == 3
    assert res["start"] == "20260801" and res["end"] == "20260805"
    assert amv.verify_against_imported()["mismatches"] == []


def test_import_zip_with_gbk_member_names(temp_db, tmp_path):
    """zip 里多份 CSV 拼起来，且成员名是 GBK、没置 UTF-8 标志位。

    Windows/网盘打的包就是这样。zipfile 会按 cp437 把名字解成乱码，
    乱码不影响解压内容，但会让「挑出 .csv」这步失灵——
    包里明明有表格，却报「没有表格文件」。
    """
    import zipfile

    p = tmp_path / "amv.zip"
    with zipfile.ZipFile(p, "w") as zf:
        for name, rows in (("活跃市值_上.csv", _ROWS[:1]), ("活跃市值_下.csv", _ROWS[1:])):
            info = zipfile.ZipInfo(name.encode("gbk").decode("cp437"))
            info.flag_bits &= ~0x800
            body = "date,close,区间\n" + "\n".join(f"{d},{c},{r}" for d, c, r in rows) + "\n"
            zf.writestr(info, body.encode("utf-8-sig"))

    res = amv.import_history(p)
    assert res["imported"] == 3
    # 两份文件拼起来顺序不保证，start/end 依赖排序
    assert res["start"] == "20260801" and res["end"] == "20260805"


def test_import_is_idempotent(temp_db, tmp_path):
    """整表 upsert：每天下全量表重复导入不能翻倍。"""
    p = tmp_path / "a.csv"
    p.write_text(_csv_text(), encoding="utf-8-sig")
    amv.import_history(p)
    amv.import_history(p)
    assert len(amv.recent(100)) == 3


def test_import_skips_rows_without_close(temp_db, tmp_path):
    """只有日期没有收盘价的补录占位行要跳过，并且**计数**。

    区间判定的唯一可信来源是收盘价，占位行进库会拖出一个假涨幅。
    """
    p = tmp_path / "a.csv"
    p.write_text("date,close\n2026-08-01,100\n2026-08-02,\n2026-08-03,\n", encoding="utf-8-sig")
    res = amv.import_history(p)
    assert res["imported"] == 1
    assert res["skipped"] == 2


def test_dry_run_does_not_write(temp_db, tmp_path):
    p = tmp_path / "a.csv"
    p.write_text(_csv_text(), encoding="utf-8-sig")
    res = amv.import_history(p, dry_run=True)
    assert res["imported"] == 3 and res["dry_run"] is True
    assert amv.get_regime() is None, "dry_run 落库了"


def test_unparseable_error_names_the_headers(temp_db, tmp_path):
    """列名对不上时，错误信息要把读到的表头说出来。

    只说「没有有效行情行」会让人去查文件是不是空的，
    实际上多半是导出器换了列名。
    """
    p = tmp_path / "a.csv"
    p.write_text("时刻,点位\n2026-08-01,100\n", encoding="utf-8-sig")
    with pytest.raises(ValueError, match="时刻"):
        amv.import_history(p)


def test_old_xls_gives_actionable_error(temp_db, tmp_path):
    p = tmp_path / "a.xls"
    p.write_bytes(b"\xd0\xcf\x11\xe0")  # OLE2 magic
    with pytest.raises(ValueError, match="convert-to xlsx"):
        amv.import_history(p)


def test_daily_file_does_not_wipe_official_labels(temp_db, tmp_path):
    """每日文件没有「区间」列时，不能清掉历史表导进来的官方标注。

    实测的日更文件只有 date,open,high,low,close,volume,amount 七列。
    直接 `regime_imported = excluded.regime_imported` 会把 8180 行标注
    一次性覆盖成空串——那是校验区间规则的唯一地面真值，清掉之后
    `zt amv verify` 永远返回 0/0，而且不报任何错。
    """
    enhanced = tmp_path / "enhanced.csv"
    enhanced.write_text(
        "date,open,close,区间\n2026-08-01,99,100.00,\n2026-08-04,105,106.00,多头区间\n",
        encoding="utf-8-sig",
    )
    amv.import_history(enhanced)
    assert amv.verify_against_imported()["total"] == 1

    # 日更文件：同样两天，但没有区间列，且这次连 open 都不给
    daily = tmp_path / "daily.csv"
    daily.write_text("date,close\n2026-08-01,100.00\n2026-08-04,106.00\n", encoding="utf-8-sig")
    amv.import_history(daily)

    v = amv.verify_against_imported()
    assert v["total"] == 1, "官方标注被日更文件清掉了"
    assert v["mismatches"] == []
    with get_connection() as conn:
        assert conn.execute("SELECT open FROM amv_daily WHERE trade_date='20260804'").fetchone()[0] == 105
