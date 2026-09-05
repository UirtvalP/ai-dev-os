# Agent Runtime V2

状态：Phase 1 已验收（退出提交 `3bf3846`，Gate 于2026-09-05 17:18:20 +08:00 PASS）；当前进入 Phase 2，不代表 REQ-020 完成。

## 执行与兼容

`AutoDispatcher` 只接收 `AgentExecutionPort`，具体 Runtime 在 `composition.py` 注入。
`RuntimeExecutor` 将 start/resume/wait/close 组合为同步执行；只有确定的
`session_missing` 才允许恢复失败后新建，超时、权限或传输异常不会重放 prompt。
执行返回候选结果，不生成 Task/Requirement 完成授权。Hook 继续负责既有绑定，
Runtime 只发布 Session/Turn 事实。

Dispatcher 的托管 Hook 信任结果在组合入口按 Adapter 支持能力收窄，仅 Codex
接收该授权；Cursor/Claude 不会因为项目存在标准 `.codex/hooks.json` 而被错误阻断。

`CodexAgentProvider`、`CodexExecProvider`、`CodexExecutionResult` 旧 import 和调用仍保留。
归档使用长期 stdio，逐请求等待 initialize 与 thread/archive 响应后才关闭 stdin。

旧 `.ai-dev-os.json` 的 `codex_model`、`codex_sandbox` 继续有效；可选新字段为：

```json
{
  "agent_runtime": "codex",
  "agent_model": null,
  "agent_sandbox": "workspace-write"
}
```

这是已有项目配置的字段示例，不是替换整份配置。缺失或 null 的新 model/sandbox
字段回退到旧值；Runtime 选择支持 codex、cursor、claude。此处不实现智能路由，
那属于 Phase 2。

## 能力边界

| Runtime | 传输 | 控制 | 写权限边界 |
|---|---|---|---|
| Codex | App Server 长期 stdio JSONL | start/resume/read/message/steer/interrupt/archive | 实际 App Server sandbox |
| Cursor | ACP stdio JSON-RPC | 协商后的 start/resume/message/interrupt | 仅受限 read-only；不宣称 OS sandbox |
| Claude | CLI stream-json 与已核实 SDK 控制协议 | start/resume/message/interrupt | 仅受限 read-only；不宣称 OS sandbox |

模型来自 Runtime 协商，不用固定列表冒充可用模型。未安装返回 `unavailable`，
未实现能力返回 `unsupported`。Cursor/Claude 的 workspace-write 请求失败关闭，
只有后续加入可信隔离后才能分配其写任务。权限请求默认拒绝；取消请求的响应
不等于轮次完成。未知事件保留原始 payload。

## 事件与入口

事件按运行分片保存在 `.workspace/runtime-events`。每次追加先持锁、完整写入并
fsync，消费者不必等 Agent 退出；序号连续、event_id 严格幂等。损坏的未提交尾字节
先备份后修复，完整行损坏拒绝截断。未知扩展字段保留，不引入数据库。

三种 Adapter 的 `kind` 统一为 `session/turn/message/tool/approval/error/completion/unknown`。
`completion` 表示轮次结束（包括失败和取消），不是成功或需求完成授权。
原始 wire payload 不改写；Cursor/Claude 的细分事件名保存在可选 `detail_kind` 扩展中。

```text
ai-dev-os runtime list
ai-dev-os runtime events RUN-ID --root PROJECT --after 0 --limit 100
```

Runtime list 只发现能力，不执行用户任务。Dispatcher 结果日志额外保存 runtime_id、
run_id 与标准摘要，旧字段继续保留。

## 集中验收

按照用户最新要求，先完成本阶段功能集成，再集中运行完整测试与独立 Review，
不在开发中反复跑全量或穿插正式审查。阶段 exact-SHA 门禁仍然适用。

- 普通全量 pytest 包含三 Adapter 的 fake-server E2E、并发/背压/EOF/超时、
  EventStore 崩溃和旧 V1 回归；默认不触发真实 Agent 推理。
- `uv run --frozen --extra dev python scripts/runtime_live_smoke.py` 是本机明确执行的
  live suite，只创建/恢复/控制/归档自己的临时 Codex Thread。
- 本机未安装 Cursor/Claude 时验证真实 unavailable；一旦发现它们已安装但缺对应
  live suite，拒绝把该机器的阶段验收标为成功。
- Phase 1 GateDefinition 已声明 live suite 命令，不能以人工自报或跳过测试替代收据。

本机 npm Codex 0.141.0 无法运行当前默认 Astra（服务端明确拒绝版本过旧）。
真实 smoke 已通过本机现有 App CLI 0.153.4；验收时通过本次进程的 `AI_DEV_OS_CODEX`
显式选择该可执行文件并设置 `PYTHONUTF8=1`，不更换模型或修改全局安装/配置。
测试输出记录实际可执行路径与版本，后续 Gate 重验必须沿用相同选择。

协议依据：[Codex App Server](https://learn.chatgpt.com/docs/app-server)、
[Cursor ACP](https://cursor.com/docs/cli/acp)、
[Claude CLI](https://code.claude.com/docs/en/cli-reference) 与
[Claude SDK 控制协议](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/_internal/query.py)。
这些是独立 Adapter 的协议参考，不复制第三方实现。
