## 6. Phase 0 — 审计与主计划（AID-151）

### 范围

- 建立当前能力矩阵、依赖关系、兼容边界和风险清单。
- 建立 V2 版本化架构与六阶段实施路线。
- 把 `REQ-019` 设为 Runtime 改造的显式前置依赖。
- 为 `REQ-020` 分配独立 branch/worktree，保护脏 `main`。

### 退出标准

- [x] `P0-AC-01` 当前源码、测试、Provider、Hook、Review、Git 和 Dispatcher 已完成证据审计。
- [x] `P0-AC-02` V2 主计划已进入版本控制候选。
- [x] `P0-AC-03` `REQ-020` Requirement、Task、Session 均绑定独立 worktree。
- [x] `P0-AC-04` 干净 `origin/main` 的 pytest、ruff、diff-check 全部通过。
- [x] `P0-AC-05` 六个实施 Task 已创建并以 `blocked_by` 串联，后阶段初始状态均为 `blocked`。
- [ ] `P0-AC-06` Windows/Linux 与 Python 3.11/当前支持版本 CI、真实类型检查均通过。
- [x] `P0-AC-07` 最小 `PhaseGateRecord` 生成、exact-SHA 校验、失效和前序包含关系有自动测试。
- [x] `P0-AC-08` `REQ-019` 修复已提交并推送，V2 分支已更新到该基线。
- [ ] `P0-AC-09` CommonQualityGate 与独立 Phase 0 Review 全部通过。

最后一项未通过前，Phase 1 只能做不依赖 Dispatcher 的设计与测试准备，不能完成 Runtime 切换。
