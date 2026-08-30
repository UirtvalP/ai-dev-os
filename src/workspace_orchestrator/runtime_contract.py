"""随全局安装包更新、由 Hook 动态注入的当前运行时契约。"""

from __future__ import annotations

from . import __version__


def hook_context(snapshot: str) -> str:
    """将当前版本契约与项目 Context Snapshot 组合。"""

    contract = f"""# AI Dev OS 运行时契约

全局 CLI 版本：{__version__}

- 本契约由当前全局 `ai-dev-os hook` 动态提供，优先采用这里的运行时流程。
- 将下方 Context Snapshot 视为 Requirement、Task、Git 状态和下一步行动的事实来源。
- 必须读取用户级 `~/.ai-dev-os/USER_PRINCIPLES.md`、项目级 `PROJECT_INTENT.md` 与当前需求 `intent.md`。
- 不重复执行 Hook 已完成的 Session、Requirement、Task、dashi 或 Git 自动化步骤。
- 明确“新增/新建/创建需求”时由 Runtime 幂等创建并接入，不要再请求二次确认。
- 语义工作完成后只调用一次 `workspace finalize REQ-ID`；默认严谨验证通过后自动完成，明确人工测试时才进入人工 Review，其余收尾交给 Automation Runtime。
"""
    return f"{contract.rstrip()}\n\n{snapshot.lstrip()}"
