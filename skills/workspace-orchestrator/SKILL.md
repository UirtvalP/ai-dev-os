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
4. 多个活动 Requirement 或多个 `in_progress` Task 返回 `ambiguity` 时，只有这一步请求用户选择。Requirement 不得因小修改自动创建。

兼容回退命令保持为 `workspace bootstrap REQ-ID --request "用户当前开发请求"` 或 `workspace bootstrap --request "用户当前开发请求"`；多个活动 Task 时不得猜测。

## 执行

- 遵循快照选择的工作流：明确的局部修改使用 `tiny`（微型），默认使用 `normal`（常规），有证据表明跨模块或高风险时使用 `complex`（复杂），调查类工作使用 `research`（研究）。
- 微型工作优先直接执行，不要仅为满足流程形式而创建计划。
- 保持需求范围。请求发生变化时应记录变化，不要静默改写历史。
- 验收标准是必要条件，但不是充分条件；全过程都必须保持意图一致。
- 技术上正确但违反用户原则、项目意图或需求意图的结果仍属于失败。
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

Automation Runtime 会自动运行已知验证命令、写 checkpoint、把 Task 推进到 `in_review`、执行 Requirement review 门禁、收集 Git changed files、生成 handoff 并 detach Session；不得把这些确定性步骤拆成 Agent 手工流程。`SessionEnd` Hook 负责异常退出或未 finalize 时的幂等 detach。

`workspace handoff REQ-ID` 保留为显式中途交接兼容命令；完整实现结束优先使用 `finalize`。

运行 `workspace review REQ-ID` 前，对照用户原则、项目意图、需求意图和不必要的复杂度，更新 `intent.md` 中的四项检查。每项必须为 `PASS`、`PARTIAL` 或 `VIOLATION`；只有全部为 `PASS` 才能进入审查。之后还需确认验收标准、验证结果以及已配置的任务状态均可审查。未经用户明确批准，不得将需求或 Dashi Issue（事项）标记为 `done`。

核心项目规则和 V1 范围记录在 `V1架构.md` 中。持久化状态由工作区系统负责；本 Skill 只负责遵循并更新这些状态。
