---
name: skill-management
description: Create, review, and maintain Skills and their sources; manage discovery, imports, deployment, synchronization, verification, and recovery across local Agent clients.
---

# Skill management

Manage the Skill lifecycle across personal, project, shared, and packaged sources. `work-skills` is one independent source; `mount-work-skills` is its compatibility entry. Source-specific paths, wrappers, runtimes, and automation permissions belong to that source.

Identify the requested operation and authoritative source first. For client-facing changes, also identify the actual client, frontend instance, and discovery scope.

- Skill creation, content review, routing, or resource maintenance use [content maintenance](references/content-maintenance.md).
- Deployment, imports, source registration, or global deletion use [registry changes](references/registry-changes.md).
- Client upgrades or loading failures use [client verification](references/client-verification.md).
- Script or dependency changes use [runtime audit](references/runtime-audit.md).

Content work can proceed directly in the selected Skill source. Use `local-agent-manager` for supported source audits, deployment transactions, and client evidence; verify the relevant command's help when needed. `local-agent-manager resources --json` locates its matching management Skills and operation documents. Deployment configuration is required for managed deployment operations, not for ordinary Skill authoring.

For deployment and loading work, confirm ownership, native discovery, body loading, and frontend menus from their corresponding evidence. Source files, installation detection, and native verification each establish different facts. Runtime-binding changes require a new affected-client check.

The source's static policy controls approved cross-Skill references. A trusted source wrapper may explicitly compose a separate runtime audit. Generic audit does not execute source scripts. Environment installation, PATH, profile, and record tasks use `agent-management`.

Complete the requested local edits and proportional verification. Report the selected source, changes, validation, unresolved gaps, and affected clients when relevant. Preserve existing user modifications, unowned files, and other sources' deployments. Reviewed plan IDs govern registry changes; deletion and replacement require authorization for their actual objects.
