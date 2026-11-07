# Runtime diagnosis, repair, and migration

## Read the evidence

Use `environment inventory --json` for discovered roots, candidates and bindings, and `environment doctor --json` for findings and coverage. Bounded discovery may miss installation channels, custom paths, remote environments, and running processes. Partial coverage means an observation was unavailable. Read the probe issue and evidence before deciding whether a candidate is missing or whether a repair is needed. A layout hint can help locate a profile. Active selection needs evidence from the actual shell or frontend.

When janitor coverage is insufficient or the command is unavailable, inspect the exact target with native read-only tools. In PowerShell, check `Get-Command <name> -All` for command selection and `Get-Item -LiteralPath <path>` for an identified file or launcher. Check the package source, desktop launch settings, provider configuration, and documented diagnostics as needed. Inspect only the targeted nonsecret configuration fields. Record a path, version/source, and binding without printing auth data, whole config files, or unredacted process arguments.

A standalone Antigravity CLI, an Antigravity desktop installation, and T3 Code's Antigravity ACP launcher can coexist. Resolve each from its own launch context and settings. Apply the same check to PATH Codex, a Desktop-bundled Codex, and an independently configured frontend runtime.

## Repair selection and configuration

Identify the executable and data root the user wants, then locate the setting that actually selects them. A frontend command override, an inherited process PATH, user or machine PATH, and a profile-root override affect different launch contexts. Change the narrowest scope that satisfies the request. Preserve unrelated entries, order, environment expansion, and config keys. Refresh the affected shell or app and re-resolve the executable in that context. Treat a changed selection and a successful provider startup as separate results.

## Migrate the selected state

Classify the source and destination per task as settings, sessions/history, attachments, cache, credentials, or shared databases. Confirm the actual source and destination roots, product versions, ownership, and whether an existing destination requires merge or replacement. Use the product's supported export/import or documented migration. Carry only the fields and data selected by the user. Prefer supported sign-in on the destination if credentials are device-bound or outside the migration scope.

For a file-level move, use a consistent snapshot or stop relevant writers before copying and verify the destination before switching its binding. Use proportional temporary rollback for a changed shared database or configuration, with rollback tied to the exact originals and retained until validation succeeds. Rebuildable cache and confirmed discarded residue do not require backup. Remove temporary rollback artifacts after successful validation when consistent with the task's retention scope.

## Verify the requested layer

1. Check package/version or file identity, then resolve the executable in the intended shell or frontend after restart.
2. Check effective config and data-root selection. Verify expected migrated settings or record metadata in the target client. Use storage-qualified identities when checking history.
3. If the task calls for provider verification, use the client's supported runtime/account status or perform the real provider handshake. For a requested generation test, use a minimal message in the intended client and record whether it returned a response. A version string, launcher presence, credential file, and account status each establish different facts from a successful provider response. Keep probes that send requests, create records, or incur costs within the authorized scope.
