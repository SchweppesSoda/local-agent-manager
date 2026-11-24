# Codex login reuse

## Identify what the selected Codex actually reads

Resolve the absolute executable and its version, effective `CODEX_HOME`, selected
provider, launch environment and credential storage setting on both sides.
Inspect relevant settings without dumping `config.toml`: it can contain secrets.
Frontend overrides and injected authentication can supersede a file-based login.

Official authentication documentation describes `cli_auth_credentials_store`:
`file` uses `auth.json` under the effective home; `keyring` uses the OS credential
store; `auto` chooses an available backend; `ephemeral` is process-local. A missing
file is therefore inconclusive. Recheck the installed version and actual backend.
[Codex authentication](https://developers.openai.com/codex/auth/)

Resolve symlinks/junctions and credential identity before copying. If both sides
already address the same physical file or credential entry, test the target
without overwriting it. Do not assume that two different homes share a keyring
entry, or that the same OS user guarantees access from every frontend.

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

## Choose where the ChatGPT sign-in will be refreshed

When two Codex homes will stay in use, use each target's supported sign-in
flow for an independent login. Copying `auth.json` gives both homes the same
refresh token. When one rotates it, the other can lose access. A successful
initial account check can occur before either copy needs a refresh. If a copy
already exists, follow the [reauthentication precautions](#reauthentication-can-revoke-the-copied-source-grant)
before starting a login that might revoke the source grant.

**Magpie.** With subscription discovery enabled, Magpie v0.1.4758 looks for
additional Codex homes in the user directory and beside its configured Codex
home. It recognizes `codex*` and `.codex*` directories by `auth.json` or
`sessions/` and checks one level below a matching name when necessary. It
records the home where it found a login and checks that home again on switch.
For an account held there, use that home to run Codex, or enable the account
for Magpie's gateway requests. Honor the switch refusal. Signing in again
through Magpie can write the new credentials back to that same home when
the account matches. Check the effective home before claiming an independent login.
[Home discovery](https://github.com/yetone/magpie/blob/v0.1.4758/internal/appdir/codex.go#L53-L103)
· [Account switch](https://github.com/yetone/magpie/blob/v0.1.4758/internal/provider/logins.go#L1538-L1543)
· [Sign-in destination](https://github.com/yetone/magpie/blob/v0.1.4758/internal/provider/signin.go#L963-L995)

## One-time transfer of a file-backed Codex login

OpenAI documents copying the native auth cache as a fallback for headless
login. Use a transfer when the target will be the sole active consumer of
that grant and the source will stop using or refreshing it. Account for
background consumers such as Magpie. For an explicit backup or export, stop
at the private artifact. Follow [transfer and verification](transfer.md).
[Codex headless authentication](https://developers.openai.com/codex/auth/#login-on-headless-devices)

1. Check source login locally and locate its native file-backed `auth.json`.
   Check target backend, provider and account/workspace restrictions first.
   Resolve conflicting environment/helper authentication rather than assuming
   this file will take precedence. Preserve restrictions; do not bypass them.
2. Preserve target settings and history. Capture its existing credential file
   as a private rollback copy, or record its absence. If the target needs a
   backend-setting change, include that exact setting in the authorized change
   and backup; do not change the source backend merely to obtain a file.
3. Transfer the complete native credential payload as opaque bytes with a stable
   source snapshot and conditional atomic target replacement. Do not extract
   only the access token, reconstruct token JSON from memory, or copy the full
   home. Never print the parsed file or a decoded JWT.
4. Check the target with its selected runtime and environment, then refresh the
   actual frontend's account/provider status through its supported interface.
   Record whether that check was local recognition or a remote authenticated
   response. Report source and target outcomes separately.

## Cindy account registration

For Cindy subscription reuse, verify the active owner, selected provider and
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

## Keyring, ephemeral and frontend-managed authentication

Use a supported frontend account binding/import if the installed version offers
one and its behavior is verified. Otherwise authenticate the target using its
official login flow after the checks below. Do not dump a credential vault,
fabricate an `auth.json`, or turn an ephemeral session into persistent storage without an explicit export
scope and a supported mechanism. For a specifically requested export, report
the missing export capability instead of silently substituting a new login.

For an API-key setup, use the selected client's supported secure field/helper or
the CLI's documented stdin route when applicable. Existing secret bytes remain
inside the local transfer process. OAuth reuse must not silently change billing
to an API key or send credentials to a different provider endpoint.

## Reauthentication can revoke the copied source grant

Codex 0.169.0's browser/device login paths call
`clear_existing_auth_before_login`, which invokes `logout_with_revoke` before
the new login completes. Different files can still contain the same OAuth grant.
A backup cannot undo server-side revocation, and cancelling a login may leave
the previous grant revoked. Verify this behavior for the installed version.
[Versioned login implementation](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/cli/src/login.rs)

Before falling back to login, establish whether the target shares a physical
credential entry or a copied grant with the source. Do not run login/logout on
that credential as a repair shortcut. For a confirmed independent file-backed
target, an authorized repair can privately back up and locally detach its exact
copied cache under the transfer procedure before login, without invoking logout
or revocation. Verify that no keyring, fallback or injected source will supply
the old grant. Preserve the source and the target's other data. A shared/link
target or uncertain backend needs a supported isolated authentication route or
a concrete scope decision about shared-account disruption; do not silently
change the full home to manufacture isolation.

Do not create auth-file symlinks or hard links as a shortcut for persistent
sharing. Resolve existing links and all consumers before mutation; link equality
alone does not establish that distinct runtimes coordinate writes or refreshes.

## Whole-home connection

When the user wants shared Codex settings and data as well as login, use the
frontend's supported home setting and independently selected binary. Read the
matching `agent-management` runtime/connection reference through the manager's
resources output. Preserve unrelated frontend settings and read back the actual
binding before validating login. A home binding is neither a frontend history
import nor evidence that a particular historical session belongs to an account.

## Evidence maintenance

The Codex storage guidance above was checked on 2026-11-16. The Cindy and Magpie
notes apply to the versions cited. Paths, storage selection and compatible file
formats remain per-operation findings. Use installed help and matching versioned
implementation to verify CLI commands. A documented or compiled export helper
may be absent from the selected CLI.
