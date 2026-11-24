# Client verification

Identify the installed binary, frontend instance, effective home, discovery root, and version. Environment inventory can provide candidates. Use `agent-management` for installation or profile diagnosis.

Verify disk access, native discovery, one Skill body load, and the actual frontend menu separately. A shared root can have several consumers with independent verification records.

For runtime-bound adapters, run `local-agent-manager skills clients capture-binding --repo-root <source> --target <adapter> --binding-evidence <new-absolute-path-outside-source>` before the native probe. Run the exact selected client and home, observe loading, then use `clients verify` with that evidence, `--verified-version`, and `--confirm-native-check`.

Adapters declaring `pythonRuntimeVerification` additionally use `clients capture --runtime-evidence <new-absolute-path-outside-source>` from that client's real tool context. The packaged probe runs in the selected interpreter without installing the manager there.

T3 has its own launch context and menu. Antigravity configurations may require a directory deployment for menu scanning and a discovery manifest for native loading. Verify the actual client build and link primitive. The manifest supports several independent source entries.

Changing a shared discovery file invalidates the binding evidence of adapters that reference it. Finish the complete source registration before rechecking those adapters. Verify representative Skills from each source, then record results independently for each source. Unchanged bindings retain their existing evidence semantics.

## Codex shared Skills and independent copies

Codex discovers user Skills in `~/.agents/skills`. On Windows, Cindy can also create `<CODEX_HOME>/skills/xdt-agents` as a junction to that directory when preparing a Codex environment. Check the actual OS user, effective `CODEX_HOME` and link target for the frontend being inspected. [Codex local Skill locations](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills)

1. Inventory the shared directory and the effective `<CODEX_HOME>/skills`, including Skills in nested collections. Record each discovery path and the frontmatter `name`. If a bridge resolves to a directory already inventoried, record the bridge once and reuse that inventory.
2. For matching names, compare the physical Skill directory and its `SKILL.md` (for example with `Path.samefile` on both). Multiple paths to the same directory and file are aliases. Separate directories can have independent references or scripts even when their `SKILL.md` bytes match. Compare the full Skill trees before reporting that one copy is identical, stale or locally modified. Report unreadable or broken links as unresolved.
3. When the question concerns what Codex lists, query the selected runtime's `skills/list` for the relevant working directory and inspect the returned names and paths. Windows Codex can list a Skill once through multiple junctions to the same source and twice when a second directory declares the same name. For a question about a frontend menu, verify the menu separately.

If existing shared discovery already satisfies the requested client scope, another private copy is unnecessary. For a Codex-only deployment, check the other consumers of the shared directory before choosing it as a destination. Preserve client-managed bridges during a duplicate-copy audit. Cindy can recreate its bridge when it next prepares that home.
