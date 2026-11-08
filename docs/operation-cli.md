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
contacted. Without an explicit binding, mutation and verification capabilities are disabled; plan/run/apply
and cold status/verify for full local cleanup remain blocked without dispatching a native writer. See
[Paseo boundaries](adapters.md#paseoagent-注册记录只读盘点).

For `antigravity`, inventory retains `unsupported:antigravity` and reports a
`provider_backend_family_hint` plus `native_cleanup_status=unsupported` in record
metadata. The hint describes the built-in provider family; custom implementations
and effective native homes still require evidence. It does not enable deletion or
make a serialized provider handle a native record ID. See
[Antigravity ownership and cleanup boundaries](adapters.md#antigravity).

### 只删 Paseo 会话

Windows 上的 `--paseo-only` 通过正在运行的本机 Paseo 0.15.3 daemon 删除一条
会话。需要标准用户安装位置的 Paseo 桌面分发包、一个显式的 `--paseo-root` 和
一个完整 Paseo 会话 UUID。Paseo 使用自己的删除接口关闭该会话并移除登记和时间线。
Codex、Antigravity 等 provider 的原生会话保留。该模式覆盖 daemon 的登记与时间线，
桌面离线副本、调度和其他产品的记录在范围之外。

```powershell
local-agent-record-janitor delete plan --client paseo --paseo-only --paseo-root 'D:\PaseoHome' --record-id '<完整 Paseo 会话 UUID>' --out plan.json --json
local-agent-record-janitor delete apply --paseo-only --operation-id '<operation_id>' --plan plan.json --plan-sha256 '<plan_sha256>' --json
local-agent-record-janitor operation verify --operation-id '<operation_id>' --plan plan.json --json
```

apply 使用冻结的 profile 和计划哈希，执行与核验期间保持目标 daemon 运行。
终验复查 daemon 与磁盘登记中的精确 ID。超时、身份不一致或回执后仍有记录时，
执行 `operation status` 和 `operation verify`。结果为 `unknown` 的计划进入只读恢复。
协议和版本边界见 [Paseo daemon 删除](adapters.md#paseo-daemon-删除)。

### Paseo 完整本地操作

Windows 上显式绑定后，可沿用标准 `delete plan/apply` 与 `operation status/verify`。
绑定必须来自实际启动配置，不能由项目 cwd 推断。示例：

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

## T3 Code 完整本地操作

先用 `records --client t3code --json` 获取元数据，或用可重复的 `--t3code-root` 指定
实际 T3 home（包含 `userdata` 的目录，不是 Electron 桌面 profile）。默认使用
`T3CODE_HOME` 或 `~/.t3`。普通 inventory 不执行删除，也不推断原生 provider 根目录。

Antigravity 记录仍使用 `unsupported:antigravity`；元数据的
`provider_backend_family_hint=antigravity-acp` 只描述内置实现线索，
`native_cleanup_status=unsupported` 明确表示原生存储和完整删除范围尚未支持。
T3 的托管运行时、隔离配置、会话文件和前端数据库不能互相替代；参见
[Antigravity 边界](adapters.md#antigravity)。

混合 profile 中没有原生 ID 的 Antigravity provider 仍然可见；将默认 provider 改成
Codex 或提供 Codex 绑定，不能绕过未支持 provider 的完整清理资格检查。

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
    {"thread_id": "t3-thread-id-2", "engine": "claude", "root": "D:/ClaudeConfig"}
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

完整清理只支持固定 schema 的本机 v2 Codex/Claude 记录；附件、弱引用、peer、旧恢复
记录和未证明的共享关系会明确阻挡。工作区、诊断日志、凭据及外部备份保留。
具体边界见 [T3 Code 适配说明](adapters.md#t3-code本机数据库与完整清理)。

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
