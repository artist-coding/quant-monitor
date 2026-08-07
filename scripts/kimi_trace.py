#!/usr/bin/env python3
"""启动 Kimi，并在退出后自动整理最近一次会话的执行结果。"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


DATASOURCE_TOOL_MARKER = "plugin-kimi-datasource"
DEFAULT_MAX_ARTIFACT_MB = 100
SENSITIVE_KEYS = {
    "accesstoken",
    "apikey",
    "authorization",
    "credential",
    "credentials",
    "password",
    "refreshtoken",
    "secret",
    "token",
}
PATH_PATTERN = re.compile(r"(?P<path>/[^\s\"'`]+\.(?:csv|json|jsonl|txt|md|xlsx|parquet))")


@dataclass(frozen=True)
class Session:
    path: Path
    wire_path: Path
    modified_ns: int

    @property
    def session_id(self) -> str:
        return self.path.name


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="启动 Kimi，退出后自动导出本次会话；也可以只导出最近一次已有会话。",
        epilog=(
            "示例：\n"
            "  ./scripts/kimi_trace.py\n"
            "  ./scripts/kimi_trace.py -- --model kimi-code/k3\n"
            "  ./scripts/kimi_trace.py --latest\n"
            "  ./scripts/kimi_trace.py --session session_xxx\n"
            "  ./scripts/kimi_trace.py --latest --include-raw"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--latest", action="store_true", help="不启动 Kimi，直接导出最近一次会话")
    mode.add_argument("--session", metavar="ID", help="不启动 Kimi，导出指定的 session ID")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="导出根目录，默认是当前目录的 data/reports/kimi-runs",
    )
    parser.add_argument(
        "--kimi-home",
        type=Path,
        help="Kimi 数据目录，默认读取 KIMI_CODE_HOME，否则使用 ~/.kimi-code",
    )
    parser.add_argument("--include-raw", action="store_true", help="额外保存未脱敏的 raw-wire.jsonl")
    parser.add_argument("--no-archive", action="store_true", help="不生成 tar.gz 压缩包")
    parser.add_argument(
        "--max-artifact-mb",
        type=int,
        default=DEFAULT_MAX_ARTIFACT_MB,
        help=f"单个插件输出文件的复制上限，默认 {DEFAULT_MAX_ARTIFACT_MB} MB",
    )
    parser.add_argument("kimi_args", nargs=argparse.REMAINDER, help="传给 kimi 的参数，放在 -- 之后")
    args = parser.parse_args(argv)
    if args.max_artifact_mb <= 0:
        parser.error("--max-artifact-mb 必须大于 0")
    if (args.latest or args.session) and args.kimi_args:
        parser.error("--latest/--session 不能和 Kimi 参数同时使用")
    if args.kimi_args and args.kimi_args[0] == "--":
        args.kimi_args = args.kimi_args[1:]
    return args


def resolve_kimi_home(value: Path | None = None) -> Path:
    if value is not None:
        return value.expanduser().resolve()
    configured = os.environ.get("KIMI_CODE_HOME")
    return Path(configured).expanduser().resolve() if configured else Path.home() / ".kimi-code"


def find_sessions(kimi_home: Path) -> list[Session]:
    sessions_root = kimi_home / "sessions"
    if not sessions_root.is_dir():
        return []

    sessions: list[Session] = []
    for wire_path in sessions_root.glob("**/agents/main/wire.jsonl"):
        try:
            modified_ns = wire_path.stat().st_mtime_ns
        except OSError:
            continue
        sessions.append(Session(path=wire_path.parents[2], wire_path=wire_path, modified_ns=modified_ns))
    return sorted(sessions, key=lambda item: item.modified_ns, reverse=True)


def select_session(
    kimi_home: Path,
    modified_since_ns: int | None = None,
    session_id: str | None = None,
) -> Session:
    sessions = find_sessions(kimi_home)
    if not sessions:
        raise RuntimeError(f"没有在 {kimi_home / 'sessions'} 找到 Kimi 会话")
    if session_id is not None:
        for session in sessions:
            if session.session_id == session_id:
                return session
        raise RuntimeError(f"没有找到 Kimi 会话：{session_id}")
    if modified_since_ns is None:
        return sessions[0]

    tolerance_ns = 2_000_000_000
    for session in sessions:
        if session.modified_ns >= modified_since_ns - tolerance_ns:
            return session
    raise RuntimeError("Kimi 已退出，但没有找到本次运行产生或更新的会话")


def load_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    events: list[dict[str, Any]] = []
    errors: list[str] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"line {line_number}: {exc}")
                continue
            if isinstance(value, dict):
                value["_export_line"] = line_number
                events.append(value)
            else:
                errors.append(f"line {line_number}: 顶层 JSON 不是对象")
    return events, errors


def normalized_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def is_sensitive_key(key: Any) -> bool:
    normalized = normalized_key(key)
    return normalized in SENSITIVE_KEYS or normalized.endswith("apikey")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "<redacted>" if is_sensitive_key(key) else redact(child)
            for key, child in value.items()
            if key != "_export_line"
        }
    if isinstance(value, list):
        return [redact(child) for child in value]
    return value


def event_payload(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("event")
    return payload if isinstance(payload, dict) else {}


def event_trace_id(record: dict[str, Any]) -> str:
    event = event_payload(record)
    value = event.get("traceId", record.get("traceId", ""))
    return str(value) if value else ""


def event_time(record: dict[str, Any]) -> int | None:
    event = event_payload(record)
    value = event.get("time", record.get("time"))
    return value if isinstance(value, int) else None


def iso_time(timestamp_ms: int | None) -> str:
    if timestamp_ms is None:
        return ""
    return datetime.fromtimestamp(timestamp_ms / 1000).astimezone().isoformat(timespec="milliseconds")


def iter_text_content(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for child in value:
            yield from iter_text_content(child)
    elif isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str):
            yield text


def extract_transcript(events: list[dict[str, Any]]) -> tuple[str, str]:
    transcript: list[str] = []
    final_answers: list[str] = []

    for record in events:
        if record.get("type") == "turn.prompt":
            origin = record.get("origin")
            if not isinstance(origin, dict) or origin.get("kind") != "user":
                continue
            prompt = "\n".join(iter_text_content(record.get("input"))).strip()
            if prompt:
                transcript.append(f"## User\n\n{prompt}\n")
            continue

        event = event_payload(record)
        if record.get("type") != "context.append_loop_event" or event.get("type") != "content.part":
            continue
        part = event.get("part")
        if not isinstance(part, dict) or part.get("type") != "text":
            continue
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            transcript.append(f"## Kimi\n\n{text.strip()}\n")
            final_answers.append(text.strip())

    transcript_text = "# Kimi Transcript\n\n" + "\n".join(transcript)
    final_text = final_answers[-1] + "\n" if final_answers else "本次会话没有记录最终文本回答。\n"
    return transcript_text, final_text


def write_jsonl(path: Path, records: Iterable[dict[str, Any]], *, redacted: bool) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            output = (
                redact(record)
                if redacted
                else {key: value for key, value in record.items() if key != "_export_line"}
            )
            handle.write(json.dumps(output, ensure_ascii=False, separators=(",", ":")) + "\n")


def write_timeline(path: Path, events: list[dict[str, Any]]) -> None:
    columns = ["line", "time", "type", "event_type", "tool", "trace_id", "turn_step"]
    lines = ["\t".join(columns)]
    for record in events:
        event = event_payload(record)
        values = [
            str(record.get("_export_line", "")),
            iso_time(event_time(record)),
            str(record.get("type", "")),
            str(event.get("type", "")),
            str(event.get("name", "")),
            event_trace_id(record),
            str(record.get("turnStep", event.get("step", ""))),
        ]
        lines.append("\t".join(value.replace("\t", " ").replace("\n", " ") for value in values))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def datasource_trace_ids(events: list[dict[str, Any]]) -> set[str]:
    trace_ids: set[str] = set()
    for record in events:
        event = event_payload(record)
        if DATASOURCE_TOOL_MARKER in str(event.get("name", "")):
            trace_id = event_trace_id(record)
            if trace_id:
                trace_ids.add(trace_id)
    return trace_ids


def write_datasource_traces(root: Path, events: list[dict[str, Any]]) -> dict[str, int]:
    trace_dir = root / "datasource-traces"
    counts: dict[str, int] = {}
    for trace_id in sorted(datasource_trace_ids(events)):
        records = [record for record in events if event_trace_id(record) == trace_id]
        if not records:
            continue
        trace_dir.mkdir(mode=0o700, exist_ok=True)
        write_jsonl(trace_dir / f"{trace_id}.jsonl", records, redacted=True)
        counts[trace_id] = len(records)
    return counts


def nested_file_paths(value: Any) -> Iterable[Path]:
    if isinstance(value, dict):
        for key, child in value.items():
            if normalized_key(key) in {"filepath", "outputpath"} and isinstance(child, str):
                yield Path(child).expanduser()
            yield from nested_file_paths(child)
    elif isinstance(value, list):
        for child in value:
            yield from nested_file_paths(child)


def referenced_artifacts(events: list[dict[str, Any]]) -> set[Path]:
    paths: set[Path] = set()
    for record in events:
        event = event_payload(record)
        if event.get("type") == "tool.call":
            paths.update(nested_file_paths(event.get("args")))
        if event.get("type") == "tool.result":
            result = event.get("result")
            if isinstance(result, dict):
                output = result.get("output")
                if isinstance(output, str):
                    for match in PATH_PATTERN.finditer(output):
                        paths.add(Path(match.group("path")).expanduser())
    return paths


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def copy_artifacts(
    destination: Path,
    events: list[dict[str, Any]],
    allowed_roots: Iterable[Path],
    max_bytes: int,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    artifact_dir = destination / "artifacts"
    copied: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    roots = [root.expanduser().resolve() for root in allowed_roots if root.exists()]

    for source in sorted(referenced_artifacts(events), key=str):
        try:
            if source.is_symlink():
                skipped.append({"path": str(source), "reason": "symbolic link"})
                continue
            resolved = source.resolve(strict=True)
            stat = resolved.stat()
        except OSError as exc:
            skipped.append({"path": str(source), "reason": f"not available: {exc}"})
            continue
        if not resolved.is_file():
            skipped.append({"path": str(source), "reason": "not a regular file"})
            continue
        if not any(is_within(resolved, root) for root in roots):
            skipped.append({"path": str(source), "reason": "outside allowed roots"})
            continue
        if stat.st_size > max_bytes:
            skipped.append({"path": str(source), "reason": f"larger than {max_bytes} bytes"})
            continue

        artifact_dir.mkdir(mode=0o700, exist_ok=True)
        target = artifact_dir / resolved.name
        suffix = 2
        while target.exists():
            target = artifact_dir / f"{resolved.stem}-{suffix}{resolved.suffix}"
            suffix += 1
        shutil.copy2(resolved, target)
        copied.append(
            {
                "source": str(resolved),
                "saved_as": str(target.relative_to(destination)),
                "bytes": stat.st_size,
            }
        )
    return copied, skipped


def usage_summary(events: list[dict[str, Any]]) -> dict[str, int]:
    total: Counter[str] = Counter()
    for record in events:
        if record.get("type") != "usage.record":
            continue
        usage = record.get("usage")
        if not isinstance(usage, dict):
            continue
        for key, value in usage.items():
            if isinstance(value, int):
                total[key] += value
    return dict(total)


def event_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for record in events:
        event_type = event_payload(record).get("type")
        key = str(record.get("type", "unknown"))
        if event_type:
            key += f":{event_type}"
        counts[key] += 1
    return dict(sorted(counts.items()))


def session_is_complete(events: list[dict[str, Any]]) -> bool:
    step_begin = 0
    step_end = 0
    for record in events:
        if record.get("type") != "context.append_loop_event":
            continue
        event_type = event_payload(record).get("type")
        if event_type == "step.begin":
            step_begin += 1
        elif event_type == "step.end":
            step_end += 1
    return step_begin > 0 and step_begin == step_end


def unique_destination(output_root: Path, session: Session) -> Path:
    timestamp = datetime.fromtimestamp(session.modified_ns / 1_000_000_000).astimezone().strftime("%Y%m%d-%H%M%S")
    base = output_root / f"{timestamp}-{session.session_id}"
    candidate = base
    suffix = 2
    while candidate.exists():
        candidate = Path(f"{base}-{suffix}")
        suffix += 1
    return candidate


def export_session(
    session: Session,
    output_root: Path,
    *,
    launch_cwd: Path,
    include_raw: bool,
    create_archive: bool,
    max_artifact_bytes: int,
    kimi_exit_code: int | None = None,
) -> tuple[Path, Path | None]:
    events, parse_errors = load_jsonl(session.wire_path)
    if not events:
        raise RuntimeError(f"会话 trace 为空或无法解析：{session.wire_path}")

    output_root.mkdir(parents=True, exist_ok=True)
    destination = unique_destination(output_root, session)
    destination.mkdir(mode=0o700)

    write_jsonl(destination / "wire.jsonl", events, redacted=True)
    if include_raw:
        shutil.copy2(session.wire_path, destination / "raw-wire.jsonl")
    write_timeline(destination / "timeline.tsv", events)

    transcript, final_result = extract_transcript(events)
    (destination / "transcript.md").write_text(transcript, encoding="utf-8")
    (destination / "result.md").write_text(final_result, encoding="utf-8")

    state_path = session.path / "state.json"
    state: dict[str, Any] = {}
    if state_path.is_file():
        try:
            loaded_state = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(loaded_state, dict):
                state = loaded_state
                (destination / "state.json").write_text(
                    json.dumps(redact(loaded_state), ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
        except (OSError, json.JSONDecodeError) as exc:
            parse_errors.append(f"state.json: {exc}")

    log_path = session.path / "logs" / "kimi-code.log"
    if log_path.is_file():
        shutil.copy2(log_path, destination / "kimi-code.log")

    trace_counts = write_datasource_traces(destination, events)
    allowed_roots = {Path("/tmp"), launch_cwd.resolve()}
    work_dir = state.get("workDir")
    if isinstance(work_dir, str):
        allowed_roots.add(Path(work_dir).expanduser().resolve())
    copied, skipped = copy_artifacts(destination, events, allowed_roots, max_artifact_bytes)

    manifest = {
        "exportedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "sessionId": session.session_id,
        "sourceSession": str(session.path),
        "sourceWire": str(session.wire_path),
        "kimiExitCode": kimi_exit_code,
        "eventCount": len(events),
        "complete": session_is_complete(events),
        "eventCounts": event_counts(events),
        "usage": usage_summary(events),
        "datasourceTraces": trace_counts,
        "artifacts": copied,
        "skippedArtifacts": skipped,
        "parseErrors": parse_errors,
        "redaction": "wire.jsonl 会脱敏常见凭据字段；使用 --include-raw 才会额外保存原始 trace",
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    readme = (
        "# Kimi Run Export\n\n"
        f"- Session: `{session.session_id}`\n"
        f"- Events: {len(events)}\n"
        f"- Complete: {session_is_complete(events)}\n"
        f"- Datasource traces: {len(trace_counts)}\n"
        f"- Copied artifacts: {len(copied)}\n"
        f"- Parse errors: {len(parse_errors)}\n\n"
        "## Files\n\n"
        "- `result.md`: 最终回答\n"
        "- `transcript.md`: 用户输入和 Kimi 文本回复\n"
        "- `wire.jsonl`: 脱敏后的完整事件流\n"
        "- `timeline.tsv`: 紧凑时间线\n"
        "- `manifest.json`: 统计、用量和输出文件清单\n"
        "- `datasource-traces/`: 按 trace ID 整理的 Datasource 调用链\n"
        "- `artifacts/`: 插件生成且仍存在的结果文件\n"
        "- `raw-wire.jsonl`: 仅在使用 `--include-raw` 时生成\n"
    )
    (destination / "README.md").write_text(readme, encoding="utf-8")

    archive_path: Path | None = None
    if create_archive:
        archive_path = Path(f"{destination}.tar.gz")
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(destination, arcname=destination.name)
        archive_path.chmod(0o600)
    return destination, archive_path


def run_kimi(kimi_args: list[str]) -> tuple[int, int]:
    started_ns = time.time_ns()
    try:
        completed = subprocess.run(["kimi", *kimi_args], check=False)
    except FileNotFoundError:
        raise RuntimeError("找不到 kimi 命令，请先执行 source ~/.bashrc 或检查 PATH") from None
    return completed.returncode, started_ns


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    kimi_home = resolve_kimi_home(args.kimi_home)
    launch_cwd = Path.cwd().resolve()
    output_root = (args.output_dir or launch_cwd / "data" / "reports" / "kimi-runs").expanduser().resolve()

    kimi_exit_code: int | None = None
    modified_since_ns: int | None = None
    try:
        if not args.latest and not args.session:
            kimi_exit_code, modified_since_ns = run_kimi(args.kimi_args)
        session = select_session(kimi_home, modified_since_ns, args.session)
        destination, archive_path = export_session(
            session,
            output_root,
            launch_cwd=launch_cwd,
            include_raw=args.include_raw,
            create_archive=not args.no_archive,
            max_artifact_bytes=args.max_artifact_mb * 1024 * 1024,
            kimi_exit_code=kimi_exit_code,
        )
    except (OSError, RuntimeError, tarfile.TarError) as exc:
        print(f"导出失败：{exc}", file=sys.stderr)
        return kimi_exit_code if kimi_exit_code not in (None, 0) else 1

    print(f"Kimi 会话已整理到：{destination}")
    if archive_path:
        print(f"压缩包：{archive_path}")
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    if not manifest.get("complete"):
        print("注意：该会话的 step.begin/step.end 未闭合，导出的可能是中间状态。", file=sys.stderr)
    return kimi_exit_code or 0


if __name__ == "__main__":
    raise SystemExit(main())
