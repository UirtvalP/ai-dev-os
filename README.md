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
- 本地 Dispatcher 把用户明确移到 `in_progress` 的开发 Task 自动交给 Codex 执行。
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

该命令只补充项目级接入文件，并保留已有内容；重复执行不会重复写入。它会创建
`.ai-dev-os.json`，默认配置 `dashi` 及由项目名和绝对路径指纹确定性生成的项目 ID，但不会创建
Requirement Workspace；同时启动本地 Dispatcher。接入完成后，再使用
`workspace new` 创建首个 Requirement。接入内容包含 `.codex/hooks.json`，Hook 直接调用已安装的
`ai-dev-os hook`，不要求目标项目包含 AI Dev OS 源码树或项目内 `.venv`。

Dispatcher 的默认配置写在 `.ai-dev-os.json`：

```json
{
  "auto_execute_in_progress": true,
  "dispatcher_poll_seconds": 2.0,
  "codex_sandbox": "workspace-write",
  "codex_model": null
}
```

用户把带 `requirement:REQ-*` 标签的普通开发 Task 移到 `in_progress`，即表示授权执行。
Dispatcher 会在本地通过官方 `codex exec` 启动新 Session；存在由 Dispatcher 以受控 sandbox 启动的
历史 Session 时优先使用 `codex exec resume`，不会恢复权限来源未知的交互式 Thread。已有活动 Thread 绑定、专用 Requirement Review 卡以及相同 task version
不会被重复认领。Codex 结束但 Task 未进入 review 时，Dispatcher 会把它转为 `blocked` 并写入错误，
不会继续显示成“处理中”。
`codex_model` 默认为 `null`，表示沿用本机 Codex CLI 配置；若独立 CLI 与桌面端模型版本不兼容，
可在此指定该 CLI 支持的模型。执行失败会把服务端错误写入 Task 评论和本地 Dispatcher 日志。

```bash
ai-dev-os dispatcher status
ai-dev-os dispatcher start
ai-dev-os dispatcher stop
ai-dev-os dispatcher run-once
```

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
workspace confirm REQ-001 --user-confirmed
workspace request-changes REQ-001 --feedback "请补充失败场景测试"
```

`bootstrap` 是新 Codex Thread 的首次执行入口。显式传入 Requirement ID 时接入该需求；
无参数时先复用当前 Thread 已有绑定，否则只自动选择唯一可执行 Requirement。
处于 `in_review` 的 Requirement 正在等待用户确认，不再被 `current` 或自动 bootstrap 当作活动开发需求。
多个活动需求时会要求显式选择，已绑定的 Thread 也不会被静默切换。
新 Requirement 默认继承项目的 `dashi` 配置；只有显式使用 `workspace new --no-task-provider`
才关闭 Provider。未传 `--task` 时，唯一的 `in_progress` 开发 Task 会被恢复；没有活动 Task 时根据 `--request`
创建 Requirement 关联的 dashi Task、直接置为 `in_progress` 并绑定当前 Thread；多个活动 Task
时要求用 `--task` 明确选择。没有用户请求的 `SessionStart` 可只恢复 Requirement，首次
`UserPromptSubmit` 再创建开发 Task。显式切换到另一个 Requirement 前会先验证目标 Task，随后清除旧
Task 的 dashi 当前 Thread 绑定并把旧 Session 记为 `detached`；历史 Task 归属保留在该
Requirement 的 `sessions.json`，当前 Thread 再绑定新 Requirement 的 Task。同一 Thread 切回
旧 Requirement 时会重新建立 dashi 当前绑定。dashi/taskctl 离线时 bootstrap 会保留本地恢复能力，
在 `sessions.json` 记录尚未收敛的绑定/解绑，并在后续 bootstrap 自动重试；本地已有 Session 记录不会
成为永久跳过外部同步的理由。

当前 Codex 支持正式 lifecycle hooks。`ai-dev-os init` 安装的 `.codex/hooks.json` 在
`SessionStart` 尝试恢复已有绑定，在 `UserPromptSubmit` 读取结构化 hook 事件中的用户 prompt 并自动
执行完整 bootstrap，在 `SessionEnd` 自动 detach。Hook 运行时来自已安装 wheel。项目 Hook 第一次
启用或内容变更后必须由用户在 Codex 中审查并信任；未受信任时 Skill 只作为一次 bootstrap 回退触发器。

## Automation First

`src/workspace_orchestrator/automation/` 是正式 Automation Layer：

- `session_runtime.py`：CODEX_THREAD_ID、Session 注册/复用/结束与 Task 双向绑定。
- `requirement_attach.py`：项目发现、已有绑定、唯一活动 Requirement 与 `ambiguity`。
- `task_attach.py`：显式/已绑定/唯一 Task 选择、结构化请求创建与状态同步。
- `git_sync.py`：Git root、branch、worktree、status、commits、changed files 与 dashi Git 上下文。
- `state_sync.py`：纯结构化 Context Snapshot、checkpoint、handoff 和已知验证命令。
- `runtime.py`：一次触发连续编排上述确定性步骤；不调用 LLM，也不读取对话历史。
- `dispatcher.py`：从 dashi `in_progress` 状态确定性认领未绑定开发 Task，通过 Codex Adapter
  启动/恢复非交互 Session，并记录本地 JSON 状态与执行日志。
- `review_packet.py`：只从 Requirement、Intent、State、Verification、Handoff、开发 Task 与
  Git Adapter 生成结构化审查事实、稳定 revision/fingerprint 和中文 Markdown。

AI 仍负责语义理解、歧义含义、Root Cause、架构与实现决策、代码修改、Intent 判断以及无法稳定程序化的选择。`finalize` 由 AI 在判断语义工作已完成后触发一次；触发后的验证、状态同步与 Requirement review 门禁不再由 AI 决定。验证命令支持统一超时并严格校验配置；验证、Task 同步或 review 失败时，CLI 返回具体 blockers，保留 Session 供修复后重试，且不会误报为“只等待用户确认”。

`resume` 生成精简 Context Snapshot，其中只抽取相关的 User Principles、Project Intent 和
Requirement Intent 摘要，不把三份原文全部塞入上下文。CLI 通过 `CodexAgentProvider` 发现当前 Thread；Core 不读取
Codex 环境变量。`--task` 可显式关联 Task，恢复时也会自动关联当前 `in_progress` Task，关联结果
同时写入 Task Provider 与 `sessions.json.task_ids`。

Git 状态通过 Local Git Adapter 读取；从 Git 关联 worktree 启动时，项目发现会通过
`--git-common-dir` 复用主工作树的 `.workspace`。Requirement 已绑定 worktree 时，`resume` 和 `handoff` 始终
优先使用该 worktree，不会被当前 repo 根目录覆盖。`handoff` 会自动收集 Git changed files。
dashi-taskboard 通过可选的 JSON `taskctl` Adapter 接入，
用 `requirement:REQ-*` 标签保持 Requirement 与 Issue 的明确关联，并使用乐观 version 更新状态。
未安装时不影响本地 Workspace。`sessions.json`、`meta.json` 及 Markdown 复合更新使用轻量跨进程
OS advisory 文件锁与原子替换；进程被 `SessionEnd` 超时强杀后锁由操作系统立即释放，多个 Session
并发写入不会相互覆盖，Provider I/O 也不占用 Session 状态写锁。Review 会输出 Intent Alignment 的
`PASS / PARTIAL / VIOLATION`；只有四项 Intent 检查全部 PASS 才允许进入 `in_review`。

最短用户审查流程：

1. Agent 只执行一次 `workspace finalize REQ-ID`。通过后 Runtime 创建或复用一张带
   `requirement-review` 稳定标签的专用 Review 卡，并把完整 Review Packet 幂等写入卡片正文。Packet
   包含目标与范围、完成内容、验收、验证命令与结果、修改文件、Git 上下文、风险、开发 Task 和操作说明。
   正文发布成功且 revision/fingerprint 与当前 Workspace 证据一致后，Requirement 与卡片才进入 `in_review`。
2. 批准：用户在 dashi 将这张 Review 卡手动移到 `done`；后续 Hook、bootstrap 或
   `workspace status REQ-ID` 只有在 dashi 结构化活动能够证明最后一次进入 `done` 的操作者是
   用户时，才会把 Requirement 确认到 `done`。Agent、脚本、未知操作者或无法取得可靠活动事实时
   保持可重试，不产生批准语义。等价 CLI 是 `workspace confirm REQ-ID --user-confirmed`；配置了
   dashi 时，该显式入口同样必须核对当前 Workspace 事实与卡片 marker、revision、fingerprint，
   缺失或陈旧时拒绝确认。显式无 Provider 的本地审查不依赖外部卡片，仍可使用该命令。
3. 要求修改：用户先在 Review 卡留言，再手动移到 `in_progress`；后续同步会记录最新留言、
   把 Requirement 恢复为 `in_progress`，并重开仍处于 `in_review` 的开发 Task。等价 CLI 是
   `workspace request-changes REQ-ID --feedback "修改意见"`。

只有 meta 中专用卡 ID 与稳定标签同时匹配时才有审查语义。普通开发 Task `done`、仅留言、
没有本轮新留言的 `in_progress`、测试通过或 Agent
判断都不会批准或退回 Requirement。Runtime 永不自行把 Review 卡置为 `done`，`confirm` 也不会
自动完成任何外部 Task。Provider 离线时，退回修改先保存本地状态并在后续 bootstrap 重试 Task 收敛。
Review 卡正文发布失败、关键证据缺失，或卡片 marker 与当前 revision/fingerprint 不一致时，Runtime 会
返回具体 blocker 并保持/恢复 `in_progress`；旧 revision 的 `done` 卡绝不会批准新证据。Packet 的
验证区同时展示命令、状态和经过长度限制及路径清理的真实结果摘要，不会把状态行误当作执行结果。

并发边界：V1 支持多个 Session 并发更新不同 Requirement，也支持不同 Requirement 的 Thread
在**已经分配好的各自 Git worktree** 中并发执行；它们共享主工作树 `.workspace`，但 Session、
Requirement meta 与 Git worktree 绑定不会串线。V1 尚不自动创建、分配或回收每个 Requirement
的 branch/worktree；需要用户或上层工具先完成隔离。自动并行 Agent 与完整 Worktree 生命周期仍是后续能力。

## 设计原则

- 用户沟通和项目文档默认使用简体中文；代码标识符、CLI 命令、协议字段及不宜翻译的技术术语保留原文。
- Requirement 是一级持久对象，Task 是它的执行分解。
- 默认采用能安全完成工作的最轻流程；只有证据支持时才升级复杂度。
- Session 可以丢弃，Workspace 必须可恢复。
- 外部工具只通过 Adapter 接入，Core 不依赖具体产品。
- 长期知识是经过筛选的可复用经验，不是完整历史记录。

## 许可证

尚未选择开源许可证。在许可证明确前，保留全部权利。
