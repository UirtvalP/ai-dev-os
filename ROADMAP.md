# Roadmap

## Phase 0 — Repository foundation

- [x] 明确 V1 架构与参考项目边界
- [x] 初始化 Python 包、测试、模板与 Skill 目录
- [x] 定义核心模型和 Provider 协议
- [x] 建立 README 与 Roadmap

## Phase 1 — Workspace basics

- [ ] 实现 `workspace new`
- [ ] 实现 `workspace status`
- [ ] 生成稳定 Requirement ID
- [ ] 初始化人类可读的 Workspace 文件
- [ ] 为创建与读取流程添加测试

验收：可以在 `.workspace/REQ-*` 中创建并读取一个完整 Requirement Workspace。

## Phase 2 — Restore, checkpoint, handoff

- [ ] 实现 `workspace resume`
- [ ] 生成精简 Context Snapshot
- [ ] 实现 `workspace checkpoint`
- [ ] 实现 `workspace handoff`
- [ ] 记录多个可替换 Session

验收：新 Codex Thread 不读取旧对话，也能从 Handoff 和 Workspace 恢复下一步。

## Phase 3 — dashi-taskboard and Git

- [ ] 实现 Local Git Adapter
- [ ] 固化 dashi-taskboard / `taskctl` 接口
- [ ] 创建与同步 Task 状态
- [ ] 关联 Requirement、Task、Thread、branch 与 worktree
- [ ] 支持无 dashi 时的本地降级运行

验收：Task 状态与 Git 上下文可恢复，并且外部系统暂时不可用时不会破坏 Workspace。

## Phase 4 — Adaptive workflow

- [ ] 实现 tiny / normal / complex / research 路由
- [ ] 记录工作流升级原因
- [ ] 建立 verification 与 review gate
- [ ] 禁止 Agent 自主将 Requirement 标为 done

验收：小修复不走重型流程，跨模块或高风险工作可基于证据升级。

## Phase 5 — Codex Skill demo

- [ ] Skill 自动检测并恢复当前 Workspace
- [ ] 跑通 Thread A handoff → Thread B resume 演示
- [ ] 完成 V1 Acceptance Criteria
- [ ] 发布首个可安装版本

## Later

- Multica 与多 Agent 调度
- Obsidian / Markdown Knowledge Provider
- GitHub Issues / Linear Task Provider
- Remote Runtime、Web UI、自动 PR 与 CI feedback loop
