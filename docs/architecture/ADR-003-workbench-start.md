# ADR-003：Workbench 拥有 Execution 启动顺序

## 状态

P2 已采用，后续阶段继续验证。

## 决策

- 新主路径由 `WorkbenchExecutionService` 按 `Requirement → Task → Execution → Runtime.start()` 执行。
- 必须先持久化 queued Execution，再发现并启动 Runtime；Runtime 失败也留下可诊断的 failed Execution。
- Runtime 返回的 runtime/run/execution/session/turn 身份必须与已创建 Execution 一致，否则 fail closed。
- 同一活动 `creation_key` 重试复用原 Execution，不重复启动 Runtime。
- Workbench 正常关闭活 Runtime 时把 Execution 转为 `waiting` 并保留完整 SessionRef；清理未确认时保留内存引用供重试，不能留下伪 `running`。
- `ai-dev-os project add` 是新项目的独立 Workbench 注册入口，只写项目元数据、Requirement 存储、Intent 和忽略规则，不写 `AGENTS.md`、`.codex/hooks.json`，也不启动 Dispatcher。
- 旧 `ai-dev-os init` 暂时保留为 V1 compatibility；P10 再完成降级/移除策略。

## Demo 边界

`ai-dev-os workbench demo REQ-ID` 使用确定性 `workbench-demo` Runtime，经过正式 Workbench 启动顺序并持久化 Execution/Event，但明确记录 `authoritative_runtime: false`，不接管任何原生 Agent。
