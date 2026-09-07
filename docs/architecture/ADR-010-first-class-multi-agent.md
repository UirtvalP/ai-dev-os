# ADR-010：Multi-Agent 复用 Supervisor，并以 Execution 作为顶层对象

## 决策

真正的多 Agent 继续使用现有 `RequirementSupervisor` 的 DAG、依赖门禁、并发上限、retry budget、fence、Execution ownership 和可信隔离，不引入 `Main Codex Thread -> subagents` 顶层模型。

`ExecutionTrackedWorkerPort` 作为薄适配层，把每个受控 Worker attempt 在启动前持久化为一等 Execution，并把 Provider 原始事件保真投影到该 Execution 的事件分片。它不替代 Worker ledger 或 Supervisor 状态机。

两个只读 Task 可以共享操作员明确授权的同一工作区；只要任一 Task 可写，重叠目录继续互斥。候选、验证中和结果未知的目录始终独占。写 Task 仍要求独立非 main branch/worktree 与可信 launcher 隔离。

最终合并继续由既有 Integration Gate 授权和执行。`IntegrationExecutionService` 只在所有源 Task 已由 Supervisor accepted 后创建 `role=integration` 的一等 Execution，并严格验证既有 `MergeReceipt`；它不自行授予 merge 权限。

Integration Execution 没有 receipt 的 `starting/running/blocked` 只表示包装层结果未知，不能覆盖 Gate 的 request journal。重试必须携带同一 `request_id` 再进入既有 Gate；`reconcile` 与 `recover-post-merge` 返回的可信 receipt 会回填同一 Execution。已有 receipt 才是该 Execution 的幂等终态。

## 结果

- Requirement 可以并行拥有多个可恢复、可观察的 Execution。
- Worker SessionRef 以 Execution 身份持久化，可直接复用 P7 原 Session 回复。
- raw Provider payload、Task attempt/fence、worktree/branch 和 Integration receipt 均保留。
- 单写者与 merge 权威仍只有现有 Supervisor / Integration Gate。
