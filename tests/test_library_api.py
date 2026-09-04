"""资料库 API（api/routes/library.py + api/services/library_service.py）测试。

只测 API 层与路径安全：列表口径（标题解析、分类、隐藏文件）、文件原样返回、
越界与非白名单扩展名的拒绝。不碰数据库。
"""

from __future__ import annotations

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.services import library_service as ls


@pytest.fixture
def roots(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    reports = tmp_path / "reports"
    docs.mkdir()
    (reports / "公司").mkdir(parents=True)
    (reports / ".private").mkdir()
    (docs / "plan.html").write_text(
        '<title>方案 &amp;\n 契约</title><meta name="description" content="一句话说明">', encoding="utf-8"
    )
    (reports / "公司" / "moutai.html").write_text(
        "<html><head><title>\n 贵州茅台 研报 \n</title></head></html>", encoding="utf-8"
    )
    (reports / "公司" / "chart.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (reports / "公司" / "notes.txt").write_text("x", encoding="utf-8")
    (reports / "untitled.htm").write_text("<p>no title</p>", encoding="utf-8")
    (reports / ".private" / "secret.html").write_text("<title>s</title>", encoding="utf-8")
    (reports / ".hidden.html").write_text("<title>h</title>", encoding="utf-8")
    (tmp_path / "outside.html").write_text("<title>outside</title>", encoding="utf-8")
    missing = tmp_path / "missing"
    monkeypatch.setattr(
        ls.library_service,
        "roots",
        ls.parse_roots(f"{docs}=方案文档;{reports}=研报;{missing}=不存在"),
    )
    return tmp_path


@pytest.fixture
def client(roots):
    return TestClient(app)


# ==================== 列表 ====================


def test_list_items_and_roots(client):
    r = client.get("/api/v1/library/")
    assert r.status_code == 200
    body = r.json()

    roots = {root["key"]: root for root in body["roots"]}
    assert set(roots) == {"docs", "reports", "missing"}
    assert roots["docs"]["label"] == "方案文档"
    assert roots["missing"]["exists"] is False and roots["missing"]["count"] == 0
    assert roots["reports"]["count"] == 2

    items = {item["id"]: item for item in body["items"]}
    # 隐藏目录、隐藏文件、非 html 一概不列
    assert set(items) == {"docs/plan.html", "reports/公司/moutai.html", "reports/untitled.htm"}
    assert body["total"] == 3

    plan = items["docs/plan.html"]
    assert plan["title"] == "方案 & 契约"  # 实体解码 + 空白折叠
    assert plan["description"] == "一句话说明"
    assert plan["category"] == ""
    assert plan["root_label"] == "方案文档"

    moutai = items["reports/公司/moutai.html"]
    assert moutai["title"] == "贵州茅台 研报"
    assert moutai["category"] == "公司"
    assert moutai["url"] == f"/api/v1/library/file/reports/{quote('公司')}/moutai.html"

    assert items["reports/untitled.htm"]["title"] == "untitled"  # 没有 <title> 用文件名


# ==================== 读取 ====================


def test_serve_html_and_sidecar_asset(client):
    r = client.get(f"/api/v1/library/file/reports/{quote('公司')}/moutai.html")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "贵州茅台" in r.text
    assert r.headers["cache-control"] == "no-cache"

    r = client.get(f"/api/v1/library/file/reports/{quote('公司')}/chart.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"

    # .txt 不在旁路资源白名单里
    r = client.get(f"/api/v1/library/file/reports/{quote('公司')}/notes.txt")
    assert r.status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/library/file/reports/%2e%2e/outside.html",  # 编码的 ..
        "/api/v1/library/file/reports/..%2Foutside.html",
        "/api/v1/library/file/reports/.private/secret.html",  # 隐藏目录
        "/api/v1/library/file/reports/.hidden.html",  # 隐藏文件
        "/api/v1/library/file/nowhere/plan.html",  # 未配置的根
        "/api/v1/library/file/missing/plan.html",  # 配置了但目录不存在
        f"/api/v1/library/file/reports/{quote('公司')}",  # 目录不是文件
    ],
)
def test_rejects_out_of_root_and_hidden(client, path):
    assert client.get(path).status_code == 404


def test_resolve_never_escapes_root(roots):
    ok = ls.library_service.resolve("reports", "公司/moutai.html")
    assert ok is not None and ok[0].name == "moutai.html" and ok[1].startswith("text/html")

    assert ls.library_service.resolve("reports", "公司/../../outside.html") is None
    assert ls.library_service.resolve("reports", "/etc/passwd") is None
    assert ls.library_service.resolve("reports", "公司\\moutai.html") is None
    assert ls.library_service.resolve("reports", "") is None
    assert ls.library_service.resolve("docs", "plan.html") is not None


# ==================== 配置解析 ====================


def test_parse_roots_keys_and_labels(tmp_path):
    roots = ls.parse_roots(f"docs=方案文档;{tmp_path / 'docs'}=外部;;reports")
    assert [r.key for r in roots] == ["docs", "docs-2", "reports"]
    assert roots[0].label == "方案文档"
    assert roots[1].path == tmp_path / "docs"
    assert roots[2].label == "reports"  # 缺省标签用目录名
    assert roots[0].path == ls.PROJECT_ROOT / "docs"
