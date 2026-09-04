"""资料库：把仓库里的 HTML 文件（方案文档、自写研报）列出来给看板原样展示。

为什么走后端而不是把文件塞进前端的 public/
------------------------------------------

- 生产环境跑的是 ``vite preview`` 提供的 dist/，改完文件要重新 build 才生效；
  研报是随手写、随手放的，不该有构建这一步。
- 目录可以是仓库外的任意路径（``LIBRARY_DIRS``），比如不想入库的个人研报。
- 后端只做两件事：列出来、原样返回。渲染交给前端的 iframe。

安全边界
--------

- 只在配置的根目录里找文件：``..``、绝对路径、反斜杠一律拒绝，
  解析后的真实路径（跟随符号链接）必须仍在根目录内。
- 只**列出** ``.html/.htm``；旁路资源（图片、CSS、JS、字体、数据文件）允许按扩展名
  白名单**读取**，研报里 ``<img src="img/x.png">`` 这样的相对引用才能工作。
- 以点开头的目录和文件不可见、不可读。
- 前端用不带 ``allow-same-origin`` 的 sandbox iframe 展示，页面里的脚本拿不到看板的存储。
"""

from __future__ import annotations

import html
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from api.config import settings

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

HTML_SUFFIXES = {".html", ".htm"}
# 旁路资源白名单：只放行研报可能引用的静态类型，其余扩展名一律 404。
ASSET_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".pdf": "application/pdf",
}
# 扫描时跳过的目录：docs/archive 是历史归档，不该混进资料库
SKIP_DIRS = {"archive", "node_modules", "__pycache__"}
# 解析 <title> / <meta name="description"> 只读文件头部，研报可能很大
_HEAD_BYTES = 65536
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_META_DESC_RE = re.compile(r"<meta\s+[^>]*name=[\"']description[\"'][^>]*>", re.IGNORECASE)
_CONTENT_RE = re.compile(r"content=[\"']([^\"']*)[\"']", re.IGNORECASE)


@dataclass(frozen=True)
class LibraryRoot:
    key: str  # URL 里用的标识，只含 [A-Za-z0-9_-]
    label: str  # 展示名
    path: Path  # 绝对路径


def parse_roots(spec: str) -> list[LibraryRoot]:
    """解析 ``路径=标签;路径=标签``。相对路径相对仓库根；标签缺省用目录名。"""
    roots: list[LibraryRoot] = []
    used: set[str] = set()
    for raw in spec.split(";"):
        raw = raw.strip()
        if not raw:
            continue
        path_text, _, label = raw.partition("=")
        path_text = path_text.strip()
        if not path_text:
            continue
        path = Path(path_text).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        base = re.sub(r"[^A-Za-z0-9_-]+", "-", path.name).strip("-") or "root"
        key, n = base, 2
        while key in used:
            key, n = f"{base}-{n}", n + 1
        used.add(key)
        roots.append(LibraryRoot(key=key, label=label.strip() or path.name, path=path))
    return roots


def read_html_meta(path: Path) -> tuple[str, str]:
    """从文件头部取 <title> 与 <meta name="description">，取不到返回空串。"""
    try:
        with path.open("rb") as fh:
            head = fh.read(_HEAD_BYTES).decode("utf-8", errors="ignore")
    except OSError:
        return "", ""
    title = ""
    match = _TITLE_RE.search(head)
    if match:
        title = html.unescape(re.sub(r"\s+", " ", match.group(1))).strip()
    description = ""
    meta = _META_DESC_RE.search(head)
    if meta:
        content = _CONTENT_RE.search(meta.group(0))
        if content:
            description = html.unescape(re.sub(r"\s+", " ", content.group(1))).strip()
    return title, description


def file_url(root_key: str, rel_path: str) -> str:
    return f"{settings.api_prefix}/library/file/{quote(root_key)}/{quote(rel_path, safe='/')}"


def _display_path(path: Path) -> str:
    try:
        return path.relative_to(PROJECT_ROOT).as_posix() + "/"
    except ValueError:
        return str(path)


class LibraryService:
    def __init__(self, spec: str | None = None) -> None:
        self.roots = parse_roots(spec if spec is not None else settings.library_dirs)

    # ==================== 列表 ====================

    def list_items(self) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        roots: list[dict[str, Any]] = []
        for root in self.roots:
            exists = root.path.is_dir()
            found = self._scan(root) if exists else []
            items.extend(found)
            roots.append(
                {
                    "key": root.key,
                    "label": root.label,
                    "path": _display_path(root.path),
                    "exists": exists,
                    "count": len(found),
                }
            )
        items.sort(key=lambda item: item["modified_at"], reverse=True)
        return {"roots": roots, "items": items, "total": len(items)}

    def _scan(self, root: LibraryRoot) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for dirpath, dirnames, filenames in os.walk(root.path):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS)
            for filename in sorted(filenames):
                path = Path(dirpath) / filename
                if filename.startswith(".") or path.suffix.lower() not in HTML_SUFFIXES:
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                rel = path.relative_to(root.path)
                title, description = read_html_meta(path)
                out.append(
                    {
                        "id": f"{root.key}/{rel.as_posix()}",
                        "root": root.key,
                        "root_label": root.label,
                        "rel_path": rel.as_posix(),
                        "name": filename,
                        "title": title or path.stem,
                        "description": description,
                        # 根目录下的第一层子目录名当分类：reports/公司/xxx.html → 公司
                        "category": rel.parts[0] if len(rel.parts) > 1 else "",
                        "size": stat.st_size,
                        "modified_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
                        "url": file_url(root.key, rel.as_posix()),
                    }
                )
        return out

    # ==================== 读取 ====================

    def resolve(self, root_key: str, rel_path: str) -> tuple[Path, str] | None:
        """把 URL 里的 (root, 相对路径) 解析成可读文件；任何越界或不允许的情况返回 None。"""
        root = next((r for r in self.roots if r.key == root_key), None)
        if root is None:
            return None
        if not rel_path or "\\" in rel_path or rel_path.startswith("/"):
            return None
        parts = rel_path.split("/")
        if any(part in ("", ".", "..") or part.startswith(".") for part in parts):
            return None
        try:
            base = root.path.resolve(strict=True)
            target = (root.path / rel_path).resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        if not target.is_relative_to(base) or not target.is_file():
            return None
        suffix = target.suffix.lower()
        if suffix in HTML_SUFFIXES:
            return target, "text/html; charset=utf-8"
        if suffix in ASSET_TYPES:
            return target, ASSET_TYPES[suffix]
        return None


library_service = LibraryService()
