# ADR-007：Workbench Requirement Space 复用现有 Dashboard

## 决策

现有本机 Dashboard 升级为 Workbench 的 Requirement Space，不创建第二套 Web UI 或状态库。
`DashboardService` 只读投影 Workspace、Main Agent、ExecutionStore、RuntimeEventStore、
Verification Receipt 与 Git 事实。

主页面展示 Intent、Acceptance、Progress、Main Agent、Active Execution、Task Graph 和交付证据。
Execution Details 展示 Provider、Runtime、Model、Reasoning、Task、Status、Duration，并把 Conversation、
Tool calls、Commands、Files、Diff、Tests、Errors 和 Events 分区呈现。详情通过独立分页接口按需读取，
分类索引可随时重建，不替代 append-only Event Store。Event Store 保留完整 Provider 原始 payload；
HTTP 安全视图会脱敏并明确标记为 redacted。

## 边界

- Dashboard 不修改 Requirement、Execution、Git 或验证状态。
- Task Graph 只读取 Supervisor 的真实 Task 状态；Execution 关系在独立 Execution Graph 展示。
- 每页 Event 在投影前校验 Requirement/Task/Execution/Runtime/Session 身份，不一致时失败关闭。
- P6 对 Workbench 创建的 Execution 只读展示；直接回复原 Session 在 P7 实现。
- Legacy Session 继续显示，但与一等 Execution 分开；有相同 Session 的记录只显示 Execution，避免重复。
- Main Agent 以 Workspace、Execution 与 Supervisor 全部稳定事实的 fingerprint 检测 stale。
