# Client verification

Identify the installed binary, frontend instance, effective home, discovery root, and version. Environment inventory can provide candidates. Use `agent-management` for installation or profile diagnosis.

Verify disk access, native discovery, one Skill body load, and the actual frontend menu separately. A shared root can have several consumers with independent verification records.

For runtime-bound adapters, run `local-agent-manager skills clients capture-binding --repo-root <source> --target <adapter> --binding-evidence <new-absolute-path-outside-source>` before the native probe. Run the exact selected client and home, observe loading, then use `clients verify` with that evidence, `--verified-version`, and `--confirm-native-check`.

Adapters declaring `pythonRuntimeVerification` additionally use `clients capture --runtime-evidence <new-absolute-path-outside-source>` from that client's real tool context. The packaged probe runs in the selected interpreter without installing the manager there.

T3 has its own launch context and menu. Antigravity configurations may require a directory deployment for menu scanning and a discovery manifest for native loading. Verify the actual client build and link primitive. The manifest supports several independent source entries.

Changing a shared discovery file invalidates the binding evidence of adapters that reference it. Finish the complete source registration before rechecking those adapters. Verify representative Skills from each source, then record results independently for each source. Unchanged bindings retain their existing evidence semantics.
