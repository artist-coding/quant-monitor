---
name: zettaranc-perspective
description: 分析 A 股个股、行业、技术面、战法、风险与公开信息时，加载项目根目录的 zettaranc 完整投资框架。
---

# Zettaranc 股票调研入口

处理股票调研前，必须完整读取并遵循 `${KIMI_SKILL_DIR}/../../../SKILL.md`。

根 Skill 中提到的 `knowledge/`、`modules/`、`rules/` 和其他相对路径，均以 `${KIMI_SKILL_DIR}/../../..` 为项目根目录解析。

本入口只负责让 Kimi Code CLI 注册并激活项目根 Skill，不替代或改写其中的分析规则、安全边界与免责声明。
