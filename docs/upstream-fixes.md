# 给上游项目的修复建议

Janitor 用于修复已经形成的残留。长期正确方案仍是：创建 Codex 对话的平台负责完整执行删除生命周期，并为跨数据库/文件系统失败提供可重试状态。

## Cindy：软删除与物理保留

Cindy `0.1.23` 的本地核验状态是：

- UI 删除后，session 仍以 `status='deleted'` 保留；
- 消息和 Codex 对话 ID 仍可能保留；
- 对应独立 Codex home 中的索引与 rollout 仍存在。

软删除本身可以是产品策略，但应明确：

- 保留期与用户可见的恢复入口；
- 何时清除消息正文；
- 何时调用 Codex `thread/delete`；
- 恢复 tombstone 时，如果 Codex 已经硬删除，应给出不可恢复提示；
- 隐私删除请求是否跳过保留期并执行物理删除。

建议使用带状态的删除作业，例如：

```text
active → deletion_requested → codex_deleted → frontend_purged
                         ↘ retry_required
```

每一步持久化，避免应用崩溃后只完成一半。

## 所有 Codex 前端的通用建议

- 删除前停止对话活动并等待写入结束。
- 使用官方 `thread/delete`，不要只删除前端 DB 行。
- 认识到该接口会同时硬删除由根对话创建的关联任务对话。
- 将对话 ID、Codex 数据目录、前端 owner 与删除请求绑定，避免跨保存位置删除。
- 对幂等重试做测试：rollout 已缺失应仍能收敛到成功状态。
- 删除后验证索引与 active/archived rollout 均不存在。
- 为 schema 升级、崩溃点、磁盘占满、文件被锁和 app-server 超时提供集成测试。
- 提供用户可见的“立即永久删除”与“软删除/保留期”语义，而不是让 UI 的“删除”含义不确定。

官方 [Codex app-server API 概览](https://learn.chatgpt.com/docs/app-server#api-overview) 定义了 `thread/delete`。前端必须按“活动/归档对话以及由该对话创建的关联任务对话都会被硬删除”的真实语义设计确认文案、恢复策略和失败重试。

Janitor 会把前端残留、Codex 对话和候选动作分开建模。清除前端残留引用目前只会结构化显示，不会随 Codex 对话删除自动执行；上游不能把 Janitor 的 `thread/delete` 当作前端数据库生命周期管理的替代品。
