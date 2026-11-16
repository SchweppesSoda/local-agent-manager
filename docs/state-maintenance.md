# Manager state maintenance

All manager state lives in one manager-owned directory, never inside an
Agent's own data directory. Each physical store root gets its own
subdirectory there for its journals, coordination lock, legacy index lock,
temporary rollback copies and T3 desktop cleanup journals. Top-level plans
and `delete run` receipts (`<operation-id>.json`, `<operation-id>.run.json`)
go to its `plans/` directory and T3 `--handoff` evidence to `handoffs/`.

One editable installation of `local-agent-manager` is the authority for the
selected Python installation. Its `direct_url.json` registration identifies
the repository; state is always that repository's Git-ignored
`.agent-manager-state`. All commands and subprocesses use that same registered
checkout, even when source is imported from another checkout or the working
directory changes. `LAM_STATE_DIR` does not select another coordination domain.
Missing, non-editable or duplicate registrations in the selected installation
are rejected before a record operation can create state.

Use one Python installation and one authoritative editable checkout per machine.
This is not a registry shared by independently installed Python interpreters.
Inventory/resource-only package use does not require another journal location;
wheel-only record operations have no supported state authority.

```text
<state directory>/
    plans/<operation-id>.json, <operation-id>.run.json
    handoffs/<handoff-id>/
    <store key>/.local-agent-manager/
        operations/.mutation.lock
        operations/<operation-id>/
        legacy-index.lock
        desktop-state-backups/, legacy-index-backups/, desktop-cleanups/
```

The store key is derived from the store's canonical path, so the same store
always maps to the same subdirectory. Keep this directory out of Git and
build-output cleanup, and use one state directory for every writer of a
store. Repointing the installation is an explicit administrative operation;
preserve its existing state before changing the registered repository.
`store.json` binds each partition to its exact physical store. It is published
atomically under the permanent root gate and every writer validates an existing
binding. An incomplete or mismatched binding fails closed.

`state inventory` and `state cleanup` operate on one
explicit store root. Select the physical root separately for official Codex,
Cindy's SDK home, and Cindy's frontend store. Follow the host-context checks
in the Agent management skill when AppData is redirected by a packaged
application.

```powershell
local-agent-manager state inventory --root '<physical store root>' --json
local-agent-manager state cleanup --root '<physical store root>' --json
local-agent-manager state cleanup --root '<physical store root>' --apply --plan-sha256 '<preview hash>' --json
```

## Inspection and cleanup

Inventory and previews validate plans, checkpoint/event bindings, terminal
results, receipts, and ordinary file boundaries. They preserve journal bytes
and expired receipts. Access to a store with manager state acquires its
permanent OS coordination lock. Inspecting a root with no manager state
creates nothing.

Cleanup applies the exact current preview hash while holding the root lock.
It compacts validated terminal journals into the existing seven-day metadata
receipt format, finishes interrupted receipt compaction, and removes expired
receipts. A changed journal or newly eligible expired receipt changes the plan
hash and requires a fresh preview. A newly compacted receipt is retained for
seven days from compaction. Cleanup can be interrupted after some items finish.
Inspect and preview the remaining state before continuing.

Unknown outcomes, missing or inconsistent results, unexpected files,
unrecognized schemas, linked/reparse paths, and every existing `apply.lock`
are retained. They have no time-based cleanup rule. Inventory reports
`recovery_required` with the specific reason. Use supported `operation status`
and `operation verify` recovery before previewing again. Cleanup can remove
empty recognized rollback containers. Nonempty rollback directories and other
unmanaged entries are reported and preserved.

The maintenance result uses `lam.state-result.v1`, its `phase`, `modified`,
`completed`, `retained`, `removed_empty_directories`, and nested plan/after fields. A cleanup phase of
`complete` means the eligible maintenance actions finished. Inspect `retained`
and `unmanaged_entries` for remaining evidence. A CLI failure reports
`modified=null` because some earlier maintenance actions may have finished.
Inventory reports use `lam.state-inventory.v1`. Exit codes are `0` for a
successful report or application, `2` for invalid arguments, and `3` for a
blocked command. JSON is emitted with or without `--json`.

## Current layout and retained historical files

The current store-key layout and persisted `larj.*` journal/receipt schemas are
unchanged. Existing state in the authoritative repository remains readable.
The old project command/package aliases, per-user plan fallback, in-store
journal lookup, layout-marker recovery and `state route/migrate/relocate`
commands have been removed. Historical files are not moved or removed
implicitly. Review any older installation's outstanding operations separately
before retiring that installation; their old locations are not an alternate
coordination domain for current commands.

## Client artifact maintenance

`maintenance inventory/plan/apply/run/status/verify` handles qualified client
artifacts through the shared operation store and mutation admission. Inventory
supports explicit profiles and known default or environment roots for Cindy,
Codex, Codex desktop, Claude, Pi, T3, Paseo, Antigravity CLI and desktop,
WorkBuddy, Orca, Herdr and QwenWork. Browser-skill diagnostics are discovered
separately at the actual user's `.bsk` root; daemon locks and control directories
remain retained. A missing candidate reports its observation scope.
Cindy discovery includes its SDK `codex-home`, qualified
`codex-accounts/<owner>/<account>` homes (reported as client `codex`), and the
Windows updater WebView profile. Custom and other portable profiles require
an explicit root. Native account homes remain separate from frontend stores;
their presence never grants authority to delete sessions.
The offline artifact writer currently supports Windows file exclusion.

Official Windows Codex MSIX profiles are discovered at the package's `LocalCache`
root. Cache discovery includes `Roaming/Codex/web/Codex` and its `Default`
profile, `codex-browser-app`, and each direct named `Partitions` profile;
an explicit older `Roaming/Codex` root also supports `web/Codex`.
Cindy/T3/Paseo share direct partition discovery; Cindy also includes `EBWebView`.
Only registered cache directories and format-qualified members qualify,
never cookies, browser local storage, artifact sessions or arbitrary descendants.
Access-denied or sharing-violation observations report
`file_in_use_or_access_denied`; they do not establish an unsupported format.

Codex diagnostics in `Local/Codex/Logs/YYYY/MM/DD` are inspected separately.
Only ordinary files with the qualified `codex-desktop-<UUID>-<PID>-t<N>-i<N>-<HHMMSS>-<N>.log`
name, a matching dated diagnostic header, and both directory date and mtime
past retention qualify. Unknown subdirectories, session files and mismatched
headers remain retained. Explicit `Local/Codex` roots support the same dated
layout under `logs`. Reports expose file metadata only. The usual frozen
manifest, client closure, file leases and verification rules still apply.

Native Codex `.sandbox/sandbox.YYYY-MM-DD.log` requires a matching dated
Codex diagnostic header and retention; `.sandbox` itself is never a removal
target. T3 rotated `server.trace.ndjson.N` and `desktop.trace.ndjson.N` require
a recognized Effect or OTLP span envelope;
the current trace and terminal transcripts remain retained.

WorkBuddy diagnostics support named root log envelopes and direct files inside
`logs/YYYY-MM-DD`: workspace logger headers, edge-sync, memory and extension
scheduler diagnostics. Unknown nested SDK/session, migration, update and crash
data stays retained. Browser-skill `daemon.log.YYYY-MM-DD` requires its dated
Rust tracing envelope and `bsk::` target, or a verified empty file. Date and mtime
must both pass retention. Root locks, current/recent files and unknown bodies
do not qualify. These producers still require normal closure/file exclusion;
an inactive app's old diagnostic log can qualify even without a rotation suffix.

Native discovery catalogs under the four known `cache/` namespaces require
recognized JSON schemas, hashed filenames and retention. Plugin exclusion
settings and unknown cache namespaces remain retained. T3 usage scan cache
v11 similarly requires its exact envelope and retention; later versions are
reported as unqualified. These rules do not read bodies into reports.

Plugin extraction trees, Chromium component archives, Electron preload caches,
crash reports, diagnostic SQLite databases and manager state are visible
retained observations with an explicit qualification/recovery reason. They
are not automatically disposable. Paseo schedules, agent requests and creation
receipts belong to the record recovery workflow, including atomic copies.

```powershell
local-agent-manager maintenance inventory --client cindy --json
local-agent-manager maintenance plan --client cindy --root '<physical profile root>' --category caches --category logs --plan maintenance.json --json
local-agent-manager maintenance apply --plan maintenance.json --plan-sha256 '<reviewed hash>' --clients-closed --json
local-agent-manager maintenance run --client cindy --category logs --older-than-days 14 --clients-closed --json
local-agent-manager maintenance status --root '<physical profile root>' --operation-id '<operation ID>' --json
local-agent-manager maintenance verify --plan maintenance.json --json
```

Plan and run require explicit categories. An omitted root selects a unique
observed profile. Multiple profiles produce a scope blocker. `run` freezes,
accepts and executes its own plan in one invocation for an already authorized
scope. `apply` binds the reviewed plan hash. Client closure acknowledgement
accompanies actual OS leases on every selected file and recovery dependency.

For reviewed busy/protected non-runtime artifacts, repeat `--exclude-path
'logs/exact-file.log'` on inventory, plan or run. Each argument is one exact
profile-relative artifact, not a glob or directory prefix. The exclusions are
recorded in both scope and evidence, rechecked during apply, and only remove
otherwise eligible actions. They never widen qualification. Use a fresh plan
after identifying an unavailable file; do not modify its ACL to make a batch
pass. Runtime exclusions instead use the review's `preserve` decisions.

| Category | Executable qualification |
|---|---|
| `caches` | Chromium blockfile caches; Simple v9 `Code Cache/js` and `wasm` with verified index checksum, v5 entry headers/footers and exact membership; known native discovery/usage cache schemas past retention. Unknown formats remain unqualified. |
| `logs` | Dated or rotated diagnostics with a qualified Cindy NDJSON or tracing/electron-log header, past retention. Current files and nested session/terminal diagnostics retain their record ownership. |
| `backups` | Dated Cindy SQLite backups with a recognized migration marker, healthy compatible current database and two healthy newer generations. Codex current/fallback atomic temporary generations, including empty interrupted writes, require valid current and fallback JSON slots. Both generation time and mtime must satisfy retention. |
| `downloads` | Old unselected electron-updater temporary downloads with qualified pending metadata and frozen selected installer/base dependencies. Pending installers, packages and differential bases retain their updater roles. |
| `runtimes` | Recognized versioned install units, with an AI-authored review of ownership, selection, shared consumers and rollback. The plan freezes selected units, retained replacements and cited selector/configuration evidence. |

The default retention period is 14 days. Cindy backup pruning keeps the newest
two generations and validates their health before pruning older generations.
Current fallback slots, native resume caches, checkpoints, memory, credentials
and manager rollback evidence retain their owning workflows. `state cleanup`
handles manager recovery metadata.

Cache qualification follows Chromium's
[blockfile format](https://github.com/chromium/chromium/blob/main/net/disk_cache/blockfile/disk_format.h)
and [Simple entry format](https://github.com/chromium/chromium/blob/main/net/disk_cache/simple/simple_entry_format.h)
with its [index checksum](https://github.com/chromium/chromium/blob/main/net/disk_cache/simple/simple_index_file.cc).
Temporary download qualification follows electron-updater's
[download staging](https://github.com/electron-userland/electron-builder/blob/master/packages/electron-updater/src/AppUpdater.ts)
and [temporary-file selection](https://github.com/electron-userland/electron-builder/blob/master/packages/electron-updater/src/DownloadedUpdateHelper.ts).

Plans freeze member lists, identities, hashes, implementation fingerprints and
SQLite sidecar presence or absence. Apply acquires every file lease and rechecks
missing sidecar slots before its durable mutation marker. It disposes only the
approved files and leaves directory containers available to their producers.
New files receive no approval from a directory name.

An interrupted attempt reports `unknown` and cannot replay. Read-only artifact
verification resolves exact absence or unchanged residual files, preserving an
unknown result when dependencies or residual identities changed. Known residuals
require a fresh plan. Status and verify support original journal plan paths,
external plan copies and root/operation-ID locators after journal compaction.
An outstanding `apply.lock` keeps the outcome unknown, including a compacted
completion receipt. Unreadable recovery evidence reports nullable mutation flags.

Reports use `lam.maintenance-inventory.v1` or `lam.maintenance-result.v1` and list
retained and unqualified observations. A complete result covers the approved
eligible files. Unmeasured residual counts and reclaimed physical capacity are
null. Logical bytes describe observed file sizes. Hard links, redirects, unknown
layouts, access errors and traversal budgets prevent affected qualification.
Unqualified runtime units include metadata-only logical size and file count
when measurable, including unversioned sandbox executables. Unknown sizes are
null with a measurement reason, never zero. This accounting does not qualify
the unit for deletion. Reconcile inventory with the requested directories'
measured usage; registered artifact coverage is not a complete profile size audit.
Inventory, ready plans and completed eligible actions return 0. Blocked, unknown
and verified residual outcomes return 3. Argument errors return 2.

### AI-reviewed runtime retirement

The operating AI inspects runtime candidates and the host's actual launch
configuration, then writes a `lam.runtime-review.v1` document. This separates
the contextual decision from mechanical file validation. Product versions and
binary hashes come from the current observation and approved plan. Compatible
upgrades require a fresh observation and review using the same workflow.

```powershell
local-agent-manager maintenance inventory --client cindy --root '<physical profile root>' --category runtimes --json
local-agent-manager maintenance plan --client cindy --root '<physical profile root>' --category runtimes --runtime-review runtime-review.json --plan maintenance.json --json
local-agent-manager maintenance apply --plan maintenance.json --plan-sha256 '<reviewed plan hash>' --clients-closed --json
local-agent-manager maintenance run --client cindy --root '<physical profile root>' --category runtimes --runtime-review runtime-review.json --clients-closed --json
```

The AI prepares this document under the user's established scope. The user can
ask to clean obsolete runtimes in plain language. The AI handles the commands,
evidence and JSON. A source-development request authorizes source work;
disposal of installed programs and closing clients follow the user's selected
maintenance scope.

```json
{
  "schema_version": "lam.runtime-review.v1",
  "client": "cindy",
  "root": "C:\\Users\\example\\AppData\\Roaming\\CindyGlobal",
  "assessment": {
    "ownership": "These are Cindy-managed Claude version directories.",
    "selection": "The inspected launcher selects the retained version.",
    "consumers": "The inspected frontend configurations and PATH use the retained version.",
    "rollback": "Keep the selected version and the required rollback generation."
  },
  "remove": [
    {"path": "claude-code/2.1.10", "snapshot_sha256": "<inventory snapshot_sha256>", "reason": "Superseded version with no observed consumer or rollback role."}
  ],
  "preserve": [
    {"path": "claude-code/2.1.20", "snapshot_sha256": "<inventory snapshot_sha256>", "reason": "Selected shared runtime."}
  ],
  "references": [
    {"root": "C:\\Users\\example\\.t3", "path": "userdata/settings.json", "reason": "Inspected frontend runtime selection."}
  ]
}
```

Classify every candidate in the selected root and retain a replacement for
each engine being pruned. Inspect installation/selection metadata, shell and
frontend launch bindings, running processes, shared consumers and required
rollback roles. Cite the relevant metadata files, direct installer pointers
and absent override slots in `references`. An unresolved consumer or selector
keeps the affected version in `preserve`. Explain the observed coverage in
the assessment. The AI's assessment establishes semantic disposal eligibility.
The tool validates its scope and evidence bindings. The operating AI remains
responsible for the factual accuracy and coverage of its assessment.

An absent reference requires lifecycle exclusion of its actual writers. Add
each producer's native Windows executable to `references` with `"writer": true`
and explain its ownership. The tool validates a PE image, holds its exclusive
file lease and checks the Windows process snapshot after acquiring all leases.
Any running process with a declared writer's basename blocks execution. The AI
establishes the full set of relevant writer executables, including detached
updaters or helpers. Where writer coverage or exclusion remains unresolved,
preserve the affected runtime. An absent slot without an executable-writer
binding blocks planning.

Candidate discovery covers Cindy Codex packages/legacy versions, Claude and Pi
version directories, standalone Codex release directories, Claude native
versions, Codex desktop versioned bins and T3 platform/version runtime units.
Codex installer units recognize supported Rust target triples (including
`x86_64`), both `app-server-daemon` and legacy `app-server` roles, and versioned
`.sandbox-bin/codex-command-runner-*.exe` units, plus the exact unversioned
`.sandbox-bin/codex.exe` and `.sandbox-bin/codex-command-runner.exe` copies. Unknown names are reported,
not silently skipped. Active pointers, separate daemon selection and helper
consumers still require review; discovery is not retirement approval.
Use the combined native Claude root `~/.local` to cover both
`share/claude/versions/<version>` and `bin/claude.exe.old.<13-digit timestamp>.<pid>`.
The current `bin/claude.exe` is a separately reviewed retained candidate;
unrelated bin tools and unknown backup names are excluded. Direct version-store
roots (`~/.local/share/claude`) remain supported. Use the physical root for Codex
desktop (`<local appdata>/OpenAI/Codex` or `Programs/OpenAI/Codex`). Runtime
inventory includes these known install-root candidates. Other layouts retain
their existing observation/qualification limits. Native sessions, plugins and
managed environments follow their owning workflows.

Inventory tokens bind metadata and membership. Planning adds content hashes
for removed and preserved units, and freezes cited references, including
absence and direct-link identities/targets. Apply checks the complete inventory
again, leases all selected and preserved files and present references, and
excludes direct-pointer replacement through Windows handles. A pointer naming
a selected removal target blocks planning unless it is explicitly included in
the narrowly scoped daemon retirement below. Changed files, selectors, new
versions and newly present overrides require a fresh plan. A changed candidate
snapshot requires renewed AI assessment. Linked/hard-linked runtime units,
unbound or cyclic pointer chains and traversal/hash budgets prevent affected
qualification. Installer aliases such as `bin -> current/bin` are supported
only when every intermediate link is explicitly cited in the review. The tool
freezes each link and the final physical target, then holds every link until
disposal finishes; an unlisted link or changed intermediate selector blocks it.
Version-container membership is frozen and rechecked after leases and during
final verification. New members in a retired unit keep recovery unresolved.
The 14-day cache/log retention setting grants no runtime disposal authority.
Post-operation verification permits the absence of exact approved single-file
runtime units from their container. It still checks every retained member and
rejects additions or unapproved missing members. Preflight requires the original
membership; interrupted verification never replays deletion.
Runtime trees have a separate 4 GiB hashing budget per unit and the shared
512 MiB per-file and 20,000-member traversal budgets.

For a stopped standalone daemon that the user explicitly retires, mark each
present `packages/app-server-daemon/{current,auto-update-version,selection-history}`
reference with `"retire": true`. All daemon release units must be selected for
removal; `current` must resolve to one of them. Unknown role members, incomplete
selector coverage, arbitrary retired reference paths and another retained
pointer to a removed runtime block planning. Cite `install.lock` as a normal
retained reference when present. Coordination locks and native daemon records
are outside the retirement scope. The role's member list is frozen as well.

This retires the stale installation selection, not just its payload. The writer
leases the current link with reparse-point DELETE access and unlinks that link
through its held handle, never traversing it for deletion. Selection metadata
is included in the ordinary approved file count; `approved_pointer_count`
reports retired links separately. Verification permits only those approved
objects to be missing. An unchanged surviving pointer is an explicit residual;
changed or newly introduced selectors keep recovery unresolved. The durable
mutation flag and no-replay rule also cover interrupted selector retirement.

Execution removes the exact approved runtime files and retains empty directory
containers. Verification checks their absence and retained dependencies.
An interruption uses the existing maintenance recovery workflow and cannot
replay the deletion attempt. Retained runtime and selector leases can require
closing their owning clients and updater within the authorized scope.

The remaining client-specific qualification work follows the design below.

### Reuse and gaps

| Existing entry | Reuse | Missing capability |
|---|---|---|
| `environment inventory/doctor` | Physical roots, installation/layout evidence, configured runtime bindings, observation coverage | Bounded storage accounting, backup roles, updater selection/rollback slots, complete consumer graph; WorkBuddy/Orca/Herdr/QwenWork environment discovery |
| `state inventory/cleanup` | Journal validation, exact preview hash, permanent root locks, terminal receipt compaction | Qualification of nonempty historical rollback directories; never infer their outcome from age |
| `delete plan/apply/run`, `operation status/verify` | Frozen approval, mutation admission, event/result binding, unknown-outcome protection, final evidence | Distinct artifact-maintenance mutation families and client rules, not reuse of session-delete authority |
| Software lifecycle skill | Owning installer/updater/uninstaller, active version and consumers | Format-qualified download/pruning adapters when the owner exposes no suitable operation |
| Skill management | Source/discovery bindings, shared resources, deployment journals | Vendor plugin-cache version/refcount rules; a plugin or Skill source is not disposable merely because it is cached |

The shared component returns observations for explicit or discovered roots
without executing discovered binaries. Further adapters need root and file
identity, source/owner, category and evidence,
logical and allocated-byte coverage, links skipped, scan races/errors/budgets,
consumer and recovery dependencies, and a decision of `retain`, `unqualified`
or `eligible_for_plan`. An observed byte count is not reclaimable capacity.
Deduplicate physical file identities and report hard-link savings separately;
do not combine overlapping profile roots or equal-content copies as savings.
Use Windows long-path-safe traversal; a disappearing active cache entry is an
incomplete snapshot, not an absent installation.

### Qualification rules

| Category | Evidence needed before planning removal | Always retain or block |
|---|---|---|
| Automatic database backup | Producer/schema, exact source DB, successful migration/current health, no restore/fallback pointer, no unresolved journal; explicit backup-disposal scope | Current DB/WAL/SHM, unverified migration, only recoverable copy |
| Config/migration/cloud-memory backup | Known generation and owner, successful replacement/sync evidence, recovery relation and retention decision | Current fallback slot, credentials, memory/source content without explicit disposal scope |
| Current automatic fallback | Producer's stable slot semantics and revision identity | Retain even when old/new bytes hash equally; a writer can replace the same bytes |
| Manager rollback evidence | Validated operation result plus supported native verification; approved recovery workflow | `apply.lock`, permanent `.mutation.lock`, unknown/partial operations, corrupt/missing results |
| Download/extraction residue | Owning installer/updater, completed install receipt, no pending update, selected rollback/base archive, lock or consumer | `pending` by name alone is not stale; current differential-update base may be reused |
| Managed old runtime | AI assessment of installer ownership, current/previous selections, frontend/plugin/daemon consumers and processes; exact unit snapshots and cited configuration/pointer evidence | Active/shared runtime, unresolved consumers/selectors, separate app-server daemon/runtime roles |
| Rebuildable cache | Product-specific producer and rebuild path, no user/session state, no active writer, exact frozen inventory | Whole `sessionData`, `Local Storage`, IndexedDB, cookie stores and plugin sources |
| Logs/crash dumps | Diagnostic owner, resolved incident/retention decision, closed writer and no recovery references | Open files, terminal transcripts participating in record recovery, evidence still needed for an unresolved incident |

Electron's [sessionData](https://www.electronjs.org/docs/latest/api/app#appgetpathname)
contains both caches and persistent state. Its
[HTTP-cache API](https://www.electronjs.org/docs/latest/api/session#sesclearcache)
does not authorize clearing all browser storage. Likewise Claude's
[file-history and session data](https://code.claude.com/docs/en/claude-directory#application-data)
include checkpoint restore inputs. Keep those in the record/recovery domain;
do not change the vendor's retention settings as part of an audit.
Use the owning [Codex installer](https://github.com/openai/codex/blob/main/scripts/install/install.ps1)
as evidence for the installed layout, then verify that the local implementation
and selection metadata match; upstream `main` alone is not local qualification.
Electron-updater separately tracks a
[pending download](https://github.com/electron-userland/electron-builder/blob/master/packages/electron-updater/src/DownloadedUpdateHelper.ts)
and uses a [current installer as a differential-update input](https://github.com/electron-userland/electron-builder/blob/master/packages/electron-updater/src/NsisUpdater.ts).
Two equal-size installers therefore require role and pending-state verification,
not a generic duplicate-file deletion rule.

### Shared execution and recovery

1. Discover and classify without content output or write authorization. Report
   missing installation records, inaccessible roots, unobserved profiles and
   unsupported formats separately. An environment override adds a profile to
   protection coverage; it cannot silently remove another known consumer.
2. Qualify only the selected artifacts using small client rules for producer,
   schema, current/rollback selection and dependencies. Freeze the full exact
   path set, revision identities and hashes, retained objects, consumer evidence,
   capacity upper bound and implementation contract. New files never inherit
   approval from a directory name. Return precise blockers and a reviewable plan.
3. Apply through the common operation journal and root locks. Prefer an owning
   API; an offline writer additionally needs actual OS exclusion, including
   updater and shared-runtime consumers. `--clients-closed`, PID absence and an
   advisory lock alone are insufficient. Never stop clients implicitly.
4. Prepare durable before images only where restoration is necessary. Record
   the irreversible attempt before deletion. Verify preserved identities and
   selections, exact absence, database health where relevant, and the owning
   application's limited offline reload/smoke contract. Report observed released
   capacity separately from planned logical bytes.
5. A caught reversible write failure restores through held handles. Crash or
   partial deletion remains `unknown`; status/verify cannot silently replay it.
   Quarantine or rollback copies consume space and are not reclaimed bytes.
   Clear them only after qualified completion, never by changing journal state.

The common engine should provide bounded traversal, dependency edges, frozen
artifact plans, handle leases, conditional restoration and receipts. Client
modules provide format/producer rules. Do not implement a separate recursive
delete script per client or grant arbitrary paths to existing session writers.

### Client coverage to implement

| Client | Existing evidence/record integration | Additional artifact rules required |
|---|---|---|
| Cindy | Global/frontend and Codex/Claude/Pi roots, runtime candidates, record writers | DB migration backups, connection/cloud-memory backup state, binary download receipts, owner/account profiles, shared browser/runtime consumers |
| ChatGPT/Codex desktop | Bundle/profile candidates and native record/desktop-state writers | Exact MSIX package roots beyond bounded directory listing, primary runtime vs app vs native CLI, plugin/marketplace generations, active UI fallback slot |
| Codex CLI | Native root, standalone candidate, native records and manager evidence | Installer pointer/daemon selection and sandbox helper versions; preserve permanent locks and remote/VPS configuration backups |
| T3 | Default/explicit roots, provider selection, native/frontend cleanup | Updater pending/base archives, usage caches vs terminal logs, provider extraction and current/retired managed runtime evidence |
| Paseo | Default/explicit roots, providers, daemon/frontend/native cleanup | Provider `.bak` migration state, scheduler/atomic/creation/receipt recovery, updater/package ownership |
| Claude Code | Native/Cindy candidates, configured home, native record cleanup | Current config backup rotation, orphaned temp writer evidence, plugin-cache references, native installer selection; checkpoint files remain record data |
| Pi | Standalone/Cindy home hints and native records | Package-manager provenance and extensions/runtime dependencies; sessions and exported artifacts remain record data |
| Antigravity | Standalone/IDE/managed ACP attribution and qualified native writers | Native resume/summary/annotation/presence dependencies, CLI updater and MCP/runtime artifacts, IDE-specific backup/cache qualification |
| WorkBuddy | Exact native record adapter | Environment discovery; bundled Python/Node/Git, managed envs/node_modules, migration/sync mappings, plugin/connector and updater ownership |
| Orca | Profile/account-root discovery and native/frontend cleanup | Environment installation inventory, managed account/runtime selection, shared frontend cache and startup/recovery copies |
| Herdr | Default/dev roots, daemon metadata and bound native/frontend cleanup | Environment installation inventory, detached daemon consumers and backup/update semantics |
| QwenWork | Qualified frontend/SDK record stores | Environment installation inventory, channel profiles and shared Qoder SDK, browser state vs cache, updater metadata |

Prioritize download/updater artifacts and format-qualified cache/log retention
by observed capacity, then obsolete runtime consumers and historical backup
qualification. Resolve unknown journals independently even when small. Do not
count large session stores, memory, temporary test dependencies or active
managed environments as savings. Host-specific measured paths, sizes and
version conflicts belong in the audit handoff, not permanent matching rules.
