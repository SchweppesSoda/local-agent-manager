# Credential transfer and verification

This procedure applies to a provider-qualified file or configuration change.
It does not qualify new credential formats. Prefer an existing native import or
supported configuration writer when available; honor that writer's contract.

## Prepare the exact operation

Establish source and target physical paths, owners, credential backend and
expected account. Inspect link/reparse targets and actual access controls. Keep
the scope to selected credential/configuration objects, not directory trees.
If another account already occupies the target and replacement was not selected,
ask that specific question. An already selected replacement needs no repeat
confirmation. An export also needs an exact private destination.

Determine whether source or target processes may refresh credentials or rewrite
configuration during the operation. Take a coherent source snapshot; recheck
its identity and digest before applying. A successful JSON parse or unchanged
size/mtime alone does not establish a coherent snapshot. Use a compatible native
lock, a verified atomic reader/writer protocol, or exclude source writers while
reading. Establish target writer exclusion through the product's supported
lifecycle or transaction interface. If that
cannot be established, do not overwrite an active store. Preserve current work
and arrange the needed lifecycle step within the user's task scope.

Backups, exports and staging files contain secrets. Create them in a private
location outside Git, shared/synced folders and diagnostic logs, with permissions
restricted to the intended OS identity and required system principals before
writing bytes. Use exclusive creation and a fresh task-specific name. On Windows
verify ACLs, including inherited access; on Unix use restrictive file modes.
Applying permissions after a permissive write is insufficient.

Before the first mutation, save a private recovery record of exact paths,
physical identities, initial existence, before-image locations, access controls,
and intended changes. Record completed steps and expected after-image digests
as work proceeds, without credential contents. This evidence must survive a
process interruption; chat context alone is not a recovery record.

For native login/import, determine whether it replaces or revokes the existing
grant and which other clients consume it. Separate files can contain the same
grant. Reauthentication is a mutation even if no manual file write is planned;
follow the provider's precautions before invoking it.

## Apply without exposing credentials

1. Store a private byte-for-byte target backup and required configuration
   before-images, or explicitly record that the target did not exist. Record
   nonsecret metadata separately. Do not put secrets in command arguments,
   shell transcripts, tool output, exception dumps, Skill files or Git.
2. Transfer within a local process using file/stream APIs. Stage the exact
   credential bytes on the target filesystem under restrictive permissions.
   Parse only as needed for the qualified format, without emitting values.
   Compare bytes/digests internally, never token substrings.
3. Revalidate the source and target against the prepared identities and
   snapshots while target writers remain excluded. Unexpected changes require
   new inspection. Atomically replace the target; when it was absent, use
   create-without-overwrite semantics. Verify final permissions and bytes.
4. Apply only the necessary frontend settings through the supported interface
   or qualified configuration writer. Preserve unrelated fields. Multi-file
   work is not one atomic transaction: retain which steps completed and their
   before/after evidence if a later step fails.
5. Release exclusion, refresh/restart only the affected provider as appropriate,
   and perform the checks below. Verification may legitimately refresh tokens;
   a subsequent byte change is not automatically corruption.

Do not handcraft a credential format or invent a manager command. When a small
task-specific transfer script is needed, inspect it before execution and use
synthetic credentials to exercise its write/recovery behavior first. If it is
added to the repository, give it corresponding isolated regression coverage.

## Verify the requested outcome

- **Artifact/configuration:** the intended private file or binding exists with
  correct permissions and the selected provider will actually read it.
- **Local authentication:** the selected runtime recognizes the intended login
  under the target launch environment. This may read only cached state.
- **Target remote validation:** the actual frontend/provider's supported account
  diagnostic makes a successful authenticated check. Model discovery alone may
  be public or cached; do not assume it proves authentication.
- **Generation:** if the user requested a model smoke test, send a minimal new
  request without project contents or tool execution. Do not resume an existing
  conversation merely to test login. Report separately if this was not tested.

Use the strongest available non-generation check for ordinary reuse. When the
user specifically requests a working model test, that scope can authorize the
minimal generation step; do not re-ask an already settled scope. Report a missing
diagnostic or required user login step instead of claiming end-to-end success.

## Recovery

After an uncertain write, inspect the target before any retry. Restore a backup
only while writers are excluded and the target still matches this operation's
expected result. Preserve later user edits and refreshed credentials. Old OAuth
backups may no longer authenticate; restoring bytes does not reverse remote
refresh/revocation. For a target that was initially absent, rollback means
removing only the exact file this operation created, under writer exclusion and
only while its identity/content still match the recorded after-image. Otherwise
preserve it and report the conflict. Restore related settings conditionally too;
do not remove a whole home or provider instance.

When validity is uncertain, inspect the provider's reauthentication effects
before choosing fresh login. Do not log out the source to repair the target,
or let target login silently revoke a grant copied from the source.

Report completed mutations, current status and private recovery locations.
Remove only this operation's disposable staging material once its outcome is
known. Retain needed rollback material until verified success or an explicit
retention decision; do not add an unrelated cache/record cleanup operation.
