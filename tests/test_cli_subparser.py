"""
P1-2 回归测试：5 个独立 main() 合并到 cli.py

- 7 个顶层 subcommand 都可识别（analyze/screen/score/workflow/diagnose/watchlist/sync）
- watchlist 5 个子动作（add/remove/list/scan/report）
- sync 4 个子动作（init/sync/status/stk-factor）
- 顶层 subcommand 缺失时 exit 非 0
- 全局 prog = "zt"
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def run_zt(*args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "modules.cli", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(PROJECT_ROOT),
    )


# ==================== 顶层 subcommand ====================

EXPECTED_TOP_COMMANDS = ["analyze", "screen", "score", "workflow", "diagnose", "watchlist", "sync", "monitor"]


def test_top_level_help_lists_all_seven_commands():
    """zt --help 必须列出 7 个顶层 subcommand"""
    result = run_zt("--help")
    assert result.returncode == 0
    for cmd in EXPECTED_TOP_COMMANDS:
        assert cmd in result.stdout, f"--help 缺 {cmd}"


@pytest.mark.parametrize("cmd", EXPECTED_TOP_COMMANDS)
def test_each_top_command_has_help(cmd):
    """每个顶层 subcommand 必须支持 --help"""
    result = run_zt(cmd, "--help")
    assert result.returncode == 0, (
        f"{cmd} --help exit {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_missing_command_exits_nonzero():
    """不传任何 subcommand 应该 exit 非 0（required=True）"""
    result = run_zt()
    assert result.returncode != 0
    # argparse 应该打印 usage + 错误到 stderr
    assert "usage:" in result.stderr or "usage:" in result.stdout


def test_prog_is_zt():
    """顶层 ArgumentParser 的 prog 必须是 zt"""
    result = run_zt("--help")
    assert "usage: zt " in result.stdout


# ==================== watchlist 子动作 ====================

EXPECTED_WL_ACTIONS = ["add", "remove", "list", "scan", "report"]


def test_watchlist_help_lists_all_five_actions():
    """zt watchlist --help 必须列出 5 个 action（含新增 report）"""
    result = run_zt("watchlist", "--help")
    assert result.returncode == 0
    for action in EXPECTED_WL_ACTIONS:
        assert action in result.stdout, f"watchlist --help 缺 {action}"


# ==================== sync 子动作 ====================

EXPECTED_SYNC_ACTIONS = ["init", "sync", "status", "stk-factor"]


def test_sync_help_lists_all_four_actions():
    """zt sync --help 必须列出 4 个 action"""
    result = run_zt("sync", "--help")
    assert result.returncode == 0
    for action in EXPECTED_SYNC_ACTIONS:
        assert action in result.stdout, f"sync --help 缺 {action}"


def test_sync_init_help():
    """zt sync init --help 必须 exit 0"""
    result = run_zt("sync", "init", "--help")
    assert result.returncode == 0


def test_sync_sync_help():
    """zt sync sync --help 必须 exit 0"""
    result = run_zt("sync", "sync", "--help")
    assert result.returncode == 0
    # ts_code 是位置参数（nargs='?'），不是 --ts_code flag
    # 其余是 flag
    for flag in ("ts_code", "--days", "--indicators", "--skip-indicators"):
        assert flag in result.stdout, f"sync sync --help 缺 {flag}"


def test_sync_stk_factor_help():
    """zt sync stk-factor --help 必须 exit 0"""
    result = run_zt("sync", "stk-factor", "--help")
    assert result.returncode == 0


# ==================== screen 11 种 strategy 仍被接受 ====================

STRATEGY_ALIAS = {
    "B1": "b1",
    "B2": "b2_breakout",
    "B3": "b3_consensus",
    "完美图形": "perfect",
    "超级B1": "super_b1",
    "长安战法": "changan",
    "建仓波": "build_wave",
    "吸筹": "xishou",
    "安全": "safe",
    "超跌": "oversold",
    "突破": "breakout",
}


@pytest.mark.parametrize("strategy", STRATEGY_ALIAS.keys())
def test_screen_accepts_all_strategies_via_cli(strategy):
    """screen --help 应该列出所有 11 种 strategy"""
    result = run_zt("screen", "--help")
    assert result.returncode == 0
    assert strategy in result.stdout, f"screen --help 缺 {strategy}"


# ==================== analyze / diagnose / score / workflow 必要 flag ====================


def test_analyze_help_has_required_args():
    result = run_zt("analyze", "--help")
    assert result.returncode == 0
    assert "ts_code" in result.stdout
    assert "--days" in result.stdout


def test_diagnose_help_has_required_args():
    result = run_zt("diagnose", "--help")
    assert result.returncode == 0
    assert "ts_code" in result.stdout
    assert "--days" in result.stdout


def test_score_help_has_ts_code():
    result = run_zt("score", "--help")
    assert result.returncode == 0
    assert "ts_code" in result.stdout


def test_workflow_help_works():
    """workflow 无参数，但 --help 应该 exit 0"""
    result = run_zt("workflow", "--help")
    assert result.returncode == 0


# ==================== zt 整体可用性 smoke ====================


def test_zt_help_does_not_crash():
    """最简单 smoke：zt --help 完整跑通"""
    result = run_zt("--help")
    assert result.returncode == 0
    # 应该看到 epilog 中的示例
    assert "zt analyze" in result.stdout
    assert "zt sync" in result.stdout


# ==================== backtest / trade 子命令回归（P0-1 修复保护）====================
#
# 背景：2026-06 修复前，cli.py 注册 subparser 时 dest="bt_action"，
# 但 cli_commands.cmd_backtest 里读 args.backtest_sub → 永远 None → 报"请指定子命令"。
# trade 同问题。下面 4 个测试保证：
# 1) help 能列出全部子命令
# 2) 真正调用时不会卡在 dest 不匹配上（不会返回"请指定子命令"）
# 注：完整业务行为依赖数据库，本测试不验证业务结果，只验证 dispatch 路径打通了


BT_ACTIONS = ["multi", "portfolio"]


@pytest.mark.parametrize("action", BT_ACTIONS)
def test_backtest_help_lists_all_actions(action):
    """zt backtest --help 必须列出 multi / portfolio"""
    result = run_zt("backtest", "--help")
    assert result.returncode == 0
    assert action in result.stdout, f"backtest --help 缺 {action}"


def test_backtest_multi_dispatches_to_handler():
    """回归：cli.py dest 必须为 backtest_sub,否则 cmd_backtest 报"请指定子命令"

    不验证回测业务结果（依赖数据库），只验证不再卡在 dispatch 错误。
    """
    result = run_zt("backtest", "multi", "600487.SH", timeout=20)
    # 真正成功需要数据库 + 数据；只要不是"请指定回测子命令"就说明 dest 修对了
    assert "请指定回测子命令" not in (result.stdout + result.stderr), (
        f"backtest multi 仍卡在 dest 错误:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_backtest_portfolio_dispatches_to_handler():
    """回归：cli.py portfolio 子命令的位参数字段名必须为 codes（与 cmd_backtest 对齐）"""
    result = run_zt("backtest", "portfolio", "600487.SH,601318.SH", timeout=20)
    assert "请指定回测子命令" not in (result.stdout + result.stderr), (
        f"backtest portfolio dest 不匹配:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    # 且不应报"股票代码列表为空"（说明 codes 字段被 argparse 正确填充）
    assert "股票代码列表为空" not in (result.stdout + result.stderr), (
        f"backtest portfolio codes 字段没传过去:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


TRADE_ACTIONS = ["add", "list", "stats"]


@pytest.mark.parametrize("action", TRADE_ACTIONS)
def test_trade_help_lists_all_actions(action):
    """zt trade --help 必须列出 add / list / stats"""
    result = run_zt("trade", "--help")
    assert result.returncode == 0
    assert action in result.stdout, f"trade --help 缺 {action}"


def test_trade_list_dispatches_to_handler():
    """回归：trade 从位参数改为 subparser 后,list 不应再卡在 dest 错误"""
    result = run_zt("trade", "list", timeout=15)
    assert "请指定交易子命令" not in (result.stdout + result.stderr), (
        f"trade list dest 仍不匹配:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


# ==================== advanced 子命令（高阶行情数据层）====================
#
# 这一层打的是东财/同花顺/财联社的公开网页接口，所以测试的第一要务不是"取没取到数据"，
# 而是**"哪些路径绝对不许碰网络"**：catalog 是纯本地注册表，key 打错要在发请求之前就失败。
# 只看返回值证明不了这件事（返回值对了也可能是碰巧对的），所以下面把子进程的 socket
# 连接口整个封死，让"偷偷联网"变成一个必然 traceback 的硬错误。

ADVANCED_ACTIONS = ["catalog", "get", "selfcheck"]

# 六个分组写死在这里，不从 modules.advanced_data 里 import——从同一个常量取，
# 分组被删掉一个时期望值会跟着一起变，测试照样绿，等于没测
EXPECTED_ADVANCED_CATEGORIES = ["资金面", "情绪面", "结构面", "风险面", "消息面", "技术榜"]

_NO_NETWORK_SITECUSTOMIZE = '''\
"""放进 PYTHONPATH 的 sitecustomize：解释器起来就把出站连接封死。

只封 connect / create_connection / getaddrinfo，**不能**整个替换 socket.socket——
ssl.py 里有 `class SSLSocket(socket)`，把类换成函数会让 import ssl 直接炸，
测的就成了"import 失败"而不是"没联网"。
"""
import socket


def _blocked(*args, **kwargs):
    raise RuntimeError("NETWORK-BLOCKED-BY-TEST")


socket.socket.connect = _blocked
socket.socket.connect_ex = _blocked
socket.create_connection = _blocked
socket.getaddrinfo = _blocked
'''


@pytest.fixture(scope="module")
def no_network_env(tmp_path_factory):
    """返回一份"一联网就炸"的环境变量，并且**先自证这个封锁真的有效**。

    少了自证这一步，万一 sitecustomize 没被加载（PYTHONPATH 拼错、site 被 -S 关掉），
    下面的用例会全绿，却什么都没证明。
    """
    site_dir = tmp_path_factory.mktemp("no_network")
    (site_dir / "sitecustomize.py").write_text(_NO_NETWORK_SITECUSTOMIZE, encoding="utf-8")

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(site_dir), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)

    # 探针打的是 127.0.0.1:1（一个必然没人监听的本地端口），**不是**真实外网地址：
    # 封锁生效时它撞的是 NETWORK-BLOCKED-BY-TEST，封锁失效时它立刻拿到 ConnectionRefusedError
    # ——两条路都不出机器。原先探的是 http://example.com，等于"为了证明自己不联网而联一次网"，
    # 在离线机器上还会白等一次超时。
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import urllib.request; urllib.request.urlopen('http://127.0.0.1:1/', timeout=1)",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert probe.returncode != 0 and "NETWORK-BLOCKED-BY-TEST" in probe.stderr, (
        f"网络封锁没生效，下面的『不联网』断言全是空的:\nstdout: {probe.stdout}\nstderr: {probe.stderr}"
    )
    return env


def run_zt_offline(env, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """在"一联网就炸"的子进程里跑 zt。"""
    return subprocess.run(
        [sys.executable, "-m", "modules.cli", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(PROJECT_ROOT),
        env=env,
    )


def test_advanced_help_lists_all_three_actions():
    """zt advanced --help 必须列出 catalog / get / selfcheck"""
    result = run_zt("advanced", "--help")
    assert result.returncode == 0, f"advanced --help exit {result.returncode}\nstderr: {result.stderr}"
    for action in ADVANCED_ACTIONS:
        assert action in result.stdout, f"advanced --help 缺 {action}"


@pytest.mark.parametrize("action", ADVANCED_ACTIONS)
def test_advanced_each_action_has_help(action):
    """三个子动作各自的 --help 都要 exit 0（--help 不执行动作，所以 get 也不会联网）"""
    result = run_zt("advanced", action, "--help")
    assert result.returncode == 0, (
        f"advanced {action} --help exit {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_advanced_get_help_lists_all_flags():
    """zt advanced get 的参数面：key 是位置参数，--param 可重复，还有 --limit/--force/--json"""
    result = run_zt("advanced", "get", "--help")
    assert result.returncode == 0
    for token in ("key", "--param", "--limit", "--force", "--json"):
        assert token in result.stdout, f"advanced get --help 缺 {token}"
    # --param 的 metavar 写成 K=V，是为了让人一眼知道要写 --param date=20260814
    assert "K=V" in result.stdout


def test_advanced_get_parses_param_limit_without_running():
    """`get zt_pool --param date=20260814 --limit 5` 必须解析成期望的 args（不执行，不联网）"""
    from modules.cli import build_parser

    args = build_parser().parse_args(
        ["advanced", "get", "zt_pool", "--param", "date=20260814", "--limit", "5"]
    )
    assert args.command == "advanced"
    assert args.advanced_action == "get"
    assert args.key == "zt_pool"
    # --param 是 append：重复给要能攒成 list，而不是后一个盖掉前一个
    assert args.param == ["date=20260814"]
    assert args.limit == 5
    assert args.force is False

    multi = build_parser().parse_args(
        ["advanced", "get", "lhb_detail", "--param", "start_date=20260810", "--param", "end_date=20260814"]
    )
    assert multi.param == ["start_date=20260810", "end_date=20260814"]


def test_advanced_catalog_is_offline_and_has_six_categories(no_network_env):
    """catalog 是纯本地注册表：在"一联网就炸"的进程里必须照样跑通，六个分组一个不少"""
    result = run_zt_offline(no_network_env, "advanced", "catalog", "--json")
    assert result.returncode == 0, (
        f"catalog 在断网进程里挂了（说明它偷偷联网了）:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "NETWORK-BLOCKED-BY-TEST" not in result.stderr

    grouped = json.loads(result.stdout)
    assert list(grouped) == EXPECTED_ADVANCED_CATEGORIES, f"分组不齐或顺序变了: {list(grouped)}"
    for category in EXPECTED_ADVANCED_CATEGORIES:
        assert grouped[category], f"分组 {category} 是空的"

    # 各组条目数之和 == 注册表总数：catalog 不许漏条目，也不许把一条塞进两组
    from modules.advanced_data import INTERFACES

    assert sum(len(v) for v in grouped.values()) == len(INTERFACES)


def test_advanced_catalog_text_output_is_offline(no_network_env):
    """不带 --json 的人读版同样不联网，且六个分组标题都在"""
    result = run_zt_offline(no_network_env, "advanced", "catalog")
    assert result.returncode == 0, f"catalog 文本输出在断网进程里挂了:\nstderr: {result.stderr}"
    for category in EXPECTED_ADVANCED_CATEGORIES:
        assert category in result.stdout, f"catalog 文本输出缺分组 {category}"


def test_advanced_get_unknown_key_fails_before_touching_network(no_network_env):
    """key 打错是调用方的 bug：必须在发请求之前就失败，别拿 IP 去换一条报错

    用断网进程来证明——如果实现把校验放到了网络动作之后，这里会是 RuntimeError 而不是
    那句"未知接口 key"。
    """
    result = run_zt_offline(no_network_env, "advanced", "get", "no_such_key")
    assert result.returncode == 1, f"期望 exit 1，实际 {result.returncode}\nstderr: {result.stderr}"
    assert "NETWORK-BLOCKED-BY-TEST" not in result.stderr, "校验前先联网了"
    assert "未知接口 key" in result.stdout
    # last_error 要原样打出来：那串可用 key 就是给人照着改的
    assert "zt_pool" in result.stdout


# ---------- selfcheck --no-probe：断言"有没有真的去打上游"，而不是"输出长得对不对" ----------
#
# 上一版这条用例只断言 exit==0 + stdout 里有两句固定文案。把 cmd_advanced 里的
# `probe=not args.no_probe` 改成 `probe=True`（即 --no-probe 被完全忽略、CLI 真的发起 8 次
# 出站取数），那三条断言在联网时同样成立，用例照样全绿——而它声称钉住的正是这件事。
#
# 所以这里换一套证据：给子进程装一个**假 akshare**，它的每个函数一被调用就往日志文件记一行。
# 于是"发没发请求"不再靠猜：日志为空 == 一次上游调用都没有。
# "akshare 版本: 0.0.0-fake" 那条断言是这套证据的地基——它证明子进程 import 到的确实是这个桩，
# 否则"日志为空"可能只是因为桩压根没被加载。

_FAKE_AKSHARE_TEMPLATE = r'''
# 假 akshare：每一次上游调用都往 ADVANCED_PROBE_LOG 记一行。
#
# 函数名照 INTERFACES 里的 func 挂满，免得 selfcheck 把它们全算进"函数已不存在"——
# 那样就分不清是实现的问题还是桩没搭好。
import os

__version__ = "0.0.0-fake"

_LOG = os.environ["ADVANCED_PROBE_LOG"]


def _make(name):
    def _call(**params):
        with open(_LOG, "a", encoding="utf-8") as fh:
            fh.write(name + "\n")
        import pandas as pd

        return pd.DataFrame({"代码": ["000001"], "名称": ["平安银行"]})

    return _call


for _name in %(funcs)r:
    globals()[_name] = _make(_name)
'''


@pytest.fixture
def fake_akshare_env(no_network_env, tmp_path):
    """断网环境 + 一个"会记账的假 akshare"，返回 (env, 调用日志路径, 缓存库路径)。"""
    from modules.advanced_data import INTERFACES

    site_dir = tmp_path / "fake_akshare"
    site_dir.mkdir()
    funcs = sorted({spec.func for spec in INTERFACES})
    (site_dir / "akshare.py").write_text(_FAKE_AKSHARE_TEMPLATE % {"funcs": funcs}, encoding="utf-8")

    probe_log = tmp_path / "upstream_calls.log"
    cache_db = tmp_path / "advanced_cache.db"

    env = dict(no_network_env)
    # PYTHONPATH 排在 site-packages 前面，所以这个桩会盖掉真 akshare
    env["PYTHONPATH"] = os.pathsep.join([str(site_dir), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env["ADVANCED_PROBE_LOG"] = str(probe_log)
    env["ADVANCED_CACHE_PATH"] = str(cache_db)  # 别碰用户真实的缓存库
    env["ADVANCED_MIN_INTERVAL"] = "0"
    return env, probe_log, cache_db


def _upstream_calls(probe_log: Path) -> list[str]:
    """假 akshare 记下的上游调用清单；文件不存在就是一次都没调过。"""
    if not probe_log.exists():
        return []
    return [line for line in probe_log.read_text(encoding="utf-8").splitlines() if line]


def _rate_ledger_rows(cache_db: Path) -> int:
    """限流台账的行数。fetch 每要打一次上游都会先登记一次，0 行 == 没打过。"""
    import sqlite3

    if not cache_db.exists():
        return 0
    with sqlite3.connect(str(cache_db)) as conn:
        try:
            return int(conn.execute("SELECT COUNT(*) FROM advanced_rate").fetchone()[0])
        except sqlite3.Error:
            return 0


@pytest.mark.parametrize("extra", [(), ("--json",)])
def test_advanced_selfcheck_no_probe_really_does_not_touch_upstream(fake_akshare_env, extra):
    """--no-probe 承诺"只做离线 hasattr 检查"：一次上游调用都不许有（文本 / --json 两条路径）。"""
    env, probe_log, cache_db = fake_akshare_env

    result = run_zt_offline(env, "advanced", "selfcheck", "--no-probe", *extra, timeout=180)

    assert result.returncode == 0, f"selfcheck --no-probe 挂了:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    assert "NETWORK-BLOCKED-BY-TEST" not in result.stderr

    # 核心断言：真的没去打上游
    assert _upstream_calls(probe_log) == [], (
        f"--no-probe 却调用了上游函数（说明 --no-probe 没被当回事）: {_upstream_calls(probe_log)}"
    )
    assert _rate_ledger_rows(cache_db) == 0, "限流台账里有登记，说明 fetch 真的准备打上游了"

    if extra:
        payload = json.loads(result.stdout)
        # 版本号证明子进程 import 到的确实是那个桩，上面"日志为空"才有意义
        assert payload["akshare 版本"] == "0.0.0-fake", "假 akshare 没被加载，这条用例什么都没证明"
        assert payload["探活通过"] == [] and payload["探活失败"] == [], "--no-probe 不许产生任何探活结果"
        assert payload["函数已不存在"] == [], "桩把 31 个函数名都挂上了，这里不该有漏网的"
    else:
        assert "akshare 版本: 0.0.0-fake" in result.stdout, "假 akshare 没被加载，这条用例什么都没证明"
        assert "接口总数: " in result.stdout
        assert "没发任何网络请求" in result.stdout
        assert "函数名: 全部对得上。" in result.stdout


@pytest.mark.parametrize("extra", ["", " --json"])
def test_advanced_catalog_pipes_into_head_without_broken_pipe(extra):
    """`zt advanced catalog [--json] | head -N` 都不许在 stderr 吐 BrokenPipeError

    Python 默认忽略 SIGPIPE，head 一关管子后面的 print 就抛异常，退出时再吐一句
    "Exception ignored in <_io.TextIOWrapper name='<stdout>'>"——看着像目录本身出了故障。
    --json 那支同样是拿来 `| head` 的（json.dumps(indent=2) 有好几百行），
    SIGPIPE 复位只做在文本分支的话，它照样会吐这一句。
    """
    import shlex

    zt = f"{shlex.quote(sys.executable)} -m modules.cli advanced catalog{extra}"

    result = subprocess.run(
        ["sh", "-c", f"{zt} | head -5"], capture_output=True, text=True, timeout=60, cwd=str(PROJECT_ROOT)
    )
    assert result.stderr.strip() == "", f"catalog 被 head 截断后往 stderr 吐了东西:\n{result.stderr}"
    assert result.stdout.count("\n") == 5

    # `| head -5` 撞不撞得上 EPIPE 要看双方谁快：目录只有十几 KB，管道缓冲区装得下，
    # 实测把修复去掉后 30 次里也只有 2 次真的报错——拿它当探测器等于掷骰子。
    # `head -n 0` 则是确定的：它一个字节都不读就退出，zt 的第一次 write 必然撞上已关闭的管子
    # （去掉 --json 那支的复位后，实测 10/10 复现）。
    closed = subprocess.run(
        ["sh", "-c", f"{zt} | head -n 0"], capture_output=True, text=True, timeout=60, cwd=str(PROJECT_ROOT)
    )
    assert closed.stderr.strip() == "", (
        f"读端一关就退出时，catalog 往 stderr 吐了 BrokenPipeError:\n{closed.stderr}"
    )
    assert closed.stdout == ""


# ---------- get 的渲染：行索引里的日期不许被丢掉 ----------


@pytest.fixture
def preheated_index_cache(no_network_env, tmp_path, monkeypatch):
    """把一张 index 是 DatetimeIndex(name='日期') 的表灌进缓存，返回 (env, 期望的日期串)。

    灌缓存走的是真的 fetch（假 akshare 只负责产表），这样缓存键、payload 格式全都由实现自己
    生成——手写缓存键的话，实现一改键格式测试就变成空跑。
    灌完之后子进程只会命中缓存，连 akshare 都不会 import，所以整条用例离线。
    """
    import pandas as pd

    from modules import advanced_data as adv

    frame = pd.DataFrame(
        {"收盘": [1234.5, 1250.0], "成交量": [100, 200]},
        index=pd.to_datetime(["2026-08-13", "2026-08-14"]),
    )
    frame.index.name = "日期"

    cache_db = tmp_path / "advanced_cache.db"
    monkeypatch.setenv("ADVANCED_CACHE_PATH", str(cache_db))
    monkeypatch.setenv("ADVANCED_MIN_INTERVAL", "0")
    monkeypatch.setitem(
        sys.modules, "akshare", type("_FakeAk", (), {"stock_board_industry_index_ths": staticmethod(lambda **p: frame)})
    )
    adv.reset_advanced_source()
    try:
        assert adv.get_advanced_source().fetch("ths_industry_index", {"symbol": "元件"}) is not None
    finally:
        adv.reset_advanced_source()

    env = dict(no_network_env)
    env["ADVANCED_CACHE_PATH"] = str(cache_db)
    env["ADVANCED_MIN_INTERVAL"] = "0"
    return env, "2026-08-13"


def test_advanced_get_text_output_keeps_the_date_index(preheated_index_cache):
    """ths_industry_index 的行标识在 DatetimeIndex 里，`to_string(index=False)` 会把它整个丢掉。

    丢了之后输出只剩"收盘 / 成交量"两列——这几行到底是哪几天，看的人完全无从判断。
    31 条接口里只有这一条是这样（R2 那条 index 侧车就是为它写的），所以没有别的用例会撞上。
    """
    env, first_day = preheated_index_cache
    result = run_zt_offline(env, "advanced", "get", "ths_industry_index", "--param", "symbol=元件")

    assert result.returncode == 0, f"get 挂了:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    assert "NETWORK-BLOCKED-BY-TEST" not in result.stderr, "本该命中缓存，却去打了上游"
    assert "日期" in result.stdout, "行索引里的日期被丢掉了：输出只剩收盘/成交量，看不出是哪几天"
    assert first_day in result.stdout, f"日期值没打出来:\n{result.stdout}"


def test_advanced_get_json_output_keeps_the_date_index(preheated_index_cache):
    """--json 走 orient="records"，同样会把 index 丢掉；columns 也要跟着带上日期。

    JSON 这条比文本更要紧：下游是程序在读，少一列日期就是把两行数据拼成一堆无主的数字。
    """
    env, first_day = preheated_index_cache
    result = run_zt_offline(env, "advanced", "get", "ths_industry_index", "--param", "symbol=元件", "--json")

    assert result.returncode == 0, f"get --json 挂了:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert "日期" in payload["columns"], f"columns 里没有日期: {payload['columns']}"
    assert payload["rows"] == 2
    assert [str(rec["日期"])[:10] for rec in payload["data"]] == ["2026-08-13", "2026-08-14"]
    assert first_day in str(payload["data"][0]["日期"])


def test_advanced_catalog_survives_being_called_off_the_main_thread():
    """cmd_advanced 的 catalog 分支不许因为一句 SIGPIPE 复位就在子线程里崩掉。

    signal.signal() 在非主线程直接抛 ValueError("signal only works in main thread of the main
    interpreter")。api/ 下有线程池，谁把 CLI 函数接进去，`advanced catalog` 就会栽在一句
    纯装饰性的信号复位上——而那句只是为了让 `| head` 不吐 BrokenPipeError。
    """
    import threading

    from modules.cli import build_parser, cmd_advanced

    args = build_parser().parse_args(["advanced", "catalog", "--json"])
    box: dict[str, BaseException | None] = {}

    def _run():
        try:
            cmd_advanced(args)
            box["error"] = None
        except BaseException as exc:  # 子线程里的任何异常都要带回主线程，否则只会打一行 traceback 然后全绿
            box["error"] = exc

    worker = threading.Thread(target=_run)
    worker.start()
    worker.join(30)

    assert not worker.is_alive(), "catalog 在子线程里卡住了"
    assert box["error"] is None, f"catalog 在子线程里抛了异常: {box['error']!r}"


# ---------- 补覆盖：selfcheck 点名、get 成功路径、参数错误路径 ----------
#
# 下面这一批对应的实现当时都是对的，补它们的理由全是变异测试：把对应实现改坏
# （删掉一句 append、把 force 写死成 False、去掉一个参数校验），全库照样绿。
# 也就是说这些行今天正确纯属运气，明天谁顺手动一下，回归是**静默**的。


@pytest.fixture
def fake_akshare_missing_one(no_network_env, tmp_path):
    """断网环境 + 一个**少挂了一个函数**的假 akshare，返回 (env, 缺掉的那条 Interface)。

    模拟的是这一层最常见的静默故障：akshare 升级把某个函数改名了。
    """
    from modules.advanced_data import BY_KEY, INTERFACES

    gone = BY_KEY["zt_pool"]
    site_dir = tmp_path / "fake_akshare_missing"
    site_dir.mkdir()
    funcs = sorted({spec.func for spec in INTERFACES} - {gone.func})
    (site_dir / "akshare.py").write_text(_FAKE_AKSHARE_TEMPLATE % {"funcs": funcs}, encoding="utf-8")

    env = dict(no_network_env)
    env["PYTHONPATH"] = os.pathsep.join([str(site_dir), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env["ADVANCED_PROBE_LOG"] = str(tmp_path / "upstream_calls_missing.log")
    env["ADVANCED_CACHE_PATH"] = str(tmp_path / "advanced_cache_missing.db")
    env["ADVANCED_MIN_INTERVAL"] = "0"
    return env, gone


def test_advanced_selfcheck_names_the_function_upstream_removed(fake_akshare_missing_one):
    """上游删了一个函数，`zt advanced selfcheck --no-probe` 必须在 stdout 里**点名**。

    把 selfcheck 里那句 ``result["函数已不存在"].append(...)`` 删掉，全库一条不红——
    现有用例只钉了"全都在"那一侧。删掉之后自检永远打"函数名: 全部对得上。"，
    而 akshare 改函数名恰恰是这一层最常见的静默故障：真正断掉的那几条要等到有人去 fetch
    才发现，那时报错只剩一句"找不到函数"，看不出是哪一条、也不知道该去改注册表。

    断言钉的是**整行**（key 与 func 并排那一行），不是各自出现过就算：
    只查"key 在 stdout 里"的话，把两条的 key 和 func 配错人也照样绿。
    """
    env, gone = fake_akshare_missing_one

    result = run_zt_offline(env, "advanced", "selfcheck", "--no-probe", timeout=180)

    assert result.returncode == 0, f"selfcheck 挂了:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    assert "akshare 版本: 0.0.0-fake" in result.stdout, "假 akshare 没被加载，这条用例什么都没证明"
    assert "! 函数已不存在 1 条" in result.stdout, "上游删了函数，自检却没报出来"
    assert f"    {gone.key:<18} {gone.func}" in result.stdout, "点名要 key 和 func 并排给全，少一样都得再翻一遍注册表"
    assert "函数名: 全部对得上。" not in result.stdout, "少了一个函数还报『全部对得上』"


@pytest.fixture
def preheated_wide_cache(fake_akshare_env, monkeypatch):
    """把一张 **50 行、缺期望列**的表灌进 fake_akshare_env 那个缓存库，原样返回它的三元组。

    灌缓存走的是真的 fetch（假 akshare 只负责产表），缓存键和 payload 格式全由实现自己生成。
    灌完之后子进程默认只会命中缓存，一次上游调用都不会有——``--force`` 那条例外，
    正好拿来证明 ``--force`` 真的把缓存绕开了（会在 probe_log 里留下记账）。
    """
    import pandas as pd

    from modules import advanced_data as adv

    env, probe_log, cache_db = fake_akshare_env
    # zt_pool 期望 5 列（代码/名称/连板数/封板资金/首次封板时间），这里只给 2 列 → 必然缺列告警
    frame = pd.DataFrame(
        {"代码": [f"{i:06d}" for i in range(50)], "名称": [f"股票{i}" for i in range(50)]}
    )

    monkeypatch.setenv("ADVANCED_CACHE_PATH", str(cache_db))
    monkeypatch.setenv("ADVANCED_MIN_INTERVAL", "0")
    monkeypatch.setitem(
        sys.modules, "akshare", type("_FakeAk", (), {"stock_zt_pool_em": staticmethod(lambda **p: frame)})
    )
    adv.reset_advanced_source()
    try:
        assert adv.get_advanced_source().fetch("zt_pool", {"date": "20260814"}) is not None
    finally:
        adv.reset_advanced_source()

    assert _upstream_calls(probe_log) == [], "灌缓存是在本进程做的，不该在子进程的记账里留下痕迹"
    return env, probe_log, cache_db


def test_advanced_get_text_output_truncates_and_keeps_warnings(preheated_wide_cache):
    """`get zt_pool --limit 5`：总行数、"只显示前 5 行"、缺列告警，三样一个都不能少。

    这条渲染路径整段没有测试。实测把 ``shown = df.head(args.limit)`` 改成 ``shown = df``
    （--limit 失效，50 行全打出来）、或者把打 warnings 那个 for 循环删掉（缺列告警被吞），
    全库都一条不红。而这两件事都是用户直接看得见的：前者让 `zt advanced get` 刷屏，
    后者更糟——上游改了列名，命令照常打出一张缺列的表，看的人完全不知道数据是残缺的。
    """
    env, probe_log, _ = preheated_wide_cache

    result = run_zt_offline(env, "advanced", "get", "zt_pool", "--param", "date=20260814", "--limit", "5")

    assert result.returncode == 0, f"get 挂了:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    assert "NETWORK-BLOCKED-BY-TEST" not in result.stderr
    assert _upstream_calls(probe_log) == [], "本该命中缓存，却去打了上游"

    assert "zt_pool  50 行 × 2 列" in result.stdout, "总行数/列数是判断这次取数完不完整的第一眼线索"
    assert "（只显示前 5 行）" in result.stdout, "--limit 截断了却不说，看的人会以为上游只给了 5 行"
    assert "! 期望列缺失" in result.stdout, "缺列告警被吞了：残缺的表被当成正常结果打了出来"
    assert "连板数" in result.stdout, "告警里要列出到底缺了哪几列"
    # 表体真的只有 5 行：股票0..股票4 各一次
    assert result.stdout.count("股票") == 5, f"--limit 没生效，表体不是 5 行:\n{result.stdout}"
    assert "股票49" not in result.stdout


def test_advanced_get_json_output_reports_rows_shown_and_warnings(preheated_wide_cache):
    """--json 这一支要把 rows / shown / columns / warnings 都摆出来（下游是程序在读）。

    ``rows`` 是总行数、``shown`` 是这次打出来几行——两个混成一个，调用方就分不清
    "上游只有 5 行"和"被 --limit 截成了 5 行"。warnings 同理：JSON 的消费者看不见 stderr，
    缺列这件事只能靠这个字段传出去。
    """
    env, probe_log, _ = preheated_wide_cache

    result = run_zt_offline(
        env, "advanced", "get", "zt_pool", "--param", "date=20260814", "--limit", "5", "--json"
    )

    assert result.returncode == 0, f"get --json 挂了:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    assert _upstream_calls(probe_log) == [], "本该命中缓存，却去打了上游"

    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["key"] == "zt_pool"
    assert payload["params"] == {"date": "20260814"}
    assert payload["rows"] == 50, "rows 要给总行数，不是截断后的行数"
    assert payload["shown"] == 5
    assert len(payload["data"]) == 5
    assert payload["columns"] == ["代码", "名称"]
    assert payload["data"][0]["代码"] == "000000", "前导零在 --json 这条路上也不能丢"
    assert any("期望列缺失" in w for w in payload["warnings"]), "缺列告警没进 JSON，程序侧完全看不见"


def test_advanced_get_json_degrades_to_strings_instead_of_crashing(monkeypatch, capsys):
    """上游塞进 ``to_json`` 认不得的东西时，--json 退化成字符串，绝不整条炸掉。

    那圈 ``except (ValueError, TypeError, OverflowError)`` 去掉，全库一条不红——
    正常路径（akshare 的表 + 缓存往返）里永远塞不进这种值，现有用例也构造不出来。
    可 --json 的消费者是程序：整条命令抛 OverflowError 的话，调用方拿到的是空 stdout
    加一屏 traceback，连"哪条接口出的事"都得自己去猜。宁可这一列变成 ``"1E+400"``
    这种一眼看得出不对劲的字符串，也不能让整批数据取不出来。
    """
    import decimal

    import pandas as pd

    from modules import advanced_data as adv
    from modules.cli import build_parser, cmd_advanced

    class _Weird:
        last_error = None
        warnings = ["上游返回了认不得的东西"]

        def fetch(self, key, params=None, *, force=False):
            # 装不进 double 的 Decimal：to_json 抛 OverflowError，astype(str) 还能救回来
            return pd.DataFrame(
                {"代码": ["000001"], "怪数": pd.Series([decimal.Decimal("1e400")], dtype=object)}
            )

    monkeypatch.setattr(adv, "get_advanced_source", lambda: _Weird())
    args = build_parser().parse_args(["advanced", "get", "hot_rank", "--json"])

    cmd_advanced(args)  # 不外抛本身就是断言的一部分

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["rows"] == 1
    assert payload["data"][0]["代码"] == "000001", "能正常序列化的列不该被这次退化拖累"
    assert payload["data"][0]["怪数"] == "1E+400", "认不得的那一列被丢了，或者根本没退化成字符串"
    assert payload["warnings"] == ["上游返回了认不得的东西"], "退化路径上 warnings 也不能吞"


def test_advanced_get_renders_a_cache_hit_exactly_like_a_fresh_fetch(preheated_wide_cache):
    """同一条 get，冷缓存和热缓存必须打出**一模一样**的列——差一列都是错。

    这条钉的是 A-1 那个 reset_index 修复的边界。行标识确实要从 index 还原成列
    （ths_industry_index 的日期就在 index 里），但"index 里有没有行标识"一度是拿
    ``isinstance(df.index, pd.RangeIndex)`` 判的，而**走缓存回来的普通行号是
    ``Index([0,1,...], dtype=int64)``，不是 RangeIndex**——split json 里存的就是一串整数。
    于是那 30 条本来是 RangeIndex 的接口，一旦命中缓存就凭空多出一列叫 ``index`` 的行号：
    ``zt advanced get zt_pool`` 第一次打 2 列、第二次打 3 列，--json 的每条记录多一个
    ``index`` 键，下游按列名取数直接错位。热缓存才是常态，所以这是天天都会撞上的。

    对拍"冷 vs 热"是唯一靠得住的写法：只看热缓存那一次的话，多出来的 index 列长得很正常，
    肉眼和断言都容易放过去。
    """
    env, probe_log, cache_db = preheated_wide_cache

    hot = json.loads(
        run_zt_offline(env, "advanced", "get", "zt_pool", "--param", "date=20260814", "--json").stdout
    )
    assert _upstream_calls(probe_log) == [], "这一次本该吃缓存"

    # --force 绕开缓存，走的是"上游刚给的表"那条路（RangeIndex），拿它当冷缓存的基准
    cold = json.loads(
        run_zt_offline(
            env, "advanced", "get", "zt_pool", "--param", "date=20260814", "--force", "--json"
        ).stdout
    )
    assert _upstream_calls(probe_log) == ["stock_zt_pool_em"], "--force 那次没去打上游，对拍不成立"

    assert hot["columns"] == cold["columns"], "命中缓存时的列和刚取回来时不一样，多半是凭空 reset 出了一列行号"
    assert "index" not in hot["columns"], f"缓存命中时多出了一列行号: {hot['columns']}"
    assert set(hot["data"][0]) == set(cold["data"][0]), "--json 每条记录的键也要对得上"


def test_advanced_get_force_really_bypasses_the_cache(preheated_wide_cache):
    """`--force` 必须真的绕开缓存去打上游，不是只把标志位传下去。

    把 ``force=args.force`` 写死成 ``force=False``，全库一条不红——两次跑都从缓存出数，
    输出一模一样。而 ``--force`` 存在的全部意义就是"我怀疑缓存里的是旧的"：
    它一旦哑火，用户会拿着 12 小时前的涨停池当今天的看，而且没有任何迹象。

    证据是假 akshare 的记账：不带 --force 一次调用都没有，带上就必须有一次。
    """
    env, probe_log, _ = preheated_wide_cache

    cached = run_zt_offline(env, "advanced", "get", "zt_pool", "--param", "date=20260814", "--json")
    assert cached.returncode == 0, f"stderr: {cached.stderr}"
    assert _upstream_calls(probe_log) == [], "不带 --force 就该吃缓存"
    assert json.loads(cached.stdout)["rows"] == 50

    forced = run_zt_offline(env, "advanced", "get", "zt_pool", "--param", "date=20260814", "--force", "--json")
    assert forced.returncode == 0, f"stderr: {forced.stderr}"
    assert _upstream_calls(probe_log) == ["stock_zt_pool_em"], "--force 没去打上游：缓存根本没被绕开"
    # 桩返回的是 1 行，缓存里那张是 50 行——行数不同才说明这次数据是新取的
    assert json.loads(forced.stdout)["rows"] == 1, "--force 拿回来的还是缓存里那 50 行"


def test_advanced_catalog_prints_ttl_in_human_units(no_network_env):
    """目录里的 TTL 要写成人话：摆一个 ``43200`` 在那儿，没人一眼认得出是 12 小时。

    ``_fmt_ttl`` 整个函数没有测试，把哪一档的换算改坏（比如去掉"天"那一档，30 天变成
    720 小时）全库都不红。它只影响可读性，可这份目录正是给人和 agent 挑接口用的——
    TTL 是"这条数据多久才会变"的唯一线索，看不懂就只能瞎猜要不要加 --force。
    """
    result = run_zt_offline(no_network_env, "advanced", "catalog")

    assert result.returncode == 0, f"catalog 挂了:\nstderr: {result.stderr}"
    # 注册表里 600/1800/21600/43200/86400/604800/2592000 七档全都在用，逐档钉
    for token in ("TTL 10分钟", "TTL 30分钟", "TTL 6小时", "TTL 12小时", "TTL 1天", "TTL 7天", "TTL 30天"):
        assert token in result.stdout, f"TTL 没换算成人话，缺 {token!r}"
    for raw in ("TTL 600", "TTL 43200", "TTL 2592000"):
        assert raw not in result.stdout, f"目录里直接摆了秒数 {raw!r}"


def test_advanced_without_an_action_exits_nonzero():
    """`zt advanced` 不给动作必须失败（``required=True``），不能一声不吭地退成功。

    去掉 ``required=True`` 后 ``advanced_action`` 是 None，cmd_advanced 三个分支全不匹配、
    函数走到底 return None，exit 0 —— 用户敲了个不完整的命令，屏幕上什么都没有、
    返回码还是成功，脚本里更是完全察觉不到。
    """
    from modules.cli import build_parser

    with pytest.raises(SystemExit) as caught:
        build_parser().parse_args(["advanced"])
    assert caught.value.code != 0

    result = run_zt("advanced")
    assert result.returncode != 0, f"`zt advanced` 不给动作却成功了:\nstdout: {result.stdout}"
    assert "usage:" in result.stderr


def test_advanced_catalog_unknown_category_fails_with_the_available_list(no_network_env):
    """`catalog --category 乱写` 要 exit 1 并把可用分组列出来，不能抛 KeyError。

    去掉那道 ``if args.category not in grouped`` 的校验，走的是 ``grouped[args.category]``
    的 KeyError：一屏 traceback，最后一行只有一个孤零零的分组名，看的人不知道正确的写法
    是什么（六个分组名全是中文，打错太容易了）。
    """
    result = run_zt_offline(no_network_env, "advanced", "catalog", "--category", "资金")

    assert result.returncode == 1, f"期望 exit 1，实际 {result.returncode}\nstderr: {result.stderr}"
    assert "Traceback" not in result.stderr, f"分组打错抛了 traceback:\n{result.stderr}"
    assert "! 未知分组: 资金" in result.stdout
    for category in EXPECTED_ADVANCED_CATEGORIES:
        assert category in result.stdout, f"没把可用分组列全，缺 {category}"


@pytest.mark.parametrize(
    ("bad", "expected"),
    [("junk", "--param 要写成 k=v"), ("=20260814", "--param 的参数名是空的")],
    ids=["没有等号", "参数名是空的"],
)
def test_advanced_get_rejects_malformed_param_before_touching_network(no_network_env, bad, expected):
    """``--param`` 写错要 exit 2 并说清错在哪，而且必须在发请求之前就失败。

    两道校验（``"=" not in item``、``name`` 为空）都没有测试。去掉前一道，
    ``item.split("=", 1)`` 直接抛 ValueError（一屏 traceback）；去掉后一道，
    参数名会变成空串传下去，报错文案变成"接口 zt_pool 不接受参数 ['']"——
    指向了接口，可错在用户手上，方向反了。

    exit 2 而不是 1 是刻意的：2 是"你命令行敲错了"，1 是"取数失败了"，
    脚本靠这个区分要不要重试。
    """
    result = run_zt_offline(no_network_env, "advanced", "get", "zt_pool", "--param", bad)

    assert result.returncode == 2, f"期望 exit 2（命令行写错），实际 {result.returncode}\nstderr: {result.stderr}"
    assert "NETWORK-BLOCKED-BY-TEST" not in result.stderr, "参数都没校验完就先联网了"
    assert "Traceback" not in result.stderr, f"参数写错抛了 traceback:\n{result.stderr}"
    assert expected in result.stdout, f"报错没说清错在哪:\n{result.stdout}"
    assert repr(bad) in result.stdout, "要把收到的原文回显出来，否则一堆 --param 里不知道是哪个写错了"


def test_advanced_get_param_value_may_contain_equals(monkeypatch):
    """``--param k=a=b`` 只在**第一个**等号处切开：参数名 ``k``，参数值 ``a=b``。

    ``item.split("=", 1)`` 去掉那个 ``1`` 全库一条不红——现有用例的参数值里都没有等号。
    去掉之后 ``a=b`` 这种值会让解包直接抛 ValueError（"too many values to unpack"），
    而带等号的参数值是真会出现的（板块名、带查询串的 symbol）。

    这条不走子进程：要看的是**切出来的东西**，不是打印出来的东西，
    所以直接把假数据源塞进去，把 fetch 收到的 params 原样接住。
    """
    import pandas as pd

    from modules import advanced_data as adv
    from modules.cli import build_parser, cmd_advanced

    seen: dict[str, object] = {}

    class _Recorder:
        last_error = None
        warnings: list[str] = []

        def fetch(self, key, params=None, *, force=False):
            seen["key"] = key
            seen["params"] = dict(params or {})
            seen["force"] = force
            return pd.DataFrame({"代码": ["000001"]})

    monkeypatch.setattr(adv, "get_advanced_source", lambda: _Recorder())

    args = build_parser().parse_args(
        ["advanced", "get", "ths_industry_index", "--param", "symbol=A=B", "--param", " start_date =20260101"]
    )
    cmd_advanced(args)

    assert seen["key"] == "ths_industry_index"
    assert seen["params"] == {"symbol": "A=B", "start_date": "20260101"}, (
        "参数值里的等号被当成分隔符了（或者参数名两侧的空格没去掉）"
    )
    assert seen["force"] is False
