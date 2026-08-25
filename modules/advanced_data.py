"""高级数据层：资金面 / 情绪面 / 结构面 / 风险面 / 消息面 / 技术榜。

为什么单独开一层，而不是往 ``modules/datasource.py`` 的 ``DataSource`` 里加方法——
因为这两层是**同级伙伴，不是父子**。

``DataSource`` 管行情主线：Tushare 商业接口，有 SLA，字段稳定，失败模式就那么两种
（限流、token 过期），重试或换 token 就能恢复。在那一层，"取不到数据"基本等价于
"出故障了"。

本模块管的是本地 Tushare 账号**无访问权限**的那几个面：资金面（龙虎榜、资金流、北向持股）、
情绪面（涨停/炸板/跌停池、人气榜）、结构面（行业概念板块与成份）、风险面（限售解禁、
机构调研）、消息面（财经直播、电报）、技术榜（同花顺技术选股）。数据来自 akshare 封装的
东财 / 同花顺 / 财联社**公开网页接口**：上游会改函数名、会改列名、会封 IP，时效从 10 分钟
（财经直播）到 30 天（主营介绍）不等，非交易日返回空表更是家常便饭。在这一层，
"取不到数据"多半是正常的。

可得性、时效、失败模式全不一样，混进同一个 Protocol，调用方就分不清"这次没数据是正常的"
和"这次没数据是出问题了"——而这两件事在选股流程里的处置恰好相反：前者该安静跳过，
后者该报警。所以这里另起一个 ``AdvancedDataSource`` 协议，并且把三种结果在 API 上分开表达：

- 真错误      → 返回 ``None``，写 ``last_error``
- 空表 / 缺列 → 照常返回 DataFrame，把话记进 ``warnings``

绝不让 ``None`` 静默流进选股逻辑，也绝不因为上游少给一列就把整批数据扔掉。

限流的边界也在这里说清楚：按 ``source`` 分桶（东财和同花顺是两套服务器，不该互相拖累），
"上次调用时刻"落在缓存那张 sqlite 小库里，用 ``BEGIN IMMEDIATE`` 划临界区——所以**同一台
机器上的多个独立进程**（zt / zt-web / zt-monitor / cron 脚本）是真的会被串成最小间隔的。
但**跨机器不协调**：两台机子各有各的缓存库，各限各的，那需要 Redis 之类的外部协调者。
"""

from __future__ import annotations

import copy
import datetime
import io
import json
import logging
import math
import multiprocessing
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, Protocol, runtime_checkable

import pandas as pd

logger = logging.getLogger(__name__)


# ==================== 常量 ====================

CATEGORY_CAPITAL = "资金面"
CATEGORY_SENTIMENT = "情绪面"
CATEGORY_STRUCTURE = "结构面"
CATEGORY_RISK = "风险面"
CATEGORY_NEWS = "消息面"
CATEGORY_RANK = "技术榜"

# 顺序固定：CLI 和 MCP 的展示顺序直接取这里，调换会让人以为分组变了
CATEGORIES: tuple[str, ...] = (
    CATEGORY_CAPITAL,
    CATEGORY_SENTIMENT,
    CATEGORY_STRUCTURE,
    CATEGORY_RISK,
    CATEGORY_NEWS,
    CATEGORY_RANK,
)

SOURCE_EM = "东财"
SOURCE_THS = "同花顺"
SOURCE_CLS = "财联社"

# 同一个源两次请求之间的最小间隔（秒）。公开网页接口没有配额文档，只有封 IP，
# 所以宁可慢一点。测试把 ADVANCED_MIN_INTERVAL 设成 "0" 来关掉等待。
DEFAULT_MIN_INTERVAL = 0.6
# 间隔的上界：ADVANCED_MIN_INTERVAL 手滑写成 3600（把"秒"当成"每小时一次"）
# 会让整条流水线挂在限流器里，夹住比让它挂死好
MAX_MIN_INTERVAL = 60.0
ENV_MIN_INTERVAL = "ADVANCED_MIN_INTERVAL"
ENV_CACHE_PATH = "ADVANCED_CACHE_PATH"

# 东财 datacenter-web（``/api/data/v1/get``）的应答码。这套接口**不用 HTTP 状态码**
# 表达失败：无论成功失败都回 200，真正的话在 body 的 ``code`` 里，而 ``result`` 一律是 null。
# 【2026-08-25 实测】0=ok / 9201=返回数据为空 / 9501=报表配置不存在 / 9701=服务器繁忙。
# 只认 code，不认 message——message 是给人看的中文，改一个字这里就失灵。
EM_CODE_EMPTY = 9201


# ==================== 接口注册表 ====================


_PARAMS_READONLY = "Interface.params 是只读的：它是进程级共享的参数白名单，就地改会污染到所有调用方"


class _FrozenParams(dict[str, str]):
    """只读的参数说明表：既挡住就地修改，又保持 ``isinstance(x, dict)`` 为真。

    为什么不用 ``types.MappingProxyType``：它确实只读，但 ``isinstance(proxy, dict)`` 是
    **False**，会把"params 是参数名 -> 中文说明的 dict"这条对外契约打掉（照着契约写的调用方
    和测试全得改）；而且 mappingproxy 自己也不可 hash，``hash(Interface)`` 照样报错。
    继承 dict 再把所有写入口封死，是同时满足"只读 + 还是 dict + 可 hash"的唯一做法。
    """

    __slots__ = ()

    def __setitem__(self, key: str, value: str) -> NoReturn:
        raise TypeError(_PARAMS_READONLY)

    def __delitem__(self, key: str) -> NoReturn:
        raise TypeError(_PARAMS_READONLY)

    def update(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise TypeError(_PARAMS_READONLY)

    def setdefault(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise TypeError(_PARAMS_READONLY)

    def pop(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise TypeError(_PARAMS_READONLY)

    def popitem(self) -> NoReturn:
        raise TypeError(_PARAMS_READONLY)

    def clear(self) -> NoReturn:
        raise TypeError(_PARAMS_READONLY)

    def __ior__(self, other: Any) -> NoReturn:  # type: ignore[misc]
        raise TypeError(_PARAMS_READONLY)

    def __hash__(self) -> int:  # type: ignore[override]
        return hash(tuple(sorted(self.items())))

    def __copy__(self) -> dict[str, str]:
        # 拷贝出来的应当是可以随便改的普通 dict——只读的是注册表那一份，不是调用方手里的副本
        return dict(self)

    def __deepcopy__(self, memo: dict[int, Any]) -> dict[str, str]:
        # 必须显式写出来，不能靠默认实现：``copy.deepcopy`` 对 dict 子类走 ``_reconstruct``，
        # 重建时**逐条调 __setitem__**，正好撞上上面封死的写入口，直接抛 TypeError。
        # 语义跟 __copy__ 对齐——副本是普通的可写 dict（只读的是注册表那一份）。
        plain = {copy.deepcopy(k, memo): copy.deepcopy(v, memo) for k, v in self.items()}
        memo[id(self)] = plain
        return plain

    def __reduce__(self) -> tuple[Any, ...]:
        # 同一个坑的 pickle 版，而且更阴：``pickle.dumps`` **不报警**（461 字节照样写出来），
        # 炸在 ``pickle.loads`` 那一侧——默认的 dict 子类 reduce 会带一个"逐条 setitem"的
        # items 迭代器，反序列化时撞上只读入口。放到 ProcessPoolExecutor 里（本仓库
        # ``zt replay --workers N`` 就是），症状是 BrokenProcessPool，报错文案完全指错方向。
        # 改成走构造函数：``_FrozenParams(dict)`` 里的 dict.__init__ 是 C 层批量插入，
        # 不经过 __setitem__。跨进程传过去的仍然是只读的 _FrozenParams——它代表的是
        # 进程级共享的白名单，子进程里也该是只读的（要可写副本请用 copy/deepcopy）。
        return (self.__class__, (dict(self),))


@dataclass(frozen=True)
class Interface:
    """一条 akshare 接口的登记信息。

    ``key`` 与 ``func`` 分开是刻意的：akshare 上游改函数名是静默的、且改得很勤，
    改名时只需要动 ``func`` 一个字段，``key`` 对外永远保持稳定——调用方（CLI、MCP 工具、
    agent 的历史会话里写死的接口名）一律只认 ``key``，不受上游改名影响。
    """

    key: str  # 对外稳定名，调用方只认它
    func: str  # akshare 函数名，上游改名时只改这里
    category: str
    desc: str
    params: dict[str, str]  # 参数名 -> 中文说明；空 dict = 无参接口（只读，见 _FrozenParams）
    expect: tuple[str, ...]  # 期望列，用于列校验；空 tuple = 不校验
    ttl: int  # 缓存有效期，单位秒
    source: str

    def __post_init__(self) -> None:
        # ``frozen=True`` 只挡住"把 params 这个字段整个换掉"，挡不住 params 自己被就地改：
        # ``BY_KEY['zt_pool'].params['evil'] = 'x'`` 是实测能成功的，而 INTERFACES 是模块级、
        # 进程内共享的参数白名单，污染一次全进程遭殃。所以这里换成只读的 _FrozenParams。
        object.__setattr__(self, "params", _FrozenParams(self.params))


# TTL 的量纲是秒，按"数据多久才会变"给：
#   600 = 10 分钟（滚动新闻）  1800 = 30 分钟（人气榜）  21600 = 6 小时（盘中资金流排名）
#   43200 = 12 小时（收盘后才定稿的日频数据）  86400 = 1 天  604800 = 7 天（板块成份）
#   2592000 = 30 天（主营介绍这类几乎不动的资料）
INTERFACES: list[Interface] = [
    # -------------------- 资金面 --------------------
    Interface(
        key="lhb_detail",
        func="stock_lhb_detail_em",
        category=CATEGORY_CAPITAL,
        desc="一段日期区间内的龙虎榜每日明细，回答『这几天哪些票被打上榜、上榜理由是什么、净买多少』——找游资接力和机构抢筹的起点。",
        params={
            "start_date": "起始交易日 YYYYMMDD，如 20260810",
            "end_date": "结束交易日 YYYYMMDD（含当日），如 20260814",
        },
        expect=("代码", "名称", "上榜日", "解读", "龙虎榜净买额"),
        ttl=43200,
        source=SOURCE_EM,
    ),
    Interface(
        key="lhb_stock",
        func="stock_lhb_stock_detail_em",
        category=CATEGORY_CAPITAL,
        desc="单只股票某个上榜日的买卖席位明细，回答『这一板是谁买的』——分辨知名游资、机构专用还是量化席位。",
        params={
            "symbol": "6 位股票代码，不带交易所后缀，如 600519",
            "date": "该股实际上榜的交易日 YYYYMMDD；不是任意日期都有数据",
            "flag": "席位方向：买入 / 卖出",
        },
        expect=(),
        ttl=43200,
        source=SOURCE_EM,
    ),
    Interface(
        key="lhb_jgmmtj",
        func="stock_lhb_jgmmtj_em",
        category=CATEGORY_CAPITAL,
        desc="区间内机构专用席位的买卖统计，回答『机构这几天在净买谁、净卖谁』——比营业部席位更能反映中长线资金态度。",
        params={
            "start_date": "起始交易日 YYYYMMDD，如 20260810",
            "end_date": "结束交易日 YYYYMMDD（含当日），如 20260814",
        },
        expect=("代码", "名称"),
        ttl=43200,
        source=SOURCE_EM,
    ),
    Interface(
        key="lhb_hyyyb",
        func="stock_lhb_hyyyb_em",
        category=CATEGORY_CAPITAL,
        desc="区间内的活跃营业部排行及其买入个股，回答『当下哪几家游资席位最活跃、手伸向了哪些票』。",
        params={
            "start_date": "起始交易日 YYYYMMDD，如 20260810",
            "end_date": "结束交易日 YYYYMMDD（含当日），如 20260814",
        },
        expect=(),
        ttl=43200,
        source=SOURCE_EM,
    ),
    Interface(
        key="fund_flow_stock",
        func="stock_individual_fund_flow",
        category=CATEGORY_CAPITAL,
        desc="单只股票逐日的主力/超大单/大单/中单/小单资金流向，回答『这波上涨到底是主力在买还是散户在抬』。",
        params={
            "stock": "6 位股票代码，不带交易所后缀，如 000001",
            "market": "所属市场：sh（沪） / sz（深） / bj（北），必须与代码一致",
        },
        expect=("日期", "主力净流入-净额"),
        ttl=43200,
        source=SOURCE_EM,
    ),
    Interface(
        key="fund_flow_rank",
        func="stock_individual_fund_flow_rank",
        category=CATEGORY_CAPITAL,
        desc="全市场个股资金流入排名，回答『最近几天钱在往哪些票里进』——做主线验证和票池粗筛的原料。",
        params={"indicator": "统计周期：今日 / 3日 / 5日 / 10日"},
        expect=("代码", "名称"),
        ttl=21600,
        source=SOURCE_EM,
    ),
    Interface(
        key="hsgt_hold",
        func="stock_hsgt_hold_stock_em",
        category=CATEGORY_CAPITAL,
        desc=(
            "沪深港通（北向）持股个股排行，回答『外资近期在加仓谁、减仓谁』——外资是持仓周期最长的一类增量资金。"
            "【2026-08 实测】上游 datacenter-web 返回 HTTP 200 但 body 是 "
            '{"success":false,"message":"服务器繁忙","result":null}，akshare 直接对 None 下标抛 TypeError；'
            "market 换 北向 / 沪股通 / 深股通 表现一致，沪深港通持股报表疑似已下线。"
            "注册**刻意保留**：上游恢复即可用，删掉反而要改代码。"
        ),
        params={
            "market": "口径：北向 / 沪股通 / 深股通",
            "indicator": "排行周期：今日排行 / 3日排行 / 5日排行 / 10日排行 / 月排行 / 季排行 / 年排行",
        },
        expect=(),
        ttl=43200,
        source=SOURCE_EM,
    ),
    # -------------------- 情绪面 --------------------
    Interface(
        key="zt_pool",
        func="stock_zt_pool_em",
        category=CATEGORY_SENTIMENT,
        desc="当日涨停池，含连板数 / 封板资金 / 首次封板时间，是连板梯队和市场高度的原料。",
        params={"date": "交易日 YYYYMMDD，如 20260814；非交易日返回空表属正常"},
        expect=("代码", "名称", "连板数", "封板资金", "首次封板时间"),
        ttl=43200,
        source=SOURCE_EM,
    ),
    Interface(
        key="zt_pool_prev",
        func="stock_zt_pool_previous_em",
        category=CATEGORY_SENTIMENT,
        desc="昨日涨停股今日的表现，回答『昨天的板今天承接住了没』——打板赚钱效应最直接的温度计。",
        params={"date": "交易日 YYYYMMDD，如 20260814；看的是这一天相对前一日涨停股的表现"},
        expect=("代码", "名称"),
        ttl=43200,
        source=SOURCE_EM,
    ),
    Interface(
        key="zt_pool_zbgc",
        func="stock_zt_pool_zbgc_em",
        category=CATEGORY_SENTIMENT,
        desc="当日炸板股池（触板未封住），回答『今天有多少板没守住』——炸板率高说明资金接力意愿在衰竭。",
        params={"date": "交易日 YYYYMMDD，如 20260814"},
        expect=("代码", "名称"),
        ttl=43200,
        source=SOURCE_EM,
    ),
    Interface(
        key="zt_pool_strong",
        func="stock_zt_pool_strong_em",
        category=CATEGORY_SENTIMENT,
        desc="当日强势股池（涨幅居前但未涨停），回答『除了涨停板，还有哪些票在被资金照顾』——次日的候选梯队。",
        params={"date": "交易日 YYYYMMDD，如 20260814"},
        expect=("代码", "名称"),
        ttl=43200,
        source=SOURCE_EM,
    ),
    Interface(
        key="dt_pool",
        func="stock_zt_pool_dtgc_em",
        category=CATEGORY_SENTIMENT,
        desc="当日跌停池，回答『恐慌盘集中在哪里、有多少票躺在跌停上』——和涨停池对照看情绪的两端。",
        params={"date": "交易日 YYYYMMDD，如 20260814"},
        expect=("代码", "名称"),
        ttl=43200,
        source=SOURCE_EM,
    ),
    Interface(
        key="hot_rank",
        func="stock_hot_rank_em",
        category=CATEGORY_SENTIMENT,
        desc="东财股吧人气榜实时排名，回答『散户的注意力当下在哪几只票上』——散户关注度是情绪的滞后指标，用来识别高位接盘风险。",
        params={},
        expect=(),
        ttl=1800,
        source=SOURCE_EM,
    ),
    # -------------------- 结构面 --------------------
    Interface(
        key="ths_industry_list",
        func="stock_board_industry_name_ths",
        category=CATEGORY_STRUCTURE,
        desc="同花顺全部行业板块的名称与代码，回答『行业维度一共有哪些格子』——其它同花顺行业接口的 symbol 都从这里取。",
        params={},
        expect=(),
        ttl=604800,
        source=SOURCE_THS,
    ),
    Interface(
        key="ths_concept_list",
        func="stock_board_concept_name_ths",
        category=CATEGORY_STRUCTURE,
        desc="同花顺全部概念板块的名称与代码，回答『当前市场承认哪些概念』——新概念的出现本身就是主线信号。",
        params={},
        expect=(),
        ttl=604800,
        source=SOURCE_THS,
    ),
    Interface(
        key="ths_concept_info",
        func="stock_board_concept_info_ths",
        category=CATEGORY_STRUCTURE,
        desc="概念板块的简介，回答『这个概念到底在讲什么故事、逻辑从哪来』——判断题材真假与持续性的文字依据。",
        params={"symbol": "概念板块名称，取自 ths_concept_list，如 阿里巴巴概念"},
        expect=(),
        ttl=604800,
        source=SOURCE_THS,
    ),
    Interface(
        key="ths_industry_index",
        func="stock_board_industry_index_ths",
        category=CATEGORY_STRUCTURE,
        desc="行业板块指数的历史行情，回答『这个行业本身走得怎么样』——个股再强也要看板块指数在不在上升趋势里。",
        params={
            "symbol": "行业板块名称，取自 ths_industry_list，如 元件",
            "start_date": "起始日 YYYYMMDD，如 20260101",
            "end_date": "结束日 YYYYMMDD，如 20260814",
        },
        expect=(),
        ttl=43200,
        source=SOURCE_THS,
    ),
    Interface(
        key="em_industry_cons",
        func="stock_board_industry_cons_em",
        category=CATEGORY_STRUCTURE,
        desc="东财行业板块的成份股，回答『这个行业里都有谁』——把主线强弱落到具体票池的映射表。",
        params={"symbol": "东财行业板块名称或板块代码，如 小金属 / BK1027"},
        expect=("代码", "名称"),
        ttl=604800,
        source=SOURCE_EM,
    ),
    Interface(
        key="em_concept_cons",
        func="stock_board_concept_cons_em",
        category=CATEGORY_STRUCTURE,
        desc="东财概念板块的成份股，回答『这个题材里都有谁』——题材发酵时用来找同板块的补涨对象。",
        params={"symbol": "东财概念板块名称或板块代码，如 融资融券 / BK0655"},
        expect=("代码", "名称"),
        ttl=604800,
        source=SOURCE_EM,
    ),
    Interface(
        key="zyjs",
        func="stock_zyjs_ths",
        category=CATEGORY_STRUCTURE,
        desc="个股主营业务介绍，回答『这家公司到底靠什么赚钱』——核对题材归属是真业务还是蹭概念。",
        params={"symbol": "6 位股票代码，不带交易所后缀，如 000066"},
        expect=(),
        ttl=2592000,
        source=SOURCE_THS,
    ),
    # -------------------- 风险面 --------------------
    Interface(
        key="restricted_queue",
        func="stock_restricted_release_queue_em",
        category=CATEGORY_RISK,
        desc="单只股票未来的限售解禁批次队列，回答『这只票什么时候有多少股要解禁』——建仓前必查的时间地雷。",
        params={"symbol": "6 位股票代码，不带交易所后缀，如 600000"},
        expect=(),
        ttl=604800,
        source=SOURCE_EM,
    ),
    Interface(
        key="restricted_detail",
        func="stock_restricted_release_detail_em",
        category=CATEGORY_RISK,
        desc="一段日期区间内全市场的解禁明细，回答『下个月哪些票有大额解禁』——用来批量排雷，而不是逐票去查。",
        params={
            "start_date": "起始日 YYYYMMDD，如 20260901",
            "end_date": "结束日 YYYYMMDD，如 20261231",
        },
        expect=(),
        ttl=86400,
        source=SOURCE_EM,
    ),
    Interface(
        key="jgdy",
        func="stock_jgdy_detail_em",
        category=CATEGORY_RISK,
        desc="机构调研详细记录，回答『机构最近去看了哪些公司、去了多少家』——密集调研常常领先于机构建仓。",
        params={
            "date": (
                "起点日 YYYYMMDD，如 20260818。取的是这一天之后的全部记录，不是这一天当天；"
                "akshare 会把命中的页全部翻完，所以日期往前挪的代价不是线性的。"
                "【2026-08-25 实测】页数：0824 空 / 0818 有 144 页 / 0101 有 3247 页。"
            )
        },
        expect=(),
        ttl=86400,
        source=SOURCE_EM,
    ),
    # -------------------- 消息面 --------------------
    Interface(
        key="ths_global_news",
        func="stock_info_global_ths",
        category=CATEGORY_NEWS,
        desc="同花顺全球财经直播的最新快讯，回答『刚刚发生了什么』——盘中突发消息的第一落点。",
        params={},
        expect=(),
        ttl=600,
        source=SOURCE_THS,
    ),
    Interface(
        key="cls_telegraph",
        func="stock_info_global_cls",
        category=CATEGORY_NEWS,
        desc="财联社电报，回答『有哪些和 A 股直接相关的政策/产业消息』——题材发酵的常见引信。",
        params={"symbol": "频道：全部 / 重点"},
        expect=(),
        ttl=600,
        source=SOURCE_CLS,
    ),
    # -------------------- 技术榜 --------------------
    #
    # 注意：ths_rank_ljqs / ths_rank_lxsz / ths_rank_cxfl 三条登记成**无参**，是照着
    # akshare 1.18.88 的真实签名来的（inspect.signature 实测：这三个函数一个参数都不收）。
    # 上游文档和 INTERFACES 的早期版本都写着 symbol，那是过时信息——params 是对外广告的
    # 白名单，登记了不存在的参数，MCP / agent 看了就会传，传了必 TypeError。
    # 哪天上游把参数加回来（历史上加过又删过），再往 params 里补，key 不用动。
    Interface(
        key="ths_rank_cxg",
        func="stock_rank_cxg_ths",
        category=CATEGORY_RANK,
        desc="创新高个股榜，回答『哪些票刚创出新高』——趋势票的入口，配合主线看是不是板块性突破。",
        params={"symbol": "新高口径：创月新高 / 半年新高 / 一年新高 / 历史新高"},
        expect=(),
        ttl=43200,
        source=SOURCE_THS,
    ),
    Interface(
        key="ths_rank_xstp",
        func="stock_rank_xstp_ths",
        category=CATEGORY_RANK,
        desc="向上突破均线个股榜，回答『哪些票今天站上了关键均线』——趋势由弱转强的常见起点。",
        params={
            "symbol": "均线：5日均线 / 10日均线 / 20日均线 / 30日均线 / 60日均线 / 90日均线 / 250日均线 / 500日均线"
        },
        expect=(),
        ttl=43200,
        source=SOURCE_THS,
    ),
    Interface(
        key="ths_rank_ljqs",
        func="stock_rank_ljqs_ths",
        category=CATEGORY_RANK,
        desc="量价齐升个股榜，回答『哪些票是放量涨的』——量能配合的上涨才有资金在真金白银地进。周期口径由上游固定，不接受参数。",
        params={},
        expect=(),
        ttl=43200,
        source=SOURCE_THS,
    ),
    Interface(
        key="ths_rank_lxsz",
        func="stock_rank_lxsz_ths",
        category=CATEGORY_RANK,
        desc="连续上涨个股榜，回答『哪些票在连阳』——连续上涨天数是短线强度的直观刻度。周期口径由上游固定，不接受参数。",
        params={},
        expect=(),
        ttl=43200,
        source=SOURCE_THS,
    ),
    Interface(
        key="ths_rank_cxfl",
        func="stock_rank_cxfl_ths",
        category=CATEGORY_RANK,
        desc="持续放量个股榜，回答『哪些票的成交量在连续放大』——放量往往先于价格突破出现。周期口径由上游固定，不接受参数。",
        params={},
        expect=(),
        ttl=43200,
        source=SOURCE_THS,
    ),
    Interface(
        key="ths_rank_xzjp",
        func="stock_rank_xzjp_ths",
        category=CATEGORY_RANK,
        desc="险资举牌记录，回答『保险资金举牌了谁』——最慢也最挑剔的一类资金，是长线价值的旁证。",
        params={},
        expect=(),
        ttl=86400,
        source=SOURCE_THS,
    ),
]


BY_KEY: dict[str, Interface] = {i.key: i for i in INTERFACES}


def catalog() -> dict[str, list[dict]]:
    """按 category 分组的接口目录，供 CLI 和 MCP 展示。

    返回 ``{category: [{key, func, desc, params, expect, ttl, source}, ...]}``。
    六个分组一定齐全（某组为空也保留键），各组条目数之和 == ``len(INTERFACES)``。
    """
    grouped: dict[str, list[dict]] = {c: [] for c in CATEGORIES}
    for i in INTERFACES:
        grouped.setdefault(i.category, []).append(
            {
                "key": i.key,
                "func": i.func,
                "desc": i.desc,
                "params": dict(i.params),
                "expect": list(i.expect),
                "ttl": i.ttl,
                "source": i.source,
            }
        )
    return grouped


# ==================== 缓存 ====================
#
# 刻意不写进 data/stock_data.db：主库已经有并发写锁的压力，这些十几分钟就作废的
# 网页数据不值得去和它抢锁。单独一个小库，每次开短连接、用完即关，跨进程跨线程都安全。


def _cache_path() -> Path:
    """缓存库路径。**每次调用时解析**——测试要 monkeypatch 环境变量。"""
    override = os.environ.get(ENV_CACHE_PATH)
    if override:
        path = Path(override)
        # 允许把环境变量指向一个目录（测试常直接指 tmp_path），那就在里面放默认文件名
        if path.is_dir():
            return path / "advanced_cache.db"
        return path
    return Path(os.environ.get("DATA_DIR", "data")) / "advanced_cache.db"


def _cache_connect() -> sqlite3.Connection | None:
    """开一个缓存连接并保证建表；任何失败都返回 None（缓存坏了不许阻断主流程）。"""
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE IF NOT EXISTS advanced_cache (k TEXT PRIMARY KEY, ts REAL, payload TEXT)")
        conn.commit()
        return conn
    except Exception as exc:
        logger.debug("高级数据缓存不可用（%s）：%s", path, exc)
        return None


# ---------- 序列化：df 的 split json + 一份"类型侧车" ----------
#
# 光靠 to_json/read_json 是**保不住类型**的，而且丢了还不报错——这一层最会伤人的坑：
#
# 1. ``date_format="iso"`` 只作用于 datetime64 dtype 的列。akshare 大量返回的是
#    **object 列里装 datetime.date / datetime.time 对象**（lhb_detail.上榜日、
#    fund_flow_stock.日期、zt_pool.首次封板时间……31 条里有 11 条中招），写出去是 ISO 字符串，
#    配 ``dtype=False`` 读回来就变成字符串，一去不回。下游 ``df[df["日期"] == date(...)]``
#    首次 1 行、命中缓存 0 行，**无异常、无告警、结果直接是错的**；datetime64 列更狠，
#    变成 object 之后 ``.dt`` 访问器直接抛 AttributeError。
# 2. ``dtype=False`` 只关了**数据**的类型推断，``convert_axes`` 还开着，**两条轴**照样被改：
#    ``['000001','600519'] -> [1, 600519]``、``['2026-08-14',...] -> [Timestamp(...), ...]``、
#    重复列名 ``['代码','代码'] -> ['代码','代码.1']``。当前 31 条接口的列名都是硬编码中文，
#    所以现在不会真害到人，但上游一改就静默发作。
# 3. ``orient="split"`` 写出去的 JSON **不带 index 的 name**。31 条里 ths_industry_index
#    产出的是 ``name='日期'`` 的 DatetimeIndex，往返一圈 name 变 None，而 ``df.equals()``
#    **不比较 index name**，测不出来。症状要到下游才发作：``fresh.reset_index()`` 抛
#    ``ValueError: cannot insert 日期, already exists``，``cached.reset_index()`` 却安安静静
#    给出一列叫 ``index`` 的表——同一份数据两种列名，静默走岔。
#
# 所以 payload 不再是裸的 split json，而是把「split json + 原始列名 + 需要还原的列类型
# + index 的 name 与 dtype」一起存：写的时候记下来，读的时候还原。
#
# 契约（说准一点，免得被当成比实际更强的保证）：**对 akshare 真实产出的表**，同一次 fetch
# 走网络与走缓存返回的 DataFrame 必须 ``.equals()`` 为 True、逐列 dtype 相同、且
# ``index.name`` 相同（最后这条 ``.equals()`` 查不到，得单独断言）。
# 唯一的例外是缺失值的**长相**：JSON 只有一个 ``null``，日期列里的 ``None`` 读回来一律是
# ``NaT``。这对真实数据是精确的——产出日期列的那 11 条接口，akshare 源码里全是
# ``pd.to_datetime(..., errors="coerce").dt.date``，它们的空值本来就是 ``NaT``；
# 只有**手工构造**的 ``None`` / ``float nan`` 才会在往返后变成 ``NaT``。
# 详见 _restore_types 的 docstring，那是有意为之的边界，不是待修的 bug。
#
# 顺带把 read_json 的 ``convert_axes`` 关掉：两条轴现在都由侧车精确还原，不需要 pandas 去猜；
# 而且它猜的时候内部走的是 ``to_datetime(..., unit=date_unit)``，pandas 2.x 会为此报
# FutureWarning（"strings 将来会按日期串解析"）。那句 warning 在 ``-W error`` 下会变成异常，
# 被 ``_cache_get`` 的兜底 except 吞成"缓存没命中"——于是**每次都去打上游**，缓存等于没有。

# v2 -> v3 加的是 index 侧车（name + dtype），并把 read_json 的 convert_axes 关掉。
# 老版本的 payload 仍然读得出来，见 _decode_payload：v3 之前的行没有 index 侧车，
# 只能继续让 pandas 猜轴（convert_axes=True），行为跟升级前逐字节一致，不会因为一次
# 格式升级把用户已有的缓存全变成硬错误；它们会随 TTL 自然换成 v3。
_PAYLOAD_VERSION = 3

_TYPE_DATE = "date"  # object 列，装的是 datetime.date
_TYPE_TIME = "time"  # object 列，装的是 datetime.time
_TYPE_DATETIME64 = "datetime64"  # 真正的 datetime64[ns] 列


def _infer_types(df: pd.DataFrame) -> dict[str, str]:
    """推断哪些列需要在读缓存时还原，返回 ``{列名: 类型标记}``。

    只认三种：datetime64 dtype、全是 ``datetime.date`` 的 object 列、全是 ``datetime.time``
    的 object 列。认不出来的一律不管——宁可少还原一列（读回来是字符串，看得见），
    也不要瞎还原（把代码列当日期解析掉，看不见）。
    """
    types: dict[str, str] = {}
    for pos, name in enumerate(df.columns):
        if not isinstance(name, str):
            # 列名不是字符串就没法写进 JSON 的对象键，跳过（这种列名本身也走不到写缓存那步）
            continue
        col = df.iloc[:, pos]
        if str(col.dtype).startswith("datetime64"):
            types[name] = _TYPE_DATETIME64
            continue
        if col.dtype != object:
            continue
        values = [v for v in col.tolist() if v is not None and v is not pd.NaT and not _is_nan(v)]
        if not values:
            continue
        # datetime.datetime 是 datetime.date 的子类，必须显式排掉：
        # 它的 ISO 串带时分秒，还原成 .dt.date 会把时间截掉
        if all(isinstance(v, datetime.date) and not isinstance(v, datetime.datetime) for v in values):
            types[name] = _TYPE_DATE
        elif all(isinstance(v, datetime.time) for v in values):
            types[name] = _TYPE_TIME
    return types


def _is_nan(value: Any) -> bool:
    return isinstance(value, float) and value != value


def _restore_types(frame: pd.DataFrame, types: dict[str, str]) -> None:
    """按类型侧车把日期/时间列还原回去（就地改 ``frame``）。

    单列还原失败就保留字符串并 ``logger.debug``——**绝不能因此让整次缓存读取失败**：
    读不出来最多是多打一次上游，抛出去却会让调用方拿到 None。
    按**列位置**逐列改而不是 ``frame[name]``，是因为列名可能重复，按名字取会拿到子表。

    **缺失值一律还原成 NaT，这是定下来的契约，不是待修的 bug。**JSON 把 ``None`` /
    ``NaN`` / ``NaT`` 统统写成 ``null``，还原时只能挑一个，这里挑 ``NaT``。理由是它对
    **真实数据是精确的**：31 条里产出日期列的那 11 条，akshare 源码里全是
    ``pd.to_datetime(..., errors="coerce").dt.date``，coerce 出来的空值本来就是 ``NaT``，
    所以照真实形态构造的表往返后 ``.equals()`` 就是 True。

    代价只落在**手工构造**的表上：自己写的 ``None`` / ``float nan`` 往返后变 ``NaT``，
    ``.equals()`` 判 False。值本身和 dtype 都是对的，``pd.isna()`` 也照样为真，不会静默算错。
    真正要防的是"日期变字符串"那种一去不回的损坏，为此拿掉 None/NaT 的区分是划算的——
    要还原得回 ``None``，就得在侧车里逐行记下每个空值原来是什么，为一个下游根本不关心的
    区别付整表的存储和复杂度。
    tests 里 ``test_cache_round_trip_matches_akshare_style_missing_dates`` 与
    ``test_manual_none_in_a_date_column_becomes_nat`` 把这条边界两头都钉住了：
    哪天有人觉得"往返后 None 变了 NaT 是 bug"想改掉，先看那两条测试。
    """
    if not types:
        return
    for pos, name in enumerate(frame.columns):
        kind = types.get(name) if isinstance(name, str) else None
        if kind is None:
            continue
        try:
            column = frame.iloc[:, pos]
            if kind == _TYPE_TIME:
                # format="mixed"：to_json 写出来的时间串有 '09:30:05' 也有 '09:30:05.123456'，
                # 不指定 format 时 pandas 会退回 dateutil 并且每次都告警一句
                converted = pd.to_datetime(column, format="mixed")
                frame.isetitem(pos, converted.dt.time)
            elif kind == _TYPE_DATE:
                frame.isetitem(pos, pd.to_datetime(column).dt.date)
            else:
                frame.isetitem(pos, pd.to_datetime(column))
        except Exception as exc:
            logger.debug("缓存里的 %r 列还原成 %s 失败，按字符串用：%s", name, kind, exc)


def _cache_get(k: str, ttl: int) -> pd.DataFrame | None:
    """读缓存；未命中、已过期、或读取出任何问题都返回 None。"""
    if ttl <= 0:
        return None
    conn = _cache_connect()
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT ts, payload FROM advanced_cache WHERE k = ?", (k,)).fetchone()
        if row is None:
            return None
        ts, payload = row
        if time.time() - float(ts) > ttl:
            return None
        return _decode_payload(payload)
    except Exception as exc:
        logger.debug("读高级数据缓存失败（%s）：%s", k, exc)
        return None
    finally:
        conn.close()


def _restore_index(frame: pd.DataFrame, meta: dict[str, Any]) -> None:
    """按 index 侧车把行索引还原回去（就地改 ``frame``）。

    两件事，缺一不可：

    - **dtype**：``convert_axes=False`` 之后 index 就是 JSON 里的原样（ISO 字符串 / int），
      datetime64 的 index 得由我们显式 ``pd.to_datetime`` 解析回去。注意这里**不传 unit**
      ——传了 pandas 就会走 ``array_with_unit_to_datetime``，对字符串报 FutureWarning，
      而这些串本来就是 ISO 日期串，直接按日期串解析才是对的。
    - **name**：``orient="split"`` 根本不写 index name，只能从侧车里补回来。``df.equals()``
      不比较 index name，所以这一步漏了不会有任何测试变红——除非显式断言 index.name。

    还原失败一律只 ``logger.debug``：读缓存出任何岔子最多是多打一次上游，抛出去却会让调用方
    拿到 None。和 ``_restore_types`` 是同一条原则。
    """
    dtype = meta.get("dtype")
    try:
        if isinstance(dtype, str) and dtype.startswith("datetime64"):
            frame.index = pd.to_datetime(frame.index)
        elif isinstance(dtype, str) and str(frame.index.dtype) != dtype:
            frame.index = frame.index.astype(dtype)
    except Exception as exc:
        logger.debug("缓存里的 index 还原成 %s 失败，按读出来的原样用：%s", dtype, exc)

    names = meta.get("names")
    if not isinstance(names, list):
        return
    try:
        if len(names) == 1:
            frame.index.name = names[0]
        elif len(names) == frame.index.nlevels:
            frame.index.names = names
    except Exception as exc:
        logger.debug("缓存里的 index name %r 写不回去：%s", names, exc)


def _decode_payload(payload: str) -> pd.DataFrame | None:
    """把磁盘上的 payload 还原成 DataFrame；还原不出来返回 None（当成没命中）。"""
    body = json.loads(payload)
    version = 0
    index_meta: dict[str, Any] = {}
    if isinstance(body, dict) and "v" in body and "df" in body:
        version = body["v"] if isinstance(body["v"], int) else 0
        split_json = body["df"]
        columns = body.get("columns")
        types = body.get("types") or {}
        raw_index = body.get("index")
        if isinstance(raw_index, dict):
            index_meta = raw_index
    else:
        # 旧格式：payload 直接就是 split json，没有任何侧车。当成 types={} 读，
        # 别让一次格式升级把用户已有的缓存全变成硬错误。
        split_json = payload
        columns = None
        types = {}

    # 有 index 侧车（v3 起）才敢关 convert_axes：两条轴都由侧车精确还原，不用 pandas 去猜，
    # 也就不会踩到它内部 ``to_datetime(..., unit=...)`` 的 FutureWarning。
    # 没有侧车的老 payload 维持原样让 pandas 猜——否则 ths_industry_index 那种 DatetimeIndex
    # 会读回成字符串 index，等于用一次格式升级把已有缓存悄悄改坏。
    convert_axes = not index_meta
    # pandas 3.0 起 read_json 不再接受字符串字面量 -> 必须包 StringIO
    # dtype=False 关掉类型推断，否则 '000001' 会被读成 int 1，深市代码前导零全丢、
    # 下游按代码匹配全部落空，而且一声不吭
    frame = pd.read_json(io.StringIO(split_json), orient="split", dtype=False, convert_axes=convert_axes)
    if not isinstance(frame, pd.DataFrame):
        return None

    if columns is not None:
        # 列名以存下来的原始版本为准：convert_axes 会把 '000001' 读成 int 1、把日期样的列名
        # 读成 Timestamp、给重复列名加 '.1' 后缀
        if len(columns) != len(frame.columns):
            logger.debug("缓存里的列名数量对不上（存 %d 实 %d），按缓存损坏处理", len(columns), len(frame.columns))
            return None
        frame.columns = pd.Index(columns)

    if index_meta:
        names = index_meta.get("names")
        if isinstance(names, list) and len(names) > 1:
            # 见 _cache_put 里的同一条：MultiIndex 读回来是转置的。宁可当没命中。
            logger.debug("缓存里存的是 MultiIndex（%s 层），split json 还原不回来，按没命中处理", len(names))
            return None
        _restore_index(frame, index_meta)
    elif version >= _PAYLOAD_VERSION:
        logger.debug("v%s 的 payload 却没有 index 侧车，index name 只能丢掉", version)

    _restore_types(frame, types)
    return frame


def _cache_put(k: str, df: pd.DataFrame) -> None:
    """写缓存；失败一律静默忽略。"""
    conn = _cache_connect()
    if conn is None:
        return
    try:
        # date_format="iso" 不能省：默认 epoch 会把日期列写成毫秒整数，
        # 配上读回来时的 dtype=False，日期就变成一串没人看得懂的大整数了
        if df.index.nlevels > 1:
            # orient="split" 把 MultiIndex 写成 [[a,1],[b,2]]，读回来 pandas 会**按列拆**，
            # 得到 [('a','b'), (1,2)]——转置了，而且一声不吭。这个格式装不下 MultiIndex，
            # 那就干脆不写：不缓存最多是多打一次上游，写了再读回来是数据直接错。
            # （31 条接口目前没有一条产出 MultiIndex，这里是防上游哪天改成 MultiIndex。）
            logger.debug("MultiIndex 的表不进缓存（split json 装不下）：%s", k)
            return
        payload = json.dumps(
            {
                "v": _PAYLOAD_VERSION,
                "df": df.to_json(orient="split", force_ascii=False, date_format="iso"),
                "columns": list(df.columns),
                "types": _infer_types(df),
                # index 侧车：orient="split" 不写 index name，dtype 也只剩 JSON 里的字面量。
                # names 用 list 而不是标量，MultiIndex 才装得下（虽然 31 条里目前没有）。
                "index": {"names": list(df.index.names), "dtype": str(df.index.dtype)},
            },
            ensure_ascii=False,
        )
        conn.execute(
            "INSERT OR REPLACE INTO advanced_cache (k, ts, payload) VALUES (?, ?, ?)",
            (k, time.time(), payload),
        )
        conn.commit()
    except Exception as exc:
        logger.debug("写高级数据缓存失败（%s）：%s", k, exc)
    finally:
        conn.close()


# ==================== 限流（按 source 分别限） ====================
#
# 东财和同花顺是两套服务器，谁也不该被对方拖累，所以每个 source 一个独立的闸门。
#
# 跨进程怎么协调——这里有个踩过的坑，写清楚免得再踩回去：原先照 modules/data_sync/
# rate_limiter.py 用 multiprocessing.Lock + multiprocessing.Value，注释写着"多进程安全"，
# 其实**只对 fork 出来的子进程成立**：那两个对象是 import 本模块时创建的，各自 python3
# 独立启动的进程各建各的，天然不共享（实测 3 个独立进程 9 次调用挤在 1.03 秒内全打了出去，
# 真共享的话应该要 4 秒）。而本仓库的实际形态恰恰全是独立进程——pyproject.toml 里
# zt / zt-web / zt-monitor 三个 entry point，外加备份脚本和 cron。
#
# 所以现在把"下一次可以调用的时刻"落在缓存那张 sqlite 小库里，用 BEGIN IMMEDIATE 划临界区，
# 这才是真的跨进程共享。进程内那把 threading.Lock 保留，只是为了少几次无谓的争抢。
#
# 边界：只在**同一台机器、同一个缓存库文件**内成立。跨机器（比如两台机子同时跑这套流水线）
# 仍然完全不协调，那需要 Redis 之类的外部协调者——本模块不做这件事。

_RATE_TABLE_DDL = "CREATE TABLE IF NOT EXISTS advanced_rate (source TEXT PRIMARY KEY, ts REAL)"
_RATE_DB_TIMEOUT = 5.0  # 秒；等别的进程放开写锁最多等这么久，超时就降级
_MAX_RATE_SLEEP = 60.0  # 秒；库里万一存进一个坏时刻，也不该让流水线睡到天荒地老


def _rate_connect() -> sqlite3.Connection | None:
    """开一个用于限流登记的连接；任何失败都返回 None（调用方降级为进程内计时）。"""
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None：事务边界要自己用 BEGIN IMMEDIATE 划，不能让 sqlite3 模块代劳
        conn = sqlite3.connect(str(path), timeout=_RATE_DB_TIMEOUT, isolation_level=None)
        conn.execute(_RATE_TABLE_DDL)
        return conn
    except Exception as exc:
        logger.debug("跨进程限流表不可用（%s）：%s，降级为进程内计时", path, exc)
        return None


def _reserve_rate_slot(source: str, interval: float) -> float | None:
    """在跨进程共享的 sqlite 上给 ``source`` 抢一个时间片，返回还需要等待多少秒。

    返回 ``None`` 表示这条路走不通，调用方应当降级回进程内计时——限流表坏了只该退化成
    "限得没那么准"，**绝不能退化成"取不到数"**。

    抢到的时间片是一个**写进库里的未来时刻**，登记完立刻提交、各睡各的，而不是持着 sqlite
    的写锁去 sleep：持锁 sleep 会让别的进程全卡在 BEGIN IMMEDIATE 上直到超时，超时后它们
    集体降级成内存计时，限流反而彻底失效。先登记后睡觉，N 个进程自然排成 interval 一档。
    """
    conn = _rate_connect()
    if conn is None:
        return None
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT ts FROM advanced_rate WHERE source = ?", (source,)).fetchone()
        last = float(row[0]) if row is not None and row[0] is not None else 0.0
        if not math.isfinite(last):
            # 库里存进了 inf/nan（手工改库、写到一半断电）就当没登记过，否则 inf 会把
            # 之后每一次调用都钉死在上限那儿——一条坏记录不该毒死整条流水线
            logger.debug("限流表里 %s 的时刻是 %r，按未登记处理", source, last)
            last = 0.0
        # 用 time.time() 而不是 time.monotonic()：monotonic 的原点每个进程都不一样，跨进程不可比
        now = time.time()
        target = max(now, last + interval)
        conn.execute("INSERT OR REPLACE INTO advanced_rate (source, ts) VALUES (?, ?)", (source, target))
        conn.execute("COMMIT")
        return target - now
    except Exception as exc:
        logger.debug("跨进程限流登记失败（%s）：%s，降级为进程内计时", source, exc)
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        return None
    finally:
        conn.close()


class _SourceRateLimiter:
    """单个数据源的最小间隔闸门。"""

    def __init__(self, source: str) -> None:
        self._source = source
        # 进程内先串一道：同进程的多个线程没必要都去撞 sqlite 的写锁
        # （modules/data_sync/syncer.py 就在用 ThreadPoolExecutor 并发拉数）
        self._lock = threading.Lock()
        # 降级用的计时。类型标 Any：typeshed 里 Value("d", ...) 返回的是 SynchronizedBase[Any]，
        # 声明上没有 .value，标死了反而要满地 type: ignore。
        # 仍旧用 multiprocessing.Value 而不是普通 float：fork 出来的子进程共享得到它，
        # 降级路径上还能多守住一点。
        self._last_call: Any = multiprocessing.Value("d", 0.0)

    def wait(self) -> None:
        """阻塞到距上次调用满一个最小间隔为止。"""
        interval = _min_interval()
        if interval <= 0:
            return
        with self._lock:
            sleep_for = _reserve_rate_slot(self._source, interval)
            if sleep_for is None:
                with self._last_call.get_lock():
                    now = time.time()
                    target = max(now, float(self._last_call.value) + interval)
                    self._last_call.value = target
                    sleep_for = target - now
            if sleep_for > 0:
                time.sleep(min(sleep_for, _MAX_RATE_SLEEP))


def _min_interval() -> float:
    """最小请求间隔（秒）。**每次调用都读环境变量**——测试要把它设成 0。"""
    raw = os.environ.get(ENV_MIN_INTERVAL)
    if raw is None:
        return DEFAULT_MIN_INTERVAL
    try:
        value = float(raw)
    except (TypeError, ValueError):
        # 限流参数配错不该让整条流水线停摆，退回默认值继续跑
        logger.debug("%s=%r 不是数字，退回默认 %.2fs", ENV_MIN_INTERVAL, raw, DEFAULT_MIN_INTERVAL)
        return DEFAULT_MIN_INTERVAL
    if not math.isfinite(value):
        # float() 认 'inf' / '1e400' / 'nan'，这三个都是灾难：
        # inf 会让 time.sleep 抛 OverflowError（栈在限流器里，一路穿出 fetch）；
        # nan 更阴——所有比较恒为 False，限流被**无声关掉**，而这一层的失败模式正是被封 IP
        logger.debug("%s=%r 不是有限数，退回默认 %.2fs", ENV_MIN_INTERVAL, raw, DEFAULT_MIN_INTERVAL)
        return DEFAULT_MIN_INTERVAL
    if value < 0:
        # 0 是"刻意关掉限流"（测试就这么用），负数只可能是手滑，按配错处理
        logger.debug("%s=%r 是负数，退回默认 %.2fs", ENV_MIN_INTERVAL, raw, DEFAULT_MIN_INTERVAL)
        return DEFAULT_MIN_INTERVAL
    # 夹一个上界：手滑写成 3600（把"秒"当成"每小时一次"）会把整条流水线挂死
    return min(value, MAX_MIN_INTERVAL)


# 模块导入时就把注册表里出现过的 source 建好闸门：每个 source 一个，互不拖累
_LIMITERS: dict[str, _SourceRateLimiter] = {
    src: _SourceRateLimiter(src) for src in sorted({SOURCE_EM, SOURCE_THS, SOURCE_CLS} | {i.source for i in INTERFACES})
}


def _rate_limit(source: str) -> None:
    limiter = _LIMITERS.get(source)
    if limiter is None:
        # 正常路径走不到（注册表里的 source 都预建了）；兜底建一个，sqlite 那层照样跨进程生效
        limiter = _LIMITERS.setdefault(source, _SourceRateLimiter(source))
    limiter.wait()


# ==================== 上游应答取证 ====================
#
# akshare 对东财 datacenter-web 的应答一律直接下标：``data_json["result"]["pages"]``。
# 而东财把"没数据"表达成 ``{"success": false, "code": 9201, "result": null}``——于是
# **一个正常的没数据的日子**会让 akshare 抛 TypeError，fetch 只能返回 None，调用方照本
# 模块的契约会把它当故障去报警。这恰好是开头那句"在这一层，取不到数据多半是正常的"的反面。
#
# 光看异常类型分不清两件事：是"上游说没数据"，还是"翻页翻到一半被限流截断"。
# 【2026-08-25 实测】jgdy 两种都出现过，抛的都是同一个 TypeError。
#
# 判据从异常自己身上取：TypeError 是在 akshare 的帧里抛的，那一帧的局部变量 ``data_json``
# 就是害它崩掉的那份应答，原样躺在 traceback 里。既不用改 requests.get（那是全进程可见的
# 副作用，而 modules/data_sync/syncer.py 正在用 ThreadPoolExecutor 并发拉数），
# 也不给成功路径添任何成本——只有出错时才走这一段。
#
# 这确实依赖 akshare 的局部变量名，和 Interface.func（函数名）、Interface.expect（列名）
# 是同一类依赖，处理方式也一样：认不出来就退回"按故障处理"，绝不会因此多抛一个异常。


def _has_partial_rows(frame: Any) -> bool:
    """这一帧里已经攒下真数据了吗——翻页翻到一半才撞上空信封的情形。

    akshare 的翻页函数一律把累积结果放在 ``big_df``、单页结果放在 ``temp_df``。
    它们非空就说明前面几页是有数据的，这次的空信封只是**截断**，不是"今天没数据"。
    那种情况必须按故障报出去：把半份数据说成"一份完整的空"比报错危险得多——
    调用方拿到空表会安静跳过，而真相是这批数据缺了一大块。
    """
    for name in ("big_df", "temp_df"):
        value = frame.f_locals.get(name)
        if isinstance(value, pd.DataFrame) and not value.empty:
            return True
    return False


def _crashed_on_empty_envelope(exc: BaseException) -> bool:
    """akshare 是不是崩在东财那句"返回数据为空"上。

    整段包得住任何意外：这是**观测**代码，跑在故障处置路径上，它自己绝不能成为新的故障源。
    认不出来一律返回 False（按故障处理）——拿不准的时候宁可多报一次警，
    也不能把一次真故障说成"今天没数据"。
    """
    try:
        tb = exc.__traceback__
        verdict = False
        while tb is not None:
            frame = tb.tb_frame
            payload = frame.f_locals.get("data_json")
            # 一路走到最内层：崩掉的那一帧才是现场，外层帧里的同名变量说明不了问题
            if isinstance(payload, dict):
                verdict = payload.get("code") == EM_CODE_EMPTY and not _has_partial_rows(frame)
            tb = tb.tb_next
        return verdict
    except Exception:  # pragma: no cover - 取证本身不该有失败模式，兜住是为了守住"不抛异常"
        return False


# ==================== 协议与实现 ====================


@runtime_checkable
class AdvancedDataSource(Protocol):
    """高级数据源协议——与 ``modules.datasource.DataSource`` 平级，不是它的子类。

    注意 ``runtime_checkable`` 的 ``isinstance`` 只检查方法/属性存不存在，不检查签名。
    """

    @property
    def name(self) -> str: ...

    def health_check(self) -> bool: ...

    def fetch(self, key: str, params: dict[str, str] | None = None, *, force: bool = False) -> pd.DataFrame | None: ...

    @property
    def last_error(self) -> str | None: ...


class _CallState(threading.local):
    """每个线程各一份的 fetch 状态。

    ``last_error`` / ``warnings`` 讲的是"**我刚才那次** fetch 出了什么事"，可单例是全进程
    共享的（限流状态必须共享，所以只能有一个实例）。放成普通属性，两个线程并发 fetch 时
    会互相盖：实测 2000 次里有 1 次"拿到数据却看见别人的错误"——而本仓库
    modules/data_sync/syncer.py 已经在用 ThreadPoolExecutor 并发拉数了。
    ``threading.local`` 的子类每个线程首次访问时都会重跑一遍 ``__init__``，正好一线程一份。

    注意只有这两个字段进 thread-local，限流器状态仍然全进程共享——那是刻意的：
    限的是本机对上游的总请求速率，按线程各限一份等于没限。
    """

    def __init__(self) -> None:
        self.last_error: str | None = None
        self.warnings: list[str] = []


class AkshareAdvancedSource:
    """基于 akshare 的高级数据源实现。"""

    def __init__(self) -> None:
        self._state = _CallState()

    @property
    def name(self) -> str:
        return "akshare_advanced"

    # ``_last_error`` / ``_warnings`` 保持属性写法，读写的却是**当前线程**那一份
    @property
    def _last_error(self) -> str | None:
        return self._state.last_error

    @_last_error.setter
    def _last_error(self, value: str | None) -> None:
        self._state.last_error = value

    @property
    def _warnings(self) -> list[str]:
        return self._state.warnings

    @_warnings.setter
    def _warnings(self, value: list[str]) -> None:
        self._state.warnings = value

    @property
    def last_error(self) -> str | None:
        """上一次 fetch 的错误；``None`` 表示没出错（空表和缺列不算错）。"""
        return self._last_error

    @property
    def warnings(self) -> list[str]:
        """上一次 fetch 的告警列表（副本，改它不影响内部状态）。"""
        return list(self._warnings)

    def health_check(self) -> bool:
        """能 import akshare 就算健康。真正的可用性归 ``selfcheck(probe=True)``。"""
        try:
            import akshare  # noqa: F401
        except ImportError as exc:
            self._last_error = f"未安装 akshare，请先 pip install akshare（{exc}）"
            return False
        except Exception as exc:
            # akshare 的依赖面很宽（requests / lxml / bs4 / py_mini_racer …），坏依赖抛出来的
            # 常常不是 ImportError。只接 ImportError 的话，签名承诺 bool 的函数会往外扔异常。
            self._last_error = f"导入 akshare 失败: {type(exc).__name__}: {exc}"
            return False
        return True

    def _validate(self, df: pd.DataFrame, spec: Interface) -> None:
        """把"这次数据不完整"记进 warnings。纯本地计算：不 import akshare、不发网络。

        必须在**两条返回数据的路径**上都调用（缓存命中 + 网络成功）。原先只在网络路径做，
        结果是：上游改列后第一次 fetch 有告警，第二次起命中缓存就静默返回残缺表——
        zt_pool 的 ttl 是 12 小时、zyjs 是 30 天，等于告警只响一次，之后整个 TTL 窗口
        全程哑火，直接违反"调用方必须知道这次不完整"这条契约。
        """
        if df.empty:
            # 空表不是错误：非交易日、该条件下确实没数据，都会走到这里
            self._warnings.append("上游返回空表（非交易日或该条件下无数据都可能，属正常）")
            return
        missing = [c for c in spec.expect if c not in df.columns]
        if missing:
            # 缺列照常返回数据：剩下的列可能还有用，扔掉才是真损失；
            # 但一定要让调用方知道这次不完整——绝不能让 None 静默流进选股逻辑
            self._warnings.append(f"期望列缺失: {missing}；实际列: {list(df.columns)}")

    def fetch(self, key: str, params: dict[str, str] | None = None, *, force: bool = False) -> pd.DataFrame | None:
        """取一条接口的数据。

        返回 ``None`` 只有一个含义：**这次出错了**，错误原因在 ``last_error``。
        取回了数据但不完整（上游改了列名）或干脆是空表，都照常返回 DataFrame，
        把话放进 ``warnings``——调用方必须能分清"没数据"和"出故障"。
        """
        self._last_error = None
        self._warnings = []
        call_params = dict(params or {})

        spec = BY_KEY.get(key)
        if spec is None:
            self._last_error = f"未知接口 key: {key!r}；可用 key: " + ", ".join(sorted(BY_KEY))
            return None

        # 参数名白名单校验，必须在任何网络动作之前：打错参数名是调用方的 bug，
        # 让它立刻失败，别浪费一次上游请求（也别把 IP 拿去换一条 TypeError）
        unknown = sorted(set(call_params) - set(spec.params))
        if unknown:
            self._last_error = f"接口 {key} 不接受参数 {unknown}；接受的参数: {list(spec.params)}"
            return None

        # json.dumps 要保护起来：白名单只校验参数**名**，值是 date / Timestamp 这类
        # 不可序列化的对象时，TypeError 会直接穿出 fetch，违反"只返回 None、不抛异常"。
        # 刻意**不用** default=str 糊过去——那会让 date 对象带着 repr 混进缓存键，
        # 同一个日期写法不同就是两份缓存，错得更隐蔽。
        try:
            cache_key = key + "|" + json.dumps(call_params, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            self._last_error = f"接口 {key} 的参数值无法序列化: {exc}"
            return None

        if not force:
            cached = _cache_get(cache_key, spec.ttl)
            if cached is not None:
                logger.debug("高级数据缓存命中：%s", cache_key)
                # 缓存命中也要校验：缓存写进去的时候上游可能就已经改过列了，
                # 不在这里再说一次，整个 TTL 窗口里调用方都不知道手上的数据是残缺的
                self._validate(cached, spec)
                return cached

        # 延迟 import：akshare 首次 import 要 2 秒左右，不该拖慢每次 `python3 -m modules.cli`
        # 启动；写在函数体内也是测试能靠替换 sys.modules 注入假 akshare 的前提
        try:
            import akshare as ak
        except ImportError:
            self._last_error = "未安装 akshare，请先 pip install akshare"
            return None
        except Exception as exc:
            # 坏依赖抛出来的常常不是 ImportError（实测：一个 raise AttributeError 的假 akshare
            # 能让 fetch 把 AttributeError 抛给调用方，而签名承诺的是返回 None）
            self._last_error = f"导入 akshare 失败: {type(exc).__name__}: {exc}"
            return None

        func = getattr(ak, spec.func, None)
        if func is None or not callable(func):
            self._last_error = f"akshare 中找不到函数 {spec.func}（上游改过接口名，请更新 INTERFACES 里的 func 字段）"
            return None

        _rate_limit(spec.source)

        try:
            df = func(**call_params)
        except Exception as exc:
            if _crashed_on_empty_envelope(exc):
                # 上游明确答复"返回数据为空"（code 9201）。akshare 是对 result:null 直接
                # 下标才抛的 TypeError——那是它的 bug，不是故障。按本模块的契约，
                # "没数据"必须表达成空表 + warnings，绝不能变成 None 让调用方去报警。
                logger.debug(
                    "接口 %s：上游回 code %d（无数据），akshare 抛了 %s", key, EM_CODE_EMPTY, type(exc).__name__
                )
                df = pd.DataFrame()
            else:
                self._last_error = f"{type(exc).__name__}: {exc}"
                return None

        if not isinstance(df, pd.DataFrame):
            self._last_error = f"上游返回了非 DataFrame: {type(df).__name__}"
            return None

        self._validate(df, spec)

        if df.empty:
            # 空表**不写缓存**——否则一次瞬时故障会被固化到 TTL 结束（zt_pool 是 12 小时）
            return df

        _cache_put(cache_key, df)
        return df


# ==================== 进程内单例 ====================
#
# 限流状态（那几把锁和时间戳）要在整个进程里共享，所以对外只给一个实例。

_SOURCE: AkshareAdvancedSource | None = None


def get_advanced_source() -> AkshareAdvancedSource:
    """取进程内的高级数据源单例。"""
    global _SOURCE
    if _SOURCE is None:
        _SOURCE = AkshareAdvancedSource()
    return _SOURCE


def reset_advanced_source() -> None:
    """清空单例，供测试在用例之间隔离 last_error / warnings 状态。"""
    global _SOURCE
    _SOURCE = None


# ==================== 自检 ====================


def selfcheck(probe: bool = True) -> dict[str, Any]:
    """体检：注册表里的函数还在不在，无参接口还能不能取到数据。

    ``probe=True`` 会**真的发网络请求**（只探无参接口），慢且可能被限流，
    适合手工排查；``probe=False`` 是纯离线的，只做 hasattr 检查——
    akshare 改函数名是静默的，这一项才是日常最该盯的。
    """
    result: dict[str, Any] = {
        "akshare 版本": None,
        "接口总数": len(INTERFACES),
        "函数已不存在": [],
        "探活通过": [],
        "探活失败": [],
        # 有参接口不探活：随手编的参数取回空表说明不了任何问题
        "跳过_需要参数": [i.key for i in INTERFACES if i.params],
    }

    try:
        import akshare as ak
    except ImportError as exc:
        # 装没装 akshare 是环境问题，不该让自检本身炸掉——照样返回同一个结构
        result["探活失败"].append({"key": "-", "错误": f"未安装 akshare，请先 pip install akshare（{exc}）"})
        return result
    except Exception as exc:
        # 同 fetch / health_check：坏依赖抛的多半不是 ImportError，自检更不能因此炸掉
        result["探活失败"].append({"key": "-", "错误": f"导入 akshare 失败: {type(exc).__name__}: {exc}"})
        return result

    result["akshare 版本"] = str(getattr(ak, "__version__", "未知"))

    for spec in INTERFACES:
        if not hasattr(ak, spec.func):
            result["函数已不存在"].append({"key": spec.key, "func": spec.func})

    if not probe:
        return result

    source = get_advanced_source()
    for spec in INTERFACES:
        if spec.params:
            continue
        # force=True：探活要的是"现在还通不通"，命中缓存等于没探
        df = source.fetch(spec.key, force=True)
        if df is None:
            result["探活失败"].append({"key": spec.key, "错误": source.last_error or "未知错误"})
            continue
        entry: dict[str, Any] = {"key": spec.key, "行数": int(len(df)), "列": list(df.columns)[:8]}
        warnings = source.warnings
        if warnings:
            entry["告警"] = warnings
        result["探活通过"].append(entry)

    return result
