# ADR-007：Workbench Requirement Space 复用现有 Dashboard

## 决策

Workbench 以两级 Requirement Space 界面复用现有 Dashboard 投影，不创建第二套状态库：顶层首页从
Global Project Registry 枚举项目和 Requirement；进入需求后继续使用既有 DashboardService 读取完整
Requirement Space。用户不需要预先知道或输入 REQ-ID。
`DashboardService` 只读投影 Workspace、Main Agent、ExecutionStore、RuntimeEventStore、
Verification Receipt 与 Git 事实。

`ai-dev-os workbench serve` 是正式用户入口：自动登记当前已接入项目并打开浏览器。本机回环入口默认免
Token；局域网、公网、反向代理或隧道入口必须显式使用 `--remote-access`，Token 通过 URL fragment
注入后立即从地址栏清除。首页可以直接创建 Requirement；用户明确点击执行后，
Workbench 才在所选项目根内启动 `workspace-write` Runtime，审批仍为拒绝。原 `dashboard serve REQ-ID`
继续作为固定 Requirement 的只读远程兼容入口。

首页的项目管理支持两条显式路径：添加现有目录时无损补齐项目接入文件；创建新项目时要求目标目录
不存在。两者都登记到同一 Global Project Registry，允许一个 Workbench 绑定多个项目。解除绑定只
删除全局索引，不删除项目目录、Requirement、Git 或 Task。

主页面展示 Intent、Acceptance、Progress、Main Agent、Active Execution、Task Graph 和交付证据。
Execution Details 展示 Provider、Runtime、Model、Reasoning、Task、Status、Duration，并把 Conversation、
Tool calls、Commands、Files、Diff、Tests、Errors 和 Events 分区呈现。详情通过独立分页接口按需读取，
分类索引可随时重建，不替代 append-only Event Store。Event Store 保留完整 Provider 原始 payload；
HTTP 安全视图会脱敏并明确标记为 redacted。

## 边界

- 首页只通过 WorkspaceStore 创建 Requirement；详情投影不修改 Requirement、Execution、Git 或验证状态。
- 写 Runtime 仅在用户从具体 Requirement Space 明确提交指令后启动，工作目录固定为已登记项目。
- Task Graph 只读取 Supervisor 的真实 Task 状态；Execution 关系在独立 Execution Graph 展示。
- 每页 Event 在投影前校验 Requirement/Task/Execution/Runtime/Session 身份，不一致时失败关闭。
- P6 对 Workbench 创建的 Execution 只读展示；直接回复原 Session 在 P7 实现。
- Legacy Session 继续显示，但与一等 Execution 分开；有相同 Session 的记录只显示 Execution，避免重复。
- Main Agent 以 Workspace、Execution 与 Supervisor 全部稳定事实的 fingerprint 检测 stale。
