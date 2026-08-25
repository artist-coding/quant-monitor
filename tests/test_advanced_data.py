"""高级数据层（modules/advanced_data.py）测试。

这一层的数据来自 akshare 封装的东财 / 同花顺 / 财联社**公开网页接口**：上游会改函数名、
会改列名、会封 IP，非交易日返回空表更是家常便饭。所以这里的测试重点**不是"能不能取到
数据"**——那要联网，归 ``selfcheck(probe=True)`` 管，也不该由 CI 来判死活；测试要钉住的是
**"取不到的时候会发生什么"**：

- 上游删了函数 / 改了列名 / 抛了异常 / 返回了空表，这四件事的处置各不相同，
  混成一种（比如一律返回 None）就会让调用方分不清"这次没数据是正常的"和"这次出故障了"；
- 参数名打错必须在**发请求之前**就失败，别拿 IP 去换一条 TypeError；
- 缓存往返不能悄悄改数据——``'000001'`` 被 read_json 推断成 int 1 是不报错的，
  深市代码前导零一丢，下游按代码匹配全部落空，而且查起来毫无线索。

因此本文件全程不联网：碰真 akshare 的两条只做 hasattr 和 ``inspect.signature``，不调用任何
函数；其余用例一律把假 akshare 塞进 ``sys.modules``（fetch 里是延迟 import，替换 sys.modules
就能整个换掉真模块）。凡是"必须走缓存 / 必须先失败"的用例，都用**一碰就炸的假 akshare**
来证明它确实没走网络路径，而不是只看返回值——返回值对了也可能是碰巧对的。
跨进程限流那条会 ``subprocess`` 起几个 python3，但它们同样只碰临时目录里的 sqlite。

断言的粒度是拿**变异测试**校准的：每一条都对着"把对应实现改坏"验证过会变红。
所以这里会出现一些看着啰嗦的断言——比如日期列往返要一路查到 ``.equals()`` 和逐列 dtype、
限流要真的量一次时间、线程串味要用事件把交错钉死而不是抢时间片（抢时间片实测跑 300 轮
一次都没串到，那种写法等于没写）。粗一格的断言实测都抓不住对应的缺陷。
"""

from __future__ import annotations

import datetime
import importlib
import inspect
import json
import math
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from modules import advanced_data
from modules.advanced_data import (
    BY_KEY,
    CATEGORIES,
    CATEGORY_SENTIMENT,
    DEFAULT_MIN_INTERVAL,
    ENV_CACHE_PATH,
    ENV_MIN_INTERVAL,
    INTERFACES,
    MAX_MIN_INTERVAL,
    SOURCE_EM,
    SOURCE_THS,
    AdvancedDataSource,
    catalog,
    get_advanced_source,
    reset_advanced_source,
    selfcheck,
)


# ==================== 测试脚手架 ====================


@pytest.fixture(autouse=True)
def advanced_env(tmp_path, monkeypatch):
    """每个用例一个独立缓存库、关掉限流、清干净单例状态。

    缓存路径走 ADVANCED_CACHE_PATH（它优先于 DATA_DIR），所以不会和 conftest.py 里
    那个把 DATA_DIR/DB_PATH 指向临时目录的 autouse fixture 打架。
    """
    monkeypatch.setenv(ENV_CACHE_PATH, str(tmp_path / "advanced_cache.db"))
    monkeypatch.setenv(ENV_MIN_INTERVAL, "0")  # 限流 sleep 在测试里纯属浪费时间
    reset_advanced_source()
    yield
    reset_advanced_source()


class _FakeAk:
    """假 akshare 模块：按 akshare 的**函数名**挂桩函数。

    没挂上的名字就等价于"上游把这个函数删了"——``getattr(ak, func, None)`` 会拿到 None。
    """

    def __init__(self, **funcs: Callable[..., Any]) -> None:
        self.__dict__.update(funcs)


class _ExplodingAk:
    """一碰就炸的假 akshare：任何函数属性被取用，都说明代码走上了网络路径。

    ``getattr(ak, name, None)`` 只吞 AttributeError，吞不掉 AssertionError，所以这里抛
    AssertionError 能把"其实还是发了请求"这件事直接顶成用例失败，而不是被默认值掩盖过去。
    """

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") and name.endswith("__"):
            # 内省属性（__name__ / __spec__ 之类）照常按缺失处理，
            # 免得炸在无关的地方、把真正的失败点埋掉
            raise AttributeError(name)
        raise AssertionError(f"不该走到这里：代码取用了 akshare.{name}，说明它真的准备发网络请求了")


def _zt_pool_frame() -> pd.DataFrame:
    """一张形状照着真实涨停池捏的小表。

    两处刻意为之：``代码`` / ``首次封板时间`` 是带前导零的**字符串**，
    ``封板资金`` 是 **float64**——这三样正是缓存往返最容易被悄悄改掉的东西。
    """
    return pd.DataFrame(
        {
            "代码": ["000001", "600519"],
            "名称": ["平安银行", "贵州茅台"],
            "连板数": [1, 3],
            "封板资金": [123456789.0, 450000000.0],
            "首次封板时间": ["093005", "100112"],
        }
    )


def _date_typed_frame() -> pd.DataFrame:
    """一张把三种"日期类型"都摆齐的小表。

    akshare 返回的日期列有三种长相，缓存往返对它们的伤害各不相同：
    - ``datetime64[ns]`` 真列（lhb_detail.上榜日）：一旦退化成 object，下游 ``.dt`` 直接抛 AttributeError；
    - object 列里装 ``datetime.date``（fund_flow_stock.日期）：退化成字符串后
      ``df[df["日期"] == date(...)]`` 恒为 0 行，**不报错、结果直接是错的**；
    - object 列里装 ``datetime.time``（zt_pool.首次封板时间）：同上。
    """
    return pd.DataFrame(
        {
            "代码": ["000001", "600519"],
            "上榜日": pd.to_datetime(["2026-08-13", "2026-08-14"]),
            "日期": [datetime.date(2026, 8, 13), datetime.date(2026, 8, 14)],
            "首次封板时间": [datetime.time(9, 30, 5), datetime.time(10, 1, 12)],
            "封板资金": [123456789.0, 450000000.0],
        }
    )


def _one_row_frame(**params: Any) -> pd.DataFrame:
    """给假 akshare 当默认桩用的一行数据。"""
    return pd.DataFrame({"代码": ["000001"], "名称": ["平安银行"]})


def _fake_ak_with_every_function(**overrides: Callable[..., Any]) -> _FakeAk:
    """挂满注册表里全部函数名的假 akshare，用来**离线**跑 selfcheck。

    selfcheck 的第一件事是对 31 条做 hasattr；挂不全的话"函数已不存在"会被塞满，
    分不清是实现的问题还是桩没搭好。
    """
    funcs: dict[str, Any] = {"__version__": "0.0.0-fake"}
    for spec in INTERFACES:
        funcs[spec.func] = overrides.get(spec.func, _one_row_frame)
    return _FakeAk(**funcs)


def _age_cache_rows(seconds: float) -> None:
    """把缓存库里所有条目的写入时刻往前拨 ``seconds`` 秒。

    TTL 是 30 分钟起步、最长 30 天，真等是等不起的；改 ts 才能让"墙上时钟真的走过了 TTL"
    这条分支被执行到——monkeypatch 一个 ttl=0 的 Interface 只会命中 ``ttl <= 0`` 那道守卫，
    过期判断本身仍然一次都没跑过。
    """
    with sqlite3.connect(str(advanced_data._cache_path())) as conn:
        conn.execute("UPDATE advanced_cache SET ts = ts - ?", (float(seconds),))
        conn.commit()


def _read_rate_ts(source: str) -> float | None:
    """读限流台账里某个 source 的登记时刻；没有这张表或没这一行都返回 None。"""
    try:
        with sqlite3.connect(str(advanced_data._cache_path())) as conn:
            row = conn.execute("SELECT ts FROM advanced_rate WHERE source = ?", (source,)).fetchone()
    except sqlite3.Error:
        return None
    return float(row[0]) if row is not None else None


# ==================== 1-4：注册表本身 ====================


def test_registry_keys_are_unique():
    """key 撞了会让后一条静默覆盖前一条，注册表看着有 31 条、实际能取到的少一条。"""
    assert len(BY_KEY) == len(INTERFACES)
    assert len(INTERFACES) == 31, "契约冻结的是 31 条接口，增删都要先改契约"


def test_registry_functions_exist_in_akshare():
    """31 个 func 在本机装的 akshare 上必须逐个 hasattr——只查名字，绝不调用。

    这条是本文件里最重要的一条：akshare 改函数名是**静默**的，没有它，某天 pip 升级完，
    整条流水线开始一片 None，而没人知道是从哪一步断的。只做 hasattr 的另一个理由是
    绝不能联网——真调用会发请求、会被限流、会让 CI 看天吃饭。
    """
    ak = pytest.importorskip("akshare")
    missing = [(i.key, i.func) for i in INTERFACES if not hasattr(ak, i.func)]
    assert missing == [], (
        f"akshare {getattr(ak, '__version__', '?')} 上这些函数已经不存在了，"
        f"去改 INTERFACES 里对应的 func 字段（key 保持不动）: {missing}"
    )


def test_every_interface_is_well_formed():
    """每条登记信息的必填字段都得有值，且落在允许的取值里。"""
    for i in INTERFACES:
        assert i.key, "key 不能为空：它是对外唯一的稳定名"
        assert i.func, f"{i.key}: func 不能为空——它是指向上游的唯一锚点"
        assert i.category in CATEGORIES, f"{i.key}: category {i.category!r} 不在 CATEGORIES 里，catalog() 会多出一组"
        assert i.ttl > 0, f"{i.key}: ttl 必须为正（秒）——ttl<=0 会被缓存层当成永不命中，等于每次都打上游"
        assert i.source, f"{i.key}: source 不能为空——限流是按 source 分桶的"
        assert i.desc, f"{i.key}: desc 不能为空——CLI 和 MCP 直接拿它当接口说明"
        assert isinstance(i.params, dict), f"{i.key}: params 必须是 dict（参数名 -> 中文说明）"
        assert isinstance(i.expect, tuple), f"{i.key}: expect 必须是 tuple"


def test_catalog_covers_all_categories():
    """catalog() 六组齐全、顺序固定，且不多不少正好装下全部接口。"""
    cat = catalog()
    assert list(cat) == list(CATEGORIES), "六个分组必须齐全且顺序固定——顺序变了会让人以为分组改了"
    assert sum(len(v) for v in cat.values()) == len(INTERFACES), "有接口漏出去或被算了两遍"
    keys = [entry["key"] for group in cat.values() for entry in group]
    assert sorted(keys) == sorted(BY_KEY)
    sample = cat[CATEGORY_SENTIMENT][0]
    assert set(sample) == {"key", "func", "desc", "params", "expect", "ttl", "source"}


# ==================== 5-8：出错时的表现 ====================


def test_source_satisfies_protocol():
    """单例得满足 AdvancedDataSource 协议，且真的是同一个实例（限流状态靠它共享）。"""
    src = get_advanced_source()
    assert isinstance(src, AdvancedDataSource)
    assert src.name == "akshare_advanced"
    assert get_advanced_source() is src, "必须是进程内单例，否则各自一套限流器，等于没限流"


def test_unknown_key_returns_none_with_error():
    """key 打错是调用方的 bug，要返回 None 并把可用 key 列出来，而不是抛异常。"""
    src = get_advanced_source()
    assert src.fetch("no_such_key") is None
    err = src.last_error or ""
    assert "未知接口" in err
    assert "zt_pool" in err, "报错里要带上可用 key 清单，否则调用方没法自救"


def test_unknown_param_name_fails_before_any_network_call(monkeypatch):
    """参数名打错必须在**任何网络动作之前**就失败，别拿一次请求去换一条 TypeError。

    用一碰就炸的假 akshare 来证明：如果实现把白名单校验放到了取数之后，
    这里会炸在 AssertionError 上，而不是安安静静返回 None。
    """
    monkeypatch.setitem(sys.modules, "akshare", _ExplodingAk())
    src = get_advanced_source()

    assert src.fetch("zt_pool", {"dat": "20260814"}) is None
    err = src.last_error or ""
    assert "不接受参数" in err
    assert "'date'" in err, "必须把正确的参数名告诉调用方，只说'不接受'等于没说"
    assert src.warnings == []


def test_upstream_exception_returns_none_without_raising(monkeypatch):
    """上游抛异常（超时、被封、改了签名）→ 返回 None 且不往外抛，last_error 带异常类型名。

    类型名是分诊用的：Timeout 该重试，TypeError 得改代码，两者处置完全不同。
    """

    def _boom(**params: str) -> pd.DataFrame:
        raise RuntimeError("连接超时")

    monkeypatch.setitem(sys.modules, "akshare", _FakeAk(stock_zt_pool_em=_boom))
    src = get_advanced_source()

    out = src.fetch("zt_pool", {"date": "20260814"})  # 不抛异常本身就是断言的一部分
    assert out is None
    err = src.last_error or ""
    assert "RuntimeError" in err
    assert "连接超时" in err


# ==================== 9-13：缓存 ====================


def test_cache_round_trip_preserves_leading_zeros(monkeypatch):
    """深市代码 '000001' 写进缓存再读出来，必须还是这两个字符串。

    read_json 默认会做类型推断，'000001' 会被读成 int 1——前导零全丢，下游按代码匹配
    全部落空，而且一声不吭。所以这里走完整的「写缓存 -> 读缓存」链路，
    盯着字符串字面量和 dtype 看，而不是去对拍私有序列化函数的输入输出。
    """
    monkeypatch.setitem(sys.modules, "akshare", _FakeAk(stock_zt_pool_em=lambda **p: _zt_pool_frame()))
    src = get_advanced_source()
    params = {"date": "20260814"}

    first = src.fetch("zt_pool", params)
    assert first is not None
    assert list(first["代码"]) == ["000001", "600519"]

    # 换成一碰就炸的假模块：接下来这一次的数据只可能来自缓存
    monkeypatch.setitem(sys.modules, "akshare", _ExplodingAk())
    cached = src.fetch("zt_pool", params)

    assert cached is not None
    assert list(cached["代码"]) == ["000001", "600519"]
    assert cached["代码"].dtype == object
    assert all(isinstance(v, str) for v in cached["代码"]), "读回来必须还是 str，不能变成数字"
    # 首次封板时间同理：'093005' 一旦被推断成 93005，早盘和尾盘就分不出来了
    assert list(cached["首次封板时间"]) == ["093005", "100112"]


def test_cache_round_trip_preserves_float_dtype(monkeypatch):
    """封板资金是 float64，往返一圈不能变成 int——金额被截断是看不出来的。

    和上一条一样，必须真的走一遍写缓存再读缓存的全链路。
    """
    monkeypatch.setitem(sys.modules, "akshare", _FakeAk(stock_zt_pool_em=lambda **p: _zt_pool_frame()))
    src = get_advanced_source()
    params = {"date": "20260814"}

    first = src.fetch("zt_pool", params)
    assert first is not None
    assert str(first["封板资金"].dtype) == "float64"

    monkeypatch.setitem(sys.modules, "akshare", _ExplodingAk())
    cached = src.fetch("zt_pool", params)

    assert cached is not None
    assert str(cached["封板资金"].dtype) == "float64"
    assert cached["封板资金"].tolist() == [123456789.0, 450000000.0]
    # 整数列也别被搞成 float：连板数 3.0 打出来很难看，比较时还会出浮点意外
    assert str(cached["连板数"].dtype) == "int64"
    assert cached["连板数"].tolist() == [1, 3]


def test_expired_ttl_misses_cache(monkeypatch):
    """TTL 到期就得当成未命中，重新向上游取。

    用 dataclasses.replace 造一条 ttl=0 的 Interface 塞进 BY_KEY，
    靠 monkeypatch.setitem 保证用例结束后还原，不污染同进程里的其它用例。
    """
    calls: list[dict[str, str]] = []

    def _hot(**params: str) -> pd.DataFrame:
        calls.append(dict(params))
        return pd.DataFrame({"代码": ["000001"], "排名": [len(calls)]})

    monkeypatch.setitem(sys.modules, "akshare", _FakeAk(stock_hot_rank_em=_hot))
    src = get_advanced_source()

    assert src.fetch("hot_rank") is not None
    assert len(calls) == 1
    assert src.fetch("hot_rank") is not None
    assert len(calls) == 1, "TTL 还没到，第二次应当命中缓存"

    monkeypatch.setitem(advanced_data.BY_KEY, "hot_rank", replace(BY_KEY["hot_rank"], ttl=0))
    again = src.fetch("hot_rank")

    assert again is not None
    assert len(calls) == 2, "ttl 已过期，必须视为未命中并重新取"
    assert again["排名"].tolist() == [2], "拿到的应该是新数据，不是缓存里那份"


def test_cache_key_is_param_sensitive(monkeypatch):
    """缓存键要带上参数：换一个 date 就是另一份数据，不能拿昨天的涨停池冒充今天的。"""
    calls: list[dict[str, str]] = []

    def _pool(**params: str) -> pd.DataFrame:
        calls.append(dict(params))
        return _zt_pool_frame()

    monkeypatch.setitem(sys.modules, "akshare", _FakeAk(stock_zt_pool_em=_pool))
    src = get_advanced_source()

    assert src.fetch("zt_pool", {"date": "20260814"}) is not None
    assert src.fetch("zt_pool", {"date": "20260814"}) is not None
    assert len(calls) == 1, "同样的参数，第二次应当命中缓存"

    assert src.fetch("zt_pool", {"date": "20260813"}) is not None
    assert len(calls) == 2, "换了 date 就该是另一个缓存键"
    assert [c["date"] for c in calls] == ["20260814", "20260813"]


def test_cache_hit_never_touches_network(monkeypatch):
    """缓存命中要在 import akshare 之前就返回：预置缓存后，哪怕 akshare 是个一碰就炸的假模块也照样成功。

    只断言"返回值对"是不够的——返回值对也可能是它又发了一次请求刚好拿到一样的数据。
    这里让假模块在被取用的瞬间抛 AssertionError，用例能通过就说明那条路根本没走。
    """
    monkeypatch.setitem(sys.modules, "akshare", _FakeAk(stock_zt_pool_em=lambda **p: _zt_pool_frame()))
    src = get_advanced_source()
    assert src.fetch("zt_pool", {"date": "20260814"}) is not None

    monkeypatch.setitem(sys.modules, "akshare", _ExplodingAk())
    out = src.fetch("zt_pool", {"date": "20260814"})

    assert out is not None
    assert len(out) == 2
    assert src.last_error is None
    assert src.warnings == []


# ==================== 14-16：上游变脸 ====================


def test_renamed_columns_still_return_data_with_warning(monkeypatch):
    """上游改了列名 → **照常返回数据** + 把缺列记进 warnings。

    绝不能因为缺一列就返回 None：剩下的列多半还有用，扔掉才是真损失；
    更要命的是 None 会静默流进选股逻辑，变成"今天没有涨停股"这种假结论。
    """
    renamed = pd.DataFrame({"股票代码": ["000001"], "股票名称": ["平安银行"]})
    monkeypatch.setitem(sys.modules, "akshare", _FakeAk(stock_zt_pool_em=lambda **p: renamed))
    src = get_advanced_source()

    out = src.fetch("zt_pool", {"date": "20260814"})

    assert out is not None, "缺列绝不能返回 None"
    assert len(out) == 1
    assert list(out.columns) == ["股票代码", "股票名称"]
    assert src.last_error is None, "改列名不是错误，是告警"
    warned = src.warnings
    assert any("期望列缺失" in w for w in warned)
    assert any("连板数" in w for w in warned), "告警里要点名缺了哪几列，否则没法排查"
    assert any("股票代码" in w for w in warned), "告警里也要给出实际列名，才知道上游改成了什么"


def test_empty_frame_is_not_an_error(monkeypatch):
    """上游返回空表不算错：非交易日、该条件下确实没数据，都会走到这里。

    顺带钉住"空表不写缓存"——否则一次瞬时故障会被固化 12 小时，当天再也取不到真数据。
    """
    calls: list[dict[str, str]] = []

    def _pool(**params: str) -> pd.DataFrame:
        calls.append(dict(params))
        return pd.DataFrame()

    monkeypatch.setitem(sys.modules, "akshare", _FakeAk(stock_zt_pool_em=_pool))
    src = get_advanced_source()

    out = src.fetch("zt_pool", {"date": "20260815"})

    assert out is not None, "空表也是数据，不是故障"
    assert out.empty
    assert src.last_error is None
    assert any("空表" in w for w in src.warnings)

    assert src.fetch("zt_pool", {"date": "20260815"}) is not None
    assert len(calls) == 2, "空表不该写进缓存，否则一次瞬时故障会被固化到 TTL 结束"


def test_missing_upstream_function_points_at_interfaces(monkeypatch):
    """上游把函数删了/改了名 → last_error 必须点名 INTERFACES，直接告诉人去哪儿改。

    这是唯一一种"改代码才能修"的失败，报错里不写清楚落点，排查成本会高得离谱。
    """
    monkeypatch.setitem(sys.modules, "akshare", _FakeAk())  # 一个函数都没挂
    src = get_advanced_source()

    assert src.fetch("zt_pool", {"date": "20260814"}) is None
    err = src.last_error or ""
    assert "INTERFACES" in err, "报错要指明改哪儿：INTERFACES 里那条的 func 字段"
    assert "stock_zt_pool_em" in err, "还要说清楚是哪个函数没了"


# ==================== 17-19：缓存的三条"静默腐蚀"路径 ====================


def test_cache_entry_really_expires_when_wall_clock_passes_ttl(monkeypatch):
    """墙上时钟真的走过 TTL 之后，缓存必须失效并重新向上游取。

    第 11 条用的是 ttl=0，只碰得到 ``ttl <= 0`` 那道守卫；``time.time() - ts > ttl``
    这条真正的过期判断在整个测试文件里一次都没被执行过——把它整段删掉，16 条照样全绿。
    这里改的是缓存行里的写入时刻（TTL 最短 30 分钟、最长 30 天，真等等不起），
    先验证"还没到期仍然命中"，再验证"过期后重新取"，两头都钉住。
    """
    calls: list[dict[str, str]] = []

    def _hot(**params: str) -> pd.DataFrame:
        calls.append(dict(params))
        return pd.DataFrame({"代码": ["000001"], "排名": [len(calls)]})

    monkeypatch.setitem(sys.modules, "akshare", _FakeAk(stock_hot_rank_em=_hot))
    src = get_advanced_source()
    ttl = BY_KEY["hot_rank"].ttl

    assert src.fetch("hot_rank") is not None
    assert len(calls) == 1

    # 拨到"还差一点点才到期"：这一次必须仍然命中缓存
    _age_cache_rows(ttl - 5)
    assert src.fetch("hot_rank") is not None
    assert len(calls) == 1, "还没到 TTL 就重新取，等于缓存白做，上游要被打成筛子"

    # 再拨过头：现在 time.time() - ts 已经大于 ttl
    _age_cache_rows(10)
    again = src.fetch("hot_rank")

    assert again is not None
    assert len(calls) == 2, "TTL 已经过去了，必须当成未命中重新取——否则一份数据会被用到天荒地老"
    assert again["排名"].tolist() == [2], "拿到的应该是新数据，不是缓存里那份"


def test_column_names_survive_the_cache_round_trip(monkeypatch):
    """列名往返一圈必须逐字符不变。

    ``dtype=False`` 只关了**数据**的类型推断，``convert_axes`` 还开着，**列名**照样被改：
    重复列名会被加 ``.1`` 后缀，数字样/日期样的列名会被转成 int / Timestamp。
    当前 31 条接口的列名都是硬编码中文，所以现在不会真害到人——但上游哪天改一次就静默发作，
    而且症状是"某一列凭空消失"，查起来毫无线索。
    """
    weird = pd.DataFrame(
        [["a", "b", "c", "d"], ["e", "f", "g", "h"]],
        columns=["000001", "2026-08-14", "代码", "代码"],
    )
    expected = ["000001", "2026-08-14", "代码", "代码"]
    monkeypatch.setitem(sys.modules, "akshare", _FakeAk(stock_hot_rank_em=lambda **p: weird))
    src = get_advanced_source()

    fresh = src.fetch("hot_rank")
    assert fresh is not None
    assert list(fresh.columns) == expected

    monkeypatch.setitem(sys.modules, "akshare", _ExplodingAk())
    cached = src.fetch("hot_rank")

    assert cached is not None
    assert list(cached.columns) == expected, "重复列名被加了 '.1' 后缀，或者列名被推断成了数字/日期"
    assert [type(c) for c in cached.columns] == [str, str, str, str], "列名必须还是 str，不能变成 int / Timestamp"


def test_cache_round_trip_preserves_date_and_time_columns(monkeypatch):
    """日期/时间列走一趟缓存，必须和走网络拿到的**完全是同一个东西**。

    这是这一层最会伤人的坑：``date_format="iso"`` 只作用于 datetime64 dtype，
    akshare 大量返回的是 object 列里装 ``datetime.date`` / ``datetime.time``，
    读回来就变成字符串，一去不回。下游 ``df[df["日期"] == date(...)]`` 首次 1 行、
    命中缓存 0 行——**无异常、无告警、结果直接是错的**。所以断言下到 ``.equals()`` 和
    逐列 dtype 这一级：只看"有没有这一列"是拦不住的。
    """
    frame = _date_typed_frame()
    monkeypatch.setitem(sys.modules, "akshare", _FakeAk(stock_hot_rank_em=lambda **p: frame.copy()))
    src = get_advanced_source()

    fresh = src.fetch("hot_rank")
    assert fresh is not None

    monkeypatch.setitem(sys.modules, "akshare", _ExplodingAk())
    cached = src.fetch("hot_rank")

    assert cached is not None
    assert cached.dtypes.to_dict() == fresh.dtypes.to_dict(), "缓存往返改了 dtype：走网络和走缓存返回的不是一个东西"
    assert cached.equals(fresh), "缓存往返改了数据本身"
    # 再逐类点名，免得哪天 equals 因为别的原因恰好成立
    assert str(cached["上榜日"].dtype).startswith("datetime64"), (
        "datetime64 列退化成 object 后，下游 .dt 会直接抛 AttributeError"
    )
    assert cached["日期"].tolist() == [datetime.date(2026, 8, 13), datetime.date(2026, 8, 14)]
    # 必须是**光秃秃的 date**：Timestamp 也是 datetime.date 的子类，用 isinstance 会被它蒙混过去
    assert all(type(v) is datetime.date for v in cached["日期"])
    assert all(type(v) is datetime.time for v in cached["首次封板时间"])
    assert cached["首次封板时间"].tolist() == [datetime.time(9, 30, 5), datetime.time(10, 1, 12)]


# ==================== 20-22：绕过缓存与状态清空 ====================


def test_force_really_bypasses_the_cache(monkeypatch):
    """``force=True`` 必须真的绕开缓存去打上游。

    selfcheck 的探活正确性完全建立在这上面：探活问的是"**现在**还通不通"，
    命中缓存等于把 12 小时前的结论重报一遍，坏了也报绿。
    """
    calls: list[dict[str, str]] = []

    def _hot(**params: str) -> pd.DataFrame:
        calls.append(dict(params))
        return pd.DataFrame({"代码": ["000001"], "排名": [len(calls)]})

    monkeypatch.setitem(sys.modules, "akshare", _FakeAk(stock_hot_rank_em=_hot))
    src = get_advanced_source()

    assert src.fetch("hot_rank") is not None
    assert len(calls) == 1
    assert src.fetch("hot_rank") is not None
    assert len(calls) == 1, "没加 force，第二次本来就该命中缓存"

    forced = src.fetch("hot_rank", force=True)

    assert forced is not None
    assert len(calls) == 2, "force=True 却没打上游——探活等于在问缓存，坏了也报绿"
    assert forced["排名"].tolist() == [2], "force 拿回来的必须是新数据"


def test_state_is_reset_at_the_start_of_every_fetch(monkeypatch):
    """``last_error`` / ``warnings`` 讲的是"**刚才那次** fetch"，不能带着上一次的话。

    这两个字段是调用方分诊的唯一依据：上一次的 RuntimeError 留在这儿，
    这一次明明成功了也会被当成故障；上一次的缺列告警留在这儿，
    这一次干干净净的数据也会被当成残缺的。
    """
    renamed = pd.DataFrame({"股票代码": ["000001"], "股票名称": ["平安银行"]})
    monkeypatch.setitem(
        sys.modules,
        "akshare",
        _FakeAk(
            stock_zt_pool_em=lambda **p: _zt_pool_frame(),
            stock_zt_pool_dtgc_em=lambda **p: renamed,
        ),
    )
    src = get_advanced_source()

    # 先制造一次失败
    assert src.fetch("zt_pool", {"dat": "20260814"}) is None
    assert src.last_error is not None

    # 紧接着来一次成功的：错误必须被清掉
    assert src.fetch("zt_pool", {"date": "20260814"}) is not None
    assert src.last_error is None, "上一次的错误留到了这一次，成功的取数会被当成故障"

    # 再制造一次告警
    assert src.fetch("dt_pool", {"date": "20260814"}) is not None
    assert any("期望列缺失" in w for w in src.warnings)

    # 紧接着取一份完整数据：告警必须被清掉
    assert src.fetch("zt_pool", {"date": "20260815"}) is not None
    assert src.warnings == [], "上一次的告警留到了这一次，完整的数据会被当成残缺的"
    assert src.last_error is None


def test_warnings_property_returns_a_defensive_copy(monkeypatch):
    """``warnings`` 返回的是副本：调用方拿去 append / clear，不该动到内部状态。

    契约里写明了"返回副本"。直接把内部列表交出去，调用方一个 ``warnings.clear()``
    就能把这次取数的告警抹掉，而它自己完全意识不到。
    """
    renamed = pd.DataFrame({"股票代码": ["000001"]})
    monkeypatch.setitem(sys.modules, "akshare", _FakeAk(stock_zt_pool_em=lambda **p: renamed))
    src = get_advanced_source()

    assert src.fetch("zt_pool", {"date": "20260814"}) is not None
    grabbed = src.warnings
    assert len(grabbed) == 1

    grabbed.append("调用方自己塞的一条")
    grabbed.clear()

    assert len(src.warnings) == 1, "warnings 把内部列表直接交出去了，调用方能就地改掉本次取数的告警"
    assert any("期望列缺失" in w for w in src.warnings)


# ==================== 23-25：上游给了不是 DataFrame 的东西 / 缓存也要校验 ====================


@pytest.mark.parametrize(
    "bad",
    [
        {"代码": ["000001"]},  # 上游改成返回 dict
        "<html>403 Forbidden</html>",  # 被拦了，返回的是一页 HTML
        None,  # akshare 内部吞了异常，返回 None
        [{"代码": "000001"}],  # 返回 list[dict]
    ],
    ids=["dict", "str", "none", "list"],
)
def test_non_dataframe_upstream_is_an_error_not_a_crash(monkeypatch, bad):
    """上游返回的不是 DataFrame（改了返回类型 / 被拦下返回一页 HTML）→ 返回 None，不外抛。

    少了这道检查，紧接着的 ``df.empty`` 会抛 ``AttributeError: 'dict' object has no attribute 'empty'``
    给调用方——签名承诺的是"要么 DataFrame 要么 None"，抛异常等于把这层的失败模式漏给了上层。
    """
    monkeypatch.setitem(sys.modules, "akshare", _FakeAk(stock_zt_pool_em=lambda **p: bad))
    src = get_advanced_source()

    out = src.fetch("zt_pool", {"date": "20260814"})  # 不外抛本身就是断言的一部分

    assert out is None
    err = src.last_error or ""
    assert "非 DataFrame" in err
    assert type(bad).__name__ in err, "要说清楚上游到底给了个什么类型，否则没法分诊"


def test_cache_hit_still_reports_missing_columns(monkeypatch):
    """命中缓存也要做列校验——否则告警只响一次，之后整个 TTL 窗口全程静默。

    原实现只在网络路径校验：上游改列后第一次 fetch 有告警，第二次起命中缓存就闷声返回残缺表。
    zt_pool 的 ttl 是 12 小时、zyjs 是 30 天，等于"调用方必须知道这次不完整"这条契约
    在绝大部分时间里根本不成立。
    """
    renamed = pd.DataFrame({"股票代码": ["000001"], "股票名称": ["平安银行"]})
    monkeypatch.setitem(sys.modules, "akshare", _FakeAk(stock_zt_pool_em=lambda **p: renamed))
    src = get_advanced_source()

    assert src.fetch("zt_pool", {"date": "20260814"}) is not None
    assert any("期望列缺失" in w for w in src.warnings)

    # 换成一碰就炸的假模块：下一次的数据只可能来自缓存
    monkeypatch.setitem(sys.modules, "akshare", _ExplodingAk())
    cached = src.fetch("zt_pool", {"date": "20260814"})

    assert cached is not None
    assert list(cached.columns) == ["股票代码", "股票名称"]
    assert src.last_error is None, "缺列是告警不是错误，走缓存也一样"
    warned = src.warnings
    assert any("期望列缺失" in w for w in warned), "命中缓存就不吭声了：整个 TTL 窗口里没人知道数据是残缺的"
    assert any("连板数" in w for w in warned), "告警里要点名缺了哪几列"


def test_cache_hit_still_reports_empty_frame(monkeypatch):
    """空表也一样：缓存里躺着一张空表，命中时照样要说"这次是空的"。"""
    monkeypatch.setitem(sys.modules, "akshare", _FakeAk(stock_hot_rank_em=lambda **p: pd.DataFrame()))
    src = get_advanced_source()

    # 空表不写缓存，所以手工把一张空表塞进缓存，模拟"缓存里就是空的"
    empty_key = "hot_rank|{}"
    advanced_data._cache_put(empty_key, pd.DataFrame())
    monkeypatch.setitem(sys.modules, "akshare", _ExplodingAk())

    out = src.fetch("hot_rank")

    assert out is not None
    assert out.empty
    assert src.last_error is None
    assert any("空表" in w for w in src.warnings), "命中缓存拿到空表却不吭声，调用方会以为今天真没数据"


# ==================== 26-31：参数值、坏依赖、体检 ====================


def test_unserializable_param_value_returns_none(monkeypatch):
    """参数**值**不能序列化（传了 date / Timestamp 而不是 'YYYYMMDD' 字符串）→ 返回 None，不外抛。

    第 4 步的白名单只校验参数**名**，值的类型它不管；缓存键那句 ``json.dumps`` 于是成了
    一个没人看守的抛点，``TypeError: Object of type date is not JSON serializable``
    会直接穿出 fetch，违反"只返回 None、不抛异常"。
    刻意不用 ``default=str`` 糊过去：那会让 date 对象带着 repr 混进缓存键，
    同一个日期写法不同就是两份缓存，错得更隐蔽。
    """
    monkeypatch.setitem(sys.modules, "akshare", _ExplodingAk())
    src = get_advanced_source()

    out = src.fetch("zt_pool", {"date": datetime.date(2026, 8, 14)})  # 不外抛本身就是断言的一部分

    assert out is None
    err = src.last_error or ""
    assert "无法序列化" in err
    assert "zt_pool" in err, "报错要说清是哪条接口的参数出的问题"
    assert src.warnings == []


def test_broken_akshare_import_is_reported_not_raised(monkeypatch, tmp_path):
    """akshare 的依赖坏了、import 时抛的**不是** ImportError → 照样返回 None / False，绝不外抛。

    akshare 的依赖面很宽（requests / lxml / bs4 / py_mini_racer …），装歪一个依赖，
    import 时抛出来的常常是 AttributeError、OSError 之类。只接 ImportError 的话，
    签名承诺 ``pd.DataFrame | None`` 的 fetch 和承诺 ``bool`` 的 health_check
    都会把异常扔给调用方——而这一层存在的全部意义就是"上游坏了不要炸到主流程"。
    """
    broken = tmp_path / "brokenpath"
    broken.mkdir()
    (broken / "akshare.py").write_text(
        "raise AttributeError(\"module 'lxml.etree' has no attribute '_Element'\")\n",
        encoding="utf-8",
    )
    monkeypatch.delitem(sys.modules, "akshare", raising=False)
    monkeypatch.syspath_prepend(str(broken))
    importlib.invalidate_caches()

    src = get_advanced_source()

    assert src.fetch("hot_rank") is None  # 不外抛本身就是断言的一部分
    err = src.last_error or ""
    assert "导入 akshare 失败" in err
    assert "AttributeError" in err, "要带上真实的异常类型，否则没法判断是哪个依赖坏了"

    assert src.health_check() is False
    assert "AttributeError" in (src.last_error or "")


def test_health_check_reflects_whether_akshare_can_be_imported(monkeypatch):
    """health_check：能 import 就 True；import 不了就 False 且写明"未安装 akshare"。

    这是 CLI 启动时用来决定"这一层要不要挂出来"的开关，恒真恒假都会误导人：
    恒真会让用户在每条命令上撞一次 None，恒假会让装好了 akshare 的人以为功能没上。
    """
    pytest.importorskip("akshare")
    src = get_advanced_source()
    assert src.health_check() is True

    # sys.modules 里放 None，import 语句会抛 ImportError——正是"没装"的那条路径
    monkeypatch.setitem(sys.modules, "akshare", None)
    assert src.health_check() is False
    assert "未安装 akshare" in (src.last_error or "")

    assert src.fetch("hot_rank") is None
    assert "未安装 akshare" in (src.last_error or "")


def test_declared_params_are_a_subset_of_the_real_signature():
    """注册表登记的参数名，必须真的出现在 akshare 函数的签名里。

    ``params`` 是**对外广告的白名单**：MCP / agent 看见它就会照着传。登记了一个上游
    根本不收的参数（历史上 ths_rank_ljqs / lxsz / cxfl 三条就是这样），
    调用必 TypeError，而 hasattr 那条测试完全看不出来——它只查名字在不在。
    这条是那类静默漂移的唯一防线，同样只做静态签名检查、绝不调用。
    """
    ak = pytest.importorskip("akshare")
    bad: list[tuple[str, str, list[str], list[str]]] = []
    for spec in INTERFACES:
        func = getattr(ak, spec.func, None)
        if func is None:
            continue  # "函数没了"归上面那条测试管，这里只管参数
        try:
            sig = inspect.signature(func)
        except (TypeError, ValueError):
            continue  # 拿不到签名（C 扩展之类）就跳过，不硬判死
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
            continue  # **kwargs 收一切，判不出对错
        extra = sorted(set(spec.params) - set(sig.parameters))
        if extra:
            bad.append((spec.key, spec.func, extra, list(sig.parameters)))
    assert bad == [], (
        f"这些接口的 params 里登记了上游签名里没有的参数名，传过去必 TypeError；"
        f"去 INTERFACES 里删掉或改名（格式：key, func, 多余的参数, 真实签名）: {bad}"
    )


def test_selfcheck_structure_without_probing(monkeypatch):
    """``selfcheck(probe=False)`` 是纯离线的：六个中文键齐全，三个清单全空。

    selfcheck 有 60 行，原先零覆盖——它是排障时第一个被跑的东西，结构一变
    （少一个键、多算一条）排障的人第一眼就被带偏。
    """
    monkeypatch.setitem(sys.modules, "akshare", _fake_ak_with_every_function())

    result = selfcheck(probe=False)

    assert set(result) == {"akshare 版本", "接口总数", "函数已不存在", "探活通过", "探活失败", "跳过_需要参数"}
    assert result["接口总数"] == len(INTERFACES) == 31
    assert result["akshare 版本"] == "0.0.0-fake"
    assert result["函数已不存在"] == []
    assert result["探活通过"] == [], "probe=False 不许发任何请求"
    assert result["探活失败"] == []
    assert result["跳过_需要参数"] == [i.key for i in INTERFACES if i.params]
    # A1 之后这三条是无参接口了，不该再出现在"跳过"里，否则它们永远探不到活
    for key in ("ths_rank_ljqs", "ths_rank_lxsz", "ths_rank_cxfl"):
        assert key not in result["跳过_需要参数"], f"{key} 已改成无参接口，应当参与探活"
    assert "zt_pool" in result["跳过_需要参数"], "有参接口必须跳过：随手编的参数取回空表说明不了任何问题"


def test_selfcheck_probes_every_parameterless_interface(monkeypatch):
    """``selfcheck(probe=True)``：无参接口逐条探活，有告警的要把告警一并带上。

    "跳过清单"一旦算错（比如把 31 条全塞进去），探活就永远不执行——自检永远绿，
    而这正是它最该报警的场景。所以这里把"探到的集合"和"无参接口的集合"直接对拍。
    """
    monkeypatch.setitem(
        sys.modules,
        "akshare",
        # 让其中一条返回空表：探活通过但要带"告警"
        _fake_ak_with_every_function(stock_info_global_ths=lambda **p: pd.DataFrame()),
    )

    result = selfcheck(probe=True)

    paramless = {i.key for i in INTERFACES if not i.params}
    assert paramless, "注册表里至少要有无参接口，否则探活形同虚设"
    assert {e["key"] for e in result["探活通过"]} == paramless, "无参接口必须逐条探活，一条都不能漏"
    assert result["探活失败"] == []
    assert set(result["跳过_需要参数"]).isdisjoint(paramless), "无参接口不该出现在跳过清单里"

    empty_entry = next(e for e in result["探活通过"] if e["key"] == "ths_global_news")
    assert empty_entry["行数"] == 0
    assert any("空表" in w for w in empty_entry["告警"]), "探活时的告警必须带出来，否则'通过'会掩盖'其实是空的'"

    normal_entry = next(e for e in result["探活通过"] if e["key"] == "hot_rank")
    assert "告警" not in normal_entry, "没告警就别加这个键，免得排障时以为出了事"
    assert normal_entry["列"] == ["代码", "名称"]


# ==================== 32-37：限流 ====================


def test_rate_limiter_actually_waits_and_is_per_source(monkeypatch):
    """限流真的会等，而且东财和同花顺各等各的。

    这一层的失败模式是**被封 IP**，限流器停摆是看不见的——数据照常取到，
    直到某天上游开始返回 403。所以要么真的量一次时间，要么这道防线等于不存在。
    """
    interval = 0.3  # 得比 sqlite 一次 commit 的 fsync 开销（几十毫秒）大出一截，量出来才有意义
    monkeypatch.setenv(ENV_MIN_INTERVAL, str(interval))
    assert advanced_data._min_interval() == interval, "环境变量要每次调用都读，改了立刻生效"

    started = time.perf_counter()
    advanced_data._rate_limit(SOURCE_EM)
    advanced_data._rate_limit(SOURCE_EM)
    elapsed = time.perf_counter() - started
    assert elapsed >= interval * 0.8, f"同一个源连着两次调用没有被拉开间隔（只用了 {elapsed:.3f}s）"

    # 东财和同花顺是两套服务器，不该互相拖累
    assert advanced_data._LIMITERS[SOURCE_EM] is not advanced_data._LIMITERS[SOURCE_THS], "每个 source 必须各有一把闸门"
    em_ts = _read_rate_ts(SOURCE_EM)
    assert em_ts is not None
    assert _read_rate_ts(SOURCE_THS) is None, "同花顺一次都没调用过，不该已经有登记"

    started = time.perf_counter()
    advanced_data._rate_limit(SOURCE_THS)
    ths_elapsed = time.perf_counter() - started
    ths_ts = _read_rate_ts(SOURCE_THS)

    assert ths_ts is not None, "同花顺没有自己的那一格：两个源共用了一把闸门"
    assert ths_ts - em_ts < interval * 0.8, "同花顺被排到了东财的队尾——它是另一套服务器，不该替东财背限流"
    assert ths_elapsed < interval * 0.8, f"同花顺被东财刚才那两次拖累了（等了 {ths_elapsed:.3f}s）"

    monkeypatch.setenv(ENV_MIN_INTERVAL, "0.25")
    assert advanced_data._min_interval() == 0.25, "间隔被缓存住了，运行中改环境变量不生效"
    monkeypatch.setenv(ENV_MIN_INTERVAL, "abc")
    assert advanced_data._min_interval() == DEFAULT_MIN_INTERVAL, "配错了该退回默认值，不该抛异常"


@pytest.mark.parametrize("raw", ["inf", "+inf", "1e400", "nan", "-1", "-inf", "abc", "", "0x10", "None"])
def test_bad_min_interval_falls_back_to_default(monkeypatch, raw):
    """``ADVANCED_MIN_INTERVAL`` 给了非有限值 / 负数 / 非数字，一律退回默认 0.6。

    ``float()`` 是认 'inf' / '1e400' / 'nan' 的，这三个都是灾难：
    ``inf`` 会让 ``time.sleep`` 抛 ``OverflowError``，栈在限流器里、一路穿出 fetch；
    ``nan`` 更阴——所有比较恒为 False，限流被**无声关掉**，而这一层的失败模式正是被封 IP。
    """
    monkeypatch.setenv(ENV_MIN_INTERVAL, raw)
    assert advanced_data._min_interval() == DEFAULT_MIN_INTERVAL


def test_huge_min_interval_is_clamped(monkeypatch):
    """把"秒"手滑写成 3600（当成"每小时一次"）要夹到上界，不能让流水线挂死在限流器里。"""
    monkeypatch.setenv(ENV_MIN_INTERVAL, "3600")
    assert advanced_data._min_interval() == MAX_MIN_INTERVAL
    monkeypatch.setenv(ENV_MIN_INTERVAL, "0")
    assert advanced_data._min_interval() == 0.0, "0 是刻意关掉限流（测试就这么用），不能被当成配错"


def test_infinite_min_interval_does_not_break_fetch(monkeypatch):
    """``ADVANCED_MIN_INTERVAL=inf`` 时 fetch 照常返回数据，不抛 OverflowError。"""
    monkeypatch.setenv(ENV_MIN_INTERVAL, "inf")
    # 先钉住 _min_interval：它一旦真返回 inf，下面那次 fetch 会睡到上限才醒
    assert advanced_data._min_interval() == DEFAULT_MIN_INTERVAL

    monkeypatch.setitem(
        sys.modules, "akshare", _FakeAk(stock_hot_rank_em=lambda **p: pd.DataFrame({"代码": ["000001"]}))
    )
    src = get_advanced_source()

    out = src.fetch("hot_rank")  # 不外抛本身就是断言的一部分

    assert out is not None
    assert src.last_error is None


def test_rate_limit_is_written_to_sqlite(monkeypatch):
    """限流时刻必须落进 sqlite——那是独立进程之间唯一的共享锚点。

    ``multiprocessing.Lock/Value`` 是 import 时创建的，只有 fork 出来的子进程共享得到；
    本仓库的实际形态是 zt / zt-web / zt-monitor 三个 entry point 加 cron，全是**独立启动**的
    进程，各建各的，等于没限。落库才是真的跨进程。
    """
    monkeypatch.setenv(ENV_MIN_INTERVAL, "0.05")

    advanced_data._rate_limit(SOURCE_EM)
    first = _read_rate_ts(SOURCE_EM)
    advanced_data._rate_limit(SOURCE_EM)
    second = _read_rate_ts(SOURCE_EM)

    assert first is not None, "限流时刻没落库：独立启动的进程之间根本没有共享的锚点"
    assert second is not None
    assert second - first >= 0.05 * 0.8, "台账没往前推进，第二个进程会以为轮到自己了"
    assert _read_rate_ts(SOURCE_THS) is None, "东财的登记不该污染同花顺那一格"


# 子进程脚本：等父进程放行后调一次 _rate_limit，记下"闸门放行的时刻"。
# 先 import 再 ready，是为了把 pandas 那 0.3 秒的启动开销挡在计时窗口之外——
# 否则量到的间隔里混着进程启动抖动，限流坏了也可能"看起来"是对的。
_RATE_SUBPROCESS = """
import pathlib, sys, time

sys.path.insert(0, sys.argv[1])
from modules.advanced_data import _rate_limit

ready, go, out = (pathlib.Path(p) for p in sys.argv[2:5])
ready.write_text("1")
deadline = time.time() + 30
while not go.exists() and time.time() < deadline:
    time.sleep(0.005)
_rate_limit("东财")
out.write_text(repr(time.time()))
"""


def test_rate_limit_is_shared_across_independent_processes(tmp_path):
    """三个**各自 python3 启动**的进程，对同一个源的调用必须被串成最小间隔。

    这条是 A9 那个"注释在骗人"的现场：原实现里 3 个独立进程 9 次调用挤在 1.03 秒内全打了
    出去（真共享的话该是 4 秒）。用 ready/go 两个文件把三个进程的起跑线对齐，
    量到的就是纯粹的限流间隔，不含进程启动抖动。
    """
    interval = 0.3
    procs_n = 3
    root = str(Path(advanced_data.__file__).resolve().parents[1])
    script = tmp_path / "one_rate_limited_call.py"
    script.write_text(_RATE_SUBPROCESS, encoding="utf-8")
    shared_db = tmp_path / "shared_advanced_cache.db"
    go = tmp_path / "go"
    env = dict(os.environ, ADVANCED_CACHE_PATH=str(shared_db), ADVANCED_MIN_INTERVAL=str(interval))

    ready = [tmp_path / f"ready{i}" for i in range(procs_n)]
    out = [tmp_path / f"out{i}" for i in range(procs_n)]
    procs = [
        subprocess.Popen(
            [sys.executable, str(script), root, str(ready[i]), str(go), str(out[i])],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for i in range(procs_n)
    ]
    try:
        deadline = time.time() + 60
        while time.time() < deadline and not all(p.exists() for p in ready):
            time.sleep(0.01)
        assert all(p.exists() for p in ready), "子进程没起来，后面的计时没有意义"
        go.write_text("go")
        for proc in procs:
            stdout, stderr = proc.communicate(timeout=60)
            assert proc.returncode == 0, f"子进程失败：{stderr.decode(errors='replace')}"
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()

    stamps = sorted(float(p.read_text()) for p in out)
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert stamps[-1] - stamps[0] >= interval * (procs_n - 1) * 0.8, (
        f"三个独立进程的调用挤在 {stamps[-1] - stamps[0]:.3f}s 内，跨进程限流没生效——上次调用时刻只存在各自的内存里"
    )
    # 单个间隔放得比总跨度松：某个进程被调度器多压 100ms，会同时让前一个间隔变大、后一个变小，
    # 总跨度不受影响，单个间隔却会。0.5 倍仍然离"限流坏掉"（毫秒级）远得很
    assert all(g >= interval * 0.5 for g in gaps), f"进程之间没有被逐个拉开间隔：{[round(g, 3) for g in gaps]}"


# ==================== 38-39：并发串味与缓存落点 ====================


@pytest.mark.parametrize("noisy_ends_with", ["error", "warning"], ids=["别人的错误", "别人的告警"])
def test_call_state_does_not_leak_between_threads(monkeypatch, noisy_ends_with):
    """``last_error`` / ``warnings`` 是每个线程各一份，不能串味。

    单例是全进程共享的（限流状态必须共享，所以只能有一个实例），可这两个字段讲的是
    "**我刚才那次** fetch 出了什么事"。放成普通属性，两个线程并发 fetch 就会互相盖：
    一个线程拿到了干净数据，却读到别人的 RuntimeError；或者反过来，干净数据被告知"缺了 5 列"。
    本仓库 modules/data_sync/syncer.py 已经在用 ThreadPoolExecutor 并发拉数了，这不是假想的场景。

    交错**不靠运气**：干净线程的假 akshare 会卡在取数中间等一个事件，主线程趁这个窗口
    把错误 / 告警写满，再放它继续走完。抢时间片的写法实测抓不住（跑 300 轮一次都没串到），
    那种测试等于没写。两个方向各跑一遍：最后一次污染是错误，和最后一次污染是告警。
    """
    renamed = pd.DataFrame({"股票代码": ["000001"], "股票名称": ["平安银行"]})
    quiet_is_inside = threading.Event()  # 干净线程已经进到 fetch 中间（状态刚清完）
    noise_is_done = threading.Event()  # 主线程已经把别人的错误/告警写满了

    def _boom(**params: str) -> pd.DataFrame:
        raise RuntimeError("这是另一个线程的错误")

    def _clean_but_slow(**params: str) -> pd.DataFrame:
        quiet_is_inside.set()
        assert noise_is_done.wait(30), "主线程没能在窗口期内制造出噪声"
        return pd.DataFrame({"代码": ["000001"], "排名": [1]})

    monkeypatch.setitem(
        sys.modules,
        "akshare",
        _FakeAk(stock_zt_pool_em=_boom, stock_zt_pool_dtgc_em=lambda **p: renamed, stock_hot_rank_em=_clean_but_slow),
    )
    src = get_advanced_source()
    seen: dict[str, Any] = {}

    def _quiet() -> None:
        df = src.fetch("hot_rank", force=True)
        seen["df"] = df
        seen["last_error"] = src.last_error
        seen["warnings"] = src.warnings

    quiet = threading.Thread(target=_quiet, daemon=True)
    quiet.start()
    assert quiet_is_inside.wait(30), "干净线程没进到 fetch 里"

    # 主线程扮演"隔壁那个倒霉的线程"：一次真错误 + 一次缺列告警，顺序决定最后留下的是哪种
    steps = ["warning", "error"] if noisy_ends_with == "error" else ["error", "warning"]
    for step in steps:
        if step == "error":
            assert src.fetch("zt_pool", {"date": "20260814"}) is None
            assert src.last_error is not None
        else:
            assert src.fetch("dt_pool", {"date": "20260814"}) is not None
            assert any("期望列缺失" in w for w in src.warnings)
    noise_is_done.set()
    quiet.join(timeout=30)

    assert not quiet.is_alive()
    assert seen["df"] is not None, "干净线程自己的取数不该失败"
    assert seen["last_error"] is None, f"干净线程读到了别的线程的错误：{seen['last_error']!r}"
    assert seen["warnings"] == [], f"干净线程被告知数据不完整，可它拿到的是完整数据：{seen['warnings']!r}"


def test_cache_path_prefers_explicit_env_over_data_dir(monkeypatch, tmp_path):
    """``ADVANCED_CACHE_PATH`` 优先于 ``DATA_DIR``，两个都设时缓存落在前者。

    测试的隔离性就靠这条优先级：conftest.py 那个 autouse fixture 把 DATA_DIR 指到临时目录，
    本文件的 fixture 再用 ADVANCED_CACHE_PATH 指到自己的 tmp_path。优先级一旦反过来，
    隔离就变成靠 conftest 侥幸兜住——而它兜不住"真库 data/stock_data.db 旁边多出一个
    advanced_cache.db"这种事。
    """
    data_dir = tmp_path / "datadir"
    explicit = tmp_path / "explicit" / "adv.db"
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv(ENV_CACHE_PATH, str(explicit))

    assert advanced_data._cache_path() == explicit

    monkeypatch.setitem(
        sys.modules, "akshare", _FakeAk(stock_hot_rank_em=lambda **p: pd.DataFrame({"代码": ["000001"]}))
    )
    src = get_advanced_source()
    assert src.fetch("hot_rank") is not None

    assert explicit.exists(), "缓存没落在 ADVANCED_CACHE_PATH 指定的位置"
    assert not (data_dir / "advanced_cache.db").exists(), "缓存跑到 DATA_DIR 里去了：优先级反了"

    # 只有在没设 ADVANCED_CACHE_PATH 时才轮到 DATA_DIR
    monkeypatch.delenv(ENV_CACHE_PATH)
    assert advanced_data._cache_path() == data_dir / "advanced_cache.db"


# ==================== 40-46：注册表只读、跨进程、缓存旧格式与契约边界 ====================


def test_interface_params_are_readonly_through_every_mutation_entry(monkeypatch):
    """``Interface.params`` 的**每一个**写入口都要被封死，而且不能因此变得不可 hash。

    ``@dataclass(frozen=True)`` 只挡住"把 params 整个换掉"，挡不住 ``params['evil'] = 'x'``——
    而 INTERFACES 是模块级、进程内共享的参数白名单，谁污染一次全进程遭殃，症状是别处一个
    毫不相干的 fetch 突然接受了本不该接受的参数名。
    dict 的写入口不止 ``__setitem__`` 一个，漏掉任何一个都等于没封（``update`` 和 ``|=``
    尤其容易漏），所以这里逐个点名。

    后半段的 hash / set 同样要钉住：曾经想用 ``MappingProxyType`` 做只读，它会让
    ``hash(Interface)`` 直接报错——冻结 dataclass 的 hash 要算上每个字段。
    """
    params = BY_KEY["zt_pool"].params
    assert isinstance(params, dict), "params 是 dict 是对外契约，照着契约写的调用方和测试都指着它"
    before = dict(params)

    mutations: list[tuple[str, Callable[[], Any]]] = [
        ("__setitem__", lambda: params.__setitem__("evil", "x")),
        ("__delitem__", lambda: params.__delitem__(next(iter(params)))),
        ("update", lambda: params.update({"evil": "x"})),
        ("setdefault", lambda: params.setdefault("evil", "x")),
        ("pop", lambda: params.pop(next(iter(params)))),
        ("popitem", lambda: params.popitem()),
        ("clear", lambda: params.clear()),
        ("|=", lambda: params.__ior__({"evil": "x"})),
    ]
    for name, mutate in mutations:
        with pytest.raises(TypeError):
            mutate()
        assert dict(params) == before, f"{name} 抛了 TypeError，但东西已经改进去了"

    # 只读不能是拿"不可 hash"换来的
    assert hash(BY_KEY["zt_pool"]) == hash(BY_KEY["zt_pool"])
    assert len(set(INTERFACES)) == len(INTERFACES)

    # 副本反过来必须是能随便改的——只读的是注册表那一份，不是调用方手里的
    import copy as _copy

    mine = _copy.copy(params)
    mine["evil"] = "x"
    assert "evil" not in params, "改副本改到注册表原件上去了"


def test_interface_survives_pickle_and_deepcopy():
    """``Interface`` 必须能 pickle 往返、能 deepcopy——多进程就靠这个。

    dict 子类的 pickle / deepcopy 走 ``__reduce_ex__`` / ``_reconstruct``，重建时**逐条调
    ``__setitem__``**，正好撞上被封死的写入口。要命的是失败点在**反序列化那一侧**：
    ``pickle.dumps`` 一声不吭地写出 461 字节，``pickle.loads`` 才炸，而且报的是
    "params 是只读的"——放进 ``ProcessPoolExecutor``（本仓库 ``zt replay --workers N``
    就是多进程）里，症状变成 BrokenProcessPool，文案把人往完全错误的方向带。

    所以这条测的是 loads / deepcopy，不是 dumps：只测 dumps 是绿的，等于没测。
    """
    import copy
    import pickle

    revived = pickle.loads(pickle.dumps(BY_KEY))
    assert sorted(revived) == sorted(BY_KEY)
    assert revived["zt_pool"] == BY_KEY["zt_pool"]
    assert dict(revived["lhb_detail"].params) == dict(BY_KEY["lhb_detail"].params)
    # 跨进程传过去的仍然代表"进程级共享的白名单"，那边也该是只读的
    with pytest.raises(TypeError):
        revived["zt_pool"].params["evil"] = "x"

    clones = copy.deepcopy(INTERFACES)
    assert [c.key for c in clones] == [spec.key for spec in INTERFACES]
    assert dict(clones[0].params) == dict(INTERFACES[0].params)
    # deepcopy 的语义跟 copy 对齐：副本是可写的普通 dict，且改它不会回流到注册表
    clones[0].params["evil"] = "x"
    assert "evil" not in INTERFACES[0].params, "deepcopy 出来的副本和注册表原件还连着"

    plain = copy.deepcopy(BY_KEY["zt_pool"].params)
    assert plain == dict(BY_KEY["zt_pool"].params)
    plain["evil"] = "x"  # 不该抛


def _params_seen_in_child(spec: Any) -> tuple[int, str, str, list[str]]:
    """在子进程里拆开一条 Interface；必须是模块级函数，否则它自己都 pickle 不了。"""
    return (os.getpid(), spec.key, type(spec.params).__name__, sorted(spec.params))


def test_interface_really_crosses_a_process_pool_boundary():
    """把 Interface 丢给真的 ``ProcessPoolExecutor``，在真的子进程里拆开看。

    ``pickle.loads(pickle.dumps(x))`` 在同一个进程里跑，糊弄得过去；ProcessPoolExecutor
    才是出事的现场——它把参数塞进 call queue，那条路上的反序列化失败会被包装成
    ``BrokenProcessPool``，跟真正的原因（params 只读）一点关系都看不出来。
    断言里带上子进程 pid，是为了防止"其实根本没起子进程"这种假绿。
    """
    from concurrent.futures import ProcessPoolExecutor

    specs = [BY_KEY["zt_pool"], BY_KEY["lhb_detail"]]
    ctx = multiprocessing.get_context("spawn")  # fork 也能过，spawn 更接近"另一台机器上重建"
    with ProcessPoolExecutor(max_workers=1, mp_context=ctx) as pool:
        results = list(pool.map(_params_seen_in_child, specs))

    assert [r[1] for r in results] == ["zt_pool", "lhb_detail"]
    assert all(r[0] != os.getpid() for r in results), "根本没起子进程，这条测试等于没测"
    assert [r[3] for r in results] == [sorted(specs[0].params), sorted(specs[1].params)]
    assert {r[2] for r in results} == {"_FrozenParams"}, "子进程里的 params 不再是只读的白名单"


def test_legacy_cache_payload_without_version_key_is_still_readable(monkeypatch):
    """升级前写下的**旧格式**缓存行（payload 就是裸的 split json，没有 v 键）必须照样读得出来。

    这条兼容分支在 ``_decode_payload`` 里，看着像永远走不到——直到你想起用户机器上那张
    advanced_cache.db 是升级前就写好的。它一旦失效，症状不是报错而是**全部当成没命中**：
    缓存静默失效，每次 fetch 都去打上游，正好踩在这一层最怕的"被封 IP"上。
    实测把这个分支改回 ``return None``，其余 52 条测试**全绿**——所以必须手写一行旧格式来钉住。
    """
    legacy = pd.DataFrame({"代码": ["000001", "600519"], "名称": ["平安银行", "贵州茅台"]})
    cache_key = "hot_rank|" + json.dumps({}, sort_keys=True, ensure_ascii=False)

    with sqlite3.connect(str(advanced_data._cache_path())) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS advanced_cache (k TEXT PRIMARY KEY, ts REAL, payload TEXT)")
        conn.execute(
            "INSERT OR REPLACE INTO advanced_cache (k, ts, payload) VALUES (?, ?, ?)",
            # 旧格式：payload 直接就是 split json，外面没有 {"v": ..., "df": ...} 这层壳
            (cache_key, time.time(), legacy.to_json(orient="split", force_ascii=False, date_format="iso")),
        )
        conn.commit()

    # 一碰就炸的假 akshare：读到了就一定是从这行旧格式缓存里读出来的，不是绕道网络拿的
    monkeypatch.setitem(sys.modules, "akshare", _ExplodingAk())
    src = get_advanced_source()
    got = src.fetch("hot_rank")

    assert got is not None, "旧格式缓存被当成没命中，整张老库等于作废、每次都要去打上游"
    assert list(got.columns) == ["代码", "名称"]
    assert got["代码"].tolist() == ["000001", "600519"], "前导零没了：旧格式也得关掉 dtype 推断"


def test_cache_round_trip_preserves_index_name(monkeypatch):
    """带 name 的 index 往返一圈，name 不能丢。

    ``to_json(orient="split")`` 压根不写 index name，读回来是 None。31 条里
    ``ths_industry_index`` 产出的正是 ``name='日期'`` 的 DatetimeIndex。
    这条**不能只用 ``.equals()`` 收尾**——equals 不比较 index name，丢了它照样判 True
    （实测就是这样漏过去的）。发作要到下游：``fresh.reset_index()`` 抛
    ``ValueError: cannot insert 日期, already exists``，``cached.reset_index()`` 却安静地
    多出一列叫 ``index`` 的表，同一份数据两种列名。
    """
    index = pd.to_datetime(["2026-08-13", "2026-08-14"])
    index.name = "日期"
    frame = pd.DataFrame({"收盘价": [1234.5, 1250.0], "涨跌幅": [0.5, 1.25]}, index=index)
    monkeypatch.setitem(sys.modules, "akshare", _FakeAk(stock_hot_rank_em=lambda **p: frame.copy()))
    src = get_advanced_source()

    fresh = src.fetch("hot_rank")
    assert fresh is not None
    assert fresh.index.name == "日期"

    monkeypatch.setitem(sys.modules, "akshare", _ExplodingAk())
    cached = src.fetch("hot_rank")

    assert cached is not None
    assert cached.index.name == fresh.index.name, "index name 在缓存往返里丢了；.equals() 看不见它"
    assert str(cached.index.dtype) == str(fresh.index.dtype), "DatetimeIndex 退化了，下游 .dt / 切片全会变样"
    assert cached.index.equals(fresh.index)
    assert cached.equals(fresh)
    # 下游真正会踩的那一步：两边 reset_index 出来的列名必须一模一样
    assert list(cached.reset_index().columns) == list(fresh.reset_index().columns)


def test_cache_round_trip_matches_akshare_style_missing_dates(monkeypatch):
    """**按 akshare 真实形态**构造的、带空值的日期列，往返后必须 ``.equals()`` 为 True。

    31 条里产出日期列的那 11 条，akshare 源码里全是
    ``pd.to_datetime(..., errors="coerce").dt.date``——coerce 出来的空值是 ``NaT``，
    不是 ``None``。所以缓存往返把 null 一律还原成 ``NaT``，对**真实数据**是精确的。
    这条测试就是把这个"精确"钉死：契约面向的是 akshare 给的表，不是手工捏的表。
    """
    frame = pd.DataFrame(
        {
            "代码": ["000001", "600519", "000002"],
            "上榜日": pd.to_datetime(["2026-08-13", None, "2026-08-14"], errors="coerce").date,
            "封板资金": [1.0, 2.0, 3.0],
        }
    )
    assert frame["上榜日"].isna().any(), "构造的表里根本没有空值，这条测试什么都没测到"
    monkeypatch.setitem(sys.modules, "akshare", _FakeAk(stock_hot_rank_em=lambda **p: frame.copy()))
    src = get_advanced_source()

    fresh = src.fetch("hot_rank")
    assert fresh is not None

    monkeypatch.setitem(sys.modules, "akshare", _ExplodingAk())
    cached = src.fetch("hot_rank")

    assert cached is not None
    assert cached.dtypes.to_dict() == fresh.dtypes.to_dict()
    assert cached.equals(fresh), "照 akshare 真实形态构造的日期列，往返后必须一模一样"
    assert cached["上榜日"].tolist()[0] == datetime.date(2026, 8, 13)
    assert pd.isna(cached["上榜日"].tolist()[1])


def test_manual_none_in_a_date_column_becomes_nat(monkeypatch):
    """**已知边界，故意钉住**：手工写进日期列的 ``None`` 往返后会变成 ``NaT``。

    JSON 只有一个 ``null``，``None`` / ``NaN`` / ``NaT`` 写出去长得一样，还原时只能挑一个，
    这一层挑的是 ``NaT``（akshare 那 11 条 coerce 出来的就是它，见上一条测试）。
    代价落在手工构造的表上：``.equals()`` 会判 False。值和 dtype 都是对的，``pd.isna()``
    照样为真，不会静默算错。

    这条测试的作用是**防止有人把它当 bug 修掉**——要还原得回 ``None``，就得在侧车里逐行
    记下每个空值原来是什么，为一个下游根本不关心的区别付整表的存储和复杂度。
    哪天真要改这个契约，这条测试会变红，那时请连同上面那条一起重新想清楚。
    """
    frame = pd.DataFrame({"代码": ["000001", "600519"], "上榜日": [datetime.date(2026, 8, 13), None]})
    monkeypatch.setitem(sys.modules, "akshare", _FakeAk(stock_hot_rank_em=lambda **p: frame.copy()))
    src = get_advanced_source()

    fresh = src.fetch("hot_rank")
    assert fresh is not None
    assert fresh["上榜日"].tolist()[1] is None, "fresh 这一侧应当原样保留手写的 None"

    monkeypatch.setitem(sys.modules, "akshare", _ExplodingAk())
    cached = src.fetch("hot_rank")

    assert cached is not None
    assert cached["上榜日"].tolist()[0] == datetime.date(2026, 8, 13), "非空值必须一字不差"
    assert cached["上榜日"].tolist()[1] is pd.NaT, "契约是统一还原成 NaT"
    assert pd.isna(cached["上榜日"]).tolist() == [False, True], "缺失位置没变，下游 isna 判断不受影响"
    assert not cached.equals(fresh), "这就是那个已知代价；哪天它变 True 了，说明契约被改了"


def test_multiindex_frame_is_not_cached_instead_of_being_silently_transposed(monkeypatch):
    """MultiIndex 的表宁可不缓存，也不能缓存出一份**转置过**的 index。

    ``to_json(orient="split")`` 把 MultiIndex 写成 ``[["a",1],["b",2]]``，
    ``read_json`` 读回来**按列拆**，得到 ``[('a','b'), (1,2)]``——行索引整个转置了，
    而且一声不吭（``convert_axes=True`` 那条老路更绝，直接抛 NotImplementedError，
    被兜底 except 吞成"没命中"，于是每次都去打上游）。
    31 条接口目前没有一条产出 MultiIndex，这条钉的是"上游哪天改成 MultiIndex"：
    那时该发生的是缓存不生效（慢一点），不是行和索引对错位（结果错，且查不出来）。
    """
    mi = pd.MultiIndex.from_tuples([("平安银行", 1), ("贵州茅台", 2)], names=["名称", "排名"])
    frame = pd.DataFrame({"涨跌幅": [10.0, 2.0]}, index=mi)
    calls: list[int] = []

    def _upstream(**params: Any) -> pd.DataFrame:
        calls.append(1)
        return frame.copy()

    monkeypatch.setitem(sys.modules, "akshare", _FakeAk(stock_hot_rank_em=_upstream))
    src = get_advanced_source()

    fresh = src.fetch("hot_rank")
    assert fresh is not None
    assert fresh.index.equals(mi)

    second = src.fetch("hot_rank")
    assert second is not None
    assert len(calls) == 2, "这种表根本不该进缓存"
    # 关键：第二次拿到的必须还是原样，不能是转置过的索引
    assert second.index.equals(mi), "缓存把 MultiIndex 读成了转置的样子，行和索引对错位了"
    assert list(second.index.names) == ["名称", "排名"]
    assert second.equals(fresh)


# ==================== 47-57：覆盖缺口回补 ====================
#
# 下面这一批和上面的不一样：它们对应的实现**当时就是对的**，一条 bug 都没查出来。
# 补它们的理由是变异测试的结论——把对应实现改坏（删掉一句 append、去掉一个 force=True、
# 把一个守卫换成 `if False`），全库照样 985 passed。也就是说这些行今天正确纯属运气，
# 明天谁顺手重构一下，回归是**静默**的：没有测试会红，症状全在运行时，而且全是这一层
# 最贵的那几种（每次都去打上游 → 被封 IP；限流睡满上限 → 流水线挂死；日期被截成天 →
# 结果错且查不出来）。
#
# 所以每一条的注释里都写清楚"把哪一行改坏它会红"，那是这条测试存在的全部理由。


def test_selfcheck_names_the_functions_upstream_removed(monkeypatch):
    """akshare 删掉/改名了函数，``函数已不存在`` 必须逐条点名——key 和 func 都要有。

    现有那条 selfcheck 测试只钉了**空集**那一侧（"全都在" → 清单为空）。把
    ``result["函数已不存在"].append(...)`` 整句删掉，清单永远是空的，那条测试照样绿——
    而 akshare 改函数名恰恰是这一层最常见、最静默的故障：自检从此永远报"函数名全部对得上"，
    真正断掉的那几条要等到有人去 fetch 才发现，报错还只是一句"找不到函数"。

    点名要给全 key + func 两样：key 是去 INTERFACES 里定位那一行用的，
    func 是拿去 akshare 的 changelog 里搜新名字用的，少一样都得再翻一遍注册表。
    """
    gone = [BY_KEY["zt_pool"], BY_KEY["ths_rank_cxfl"]]
    fake = _fake_ak_with_every_function()
    for spec in gone:
        delattr(fake, spec.func)  # 上游把这个函数删了
    monkeypatch.setitem(sys.modules, "akshare", fake)

    result = selfcheck(probe=False)

    # 顺序按注册表来（zt_pool 在 ths_rank_cxfl 前面），逐条对拍而不是只看条数：
    # 只看条数的话，"点名点错了人"是看不出来的
    assert result["函数已不存在"] == [{"key": s.key, "func": s.func} for s in gone]
    assert result["探活通过"] == [] and result["探活失败"] == [], "probe=False 不许有任何探活结果"
    assert result["接口总数"] == len(INTERFACES), "少了两个函数不代表注册表少了两条"


def test_selfcheck_probe_bypasses_the_cache_and_really_calls_upstream(monkeypatch):
    """``selfcheck(probe=True)`` 探的是"**现在**还通不通"，命中缓存等于没探。

    ``source.fetch(spec.key, force=True)`` 里的 ``force=True`` 去掉之后，全库测试一条不红：
    探活会从 12 小时前的缓存里读出数据，然后报告"全部通过"——而上游可能早就 502 了。
    自检是排障时第一个跑的命令，它报的"通过"必须是刚刚验证过的，不能是昨天的回声。

    证据链是这样搭的：先用能出数的假 akshare 把每条无参接口的缓存灌满，再换成
    "函数都在、但一调用就炸"的桩。带 force 就一定会去调那些函数（于是全部探活失败），
    不带 force 就会命中缓存（于是全部探活通过）——两种结果正好相反，赖不掉。
    """
    paramless = [i for i in INTERFACES if not i.params]
    assert paramless, "注册表里没有无参接口，这条用例什么都测不到"

    monkeypatch.setitem(sys.modules, "akshare", _fake_ak_with_every_function())
    src = get_advanced_source()
    for spec in paramless:
        assert src.fetch(spec.key) is not None, f"缓存没灌进去，后面的对拍不成立：{spec.key}"

    called: list[str] = []

    def _boom(func_name: str) -> Callable[..., Any]:
        def _call(**params: Any) -> pd.DataFrame:
            called.append(func_name)
            raise RuntimeError("上游 502")

        return _call

    funcs: dict[str, Any] = {"__version__": "0.0.0-fake"}
    for spec in INTERFACES:
        funcs[spec.func] = _boom(spec.func)
    monkeypatch.setitem(sys.modules, "akshare", _FakeAk(**funcs))

    result = selfcheck(probe=True)

    assert result["函数已不存在"] == [], "函数都还在，桩没搭好的话下面的断言就没意义了"
    assert result["探活通过"] == [], "探活读了缓存：上游已经炸了，自检却报通过"
    assert {e["key"] for e in result["探活失败"]} == {s.key for s in paramless}
    assert sorted(called) == sorted(s.func for s in paramless), "无参接口必须逐条真的去调一次"
    assert all("RuntimeError" in e["错误"] for e in result["探活失败"]), "失败原因要带异常类型"


@pytest.mark.parametrize(
    "broken",
    [float("inf"), "inf", float("nan"), "nan"],
    ids=["real-inf", "text-inf", "real-nan", "text-nan"],
)
def test_a_broken_timestamp_in_the_rate_ledger_is_treated_as_unregistered(broken):
    """限流台账里存进了 inf / nan，必须当成"没登记过"，而不是让它把闸门焊死。

    ``_reserve_rate_slot`` 里那句 ``if not math.isfinite(last)`` 换成 ``if False``，
    全库一条不红——因为正常路径永远写不进 inf。可这一行防的本来就不是正常路径：
    手工改过库、写到一半断电、别的工具往同一张表里塞过东西，都会留下一个坏时刻。
    留下之后 ``target = max(now, inf + interval)`` 恒为 inf，此后**每一次调用**都要
    ``time.sleep(min(inf, 60))`` 睡满上限，而且库里的 inf 会被原样写回去——**永不恢复**。
    症状是整条流水线肉眼可见地卡死，日志里却什么都没有。

    所以两头都要断言：这次不能等（返回有限且约等于 0），而且台账要被这次调用治好
    （写回去的是有限时刻），否则下次还是死的。

    四个参数里真正能顶死那句守卫的是两条 ``inf``（去掉守卫就是等 inf 秒）；两条 ``nan``
    顶不死它——``max(now, nan)`` 恰好返回 ``now``，有没有守卫结果都一样。留着它们是因为
    坏值不止一种长相：``nan`` 走的是"实数列存进 NULL"和"文本列存进 'nan'"两条不同的路，
    哪天上面那句 ``float(row[0])`` 或 ``row[0] is not None`` 被动过，它们才是会响的那个。
    """
    with sqlite3.connect(str(advanced_data._cache_path())) as conn:
        conn.execute(advanced_data._RATE_TABLE_DDL)
        conn.execute("INSERT OR REPLACE INTO advanced_rate (source, ts) VALUES (?, ?)", (SOURCE_EM, broken))
        conn.commit()

    waited = advanced_data._reserve_rate_slot(SOURCE_EM, 1.0)

    assert waited is not None, "限流表还在，不该降级"
    assert math.isfinite(waited), f"坏时刻让闸门要求等 {waited!r} 秒——此后每次调用都睡满上限"
    assert waited == pytest.approx(0.0, abs=0.5), "没登记过就该立刻放行"
    healed = _read_rate_ts(SOURCE_EM)
    assert healed is not None and math.isfinite(healed), "坏时刻原样留在台账里，下一次调用照样被焊死"


def test_cache_path_env_may_point_at_a_directory(monkeypatch, tmp_path):
    """``ADVANCED_CACHE_PATH`` 指向一个**目录**时，缓存落在它下面的默认文件名里。

    这条分支删掉之后全库一条不红——因为别的用例都把它指向一个具体文件。可"指向目录"
    正是最顺手的写法（测试里 ``setenv(ENV_CACHE_PATH, str(tmp_path))``、部署时指一个数据盘），
    分支没了 ``_cache_path()`` 就返回那个目录本身，``sqlite3.connect(目录)`` 必然失败，
    而 ``_cache_connect()`` 的兜底会把失败**咽下去返回 None**：缓存被整个静默关掉，
    每次 fetch 都去打上游——正好踩在这一层最怕的"被封 IP"上，且没有任何报错。
    """
    box = tmp_path / "cache_dir"
    box.mkdir()
    monkeypatch.setenv(ENV_CACHE_PATH, str(box))

    assert advanced_data._cache_path() == box / "advanced_cache.db"

    conn = advanced_data._cache_connect()
    assert conn is not None, "缓存被静默关掉了：此后每次 fetch 都会去打上游"
    conn.close()

    # 端到端再确认一遍：写得进去，也读得回来（缓存真的在起作用，不是只有路径对）
    monkeypatch.setitem(sys.modules, "akshare", _FakeAk(stock_hot_rank_em=lambda **p: _one_row_frame()))
    src = get_advanced_source()
    assert src.fetch("hot_rank") is not None
    assert (box / "advanced_cache.db").exists(), "缓存文件没落在目录里"

    monkeypatch.setitem(sys.modules, "akshare", _ExplodingAk())
    assert src.fetch("hot_rank") is not None, "第二次又去打上游了：缓存等于没开"


def test_catalog_hands_out_a_plain_writable_copy_of_params():
    """``catalog()`` 递出去的 ``params`` 必须是**普通 dict 副本**，不是注册表那份只读原件。

    ``dict(i.params)`` 改成 ``i.params`` 全库一条不红：``_FrozenParams`` 继承自 dict，
    ``isinstance(x, dict)`` 照样为真，长得一模一样。区别要到调用方手里才炸——
    catalog 的返回值是给 CLI / MCP / agent 拿去**加工**的（补一个默认值、标一下哪个参数必填），
    递出去只读原件的话，那一句普通的 ``params['date'] = ...`` 会抛 TypeError，
    报错文案还指向"进程级共享的参数白名单"，看的人一头雾水。

    反过来也要钉住：副本改了不能污染到注册表——那才是 ``_FrozenParams`` 存在的理由。
    """
    grouped = catalog()
    sample = next(it for items in grouped.values() for it in items if it["params"])

    assert type(sample["params"]) is dict, (
        "catalog 把注册表里那份只读 _FrozenParams 直接递出去了：调用方一改就 TypeError"
    )
    sample["params"]["新参数"] = "调用方随手加的"  # 不抛异常本身就是断言的一部分

    assert "新参数" not in BY_KEY[sample["key"]].params, "改 catalog 的返回值污染到了进程级共享的注册表"
    again = catalog()
    fresh = next(it for items in again.values() for it in items if it["key"] == sample["key"])
    assert "新参数" not in fresh["params"], "两次 catalog 拿到的是同一个 dict，调用方会互相串味"


def test_object_column_of_datetimes_keeps_its_time_of_day(monkeypatch):
    """object 列里装的是 ``datetime.datetime`` 时，往返后**时分秒一字不差**。

    ``_infer_types`` 里那句 ``and not isinstance(v, datetime.datetime)`` 删掉，全库一条不红：
    现有的日期用例装的都是 ``datetime.date``，走不到这个分支。可 ``datetime.datetime`` 是
    ``datetime.date`` 的**子类**，守卫一没，整列会被标成 ``date`` 类型，读缓存时按
    ``pd.to_datetime(...).dt.date`` 还原——**时分秒直接被截掉**。
    对"首次封板时间 09:30:05"这种列，走网络拿到的是 09:30:05、命中缓存拿到的是当天 00:00，
    两次取数结果不一样，而且不报错。

    认不出类型时这一列会以 ISO 字符串的形态留着（看得见、可解析），这是刻意的取舍：
    宁可少还原一列，也不要瞎还原成截断过的日期。
    """
    stamps = [datetime.datetime(2026, 8, 13, 9, 30, 5), datetime.datetime(2026, 8, 14, 10, 1, 12)]
    frame = pd.DataFrame({"代码": ["000001", "600519"]})
    # 直接塞进 DataFrame 会被 pandas 自动收敛成 datetime64，object 列必须显式指定 dtype
    frame["首次封板时间"] = pd.Series(stamps, dtype=object)
    assert frame["首次封板时间"].dtype == object, "构造的就不是 object 列，这条用例测不到那个守卫"

    assert advanced_data._infer_types(frame) == {}, (
        "datetime.datetime 被当成了 datetime.date：还原时会按 .dt.date 截掉时分秒"
    )

    monkeypatch.setitem(sys.modules, "akshare", _FakeAk(stock_hot_rank_em=lambda **p: frame.copy()))
    src = get_advanced_source()
    fresh = src.fetch("hot_rank")
    assert fresh is not None

    monkeypatch.setitem(sys.modules, "akshare", _ExplodingAk())
    cached = src.fetch("hot_rank")

    assert cached is not None
    assert cached["首次封板时间"].dtype == object
    back = [str(v) for v in cached["首次封板时间"].tolist()]
    assert "2026-08-13" in back[0] and "09:30:05" in back[0], f"时分秒被截掉了: {back[0]!r}"
    assert "2026-08-14" in back[1] and "10:01:12" in back[1], f"时分秒被截掉了: {back[1]!r}"


def test_selfcheck_survives_an_akshare_that_cannot_be_imported(monkeypatch, tmp_path):
    """akshare 装歪了（import 就抛异常），``selfcheck`` 照样正常返回，绝不自己抛栈。

    两条兜底（``except ImportError`` 与 ``except Exception``）随便哪条改成 ``raise``，
    全库一条不红——现有的 selfcheck 用例全是在健康环境里跑的。可 selfcheck 恰恰是**装歪依赖时
    第一个被跑的命令**：它自己抛一屏 traceback，等于把唯一的排障入口也堵上了，
    用户看到的是 ``AttributeError: module 'lxml.etree' has no attribute '_Element'``，
    完全指不到"akshare 的依赖坏了"。

    所以要求是：结构键一个不少（排障脚本照着键名读）、``探活失败`` 里带上**异常类型**
    （少了它没法判断是哪个依赖坏的）、``接口总数`` 照常给（注册表是纯本地的，跟 akshare 无关）。
    """
    keys = {"akshare 版本", "接口总数", "函数已不存在", "探活通过", "探活失败", "跳过_需要参数"}

    # 一、坏依赖：import 抛的**不是** ImportError
    broken = tmp_path / "brokenpath"
    broken.mkdir()
    (broken / "akshare.py").write_text(
        "raise AttributeError(\"module 'lxml.etree' has no attribute '_Element'\")\n",
        encoding="utf-8",
    )
    monkeypatch.delitem(sys.modules, "akshare", raising=False)
    monkeypatch.syspath_prepend(str(broken))
    importlib.invalidate_caches()

    result = selfcheck(probe=False)  # 不外抛本身就是断言的一部分

    assert set(result) == keys, "环境坏了也不许少给键：排障脚本是照着键名读的"
    assert result["接口总数"] == len(INTERFACES), "注册表是纯本地的，跟 akshare 装没装没关系"
    assert result["akshare 版本"] is None
    assert result["函数已不存在"] == [], "import 都没成，无从判断函数在不在，别乱扣帽子"
    assert result["探活通过"] == []
    assert len(result["探活失败"]) == 1
    assert "AttributeError" in result["探活失败"][0]["错误"], "要带上真实异常类型，否则不知道哪个依赖坏了"

    # 二、根本没装：import 抛的是 ImportError
    monkeypatch.setitem(sys.modules, "akshare", None)
    missing = selfcheck(probe=True)  # probe=True 也不许炸：它连 import 都没过去

    assert set(missing) == keys
    assert missing["探活通过"] == []
    assert len(missing["探活失败"]) == 1
    assert "未安装 akshare" in missing["探活失败"][0]["错误"], "没装就要直说，并给出 pip install 的话"


def test_legacy_cache_payload_still_gets_its_datetime_index_parsed(monkeypatch):
    """**旧格式**缓存行里的 DatetimeIndex，读回来仍然要是 datetime64，不能退化成字符串。

    ``_decode_payload`` 里的 ``convert_axes = not index_meta`` 改成写死 ``False``，
    全库一条不红——现有那条旧格式用例用的是平凡 RangeIndex 的表，两条轴猜不猜都一样。
    可 31 条里的 ``ths_industry_index`` 产出的正是 ``DatetimeIndex``，用户机器上那张
    升级前写好的 advanced_cache.db 里就躺着这样的行：侧车是 v3 之后才有的，
    老行只能继续让 pandas 猜轴。猜的那条路一关，index 读回来是一串 ISO 字符串，
    下游 ``df.loc['2026-08':]`` / ``.index.max()`` 全部走样，而且一声不吭。

    index 的 **name** 在旧格式里是找不回来的（``orient="split"`` 压根不写它），
    这里一并钉住那条已知边界：那正是 v3 加 index 侧车的理由。
    """
    index = pd.to_datetime(["2026-08-13", "2026-08-14"])
    index.name = "日期"
    legacy = pd.DataFrame({"收盘": [1234.5, 1250.0], "成交量": [100, 200]}, index=index)
    params = {"symbol": "元件"}
    cache_key = "ths_industry_index|" + json.dumps(params, sort_keys=True, ensure_ascii=False)

    with sqlite3.connect(str(advanced_data._cache_path())) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS advanced_cache (k TEXT PRIMARY KEY, ts REAL, payload TEXT)")
        conn.execute(
            "INSERT OR REPLACE INTO advanced_cache (k, ts, payload) VALUES (?, ?, ?)",
            # 旧格式：payload 就是裸的 split json，外面没有 {"v": ..., "index": ...} 那层壳
            (cache_key, time.time(), legacy.to_json(orient="split", force_ascii=False, date_format="iso")),
        )
        conn.commit()

    # 一碰就炸的假 akshare：读到了就一定是从这行旧格式缓存里读出来的
    monkeypatch.setitem(sys.modules, "akshare", _ExplodingAk())
    got = get_advanced_source().fetch("ths_industry_index", params)

    assert got is not None, "旧格式缓存被当成没命中，老库整个作废、每次都要去打上游"
    assert str(got.index.dtype).startswith("datetime64"), (
        f"DatetimeIndex 退化成了 {got.index.dtype}：下游按日期切片全会落空"
    )
    assert list(got.index) == list(index), "行索引的值也得对得上，不只是 dtype"
    assert got.index.name is None, "已知边界：旧格式里没有 index 侧车，name 找不回来（v3 加侧车就是为了这个）"


def test_cache_row_with_a_mismatched_column_sidecar_is_treated_as_a_miss(monkeypatch):
    """列名侧车的长度和实际列数对不上 = 缓存坏了，必须**干净地**判没命中。

    ``if len(columns) != len(frame.columns)`` 换成 ``if False``，全库一条不红：
    从 fetch 那一层看，两种走法都是"没命中、去打上游"——区别在于走守卫是
    ``return None``，不走守卫是 ``frame.columns = pd.Index(columns)`` 抛 ValueError，
    再被 ``_cache_get`` 的兜底 except 吞掉。**结果一样、代价不一样**：靠外层兜底
    等于把"我知道这行坏了"降级成"这次读缓存出了点什么事"，日志里连损坏的形状都没有；
    更要紧的是这条守卫哪天挪个位置（比如把 columns 赋值提到别处、或收紧那个 except），
    错位的表就会直接流到调用方手里——列名和数据对不上，静默算错。

    所以这条测试**直接钉 ``_decode_payload`` 的返回值**：它必须自己返回 None，
    而不是抛异常让别人接。外层那条断言只是顺带把契约（按未命中处理，返回新取的数据）写下来。
    """
    frame = pd.DataFrame({"代码": ["000001"], "名称": ["平安银行"]})
    payload = json.dumps(
        {
            "v": 3,
            "df": frame.to_json(orient="split", force_ascii=False, date_format="iso"),
            # 侧车比实际多一列：手工改过库、或者写到一半断电，都会留下这种行
            "columns": ["代码", "名称", "上游后来加的一列"],
            "types": {},
            "index": {"names": [None], "dtype": "int64"},
        },
        ensure_ascii=False,
    )

    assert advanced_data._decode_payload(payload) is None, (
        "损坏的缓存行没有被干净地判没命中——它是靠抛异常、再被外层 except 吞掉的"
    )

    cache_key = "hot_rank|" + json.dumps({}, sort_keys=True, ensure_ascii=False)
    with sqlite3.connect(str(advanced_data._cache_path())) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS advanced_cache (k TEXT PRIMARY KEY, ts REAL, payload TEXT)")
        conn.execute(
            "INSERT OR REPLACE INTO advanced_cache (k, ts, payload) VALUES (?, ?, ?)",
            (cache_key, time.time(), payload),
        )
        conn.commit()

    monkeypatch.setitem(
        sys.modules,
        "akshare",
        _FakeAk(stock_hot_rank_em=lambda **p: pd.DataFrame({"代码": ["600519"], "名称": ["贵州茅台"]})),
    )
    got = get_advanced_source().fetch("hot_rank")

    assert got is not None
    assert list(got.columns) == ["代码", "名称"], "拿到的是列名错位的表"
    assert got["代码"].tolist() == ["600519"], "损坏的缓存行被当成命中了"


# ==================== 58-63：东财的空信封与 akshare 的崩法 ====================
#
# 东财 datacenter-web 不用 HTTP 状态码表达失败：成功失败一律回 200，真正的话在 body 的
# ``code`` 里，而 ``result`` 一律是 null。akshare 拿到就直接下标 ``data_json["result"]["pages"]``，
# 于是**一个正常的没数据的日子**会抛 TypeError。这一组钉的就是"那之后该发生什么"：
# code 9201（返回数据为空）必须变成空表，其余码一律照旧按故障报出去。


_ENVELOPE_EMPTY = {"version": None, "success": False, "message": "返回数据为空", "code": 9201, "result": None}
_ENVELOPE_BUSY = {"version": None, "success": False, "message": "服务器繁忙", "code": 9701, "result": None}
_ENVELOPE_NO_REPORT = {
    "version": None,
    "success": False,
    "message": "报表配置不存在,RPT_X",
    "code": 9501,
    "result": None,
}


def _ak_crashing_on(envelope: dict[str, Any], partial: pd.DataFrame | None = None) -> Callable[..., pd.DataFrame]:
    """造一个**和 akshare 真实崩法一模一样**的桩。

    akshare 的东财翻页函数长这样::

        data_json = r.json()
        total_page = data_json["result"]["pages"]

    ``result`` 是 null 时就崩在第二句，而那份应答正以局部变量 ``data_json`` 的身份留在
    出错的帧里——取证读的就是它。所以桩必须**真的这么崩**：手工 ``raise TypeError(...)``
    的帧里没有 data_json，测出来的是另一件事，实现改坏了也照样绿。

    ``partial`` 模拟"翻页翻到一半才撞上空信封"：akshare 把已经攒下的页放在 ``big_df``。
    """

    def _call(**params: str) -> pd.DataFrame:
        big_df = pd.DataFrame() if partial is None else partial
        data_json = envelope
        total_page = data_json["result"]["pages"]  # ← 崩在这里，和 akshare 同一句
        return pd.concat([big_df, pd.DataFrame({"页数": [total_page]})], ignore_index=True)

    return _call


def test_empty_envelope_becomes_empty_frame_not_error(monkeypatch):
    """上游回 code 9201（返回数据为空）→ 空表 + 告警，**不是** None。

    这是本模块的立身之本：``None`` 只有一个含义——出故障了。而"这一天没有机构调研"
    是再正常不过的事，把它表达成 None，调用方照契约就得去报警。
    修之前 jgdy 每个没数据的日子都会这样误报一次。
    """
    monkeypatch.setitem(sys.modules, "akshare", _FakeAk(stock_jgdy_detail_em=_ak_crashing_on(_ENVELOPE_EMPTY)))
    src = get_advanced_source()

    out = src.fetch("jgdy", {"date": "20260824"})

    assert out is not None, "上游只是说没数据，不该被当成故障"
    assert out.empty
    assert src.last_error is None, "last_error 非空等于告诉调用方『出故障了』"
    assert any("空表" in w for w in src.warnings), "空表必须留一句告警，否则调用方不知道这次是空的"


def test_busy_envelope_still_reported_as_error(monkeypatch):
    """上游回 code 9701（服务器繁忙）→ 照旧 None + last_error。

    9701 和 9201 抛的是同一个 TypeError、走的是同一行代码，只有 code 不一样。
    要是判据松到"崩在 result:null 上就算空表"，hsgt_hold 那种**真的下线了**的接口
    就会天天返回空表，而调用方永远不会知道它已经取不到数据了。
    """
    monkeypatch.setitem(sys.modules, "akshare", _FakeAk(stock_jgdy_detail_em=_ak_crashing_on(_ENVELOPE_BUSY)))
    src = get_advanced_source()

    out = src.fetch("jgdy", {"date": "20260824"})

    assert out is None
    assert "TypeError" in (src.last_error or "")


def test_unknown_envelope_code_is_error_not_empty(monkeypatch):
    """不认识的 code（9501 报表配置不存在）→ 按故障处理。

    白名单而不是黑名单：只有明确认出的 9201 才算空，其余一律算故障。
    反过来写（"不是已知故障码就算空"）会让上游任何一个新错误码都被静默吞掉。
    """
    monkeypatch.setitem(sys.modules, "akshare", _FakeAk(stock_jgdy_detail_em=_ak_crashing_on(_ENVELOPE_NO_REPORT)))
    src = get_advanced_source()

    assert src.fetch("jgdy", {"date": "20260824"}) is None
    assert "TypeError" in (src.last_error or "")


def test_empty_envelope_midway_through_pagination_is_error(monkeypatch):
    """翻页翻到一半才撞上空信封（前面几页已有真数据）→ 必须报故障，不能当空表。

    这是被限流截断的样子：拿到的是**半份**数据。把半份说成"一份完整的空"比报错危险得多——
    调用方拿到空表会安静跳过，而真相是这批数据缺了一大块，且毫无线索。
    """
    partial = pd.DataFrame({"代码": ["000001", "600519"], "名称": ["平安银行", "贵州茅台"]})
    monkeypatch.setitem(
        sys.modules, "akshare", _FakeAk(stock_jgdy_detail_em=_ak_crashing_on(_ENVELOPE_EMPTY, partial=partial))
    )
    src = get_advanced_source()

    out = src.fetch("jgdy", {"date": "20260818"})

    assert out is None, "前面几页有数据，这次空信封是截断而不是『今天没数据』"
    assert "TypeError" in (src.last_error or "")


def test_crash_without_envelope_evidence_is_error(monkeypatch):
    """同样是 TypeError，但帧里没有 data_json（比如连接被重置）→ 按故障处理。

    判据必须**看见证据**才敢说空，不能凭异常类型猜。akshare 内部还有别的地方会抛
    TypeError，认类型不认证据的话，那些真故障会被一并吞成空表。
    """

    def _boom(**params: str) -> pd.DataFrame:
        raise TypeError("'NoneType' object is not subscriptable")

    monkeypatch.setitem(sys.modules, "akshare", _FakeAk(stock_jgdy_detail_em=_boom))
    src = get_advanced_source()

    assert src.fetch("jgdy", {"date": "20260824"}) is None
    assert "TypeError" in (src.last_error or "")


def test_empty_envelope_result_is_not_cached(monkeypatch):
    """空信封判成的空表同样不写缓存——和其它空表一视同仁。

    jgdy 的 TTL 是 24 小时。要是把这张空表缓存住，当天下午上游有数据了也取不到，
    一次"早上还没披露"会被固化成"今天全天没数据"。
    """
    monkeypatch.setitem(sys.modules, "akshare", _FakeAk(stock_jgdy_detail_em=_ak_crashing_on(_ENVELOPE_EMPTY)))
    src = get_advanced_source()
    params = {"date": "20260824"}

    first = src.fetch("jgdy", params)
    assert first is not None and first.empty

    # 换成一碰就炸的假 akshare：再取一次如果没炸，就说明它命中了缓存
    monkeypatch.setitem(sys.modules, "akshare", _ExplodingAk())
    with pytest.raises(AssertionError, match="akshare.stock_jgdy_detail_em"):
        src.fetch("jgdy", params)
