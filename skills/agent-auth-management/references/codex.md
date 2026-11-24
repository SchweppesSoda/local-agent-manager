# Codex login

## Identify what the selected Codex actually reads

Resolve the absolute executable and its version, effective `CODEX_HOME`, selected
provider, launch environment and credential storage setting for the selected instance.
Inspect relevant settings without dumping `config.toml`: it can contain secrets.
Frontend overrides and injected authentication can supersede a file-based login.

Official authentication documentation describes `cli_auth_credentials_store`:
`file` uses `auth.json` under the effective home; `keyring` uses the OS credential
store; `auto` chooses an available backend; `ephemeral` is process-local. A missing
file is therefore inconclusive. Recheck the installed version and actual backend.
[Codex authentication](https://developers.openai.com/codex/auth/)

Check `login --help` on the selected binary. `login status` is the documented local
status command; capture both output streams and emit only a classified result
and approved account metadata, since some versions show API-key fragments.
Use a child process with the intended profile/environment rather than changing
the parent shell or persistent user environment. Match frontend overrides when
testing; a standalone CLI probe proves only that launch context.
[Codex CLI reference](https://developers.openai.com/codex/cli/reference/)

`login status` can report the authentication method without identifying the
account. Do not infer account equality from two "ChatGPT" results. Prefer the
frontend's account metadata; if a selected file must be inspected, parse it
locally and return only necessary account/workspace labels. JWT claims are
unverified cached labels, not proof of current validity or subscription. An
unresolved account remains unknown. Classify explicit no-login separately from
load/configuration/keyring errors before deciding any repair.

## Magpie account discovery

Magpie v0.1.4758 can discover a login in another Codex home and record its
directory. A sign-in through Magpie can write back to that home when the
account matches. To diagnose the current login, compare the saved account's
home with the selected Codex profile.
[Home discovery](https://github.com/yetone/magpie/blob/v0.1.4758/internal/appdir/codex.go#L53-L103)
· [Sign-in destination](https://github.com/yetone/magpie/blob/v0.1.4758/internal/provider/signin.go#L963-L995)

## Cindy account registration

For a Cindy subscription, verify the active owner, selected provider and
exact Cindy account ID alongside the native Codex home. In Cindy v0.1.144, a
failed or cancelled first login rolls back the newly added non-default account
and its provider placement. The native home and its credential file can remain.
The standard add flow uses a new ID for another account. Its existing-login
shortcut attaches only the default CLI login.
[Cindy's versioned login flow](https://github.com/makecindy/cindy/blob/v0.1.144/apps/desktop/src/main/subscription-accounts/runtime.ts#L3015-L3217)

Before retrying a login, check the active owner's `subscription-accounts.json`
for the exact ID under `pools.openai.accounts` and check its provider placement.
In v0.1.144, an account with no mapping in `subscription-account-placement.json`
belongs to the built-in OpenAI provider. For an authorized repair of a known
Cindy-generated ID with a verified native login, prefer a supported attach
operation if the installed build offers one. Otherwise restore only its missing
pool entry and any confirmed placement required by the target provider. Let
any pending add/login for that ID finish before editing so its rollback cannot
remove the restored entry. Use a private before-image and a conditional atomic
write under Cindy's settings write lock or with Cindy closed. Preserve other
accounts, the default account routing and task/native bindings. Leave valid
credentials in place.
[Cindy's versioned placement lookup](https://github.com/makecindy/cindy/blob/v0.1.144/apps/desktop/src/main/subscription-accounts/placement.ts#L91-L115)
[Cindy's versioned settings writer](https://github.com/makecindy/cindy/blob/v0.1.144/apps/desktop/src/main/maker-host/override-settings-file.ts#L258-L284)

Verify the account under the intended provider in Cindy's account view. For
v0.1.144, the native identity check uses the selected Codex app-server's
`account/read` with `refreshToken: false` and Cindy's launch environment. The
RPC can confirm the identity for that home while the account is still missing
from the frontend. Report desktop registration and native login separately.
[Cindy's versioned account reader](https://github.com/makecindy/cindy/blob/v0.1.144/apps/desktop/src/main/subscription-accounts/nativeCli.ts#L78-L130)

## Official login

For ChatGPT, use the target frontend's sign-in or the selected Codex binary's
documented login in its effective home. Check credential replacement effects
first. Codex 0.169.0's browser and device flows can revoke an existing grant
before the new login completes, including a grant another client still uses.
[Versioned login implementation](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/cli/src/login.rs)
