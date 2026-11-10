# Operation CLI contract

The operation CLI is the multi-project, body-free interface for local record
cleanup. It is a thin dispatcher: scanning, immutable planning, child-batch
partitioning, journal recovery, mutation, and verification belong to the
`OperationCoordinator`, which delegates storage work to `CleanupService`. The
CLI does not maintain a second scanner or mutation implementation. The
implemented deletion paths currently cover healthy/native records with a
frozen frontend closure, Cindy Pi/Claude sessions, exact supported local WorkBuddy and Office sessions, and Cindy frontend sessions
whose exact row status is `deleted`.

## Commands

Plans containing retired `aionui` or `qoderwork` client identities are rejected
before storage guards, recovery or mutation, including native-scoped plans with
retired frontend references. Their old command-line selectors and path options
are no longer accepted. Supported clients require a fresh plan for their own scope.

The `native` client also supports exact stale local-environment registrations
through the independent `delete_native_project` family. See
[the supported schema and recovery contract](native-project-cleanup.md).

Use one client per operation. A plan/run selects exactly one scope mode:

```text
records --client <native|codex-native|cindy|pi|claude|orca|herdr|workbuddy|qwenwork|paseo|t3code> [--project <selector> ...]
delete plan --client <client> (--project <selector> ... | --all-projects | --record-id <id> ...)
delete apply --operation-id <id> [--plan <plan.json>] [--clients-closed]
delete run --client <client> (--project <selector> ... | --all-projects | --record-id <id> ...)
operation status --operation-id <id> [--operation-home <path>] [--plan <plan.json>]
operation verify --operation-id <id> [--operation-home <path>] [--plan <plan.json>] [--verify-timeout <seconds>] [--progress]
```

Add `--progress` to `records`, `delete plan/apply/run`, or
`operation status/verify` when a live diagnostic stream is useful. The option
writes metadata-only JSONL events to stderr as inventory, planning, child-batch
execution, and verification phases start and report completion, failure, or a
recovery boundary. Events include the phase, status, elapsed seconds, and
bounded counts; they never include message bodies or alter the one-document
JSON result on stdout. It is opt-in and has no effect on the default output
contract. During execution, verified successful actions advance
`counts.completed_action_count` toward `counts.total_action_count`; failed,
partial, or unknown checks do not count as completed actions. These are phase
and action events, not a periodic heartbeat during a blocked call. Always use
the final operation result to determine whether the whole goal is complete.

`--engine` may further limit a client scope. A project selector is resolved by
the core using a normalized working directory, authoritative project ID, or a
unique project name. Same-name projects at different paths remain ambiguous;
records without project evidence are not absorbed by name.

When `delete plan` receives `--out PATH`, pass that exact file as `delete apply
--plan PATH` and as `operation status/verify --plan PATH`. The plan path is
optional only when the coordinator can resolve the operation's default state
location by operation ID. `operation_home` and `codex_home` are state-root
selectors for status/verify; they are not plan files and must not be substituted
for `--plan`.

`delete plan` creates one immutable top-level operation. Its child batches are
the write boundary: each child targets one physical store and one mutation
family. The core freezes the target/reference closure supported by the
selected adapters before mutation and uses targeted guards while applying.
Terminal verification is path-dependent; for healthy/native records the
measured contract is one planner catalog pass plus one terminal catalog pass.
The default planner performs one pass per store and the action loop does not
rebuild a complete catalog/plan/frontend. Anomaly adapters can require
source-specific reads, so this two-pass measurement is not a promise for every
classification. A successful `delete run` performs the same plan/apply
sequence without pausing for an interactive review. Within an unchanged
authorized scope, the Agent may proceed from plan to apply without asking for
a second confirmation.

The operation store owns the operation ID and plan hash binding. `apply` must
not infer a changed plan or resend an ambiguous native request. If a mutation
is `unknown`, stop and use `operation status` followed by `operation verify`;
never invoke `delete apply` again for that operation.

Before dispatch creates a child journal, cold status reports that child as
`pending` and the aggregate as `ready`; this describes journal admission, not
proof that the last apply's external blocker is resolved. A partially created
journal, an existing apply lock, or untrusted/started state remains `unknown`.
Verification never creates journals for unstarted children. A blocked-before-write
apply may therefore have pending children and a read-only residual verdict.

`records --client orca [--orca-root PATH ...]` reads the supported local journal
and proven native homes. Orca's static capabilities remain closed. The high-level
operation coordinator may qualify an explicitly selected native Codex target
through the [fixed Windows combination](adapters.md#orca精确原生删除的限定组合).
Other combinations return structured blockers. A plan without authorized actions
remains blocked; an empty executable catalog never proves deletion.

Plans that discover Orca protection profiles use `larj.operation-plan.v2`.
The required `guard_sources` list contains each canonical local profile root,
client, host and path namespace, and is included in the approval hash. These
locators are separate from mutation storages and never create a journal or lock
under an Orca profile. Apply, residual run rounds and verify restore frozen
profiles even when the caller omits or changes `--orca-root`. They also retain
current default/env and explicitly supplied protections; those guards do not
expand the selected catalog or approved targets. A required source that fails
to read cannot become an empty guard set. Store-qualified failures affect the
matching approved store; unscoped coverage failure of a required profile blocks
dispatch. Only bounded product metadata is refreshed, without another native
planner pass.

A guard-only Orca profile with no journal may qualify a narrower read-only
absence proof. It requires a valid profile index, every indexed profile's v3
database, all registered JSON/SQLite recovery copies, and empty session-bearing
state in profiles, legacy records and hook namespaces. Only the supported plain
terminal layout is accepted. Unknown domains, bindings, restore files, schema
changes, unreadable sources or changes between observations keep the original
blockers. The proof is rebuilt for each required-source check, including apply
and verify; it never grants Orca mutation capabilities or substitutes for
`--clients-closed`. Missing journals without that positive evidence stay blocked.

Qualified Orca native deletion uses `larj.operation-plan.v3`. Its target safety
evidence freezes one managed account home, each complete native cascade, ordinary
rollout/SQLite/index identities and link counts, required sources, bounded
configuration hashes, opaque credential identities/hashes, approved startup
artifacts, and the fixed binary/invocation policy. Target evidence and runtime
policy v2 qualify configured homes under the exact [registered boundary](adapters.md#orca精确原生删除的限定组合).
Unregistered optional databases, compressed rollouts, a pre-existing fixed index
replacement file, or nonempty native logs block startup. Apply rechecks current references
and target-scoped Windows process metadata and still requires a real
`--clients-closed` acknowledgement. Plan-time running observations may become
closed before apply. Only the exact approved action/home/IDs/paths receive an
internal execution ticket; direct/manual/GUI/legacy calls cannot mint it.

An exact local structured-chat selection may also include `delete_orca_frontend`.
Target evidence v3 binds the native cascade to the complete approved frontend
closure. Native children retain child-plan v2 startup handling; the frontend uses
child-plan v1 and depends on verified native child receipts. All associated native
homes participate in root admission, including a home whose records are already
absent. Frontend-only plans remain top-level v2. Cold continuation preserves the
original closure, timestamp and dependencies; a started ambiguous frontend is
never dispatched again. Read-only verification checks every approved JSON/SQLite
after-state and current recovery-source membership before issuing a terminal receipt.

The corresponding `larj.child-operation-plan.v2` records root-wide coordination
for app-server startup. Before any startup, the durable mutation marker includes
the original named Job and machine/Windows session identity. The root mutex stays
held through Job teardown and post-close verification. A startup/close failure
keeps the whole batch unknown, even if an earlier request returned success. An
unknown child blocks another target in that same home, since startup touches shared
SQLite families; it does not enlarge the approved deletion closure.

Read-only recovery never starts a binary and does not require today's write
capability or invocation-policy hash to match. It checks the frozen storage and
sources, the original Job's absence on the same machine and Windows session, and
the original native IDs/rollouts/rows/edges/sidebar index directly. A terminal
child receipt retains that original runtime evidence. An existing Job, missing
runtime proof, bad source, linked survivor or incomplete read keeps the outcome
unknown. Software upgrades cannot add evidence or change the original plan hash.
Legacy target evidence v1 remains readable for recovery, but cannot authorize a
new v2 invocation; an unstarted operation needs a newly reviewed plan. Existing
startup objects are checked according to the stored evidence version.
Recovery inventories only the frozen native home; an ambient `CODEX_HOME` or an
extra native adapter does not add a store to that inventory. Frozen and current
protection sources still participate in guard checks, including source failures.

Existing v1 plans keep their original fields, bytes and hash. If an unattempted
v1 operation now requires these new protection locators, apply returns
`missing_guard_source_evidence` and requires a new top-level plan. Started or
ambiguous child journals remain `unknown` before any such gate; their native
status/verify diagnosis remains available without resending mutation. A new
adapter's `verify=false` does not revoke that legacy read-only diagnosis.
Existing v2 plans likewise receive no v3 evidence. An affected unstarted mutation
must be replanned; an unknown operation must be recovered before any new mutation.

If an older top-level plan is missing, `operation status/verify` also accepts
the exact existing child operation ID and its native `--codex-home` (or the
original journal `plan.json` via `--plan`). Standalone recovery is limited to
self-contained `larj.child-operation-plan.v1` / `delete_conversation` journals.
It validates the original child hash, store binding, checkpoint and event
sequence, holds the store and operation locks, and reads the complete native
catalog plus frozen artifacts and Desktop references. Surviving records produce
`completed_with_residuals`; incomplete evidence stays `unknown`. Recovery never
starts an app-server, repeats deletion, reconstructs a parent plan, or expands
authorization. The original child plan and journal are retained. A fresh plan
within the user's existing scope is required for remaining records. Other
families, mixed frontend evidence and Orca startup boundaries still require
their original top-level plan.

Herdr is inventory-only without an explicit binding manifest. By default, `records --client herdr
[--herdr-root PATH ...]` reads snapshot schema3 current and recognized recovery
files across default/named sessions. Explicit roots replace default candidate
profiles. Rootless IDs and Pi paths do not prove native ownership or join a
native catalog; project cwd is not a native root. Without `--inspect-clients`,
valid persisted sources retain `live_metadata_not_probed`. Explicit Herdr
inspection sends only `ping` and `session.snapshot` to known local endpoints,
using protocol22 with per-endpoint and profile deadlines/response limits.
Live references remain separate from persisted current/restore; public pane
IDs do not identify persisted panes. `client_ownership` uses that same cached
snapshot and stays outside `snapshot_id`. `reference_values_match` describes
observed session values, not an atomic generation or pane identity mapping.
Successful ping retains `clients_closed=false` even if the later snapshot fails;
without a valid response the state remains unknown. `detached_daemon_observed`
is the server's boolean startup self-report, not attached-client or writer proof.
Full writer coverage remains incomplete. `runtime_writer_coverage_unknown`
and `live_metadata_not_probed` have `blocks_delete=true` and `blocks_inventory=false`.
Records may return exit code `0` and `goal_status=complete` for its explicit
`inventory_scope` (persisted metadata or persisted plus live metadata), while
retaining these cleanup restrictions. Actual read/schema/transport/conflict
failures still return blocked. Source errors
remain scoped to the selected profile/source. Herdr does not add Orca
`guard_sources`; the rootless reader's mutation and verify capabilities stay false.
Delete plan/run without bindings returns structured capability blockers;
apply/status/verify preserve a blocked plan with no authorized actions instead
of interpreting the empty action set as successful cleanup. See
[the source and coverage limits](adapters.md#herdr已接入持久化只读引用).

On Windows, `delete plan --client herdr --herdr-bindings bindings.json
--record-id ID --out plan.json` enables the qualified native → frontend workflow.
Use the normal operation ID, immutable plan SHA and `--clients-closed` acknowledgement
for apply; status/verify rehydrate the frozen binding without reading the input file again.
An example binding is:

```json
{
  "schema_version": "larj.herdr-bindings.v1",
  "profile_root": "D:/HerdrConfig",
  "runtime_binaries": ["D:/Apps/Herdr/herdr.exe"],
  "native_stores": [
    {"session": "default", "engine": "codex", "root": "D:/CodexHome", "codex_binary": "D:/Apps/Codex/codex.exe"},
    {"session": "work", "engine": "claude", "root": "D:/ClaudeHome"},
    {"session": "work", "engine": "pi", "root": "D:/PiHome/sessions", "agent_dir": "D:/PiHome"}
  ]
}
```

Every observed agent pane needs an explicit session/engine root. `default` names the
profile's root session; other names refer to `sessions/<name>`. Select a native ID,
exact Pi path, or persisted `session/workspaces/N/tabs/N/panes/N` locator. All panes
and recovery copies bound to the selected native record enter its frozen closure.
The input declares root ownership; cwd and resume arguments never supply it.
See [the lifecycle and recovery limits](adapters.md#herdrwindows-显式绑定的完整清理).

Cindy Codex plans include ordinary native conversations from the same
client-qualified inventory used by `records`, together with their frozen
frontend reference closure. Project selection carries the native project's
metadata to the paired reference actions. When selected roots include both a
parent and its descendants, the plan retains one encompassing native cascade
and its complete reference closure; it does not send overlapping deletion
requests. Descendant references remain bound to their own native IDs. A
child-only selection never implicitly selects its parent and remains subject
to the existing lineage guards.

If a verified native deletion leaves Cindy references, a fresh plan can clear
those exact references only when a complete inventory proves the native index,
rollouts, legacy index and descendants absent. Incomplete catalogs and unproven
row ownership remain blocked. Reappearing native records invalidate the plan.
Reference actions remove SDK references only. Deleting retained chat rows requires
explicit Cindy session IDs as described below.
Missing-parent subagents use the existing native orphan evidence and guards for
the same Cindy store rather than a synthetic manual-delete finding.

Cindy frontend references are checked during planning against the supported
upstream FTS trigger definitions and index layouts. Unknown triggers return a
`frontend_preflight_blocked` blocker before any native deletion is attempted.
See [Cindy storage contract](cindy-storage-contract.md) for pinned upstream
sources, the closed-client CJK fallback, and regression fixtures.

## Office local sessions

`records --client qwenwork` reads the registered CN database schema and known
SDK UUID locations. Repeatable `--qwenwork-root` selects profiles;
`--qwenwork-sdk-root` selects the shared SDK root. IDs stay qualified by client and
profile. Unknown schemas and inaccessible sources cannot produce empty success.

`inventory_scope=database_metadata_and_known_sdk_paths` describes the body-free
inventory. Planning separately freezes the complete supported local closure.
Select full `chats.id` values for retained conversations; project/all-projects
scope selects only soft-deleted conversations. Install the pinned LevelDB
dependency explicitly before planning profiles with Chromium storage:

```powershell
python -m local_agent_record_janitor.office_leveldb --install
local-agent-record-janitor delete plan --client qwenwork --record-id '<chat ID>' --out office-plan.json --json
local-agent-record-janitor delete apply --operation-id '<operation ID from plan>' --plan office-plan.json --clients-closed --json
local-agent-record-janitor operation verify --operation-id '<operation ID from plan>' --plan office-plan.json --json
```

The dependency installer requires Node.js 18
or newer and npm; it installs the locked `classic-level` package without install
scripts. `LARJ_LEVELDB_RUNTIME` can select a dedicated runtime directory.
The default is `local-agent-manager/runtimes/leveldb-v1` under the local
application-data directory. An existing legacy runtime remains discoverable
when the new directory is absent.
Deletion never installs dependencies implicitly. Freeze/apply bind the Node
binary, helper and dependency code, native library, physical store and logical
before/after projections. Planning and status inspect private copies.

Each operation has SDK `delete_office_artifacts` and profile
`delete_office_frontend` batches. Native transcripts and owned caches must be
verified deleted before the profile's database, drafts and known UI references
are removed. Unknown prior operations on either root block the operation before
its first mutation. A partial write stays unknown; cold status/verify use the
original roots and evidence without retrying writes. Close Office main/helper
processes and SDK workers first; `--clients-closed` does not bypass process checks.

Only the registered CN schemas and proven local ownership qualify. Shared SDK
UUIDs, selected remote mappings, automation/import/fork/ACP recovery references,
unknown UI formats, changed runtime or store evidence produce targeted blockers.
Workspace files, generated artifacts, credentials and cloud records are preserved.
See [the evidence and limits](adapters.md#千问办公cn-本地会话清理).

## Paseo agent registry metadata

```powershell
local-agent-record-janitor records --client paseo --json
local-agent-record-janitor records --client paseo --paseo-root 'D:\profiles\paseo' --engine claude --json
local-agent-record-janitor records --client paseo --record-id '<Paseo agent ID>' --inspect-clients --json
```

The default is `PASEO_HOME`, then `~/.paseo`; `--paseo-root` can be repeated.
Records include flat legacy and project-directory snapshots, including archived
agents and agents without a provider session ID. Agent IDs are profile-qualified;
provider persistence/runtime references retain their separate provenance. Duplicate
snapshots and unreadable sources report incomplete inventory rather than an empty success.

`inventory_scope=persisted_agent_registry` does not cover provider transcripts,
schedules, workspaces, client caches or all writers. Saved status is not a live
probe: `--inspect-clients` keeps `clients_closed=null`. No daemon is started or
contacted. Full cleanup plan/run automatically prepares bindings for built-in Codex, Claude and Pi.
Unproven bindings block planning. Frozen bindings drive apply and cold status/verify. See
[Paseo boundaries](adapters.md#paseoagent-注册记录只读盘点).

For `antigravity`, inventory retains `unsupported:antigravity` and reports a
`provider_backend_family_hint` plus `native_cleanup_status=requires_paseo_binding` for qualified built-in handles
(or `unsupported` for unknown variants) in record
metadata. The hint describes the built-in provider family; custom implementations
and effective native homes still require evidence. It does not enable deletion or
make a serialized provider handle a native record ID. See
[Antigravity ownership and cleanup boundaries](adapters.md#antigravity).

### 只删 Paseo 会话

Windows 上的 `--paseo-only` 通过正在运行的本机 Paseo 0.15.3 daemon 删除一条
会话。需要标准用户安装位置的 Paseo 桌面分发包、对应的 `--paseo-root` 和
一个完整 Paseo 会话 UUID。Paseo 使用自己的删除接口关闭该会话并移除登记和时间线。
Codex、Antigravity 等 provider 的原生会话保留。该模式覆盖 daemon 的登记与时间线，
桌面离线副本、调度和其他产品的记录在范围之外。

```powershell
local-agent-manager delete run --client paseo --paseo-only --record-id '<完整 Paseo UUID 1>' --record-id '<完整 Paseo UUID 2>' --yes --progress --json
local-agent-record-janitor delete plan --client paseo --paseo-only --paseo-root 'D:\PaseoHome' --record-id '<完整 Paseo 会话 UUID>' --out plan.json --json
local-agent-record-janitor delete apply --paseo-only --operation-id '<operation_id>' --plan plan.json --plan-sha256 '<plan_sha256>' --json
local-agent-record-janitor operation verify --operation-id '<operation_id>' --plan plan.json --json
```

apply 使用冻结的 profile 和计划哈希，执行与核验期间保持目标 daemon 运行。
`run --yes` 可选择多个完整 UUID，省略根目录时使用 `PASEO_HOME` 或 `~/.paseo`。
全部目标先生成独立计划，然后自动按哈希执行并核验，每条记录保留独立恢复入口。
Codex、Claude、Pi、Antigravity 共用这一流程。结果未知时执行只读 status/verify，
后续目标等待核验收口。
终验复查 daemon 与磁盘登记中的精确 ID。超时、身份不一致或回执后仍有记录时，
执行 `operation status` 和 `operation verify`。结果为 `unknown` 的计划进入只读恢复。
协议和版本边界见 [Paseo daemon 删除](adapters.md#paseo-daemon-删除)。

### Paseo 完整本地操作

Windows 上显式绑定后，可沿用标准 `delete plan/apply` 与 `operation status/verify`。
`delete plan/run --client paseo` 会自动准备绑定，也可单独准备并检查。

```powershell
local-agent-manager environment paseo-cleanup prepare --json
local-agent-manager delete plan --client paseo --record-id '<完整 Paseo agent ID>' --out plan.json --json
local-agent-manager delete run --client paseo --record-id '<完整 Paseo agent ID>' --clients-closed --progress --json
```

自动发现核对 provider 环境、逐 agent 的 `launchEnv` 及所有已保存原生句柄。
逐 agent 环境覆盖 provider 环境，默认目录需由实际原生 ID 或 Pi 文件头证明。
自定义 provider 实现、额外启动参数和未知原生目录仍需显式资格。
内置 Antigravity 插件的已验证句柄可绑定 CLI 原生 writer，见下文
[T3/Paseo 与 CLI 联合清理](#t3paseo-与-antigravity-cli-联合清理)。旧插件和未知句柄仍阻挡。
桌面 IndexedDB 存在时使用 `LARJ_PASEO_ELECTRON_RUNTIME` 指向下述已验证依赖。
绑定文件仍可显式传入，来自实际启动配置和原生证据。示例为

```json
{
  "schema_version": "larj.paseo-bindings.v1",
  "profile_root": "D:/PaseoHome",
  "server_id": "srv_example",
  "runtime_binaries": ["D:/Apps/Paseo/paseo.exe"],
  "native_stores": [
    {"agent_id": "11111111-1111-4111-8111-111111111111", "engine": "codex", "root": "D:/CodexHome", "codex_binary": "D:/Apps/Codex/codex.exe"}
  ],
  "desktop_profiles": [
    {"root": "D:/PaseoDesktop", "electron_runtime": "D:/Dependencies/electron-44.2.0", "runtime_binaries": ["D:/Apps/PaseoDesktop/Paseo.exe"]}
  ]
}
```

`native_stores` 包含 profile 中每个观察到的 agent。Pi 另需 `agent_dir`，其 `root` 是
session 根；Claude `root` 是 config 根。一个计划只支持一个 Codex 根，可包含多个
已绑定 Pi/Claude 根。`server_id` 必须与 `server-id` 文件和环境一致。
`desktop_profiles: []` 明确限定为 headless 服务端；有本机桌面缓存时须列全。

```powershell
local-agent-record-janitor delete plan --client paseo --paseo-bindings bindings.json --record-id '<Paseo agent ID>' --out plan.json
```

检查计划中展开的后代、原生批次及前端批次，再按通用协议用 operation ID、plan SHA 和
`--clients-closed` 执行。冷恢复从计划读取冻结绑定，不重新加载 bindings.json。

存在桌面缓存时，先按办公版说明准备固定 ClassicLevel 依赖，并提供完整官方
[Electron 44.2.0 Windows x64 分发包](https://github.com/electron/electron/releases/tag/v44.2.0)
的解压目录；计划会逐文件验证内置 manifest，不能用正在运行的产品目录替代。
源码 checkout 可用 `python -m tests.install_paseo_electron D:/Dependencies/electron-44.2.0`
下载并核对固定 SHA 的测试依赖；该命令不安装或启动 Paseo。
完整范围、中断恢复和明确阻挡条件见
[Windows 清理边界](adapters.md#paseowindows-显式绑定的完整清理)。

## 只删 T3 会话

Windows `--t3code-only` 使用本机已安装 T3 Nightly 的 CLI 发放短期凭证，通过该 profile
的回环服务调用 `thread.delete`。要求服务运行、所选 thread 空闲、完整 ID 精确匹配，
使用一个 T3 home，省略 `--t3code-root` 时采用 `T3CODE_HOME` 或 `~/.t3`。
不接受 ID 前缀、批量 project 范围或 native bindings。版本号只作运行信息记录。
准入检查本机 locator、所需投影表和列、冻结的运行文件、所选线程及 provider 状态。
增加无关表、列或索引可以继续使用该入口，缺少必需字段会阻挡。
RPC 仍使用既有本机认证和 orchestration protocol 2，成功必须经过实际结果回读。
原生记录的直接磁盘清理与历史导入各自沿用对应格式和接口契约。

```powershell
local-agent-record-janitor delete plan --client t3code --t3code-only --t3code-root 'D:/T3Home' --record-id '<完整 T3 thread ID>' --out t3-only.json --json
local-agent-record-janitor delete apply --t3code-only --operation-id '<计划中的 operation_id>' --plan t3-only.json --plan-sha256 '<已批准的 plan_sha256>' --json
local-agent-record-janitor operation verify --operation-id '<同一 operation_id>' --plan t3-only.json --json
```

已授权的多个完整 ID 可用一条命令自动完成计划、应用和验证。

```powershell
local-agent-manager delete run --client t3code --t3code-only --record-id '<完整 T3 ID 1>' --record-id '<完整 T3 ID 2>' --yes --progress --json
```

`--yes` 确认这组精确 ID 的 T3 内置删除范围。批量 run 先为所有 ID 生成独立计划，
预检都通过才开始写入，自动传递实际计划哈希。每个操作都有原计划路径和单独的
`operation status/verify` 恢复入口。可用 `--out batch.json` 保存批次定位清单，
相邻的 `batch.1.plan.json` 等文件保存各线程原计划。发生 unknown 时只运行
status/verify，未收敛就停止后续线程；不会自动重发删除。

计划固定 profile、environment ID、服务实例、运行文件摘要和所选记录。删除前先通过
`thread.metadata.update` 的预期路径/分支条件解除工作树关联，读回确认后再删除，
所以启用 `worktreeOnDelete` 也保留原工作树。核验要求删除标记一致、没有活动运行、
后台 effects 全部结束、原生引用不变且工作树目录身份仍一致；并非收到 RPC 响应就成功。
临时凭证只在进程内传递，并通过自带 CLI 撤销。

这实现 T3 自带软删除，保留 provider 原生会话及工作树，不宣称 T3 全部历史物理清除。
Codex、Claude、Antigravity 混合 profile 不需要原生绑定，选中 Antigravity 也适用。
不更改原生存储，因此原生保留依据是已验证接口语义、引用不变和临时 profile 验收；
运行时不会扫描或散列全部原生会话。工作树内容也不递归扫描。
不要求 `--clients-closed`，也不能与 `--t3code-bindings` / `--paseo-only` 合用。
超时或解除关联后的异常进入 `unknown`，禁止重发；使用原计划 `status/verify`。

## T3 Code 完整本地操作

先用 `records --client t3code --json` 获取元数据，或用可重复的 `--t3code-root` 指定
实际 T3 home（包含 `userdata` 的目录，不是 Electron 桌面 profile）。默认使用
`T3CODE_HOME` 或 `~/.t3`。普通 inventory 不执行删除，也不推断原生 provider 根目录。

`delete plan/run --client t3code` 省略 `--t3code-bindings` 时自动准备绑定。
准备流程读取 provider instance、配置的原生 home 和 executable，并用原生会话 ID
及历史引用核对归属，支持自定义 instance ID。已知桌面 profile 一并纳入原有计划。
已有显式绑定文件仍可继续使用。

```powershell
local-agent-manager environment t3-cleanup prepare --json
local-agent-manager delete plan --client t3code --record-id '<完整 T3 ID>' --out plan.json --json
local-agent-manager delete run --client t3code --record-id '<完整 T3 ID>' --clients-closed --progress --json
```

独立 prepare 命令返回绑定或结构化 blockers，可用 `--out bindings.json` 保存。
准备和计划均不删除记录、不关闭程序。完整清理执行仍要求相关写入进程退出，并持有
原有生命周期锁。受当前会话占用时，助手须按已授权范围办理关闭或执行器交接。
无法证明的旧 home、历史句柄、原生格式及共享引用会汇总报告并保留。
自动发现失败时 plan 仍生成阻挡计划，不会将完整清理自动改成 T3 软删除。

配置中的安装器链接会解析为实际运行文件，并继续验证文件身份和原生 ID 归属。
自定义 provider instance 在历史 run/attempt 中的名称，需要由同线程的 instance、
默认 provider 和持久化 driver 共同证明。已登记的子任务线程也会加入后代范围，
子任务的 provider 按子线程归属核验。

已知共享 provider session 保留其全局行及未选线程的绑定，只清理选中线程的绑定。
该路径要求共享 ID、instance、driver、status 和 payload 相符，且全局行的当前 owner
位于选区之外。未知共享格式、owner 位于选区、恢复引用及真实跨线程 transfer 继续阻挡。
当前其他对话中的用户 thread mention，以及 `t3-code.t3_thread_read` 的已知输入/输出
审计内容属于各自对话的内容。其引用文本继续保留，原线程清理按指定记录边界执行。
已知消息正文、命令执行文本和子任务 prompt 中的 ID 文字也按对话内容保留。
JSON 恢复字段、未知工具引用和包含目标 ID 的未知键仍作为恢复依赖检查。

V2 线程事件在 `orchestration_events` 中的镜像也参与原生引用核验。已知 Codex／Claude
导入 cursor 和 transcript 元数据作为只读保护源，按引擎和原生 ID 保护历史来源，
即使当前 provider home 已改变。直接选择这些导入记录进行完整删除仍会阻挡。
恢复依赖检查覆盖完整原生删除范围，包括原生发现的后代。所有保留的 provider session
和历史对象均参与检查。未知旧事件类型、字段或恢复 cursor 继续阻挡。
其他线程的已证明上传附件及支持格式的提取文本会冻结保留。选区附件、未知同名文件、
共享附件和未证明归属的文件继续阻挡完整清理。

Antigravity 记录仍使用 `unsupported:antigravity`；元数据的
`provider_backend_family_hint=antigravity-acp` 只描述内置实现线索，
`native_cleanup_status=requires_t3code_binding` 表示需要显式绑定才能验证原生清理资格。
T3 的托管运行时、隔离配置、会话文件和前端数据库不能互相替代；参见
[Antigravity 边界](adapters.md#antigravity)。

混合 profile 中没有原生 ID 的 Antigravity provider 仍然可见；将默认 provider 改成
Codex 或提供 Codex 绑定，不能绕过原生 provider 和实例的资格检查。

Windows 的显式绑定文件使用以下格式；`environment_id` 必须来自该 profile 的
`userdata/environment-id`，`native_stores` 必须覆盖所有已观察 thread。示例路径和 ID
必须替换为已确认的本机值，不能直接复制执行。T3 thread ID 与原生 ID 分开处理。

```json
{
  "schema_version": "larj.t3code-bindings.v1",
  "profile_root": "D:/T3Home",
  "environment_id": "environment-id-from-file",
  "runtime_binaries": ["D:/Apps/T3/T3 Code (Nightly).exe"],
  "native_stores": [
    {"thread_id": "t3-thread-id-1", "engine": "codex", "root": "D:/CodexHome", "codex_binary": "D:/Apps/Codex/codex.exe"},
    {"thread_id": "t3-thread-id-2", "engine": "claude", "root": "D:/ClaudeConfig"},
    {"thread_id": "t3-thread-id-3", "engine": "pi", "root": "D:/PiAgent/sessions", "agent_dir": "D:/PiAgent"}
  ],
  "desktop_profiles": [
    {"root": "D:/T3DesktopProfile", "runtime_binaries": ["D:/Apps/T3/T3 Code (Nightly).exe"]}
  ]
}
```

绑定所有相关的本地 runtime 文件和桌面 profile（Windows 常见桌面路径为
`%APPDATA%/t3code-v2`）。仅明确限定 headless 服务端时才能用 `desktop_profiles: []`；
它不能代表浏览器、移动端或远程副本已经删除。桌面缓存需要已安装的固定 LevelDB 依赖：

```powershell
python -m local_agent_record_janitor.office_leveldb --install --runtime-dir 'D:/Dependencies/larj-leveldb'
$env:LARJ_LEVELDB_RUNTIME = 'D:/Dependencies/larj-leveldb'
local-agent-record-janitor delete plan --client t3code --t3code-bindings bindings.json --record-id '<T3 thread ID>' --out plan.json
```

计划列出原生及前端动作、后代、文件/数据库摘要和依赖。审阅并明确授权该计划后，关闭
所有绑定客户端和后台服务，使用现有 `delete apply --authorized-plan-sha256` 流程；
`--clients-closed` 是用户确认，不能代替 writer 枚举和 server-owner 锁。随后使用
`operation status`、`operation verify` 对同一计划做冷验证。命令参数沿用本文
[Commands](#commands)，不另设直接删表命令。

Pi 的 T3 原生句柄是绝对 JSONL 路径。绑定同时核对 session 根、文件头 UUID 与精确文件身份，
同 UUID 的其他文件独立处理。自动发现读取 `PI_CODING_AGENT_DIR` 和
`PI_CODING_AGENT_SESSION_DIR`，配置有额外 Pi 启动参数时要求显式绑定。
中断后使用冻结文件身份恢复，管理日志写入 agent 根目录。

完整清理支持固定 schema 的本机 v2 Codex/Claude/Pi 和下述受限 Antigravity ACP 记录；附件、弱引用、peer、旧恢复
记录和未证明的共享关系会明确阻挡。工作区、诊断日志、凭据及外部备份保留。
具体边界见 [T3 Code 适配说明](adapters.md#t3-code本机数据库与完整清理)。

### 已退役 Antigravity 的 T3 数据库清理

只清理共享 `statev2.sqlite` 时，可通过 `--t3code-bindings` 提供
`larj.t3code-antigravity-database.v1` 绑定。沿用上文的 `profile_root`、`environment_id`
和 `runtime_binaries`，将 `native_stores` 与 `desktop_profiles` 都设为 `[]`，并填写
`provider_instance_id`。用可重复的 `--record-id` 逐条指定完整的 T3 thread ID。

要求 Windows、已禁用的 Antigravity 实例、已移除的托管运行时和实例目录，以及同一实例
下已软删除且无活动会话的记录。计划冻结对应的事件、消息和投影行，并拒绝仍有共享依赖
或关联文件待处理的选区。执行持有 T3 写入锁，按行数及数据库摘要验证提交，并截断 SQLite
WAL。范围限于选中的数据库记录，原生存储和桌面缓存属于其他操作范围。

### Antigravity ACP 完整删除绑定

此路径支持格式与启动绑定校验通过的 Windows T3 托管 ACP。CLI 使用下一节的独立布局绑定；
需要保留原生历史时使用 `--t3code-only`，不得把旧 ACP 目录当作新 CLI 会话目录。
每条 Antigravity 绑定使用 `engine: "antigravity"`，增加
`provider_instance_id` 和 `acp_runtime_binaries`（`agy_acp_server.exe`、配套
`localharness_external.exe` 的绝对路径）；二者也必须列入顶层 `runtime_binaries`。
`root` 必须严格对应 `<T3Home>/userdata/providers/antigravity/<SHA256(instanceId)>/antigravity-acp`。
原生文件证据版本为 `larj.antigravity-acp-files.v2`，额外冻结 `userdata/settings.json`。
绑定实例必须仍选择该双文件运行时：显式路径须匹配绑定的 server，默认模式须由
`tools/antigravity-acp/win32-x64/active.json` 指向同一 server。CLI 选择返回
`antigravity_cli_acp_cleanup_unsupported`；设置缺失、选择不符或计划后漂移阻挡写入。
旧 v1 文件证据不再用于新执行，应重新生成计划；已有未知操作仍遵循恢复契约，不得重跑。
当前及历史强引用必须属于同一实例；不得把其他 instance 的原生 ID 填入该绑定。

```json
{
  "thread_id": "完整-T3-thread-ID",
  "engine": "antigravity",
  "root": "D:/T3Home/userdata/providers/antigravity/<instance-hash>/antigravity-acp",
  "provider_instance_id": "antigravity",
  "acp_runtime_binaries": [
    "D:/T3Home/tools/antigravity-acp/win32-x64/versions/<release>/agy_acp_server.exe",
    "D:/T3Home/tools/antigravity-acp/win32-x64/versions/<release>/localharness_external.exe"
  ]
}
```

普通完整清理命令不变。计划冻结原生文件摘要；同一 T3 前端动作在持有启动锁和运行文件
排他边界后，删除精确 UUID 对应的数据库、SQLite 侧文件、`.meta` 和 `brain/<UUID>`，
然后清理 T3 本地历史。未选会话、设置、凭据、skills、runtime 和工作区保留并核验。
原生数据库 schema/ID 不符、未知恢复文件、`parent_references` 或子轨迹关系都阻挡；
这些关系尚无完整解析器。未知存储格式和远程数据保持阻挡，CLI 使用下节的布局绑定。
中断后只允许原计划冷验证；残留需要新计划。

开发验收可设置 `LARJ_T3_BUNDLE_ACCEPTANCE=1` 后运行
`python -m unittest tests.test_t3code_runtime_acceptance`。它仅使用临时 profile、无效测试
key 和合成会话，不发送模型提示；Windows 的 SQLite 测试临时目录应使用较短的绝对路径。
旧受管运行时已卸载时，其验收自动跳过，不为测试重新安装。CLI 模式的隔离存储验收使用
`LARJ_AGY_CLI_ACCEPTANCE=1` 和绝对路径 `LARJ_AGY_CLI_BINARY`。测试使用当前指定的程序，
通过 ACP 握手、无真实凭据的空会话及原生数据格式核验实际存储行为。
产品版本与编译摘要不作为测试通过条件。
旧存储探测本身不授予删除能力。独立 CLI writer 的新验收见下节；两者均不证明实际账号生成成功。

### T3/Paseo 与 Antigravity CLI 联合清理

Windows 上，普通 `delete plan/run --client t3code` 或 `--client paseo` 可把已证明的 CLI
会话及其原生子会话，与选中前端记录放进同一个冻结计划。选择仍用前端 ID；native UUID
必须来自该记录的强引用或已验证插件句柄。完整计划、哈希批准、`--clients-closed` 和
冷启动 `operation status/verify` 沿用对应客户端流程，不与 `--t3code-only/--paseo-only` 混用。

CLI 绑定在各自 `native_stores` 项增加以下字段；T3 使用 `thread_id` 并保留
`provider_instance_id`，Paseo 使用 `agent_id`，不使用 `acp_runtime_binaries`：

```json
{
  "thread_id": "完整-T3-thread-ID",
  "engine": "antigravity",
  "provider_instance_id": "antigravity",
  "native_layout": "cli",
  "root": "C:/Users/example/.gemini/antigravity-cli",
  "cli_user_home": "C:/Users/example",
  "cli_binary": "C:/Users/example/AppData/Local/agy/bin/agy.exe"
}
```

`cli_binary` 也必须列入顶层 `runtime_binaries`。准入核验实际存储格式、native ID、
前端强引用与启动绑定。兼容的 CLI 和托管 ACP 升级可直接使用，产品版本和预设二进制
摘要不参与准入。新增索引和等价 SQL 排版可继续使用，已有表、约束、关系字段及未知恢复对象仍按格式校验。
计划另外冻结共享数据库的完整 schema 摘要，涵盖本次接受的索引。执行和冷验证会核对该摘要，
索引定义在计划后变化时保留恢复材料。旧证据按其原有基线 schema 核验。
原生清理通过管理器自身的离线 writer 和私有副本转换执行。
每次计划冻结当前程序的文件身份和摘要，执行前后继续核对。计划生成后发生升级时，
旧计划会在写入前阻挡；重新运行 `delete run` 会按当前程序自动生成新计划。
T3 的绝对 `config.binaryPath`、Paseo 的单项绝对 `agents.providers.antigravity.command`
必须选择该实际文件；裸 `agy` 的 daemon PATH 尚无精确证明格式，当前拒绝自动猜测。CLI 根必须对应实际 OS 用户 home 下的
`.gemini/antigravity-cli`；provider/逐 agent 的 home 覆盖须一致，T3 的 `GEMINI_HOME`
不能替代该证明。包装脚本、额外命令参数和冲突 home 需要相应启动契约，不能手填目录绕过。

Paseo 的已知插件格式把 `{version:1,data:{conversationId:"<UUID>"}}` 序列化成
`persistence.sessionId = "plugin:" + JSON`，并在
`persistence.metadata.pluginProviderPersistence` 保存恢复对象；`runtimeInfo.sessionId`
若存在也必须解出同一 UUID。这里的 `version:1` 标识持久化格式；Paseo 产品版本不参与准入。
字段形状及恢复关系按契约校验，裸 UUID、旧 ACP 自定义序列化、
`nativeHandle` 变体、额外 metadata 和相互冲突的恢复句柄明确阻挡。
依据：[provider 持久对象](https://github.com/getpaseo/paseo/blob/d91ae138cb931447635e94296023414a58bedcc5/plugins/antigravity-provider/server/internal/session.ts)
与 [核心序列化及恢复](https://github.com/getpaseo/paseo/blob/d91ae138cb931447635e94296023414a58bedcc5/packages/server/src/server/agent/plugin-provider.ts)。

计划冻结全部原生子会话、共享摘要/cache/恢复镜像、原生会话文件，以及原有前端文件和绑定桌面闭包。
跨客户端 guard 只允许冻结的选中前端转换：扫描其私有 after-image，其他 profile、未选记录、
历史和可恢复引用、共享原生消费者保持保护。一次计划仅选一个前端 profile；另一个 profile
也引用同一原生会话时阻挡，不自动扩大批准范围。

执行复用前端启动锁、运行文件租约及 CLI 写入 ticket。原生根另保存关联父计划哈希的占用日志，
防止中断后其他独立 native 操作修改共享文件。共享文件 before-image 写入该原生日志目录，
部分删除或前端后续失败保持 `unknown`，禁止重放。用原计划 status/verify 核验；只有完整
after-state 得到确认才解除占用，不能通过重建计划或删除管理日志强行继续。

旧托管 ACP 的 manifest 和冻结 evidence 继续由原适配器处理，不自动升级成 CLI 绑定。
未选会话、凭据、配置、skills、工作目录和未授权前端 profile 不在删除范围。

## 独立 Antigravity CLI 原生清理

摘要库中的已知 `conversations_fts` 全文索引与 `search_schema_version` 可参与清理。
计划校验 FTS 声明、SQLite shadow tables、索引完整性并冻结完整 schema 摘要。
删除按 `conversation_id` 清理全部 chunk，随后依据保留内容重建索引，并沿用
`secure_delete`、VACUUM、前后逻辑摘要及恢复镜像验证。检查时间标记
`last_check.timestamp` 按原样保留。索引重建行为见 [SQLite FTS5 文档](https://www.sqlite.org/fts5.html#the_rebuild_command)。

`antigravity` 指使用 CLI 原生存储的会话，而非 T3 thread ID、Paseo agent ID 或托管
ACP 实例目录。默认根为当前 OS 用户的 `~/.gemini/antigravity-cli`；非默认根必须显式
指定。`GEMINI_HOME` 不能证明 CLI 使用该根。读取和计划按实际 SQLite DDL、文件格式
与身份校验，不设产品版本白名单；写入目前需要 Windows 的排他文件句柄。

```powershell
local-agent-manager records --client antigravity --antigravity-root 'D:\Profile\.gemini\antigravity-cli' --json
local-agent-manager delete plan --client antigravity --record-id '<完整 CLI 会话 UUID>' --antigravity-root 'D:\Profile\.gemini\antigravity-cli' --antigravity-bin 'C:\Apps\agy.exe' --out plan.json --json
local-agent-manager delete apply --operation-id '<返回的 ID>' --plan plan.json --authorized-plan-sha256 '<返回的 SHA256>' --clients-closed --json
local-agent-manager operation status --operation-id '<返回的 ID>' --plan plan.json --json
local-agent-manager operation verify --operation-id '<返回的 ID>' --plan plan.json --json
```

同一选择可用 `delete run`、相同 root/bin 参数、`--clients-closed --yes` 完成计划和执行。
只接受完整 UUID，可重复 `--record-id`；没有 project/all-projects 或前缀批量删除。
`--antigravity-bin` 必须是已核实的实际 CLI 文件，计划冻结其身份和摘要；写入不启动它，
不停止任何进程。操作日志固定存入原生根的管理器控制目录，避免与其他同根操作失去锁关联。
外部 `--out` 只是可审阅副本；也可用 `--operation-home '<原生根>'` 定位原始日志。

冻结范围包括 `conversations/<UUID>.db` 及 SQLite 侧文件、`brain/<UUID>`、
仅含 title 的 `annotations/<UUID>.pbtxt`、零字节 `presence/<UUID>.lock`、
`conversation_summaries.db` 的精确行、`jetbox_summaries_proto.pb` 的 UUID map entry、
`cache/conversation_metadata.json` 和 `cache/last_conversations.json` 中的目标记录。
protobuf map 的值必须与数据库的 raw summary 对应，未选 map entry 保持原字节。
摘要索引可证的子会话纳入计划的 `selected_ids`；未选父引用、无法解码的
`parent_references`/subtrajectory、group/battle 关系、未知备份和格式不自动吸收。
workspace、设置、凭据、skills、运行文件及诊断日志保留。
`bin`、`mcp`、`updater` 原样保留；只接受指向 `log/` 单个文件的 `cli.log` 符号链接，
冻结链接本身且不跟随，其他未知链接阻挡。附属文件须与摘要中的精确 ID 对应；
未知注释字段、非空 presence 文件及已打开的锁文件均不绕过。
日志可能包含历史诊断文本；本功能不承诺全盘敏感文本擦除、云端删除、其他 OS 用户
或未知复制目录清理。

默认、环境指定及额外 `--t3code-root`/`--paseo-root` 均作为保护来源；显式根不取消
默认保护。T3 所有合格数据库历史单元、Paseo agent/atomic/schedule/creation/receipt
恢复文件存在目标 ID 时阻挡，不按前端软删除状态豁免，也不凭相同 UUID猜测归属。
这些前端仍运行或其来源读取失败时不写入。本入口不删除 T3/Paseo 记录；现有托管
ACP writer 仍拒绝 CLI 绑定；T3/Paseo 联合删除使用上文独立的 CLI 布局和精确前端闭包。
若存在引用，应在已授权、已支持的前端完整清理路径解决；不能编辑日志状态或关闭保护。

writer 对运行文件、共享重写文件和待删会话文件持有 Windows 排他句柄，对已有前端
保护文件也持有句柄；摘要数据库锁本身不是 CLI 全局启动锁。文件集合和未选记录有
冻结证据，写后重新核验。未知运行文件、其他客户端的私有内存和未发现配置不属于
已证明范围；不能以一次进程快照承诺任意未来启动都被阻止。
共享文件在私有副本上计算 after，原文件通过持有句柄写入；跨文件不是原子事务。
捕获到共享写入失败时在原句柄内恢复 before；崩溃或删到一半保持 `unknown`，不重放。
`native-recovery/` 保存必要 before images，状态维护保留该目录。只在精确终验通过后
清除核实过的恢复副本；其余残留需独立恢复判断，本实现不自动覆盖崩溃后改变的文件。

开发验收：设置 `LARJ_AGY_CLI_ACCEPTANCE=1`、绝对路径 `LARJ_AGY_CLI_BINARY`，运行
`python -m unittest tests.test_t3code_runtime_acceptance.AgyCliAcceptanceTests`。
使用隔离用户 home、假 key、拒绝网络的代理，仅创建空会话，不提交模型提示。
新原生验收核对镜像删除、未选会话字节保持、真实 CLI 重开并加载保留会话后目标不复现。
空会话可能不出现在 `session/list`，因此列表为空不是删除成功证据。

依据：[官方 resume/cache 文档](https://antigravity.google/docs/cli/commands/resume)、
[官方 CLI ACP](https://antigravity.google/docs/ide/extensions/acp/)、
[ACP 可选删除语义](https://agentclientprotocol.com/protocol/v1/session-delete)，
以及仓库隔离运行时验收和随包存储 schema。官方 F4 删除不等价于上述本地闭包核验。

## WorkBuddy local sessions

`records --client workbuddy [--workbuddy-root PATH ...]` inventories an independent
WorkBuddy store. Explicit repeatable roots replace the default
`WORKBUDDY_CONFIG_DIR` or `~/.workbuddy`; unrelated clients do not implicitly
inspect that default root. The engine is `workbuddy`, and record identity includes
the physical profile root. Metadata contains no chat titles, prompts or transcripts.
`--inspect-clients` adds the bounded Windows writer probe; process failures cannot
prove closure. Windows is currently required for mutation.

The dedicated `delete_workbuddy_session` family supports the fixed WorkBuddy
5.6.2 schema and proven local terminal record closure. Project/all-projects scopes
select only soft-deleted rows (`deleted_at > 0`). Retained terminal rows require
explicit full session UUIDs through `--record-id`; prefixes do not select records.
One root has one batch per selected mutation family containing only approved IDs. Plan freezes exact SQLite
rows, exclusive artifact paths, supported sidebar/pinned references, complete shared
store fingerprints, and reproducible batch after fingerprints.

The independent `remove_workbuddy_ui_reference` family removes exact local
pinned-only IDs, without dispatching native SQLite DELETE. Eligibility requires
no native/usage row, session artifact, sidebar reference, sync association or
automation dependency, and one user/environment's current global store plus only
its known legacy migration sources. Select these IDs explicitly in a separate
fresh plan from local-session deletion; mixed selection is blocked. Results report
`removed_ui_only_ids` separately from `deleted_session_ids`.

Local-session closure includes a same-stem `.file-rollback.ndjson` only beside its
top-level UUID transcript and only for the supported `{v:1,requestId,commitSeq}`
metadata format. The plan records format/count/fingerprint, not request IDs.

Apply restores frozen WorkBuddy roots when they are omitted and rejects conflicting
explicit roots. It requires `--clients-closed`, complete related-writer closure and
unchanged evidence, including under the SQLite writer lock. It deletes approved
session/usage rows, proven exclusive artifacts and precise JSON ID references,
checks exact affected counts, and removes temporary shared DB/JSON rollback only
after complete proof. User work products and configuration/automation definitions
remain. Shared attachment cleanup and cloud deletion are outside this guarantee;
`remote_delete=false` and no cloud API is called.

Unknown schema, orphan files/unknown UI-only IDs, unproven rollback formats or subagent
copies, shared media indexes and related remote mappings remain visible as blockers.
An empty database does not prove full cleanup. Unrelated UI-only IDs are retained
without blocking an independently proven exact local ID; all-projects reports their
coverage gap. See [the complete support limits](adapters.md#workbuddy独立本地会话).

After any partial unlink or post-commit failure, the child remains unknown and
holds the shared database footprint, including against another ID in that store.
Status/verify never resend deletion or restore a shared store. Recovery requires
the immutable complete before/after proof, even if backup files are missing; mixed
state preserves recovery evidence. Only a trusted completed child checkpoint or
receipt with no remaining rollback permits later exact-ID verification without
requiring the historical whole-store hash after other legitimate operations.
WorkBuddy evidence preserves explicit null row/usage/sidecar fields across plan
persistence so a fresh process can verify the exact frozen snapshot. A started
unknown operation is marked modified only after strict after-state verification;
an unchanged before-state retains its residuals and does not claim modification.

## Output

JSON output and human summaries contain metadata only: IDs, stores, paths,
counts, classifications, blockers, and progress grouped by project, engine,
and physical location. Chat messages, prompts, transcripts, and response
content are not read into the report or saved in operation evidence.

For structured goal fields, see [Result contract](agent-automation.md#result-contract).
Evidence retention and rollback behavior are documented in
[Operation evidence and receipts](agent-automation.md#operation-evidence-and-receipts)
and [Temporary rollback copies](agent-automation.md#temporary-rollback-copies).

The stable classification vocabulary is:

```text
healthy
orphan_native
orphan_frontend
orphan_project
broken_relation
stale_index
partial_remote
corrupt_unreadable
unknown_operation
unverified
```

Codex child records do not need their own frontend reference to be `healthy`.
The complete catalog resolves their parent chain within the same native store
before project or record filtering. Each ancestor must have a native index row
or a verifiable rollout; a frontend reference or graph-only placeholder is not
proof that the parent exists. In third-party client scope, unreferenced roots retain `orphan_native`, as do
children whose immediate native parent is confirmed missing. Unknown or
conflicting parent chains are `broken_relation`, not confirmed orphans.
Normal standalone Codex/Pi/Claude records are `healthy` without frontend
references. Unknown backends or unverified native ownership are `unverified`.
Unknown backends remain inventory-only; unverified native roots cannot grant a
native writer, even when a separate exact frontend writer is supported.
Consumers must tolerate additional inventory
classifications and must never treat a classification as write authorization.

Record metadata includes `is_subagent`, `parent_thread_ids`,
`descendant_thread_ids`, and `lineage_status` (`root`, `known`, `missing_parent`,
`unknown`, or `conflict`). Parent references are not copied into a child's
`frontend_reference_ids`. Normal manual-record plans use the same classification;
explicit anomaly findings retain their anomaly classification. Classification
is inventory metadata, not deletion authorization: exact scope, descendant
closure, parent protection, and frozen-plan checks still apply.

`frontend_reference_ids` retains compatibility display IDs;
`frontend_binding_keys` qualifies each binding by client, database, frontend ID,
engine, native target, reference kind and historical boundary. Matching, grouping
and record selection use these qualified bindings. Native catalog errors and
per-record blockers remain visible through the client projection. Every selected
profile participates in initial inventory and revalidation. Cindy's default
inventory also probes supported native engines with no remaining frontend rows.

Pi `parentSession` is fork provenance, not a recursive deletion rule. Claude
subagent files are members of a session manifest. Neither relationship inherits
Codex's descendant deletion semantics. See [adapter contracts](adapters.md).

An unreadable unrelated store is a warning, while an unreadable target or an
unprovable shared reference blocks only the affected child batch. When
`remote_delete=false`, residuals are reported only when the adapter provides
authoritative discovery evidence; otherwise the output states the capability
boundary without claiming residuals, and nothing is deleted. Stale-index and broken-relation discovery remains in the
anomaly scan and is not guaranteed to appear as a uniformly executable
`records` target for every client.

Cindy frontend-session deletion is deliberately narrower than "not active":
project/all-projects cleanup selects only `sessions.status='deleted'` rows.
An explicit `--record-id <Cindy-session-id>` selection also supports `active` and
`archived` rows when the user requests their permanent deletion. This selection
is frozen in the operation scope and in each row's `explicitly_selected` evidence;
a native SDK thread ID does not authorize deleting its Cindy chat row.
All approved IDs in one Cindy database are guarded as a set and deleted in one
transaction together with their supported message, FTS, embedding/vector, and
session-owned dependency rows. Status changes invalidate the frozen evidence.
Unselected retained rows and unproven schemas remain protected.

Cindy automation run history can be selected separately with exact
`--record-id schedule-run:<full-run-id>` values. This uses the independent
`delete_schedule_run` family; project scopes and ID prefixes never select run
history. It deletes terminal `schedule_runs` rows and maintains the supported
`schedule_session_latest_runs` dependencies. Task definitions and conversations
are preserved. Deleting conversations alone only clears their run references.
To remove both, first delete explicitly selected conversations, then freeze a
fresh run-history plan because conversation deletion changes the run metadata.
The writer requires the probed schedule schema and complete trigger definitions,
closed owning clients, unchanged metadata, exact row counts and verification.
Unknown schemas remain inventory-only. Interrupted verification keeps a temporary
rollback copy; `operation verify` removes it only after proving the frozen
before/after state. Evidence contains IDs, statuses, timestamps and hashes, never
prompts, error text, hook output or run results.

## Official Desktop inventory and completion

Select the official native `CODEX_HOME` explicitly when the caller itself runs
inside Cindy. `records --inspect-clients` adds read-only process ownership
evidence (PID, parent PID, executable and store relation); process names alone
do not identify which store is open. On Windows, the read-only CIM process
probe is bounded and uses a hidden PowerShell window; a timeout or launch error
is reported as an inability to prove that clients are closed. Apply and verify restore the frozen native
store when a plan is supplied and reject a conflicting explicit home.

Cindy process attribution supports both per-user `Programs\Cindy` and machine
`Program Files\Cindy` / `Program Files (x86)\Cindy` installations. Excluding a
separate Cindy family still requires existing matching executable identities,
its process ancestry and consistent absolute `--user-data-dir` evidence.
Missing or conflicting evidence remains blocking.

Record output contains the Desktop catalog's UI display title when available,
its source, the full stable record ID, parent/descendant IDs and a snapshot ID.
Project and record filters restrict both displayed rows and grouped counts.
Temporary labels such as M14 belong to the inventory that produced them; resolve
them to full IDs before generating an immutable plan. Titles containing known
automatic-review history wrappers are displayed as `自动审查记录`; other long
display strings are bounded without changing identity fingerprints.

The same official home may contain both `sqlite/codex.db` and the older
`sqlite/codex-dev.db`. Discovery includes both supported catalogs; filename or
modification time never selects one to the exclusion of the other. Local rows
retain their database identity and aggregate into one thread target. A Desktop
cleanup plan binds the complete catalog path set, each catalog revision, exact
local rows and shared JSON references. A new or removed catalog invalidates that
snapshot; unreadable or incompatible known candidates cannot be hidden by a
healthy sibling. Additional catalogs of unproven ownership and linked database
aliases remain blocked.

Desktop residual cleanup deletes each selected local row from every catalog
where it exists and handles shared JSON references once. It preserves other
threads, non-local host rows and the database files. All catalogs and shared
JSON files receive temporary rollback copies before writing. Failure attempts
restoration of every file whose write was attempted and verifies the restored
snapshot. A sibling rejected before writing is not overwritten by rollback;
external drift or incomplete recovery retains its evidence. Separate databases
and JSON do not share one atomic
transaction, so closed-client guards, frozen evidence and final verification
remain required. Single-catalog snapshots retain their existing v1 fingerprints;
dual-catalog snapshots use v2 fingerprints without rewriting persisted plans.

The lineage graph combines native spawn edges, structured source metadata and
top-level `parent_thread_id`/`thread_source` fields. Conflicting parents, roles
and cycles block deletion. An explicitly selected guardian whose parent is
absent can be deleted when its sole rollout matches its native index, the index
independently marks it as a subagent, and the rollout proves one top-level
parent. This exception does not enter automatic orphan cleanup and does not
allow an orphan with remaining descendants.

`delete apply` executes one immutable operation. Native `delete run` may create
fresh plans for supported Desktop residuals within the original frozen record
set, up to three rounds. It never resends an unknown native delete. Each round
has a new operation ID/hash; the run receipt retains every round and verifies
the entire original scope. Partial native results remain residuals, and exact
global-state references count even when the Desktop catalog row is gone.
An empty scan proves completion only for the exact successfully scanned store;
frontend databases additionally require the matching discovery family.

For a full explicit native thread ID, planning also probes exact JSON UI
references when both the native record and Desktop catalog row are absent.
These use `remove_desktop_state` with zero catalog rows and the same frozen
state fingerprints, closed-client guard, rollback and verification. A compatible
Desktop database is still required. Prefix, project and all-projects selectors
do not discover these catalog-free references; verification checks every frozen
descendant ID even if ordinary `records` output no longer lists it.

## Legacy commands

The older `agent doctor/plan/apply/status/verify` commands remain available
for integrations that need one exact `CODEX_HOME` and one mutation family.
They retain their one-store plan hash and explicit client-closed acknowledgement
requirements. They are not a fallback for `delete plan/apply/run`. If the
installed `OperationCoordinator` is unavailable, the new command returns
`operation_api_unavailable` and performs no write.

Legacy agent v1 plans cannot freeze Orca protection locators. New plans and
unattempted applies that discover them return `missing_guard_source_evidence`
and direct callers to the high-level operation surface. Existing agent status/verify
remain read-only, and unknown apply is still refused before scanning guards.
