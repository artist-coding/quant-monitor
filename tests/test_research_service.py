"""Kimi Swarm 股票调研服务测试。"""

import json

import pytest

from api.services.research_service import (
    KimiResearchService,
    SwarmRunResult,
    _parse_stream_final,
    build_research_prompt,
    classify_research_target,
    normalize_ts_code,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("600519", "600519.SH"),
        ("000001.sz", "000001.SZ"),
        ("300750", "300750.SZ"),
        ("832000", "832000.BJ"),
        ("SH600519", "600519.SH"),
        ("600519 贵州茅台", "600519.SH"),
        ("６００５１９", "600519.SH"),
        ("贵州茅台", "贵州茅台"),
        ("  宁德时代  ", "宁德时代"),
    ],
)
def test_normalize_ts_code(raw, expected):
    assert normalize_ts_code(raw) == expected


@pytest.mark.parametrize("raw", ["", "600519;000001", "ABC", "600519.SZ", "000001.SH"])
def test_normalize_ts_code_rejects_invalid_input(raw):
    with pytest.raises(ValueError):
        normalize_ts_code(raw)


def test_prompt_enables_swarm_and_skill():
    prompt = build_research_prompt("600519.SH")
    assert prompt.startswith("/swarm")
    assert "zettaranc-perspective" in prompt
    assert "/skill:zettaranc-perspective" in prompt
    assert "禁止修改" in prompt
    assert "不构成投资建议" in prompt


def test_theme_research_uses_comparison_workflow():
    target = normalize_ts_code("分析一下A股的磷化铟公司如何")
    assert classify_research_target(target) == "theme"
    prompt = build_research_prompt(target)
    assert "自然语言研究主题" in prompt
    assert "A 股相关公司" in prompt
    assert "横向比较" in prompt


def test_research_task_runs_and_persists_report(tmp_path, monkeypatch):
    service = KimiResearchService(data_dir=tmp_path)
    monkeypatch.setattr(
        service,
        "_run_swarm",
        lambda task, timeout, idle_timeout: SwarmRunResult(["基本面证据", "技术面证据"], 2),
    )
    monkeypatch.setattr(service, "_synthesize", lambda task, evidence, timeout: "# 调研报告\n\n结论内容")
    created = service.create("600519", enqueue=False)

    trace_dir = tmp_path / "traces" / created["task_id"]
    assert (trace_dir / "manifest.json").exists()
    assert (trace_dir / "workflow.jsonl").exists()

    service.run_task(created["task_id"])
    completed = service.get(created["task_id"])

    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["progress"] == 100
    assert "调研报告" in completed["report"]
    assert completed["trace_available"] is True
    assert completed["agent_count"] == 2
    assert (trace_dir / "agent_reports.json").exists()
    assert (trace_dir / "report.md").exists()
    assert '"status": "completed"' in (trace_dir / "manifest.json").read_text(encoding="utf-8")
    command = service._base_command("stream-json")
    assert "--skills-dir" in command
    skills_dir = command[command.index("--skills-dir") + 1]
    assert skills_dir.endswith("/.kimi-code/skills")


def test_partial_swarm_result_is_synthesized_instead_of_failed(tmp_path, monkeypatch):
    service = KimiResearchService(data_dir=tmp_path)
    partial = SwarmRunResult(
        ["基本面证据", "公告证据", "技术面证据", "风险证据"],
        expected_agent_count=5,
        partial=True,
        partial_reason="连续 600 秒没有新的工作流活动",
    )
    monkeypatch.setattr(service, "_run_swarm", lambda task, timeout, idle_timeout: partial)
    monkeypatch.setattr(service, "_synthesize", lambda task, evidence, timeout: "# 降级汇总报告")
    created = service.create("磷化铟产业链", enqueue=False)

    service.run_task(created["task_id"])
    completed = service.get(created["task_id"])

    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["partial_result"] is True
    assert completed["agent_count"] == 4
    assert completed["expected_agent_count"] == 5
    assert "部分研究单元超时" in completed["message"]
    assert "降级汇总报告" in completed["report"]


def test_failed_task_can_recover_saved_agent_reports(tmp_path, monkeypatch):
    service = KimiResearchService(data_dir=tmp_path)
    created = service.create("磷化铟产业链", enqueue=False)
    created.update(status="failed", error="旧的固定超时", completed_at=created["created_at"])
    service._write(created)
    session_dir = tmp_path / "saved-session"
    for index in range(2):
        wire_path = session_dir / "agents" / f"agent-{index}" / "wire.jsonl"
        wire_path.parent.mkdir(parents=True, exist_ok=True)
        events = [
            {
                "type": "context.append_loop_event",
                "event": {
                    "type": "content.part",
                    "stepUuid": f"step-{index}",
                    "part": {"type": "text", "text": f"子报告 {index}"},
                },
            },
            {
                "type": "context.append_loop_event",
                "event": {"type": "step.end", "uuid": f"step-{index}", "finishReason": "end_turn"},
            },
        ]
        wire_path.write_text("\n".join(json.dumps(item) for item in events), encoding="utf-8")
    monkeypatch.setattr(service, "_synthesize", lambda task, evidence, timeout: "# 恢复后的报告")

    recovered = service.recover_task(created["task_id"], session_dir=session_dir)

    assert recovered["status"] == "completed"
    assert recovered["partial_result"] is True
    assert recovered["agent_count"] == 2
    assert recovered["error"] == ""
    assert (tmp_path / "traces" / created["task_id"] / "agent_reports.json").exists()


def test_parse_stream_final_uses_last_plain_assistant_message():
    output = "\n".join(
        [
            '{"role":"assistant","content":"处理中","tool_calls":[{"type":"function"}]}',
            '{"role":"tool","content":"ok"}',
            '{"role":"assistant","content":"最终报告"}',
        ]
    )
    assert _parse_stream_final(output) == "最终报告"


def test_duplicate_active_task_is_reused(tmp_path):
    service = KimiResearchService(data_dir=tmp_path)
    first = service.create("600519", enqueue=False)
    second = service.create("600519.SH", enqueue=False)
    assert second["task_id"] == first["task_id"]
