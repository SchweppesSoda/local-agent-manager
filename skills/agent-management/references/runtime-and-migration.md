# Runtime diagnosis, repair, and migration

## Read the evidence

Use `environment inventory --json` for discovered roots, candidates and bindings, and `environment doctor --json` for findings and coverage. Bounded discovery may miss installation channels, custom paths, remote environments, and running processes. Partial coverage means an observation was unavailable. Read the probe issue and evidence before deciding whether a candidate is missing or whether a repair is needed. A layout hint can help locate a profile. Active selection needs evidence from the actual shell or frontend.

Codex or app sandbox processes may supply a container `LOCALAPPDATA`. Include the reported `roots` in diagnosis. If they differ from the verified target Windows profile, pass its actual `--home`, `--appdata`, and `--local-appdata` values and constrain PATH to that context. Treat an unobserved application under the wrong roots as unknown.

### Inspect an alternate profile or fixture

If a supplied profile defines the entire scope, pass its actual application-data roots and an empty PATH override. This example uses the default AppData layout within the supplied profile.

```powershell
$profileRoot = 'D:\path\to\profile'
local-agent-manager environment inventory --json `
    --home $profileRoot `
    --appdata (Join-Path $profileRoot 'AppData/Roaming') `
    --local-appdata (Join-Path $profileRoot 'AppData/Local') `
    --path-value=
```

`--path-value=` passes an empty value even in shells that drop empty native arguments. If PATH inspection is requested for the alternate profile, pass the explicit in-scope directory list as `--path-value`. The current shell's PATH belongs to the current launch context.

If the janitor is unavailable or a probe is inconclusive, keep fallback inspection within the supplied scope. Use known candidate locations, exact-path filesystem metadata, and documented nonsecret fields under the supplied roots. Current-host command resolution, running processes, package managers, and registry entries are evidence for a live-host task when that context is in scope. Record unproven selection as unknown.

### Inspect a live launch context

When janitor coverage is insufficient or the command is unavailable, inspect the exact target with native read-only tools. In the intended PowerShell session, check `Get-Command <name> -All` for command selection and `Get-Item -LiteralPath <path>` for an identified file or launcher. Check the package source, desktop launch settings, provider configuration, and documented diagnostics as needed. Inspect only the targeted nonsecret configuration fields. Record a path, version/source, and binding without printing auth data, whole config files, or unredacted process arguments.

A standalone Antigravity CLI, an Antigravity desktop installation, and T3 Code's managed Antigravity ACP pair can coexist. T3 can also select the CLI through a custom `binaryPath` and launch it as `agy acp`; that does not prove it uses the managed ACP profile. Follow the [Antigravity launch-mode and storage checks](antigravity.md) when switching or removing the pair. Resolve each from its own launch context and settings. Apply the same check to PATH Codex, a Desktop-bundled Codex, and an independently configured frontend runtime.

### Windows PATH inheritance

Use this route when a frontend reports it cannot start an installed command, particularly after installation or a PATH change. Windows normally gives a child a copy of its parent's environment. A persistent PATH update does not establish that a running launcher refreshed its own environment. [Windows environment model](https://learn.microsoft.com/en-us/windows/win32/procthread/environment-variables)

Keep four evidence sources separate: the actual installation, persistent user/machine PATH, this task's process environment, and the target launcher/client/server environments. The current `environment inventory/doctor` commands inspect the supplied or calling process's PATH; they do not read persistent PATH or another process's environment. The Python API's optional `process_executables` observations establish executable paths only.

For the affected host and account:

1. Identify the intended executable and its installation channel. Keep a standalone CLI separate from a Desktop-bundled CLI, even when both answer the same command name. Inspect the frontend's effective command or `binaryPath` and data-root setting independently. An explicit command override can make PATH irrelevant to that launch.
2. Inspect just the persistent PATH values and the current shell's command selection. For example, `[Environment]::GetEnvironmentVariable('Path', 'User')` and the `Machine` variant read persistent settings on Windows; `$env:Path` and `Get-Command codex -All` describe this PowerShell context. Compare relevant entries and ordering without logging unrelated environment variables. [Environment variable scopes](https://learn.microsoft.com/en-us/dotnet/api/system.environment.getenvironmentvariable)
3. Identify the actual launcher, frontend, and backend service using executable paths, PIDs, parent relationships, and start times. Recheck process identity if it exits or restarts; a reused PID is not the same observation. A launcher such as uTools may remain in the tray after its window closes. A separately managed service may not inherit from the visible frontend.
4. Use available read-only process inspection to compare the relevant PATH entries in each identified process. Keep a copied environment in memory and project only the fields needed for diagnosis; do not dump the complete block or process arguments. If target environment access is unavailable, report it as unknown. Parent relationships alone do not prove identical environments because a launcher can supply an explicit block.

Choose the finding supported by the evidence:

| Observation | Diagnosis and next step |
|---|---|
| No verified installation candidate within the inspected scope | Installation remains unconfirmed; resolve coverage and the intended install channel. |
| Intended executable exists, but its directory is absent from persistent PATH and no explicit selection applies | Check command registration or the chosen frontend setting. |
| The directory is registered but absent from the target process PATH, with the same account and launch scope established | The target has stale or overridden environment state; inspect the launcher chain to distinguish inheritance from an intentional override. |
| The frontend explicitly selects another executable | Inspect that override; changing PATH alone may not affect it. |
| The target resolves a Desktop-bundled CLI while another context resolves the standalone CLI | Report the two selections and their version evidence; this does not authorize removing either installation. |

An absolute-path override can test command lookup when configuration changes are in scope. Label it as a temporary diagnostic or an intentional final setting. If the user's goal is normal PATH lookup, verify the provider's default command semantics before removing a temporary override. T3 Antigravity's empty Binary path selects its managed-runtime resolver in the qualified builds, so retain an explicit CLI selection when that is the intended runtime. Preserve the chosen data root and account mode. A launch failure by itself does not justify changing `CODEX_HOME`, reinstalling, or resetting authentication.

### Windows file visibility and AppData redirection

Use this branch when the target receives an explicit path but reports `ENOENT` or access failure while the same path works inside the inspecting Agent. Windows can redirect AppData writes into a packaged app's private storage and merge those files into that app's view. Even a normal-looking `LOCALAPPDATA` value and identical absolute path strings can refer to different physical files in different process contexts. [Windows packaged-app file behavior](https://learn.microsoft.com/en-us/windows/msix/desktop/desktop-to-uwp-behind-the-scenes#appdata-operations-on-windows-10-version-1903-and-later)

Establish the actual user's launcher, client and plugin/backend host, including account, package identity and elevation. Compare exact-path visibility in those contexts. For an identified local file, a read-only handle can provide its final path through `GetFinalPathNameByHandleW` and its volume/file identity. These observations belong to the inspecting process. Matching hashes establish matching content, while matching physical file identities can establish a shared file. Keep unavailable observations unknown. [Windows final-path API](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getfinalpathnamebyhandlew)

`CreateEnvironmentBlock` refreshes environment variables. It does not change the new process's package identity or establish the target's filesystem view. Elevation differences are another diagnostic observation, and require the same visibility check. The environment CLI currently reports supplied roots and observed file identities without automatically resolving package virtualization or other processes' final paths.

If installation repair is requested, run the verified installer in the intended user's host context and verify the actual destination there. Test through the user's usual launcher, including the real plugin host when relevant, before removing the old executable. Compare configuration roots by physical identity before deciding whether migration is needed. A shared physical profile can be reused directly. Separate profiles require the [selected-state migration](#migrate-the-selected-state) checks.

### Refresh the affected launch chain

Before restarting a launcher or client, check its active tasks and unsaved state. Use its normal exit action, including tray exit when needed, and verify the relevant background processes stopped. Reopening a window or relaunching a client from the same stale launcher does not establish a refresh. If normal exit requires the user, request that specific action; do not substitute a forced process-tree termination without authorization for its state-loss risk.

Start the affected launcher and client from a verified environment for the intended user. For a programmatic refresh, Windows `CreateEnvironmentBlock` with the appropriate user token and `bInherit=FALSE` can construct a block without inheriting this task's stale or sandbox environment; a null token provides system variables only. Check API success and user profile availability, pass the block with the required Unicode handling, and release it with `DestroyEnvironmentBlock`. Preserve deliberately configured app-specific overrides when they are part of the intended launch. This supplies a new process's environment; it does not rewrite existing processes. [CreateEnvironmentBlock contract](https://learn.microsoft.com/en-us/windows/win32/api/userenv/nf-userenv-createenvironmentblock)

Verify the new launcher, frontend, and server identities and their relevant PATH entries. When a version probe is appropriate, use the trusted command in the observed target environment and working directory, record the resolved executable, version and exit code, and keep inherited secrets out of output. This probe still needs the frontend's own provider check to establish connection. Report executable resolution, provider readiness, authentication, model discovery, and actual generation as separate results; a ready provider and a model list do not establish generation success.

## Repair selection and configuration

Identify the executable and data root the user wants, then locate the setting that actually selects them. A frontend command override, an inherited process PATH, user or machine PATH, and a profile-root override affect different launch contexts. Change the narrowest scope that satisfies the request. Preserve unrelated entries, order, environment expansion, and config keys. Refresh the affected shell or app and re-resolve the executable in that context. Treat a changed selection and a successful provider startup as separate results.

For Windows launch failures after an installation or PATH update, use the [inheritance diagnosis](#windows-path-inheritance) and [launch-chain refresh](#refresh-the-affected-launch-chain) above before deciding which setting needs changing.

## Migrate the selected state

Classify the source and destination per task as settings, sessions/history, attachments, cache, credentials, or shared databases. Confirm the actual source and destination roots, product versions, ownership, and whether an existing destination requires merge or replacement. Use the product's supported export/import or documented migration. Carry only the fields and data selected by the user. Prefer supported sign-in on the destination if credentials are device-bound or outside the migration scope.

For a file-level move, use a consistent snapshot or stop relevant writers before copying and verify the destination before switching its binding. Use proportional temporary rollback for a changed shared database or configuration, with rollback tied to the exact originals and retained until validation succeeds. Rebuildable cache and confirmed discarded residue do not require backup. Remove temporary rollback artifacts after successful validation when consistent with the task's retention scope.

## Verify the requested layer

1. Check package/version or file identity, then resolve the executable in the intended shell or frontend after restart.
2. Check effective config and data-root selection. Verify expected migrated settings or record metadata in the target client. Use storage-qualified identities when checking history.
3. If the task calls for provider verification, use the client's supported runtime/account status or perform the real provider handshake. For a requested generation test, use a minimal message in the intended client and record whether it returned a response. A version string, launcher presence, credential file, and account status each establish different facts from a successful provider response. Keep probes that send requests, create records, or incur costs within the authorized scope.
