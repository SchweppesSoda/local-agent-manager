---
name: agent-auth-management
description: Diagnose Codex, Claude Code and Antigravity (agy) sign-in state for a selected frontend or profile. Use to locate its effective authentication source, check account registration or guide the client's official login.
---

# Agent authentication management

Identify the requested account, frontend instance, executable, OS user, profile
and credential backend. Resolve them from the actual launch configuration.
A frontend can inject a provider, gateway or API key that overrides a native
login. Inspect only the relevant settings and environment variable presence.

- **Inspect an existing login.** Start with local evidence. Check the side
  effects of any status command before using it. For a read-only request,
  avoid diagnostics that refresh credentials. Report only the account label,
  method, profile and status. Keep tokens, keys and raw responses private.
- **Sign in.** Use the selected client's official login flow in the intended
  profile. Check whether replacing an existing login would revoke credentials
  used elsewhere. Let the user complete browser consent or MFA when required.
- **Verify.** Distinguish cached account recognition, an authenticated remote
  status response and a successful model request. A timeout or denied keyring
  leaves login state unknown. Run a model request only when requested.

Read [Codex login](references/codex.md) for Codex or Cindy account registration,
or [Claude Code and Antigravity login](references/providers.md) for those
providers. Runtime selection, whole-home configuration and record management
belong to `agent-management`.
