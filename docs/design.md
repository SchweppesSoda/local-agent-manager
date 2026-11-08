# 本地 Agent 管理模型与安全边界

项目在同一入口下管理本地 Agent 的环境证据与记录：环境盘点说明程序、客户端和存储
之间的已知关系；记录清理在已支持的适配器范围内找出失效或用户明确选择的记录，展示
精确影响，永久删除，再确认目标已消失。通用 Windows 磁盘和非 Agent 软件维护独立于
本项目。项目不是长期备份库、rollout 隔离区或自动数据修复器；分类词汇不代表每个
客户端都已实现全套发现和删除。

## Agent 环境与记录的分工

统一管理不等于共用修改授权。实现分为三个部分：

| 部分 | 入口与职责 | 执行边界 |
|---|---|---|
| 环境盘点和诊断 | `environment inventory/doctor`；发现运行程序、版本线索、客户端及存储位置 | 有界、只读的元数据探测，不运行发现的程序、不读取登录凭据或聊天正文 |
| 环境维护 | 仓库维护的 `agent-management` skill；安装升级、调用路径修复、配置迁移和旧运行时处理 | 按实际安装渠道及客户端接口执行，先确定消费者和用户目标，再验证对应功能 |
| 记录清理 | `records`、`delete`、`operation` 与现有 `CleanupService` | 沿用每种存储、记录和 mutation family 的精确协议 |

`environment doctor` 检查环境事实及证据缺口；既有 `agent doctor` 检查记录操作的目标
存储和客户端归属。二者不互相替代。环境清单是诊断输入，不会生成或批准记录删除动作。
安装、升级、卸载和修复没有虚构的通用 `environment apply`；skill 使用当次核实的
官方安装器、包管理器或客户端配置入口，记录操作仍进入既有 operation 协议。

### 环境发现与诊断

运行时、前端与存储分别建模。独立 CLI、桌面内置引擎、Cindy 托管引擎和 T3 ACP
即使产品名称相近，也保留各自的来源。环境报告必须区分：

- 已发现的可执行文件，与有证据的客户端实际选择；
- 路径中声明的版本，与执行程序核验的版本；
- 当前进程 PATH，与客户端后台继承的环境；
- 默认或已存在的数据根候选，与已验证的 native store 绑定；
- 同一物理文件的路径别名，与内容相同但独立的文件；
- 未发现、读取失败、未覆盖和已证明不存在。

默认环境探测不启动程序，因而不会声称已核验程序版本、登录状态、模型列表或真实消息
生成。最高版本目录、文件修改时间、同一账号和存在凭据文件都不足以证明上述事实。
配置只按已支持的格式提取最小白名单字段；未知格式保留不确定性，不倾倒整个配置。
报告使用独立的 `larj.environment-inventory.v1` schema，不修改旧记录计划和审批指纹。

T3 的 ACP 协议家族与底层程序分开：自定义 `agy acp` 的绑定仍属 `antigravity-acp`，
可执行文件候选属于 `antigravity-cli`，配置追加 `launch_mode_hint=cli_acp`。
该线索不把 CLI 绑定到旧的 T3 哈希实例目录；原生根保持未知，原生清理状态为
`unsupported_cli_acp`。受管双文件运行时及历史实例目录继续独立盘点。

Cindy 的运行时发现同时识别旧 `codex/<version>/codex` 布局与
`codex-package/<version>/bin/codex` 托管包布局；Windows 使用对应 `.exe` 文件。
自动发现仅提供运行候选，显式程序路径保持优先。候选选择不授予清理旧版本的权限。

### Windows 启动环境与 PATH 诊断

CLI 已安装且 Windows 用户 PATH 已注册，仍可能无法被某个客户端启动。常驻启动器
保留旧环境时，其后启动的客户端和服务端可能继续继承旧 PATH。T3 由 uTools 启动的
排障案例说明：当前 Codex 任务内 `Get-Command codex` 成功，不证明 T3 的命令解析成功。
父子关系也不能单独证明环境相同，启动器可以显式提供不同的环境块。
[Windows 环境继承说明](https://learn.microsoft.com/en-us/windows/win32/procthread/environment-variables)

诊断分别记录安装来源、持久用户/系统 PATH、当前检查进程环境、目标启动器/客户端/
服务端环境，以及 frontend 显式命令覆盖。缺失安装证据、命令未注册、进程环境过期或
被覆盖、显式 `binaryPath` 和独立/桌面捆绑 CLI 选择差异，不能合并为“CLI 未安装”。
目标进程不可读时保留未知；使用 PID、启动时间和执行路径限定一次进程观察。

当前 `environment inventory/doctor` 只读取调用进程或显式传入的 PATH，不自动读取
持久 PATH、其他进程环境或继承链。Python API 的 `process_executables` 也仅表示外部
提供的执行路径观察。上述进一步诊断与刷新由
[Agent 技能操作流程](../skills/agent-management/references/runtime-and-migration.md#windows-path-inheritance)
完成，不能声称 CLI 已实现自动过期环境检测。

刷新范围覆盖实际启动链。先检查进行中的任务，优先正常退出；窗口关闭不等于托盘
启动器退出。用目标用户的已验证环境重新启动后，检查新进程的实际选择。临时绝对路径
覆盖与用户要求的最终配置分开；目标为恢复默认命令时，验证后撤回临时覆盖，同时保留
原来的数据根和账号模式。命令解析、Provider 连接、认证、模型发现与实际生成分层验收。
本机 PID、版本、模型数量和用户名路径属于当次观察，不写成永久匹配规则。

### 环境维护工作流

安装或升级先确定安装来源、实际消费者、稳定命令入口以及现有数据根；卸载引擎前检查
仍依赖它的客户端。文件内容相同或全局已有一份引擎，不构成移除客户端托管副本的理由。
升级验证分层报告：安装完成、可执行、客户端握手、模型发现、实际任务，各层分别给出
证据，不能以 `--version` 成功代替最终使用验证。

配置和数据迁移按任务决定保留策略，验证实际启动路径和生效配置后再处理原位置。
可重建缓存与明确废弃残留可按用户授权直接删除；共享配置或数据库写入所需的临时回滚
材料不等于长期备份。已有授权在相同范围内持续有效，改变产品、目标、数据保留或迁移
策略时才需要新的选择。操作系统的提权提示与聊天中的任务授权分别说明。

运行时卸载、解包缓存清理、旧实例整体弃用和逐条记录删除分别定义范围与完成条件。
例如 T3 改用 `agy acp` 后，CLI 仍以 ACP 协议服务；卸载旧 server/helper 不等于旧实例
的凭据、设置与历史记录已经清理。整体弃用须先确认实际消费者、活动数据根及用户选定
的完整范围。实例内指向共享 skills 的链接与目标分别核对，不能递归穿过链接清理共享目录。
当前项目没有通用 profile-retirement 命令；单次任务脚本和历史清理结果不构成通用能力。
具体范围与验证方法见 [旧实例数据检查](../skills/agent-management/references/antigravity.md#distinguish-uninstall-results-from-discarded-instance-data)。

### 多主机与验证

代码和 skill 可以部署到多台主机；安装路径、环境变量、消费者、凭据及清理计划不在
主机间复用。发现器可注入用户目录、AppData、PATH 和客户端选择，使测试只触及临时
目录。每台主机分别发现、分别验证；环境报告不是远程执行协议。

验收覆盖新旧运行时布局并存、显式路径优先、目录链接和硬链接、读取错误、扫描预算、
缺失运行时、未知绑定、含秘密的配置和隔离用户目录。CLI 路由必须在加载记录适配器前
进入环境入口，环境诊断不扫描会话正文。skill 另用模拟请求检查路由和操作范围；真实
软件卸载、用户记录清理和真实凭据均不作为开发测试。

## 统一执行架构

```text
Driver
  → StoreSnapshot
  → Planner
  → AuthorizedAction
  → Guard
  → Executor
  → Verifier
```

人类入口 `scan/records/delete/clean/purge` 和 Agent 入口
`agent doctor/plan/apply/status/verify` 都调用同一个 `CleanupService`。CLI 负责参数、
确认和输出，不拥有另一套扫描或删除规则。

`gui` 是 Codex `records/delete` 的 Tk 人工界面：它把统一 inventory 和
`ManualDeletePlan` 投影成可搜索、逐项勾选的列表，并复用相同的计划指纹、客户端关闭
确认、执行前重验证和删除后验证。GUI 不提供全选。一次明确勾选和确认会额外绑定范围内
精确 Codex Desktop 快照，并顺序编排 native thread 删除与 Desktop 状态清理；两者仍是
独立物理修改批次，各自执行客户端归属检查、回滚保护和写后验证，不组成伪事务。
Cindy frontend mutation family 不在该组合授权内，仍明确保留并单独处理。

核心类型分工：

- `StorageRef`：一个精确物理存储；
- `RecordRef`：该存储中的一条逻辑记录；
- `Evidence`：不含聊天正文的身份、状态、schema 和指纹证据；
- `Action`：稳定 action ID、目标、影响与 mutation family；
- `GuardToken`：执行前重验证所得的不可变授权绑定；
- `Result`：结构化状态、计数、blocker code 与验证结果。

兼容 facade 继续输出既有 JSON 字段和 `delete_conversation`、
`repair_legacy_index` 等 action kind。自由文本只用于显示，不能参与授权决定。

## 身份与物理边界

```text
Frontend namespace -- reference/mapping --> Native record
Harness runtime -- operates on --> Native store -- contains --> Native record
```

Codex thread 的身份是 `(CODEX_HOME, thread_id)`；Pi session 和 Claude session
还绑定各自的 session/config root。相同 ID 出现在不同物理存储时必须拆成不同目标。
认证来源、账号、登录状态和前端 owner 只属于诊断信息，不能替代物理存储身份。

前端显示 ID 不承担绑定身份：当前引用与历史切换可能来自同一 UI 会话、指向不同原生
记录；不同数据库也可能复用相同 UI ID。清单、筛选和分组都以数据库、引擎、原生目标
及历史 boundary 限定绑定。读取失败与逐记录 blockers 在每次投影中保留，不能把底层
不可删除记录显示为可清理，也不能把失败清单当成“没有残留”。

原生 Codex/Pi/Claude 的正常独立记录不要求 Cindy 或 Desktop 引用。第三方客户端范围
中的 `orphan_native` 表示已验证存储中的无绑定根或确认缺失父的 Codex 子记录，仍须
明确选择和删除保护；未知引擎输出 `unverified`，只提供盘点。未验证原生归属也输出
`unverified`，不能授予 native 删除能力；已验证的独立前端 writer 仍遵循自己的精确契约。
分类、引用状态和删除能力是独立判断。

Codex 的 native parent、Pi 的分支来源和 Claude manifest 下的 subagent 文件具有不同
删除语义，不能统一为“父子全删”。具体关系与新客户端接入要求见
[Adapter 贡献指南](adapters.md)。

`native`/`codex-native` 仅表示官方 Codex 本地存储，以及可探测的 Codex/ChatGPT
Desktop 编码环境本地状态。ChatGPT 云端聊天、云端项目和远程记录不在本工具范围内；
`chatgpt` 不作为本地 Codex 客户端别名。

一个顶层 operation 可以预先冻结多个子批次；每个子批次只处理一个物理存储和一种
mutation family，跨批次不构成事务。新发现的动作须重新生成计划，不会被吸收到旧
授权中。底层 Agent 单批次计划仍遵循单 store、单 mutation family 的边界。

## 快照与计划

一次完整快照按所选适配器的实际能力聚合可用证据，可能包括：

- Codex SQLite 列表、活动/归档 rollout 首行 metadata 和旧索引；
- 精确关系边与完整关联任务范围；
- Codex Desktop 可探测的 local catalog/UI 结构化引用；
- Cindy 当前与历史 frontend reference；
- Pi/Claude storage-qualified session manifest；
- 读取失败、进程归属和 identity conflict。

扫描只读取判定和显示所需的最小结构化字段，不读取或输出聊天正文。Planner 为每个
action 生成完整目标、影响范围、schema/行/文件指纹、稳定 ID 和 blocker code。
活动引用、读取失败、身份冲突或无法唯一定位只阻止受影响目标。

## 问题与动作

| 问题 | 当前动作 |
|---|---|
| 前端已删除、底层会话仍在 | 删除整条底层会话；前端残留作为下一独立动作 |
| index-only、rollout-only | 删除整条 Codex thread |
| 重复 rollout、路径错位 | 保留，或删除整条 thread 及全部已确认副本 |
| 孤立关联任务 thread | 展示完整级联范围后删除 |
| 只剩无效关系边 | 精确删除一条已批准关系行 |
| 只剩旧索引/Desktop 状态 | 精确清除残留 |
| 只剩 Cindy 映射 | 精确清除该映射；Cindy `status=deleted` 会话可作为独立批次物理删除 |
| Pi/Claude session | 删除精确文件或 manifest |
| 同一 ID 位于多个存储 | 每个物理存储分别选择 |
| 活动引用、读取失败、身份冲突 | 阻止该目标并返回稳定原因 |

`repair_index_path` 和 `quarantine_artifacts` 不再生成或提供。对应枚举只为读取旧
JSON 保留；当前决定只有“保留”或“删除整条已验证记录”。

## 精确写入器

Orca 的本机 journal schema4 / record schema2 提供清单和保护证据，品牌静态能力默认
关闭。候选 adapter 与保护 adapter 分开；保护来源不能扩大被选客户端的 catalog、目标
或批准范围。普通保护来源由顶层 operation v2 冻结已知 profile locator，执行前恢复并
合并当前来源。限定的 Windows/Codex managed-account-home 原生删除采用顶层 v3，按
目标冻结并复查 runtime/配置/副作用证据，复用 native writer；startup 子批次采用
child v2 的 root-wide 协调和持久状态。未受影响的旧 writer 及 v1/v2 证据保持原 hash，
旧 unknown 只读恢复，不补授权或重发 mutation。其他组合不继承删除资格，见
[限定组合](adapters.md#orca精确原生删除的限定组合)与
[协议版本](operation-cli.md)。

Herdr 的持久化 snapshot schema3 只提供 current/restore 引用。来源不含 native root，
因此不自动加入原生 catalog、父子关系或 mutation 范围；同 ID 不用于跨 store 绑定。
明确选择 Herdr 的 records 可用 `--inspect-clients` 附加有界本机 protocol22 metadata，
与持久化 current/restore 独立保留；公共 pane ID 不充当持久化 identity，不据相同引用
值证明同代实例或全部 writer 已停止。API 和运行投影复用同次缓存，后者不参与旧审批。
完整读取请求范围内的已支持 metadata 时，records 可返回 complete；writer 归属未知
仅保留删除限制，不把成功的只读盘点标为失败。读取、schema、transport 或引用冲突
仍使清单 incomplete/blocked，并保留已观察到的有效引用。
全部 Herdr 写入与自身 verify 关闭；无授权动作的只读计划保持 blocked，旧 native
operation 仍可按冻结证据执行只读恢复。

### Codex thread

原生 thread 删除调用匹配目标 `CODEX_HOME` 的官方 `thread/delete`。同一批只启动
一个 app-server。若官方 API 已删除 thread 但留下已批准 rollout，只有在路径位于目标
store、是唯一链接的普通文件、thread ID 与完整指纹仍匹配且已无 index/活动引用时，
才能作为同一动作的残留清除。任何未知残留使结果成为 `partial` 或 `unknown`。

### Cindy

- 软删除会话：只接受 `sessions.status='deleted'`，按一个数据库冻结全部精确行和依赖
  证据，并在单个事务中集合删除会话、消息、FTS、embedding/vector 及已支持依赖；
- `active`、`archived`、schema 漂移或无法证明归属的行保持不变；
- 当前引用：只把目标 session 的 `sdk_session_id` 清为 `NULL`；
- 历史引用：只从绑定消息 ID、消息行指纹和原始内容哈希的结构化
  `agent_switch` JSON 中移除 `fromSdkSessionId`。

历史写入保留消息行和其他 JSON 字段；重复 key、内容漂移或字段值不符都会阻止。

### Codex 关系边

只删除 parent、child、status、schema fingerprint 与完整 row fingerprint 全部匹配的
一条 `thread_spawn_edges` 行。open edge、重复身份行、未知 schema 或不完整证据都
fail closed。

### 旧索引与 Desktop 状态

旧索引只移除已证明没有 live thread 的原始整行。Desktop 写入只删除
`host_id='local'` 的精确 catalog 行和结构化精确 ID 引用；正文中的相同字符串不参与
匹配。

所有共享 SQLite/JSON 写入都要求：客户端关闭、schema 白名单、事务或原子替换、
影响范围等于预期、写后重新读取，以及临时回滚副本。

## 客户端归属

`ClientInspector` 由服务注入，默认单元测试不读取真实宿主进程。生产检查综合可执行
文件、父子进程和物理 store 归属。已证明属于官方 Codex 的进程不阻止独立 Cindy
store；Cindy 进程也不阻止已证明独立的官方 store。无法证明归属时才 fail closed。

## 执行、验证与性能

默认 planner 对每个 store 做一次 catalog pass；apply 的终验取决于路径。healthy/native
记录的已测合同是计划一次、终验一次两次 full catalog pass。N 个 action 之间只重查本
action 涉及的数据库行、文件、活动引用和关联范围：

```text
一次完整扫描 + N 次定点检查 + 一次完整终检
```

中途漂移立即停止剩余动作，不在每个 action 前重扫全库，也不重建完整 catalog/plan/
frontend。性能测试直接统计 full-scan 调用次数；两次 pass 是 healthy/native 路径的
实测结果，不对 stale/broken 等 anomaly scan 路径作统一承诺。

执行结果采用 `deleted/not_deleted/partial/unknown` 或 Agent
`complete/completed_with_residuals/blocked/unknown`。API 成功响应不能代替磁盘、
数据库和完整目标终检。

## 回滚副本与 operation 数据

Codex/Pi/Claude 会话和独立 rollout 永久删除，不创建备份。共享 SQLite、旧索引或
Desktop JSON 写入前创建临时回滚副本：

- 写入并验证成功：立即删除；
- 自动回滚成功：立即删除；
- partial、unknown 或回滚失败：暂时保留，直到 verify 收口。

执行中或结果未知时保留详细 operation journal。已知终态压缩为不含正文的最小
`receipt.json`，只记录 plan hash、action 状态、blocker code、计数和时间；最长
七天后自动清除。回执不是备份，项目不提供 recover 命令。

## 关键不变量

- 所有计划、回执和输出都不含聊天正文；
- 不跨物理存储合并目标；
- schema、身份、范围或客户端归属不确定时只阻止相关目标；
- `--yes` 不能代替目标选择、客户端关闭或当前计划指纹；
- 同一动作在结果不明时不得重复发送；
- 删除成功必须由重新读取和完整终检证明；
- 不以节省空间为理由降低证据门槛。
