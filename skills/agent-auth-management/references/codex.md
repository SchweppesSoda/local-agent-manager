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

## Reuse file-backed authentication only

Use this route when both selected runtimes consume compatible file-backed Codex
authentication, the source account is the intended one, and the target is an
identified native Codex home. Official guidance permits copying an existing
file-backed auth cache; this does not qualify an arbitrary third-party import
format. Follow [transfer and verification](transfer.md).

1. Check source login locally and establish the actual source `auth.json` path.
   Check target backend, provider and account/workspace restrictions first.
   Resolve conflicting environment/helper authentication rather than assuming
   this file will take precedence. Preserve restrictions; do not bypass them.
2. Preserve target settings and history. Capture its existing credential file
   as a private rollback copy, or record its absence. If the target needs a
   backend-setting change, include that exact setting in the authorized change
   and backup; do not change the source backend merely to obtain a file.
3. Transfer the entire compatible credential file as opaque bytes with a stable
   source snapshot and conditional atomic target replacement. Do not extract
   only the access token, reconstruct token JSON from memory, or copy the full
   home. Never print the parsed file or a decoded JWT.
4. Check the target with its selected runtime and environment, then refresh the
   actual frontend's account/provider status through its supported interface.
   Record whether that check was local recognition or a remote authenticated
   response. Report source and target outcomes separately.

A copied OAuth cache is a snapshot. Do not promise that two copies will remain
valid independently forever: token refresh and revocation behavior must be
verified for the selected version/backend. On a refresh conflict, inspect which
copy is current. Follow the login precautions below before reauthentication;
never repeatedly overwrite with an older snapshot or run a two-way sync loop.

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

The provider guidance above was checked on 2026-11-16. Paths, storage selection
and compatible file formats remain per-operation findings. Installed help and
matching versioned implementation take precedence over assumptions based on an
upstream development branch; a documented or compiled export helper is not
proof that this CLI exposes an export command.
