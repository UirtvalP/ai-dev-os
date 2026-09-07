"""AI Dev OS 产品级命令行入口。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .adapters.git import GitError
from .adapters.package import ToolInstallerError, ToolUpgradeResult, UvToolInstaller
from .agent_runtime.events import RuntimeEventStore
from .automation.dispatcher import (
    AutoDispatcher,
    dispatcher_status,
    serve_dispatcher,
    start_dispatcher,
    stop_dispatcher,
)
from .automation.requirement_attach import discover_project_root
from .composition import configured_executor, runtime_descriptors
from .console import configure_standard_streams as _configure_standard_streams
from .hook_runtime import main as hook_main
from .orchestration.contracts import PlanningRequest
from .project_config import load_project_config
from .project_init import InitResult, initialize_project, migrate_project, register_project
from .project_registry import GlobalProjectRegistry, RegisteredProject
from .workspace import WorkspaceError, WorkspaceStore

DEFAULT_UPGRADE_SOURCE = "git+https://github.com/UirtvalP/ai-dev-os.git"
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-dev-os",
        description="管理全局 AI Dev OS CLI 与项目接入。",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    init_parser = commands.add_parser("init", help="将现有项目接入 AI Dev OS")
    init_parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=Path.cwd(),
        help="项目目录（默认：当前目录）",
    )
    upgrade_parser = commands.add_parser("upgrade", help="更新全局 AI Dev OS CLI")
    upgrade_parser.add_argument(
        "--source",
        default=DEFAULT_UPGRADE_SOURCE,
        help="安装来源（默认：AI Dev OS 官方 Git 仓库）",
    )
    migrate_parser = commands.add_parser("migrate", help="迁移已接入项目的持久文件格式")
    migrate_parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=Path.cwd(),
        help="已接入的项目目录（默认：当前目录）",
    )
    project = commands.add_parser("project", help="查询或取消登记全局项目索引")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    add = project_commands.add_parser("add", help="注册独立 Workbench 项目，不安装 Agent Hook")
    add.add_argument("path", nargs="?", type=Path, default=Path.cwd())
    isolation = project_commands.add_parser(
        "isolation-check", help="检查项目是否会接管原生 Agent 生命周期",
    )
    isolation.add_argument("path", nargs="?", type=Path, default=Path.cwd())
    project_commands.add_parser("list", help="列出全部已登记项目")
    show = project_commands.add_parser("show", help="显示一个已登记项目")
    show.add_argument("project_id", help="稳定项目 ID")
    unregister = project_commands.add_parser(
        "unregister", help="只取消全局登记，不删除项目文件或外部 Task"
    )
    unregister.add_argument("project_id", help="稳定项目 ID")
    commands.add_parser("hook", help=argparse.SUPPRESS)
    runtime = commands.add_parser("runtime", help="发现 Agent Runtime 能力或读取实时事件")
    runtime_commands = runtime.add_subparsers(dest="runtime_command", required=True)
    runtime_commands.add_parser("list", help="显示已安装 Runtime 的真实能力与可用模型")
    events = runtime_commands.add_parser("events", help="按运行 ID 和游标重放持久事件")
    events.add_argument("run_id", help="Runtime 运行 ID")
    events.add_argument("--root", type=Path, default=Path.cwd(), help="项目或关联 worktree 目录")
    events.add_argument("--after", type=int, default=0, help="排他事件序号（默认：0）")
    events.add_argument("--limit", type=int, default=1000, help="最多返回的事件数")
    workbench = commands.add_parser("workbench", help="由 AI Dev OS 主动创建并启动 Execution")
    workbench_commands = workbench.add_subparsers(dest="workbench_command", required=True)
    demo = workbench_commands.add_parser("demo", help="运行不接管原生 Agent 的 P2 vertical slice")
    demo.add_argument("requirement_id")
    demo.add_argument("--task", default="TASK-P2-DEMO")
    demo.add_argument("--root", type=Path, default=Path.cwd())
    owner = workbench_commands.add_parser("owner", help="刷新并显示 Requirement Main Agent 状态")
    owner.add_argument("requirement_id")
    owner.add_argument("--root", type=Path, default=Path.cwd())
    dashboard = commands.add_parser("dashboard", help="启动带认证的本机 Dashboard 控制面")
    dashboard_commands = dashboard.add_subparsers(dest="dashboard_command", required=True)
    serve = dashboard_commands.add_parser("serve", help="在回环地址启动远程控制入口")
    serve.add_argument("requirement_id", help="固定 Requirement ID")
    session_target = serve.add_mutually_exclusive_group(required=True)
    session_target.add_argument("--session", help="固定 Codex Session ID")
    session_target.add_argument("--new-session", action="store_true", help="创建专用 Codex Session")
    serve.add_argument("--root", type=Path, default=Path.cwd(), help="项目或关联 worktree 目录")
    serve.add_argument("--host", default="127.0.0.1", help="只允许回环地址")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--token-file", type=Path, required=True, help="token 文件；不存在时自动生成")
    serve.add_argument("--run-id", help="事件分片 ID；默认 remote-<session>")
    orchestration = commands.add_parser("orchestration", help="V2 单写者计划、执行与恢复控制面")
    orchestration_commands = orchestration.add_subparsers(dest="orchestration_command", required=True)
    for action, description in (
        ("status", "读取本地编排状态，不启动 Worker"),
        ("prepare", "从 main 为原始 Task 幂等分配工作树，输出可冻结的计划请求"),
        ("plan", "从结构化 JSON 冻结本需求执行计划，不启动 Worker"),
        ("run", "前台续租并执行至候选或阻塞；不会完成 Requirement"),
        ("verify", "整批验证已退出 Worker 的候选；不合并或完成 Requirement"),
    ):
        command = orchestration_commands.add_parser(action, help=description)
        command.add_argument("requirement_id", help="已经存在的 Requirement ID")
        command.add_argument("--root", type=Path, default=Path.cwd())
        if action not in ("status", "prepare"):
            command.add_argument("--owner", required=True, help="操作员/控制器唯一身份")
            command.add_argument("--max-workers", type=int, default=1)
            command.add_argument("--allow-worktree-root", type=Path, action="append", default=[])
            command.add_argument("--allow-network", action="store_true",
                                 help="显式允许隔离域联网；不宣称已实现域名过滤")
        if action in ("plan", "prepare"):
            command.add_argument("--file", type=Path, required=True, help="PlanningRequest JSON 文件")
        if action == "prepare":
            command.add_argument("--expected-main", required=True, help="完整、明确的 main 基线 SHA")
        elif action == "run":
            command.add_argument("--timeout", type=float, default=300, help="本次前台服务最长秒数")
        elif action == "verify":
            command.add_argument("--task-id", action="append", default=[], help="省略时验证本批全部候选")
            command.add_argument("--refresh", action="store_true", help="明确重验已 accepted 的同一候选，保留旧收据历史")
            command.add_argument("--commands-file", type=Path, help="可信操作员提供的 VerificationCommand 数组")
    integration = commands.add_parser("integration", help="Git 集成队列、CAS 合并与中断恢复")
    integration_commands = integration.add_subparsers(dest="integration_command", required=True)
    for action in ("merge", "status", "reconcile", "recover-post-merge"):
        command = integration_commands.add_parser(action)
        command.add_argument("requirement_id")
        command.add_argument("--request-id", required=True, help="可重试的稳定操作 ID")
        command.add_argument("--root", type=Path, default=Path.cwd())
        if action == "merge":
            command.add_argument("--expected-main", required=True, help="副作用前以 CAS 再校验的完整 main SHA")
            command.add_argument("--commands-file", type=Path)
        elif action == "recover-post-merge":
            command.add_argument("--recovery-id", required=True, help="本次显式恢复的稳定 ID；未知执行不可重放")
    dispatcher = commands.add_parser(
        "dispatcher", help="管理 Task → Agent 自动执行 Dispatcher"
    )
    dispatcher_commands = dispatcher.add_subparsers(dest="dispatcher_command", required=True)
    for name, help_text in (
        ("start", "启动后台 Dispatcher"),
        ("stop", "停止后台 Dispatcher"),
        ("status", "查看 Dispatcher 状态"),
        ("run-once", "同步检查一次并处理至多一个 Task"),
        ("serve", argparse.SUPPRESS),
    ):
        command = dispatcher_commands.add_parser(name, help=help_text)
        command.add_argument(
            "--root",
            type=Path,
            default=Path.cwd(),
            help="项目或关联 worktree 目录（默认：当前目录）",
        )
    return parser


def _format_result(result: InitResult, *, action: str) -> str:
    lines = [f"AI Dev OS {action}：{result.root}"]
    labels = (
        ("已创建", result.created),
        ("已更新", result.updated),
        ("已保留", result.preserved),
    )
    lines.extend(f"{label}：{', '.join(paths)}" for label, paths in labels if paths)
    if action == "已接入":
        lines.append("下一步：使用 workspace new 创建首个 Requirement。")
    return "\n".join(lines)


def _format_upgrade_result(result: ToolUpgradeResult) -> str:
    state = "更新已安排" if result.scheduled else "已更新"
    lines = [f"AI Dev OS 全局 CLI {state}。", f"来源：{result.source}"]
    if result.details:
        lines.append(result.details)
    if result.result_path:
        lines.append(f"结果日志：{result.result_path}")
    lines.append("更新完成后，后续 ai-dev-os、workspace 与项目 Hook 调用将使用新版本能力。")
    return "\n".join(lines)


def _sync_registry_after_local_success(root: Path, *, action: str) -> str:
    """本地项目操作成功后刷新索引；失败时保留可重试的有效项目状态。"""

    try:
        config = load_project_config(root)
        if config is None:
            raise WorkspaceError(f"项目配置不存在：{root / '.ai-dev-os.json'}")
        project = GlobalProjectRegistry().register(root, config)
    except (OSError, UnicodeError, WorkspaceError) as exc:
        return (
            f"警告：项目{action}成功，但全局注册失败：{exc}\n"
            "再次运行同一命令可重试 Global Project Registry。"
        )
    return f"Global Project Registry：已登记 {project.id}（{project.name}）"


def _format_project_list(projects: tuple[RegisteredProject, ...]) -> str:
    if not projects:
        return "尚未登记任何项目。"
    headers = ("ID", "NAME", "STATUS", "PATH")
    rows = [(item.id, item.name, item.status, item.path) for item in projects]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers) - 1)
    ]
    lines = [
        (
            f"{headers[0]:<{widths[0]}}  {headers[1]:<{widths[1]}}  "
            f"{headers[2]:<{widths[2]}}  {headers[3]}"
        )
    ]
    lines.extend(
        f"{row[0]:<{widths[0]}}  {row[1]:<{widths[1]}}  "
        f"{row[2]:<{widths[2]}}  {row[3]}"
        for row in rows
    )
    return "\n".join(lines)


def _format_project(project: RegisteredProject) -> str:
    return "\n".join(
        (
            f"Project: {project.name}",
            f"ID: {project.id}",
            f"Path: {project.path}",
            f"Status: {project.status}",
            f"Task Provider: {project.task_provider or '未配置'}",
            f"Task Project: {project.task_project_id or '未配置'}",
            f"Registered At: {project.registered_at}",
            f"Updated At: {project.updated_at}",
        )
    )


def run(args: argparse.Namespace) -> str:
    if args.command == "workbench":
        if args.workbench_command == "owner":
            from .main_agent import RequirementOwner

            execution_root = args.root.expanduser().resolve()
            owner_store = WorkspaceStore(
                discover_project_root(execution_root), execution_root=execution_root,
            )
            requirement_owner = RequirementOwner(owner_store, args.requirement_id)
            owner_state = requirement_owner.observe(
                expected_revision=requirement_owner.current_revision(),
            )
            return json.dumps(owner_state.to_dict(), ensure_ascii=False, indent=2)
        from .workbench import demo_workbench

        execution_root = args.root.expanduser().resolve()
        store = WorkspaceStore(discover_project_root(execution_root), execution_root=execution_root)
        execution = demo_workbench(store, args.requirement_id, args.task)
        return json.dumps(execution.to_dict(), ensure_ascii=False, indent=2)
    if args.command == "integration":
        from .integration_composition import configured_integration, load_verification_commands

        execution_root = args.root.expanduser().resolve()
        workspace = WorkspaceStore(discover_project_root(execution_root), execution_root=execution_root)
        service = configured_integration(workspace, args.requirement_id)
        if args.integration_command == "status":
            integration_result = service.status(args.requirement_id, args.request_id)
        elif args.integration_command == "reconcile":
            integration_result = service.reconcile(args.requirement_id, args.request_id).to_dict()
        elif args.integration_command == "recover-post-merge":
            integration_result = service.recover_post_merge(
                args.requirement_id, args.request_id, args.recovery_id,
            ).to_dict()
        else:
            from .integration_composition import configured_verification

            integration_result = service.integrate(
                args.requirement_id, args.request_id, args.expected_main,
                load_verification_commands(workspace, args.commands_file),
                configured_verification(workspace).environment,
            ).to_dict()
        return json.dumps(integration_result, ensure_ascii=False, indent=2)
    if args.command == "orchestration":
        from .orchestration.contracts import PolicyError
        from .orchestration_composition import (
            configured_projection,
            configured_supervisor,
            control_store,
            run_supervisor,
        )

        execution_root = args.root.expanduser().resolve()
        store = WorkspaceStore(discover_project_root(execution_root), execution_root=execution_root)
        if args.orchestration_command == "status":
            orchestration_result = control_store(store, args.requirement_id).snapshot()
        elif args.orchestration_command == "prepare":
            from .integration_composition import prepare_git_request

            request = _load_planning_request(args.file, args.requirement_id)
            orchestration_result = prepare_git_request(
                store, request, expected_main_sha=args.expected_main,
            ).to_dict()
        else:
            supervisor = configured_supervisor(
                store, args.requirement_id, owner=args.owner, max_workers=args.max_workers,
                allow_network=args.allow_network,
                allowed_worktree_roots=tuple(path.absolute() for path in args.allow_worktree_root),
            )
            try:
                if args.orchestration_command == "plan":
                    request = _load_planning_request(args.file, args.requirement_id)
                    supervisor.acquire()
                    try:
                        supervisor.initialize(request)
                    finally:
                        supervisor.close()
                    orchestration_result = supervisor.status()
                elif args.orchestration_command == "verify":
                    from .integration_composition import (
                        configured_verification,
                        load_verification_commands,
                        verify_candidates,
                    )

                    orchestration_result = verify_candidates(
                        supervisor, load_verification_commands(store, args.commands_file),
                        configured_verification(store).environment, task_ids=tuple(args.task_id),
                        refresh=args.refresh,
                    )
                else:
                    orchestration_result = run_supervisor(
                        supervisor, timeout_seconds=args.timeout,
                        projection=configured_projection(store, args.requirement_id),
                    )
            except (ValueError, PolicyError) as exc:
                raise WorkspaceError(f"编排请求被拒绝：{exc}") from exc
        return json.dumps(orchestration_result, ensure_ascii=False, indent=2)
    if args.command == "runtime":
        if args.runtime_command == "list":
            descriptions = []
            for item in runtime_descriptors():
                document = asdict(item)
                document["canonical_capabilities"] = item.canonical_capabilities
                descriptions.append(document)
            return json.dumps(descriptions, ensure_ascii=False, indent=2)
        execution_root = args.root.expanduser().resolve()
        store = WorkspaceStore(discover_project_root(execution_root), execution_root=execution_root)
        events = RuntimeEventStore(store.root / "runtime-events").replay(
            args.run_id, after=args.after, limit=args.limit
        )
        return json.dumps([item.to_dict() for item in events], ensure_ascii=False, indent=2)
    if args.command == "dashboard":
        from .remote_control import RemoteController, load_or_create_token, serve_remote_control

        execution_root = args.root.expanduser().resolve()
        store = WorkspaceStore(discover_project_root(execution_root), execution_root=execution_root)
        run_id = args.run_id or f"remote-{args.session or 'new-session'}"
        token = load_or_create_token(args.token_file)
        controller = RemoteController(
            store, args.requirement_id, args.session, run_id=run_id,
        )
        print(json.dumps({
            "status": "serving", "host": args.host, "port": args.port,
            "requirement_id": args.requirement_id, "session_id": args.session,
            "token_file": str(args.token_file.expanduser().resolve()),
            "pid": os.getpid(), "safety": {"sandbox": "read-only", "approvals": "deny"},
        }, ensure_ascii=False), flush=True)
        serve_remote_control(controller, token=token, host=args.host, port=args.port)
        return ""
    if args.command == "init":
        result = initialize_project(args.path)
        registry_message = _sync_registry_after_local_success(result.root, action="接入")
        status = start_dispatcher(
            WorkspaceStore(result.root, execution_root=result.root)
        )
        suffix = (
            "\nDispatcher：已启动，dashi Task 移到 in_progress 后会自动执行。"
            if status.get("running") or status.get("status") == "starting"
            else f"\nDispatcher：{status.get('status', '未启动')}。"
        )
        return _format_result(result, action="已接入") + f"\n{registry_message}" + suffix
    if args.command == "upgrade":
        return _format_upgrade_result(UvToolInstaller().upgrade(args.source))
    if args.command == "migrate":
        result = migrate_project(args.path)
        registry_message = _sync_registry_after_local_success(result.root, action="迁移")
        return _format_result(result, action="项目格式已迁移") + f"\n{registry_message}"
    if args.command == "project":
        if args.project_command == "add":
            result = register_project(args.path)
            registry_message = _sync_registry_after_local_success(result.root, action="注册")
            return _format_result(result, action="Workbench 项目已注册") + f"\n{registry_message}"
        if args.project_command == "isolation-check":
            from .native_isolation import require_native_isolation

            report = require_native_isolation(args.path)
            return json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
        project_registry = GlobalProjectRegistry()
        if args.project_command == "list":
            return _format_project_list(project_registry.list())
        if args.project_command == "show":
            return _format_project(project_registry.show(args.project_id))
        if args.project_command == "unregister":
            project = project_registry.unregister(args.project_id)
            return (
                f"已取消全局登记：{project.id}（{project.name}）\n"
                "项目文件、.workspace、Git 与 dashi Task 均未删除。"
            )
    if args.command == "dispatcher":
        execution_root = args.root.expanduser().resolve()
        project_root = discover_project_root(execution_root)
        store = WorkspaceStore(project_root, execution_root=execution_root)
        if args.dispatcher_command == "start":
            return json.dumps(start_dispatcher(store, explicit=True), ensure_ascii=False, indent=2)
        if args.dispatcher_command == "stop":
            return json.dumps(stop_dispatcher(store), ensure_ascii=False, indent=2)
        if args.dispatcher_command == "status":
            return json.dumps(dispatcher_status(store), ensure_ascii=False, indent=2)
        if args.dispatcher_command == "run-once":
            return AutoDispatcher(store, configured_executor(store)).run_once()
    raise AssertionError(f"未处理的命令：{args.command}")


def _load_planning_request(path: Path, requirement_id: str) -> PlanningRequest:
    if path.stat().st_size > 1024 * 1024:
        raise WorkspaceError("计划 JSON 超出 1 MiB 限制")
    request = PlanningRequest.from_dict(json.loads(path.read_text(encoding="utf-8")))
    if request.requirement_id != requirement_id:
        raise WorkspaceError("计划 Requirement ID 与命令目标不一致")
    return request


def main(argv: list[str] | None = None) -> int:
    _configure_standard_streams()
    effective = sys.argv[1:] if argv is None else argv
    if effective == ["hook"]:
        return hook_main()
    try:
        args = build_parser().parse_args(effective)
        if args.command == "dispatcher" and args.dispatcher_command == "serve":
            execution_root = args.root.expanduser().resolve()
            project_root = discover_project_root(execution_root)
            store = WorkspaceStore(project_root, execution_root=execution_root)
            return serve_dispatcher(store, configured_executor(store))
        output = run(args)
    except (OSError, ValueError, ToolInstallerError, UnicodeError, WorkspaceError, GitError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
