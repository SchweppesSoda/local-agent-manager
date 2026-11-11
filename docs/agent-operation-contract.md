# Agent record operation contract

Use the non-interactive `records`/`delete`/`operation` surface for live records.
Do not drive human `clean`/`purge` prompts or use legacy `agent` commands as a
fallback when the operation API is unavailable.
For long inventory, deletion or verification commands, add `--progress` and
surface its stderr phase/count events while keeping stdout as the JSON result.

## Read for the selected task

- High-level inventory or deletion: [Commands](operation-cli.md#commands) and
  [Output](operation-cli.md#output), including its linked result contract.
  For official native stores, also read
  [Official Desktop inventory and completion](operation-cli.md#official-desktop-inventory-and-completion).
- Native local-environment registrations: also read
  [native project cleanup](native-project-cleanup.md); project IDs are not thread IDs.
- Standalone Antigravity CLI: read [native CLI cleanup](operation-cli.md#独立-antigravity-cli-原生清理).
  Use its exact conversation ID and actual CLI root, including when shared by
  T3/Paseo. Frontend IDs and the legacy managed ACP home do not authorize it.
- Joint T3/Paseo and native CLI deletion: read [joint CLI binding](operation-cli.md#t3paseo-与-antigravity-cli-联合清理).
  Use the selected frontend ID, qualified runtime/home, and frozen native child closure;
  unselected frontend references remain protection sources. Partial execution is unknown, never replayed.
- Existing legacy `agent` integrations: read [Commands](agent-automation.md#commands)
  and [Result contract](agent-automation.md#result-contract) instead of the high-level route.
- Recovery evidence or rollback copies: read
  [Operation evidence and receipts](agent-automation.md#operation-evidence-and-receipts)
  and [Temporary rollback copies](agent-automation.md#temporary-rollback-copies).
- Manager state cleanup or layout migration: read [State maintenance](state-maintenance.md).
- Client cache, diagnostic log, historical backup or download maintenance uses `maintenance inventory/plan/apply/run/status/verify` and the qualified rules in [State maintenance](state-maintenance.md#client-artifact-maintenance).
  Use its explicit-root preview/apply commands and retain the permanent lock
  and migration marker at their original location.

## Required agent workflow

1. Select one exact client and its physical stores. Official Codex/ChatGPT
   Desktop uses `native`/`codex-native` with the official native `CODEX_HOME`,
   never a Cindy substitute. Inventory and reports expose metadata only;
   do not read or expose chat bodies.
2. Review plan targets, counts, blockers, progress groups and `plan_sha256`.
   Each immutable child batch owns one physical store and mutation family.
   `delete plan/run` performs preflight; `agent doctor` is optional. Structured
   blockers must be resolved; unsupported backends or unproven schemas remain inventory-only.
3. Apply only within the user's frozen authorization. Disk cleanup requires
   the owning clients to be closed. [Paseo daemon deletion](operation-cli.md#只删-paseo-会话)
   and [T3 built-in deletion](operation-cli.md#只删-t3-会话) require the selected local
   service to be running and an explicit plan hash. These frontend-only modes
   preserve provider history; T3 also detaches and preserves the worktree.
   Never invent a hash or `--clients-closed` acknowledgement.
   Repeated scope selectors must match the full frozen scope. Within unchanged
   authorization, proceed without a second confirmation. Exact frontend-reference
   and relation writes require supported schemas, immutable row evidence,
   exact affected-row counts and post-write verification.
   Cleanup authorization is not lifecycle authorization. Before closing or
   restarting any Agent, show the user a dialog listing the exact software and
   affected tasks and obtain explicit confirmation for the current run and
   lifecycle identities. Without that confirmation, do not arm or close.
   The `lifecycle_confirmation.LifecycleGate` support entry enforces in-memory,
   expiring consent, exact scope/run/identity matching and single-use actions;
   it does not itself operate clients. New callers must supply fresh identities
   immediately before dispatch and retain normal startup fences/exit checks.
   Old frozen controllers and on-disk arm receipts do not satisfy this gate:
   preserve them as evidence, never replay or edit them to manufacture consent.
4. Cindy retained chat-row deletion requires explicit Cindy session IDs via
   `--record-id`; native SDK IDs do not authorize it. Project/all-projects
   cleanup retains its soft-deleted-only default.
5. Decide from `goal_status`, `goal_satisfied`, structured blockers and exit
   codes, never human message text. Only verified `complete` satisfies the goal.
6. If a mutation result is `unknown`, never repeat it: run `operation status`,
   then `operation verify` (legacy integrations use `agent status/verify`).
   Preserve the exact plan/state location. Never edit a frozen plan or journal,
   manually delete/bypass `apply.lock`, or discard unresolved recovery evidence.
   The explicit joint CLI reservation recovery below is a narrowly qualified
   verification-only product protocol, not permission to reclaim other locks.
   When a parent plan is missing, the supported native v1 child-only
   `operation status/verify` route can use the original child journal; see
   [the operation CLI recovery boundary](operation-cli.md#commands).
7. Further work after verification needs a fresh plan and must remain within
   the user's authorized scope. Newly discovered actions are not authorized by
   an old plan. Report residuals only from authoritative discovery evidence;
   otherwise report the capability boundary. `remote_delete=false` permits no remote writes.

Keep the record, or delete the whole verified record and every approved copy;
never offer `repair_index_path` or `quarantine_artifacts`. Shared-file rollback
copies are temporary and removed immediately after successful verification; receipts are
not backups. Recovery must prove the frozen state before completion or evidence removal.

The permanent `operations/.mutation.lock` serializes cooperating processes for
one trusted local root, from journal admission through mutation and result
publication. Verification and receipt cleanup use the same root lock. Never
delete or replace it. This root lock does not prove that an older writer holding
`apply.lock` has exited; that operation remains unknown even with a terminal
receipt. Unknown journals have no expiry. Missing or untrusted journal scope
blocks the whole affected root. Exact frozen IDs and artifact paths allow
independent targets and stores to proceed when their scopes do not overlap.
The qualified Orca v3/child-v2 startup boundary occupies the whole selected home:
another record in that home remains blocked while startup/teardown is unknown.
Its original named Job must be absent on the same machine and Windows session
before read-only recovery can publish a known result.

For an interrupted **in-process joint Antigravity CLI reservation** after every
parent child has a bound, fully verified completion result, the supported
`operation verify --recover-joint-reservations --authorized-plan-sha256 <original>`
route can retire that reservation's lock after offline verification. It checks
the exact reservation/parent/child plans, original kernel process exit (live or
reused PIDs and access denial block), census descendants, exclusive lock handle,
native runtime leases and frontend startup boundary. This writer performs native
file writes synchronously, with no delegated writer or native Job. App-server,
Orca startup Job and all other journal locks are outside this recovery protocol.
The root mutex and a receipt alone remain insufficient. See
[joint reservation recovery](operation-cli.md#joint-cli-reservation-recovery)
for after-state and evidence retention requirements.

Legacy direct CLI/GUI and service writers share this admission gate, so they
cannot bypass an existing journal's unknown outcome. They do not create a new
operation journal themselves; durable recovery of a new direct-call timeout is
not provided by this gate. Use the journaled operation surface for that
guarantee. There is no global lock or replay guarantee across different native
roots, unproven bridges, or older versions that do not use the root mutex.
