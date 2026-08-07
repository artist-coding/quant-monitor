#!/usr/bin/env python3
"""Kimi 会话 trace 导出脚本测试。"""

import json
import os
from pathlib import Path

from scripts.kimi_trace import export_session, find_sessions, load_jsonl, select_session, session_is_complete


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records), encoding="utf-8")


def make_session(kimi_home: Path, session_id: str, records: list[dict]) -> Path:
    session = kimi_home / "sessions" / "wd_test" / session_id
    write_jsonl(session / "agents" / "main" / "wire.jsonl", records)
    (session / "state.json").write_text(
        json.dumps({"workDir": str(kimi_home.parent), "lastPrompt": "测试问题"}, ensure_ascii=False),
        encoding="utf-8",
    )
    return session


def test_find_and_select_latest_session(tmp_path: Path) -> None:
    kimi_home = tmp_path / ".kimi-code"
    first = make_session(kimi_home, "session_first", [{"type": "metadata", "time": 1}])
    second = make_session(kimi_home, "session_second", [{"type": "metadata", "time": 2}])
    first_wire = first / "agents" / "main" / "wire.jsonl"
    second_wire = second / "agents" / "main" / "wire.jsonl"
    os.utime(first_wire, ns=(1, 1))
    os.utime(second_wire, ns=(2, 2))

    sessions = find_sessions(kimi_home)

    assert len(sessions) == 2
    assert select_session(kimi_home).session_id == "session_second"
    assert select_session(kimi_home, session_id="session_first").session_id == "session_first"


def test_load_jsonl_keeps_valid_lines_and_reports_invalid(tmp_path: Path) -> None:
    wire = tmp_path / "wire.jsonl"
    wire.write_text('{"type":"metadata"}\nnot-json\n{"type":"usage.record"}\n', encoding="utf-8")

    events, errors = load_jsonl(wire)

    assert [event["type"] for event in events] == ["metadata", "usage.record"]
    assert events[0]["_export_line"] == 1
    assert errors and errors[0].startswith("line 2:")


def test_export_session_creates_result_trace_and_artifacts(tmp_path: Path) -> None:
    kimi_home = tmp_path / ".kimi-code"
    artifact = tmp_path / "result.csv"
    artifact.write_text("value\n42\n", encoding="utf-8")
    trace_id = "trace-123"
    records = [
        {"type": "metadata", "api_key": "secret-key", "time": 1_700_000_000_000},
        {
            "type": "turn.prompt",
            "input": [{"type": "text", "text": "测试问题"}],
            "origin": {"kind": "user"},
            "time": 1_700_000_000_100,
        },
        {
            "type": "context.append_loop_event",
            "event": {"type": "step.begin", "step": 1, "time": 1_700_000_000_150},
        },
        {
            "type": "context.append_loop_event",
            "event": {
                "type": "tool.call",
                "name": "mcp__plugin-kimi-datasource_data__call_data_source_tool",
                "args": {"params": {"file_path": str(artifact)}},
                "traceId": trace_id,
                "time": 1_700_000_000_200,
            },
        },
        {
            "type": "context.append_loop_event",
            "event": {"type": "step.end", "step": 1, "time": 1_700_000_000_450},
        },
        {
            "type": "context.append_loop_event",
            "event": {
                "type": "tool.result",
                "result": {"output": f"saved to {artifact}"},
                "traceId": trace_id,
                "time": 1_700_000_000_300,
            },
        },
        {
            "type": "context.append_loop_event",
            "event": {
                "type": "content.part",
                "step": 2,
                "part": {"type": "text", "text": "最终回答"},
                "traceId": "final-trace",
                "time": 1_700_000_000_400,
            },
        },
        {
            "type": "usage.record",
            "model": "kimi-code/k3",
            "usage": {"inputOther": 10, "output": 5, "inputCacheRead": 20},
            "time": 1_700_000_000_500,
        },
    ]
    session_path = make_session(kimi_home, "session_test", records)
    session = find_sessions(kimi_home)[0]

    destination, archive = export_session(
        session,
        tmp_path / "exports",
        launch_cwd=tmp_path,
        include_raw=False,
        create_archive=True,
        max_artifact_bytes=1024,
        kimi_exit_code=0,
    )

    assert session.path == session_path
    assert archive is not None and archive.is_file()
    assert (destination / "result.md").read_text(encoding="utf-8") == "最终回答\n"
    assert "测试问题" in (destination / "transcript.md").read_text(encoding="utf-8")
    assert (destination / "datasource-traces" / f"{trace_id}.jsonl").is_file()
    assert (destination / "artifacts" / artifact.name).read_text(encoding="utf-8") == "value\n42\n"

    exported_wire = (destination / "wire.jsonl").read_text(encoding="utf-8")
    assert "secret-key" not in exported_wire
    assert "<redacted>" in exported_wire
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["eventCount"] == len(records)
    assert manifest["complete"] is True
    assert manifest["usage"] == {"inputOther": 10, "output": 5, "inputCacheRead": 20}
    assert manifest["datasourceTraces"] == {trace_id: 2}


def test_session_is_complete_requires_balanced_steps() -> None:
    begin = {"type": "context.append_loop_event", "event": {"type": "step.begin"}}
    end = {"type": "context.append_loop_event", "event": {"type": "step.end"}}

    assert session_is_complete([begin, end]) is True
    assert session_is_complete([begin]) is False
