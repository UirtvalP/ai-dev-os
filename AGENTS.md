# 项目指南

- 面向用户的沟通、项目文档、模板、帮助信息、报错、注释和文档字符串默认使用简体中文。
  代码标识符、CLI 命令、协议字段以及翻译后会影响清晰度或兼容性的既有技术术语保留原文，
  必要时补充中文解释。
- V1 保持本地优先（local-first），使用 Python 3.11+。
- 将 `V1架构.md` 作为 V1 范围与验收标准的依据。
- 必须阅读 `USER_PRINCIPLES.md`、`PROJECT_INTENT.md` 和当前需求的 `intent.md`，
  并将其视为强制意图约束，而不是可选背景资料。
- 默认采用能够安全完成任务的最轻工作流。
- 在外部集成之前，优先完成恢复（Restore）、检查点（Checkpoint）、交接（Handoff）和上下文快照（Context Snapshot）。
- 核心层不得依赖 dashi-taskboard、Codex、Multica、Obsidian 或 Git 的具体实现。
- 保留现有用户文件以及人类可读的 Markdown/JSON 状态。
- V1 不得添加数据库、Web UI、多 Agent 调度器或自动知识写入。
- 技术上正确但违反已记录意图的修改不算完成。
- 仓库级 Codex Hook 会在 `SessionStart` / `UserPromptSubmit` 自动触发 bootstrap；Agent 不得重复手工执行其内部的 Session、Requirement、Task、dashi 或 Git 步骤。Hook 未启用或未受信任时，Skill 仅回退触发一次
  `.\.venv\Scripts\workspace.exe bootstrap --request "<当前开发请求>"`。将返回的 Context Snapshot 视为事实来源。多个活动需求且当前 Thread 尚未绑定时不得静默选择；多个 `in_progress` Task 时不得静默绑定。
- 语义工作完成后只触发一次 `workspace finalize REQ-ID`；已知验证、checkpoint、Task `in_review`、handoff、Git changed files 和 Session detach 由 Automation Runtime 连续执行。
- `.ai-dev-os.json` 默认开启已推送 Thread 自动收尾。只有当前 Thread 启动后产生新提交、工作树干净且 HEAD 与上游一致时，Runtime 才自动完成关联开发 Task 并归档 Thread；Requirement 不会自动完成。将 `automation.auto_finish_pushed_thread` 设为 `false` 可关闭。
