# Registry changes

Use the selected source's deployment configuration: `local-agent-manager skills <command> --repo-root <source>` reads its `config/skill-targets.yaml`, or pass an explicit `--config`. Check the relevant command's help. Source registration and deployment changes use this workflow; ordinary Skill content edits use [content maintenance](content-maintenance.md).

1. Identify the source, actual consumer and selected target. Run `local-agent-manager skills clients detect --repo-root <source>`.
2. Run `local-agent-manager skills plan --repo-root <source> --target <adapter>` and review every action and blocker.
3. Apply the same arguments and exact reviewed ID with `local-agent-manager skills apply --repo-root <source> --target <adapter> --plan-id <id> --yes`.
4. Read back the deployment, then perform affected native loading checks.

The source owns its manifest and transaction journal. Shared target roots and discovery files use cross-source locks. Other sources' entries and unrelated external links are preserved. Same-name Skills in the actual discovery scope are blocking conflicts.

`--create-only` permits CREATE_LINK only. Source registration, adoption, replacement, import, and deletion need separately reviewed plans. Existing unattended authorization belongs to its original source workflow.

Global deletion requires named `--delete-skill` values and `--confirm-global-delete` on plan and apply. The recovery bundle and journal preserve original paths and link primitives. Skill deletion does not unregister an entire source.

Discovery file replacement records its byte before-image and expected after-image before writing. Recovery checks current content before restoring. External edits require review of retained recovery material. Journals support exception rollback and manual interrupted-process recovery.

Native client sync trash is distinct from this manager's recovery bundle. For
an explicitly authorized cleanup of `.claude/skills/.trash`, inspect the actual
sync producer's current manifest, completed-round marker, deployed bodies and
resources. Confirm that each selected old copy has a healthy current replacement
and no pending restoration role. Keep current source/deployment paths, active
staging, coordination files and manager recovery journals out of that scope.
Use exact frozen file identities and membership, writer exclusion and a receipt
for the approved native copies; do not invoke global Skill deletion or remove
an entire discovery root. Names or age alone do not prove a completed sync.
