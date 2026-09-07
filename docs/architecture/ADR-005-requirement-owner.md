# ADR-005：Main Agent 是 Requirement Owner 领域角色

## 状态

P4 已采用，后续阶段继续扩展 Action 执行能力。

## 决策

- Main Agent 身份由 AI Dev OS 的 `RequirementOwnerState` 持有，不等于 Codex/Claude/Cursor Session。
- Owner 每轮从 Requirement、Intent、Plan、Execution、Verification 与 Decision 的结构化持久事实 Observe，不默认重读完整 conversation。
- 持久字段覆盖 goal/phase、Intent、active/completed/blocked task 与 execution、验收标准及状态、Decision、风险、Git、验证摘要、Supervisor signal、next action、review 状态。
- 决策循环固定为 Observe → Assess → Plan → Act → Inspect → Review → Replan；所有写入同时校验 stage 与 revision，避免跨周期 ABA。Observe 只刷新事实，不会把进行中的阶段重置。
- 已存在的 Supervisor ledger 通过只读 snapshot 恢复 revision/fence/lease/node 信号；不存在时明确为空，不创建旁路事实。
- Action 只允许计划列出的结构化类型；P4 仅持久化候选 Action，不越权执行完成、验证或外部副作用。
- Runtime 将在后续阶段承载可替换推理，但 Runtime/Model/Session 不进入 Owner 身份。
