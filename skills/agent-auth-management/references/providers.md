# Claude Code and Antigravity login

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

For a requested sign-in, use `claude auth login` in the target context.
Check the selected version's replacement effects and which clients share its
credential store before reauthenticating.

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

Check installed help for a supported account/status interface. If a sign-in
is requested, use the official flow for the selected surface and let the user
complete consent. Check the actual frontend afterwards. An App sign-in or a
cached model list alone cannot confirm that the CLI/ACP backend can authenticate.

## Version evidence

The official guidance above was checked on 2026-11-16. It supplies discovery and
verification routes, not a guarantee about all installed versions. Inspect
current help/configuration when behavior differs; keep unknown capability or
backend mappings explicit. Never treat successful account login as proof of
ownership of historical conversations.
