---
name: agent-auth-management
description: Inspect, reuse, copy, export, restore, or repair existing Codex, Claude Code, and Antigravity (agy) login credentials between local Agent clients or profiles, then verify the target account. Use for requests to reuse an already signed-in account or find its effective authentication source. Installation, runtime repair, conversation migration/deletion, and Skill deployment use their separate workflows.
---

# Agent authentication management

Complete the requested authentication task from source discovery through target
configuration and verification. Default to one account on the current machine;
start with Codex. Multiple accounts follow the chosen client's actual native
capabilities. Do not build an account pool, automatic token synchronizer, or
account-switching service for an ordinary reuse request.

Skill authoring does not authorize changing live credentials. For live work,
identify the source and target frontend/provider instance, OS account, and
requested outcome from the conversation and local evidence. Ask only for an
unresolved choice that affects the operation, such as two different signed-in
source accounts, an existing different target account, or a missing export
destination. Carry established authorization through execution and verification.

## Read the matching route

- **Codex:** read [Codex reuse](references/codex.md).
- **Claude Code or AGY:** read [other providers](references/providers.md).
- **Any credential/configuration write or export:** also read
  [transfer and verification](references/transfer.md).

Use `agent-management` for installation, executable selection, host visibility,
or environment diagnosis. Its metadata-only inventory can locate candidates;
it does not establish a working login. Use `skill-management` for deploying
this Skill. Locate these resources with `local-agent-manager resources --json`
when needed; this Skill does not add an `auth` CLI command.

## Establish the effective authentication source

1. Resolve each side's actual executable, version, frontend launch configuration,
   OS user, home/profile, and credential backend independently. A shared binary
   does not imply a shared home. A home candidate is not a confirmed binding.
2. Distinguish subscription/OAuth login, API key, gateway/helper authentication,
   and frontend-injected credentials. Inspect only the necessary configuration
   fields and environment variable names/presence. A native auth file may be
   ignored by the selected provider or launch environment.
3. Use the selected runtime's supported account/status interface. Capture and
   project output to an allowlist before displaying it: account label, method,
   profile, backend, status and time. Omit token/key values and fragments, raw
   responses, environment dumps, and login logs. File presence alone is only
   `present`; locally recognized login is not proof of remote validity.
4. For T3, Cindy, Paseo and similar frontends, follow the specific instance's
   configuration, including overrides. An account label in the frontend is not
   proof that its running backend uses those credentials. Recheck after upgrades.

## Choose the least disruptive complete route

- **Already shared and working:** verify the target; make no redundant copy.
- **Supported credential reuse/import:** use the client's native mechanism or
  the matching provider's qualified credential-file route. Keep the target's
  configuration, skills, history and sessions independent by default.
- **Whole-home binding:** use only when sharing that home's settings and data is
  intended. Explain that scope before selecting it; an authentication-only
  request does not authorize replacing `CODEX_HOME` or `CLAUDE_CONFIG_DIR` with
  another client's full home. Reuse existing supported configuration writers.
- **Backend cannot be reused safely:** use the target's official login flow.
  Changing a keyring setting alone does not export its contents. Browser consent,
  account choice or MFA that only the user can complete is a real handoff; resume
  verification afterwards. Use the host's secret-input tool for required secrets.
- **Export explicitly requested:** create only the selected credential artifact
  at a private destination using the transfer procedure. Do not scan/export all
  accounts. Export completion does not establish successful target login.

No automatic subscription-to-API-key conversion, endpoint change, sign-out,
source deletion, global environment rewrite, history import or client shutdown.
If lifecycle changes are needed, inspect active work and use the task's existing
authorization; do not terminate the session performing this work.

## Finish with evidence

Report the source and target, account label, route taken, files/settings changed,
backup location if any, and the strongest verification actually achieved.
Separate saved configuration, locally recognized authentication, target remote
account/provider validation, and a successful model response. Use a supported
non-generation diagnostic first. A paid model request requires that scope in
the user's instruction; never call an untested setup fully usable.

An uncertain write or refresh needs inspection before retry. Preserve source
credentials and recovery material, report completed changes even if a later
step failed, and state the concrete remaining action. Do not repeatedly ask for
permission for the same source, target and operation.
