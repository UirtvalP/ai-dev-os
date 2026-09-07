# P6 Requirement Space 迁移说明

- 新的正式入口为 `ai-dev-os workbench serve`：无需 REQ-ID，自动打开项目与 Requirement Space 首页。
- 页面可添加现有项目、创建新项目、同时绑定多个项目并无损解除绑定；也支持新建 Requirement、进入详情并开始/继续真实执行。
- 在已接入项目目录启动时会幂等补登记当前项目。
- 默认本机回环入口免 Token，打开 `http://127.0.0.1:8765/` 即进入需求空间。
- 局域网、公网、反向代理或隧道入口必须使用 `--remote-access`；该模式从
  `~/.ai-dev-os/secrets/workbench.token` 加载 Token，浏览器通过 fragment 自动接收并立即清除。
- 正式公网隧道转发到独立的 `127.0.0.1:8767` 认证监听，不复用本机免密的 `127.0.0.1:8765`。
- `dashboard serve` 命令、Bearer token、回环监听与既有 API 路径保持不变。
- `/api/status` 的 `projection` 新增 `requirement_space`、`main_agent`、`executions` 和 `task_graph`；
  旧字段保持兼容。
- 总览不再内嵌 Execution Event；`/api/executions/EXE-ID?after=&limit=` 按需分页返回详情。
- Event Store 保留 Runtime 原始事件；HTTP 返回的是安全脱敏视图并标记 `payload_view=redacted`。
  其他详情数组只是当前事件页的 UI 分类视图。
- Command 仍是 Session scoped，不冒充 Execution/Turn 归属。
- 既有 Session 仍可查看；Workbench 可以从 Requirement Space 创建一等 Execution，并继续其原 Session。
- Requirement 标题已从旧 P0 文案纠正为“独立工作台架构全量迁移 P0–P10”。
- 旧 `scripts/start_remote_dashboard.ps1` 现启动通用 Workbench，不再硬编码 REQ-020；
  `scripts/start_workbench.ps1` 提供开发仓库的一键本机入口。
