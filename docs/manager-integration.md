# Local Agent Manager 整合设计

项目供本人少数几台 PC 使用，是个人本地管理工具，非商业或面向公众的产品。项目统一维护环境、Skill 和记录管理的通用代码。`agent-management` 负责环境与记录，`agent-auth-management` 负责已有登录授权的定位、复用与验证，`skill-management` 负责 Skill 生命周期。Skill 管理面向个人、项目、共享和包管理的技能源，各源按自身归属与更新方式维护。

`agent-auth-management` 以本机单账号和 Codex 为主要场景，按原生凭据后端选择已共享授权的验证、兼容凭据复制、明确的数据目录共用或官方重新登录。多账号沿用客户端原生能力，不构建账号池或 token 同步服务。运行程序、数据目录、凭据来源、登录状态与历史归属独立核验；Skill 不新增通用认证命令，也不将开发验收扩展为真实凭据迁移。

`skill-management` 负责 Skill 的创建、内容与路由维护、资源检查，以及跨源导入、客户端发现、部署、同步、验证和恢复。先判断任务和权威源，再选择内容维护或部署流程。内容维护可直接处理独立 Skill；需要管理器执行部署事务时才使用源配置与计划协议。`work-skills` 是一个接入源，旧 `mount-work-skills` 仅保留该源的兼容路由；其业务路径、运行时、wrapper 和自动化授权不成为通用管理默认值。

## 模块与源边界

`local_agent_manager` 保存环境和记录的原模块结构。`skills/core.py` 保存挂载事务，`skills/clients/` 保存客户端模型与原生证据逻辑，`providers/t3.py` 提供共享的严格解析与字段投影。环境线索、运行身份、原生加载证据和记录删除授权分别判断。

Skill 命令显式接受 `--repo-root`。只给配置时，按配置位置展开 `repo.root`。两种声明矛盾时拒绝。安装目录不作为用户技能源。源的 `sourceAudit.sharedResourceReferences` 声明精确的源内跨 Skill 引用。通用配置只提供静态策略。工作区的桌面资源与客户端资源路径约束分别由 `sourceAudit.forbidDesktopResourcePaths` 和 `sourceAudit.forbidClientResourcePaths` 显式启用。可信私人 wrapper 可显式组合独立运行时审核，并负责本源的业务依赖和自动化授权。

## 当前入口与持久状态

发行包、主命令和模块分别为 `local-agent-manager`、`local-agent-manager` 和 `local_agent_manager`，旧项目 CLI/package 别名已移除。

现行协议 `larj.*`、运行时变量 `LARJ_*`、Skill 源状态键和恢复 journal 保持原样。每台 PC 以一个 Python 安装登记一个权威 editable 仓库，记录操作的锁、日志、计划和回执固定归该仓库 `.agent-manager-state`；多个源码 checkout 不各自创建协调域。当前 store key 和 journal 布局不变，不迁移现用证据。旧状态路由、迁移 marker 和旧目录恢复入口已移除，历史文件不自动迁删，详见[状态维护](state-maintenance.md)。

新的实现摘要覆盖 Skill 执行代码、相关资源和共享 provider 解析。受影响旧计划重新生成，原生证据按原 source、adapter、policy 与 binding 继续判断。代码维护不写入新的 verified 结论。

## 取舍与数据安全

按实际个人使用需求做最小修复，采用当前命令和路由，移除旧命令及旧路由兼容，不为假想公众部署增设通用注册或迁移框架。移除兼容入口不授权自动迁移、删除已有真实记录或恢复证据。

冻结审批范围、物理 store 身份、原生删除确认后再清理前端引用、原子写入和中断恢复仍是必要约束。原生批次中成功与失败混合时，保守保留前端引用是可接受的取舍，不为释放部分引用放宽整批完成门控。重试或恢复被新证据阻挡时，报告继续保留已发生的累计修改、写入开始状态和已完成批次；本次没有继续写入不代表此前没有修改。

## 多源协调与恢复

共享目标根和 discovery manifest 使用按规范物理路径排序的跨源锁。apply 与 clients verify 的回读和落盘阶段均使用这些锁。manifest 新增本源 entry 时保留他源 entries、重复他源记录及未知字段。本源重复 entry 阻断，无变化不写。实际 discovery 范围内的同名 Skill 阻断。

替换前将原始字节前像和预期后像摘要写入 journal。异常恢复只在当前内容仍为本次后像时回写。外部修改保留现场和恢复材料供审阅。恢复能力包括异常回滚与进程中断后的人工恢复。

共享清单改变使引用它的 adapter 绑定证据失效。完成最终两源清单后，对受影响 adapter 验证两源代表性 Skill，并各自记录新证据。原有未变化的其他客户端证据按原语义判断。单个 Skill 删除不代表注销整个技能源。

## 安装与资源

发行包需要 Python 3.10 及以上、PyYAML 和 jsonschema。业务包由源仓库管理。顶层 `skills/` 与 `docs/` 保持权威，构建钩子依据 `resource-manifest.json` 在干净构建目录生成安装资源。sdist 和 wheel 均包含资源及引用闭包。运行时定位器在 editable/source 模式读取权威源，在 wheel 模式读取安装资源。

标准库 probe 按资源绝对路径执行，从相邻标准库模块加载证据逻辑。被检测解释器无需安装管理器。wheel-only 验收使用任意工作目录与隔离安装，另比较直接 wheel 和 sdist 所建 wheel 的资源路径与内容摘要。

## 本地维护与验收

保全源仓库现有修改，验证当前入口、现行 operation 契约和隔离测试。源码调整不自动修改本机运行环境；经授权刷新本项目 editable 登记时保持解释器和第三方依赖不变。物理根改变需要单独审阅现有状态与归属登记，保留原 journal，不改写其身份。GitHub 仓库名和发布状态按实际外部授权处理。自动任务仍使用原目标集、退出码和 create-only 限制。

回归覆盖当前导入和命令、权威状态定位、冻结依赖、源解析、静态策略、双源 manifest、并发锁、条件回滚、T3 格式与启用状态、证据漂移、独立 probe 和安装资源。

测试数据隔离与可选依赖校验分开：bootstrap 只保留数据路径隔离前的已配置或已存在运行时位置，真正依赖该组件的测试或 helper 才校验。坏可选依赖只阻断相关测试；未配置可跳过，显式配置错误、已存在但损坏的运行时及 CI 强制依赖要求继续失败，不全局替换生产状态选择器或另设生产 authority。
