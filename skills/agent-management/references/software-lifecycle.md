# Agent software lifecycle

## Resolve the product and installation

Record the host and user, requested product, form (desktop, CLI, bundled executable, or ACP backend), package or installer identity, current version and channel, install path, scope, and known consumers. Resolve these independently on each host. Inspect command resolution and launch configuration first. Use the manager's read-only listing or app package details for ownership.

On Windows, `Get-Command <name> -All` shows the current PowerShell resolution order. Installed Apps, WinGet's list/show, or the relevant manager can establish package identity. For an npm global install, inspect the package listing and prefix of the npm installation that owns the shim. For a packaged desktop app, check its package identity and app settings. For a portable copy, prove the exact directory and find the shortcut or consumer configuration pointing there. Treat launchers, symlinks, shims and their targets as separate evidence until ownership is established.

Check current official product documentation for the requested channel, platform, package ID, supported migration path, and any downgrade or uninstall behavior. Select the exact package and channel in the owning manager or official installer. Preserve the original channel for routine updates and repair unless the requested change calls for a channel switch. Use the agent app's documented updater when the app manages its own installation.

## Perform the requested change

- **Install.** Install the chosen product in the requested scope with its verified installer or package-manager identity. Inspect how the new command resolves alongside existing installs. Point a frontend at the new runtime when that binding is part of the request.
- **Update.** Update the identified installation through its owning channel. Compare package identity and launch resolution before and after. If an update succeeds but a shell or frontend still starts another copy, trace the binding before making another package change.
- **Uninstall.** Resolve active consumers and user data before removing the exact package. If a selected frontend would lose its only known backend, resolve that dependency within the user's scope before uninstalling. Use the package manager, official uninstaller, or Installed Apps. If the request includes residual caches, verify the exact paths, ownership, and that the paths are rebuildable or explicitly discarded. Route record stores through the record operation contract. Keep unrelated clients, shared profiles, and provider credentials outside the uninstall scope unless the user selected them.

Use the exact paths and identities again when checking the result. Refresh the shell or frontend if it inherited old environment variables. For uninstall, check package and launcher absence separately and check that any intended retained consumer still resolves. For install/update, verify version and selection, then verify provider use at the layer requested by the user. Follow [runtime and migration](runtime-and-migration.md) for verification steps.

Report uninstall scope precisely: program files, extraction caches, retained profiles and shared resources have separate outcomes. Removing a runtime does not establish that its old instance data was removed. For T3 Antigravity, use the [instance-data scope checks](antigravity.md#distinguish-uninstall-results-from-discarded-instance-data) before treating an old profile as a selected disposal target.

## Audit accumulated backups and runtime artifacts

Start with metadata-only environment inventory and the exact owning profiles,
including configured and shared consumers. Project path identities, sizes,
producer/selection evidence and coverage errors; do not print configuration,
credentials, transcripts or process command lines. Skip reparse targets and
handle long paths and files disappearing during an active scan explicitly.
Equal bytes, old timestamps, a `.bak` suffix or a `cache` directory do not prove
that an object is disposable.

Keep current fallback slots, migration/cloud-memory backups, unresolved
rollback evidence and permanent coordination locks distinct from historical
orphaned copies. Use `state inventory/cleanup` only for manager-owned evidence.
Use the owning lifecycle workflow for runtimes/downloads, Skill management for
sources/deployments, and the record contract for sessions and checkpoints.
Never remove a current runtime, managed Python environment or node_modules
tree just because another installation exists.

Use the matching repository's `docs/state-maintenance.md` for the implemented
`maintenance inventory/plan/apply/run/status/verify` workflows and category
qualification. Prepare exact targets and evidence under the user's audit scope.
Production disposal, updater policy changes and stopping clients follow the
existing authorization for those objects and actions.

## AI-reviewed runtime retirement

Use `maintenance inventory --client <owner> --root <physical install/profile root>
--category runtimes --json`. Inspect the actual launcher and installer selection,
PATH, frontend/plugin/daemon bindings, running executables and rollback roles.
Make these judgments from the observed host context and explain unresolved
coverage. Keep unresolved or shared selections and required rollback versions.

Generate the `lam.runtime-review.v1` document described in the matching
`docs/state-maintenance.md`. Classify every candidate as remove or preserve using
its exact path and `snapshot_sha256`, and cite relevant configuration files,
direct installer pointers and absent override slots. The operating AI authors
the assessment and JSON. The tool binds it to the current local evidence.
For absent override slots, identify every actual writer and cite its native
Windows executable as a reference with `"writer": true`. The tool leases these
images and checks that their processes are closed. Preserve affected versions
where writer coverage remains unresolved.
Use `maintenance plan/run --category runtimes --runtime-review <file>` for the
authorized scope and `maintenance status/verify` for recovery. Resolve stale
snapshots through fresh inventory and assessment. Apply requires the plan hash,
unchanged retained/runtime/selector evidence and Windows file exclusion.

Use scoped client/updater closure when those leases require it. Verify selected
files are gone and retained launch bindings remain usable. Installed runtime
cleanup and source implementation have separate target scopes.

## Standalone CLI installation on Windows

Run the vendor installer or package manager in a normal Windows host shell for the intended user, outside the packaged Agent's private or sandboxed environment. Before running an installer, verify the account, effective AppData roots, and the destination for the chosen install scope. Keep the standalone executable and its command entry point in the vendor-supported host locations, outside desktop app caches and sandbox directories. If the host shell is unavailable, give the user the exact installer command and the context in which to run it.

From a fresh shell in that user's normal launch context, check `Get-Command <name> -All`, resolve any shim to its executable, and verify the physical install path and version. If a frontend will use the CLI, check resolution in that frontend's actual launcher or backend context as well. For discrepancies, follow [Windows PATH inheritance](runtime-and-migration.md#windows-path-inheritance) and [AppData redirection checks](runtime-and-migration.md#windows-file-visibility-and-appdata-redirection).
