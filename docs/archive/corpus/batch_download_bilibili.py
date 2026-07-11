#!/usr/bin/env python3
"""批量下载 B 站合集音频（用于 zettaranc ztalk 语料提取）"""

import subprocess
import sys
import json
import urllib.request
from pathlib import Path

SERIES_ID = "2194911"
MID = "326246517"
ARCHIVE_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ARCHIVE_ROOT / "references" / "sources" / "transcripts"


def get_archives():
    url = f"https://api.bilibili.com/x/series/archives?mid={MID}&series_id={SERIES_ID}&only_normal=true&sort=asc&pn=1&ps=30"
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0", "Referer": f"https://space.bilibili.com/{MID}"}
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    resp = opener.open(req, timeout=15)
    return json.loads(resp.read().decode("utf-8"))["data"]["archives"]


def download_audio(bvid):
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "-f",
        "ba[ext=m4a]/ba",
        "--proxy",
        "",
        "--no-overwrites",
        "-o",
        str(OUTPUT_DIR / f"{bvid}_audio.%(ext)s"),
        f"https://www.bilibili.com/video/{bvid}/",
    ]
    print(f"[下载] {bvid} ...")
    return subprocess.run(cmd, check=False).returncode == 0


if __name__ == "__main__":
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    archives = get_archives()
    print(f"共 {len(archives)} 个视频，开始批量下载音频...")
    failed = []
    for item in archives:
        if not download_audio(item["bvid"]):
            failed.append(item["bvid"])
    if failed:
        print(f"下载完成，失败 {len(failed)} 个: {', '.join(failed)}")
        raise SystemExit(1)
    print("全部下载完成。")
