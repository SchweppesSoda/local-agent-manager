# Manager state maintenance

`state inventory`, `state cleanup`, and `state migrate` operate on the manager's
local recovery metadata in one explicit store root. Select the physical root
separately for official Codex, Cindy's SDK home, and Cindy's frontend store.
Follow the host-context checks in the Agent management skill when AppData is
redirected by a packaged application.

```powershell
local-agent-manager state inventory --root '<physical store root>' --json
local-agent-manager state cleanup --root '<physical store root>' --json
local-agent-manager state cleanup --root '<physical store root>' --apply --plan-sha256 '<preview hash>' --json
local-agent-manager state migrate --root '<physical store root>' --json
local-agent-manager state migrate --root '<physical store root>' --apply --plan-sha256 '<preview hash>' --json
```

## Inspection and cleanup

Inventory and previews validate plans, checkpoint/event bindings, terminal
results, receipts, and ordinary file boundaries. They preserve journal bytes
and expired receipts. Access to an existing state root acquires its permanent
OS coordination lock and creates that lock if this predates root locking.
Inspecting a root with no manager state creates nothing.

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
blocked command or pending migration. JSON is emitted with or without `--json`.

## Explicit layout migration

Before migration, journal and temporary rollback paths use
`<root>/.local-agent-record-janitor`. Migration moves validated terminal
operation directories to `<root>/.local-agent-manager/operations`, preserving
their exact bytes, IDs, hashes, and event sequence. It requires all journals
to be resolved and all other legacy control entries to be cleared through
their owning recovery workflow. An unmarked destination is a conflict.
The command preserves it for inspection. A historical terminal child checkpoint
with validated bindings and final events can migrate intact even when its
original writer did not publish a result document. Cleanup retains those
details until supported verification produces a result.

Migration keeps these entries at their original location permanently.

```text
<root>/.local-agent-record-janitor/operations/
    .mutation.lock
    .manager-layout.json
```

An existing `<root>/.local-agent-record-janitor/legacy-index.lock` also stays
at its original location. Upgraded index writers share the root lock and
continue using this original index lock. Maintenance additionally acquires
an existing index lock to exclude older standalone index writers.

The lock retains its original identity and continues to serialize new and
older cooperating processes. The ordinary JSON marker makes earlier journal
scanners reject new mutations. Upgraded readers and writers use the new
operation and rollback paths after the completed marker is published. A
cached upgraded store object rejects access when its layout changes. Any new
journal appearing in the old directory blocks access pending inspection.
Do not remove the lock or marker. This migration leaves a small coordination
directory at the old name. It creates no junction or directory symlink.

Before the first move, a durable marker records the exact approved manifest.
Until every journal has been moved and checked, normal operations fail closed.
`state inventory` reports the original migration hash. Resume with
`state migrate --apply --plan-sha256 '<original hash>'`. Resume validates the
complete manifest on both sides before moving anything else. Missing,
duplicated, changed, or unexpected evidence blocks completion. Successful
migration replaces the manifest with a small layout marker containing the
root, destination, and migration hash.

Recovery by exact operation ID and store root follows the new layout. An old
exact journal plan path also resolves through the completed marker. Frozen
operation documents and original plans remain byte-for-byte intact. An
unexecuted record-deletion plan can still require regeneration when its
implementation fingerprints have changed during a manager upgrade.

## Client artifact maintenance

`maintenance inventory/plan/apply/run/status/verify` handles qualified client
artifacts through the shared operation store and mutation admission. Inventory
supports explicit profiles and known default or environment roots for Cindy,
Codex, Codex desktop, Claude, Pi, T3, Paseo, Antigravity CLI and desktop,
WorkBuddy, Orca, Herdr and QwenWork. A missing candidate reports its observation
scope. Custom, portable and additional account profiles require an explicit root.
The offline artifact writer currently supports Windows file exclusion.

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

| Category | Executable qualification |
|---|---|
| `caches` | Chromium blockfile caches in registered cache locations, recognized index magic/version and ordinary known members. Other cache formats are reported as unqualified. |
| `logs` | Dated or rotated diagnostics with a qualified Cindy NDJSON or tracing/electron-log header, past retention. Current files and nested session/terminal diagnostics retain their record ownership. |
| `backups` | Dated Cindy SQLite backups with a recognized migration marker, healthy compatible current database and two healthy newer generations. Codex atomic fallback temporary generations require valid current and fallback JSON slots. Both generation time and mtime must satisfy retention. |
| `downloads` | Old unselected electron-updater temporary downloads with qualified pending metadata and frozen selected installer/base dependencies. Pending installers, packages and differential bases retain their updater roles. |
| `runtimes` | Recognized versioned install units, with an AI-authored review of ownership, selection, shared consumers and rollback. The plan freezes selected units, retained replacements and cited selector/configuration evidence. |

The default retention period is 14 days. Cindy backup pruning keeps the newest
two generations and validates their health before pruning older generations.
Current fallback slots, native resume caches, checkpoints, memory, credentials
and manager rollback evidence retain their owning workflows. `state cleanup`
handles manager recovery metadata.

Cache qualification follows Chromium's
[blockfile format](https://github.com/chromium/chromium/blob/main/net/disk_cache/blockfile/disk_format.h).
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
Use the physical install root for Claude (`~/.local/share/claude`) or Codex
desktop (`<local appdata>/OpenAI/Codex` or `Programs/OpenAI/Codex`). Runtime
inventory includes these known install-root candidates. Other layouts retain
their existing observation/qualification limits. Native sessions, plugins and
managed environments follow their owning workflows.

Inventory tokens bind metadata and membership. Planning adds content hashes
for removed and preserved units, and freezes cited references, including
absence and direct-link identities/targets. Apply checks the complete inventory
again, leases all selected and preserved files and present references, and
excludes direct-pointer replacement through Windows handles. A pointer naming
a selected removal target blocks planning. Changed files, selectors, new
versions and newly present overrides require a fresh plan. A changed candidate
snapshot requires renewed AI assessment. Linked/hard-linked runtime units,
chained pointers and traversal/hash budgets prevent affected qualification.
Version-container membership is frozen and rechecked after leases and during
final verification. New members in a retired unit keep recovery unresolved.
The 14-day cache/log retention setting grants no runtime disposal authority.
Runtime trees have a separate 4 GiB hashing budget per unit and the shared
512 MiB per-file and 20,000-member traversal budgets.

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
| `state inventory/cleanup/migrate` | Journal validation, exact preview hash, permanent root locks, terminal receipt compaction | Qualification of nonempty historical rollback directories; never infer their outcome from age |
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
