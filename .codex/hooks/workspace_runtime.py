"""源码仓库兼容入口；正式 Hook 使用已安装的 ``ai-dev-os hook``。"""

from workspace_orchestrator.hook_runtime import main

if __name__ == "__main__":
    raise SystemExit(main())
