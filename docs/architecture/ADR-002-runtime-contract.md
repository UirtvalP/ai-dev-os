# ADR-002：Workbench 只依赖统一 Runtime Contract

## 状态

P1 已采用，后续阶段继续验证。

## 决策

- Workbench 使用 `AgentRuntime` 门面与 `StandardAgentRuntimePort`，不判断 Codex、Cursor 或 Claude。
- `ExecutionSpec` 是启动输入；`RuntimeSessionRef` 是恢复、消息、取消、状态和事件查询的引用。
- 标准能力词汇包含计划要求的 `start`、`resume`、`interactive_message`、`event_stream`、`cancel`、`model_selection`、`reasoning_selection`、`approval`、`tool_events`、`diff_events`，并补充接口本身需要发现的 `status` 与 `archive`。
- Adapter 可以继续发布旧能力名；`RuntimeDescriptor` 在边界统一为 canonical capabilities，旧调用不失效。
- `resume(session, message)` 必须从 Session 引用恢复原 sandbox、model、reasoning 与 Execution 身份，不能偷偷回落到另一执行策略。
- `list_events`/`stream_events` 使用持久 JSONL 的游标分页；当前 P1 的 `stream_events` 是非阻塞消费页，实时推送交给后续 Console/Event Plane。
- 标准 Runtime 组装必须提供 `RuntimeEventStore`；不能声明事件能力却把“未配置存储”伪装成空事件流。

## 原因

三个 Runtime 已有可工作的协议适配器。迁移目标是建立稳定、可发现、可测试的产品契约，而不是复制实现或把 Provider 条件分支搬进核心层。

## 兼容性

`AgentRunRequest`、`AgentRuntimePort` 以及旧 capability 名继续保留。P1 没有删除 V1 执行入口，也没有改变 Hook 主路径；P2 才把 Workbench 启动接到该契约。
