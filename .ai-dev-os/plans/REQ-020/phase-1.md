## 7. Phase 1 — Agent Runtime V2（AID-170）

### 实现范围

1. 新增 Provider 无关的 Runtime contracts、ports、error 与 capability model。
2. 建立 append-only Runtime Event Store，支持有序追加、幂等 event ID、游标 replay、并发写入和损坏尾行恢复。
3. 实现 Codex App Server `stdio` JSON-RPC Client：初始化、模型发现、thread start/resume/read/archive、turn start/steer/interrupt、事件与服务端请求处理。
4. 实现 Cursor ACP `stdio` JSON-RPC Adapter 与 Claude Agent/CLI 事件 Adapter；三者共享 Runtime contracts，只声明各自真实能力。
5. 将各 Runtime 通知映射为标准 session/turn/message/tool/approval/error/completion 事件；未知事件保真降级。
6. 保留 `CodexAgentProvider` / `CodexExecProvider` 兼容 façade，一个发布周期内不破坏现有 import 和 CLI。
7. Dispatcher 只依赖 `AgentExecutionPort`；具体 Runtime Adapter 仅在 composition root 注入。
8. Runtime 只发布 Session 事实，不重复执行 Hook 已负责的 Requirement/Task/Session attach。

### 退出标准

- [ ] `P1-AC-01` 非 Codex fake Runtime 可完整驱动 Dispatcher。
- [ ] `P1-AC-02` Runtime 能报告真实 capabilities 与 model descriptors；不支持/未安装操作返回结构化结果。
- [ ] `P1-AC-03` Codex start、resume、resume fallback、message、steer、interrupt、archive 与 event replay 契约通过；真实 Stop Hook archive 不再因 stdin 提前关闭而丢响应。
- [ ] `P1-AC-04` Cursor ACP 与 Claude Adapter 的 start/resume/message/control/event 能力矩阵、契约测试和 hermetic E2E 通过。
- [ ] `P1-AC-05` 事件在 Agent 进程退出前可被消费者读取。
- [ ] `P1-AC-06` 旧 Workspace、Session JSON、配置、CLI 和 Python imports 保持兼容。
- [ ] `P1-AC-07` 三种 Adapter 的 fake-server E2E 通过；本机已安装 Runtime 完成 live smoke，未安装 Runtime 明确报告 `unavailable` 而非伪成功。
- [ ] `P1-AC-08` CommonQualityGate 与独立 Phase 1 Review 全部 PASS。
