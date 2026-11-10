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
