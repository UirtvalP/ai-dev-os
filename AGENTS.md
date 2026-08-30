# 项目指南

- 面向用户的沟通、项目文档、模板、帮助信息、报错、注释和文档字符串默认使用简体中文。
  代码标识符、CLI 命令、协议字段以及翻译后会影响清晰度或兼容性的既有技术术语保留原文，
  必要时补充中文解释。
- V1 保持本地优先（local-first），使用 Python 3.11+。
- 将 `V1架构.md` 作为 V1 范围与验收标准的依据。
- 必须阅读用户级 `~/.ai-dev-os/USER_PRINCIPLES.md`、项目级 `PROJECT_INTENT.md` 和当前需求的 `intent.md`，
  并将其视为强制意图约束，而不是可选背景资料。
- 默认采用能够安全完成任务的最轻工作流。
- 在外部集成之前，优先完成恢复（Restore）、检查点（Checkpoint）、交接（Handoff）和上下文快照（Context Snapshot）。
- 核心层不得依赖 dashi-taskboard、Codex、Multica、Obsidian 或 Git 的具体实现。
- 保留现有用户文件以及人类可读的 Markdown/JSON 状态。
- V1 不得添加数据库、Web UI、多 Agent 调度器或自动知识写入。
- 技术上正确但违反已记录意图的修改不算完成。
- 仓库级 Codex Hook 会在 `SessionStart` / `UserPromptSubmit` 自动触发 bootstrap；Agent 不得重复手工执行其内部的 Session、Requirement、Task、dashi 或 Git 步骤。Hook 未启用或未受信任时，Skill 仅回退触发一次
  `.\.venv\Scripts\workspace.exe bootstrap --request "<当前开发请求>"`。将返回的 Context Snapshot 视为事实来源。明确说“新增/新建/创建需求”时自动创建；否则多个活动需求且当前 Thread 尚未绑定时不得静默选择，多个 `in_progress` Task 时不得静默绑定。
- 语义工作完成后只触发一次 `workspace finalize REQ-ID`；已知验证、checkpoint、Task review、handoff 和 Git changed files 由 Automation Runtime 连续执行。默认验证通过后自动完成 Requirement；只有明确要求人工测试时进入人工审查。
- dashi 中未绑定的普通开发 Task 被用户移到 `in_progress` 后，由本地 Dispatcher 自动启动或恢复 Codex；Agent 不得重复认领或再次启动执行。Requirement Review 卡不进入该路径。
- `.ai-dev-os.json` 默认开启已推送 Thread 自动收尾。finalize 保留待推送记录；只有当前 Thread 启动后产生新提交、工作树干净且 HEAD 与上游一致时，Runtime 才完成关联开发 Task 并归档 Thread。将 `automation.auto_finish_pushed_thread` 设为 `false` 可关闭。

<!-- ai-dev-os:start -->
## AI Dev OS

- 仓库级 Codex Hook 启用时，由 `SessionStart` / `UserPromptSubmit` 自动触发 bootstrap；
  Agent 不得重复执行内部的 Session、Requirement、Task、dashi 或 Git 步骤。
- Hook 未启用或未受信任时，只回退执行一次
  `workspace bootstrap --request "<当前开发请求>"`。请求包含明确的 `REQ-<数字>` 或 Task ID
  时，将它传给 bootstrap。
- 将 Hook 或回退命令返回的 Context Snapshot 视为当前需求、状态、交接和下一步行动的事实来源。
- 必须阅读 `USER_PRINCIPLES.md`、`PROJECT_INTENT.md` 和当前需求的 `intent.md`。
- 多个活动 Requirement 或多个 `in_progress` Task 存在歧义时，不得静默选择。
- 用户明确说“新增需求”“新建需求”或“创建需求”时，由 Runtime 幂等自动创建并接入，不再二次确认；普通修改仍不得自动创建 Requirement。
- 语义工作完成后只触发一次 `workspace finalize REQ-ID`；验证、checkpoint、Task review、
  handoff、Git changed files 和 Session detach 由 Automation Runtime 连续执行。
- 默认在严谨验证与意图门禁通过后自动完成 Requirement；只有需求明确要求人工测试或人工验收时才使用 dashi 专用 Review 卡或 `workspace confirm REQ-ID --user-confirmed`。
- dashi 中未绑定的普通开发 Task 被用户移到 `in_progress` 后，由本地 Dispatcher 自动启动或
  恢复 Codex；Agent 不得重复认领或再次启动执行。Requirement Review 卡不进入该路径。
- `.ai-dev-os.json` 默认开启已推送 Thread 自动收尾。finalize 后保留待推送归档记录；当前 Thread 启动后产生新提交、工作树干净且 HEAD 与上游一致时，Runtime 完成关联开发 Task 并归档 Thread。将 `automation.auto_finish_pushed_thread` 设为 `false` 可关闭。
<!-- ai-dev-os:end -->
