"""
回归测试：文档里写的仓库相对路径必须真实存在

背景：e47a1b8「精简: 移除实验性子系统」删掉了一批模块、测试和 knowledge/，
96a0ba9 又把 corpus/ 和 references/ 挪进了 docs/archive/，但 SKILL.md、AGENTS.md、
GEMINI.md 里的路径没跟着改 —— agent 照着文档去跑 `corpus/quality_check.py`
只会拿到 No such file，照着 AGENTS.md 的测试清单找 `test_intent_router.py` 也找不到。
tests/test_commentary_knowledge.py 只盯 knowledge/，挡不住这一类。

这里做静态检查（不 import 任何业务模块）：把三份 agent 入口文档里**行内反引号**
（不含 ``` 代码块）写的仓库相对路径全解析出来，逐个确认它在。
"""

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = PROJECT_ROOT / "tests"
DOCS = ["SKILL.md", "AGENTS.md", "GEMINI.md"]

# 运行时生成或按设计不入库的路径，克隆出来本就没有，不参与校验
SKIP_PREFIXES = ("data/", "logs/", "references/sources/")
SKIP_EXACT = {".env"}

INLINE_CODE = re.compile(r"`([^`\n]+)`")
DIR_REF = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_./-]*/$")
FILE_REF = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_./-]*/[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,5}$")
DOTFILE_REF = re.compile(r"^\.[A-Za-z0-9][A-Za-z0-9_.-]*$")
BARE_TEST = re.compile(r"^test_[A-Za-z0-9_]+\.py$")
TABLE_ROW_TEST = re.compile(r"`(test_[A-Za-z0-9_]+\.py)`")


def _spans(doc: str) -> list[str]:
    text = (PROJECT_ROOT / doc).read_text(encoding="utf-8")
    out = []
    for raw in INLINE_CODE.findall(text):
        s = raw.strip()
        if not s or s in SKIP_EXACT:
            continue
        if any(ch in s for ch in " \t*$<>|()"):
            continue
        if s.startswith(("/", "~", "http")) or s.startswith(SKIP_PREFIXES):
            continue
        out.append(s)
    return out


def test_referenced_paths_exist():
    """文档里带目录的路径引用必须存在（含根目录下的点文件）"""
    missing: list[str] = []
    checked = 0
    for doc in DOCS:
        for s in _spans(doc):
            if DIR_REF.match(s):
                target, ok = s, (PROJECT_ROOT / s).is_dir()
            elif FILE_REF.match(s) or DOTFILE_REF.match(s):
                target, ok = s, (PROJECT_ROOT / s).exists()
            else:
                continue
            checked += 1
            if not ok:
                hint = ""
                archived = PROJECT_ROOT / "docs" / "archive" / s
                if archived.exists():
                    hint = f"（实际在 docs/archive/{s}）"
                missing.append(f"{doc}: {target}{hint}")

    assert checked > 20, f"只解析到 {checked} 条路径引用，正则可能失效了"
    assert not missing, "文档引用的路径不存在:\n  " + "\n  ".join(missing)


def test_referenced_test_files_exist():
    """文档里提到的 test_*.py 必须在 tests/ 下"""
    missing = sorted(
        {s for doc in DOCS for s in _spans(doc) if BARE_TEST.match(s) and not (TESTS_DIR / s).exists()}
    )
    assert not missing, f"文档提到但 tests/ 下不存在: {missing}"


def test_agents_md_test_table_matches_tests_dir():
    """AGENTS.md 的测试清单就是 tests/ 的目录，两边必须一一对应"""
    text = (PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    start = text.index("### 测试覆盖范围")
    listed = set(TABLE_ROW_TEST.findall(text[start : text.index("### 运行预期", start)]))
    actual = {p.name for p in TESTS_DIR.glob("test_*.py")}

    assert not (listed - actual), f"清单里有、tests/ 下没有: {sorted(listed - actual)}"
    assert not (actual - listed), f"tests/ 下有、清单里漏了: {sorted(actual - listed)}"

    declared = re.search(r"### 测试覆盖范围（当前 (\d+) 个测试文件）", text)
    assert declared, "测试清单的标题格式变了，本测试要同步更新"
    assert int(declared.group(1)) == len(actual), (
        f"标题写 {declared.group(1)} 个，实际 {len(actual)} 个"
    )
