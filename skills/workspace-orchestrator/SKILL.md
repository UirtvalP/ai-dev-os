---
name: workspace-orchestrator
description: 跨越可替换的 Codex 会话恢复并推进持久化 AI 开发需求工作区。创建、继续、设置检查点、交接、审查或查看指定需求状态时使用；没有持久工作区的一次性任务不要使用。
---

# 工作区编排器

将工作区视为事实来源，将当前 Thread（线程）视为可替换对象。
面向用户的回复和新编写的项目文档默认使用简体中文。代码标识符、CLI 命令、协议字段以及翻译后会降低清晰度或兼容性的既有技术术语保留原文。

## 恢复

1. 优先使用仓库 `.codex/hooks.json`：`SessionStart` / `UserPromptSubmit` 会在 Agent 推理前自动执行 bootstrap，并把 Context Snapshot 注入 developer context。不要重复执行 Session、Requirement、Task、dashi 或 Git 子步骤。
2. 如果 Hook 未启用、未受信任或当前 Codex surface 不支持 Hook，Skill 只负责触发一次 `workspace bootstrap --request "用户当前开发请求"`。用户请求中含准确 `REQ-<数字>` 或 Task ID 时原样传入，不得推导或改写。
3. 将生成的 Context Snapshot 视为需求、状态、交接、计划、决策、验证、相关意图、Dashi Task、Git 上下文和下一步行动的事实来源。`workspace current` 和 `workspace resume REQ-ID` 仅作为诊断入口。
4. 用户明确以“新增需求”“新建需求”或“创建需求”发令时，直接让 Runtime 幂等创建并接入新 Requirement，不再二次确认。普通修改不得自动创建；只有未明确新增且多个活动 Requirement 或多个 `in_progress` Task 返回 `ambiguity` 时才请求用户选择。

项目配置启用 `auto_execute_in_progress` 时，dashi 中由用户移到 `in_progress` 的未绑定开发 Task
会由本地 Dispatcher 自动启动或恢复 Codex。新 Session 的 Hook Snapshot 仍是事实来源；Agent 不得
再次启动 Dispatcher 或重复认领 Task。Requirement Review 卡不属于自动执行目标。

交互式 Main Thread 是轻量控制面。tiny 工作可直接执行；normal、complex、research 的实际代码阅读、
实现与长验证优先通过 `workspace delegate REQ-ID --title ... --description ...` 持久化后立即返回，
不得同步等待 `CodexExecProvider`。Worker 运行时 Main 继续接收消息，可用 `workspace worker-status`
查询结构化状态，或用 `workspace cancel TASK-ID` 取消尚未启动的 queued Task；V1 不支持即时中断运行中的
Worker。补充约束先写入 Task/Workspace，必要时在 Worker 启动前取消并
依据新 Snapshot 恢复；不得复制 Main 完整 conversation、实时注入私有协议、递归创建 Worker或并行启动
第二个 write Worker。

兼容回退命令保持为 `workspace bootstrap REQ-ID --request "用户当前开发请求"` 或 `workspace bootstrap --request "用户当前开发请求"`；多个活动 Task 时不得猜测。

## 执行

- 遵循快照选择的工作流：明确的局部修改使用 `tiny`（微型），默认使用 `normal`（常规），有证据表明跨模块或高风险时使用 `complex`（复杂），调查类工作使用 `research`（研究）。
- 微型工作优先直接执行，不要仅为满足流程形式而创建计划。
- 保持需求范围。请求发生变化时应记录变化，不要静默改写历史。
- 验收标准是必要条件，但不是充分条件；全过程都必须保持意图一致。
- 技术上正确但违反用户原则、项目意图或需求意图的结果仍属于失败。
- 复杂或高风险实现完成后，优先由独立子 Agent 审阅代码，再运行已知验证；这只是审阅工作流，不得在产品中新增多 Agent 调度器。
- 执行下一步行动并验证结果；外部工具必须置于已配置的 Adapter（适配器）之后。

## 持久化

完成一个有意义的中间阶段时，可保存发生变化的语义事实：

```text
workspace checkpoint REQ-ID --phase PHASE \
  --completed "已完成事项" \
  --next-action "下一步行动" \
  --verification "状态：PASS - 验证命令或证据"
```

语义工作完成后只触发一次自动收尾：

```text
workspace finalize REQ-ID \
  --completed "已完成事项" \
  --current-state "当前状态" \
  --important-context "重要上下文" \
  --next-action "下一步行动"
```

Automation Runtime 会自动运行已知验证命令、写 checkpoint、把 Task 推进到可审查状态、执行 Requirement review 门禁、收集 Git changed files 并生成 handoff。默认在全部门禁通过后自动完成 Requirement；只有需求明确记录人工测试或人工验收时才发布 Review Packet 并进入 `in_review`。验证、Task 同步或 review gate 失败时，按 CLI 返回的具体 blockers 继续修复。`SessionEnd` Hook 负责异常退出时的幂等 detach。项目配置 `automation.auto_finish_pushed_thread` 开启时，finalize 会保留待推送归档记录；`Stop` Hook 在该 Thread 的新提交完整推送后幂等完成关联 Task、归档 Thread并清除待处理状态。

`workspace handoff REQ-ID` 保留为显式中途交接兼容命令；完整实现结束优先使用 `finalize`。

运行 `workspace review REQ-ID` 前，对照用户原则、项目意图、需求意图和不必要的复杂度，更新 `intent.md` 中的四项检查。每项必须为 `PASS`、`PARTIAL` 或 `VIOLATION`；只有全部为 `PASS` 才能进入审查。之后还需确认验收标准、验证结果以及已配置的任务状态均可审查。

只有明确要求人工测试或人工验收、且 Requirement 已处于 `in_review` 时，才使用 `workspace confirm REQ-ID --user-confirmed` 或 dashi Review 卡记录批准。用户明确要求修改时执行 `workspace request-changes`。默认自动完成路径不伪造人工批准，也不创建多余 Review 卡。

默认 dashi 项目会在 `ai-dev-os init` 时写入 `.ai-dev-os.json`。每个活动 Requirement 必须幂等保持至少一张主面板可见工作卡；Provider 离线时保留本地事实并在后续 Hook 补偿，不能因并发重试重复建卡。人工测试路径才创建或复用带 `requirement-review` 标签的专用 Review 卡，并继续核对 marker、revision、fingerprint 与可靠用户活动事实。

核心项目规则和 V1 范围记录在 `V1架构.md` 中。持久化状态由工作区系统负责；本 Skill 只负责遵循并更新这些状态。
