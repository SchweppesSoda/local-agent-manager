# Registry changes

1. Identify the source, actual consumer and selected target. Run `local-agent-manager skills clients detect --repo-root <source>`.
2. Run `local-agent-manager skills plan --repo-root <source> --target <adapter>` and review every action and blocker.
3. Apply the same arguments and exact reviewed ID with `local-agent-manager skills apply --repo-root <source> --target <adapter> --plan-id <id> --yes`.
4. Read back the deployment, then perform affected native loading checks.

The source owns its manifest and transaction journal. Shared target roots and discovery files use cross-source locks. Other sources' entries and unrelated external links are preserved. Same-name Skills in the actual discovery scope are blocking conflicts.

`--create-only` permits CREATE_LINK only. Source registration, adoption, replacement, import, and deletion need separately reviewed plans. Existing unattended authorization belongs to its original source workflow.

Global deletion requires named `--delete-skill` values and `--confirm-global-delete` on plan and apply. The recovery bundle and journal preserve original paths and link primitives. Skill deletion does not unregister an entire source.

Discovery file replacement records its byte before-image and expected after-image before writing. Recovery checks current content before restoring. External edits require review of retained recovery material. Journals support exception rollback and manual interrupted-process recovery.
