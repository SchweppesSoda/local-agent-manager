# Local Agent Manager 整合设计

项目统一维护环境、Skill 和记录管理的通用代码。`agent-management` 与 `skill-management` 是两个操作入口，业务技能库按独立 source 配置接入。

## 模块与源边界

`local_agent_manager` 保存环境和记录的原模块结构。`skills/core.py` 保存挂载事务，`skills/clients/` 保存客户端模型与原生证据逻辑，`providers/t3.py` 提供共享的严格解析与字段投影。环境线索、运行身份、原生加载证据和记录删除授权分别判断。

Skill 命令显式接受 `--repo-root`。只给配置时，按配置位置展开 `repo.root`。两种声明矛盾时拒绝。安装目录不作为用户技能源。源的 `sourceAudit.sharedResourceReferences` 声明精确的源内跨 Skill 引用。通用配置只提供静态策略。工作区的桌面资源与客户端资源路径约束分别由 `sourceAudit.forbidDesktopResourcePaths` 和 `sourceAudit.forbidClientResourcePaths` 显式启用。可信私人 wrapper 可显式组合独立运行时审核，并负责本源的业务依赖和自动化授权。

## 兼容与持久状态

发行包、主命令和模块采用新名字，旧 CLI 和模块保留薄兼容入口。旧业务子模块转发到同一对象，旧路径的 patch 修改实际实现。根包和 adapters 包保留真实兼容入口，已有可执行模块通过新进程执行相应实现。

历史协议 `larj.*`、变量 `LARJ_*`、Skill 源状态键和恢复 journal 保持原样。记录控制目录可显式迁移，永久锁保留原位置，迁移标记阻止旧版本写入，详见 [状态维护](state-maintenance.md)。新的实现摘要覆盖 Skill 执行代码、相关资源和共享 provider 解析。旧计划重新生成，原生证据按原 source、adapter、policy 与 binding 继续判断。代码迁移不写入新的 verified 结论。

## 多源协调与恢复

共享目标根和 discovery manifest 使用按规范物理路径排序的跨源锁。apply 与 clients verify 的回读和落盘阶段均使用这些锁。manifest 新增本源 entry 时保留他源 entries、重复他源记录及未知字段。本源重复 entry 阻断，无变化不写。实际 discovery 范围内的同名 Skill 阻断。

替换前将原始字节前像和预期后像摘要写入 journal。异常恢复只在当前内容仍为本次后像时回写。外部修改保留现场和恢复材料供审阅。恢复能力包括异常回滚与进程中断后的人工恢复。

共享清单改变使引用它的 adapter 绑定证据失效。完成最终两源清单后，对受影响 adapter 验证两源代表性 Skill，并各自记录新证据。原有未变化的其他客户端证据按原语义判断。单个 Skill 删除不代表注销整个技能源。

## 安装与资源

发行包需要 Python 3.10 及以上、PyYAML 和 jsonschema。业务包由源仓库管理。顶层 `skills/` 与 `docs/` 保持权威，构建钩子依据 `resource-manifest.json` 在干净构建目录生成安装资源。sdist 和 wheel 均包含资源及引用闭包。运行时定位器在 editable/source 模式读取权威源，在 wheel 模式读取安装资源。

标准库 probe 按资源绝对路径执行，从相邻标准库模块加载证据逻辑。被检测解释器无需安装管理器。wheel-only 验收使用任意工作目录与隔离安装，另比较直接 wheel 和 sdist 所建 wheel 的资源路径与内容摘要。

## 迁移顺序与验收

先保全源仓库现有修改，验证新旧模块身份和历史 operation 契约。通用管理器及两个完整 Skill 经隔离安装验收后更新本机入口，再注册新 source 并核验加载。旧 Skill 名由业务仓库保留薄路由。

本地目录与兼容入口按实际占用情况处理。目录被活动进程占用时，可先提供新名称的路径入口，并更新 editable 安装元数据。源身份按实际解析的物理根确定。后续物理根改变需要重新审阅归属登记和原生验收，保留旧 journal，不改写历史状态身份。GitHub 仓库名和发布状态按实际外部授权处理。自动任务仍使用原目标集、退出码和 create-only 限制。

回归覆盖历史记录契约、新旧导入和命令、源解析、静态策略、双源 manifest、并发锁、条件回滚、T3 格式与启用状态、证据漂移、独立 probe 和安装资源。
