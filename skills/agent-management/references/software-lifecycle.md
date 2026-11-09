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
