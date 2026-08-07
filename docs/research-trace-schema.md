# Kimi 股票分析 Trace 数据结构

每次股票分析都会在以下目录保存一份独立、可长期复用的 workflow trace：

```text
data/kimi_research/traces/<task_id>/
├── manifest.json
├── workflow.jsonl
├── swarm_prompt.md
├── swarm_stdout.jsonl
├── swarm_stderr.log
├── agent_reports.json
├── synthesis_prompt.md
├── synthesis_stdout.jsonl
├── synthesis_stderr.log
├── report.md
└── sessions/
    ├── swarm/
    │   ├── state.json
    │   ├── logs/kimi-code.log
    │   └── agents/<agent_id>/wire.jsonl
    └── synthesis/
        ├── state.json
        ├── logs/kimi-code.log
        └── agents/main/wire.jsonl
```

## `workflow.jsonl`

这是供后续量化处理优先使用的统一事件流，每行一个 JSON 对象。

| 字段 | 说明 |
|------|------|
| `schema_version` | Trace 数据结构版本，当前为 `1` |
| `task_id` | 分析任务 ID |
| `stage` | `orchestrator`、`swarm` 或 `synthesis` |
| `actor` | `research_service`、`main` 或子 Agent ID |
| `timestamp` / `timestamp_ms` | 事件时间 |
| `event_type` | 阶段事件、模型步骤、工具调用或工具结果类型 |
| `turn_id` / `step` | Kimi 会话轮次和步骤 |
| `finish_reason` | 模型步骤结束原因 |
| `content_type` | `think`、`text` 等内容类型 |
| `content` | 思考或正文内容 |
| `tool_name` | 工具名称 |
| `tool_args` | 工具输入参数 |
| `tool_result` | 工具返回结果 |
| `usage` | 模型用量信息 |

`manifest.json` 还会记录 `agent_count`、`expected_agent_count`、`partial_result`、
`partial_reason` 和 `last_activity_at`。当少数子 Agent 超时但已有至少 2 份完整结果时，
服务会先归档所有原始会话，再以 `partial_result: true` 继续生成最终报告。

## 使用建议

- 量化统计、特征提取和训练集加工优先读取 `workflow.jsonl`、`manifest.json` 和 `agent_reports.json`。
- 需要重新还原 Kimi 执行现场时读取 `sessions/**/wire.jsonl` 原始会话。
- `report.md` 是最终面向用户的分析结论，不能替代原始事实和工具结果。
- `swarm.timeout` 与 `swarm.partial` 表示超时降级路径；这些任务仍可正常完成并保留实际使用的子报告数量。
- Trace 可能包含 Agent 检索到的公开资料原文和工具参数，只应保存在受控服务器环境中，不应直接公开下载。
