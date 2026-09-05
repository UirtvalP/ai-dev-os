## 11. Phase 5 — Dashboard / Event Plane（AID-175）

### 实现范围

1. 以 Requirement 为首页对象，展示 Task DAG、Primary/Worker/Test Agent 树、Runtime/model、状态、Git、验证和阻塞。
2. Event Query API 从 append-only Event Store 构建可重放投影；Dashboard 缓存可重建，不成为事实来源。
3. Agent 详情展示规范化消息、工具调用、文件变化、验证、审批、成本/用量（Runtime 提供时）和当前 turn。
4. 支持向指定 Agent：运行中 `steer`、空闲时开始新 turn、不支持即时投递时写入持久 command queue。
5. 每条用户指令具有 command ID、目标 Session、状态、投递时间和结果；重试不重复发送。
6. 提供本地认证/来源检查、输入限长和危险操作的服务端 Gate；前端不能直接调用 Runtime 或 Git。
7. dashi 继续作为 Task Provider；V2 Dashboard 聚合它而不是强制替换。

### 退出标准

- [ ] `P5-AC-01` 页面可展开 Requirement → Agent → Session/Turn，并实时看到事件增量。
- [ ] `P5-AC-02` 运行中回复、排队下一条、取消和失败重试都有 API/浏览器 E2E。
- [ ] `P5-AC-03` 页面刷新、服务重启和投影重建不丢消息、不重复投递。
- [ ] `P5-AC-04` Dashboard 关闭时 Orchestrator、Workspace 和 Hook 生命周期仍完整工作。
- [ ] `P5-AC-05` 本地来源限制、权限、脱敏、游标、断线重连与跨 Requirement 隔离有负向测试。
- [ ] `P5-AC-06` CommonQualityGate、可访问性 smoke 与独立 Phase 5 Review 全部 PASS。
