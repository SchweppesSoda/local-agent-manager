---
name: agent-management
description: Operate local coding Agent installations and record stores. Use to inventory or troubleshoot installed apps, CLIs, bundled runtimes, ACP launchers, provider connections, PATH or profile settings, install, update or remove Agent software, migrate Agent settings or data, or inspect and clean local Agent conversations. Use separate workflows for Agent app source development and unrelated Windows maintenance.
---

# Agent management

Identify the host, Windows user or other OS account, Agent client, and intended scope. Keep these identities separate when they differ.

- An installed app or package has a distribution channel and installation scope. Its runtime may be bundled, resolved through PATH, or launched by a frontend as an ACP/provider backend.
- An observed executable, the shell's selected command, a frontend's configured command, and a running provider process each require their own evidence.
- A data root and record ID identify local storage. A provider account can be shared by independent installations or profiles. An auth file establishes file presence. Use supported account status or runtime evidence for authentication.

Skill deployment and source maintenance use `skill-management`. Client installation and runtime repair remain here.

## Choose the workflow

- **Inventory or diagnose.** Locate the manager command and run `environment inventory --json` or `environment doctor --json`. Use repeated `--client` to narrow discovery. When an alternate profile or fixture defines the whole scope, use explicit root and PATH overrides. Read [runtime and migration](references/runtime-and-migration.md) for the scoped example, inconclusive probes, PATH/config repair, profile moves, or functional verification.
- **Installed CLI cannot start from a client.** Follow [Windows PATH inheritance](references/runtime-and-migration.md#windows-path-inheritance) to compare persistent settings with the launcher, client, and server environments. For an explicit path that works only in this task, check [Windows file visibility](references/runtime-and-migration.md#windows-file-visibility-and-appdata-redirection) in the user's actual launch context.
- **Install, update, or uninstall.** Read [software lifecycle](references/software-lifecycle.md). Use the confirmed package manager, official installer, or app UI for the chosen product and channel. For a standalone Windows CLI, establish the target user's host shell before running the installer.
- **Inspect or clean records.** Locate the matching operation contract with `local-agent-manager resources --json` or find its source checkout, read its `docs/agent-operation-contract.md`, and follow its task-specific reading route. Use `records` for discovery and the supported `delete plan/apply` or `delete run` plus `operation status/verify` for deletion. The contract defines the scope, authorization, verification, and recovery for each target. Environment findings can guide store discovery. The record operation must establish deletion authority independently. If the store remains unknown or the product is unsupported, report the unresolved scope.
- **Antigravity.** For Antigravity CLI or App/IDE and its Paseo or T3 Code integrations, read [Antigravity runtime and storage](references/antigravity.md) to determine the actual launch context, profile, and supported action.
- **Connect Claude or Cindy state to another client.** Read [Claude and Cindy connections](references/claude-connections.md) for independent runtime/home selection, official history import, and selected project memory through original files or an exported snapshot. Establish and verify each requested connection separately.

Carry actions the user already authorized through scoped verification. Ask only when a materially different target or scope needs a decision, or a required fact cannot be resolved. Follow the current host's tool permissions and the product's operation contract.

## Locate the manager

Use the installed `local-agent-manager` command if it supports the requested operation. In PowerShell, use `Get-Command local-agent-manager` to identify the selected entry point. If only the Python package is installed, use `python -m local_agent_manager` with the interpreter that owns it. Run `--help` to verify the entry point and commands.

If a command or its contract is missing, look in the current Git root, the task's known checkouts, or the source location of an editable installation. Run this in the relevant interpreter to locate the installed module.

```powershell
python -c "import importlib.util as u; spec = u.find_spec('local_agent_manager'); print(spec.origin if spec else '')"
```

Identify the checkout by the `pyproject.toml` project name `local-agent-manager` and `src/local_agent_manager`. For a source checkout, temporarily prepend its `src` to the process's `PYTHONPATH` and run the same Python module, then restore the previous value. A fresh host may have a different checkout path or only an installed CLI. For environment diagnosis, use the read-only fallback in [runtime and migration](references/runtime-and-migration.md). If record work still lacks the matching contract or executable, ask for that concrete missing location.
