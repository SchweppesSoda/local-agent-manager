# Claude Code and Antigravity login reuse

## Claude Code

Resolve the actual binary, OS account and effective `CLAUDE_CONFIG_DIR` from the
selected frontend's launch configuration. Do not infer a config root from a
Cindy-managed executable or an old `claude-home` folder. For environment/home
binding, locate `agent-management` and its Claude connection reference using
`local-agent-manager resources --json`.

Current official guidance places file credentials in `.credentials.json` under
the selected config directory on Windows/Linux; macOS uses Keychain with a
documented file fallback. The directory can hold settings, skills and history
as well. Sharing the entire directory needs that intended scope. Environment,
helper, gateway and cloud authentication can select a different credential.
[Claude authentication](https://code.claude.com/docs/en/authentication)

Check the selected version's `auth --help`. Use `claude auth status` in the target
launch context, projecting only account/method/config-directory/status fields.
Check field availability and avoid raw output. The exit status/local report
alone does not prove a successful remote model call.
[Claude CLI reference](https://code.claude.com/docs/en/cli-reference)

For credential-only reuse, prefer a supported import/binding. Before copying
`.credentials.json`, qualify the source/target runtime formats and effective
credential precedence using the installed implementation or documented support.
A documented storage path alone does not prove transfer compatibility. Follow
[transfer and verification](transfer.md) for a qualified file transfer; otherwise
use `claude auth login` in the target context. Do not copy `.claude.json`, an entire
profile, or fabricated onboarding flags to suppress a login prompt. Diagnose
the prompt with the native status and current authentication guidance.

Official guidance supports multiple accounts through separate configuration
directories, with exceptions for some Console sign-ins stored elsewhere. This
is distinct from a frontend's account-switcher UI. Default to the single account
requested here; do not assume that only Cindy supports multiple accounts or
create extra profiles without need.

## Antigravity / agy

Identify the surface first: standalone CLI, CLI launched as `agy acp`, a
frontend-managed ACP runtime, or App/IDE. Resolve binary, OS identity, profile
and account independently. A managed `acp_token.json` is not automatically a
standalone CLI credential. Read `agent-management`'s Antigravity reference for
the selected frontend's runtime/data boundaries.

Google's current guidance describes local silent sign-in through the OS keyring
and shared authentication across local Antigravity surfaces. Verify that the
installed versions and chosen launch context actually participate; this does
not establish that a third-party managed ACP instance shares the same store.
[Antigravity authentication](https://antigravity.google/docs/install?tab=cli)

First use the target's supported account/status interface to see whether the
existing login is already reusable. Check installed help for available commands;
do not invent an `agy auth export` or status subcommand. If necessary, run its
official login flow and let the user complete consent. Do not dump Windows
Credential Manager, transplant encrypted credential-store files, or move an old
managed token into the CLI profile as a generic repair. A file-backed managed
ACP transfer needs separately verified source/target format and account binding
before following the transfer procedure.

Check the actual target frontend after reuse. A signed-in App, cached model list,
or account label is not proof that its CLI/ACP backend can authenticate. Do not
use `/logout` as a test or remove old instance data as part of login reuse.

## Version evidence

The official guidance above was checked on 2026-11-16. It supplies discovery and
verification routes, not a guarantee about all installed versions. Inspect
current help/configuration when behavior differs; keep unknown capability or
backend mappings explicit. Never treat successful account login as proof of
ownership of historical conversations.
