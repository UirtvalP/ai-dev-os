# ADR-009：Supervisor Watchdog 独立于 Main Agent

## 决策

保留现有 `RequirementSupervisor` 作为 Task 状态机、重试、并发槽位与单写者执行权威；新增独立的确定性 `RequirementWatchdog` 作为只读监测层，不把模型判断引入安全控制面。

Watchdog 消费 Execution 及其原始事件、Task 状态、Acceptance、Git/Test 摘要、Main Agent Action 与时间戳，并输出 `ExecutionStuck`、`ExecutionLooping`、`RepeatedFailure`、`NoRequirementProgress`、`BudgetWarning`、`DuplicateWork` 和汇总的 `MainAgentReviewRequired`。

信号只设置 `review_required`，默认不启动、取消或杀死 Agent。事件身份不匹配时扫描 fail closed。阈值由 `WatchdogPolicy` 注入，规则可测试且不依赖 Provider。

## 结果

- Main Agent 可以观察 Supervisor 信号并执行 Review，但 Supervisor 不负责开发。
- Requirement Space 直接展示当前信号。
- Watchdog 状态保存在 Requirement 内的人类可读 JSON 中，可在重启后继续判断 Acceptance stagnation。
