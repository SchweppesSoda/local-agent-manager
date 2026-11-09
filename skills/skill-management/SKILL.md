---
name: skill-management
description: Create or maintain Skill sources, inspect client discovery, and manage Skill deployment, imports, synchronization, verification, and recovery across local Agent clients.
---

# Skill management

Identify the authoritative Skill source, client, frontend instance, and requested change. Locate `local-agent-manager` and verify its help. `local-agent-manager resources --json` locates the bundled Skills and matching operation documents in editable and installed environments.

- Content or routing edits use [content maintenance](references/content-maintenance.md).
- Deployment, imports, source registration, or global deletion use [registry changes](references/registry-changes.md).
- Client upgrades or loading failures use [client verification](references/client-verification.md).
- Script or dependency changes use [runtime audit](references/runtime-audit.md).

Use `local-agent-manager skills <command> --repo-root <source>` with the source's `config/skill-targets.yaml`, or pass an explicit `--config`. Edit the source and preserve existing user modifications. Reviewed plan IDs govern registry changes. Ordinary content edits use proportional content checks.

Confirm deployment ownership, native discovery, body loading, and frontend menus from their corresponding evidence. Source files, installation detection, and native verification each establish different facts. Runtime-binding changes require a new affected-client check.

The source's static policy controls approved cross-Skill references. A trusted source wrapper may explicitly compose a separate runtime audit. Generic audit does not execute source scripts. Environment installation, PATH, profile, and record tasks use `agent-management`.

Complete the requested local edits and verification. Report the selected source, affected clients, changes, validation, and unresolved gaps. Preserve unowned files and other sources' deployments. Deletion and replacement require authorization for their actual objects.
