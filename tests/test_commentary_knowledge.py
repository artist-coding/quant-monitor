"""
回归测试：knowledge/ 里被代码和文档引用的文件必须真实存在

背景：e47a1b8「精简: 移除实验性子系统」把整个 knowledge/ 当作 intent_router 的
附属删掉了，但 modules/commentary_service.py 也在读它，而且 _read_section() 读不到
文件时 return ""，不抛异常不打日志 —— LLM 点评的【参考知识库】那段从 2026-08-07
起一直是空的，三周半没有任何人发现。

这里做静态检查（不 import，不跑 LLM）：把源码里的 _read_section("x.md") 和
SKILL.md 里声明的 knowledge/*.md 路径全都解析出来，逐个确认文件在。
"""

import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
COMMENTARY_SRC = PROJECT_ROOT / "modules" / "commentary_service.py"
SKILL_MD = PROJECT_ROOT / "SKILL.md"
KNOWLEDGE_DIR = PROJECT_ROOT / "knowledge"


def test_knowledge_dir_exists():
    assert KNOWLEDGE_DIR.is_dir(), (
        "knowledge/ 不存在。commentary_service 读不到时会静默返回空串，"
        "不会报错 —— 只能靠这个测试兜住"
    )


def test_read_section_targets_exist():
    """commentary_service.py 里 _read_section() 引用的知识文件必须存在"""
    src = COMMENTARY_SRC.read_text(encoding="utf-8")
    names = sorted(set(re.findall(r'_read_section\(\s*"([^"]+)"', src)))
    assert names, "解析不到 _read_section 调用，说明代码结构变了，这个测试本身要更新"

    missing = [n for n in names if not (KNOWLEDGE_DIR / n).exists()]
    assert not missing, f"代码引用但文件不存在: {missing}"


def test_knowledge_dir_constant_matches_layout():
    """_KNOWLEDGE_DIR 指向的目录就是仓库根的 knowledge/"""
    src = COMMENTARY_SRC.read_text(encoding="utf-8")
    assert '_KNOWLEDGE_DIR = Path(__file__).parent.parent / "knowledge"' in src, (
        "_KNOWLEDGE_DIR 的定义变了，确认新路径后更新本测试"
    )


def test_skill_md_declared_paths_exist():
    """SKILL.md 声明的 knowledge/*.md 必须存在

    这些路径以项目根解析（见 .kimi-code/skills/zettaranc-perspective/SKILL.md），
    Kimi 多智能体调研那条链路按声明去读，文件不在就读到空。
    """
    text = SKILL_MD.read_text(encoding="utf-8")
    paths = sorted(set(re.findall(r"knowledge/[A-Za-z0-9_./-]+\.md", text)))
    assert paths, "SKILL.md 里解析不到 knowledge/ 路径声明，测试本身要更新"

    missing = [p for p in paths if not (PROJECT_ROOT / p).exists()]
    assert not missing, f"SKILL.md 声明但文件不存在: {missing}"
