# ADR-008：Workbench 只继续 Execution 的原 Session

## 决策

Workbench 以 Execution 中持久化的完整 `RuntimeSessionRef` 为唯一回复目标。服务进程首次投递时调用统一 Runtime Contract 的 `resume`，连接建立后调用 `send_message`；该路径永不调用 `start`。

每次回复必须携带 `command_id`。Workbench 在调用 Provider 前持久化 claim，Provider 接收后先写 delivery receipt，再更新 Execution；同一 ID 的并发或重试只读取已有结果。投递结果未知时保持 fail closed，不自动重发。

回复前必须校验 Requirement、Task、Execution、Runtime、Session、工作目录、sandbox、模型与 reasoning 身份。Runtime 必须同时声明 `resume` 与 `interactive_message` 能力；能力缺失、Runtime 不可用或返回身份漂移时 fail closed，并提示用户在原生 Agent 中打开原 Session。

Provider 原始事件继续写入现有 `RuntimeEventStore`。Requirement Space 只展示脱敏投影，不改写原始 payload。

## 结果

- Workbench 消息不会静默创建第二个 Agent 或 Session。
- Native Agent 的原生入口不受影响。
- 重启 Workbench 后仍可从持久化 SessionRef 恢复同一 Session。
- 不支持恢复的 Provider 有显式能力状态和人工替代路径。
