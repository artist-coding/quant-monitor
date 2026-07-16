#!/usr/bin/env python3
"""批量转写已下载的 B 站音频（faster-whisper base 模型）"""

from pathlib import Path

from faster_whisper import WhisperModel

ARCHIVE_ROOT = Path(__file__).resolve().parent.parent
INPUT_DIR = ARCHIVE_ROOT / "references" / "sources" / "transcripts"
MODEL_SIZE = "base"


def main():
    model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
    files = sorted(INPUT_DIR.glob("*_audio.m4a"))
    print(f"找到 {len(files)} 个音频文件，开始转写...", flush=True)
    failed = []

    for i, audio_path in enumerate(files, 1):
        bvid = audio_path.name.removesuffix("_audio.m4a")
        out_path = INPUT_DIR / f"{bvid}_transcript.txt"
        temp_path = out_path.with_suffix(".txt.tmp")

        if out_path.exists():
            print(f"[{i}/{len(files)}] {bvid} 已转写，跳过", flush=True)
            continue

        print(f"[{i}/{len(files)}] 转写 {bvid} ...", flush=True)
        try:
            segments, _ = model.transcribe(str(audio_path), beam_size=5, language="zh")
            text = "\n".join(segment.text.strip() for segment in segments if segment.text.strip())
            temp_path.write_text(text, encoding="utf-8")
            temp_path.replace(out_path)
            print(f"[{i}/{len(files)}] {bvid} 完成，字数 {len(text)}", flush=True)
        except Exception as exc:
            temp_path.unlink(missing_ok=True)
            failed.append(bvid)
            print(f"[{i}/{len(files)}] {bvid} 失败: {exc}", flush=True)

    if failed:
        print(f"转写完成，失败 {len(failed)} 个: {', '.join(failed)}", flush=True)
        raise SystemExit(1)
    print("全部转写完成！", flush=True)


if __name__ == "__main__":
    main()
