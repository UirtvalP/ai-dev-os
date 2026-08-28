# Roadmap

## Phase 0 — Repository foundation

- [x] 明确 V1 架构与参考项目边界
- [x] 初始化 Python 包、测试、模板与 Skill 目录
- [x] 定义核心模型和 Provider 协议
- [x] 建立 README 与 Roadmap

## Phase 1 — Workspace basics

- [x] 实现 `workspace new`
- [x] 实现 `workspace status`
- [x] 生成稳定 Requirement ID
- [x] 初始化人类可读的 Workspace 文件
- [x] 为创建与读取流程添加测试

验收：可以在 `.workspace/REQ-*` 中创建并读取一个完整 Requirement Workspace。

## Phase 2 — Restore, checkpoint, handoff

- [x] 实现 `workspace resume`
- [x] 生成精简 Context Snapshot
- [x] 实现 `workspace checkpoint`
- [x] 实现 `workspace handoff`
- [x] 记录多个可替换 Session

验收：新 Codex Thread 不读取旧对话，也能从 Handoff 和 Workspace 恢复下一步。

## Phase 3 — dashi-taskboard and Git

- [x] 实现 Local Git Adapter
- [x] 固化 dashi-taskboard / `taskctl` JSON 接口
- [x] 实现 Task 创建、状态同步与关联接口
- [x] 关联 Requirement、Task、Thread、branch 与 worktree
- [x] 支持无 dashi 时的本地降级运行

说明：已在开发机上使用 dashi-taskboard v1.1.9 完成真实 `taskctl` 验收，包括项目与
Issue 创建、查询、评论、乐观版本状态更新、Git 上下文、完整 Codex Thread Binding，
以及 Workspace Restore / Review 联合验证。

验收：Task 状态与 Git 上下文可恢复，并且外部系统暂时不可用时不会破坏 Workspace。

## Phase 4 — Adaptive workflow

- [x] 实现 tiny / normal / complex / research 路由
- [x] 记录工作流升级原因
- [x] 建立 verification 与 review gate
- [x] 禁止 Agent 自主将 Requirement 标为 done

验收：小修复不走重型流程，跨模块或高风险工作可基于证据升级。

## Phase 5 — Codex Skill demo

- [x] Skill 自动检测并恢复当前 Workspace
- [x] 跑通 Thread A handoff → Thread B resume 演示
- [x] 完成 V1 Acceptance Criteria
- [x] 发布首个可安装版本

验收：`workspace-orchestrator` 已通过统一 Skill 管理器链接到 Codex、Cursor 和 Claude；
`ai_dev_os-0.1.0-py3-none-any.whl` 已完成隔离导入和 CLI 冒烟测试。

## Later

- Multica、多 Agent 调度与自动并行 Agent（待 Task Graph、锁和 Worktree 隔离成熟）
- 跨项目知识库与 Obsidian / Markdown Knowledge Provider（待知识候选审核模型成熟）
- GitHub Issues / Linear Task Provider
- Remote Runtime、Web UI、自动 PR 与 CI feedback loop
