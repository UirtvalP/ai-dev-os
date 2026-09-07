# P6 Requirement Space 迁移说明

- `dashboard serve` 命令、Bearer token、回环监听与既有 API 路径保持不变。
- `/api/status` 的 `projection` 新增 `requirement_space`、`main_agent`、`executions` 和 `task_graph`；
  旧字段保持兼容。
- 总览不再内嵌 Execution Event；`/api/executions/EXE-ID?after=&limit=` 按需分页返回详情。
- Event Store 保留 Runtime 原始事件；HTTP 返回的是安全脱敏视图并标记 `payload_view=redacted`。
  其他详情数组只是当前事件页的 UI 分类视图。
- Command 仍是 Session scoped，不冒充 Execution/Turn 归属。
- 既有 Session 仍可查看。Workbench Execution 在 P7 前不会显示可发送消息的输入框，避免误导为已支持续聊。
- Requirement 标题已从旧 P0 文案纠正为“独立工作台架构全量迁移 P0–P10”。
