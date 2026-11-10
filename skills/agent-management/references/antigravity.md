# Antigravity on its own, in Paseo, or in T3 Code

## Attribute runtime and storage

Use `environment inventory --json --client antigravity --client paseo --client t3code` when all three are in scope. Review `roots`, runtime candidates, configuration bindings, and coverage. Use the explicit profile and PATH overrides in [runtime diagnosis](runtime-and-migration.md#inspect-an-alternate-profile-or-fixture) when needed. When the report is incomplete, inspect the exact paths and only the nonsecret fields needed for attribution.

- **Antigravity CLI.** Resolve the selected `agy` executable in the relevant shell or daemon. Its normal profile is `~/.gemini/antigravity-cli` under the selected OS user directory. Settings, the workspace resume cache, the shared conversation index, per-session files, and generated artifacts have different roles. The presence of CLI profile files cannot attribute a specific conversation to the shell or to Paseo. [CLI settings](https://antigravity.google/docs/cli/using/) · [CLI resume cache](https://antigravity.google/docs/cli/commands/resume#under-the-hood-the-session-cache)
- **Paseo built-in provider.** Inspect the effective daemon profile and `agents.providers.antigravity.command` in its `config.json`. An explicit path shows configured selection. With default lookup, resolve the daemon's actual PATH if authorized. Account and profile also depend on the launch context. The Paseo agent ID and the Google CLI conversation ID identify different layers. A `.bak` provider directory remains a layout candidate until current binding and provenance establish its role. [Paseo provider configuration](https://github.com/getpaseo/paseo/blob/ef7de3d066256901396e1f47d9e309d569589b2c/public-docs/supported-providers.md#antigravity)
- **T3 Code Antigravity.** Identify the environment, provider instance and launch mode. The default managed pair is selected under `tools/antigravity-acp/<platform>-<arch>/`; its runtime is shared while credentials and sessions use `userdata/providers/antigravity/<instance-hash>/antigravity-acp/`. Nightly `0.1.8-nightly.20261108.4353` also accepts a CLI 1.5.0+ binary override, automatically launching `agy acp` without a helper. In this mode the executable is still the CLI; `acp` names its service subcommand and protocol, not a second installed runtime. Inventory reports `launch_mode_hint=cli_acp` for recognized CLI names; the name alone is not executable identity. Keep that mode's native root unknown until verified. [T3 CLI support](https://github.com/pingdotgg/t3code/pull/32503) · [T3 instance environment](https://github.com/pingdotgg/t3code/blob/3cd97a1ee230437b1bfd8441dffe65eac55be1e5/apps/server/src/provider/antigravityAuthSupport.ts#L171-L247)
- **CLI ACP storage caveat.** Passing `GEMINI_HOME` is not proof the executable honors it. The qualified Windows CLI 1.5.3 build created `<USERPROFILE>/.gemini/antigravity-cli/conversation_summaries.db` in an isolated test despite T3-style `GEMINI_HOME` and `AGY_ACP_FORCE_FILE_STORAGE=1`. It did not use the old instance directory. Native data and credentials may therefore be shared with the standalone CLI. Do not promise isolated accounts, automatic history migration or resumed-thread compatibility. Verify other builds independently; the repository's `tests/test_t3code_runtime_acceptance.py` pins this observation to the exact build.
- **Other launchers.** Identify an installed Antigravity App, IDE, custom Paseo plugin, or Google `agy acp` launch separately. Google documents cached CLI credentials for `agy acp`. That evidence applies to the CLI mode. T3's managed ACP provider has its own profile. Presence of a directory, auth file, or managed binary establishes only that observation. [Google CLI ACP](https://antigravity.google/docs/ide/extensions/acp/)

For credential-location questions, distinguish the managed instance's
`antigravity-acp/acp_token.json` from CLI authentication. The CLI can use the OS
credential store, including Windows Credential Manager, depending on its build
and authentication mode. Check current Google authentication guidance and
supported account status. A missing JSON file alone cannot establish sign-out,
and an old managed token does not establish CLI authentication. Inspect file
presence and nonsecret status without displaying credential contents.
[Google CLI authentication](https://antigravity.google/docs/install?tab=cli#local-silent-keyring-sign-in)

## Diagnose T3 CLI selection and permission modes

In the qualified T3 builds, an empty Binary path uses managed ACP resolution.
CLI support does not make it automatically select the standalone `agy`. Set the
verified CLI executable explicitly when that is the user's chosen runtime.
T3 appends `acp`. Check actual launch selection after updates or provider changes.
If an explicit path works only inside the inspecting Agent, use the
[Windows file-visibility checks](runtime-and-migration.md#windows-file-visibility-and-appdata-redirection).

If the CLI starts but rejects `auto_edit` or `yolo`, check the installed T3
permission-mode implementation. Official PR #33143 maps those managed mode IDs
to CLI `default`, with T3 answering approval requests from its live policy.
Verify that the installed release includes that fix and test the requested
permission modes separately. A prior local patch or a merged PR alone does not
establish the installed behavior. [T3 permission-mode fix](https://github.com/pingdotgg/t3code/pull/33143)

## Identify the Magpie integration

Magpie's Agent integration for running a CLI through Magpie and the `agy-cli`
supplier plugin for serving models from that CLI have separate entry points.
Identify the selected integration before changing settings. The supplier
plugin supports `AGY_PATH`, and its Bun/plugin host must receive the intended
path and be able to open it. A shell environment override affects that shell
and its children. Use the installed plugin's documented configuration for a
persistent override. [Magpie supplier plugin](https://github.com/magpie-community/plugins/tree/main/packages/agy-cli)

Verify through the user's actual Magpie launch chain. A saved sign-in entry,
quota response, model list and generated reply each establish a separate
result. When the desktop/plugin host cannot open a path that works in this
task, compare physical file visibility, package context and elevation before
choosing a repair. An Agent-launched replacement instance may have a different
filesystem view.

## Match the action to the requested layer

- **Standalone CLI native cleanup.** Use `records/delete --client antigravity` with the actual CLI root, full conversation UUIDs and an explicit verified `--antigravity-bin`. The qualified Windows writer freezes session databases, summaries and protobuf/JSON mirrors, workspace resume cache, artifacts and known child IDs. It uses mandatory file leases, shared operation journals and status/verify. T3/Paseo history or restore references, opaque relationships and unknown schemas block. It does not delete frontend records or qualify their combined CLI cleanup; never substitute the old managed ACP root. Read the repository's `docs/operation-cli.md` section “独立 Antigravity CLI 原生清理” before live use. Settings, credentials, workspaces and diagnostic logs remain. Unknown operations retain recovery images and cannot be replayed.

- **Inspection.** Report the host/profile, observed runtime and version evidence, effective binding if known, and whether each storage path is a candidate or tied to a selected record. `storage_artifacts` identifies layout metadata. In the frontend record report, `provider_backend_family_hint` describes the built-in integration family and `native_cleanup_status` shows the available native capability. Confirm the actual instance and store independently. Keep credential values, command-line arguments, and transcript bodies out of diagnostic output. Start a provider process only if the user has included that layer of verification.
- **CLI session removal.** Follow the matching janitor operation contract for actual record work. If the user has authorized removal through the official CLI, open the correct CLI version, host, user/profile and workspace. In `/resume`, select the exact conversation ID in the CLI tab and use the deletion shortcut shown by that version. Current docs show `F4` and a confirmation. Reopen the picker in the same profile to verify the visible result. Record the limited verification, including any unresolved frontend reference, imported original, related local artifacts or remote copy. Later CLI releases changed cleanup behavior for child histories and workspaces, so review the installed version before acting. [Google's CLI delete UI](https://antigravity.google/docs/cli/commands/resume#deleting-a-conversation) · [CLI release changes](https://github.com/google-antigravity/antigravity-cli/blob/099f43f0b0b7a9ffe17b549d80aac1f1ff6e2bb5/CHANGELOG.md#148)
- **ACP or frontend history.** T3 `--t3code-only` uses the built-in exact-ID deletion, detaches and preserves the worktree, and preserves native sessions, including mixed Antigravity profiles. Full deletion is separate: Janitor qualifies Windows T3-managed ACP 1.4.5 with explicit instance/runtime bindings, strong native IDs, frozen database schemas and exact session files under held writer exclusion. Unknown parent/subtrajectory relationships block; settings, credentials, skills, runtimes and workspaces remain. Follow the repository operation contract and `docs/operation-cli.md` for both modes. This does not qualify standalone CLI or Paseo native cleanup. ACP `session/delete` is optional, permits soft deletion and is not advertised by this qualified runtime. Report frontend and native results separately. [ACP v1 delete contract](https://agentclientprotocol.com/protocol/v1/session-delete) · [T3 temporary probe cleanup](https://github.com/pingdotgg/t3code/blob/e455200077ce60806a01c8f28435b58860dcfc74/apps/server/src/provider/acp/AntigravitySessionFiles.ts#L5-L41)
- **CLI joint history.** T3/Paseo full cleanup supports the qualified Windows CLI 1.5.3 build through an explicit `native_layout=cli` binding, actual OS user home and executable evidence. Read the manager's `docs/operation-cli.md` joint CLI section. Paseo requires the built-in `plugin:` persistence envelope and matching `pluginProviderPersistence` object; old custom ACP formats remain blocked. The same plan freezes native descendants and selected frontend references while retaining unselected profiles and restore sources. Partial execution stays unknown with before-images and native-root journal occupancy; use the original plan's status/verify, never replay. Managed ACP bindings retain their separate path. `--t3code-only` preserves native history; a soft-deleted row can still protect it. Do not invent ACP `session/delete`; the tested CLI 1.5.3 does not advertise it.
- **Sign-in or runtime removal.** Use the selected product's account controls or installer. T3 exposes separate controls to disable an instance, sign out, and remove its shared downloaded runtime. Confirm consumers before removing the runtime. T3 documents that these controls retain thread history and workspace files. Runtime removal does not authorize deleting credentials or old instance data. [T3 account and removal behavior](https://github.com/pingdotgg/t3code/blob/e455200077ce60806a01c8f28435b58860dcfc74/docs/user/providers-antigravity.md#accounts-and-removal)

## Switch T3 to the CLI and remove the managed runtime

1. Verify the T3 environment and CLI 1.5.0+ installation. Set the provider's Binary path to the verified CLI executable; T3 appends `acp`. Prefer `agy` or an absolute path over the ambiguous `antigravity` name, which can select an IDE launcher. Refresh provider status and models. Report version, authentication, model discovery and generation separately.
2. Check active work and disable all instances consuming the managed pair. Use T3's **Remove downloaded runtime** control or its official local service interface. In Nightly `20261108.4353`, the service rejects removal while the selected instance has any custom binary path, including an external CLI path. Temporarily clear that path while disabled, remove the managed runtime, then restore the CLI path and enable the intended instance. Prepare restoration before removing anything and restore the working CLI configuration even if removal fails. Do not sign out as part of this procedure.
3. Verify the managed platform directory and installer pointer are absent, the CLI remains installed, and the restored provider reports its CLI version and account/model status. Leave old native records, credentials, skills and extraction caches outside the runtime uninstall scope unless separately requested and qualified. Never delete the whole T3 home or provider profile to remove a runtime.

[T3 installation service](https://github.com/pingdotgg/t3code/blob/3cd97a1ee230437b1bfd8441dffe65eac55be1e5/apps/server/src/provider/providerInstallation.ts) · [Managed runtime removal](https://github.com/pingdotgg/t3code/blob/3cd97a1ee230437b1bfd8441dffe65eac55be1e5/apps/server/src/provider/AntigravityInstallation.ts#L846-L899)

## Distinguish uninstall results from discarded instance data

When asked whether residues were cleared, report each inspected scope separately:

| Scope | What completion establishes |
|---|---|
| Managed runtime and installer pointer | The old server/helper installation was removed. |
| Runtime extraction cache | Its observed files or empty directories were assessed separately. |
| Old provider instance | Its settings, credentials and history remain unless the user selected that instance data for disposal. |
| Active CLI profile, T3 settings and shared skills | These remain independently owned and must retain the intended state. |

Before planning disposal of an entire obsolete instance, establish its exact absolute path, current consumers, and the active CLI's actual data root. A missing managed binary, a different configured command, or an old directory timestamp alone does not prove that the instance is disposable. Include credentials and records in the stated scope when they are present; they are not rebuildable cache. Use the user's existing explicit disposal scope rather than requesting the same decision again.

Inspect directory links without following them. For an instance-local `config/skills` junction pointing to shared skills, disposal concerns the link itself, not the shared target. Freeze the selected file/link inventory, exclude live writers, and compare retained CLI data, T3 configuration and shared targets before and after any authorized operation. Unknown ownership, unresolved operation journals, changing contents or unverified writer exclusion block mutation; an unknown result requires recovery rather than a blind retry. Report remaining files and empty directories from observed evidence, not from the uninstall result alone.

Whole-instance disposal is distinct from exact-record deletion and from `--t3code-only`. Follow the applicable record operation contract when records are involved. Janitor currently exposes no generic profile-retirement command; a task-specific disposal script or a successful historical cleanup does not add that capability or qualify other profiles. Do not reuse an old manifest, authorization hash, process observation or machine-specific path for a later task.

### Retired T3 database records

After a managed instance has been retired, Janitor supports exact database
cleanup through a `larj.t3code-antigravity-database.v1` manifest passed with
`--t3code-bindings`. Read the matching checkout's operation contract and the
"已退役 Antigravity 的 T3 数据库清理" section of `docs/operation-cli.md` for the
binding and plan/apply/verify route.

This requires the selected instance to be disabled with an empty custom binary,
its managed runtime and instance directory removed, and each full T3 thread ID
explicitly selected. Threads must already be soft-deleted, with no active
sessions or worktrees, and all frozen dependency checks must pass. The scope is
the selected shared-database records. Recheck retirement for each new plan.
Do not clear an active CLI binding to manufacture retirement eligibility.
