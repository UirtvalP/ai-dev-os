# AI Dev OS

AI Dev OS 是一个面向 AI Coding Agent 的持久工作区编排系统。它让需求跨越可替换的 Agent Session / Thread 持续存在，并把需求、任务、执行、Git 状态和长期知识放在边界清晰的层中。

> 需求持续存在，任务不断演进，会话可以替换，知识持续积累。

本文保留必要的程序标识符和产品名称。常用术语对照：Requirement（需求）、Task（任务）、
Workspace（工作区）、Session / Thread（会话 / 线程）、Workflow（工作流）、Checkpoint（检查点）、
Handoff（交接）、Context Snapshot（上下文快照）、Adapter（适配器）、Provider（提供方）、
Source of Truth（事实来源）。程序状态值对照：`draft`（草稿）、`ready`（就绪）、
`in_progress`（进行中）、`in_review`（审查中）、`done`（已完成）、`blocked`（已阻塞）。

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
- Intent Layer 保存用户长期原则、项目取舍和 Requirement 的设计理由；技术验收通过但 Intent
  Alignment 失败时仍不能进入 review。
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

从源码目录安装为用户级命令（推荐使用 `uv`）：

```bash
uv tool install .
ai-dev-os --version
```

`uv` 会把 `ai-dev-os` 和 `workspace` 安装到用户 PATH 可发现的工具目录。安装后可以离开
本仓库，在任意现有项目目录直接运行 `ai-dev-os init`。仅在项目虚拟环境中执行
`pip install -e` 不会让其他目录自动找到该命令。

开发本仓库时再使用 editable 环境：

```bash
python -m pip install -e ".[dev]"
pytest
ai-dev-os --help
workspace --help
```

将一个现有项目接入 AI Dev OS：

```bash
cd existing-project
ai-dev-os init
```

该命令只补充项目级接入文件，并保留已有内容；重复执行不会重复写入。它不会创建
Requirement Workspace，也不会改变 `workspace` 命令层级。接入完成后，再使用
`workspace new` 创建首个 Requirement。

构建可安装 wheel：

```bash
python -m pip wheel . --no-deps --wheel-dir dist
uv tool install --force dist/ai_dev_os-0.1.0-py3-none-any.whl
```

当前仓库已经跑通 V1 的本地核心生命周期：

```bash
workspace new "实现身份认证" --acceptance "有效用户可以登录"
workspace bootstrap REQ-001 --request "实现登录接口"
workspace current
workspace status REQ-001
workspace checkpoint REQ-001 --phase implementation --task TASK-001 --completed "登录接口" --next-action "实现中间件"
workspace handoff REQ-001 --current-state "登录功能可用" --next-action "实现中间件"
workspace finalize REQ-001 --completed "登录接口"
workspace resume REQ-001
workspace review REQ-001
```

`bootstrap` 是新 Codex Thread 的首次执行入口。显式传入 Requirement ID 时接入该需求；
无参数时先复用当前 Thread 已有绑定，否则只自动选择唯一活动 Requirement。
多个活动需求时会要求显式选择，已绑定的 Thread 也不会被静默切换。
未传 `--task` 时，唯一的 `in_progress` Task 会被恢复；没有活动 Task 时根据 `--request`
创建 Requirement 关联的 dashi Task、直接置为 `in_progress` 并绑定当前 Thread；多个活动 Task
时要求用 `--task` 明确选择。显式切换到另一个 Requirement 前会先验证目标 Task，随后清除旧
Task 的 dashi 当前 Thread 绑定并把旧 Session 记为 `detached`；历史 Task 归属保留在该
Requirement 的 `sessions.json`，当前 Thread 再绑定新 Requirement 的 Task。同一 Thread 切回
旧 Requirement 时会重新建立 dashi 当前绑定。

当前 Codex 支持正式 lifecycle hooks。`ai-dev-os init` 会幂等安装项目的
`.codex/hooks.json`：`SessionStart` 尝试恢复已有绑定，`UserPromptSubmit` 读取结构化 hook
事件中的用户 prompt 并自动执行完整 bootstrap，`SessionEnd` 自动 detach。项目配置了
dashi Task Provider 时，Hook 会先检测本地任务面板端口；未运行则通过本机
`dashi-taskboard` 启动器在后台按需拉起，已运行则直接复用。项目 Hook 第一次启用或内容
变更后必须由用户在 Codex 中审查并信任；未受信任时 Skill 只作为一次 bootstrap 回退触发器。

## Automation First

`src/workspace_orchestrator/automation/` 是正式 Automation Layer：

- `session_runtime.py`：CODEX_THREAD_ID、Session 注册/复用/结束与 Task 双向绑定。
- `requirement_attach.py`：项目发现、已有绑定、唯一活动 Requirement 与 `ambiguity`。
- `task_attach.py`：显式/已绑定/唯一 Task 选择、结构化请求创建与状态同步。
- `git_sync.py`：Git root、branch、worktree、status、commits、changed files 与 dashi Git 上下文。
- `state_sync.py`：纯结构化 Context Snapshot、checkpoint、handoff 和已知验证命令。
- `runtime.py`：一次触发连续编排上述确定性步骤；不调用 LLM，也不读取对话历史。

AI 仍负责语义理解、歧义含义、Root Cause、架构与实现决策、代码修改、Intent 判断以及无法稳定程序化的选择。`finalize` 由 AI 在判断语义工作已完成后触发一次；触发后的验证、状态同步、Requirement review 门禁、交接与 detach 均不再由 AI 决定。

`resume` 生成精简 Context Snapshot，其中只抽取相关的 User Principles、Project Intent 和
Requirement Intent 摘要，不把三份原文全部塞入上下文。CLI 通过 `CodexAgentProvider` 发现当前 Thread；Core 不读取
Codex 环境变量。`--task` 可显式关联 Task，恢复时也会自动关联当前 `in_progress` Task，关联结果
同时写入 Task Provider 与 `sessions.json.task_ids`。

Git 状态通过 Local Git Adapter 读取；Requirement 已绑定 worktree 时，`resume` 和 `handoff` 始终
优先使用该 worktree，不会被当前 repo 根目录覆盖。`handoff` 会自动收集 Git changed files。
dashi-taskboard 通过可选的 JSON `taskctl` Adapter 接入，
用 `requirement:REQ-*` 标签保持 Requirement 与 Issue 的明确关联，并使用乐观 version 更新状态。
未安装时不影响本地 Workspace。Review 会输出 Intent Alignment 的 `PASS / PARTIAL / VIOLATION`；
只有四项 Intent 检查全部 PASS 才允许进入 `in_review`，且永远不会自动标记 `done`。

## 设计原则

- 用户沟通和项目文档默认使用简体中文；代码标识符、CLI 命令、协议字段及不宜翻译的技术术语保留原文。
- Requirement 是一级持久对象，Task 是它的执行分解。
- 默认采用能安全完成工作的最轻流程；只有证据支持时才升级复杂度。
- Session 可以丢弃，Workspace 必须可恢复。
- 外部工具只通过 Adapter 接入，Core 不依赖具体产品。
- 长期知识是经过筛选的可复用经验，不是完整历史记录。

## 许可证

尚未选择开源许可证。在许可证明确前，保留全部权利。
