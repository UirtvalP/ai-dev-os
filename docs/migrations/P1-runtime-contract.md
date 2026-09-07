# P1 Runtime Contract 迁移说明

## 可见入口

```powershell
ai-dev-os runtime list
```

输出同时包含 Adapter 原始 `capabilities` 与稳定 `canonical_capabilities`，模型与 reasoning 档位来自真实无 prompt 协议发现；不可用 Runtime 返回 `available: false` 和原因，不伪造支持。

## 统一接口

Workbench 可通过 `AgentRuntime` 调用：

- `describe()` / `list_models()`
- `start(ExecutionSpec)` / `resume(session, message)`
- `send_message()` / `cancel()` / `status()`
- `list_events()` / `stream_events()`
- `archive()`

所有操作委派给现有 Adapter。统一层只处理能力词汇、恢复参数、活动轮次引用和事件读取，不包含 Provider 分支。

## 兼容边界

- 旧 `message`、`events`、`interrupt`、`models`、`approval_response` 能力名仍可查询。
- Session 新增字段均有默认值，旧 JSON/旧构造方式不失效；但旧引用缺少 run/execution/workspace/sandbox 身份时统一 `resume` 会 fail closed，不能猜测执行归属或扩大权限。
- 本阶段没有切换 Dispatcher、Hook 或 Main Agent 的控制权。
- `diff_events` 只在 Runtime 明确声明后暴露，不从普通事件能力推断。
