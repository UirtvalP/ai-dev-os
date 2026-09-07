# P7 原 Session 回复迁移说明

## 行为变化

- `POST /api/executions/{execution_id}/message` 接收 `message` 与幂等 `command_id`，只向该 Execution 的原生 Session 投递。
- Execution Details 返回 `reply` 能力信息；不可恢复时 UI 隐藏发送框并展示原因与替代路径。
- 首次投递使用 `resume`，同一 Workbench 进程内后续投递使用 `send_message`。
- 回复产生的 Provider 原始事件仍保存在 `runtime-events`，HTTP 只返回脱敏视图。

## 兼容性

旧 `/api/message` 专用远程 Session 路径暂时保留，供 P10 Legacy Hook/入口迁移处理；它不会被 Execution 回复端点调用。

## 回滚

回滚 P7 提交即可移除新端点和 Execution composer。已有 Execution、SessionRef、事件及旧 Dashboard 指令队列均为向后兼容的人类可读数据，无需迁移或删除。
