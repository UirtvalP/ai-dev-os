# P5 Routing Policy 迁移说明

- 旧 `CapabilityModelRouter` API 保持可用。
- Runtime 能力校验现在同时接受 P1 的 canonical capability 与旧别名。
- 新调用方应提交 `ExecutionRecommendation`，并使用 `ExecutionRoutingPolicy.decide()` 获取最终结果。
- `workbench route` 是可见 vertical slice，只展示裁决，不创建 Execution 或修改 Requirement 状态。
- 不可用的软建议会在 `recommendation_accepted=false` 中显式呈现；显式 Task 偏好仍失败关闭。
