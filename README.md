# AI Dev OS

AI Dev OS 是一个面向 AI Coding Agent 的持久工作区编排系统。它让需求跨越可替换的 Agent Session / Thread 持续存在，并把需求、任务、执行、Git 状态和长期知识放在边界清晰的层中。

> Requirements persist. Tasks evolve. Sessions are disposable. Knowledge compounds.

## V1 目标

V1 优先证明一个最小、可靠、local-first 的闭环：

```text
Requirement Workspace
        ↓
Adaptive Workflow
        ↓
Task Graph
        ↓
Agent Session + Git
        ↓
Checkpoint / Handoff
```

- Workspace 保存需求、验收标准、计划、决策、验证与交接，是跨 Session 的 Source of Truth。
- dashi-taskboard 作为可替换的 Task Provider，保存执行状态与 Thread / branch / worktree 关联。
- Codex Skill 负责恢复 Workspace，并按 tiny / normal / complex / research 选择最轻的安全流程。
- Git 保存代码状态；V1 的 Agent Provider 仅支持 Codex。
- Multica 多 Agent 调度和 Obsidian 知识写入保留为后续 Adapter，不进入 V1。

## 目录

```text
src/workspace_orchestrator/   Python 核心包与 Adapter 协议
templates/workspace/          人类可读的 Workspace 文件模板
skills/workspace-orchestrator Codex 工作流 Skill
tests/                        核心生命周期测试
V1架构.md                     完整 V1 架构与验收标准
参考项目边界.md               可借鉴抽象与禁止复制的边界
ROADMAP.md                    第一版实施路线图
```

## 快速开始

需要 Python 3.11+。

```bash
python -m pip install -e ".[dev]"
pytest
workspace --help
```

当前仓库处于骨架阶段：领域模型、Provider 协议、模板和 Skill 已建立，CLI 生命周期实现列在 Roadmap 中。

## 设计原则

- Requirement 是一级持久对象，Task 是它的执行分解。
- 默认采用能安全完成工作的最轻流程；只有证据支持时才升级复杂度。
- Session 可以丢弃，Workspace 必须可恢复。
- 外部工具只通过 Adapter 接入，Core 不依赖具体产品。
- 长期知识是经过筛选的可复用经验，不是完整历史记录。

## License

尚未选择开源许可证。在许可证明确前，保留全部权利。
