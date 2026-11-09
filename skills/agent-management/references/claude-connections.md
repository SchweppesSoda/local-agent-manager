# Reuse Claude and Cindy state in another client

Use this workflow when connecting a chosen Claude executable, native history,
or Cindy project memory to another Agent client. These are three independent
choices. A request to implement or test connection support does not ask to
connect the current host. Use temporary profiles and synthetic records for
development; do not import the user's real history as an acceptance test.

T3 can also use an independently installed Codex executable with Cindy's
Codex home. Its Binary path and CODEX_HOME path settings select those two
resources separately. Verify the standalone executable and the exact Cindy
`codex-home` before saving the binding. The selected home also supplies Codex
configuration and authentication storage. Cindy's project memory uses its own
`maker-memory` scope and needs the memory-loading step below.
[T3 Codex configuration](https://github.com/pingdotgg/t3code/blob/v0.1.8-nightly.20261109.4399/docs/user/providers-codex.md)

## Establish the requested scope

Identify the target host/account, frontend and profile, exact Claude executable,
native Claude root, and original project working directory. The environment
inventory can supply candidates without launching them. Cindy's downloaded
Claude executable and its `claude-home` directory do not establish where a
particular native session lives. Keep the frontend's Cindy ID separate from the
native Claude UUID; sharing an account or title is not an identity join.

For Cindy's current local project memory, the caller chooses one owner's
`maker-memory` root. Do not search all owners or export their files by default.
The project directory encoding is lossy; the scope's `meta.json.absPath` must
match the requested working directory. An unresolved worktree-to-main-project
mapping is a blocker for that memory scope, not permission to choose a similar
directory. See [Cindy's scope storage](https://github.com/makecindy/cindy/blob/b6b668550e2cd276e047e666640ea3d727dac027/packages/maker-core/src/memory/storage.ts)
and [worktree mapping](https://github.com/makecindy/cindy/blob/b6b668550e2cd276e047e666640ea3d727dac027/packages/maker-core/src/memory/scope-resolver.ts).

## Select the runtime and native home

Use `environment claude-connect prepare/apply` with explicit paths. The plan
contains fingerprints and changed field metadata, not a copy of the config or
credentials. It preserves unrelated JSON fields. Existing product-created
settings are required; use the product's normal setup if no settings exist.
Do not initialize a fabricated config shape to bypass validation.

```powershell
local-agent-record-janitor environment claude-connect prepare `
    --target paseo --config 'D:\profiles\paseo\config.json' `
    --claude-binary 'D:\apps\Cindy\claude-code\2.1.312\claude.exe' `
    --claude-home 'D:\profiles\claude' --provider claude-cindy `
    --out 'D:\private\paseo-claude-plan.json' --json
```

For T3, use `--target t3code` and that profile's `userdata/settings.json`.
This changes the default `claudeAgent` instance's `config.binaryPath` and
`config.homePath`. Paseo uses a custom provider extending `claude`, with an
exact command and `env.CLAUDE_CONFIG_DIR`. These paths select a program and
store; they do not migrate credentials or prove that the provider is logged in.

Review the plan against the user's existing scope. Once the frontend and its
configuration writers are stopped, apply the exact reviewed hash:

```powershell
local-agent-record-janitor environment claude-connect apply `
    --plan 'D:\private\paseo-claude-plan.json' `
    --approve '<plan_sha256>' --clients-closed --json
```

`--clients-closed` is the caller's lifecycle assertion; the command does not
kill or discover processes. It revalidates file identity and content before
writing and verifies the selected file afterwards. It does not create a
permanent backup. Windows preserves the source access controls during staging
and replacement. A `config_locked` result means another connection operation
or an unresolved lock owns that profile; inspect its state before removing any
lock. `unknown` means a change may have happened and needs read-only inspection
before retrying. Decide config rollback retention for the actual task. A
successful config result establishes file configuration only; restart the
selected client and use its own provider diagnostic when connection testing
is requested. Existing provider enable policy is preserved; `provider_enabled=false`
does not establish a usable connection. Do not automatically send a paid
model request.

The qualified config shapes are Paseo v1 custom providers and T3's
`providerInstances` default Claude driver. A custom T3 Claude instance can be
shadowed by the default instance during native session scanning; this workflow
uses the default. Unexpected or legacy shapes remain blocked until qualified.

## Import native history

Use the target client's official import, not SQL inserts or transcript copying.
Importing creates frontend state and may start a provider runtime, even without
sending a new user message. Establish the target provider's native home and
original working directory first. A session currently being continued by
another frontend should not be resumed concurrently.

The `environment claude-import` writer currently qualifies Paseo 0.15.3 and
T3 Code Windows Nightly `0.1.8-nightly.20261108.4353`. Pass the exact installed
desktop executable (`Paseo.exe` or `T3 Code (Nightly).exe`), profile and project;
Paseo also accepts a direct native CLI executable, not a shell shim. The target
local server must already be running, and its Claude provider must have an
explicit binary/home binding. Paseo import requires an executable-only command
without extra launcher flags. Unknown versions, profiles and schemas need
qualification before this writer can use them.

The T3 built-in deletion versions are qualified separately. A newer version
accepted for deletion does not extend this import writer's 4353 qualification.
For another version, qualify its import interface with synthetic history first.

```powershell
local-agent-record-janitor environment claude-import prepare `
    --target paseo --executable 'D:\apps\Paseo\Paseo.exe' `
    --profile 'D:\profiles\paseo' --project 'D:\work\project' `
    --native-id '<complete-Claude-UUID>' --provider claude-cindy `
    --native-transcript 'D:\profiles\claude\projects\PROJECT_KEY\UUID.jsonl' `
    --out 'D:\private\history-plan.json' --json
local-agent-record-janitor environment claude-import apply `
    --plan 'D:\private\history-plan.json' --approve '<plan_sha256>' --json
local-agent-record-janitor environment claude-import verify `
    --plan 'D:\private\history-plan.json' --json
```

For T3, prepare with `--target t3code`, its executable/profile, and
`--project-id '<existing-project-id>'` in place of `--native-id` and `--provider`.
Applying a T3 plan additionally requires `--accept-project-history`, reflecting
the already authorized project scope. Paseo prepare also reads the exact selected
JSONL internally to verify its native UUID/cwd and freezes its fingerprint;
no message bodies enter the plan. The path must be inside the selected Claude
home's `projects` tree. Apply invokes the official native scanner/importer;
T3 uses a short-lived local auth session and revokes it afterwards. No token or
raw transcript is printed. The command does not send a continuation message.

Paseo's supported CLI accepts an exact native UUID, provider and working
directory. Its returned `agentId` is a frontend ID; verify the registry's
native persistence/runtime reference as well as cwd. Paseo can create a wrapper
even if native history cannot be loaded, so the exact source transcript proof
is required; a persisted mapping alone does not prove all history was replayed.
An already active exact
mapping is already connected. Restoring an archived frontend is a different
state change, so stop for that scope decision unless already requested.
[Paseo import implementation](https://github.com/getpaseo/paseo/blob/v0.15.3/packages/cli/src/commands/agent/import.ts)

T3's qualified API accepts `projectId` and `expectedWorkspaceRoot`; it does
not accept a Claude UUID or an engine-only filter. Its scan reports project
summaries, and project import can include recent Claude, Codex and Pi history.
An exact-session request cannot authorize this broader import. Use the
whole-project path only when that scope is requested. Select an existing
verified project or create one through the product's supported UI/API, never
by inserting a projection row into SQLite.
[T3 import contract](https://github.com/pingdotgg/t3code/blob/v0.1.8-nightly.20261108.4353/packages/contracts/src/agentSessions.ts)

After an import or timeout, verify actual native references in the target
profile. T3 can count an already imported session as imported again, so a
response count is not evidence of duplication or of a new import. An uncertain
response needs verification before any retry. Report runtime configuration,
frontend history, and actual continuation as separate outcomes.
T3 verification reports observed strong references in the selected project's
projection. It cannot prove that every eligible native session was imported,
because the scanner exposes aggregate counts rather than an exact ID manifest.
`observed` reports this evidence, not a claim that the entire import is complete.

## Read original project memory

When the user wants current Cindy memory without exporting a snapshot, use
`environment cindy-memory inspect` with the selected owner root and project as
shown below. Its `store` identifies a scope verified through `meta.json.absPath`.
It lists curated shard metadata without reading their bodies. Check the original
`MEMORY.md` separately within that store, including its physical location and
link status. Missing or redirected files require resolving the intended source.

Add an instruction through the project's existing, verified loading entry point
to read that original index when the task needs Cindy memory, then read only
the relevant Markdown shards within the verified project scope. Preserve the
effective `AGENTS.md` and `CLAUDE.md` import chain. For a local-only entry point,
verify that the selected runtime actually loads it and keep host-specific paths
private and untracked. A new file name alone does not establish loading.

For example, the project instruction can say, with the path replaced by the
verified original index for this owner and project, "When this task needs Cindy
project memory, read D:/private/owner/maker-memory/project/MEMORY.md, then open
the relevant Markdown shards in that project memory directory."

Treat memory as contextual instructions and reconcile conflicts with current
project rules. An index reference to another directory needs its own scope
check. Each later read obtains the source's then-current content. Verify the
instruction chain and report actual model loading separately. This workflow
uses the original files through project instructions. Use the snapshot route
below when a fixed selected copy is requested.

## Export selected project memory

Inspect only the selected owner's project metadata first. This command reports
eligible file names and sizes without reading their contents:

```powershell
local-agent-record-janitor environment cindy-memory inspect `
    --maker-memory-root 'D:\profiles\Cindy\owners\OWNER\maker-memory' `
    --project 'D:\work\project' --json
```

Export exact selected shards into a private new file. The output directory must
already exist. Inspection is not authority to export every shard. When the
user has already chosen the project memories, select the matching listed
files without another blanket confirmation.

```powershell
local-agent-record-janitor environment cindy-memory export `
    --maker-memory-root 'D:\profiles\Cindy\owners\OWNER\maker-memory' `
    --project 'D:\work\project' --shard project_conventions.md `
    --shard feedback_review.md --out 'D:\private\cindy-project-memory.md' --json
```

Only `user_`, `feedback_`, `project_`, and `reference_` Markdown shards are
eligible. `MEMORY.md`, FTS databases, digest files, unrelated projects and bot/SSH
scopes are not exported. The source is unchanged and the new snapshot is not
automatically loaded. Existing output files are never overwritten. Refresh by
exporting a new named snapshot and reviewing the selected import separately.

The snapshot contains the selected real memory bodies; reports contain only
metadata. Keep it in a private location, and keep any project-local snapshot or
loader untracked and ignored. Fencing prevents a shard's `@path` text from
recursively importing more files; it does not make the memory instructions
inert once the model reads them. Review conflicts with current project rules.

## Enable the memory in the chosen project

When loading the exported snapshot is requested, inspect the project's existing
Claude/AGENTS instruction loading and the selected runtime's supported settings
sources. Preserve the original files and their effective behavior. Add one
explicit snapshot import to the appropriate local loader; do not replace
project instructions or copy Cindy's generated index into Claude auto memory.
Use Claude's `@path` syntax with escaped spaces, not a quoted path. The SDK must
load the applicable `project`/`local` setting sources.

Adding `CLAUDE.local.md` can change AGENTS.md fallback selection, including
ancestor loading. Where an existing same-scope AGENTS.md was effective, preserve
it with an explicit import or the already selected dual-loading setting. Do not
change global instruction mode merely to enable this project. If the existing
loader relationship cannot be established, deliver the exported snapshot and
report that activation remains pending.
[Claude imports and loading](https://code.claude.com/docs/en/memory#import-additional-files)
and [AGENTS.md selection](https://code.claude.com/docs/en/memory#when-claude-code-reads-agentsmd).

Verify the memory and original project instructions both load using the
client's memory/context inspection. For feature development, use synthetic
markers and a loopback mock, with remote requests and tools disabled. A live
continuation test is a separate requested action, not an automatic consequence
of saving a config or exporting a snapshot.
