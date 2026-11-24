# local-agent-manager maintenance

This repository implements local Agent environment inventory, diagnostics,
Skill source/deployment management, management workflows, and record cleanup. Generic Windows disk/software
maintenance is maintained separately. Editing code/documentation is distinct
from changing live software/configuration or deleting a user's live stores.

This is the owner's local management tool for a few personal PCs, not a
commercial or public product. Each PC uses one Python installation to register
one authoritative editable checkout; record-operation locks, logs, plans and
receipts belong to that checkout's `.agent-manager-state`.

## Source work

- Use the existing `main` checkout with one writer. Inspect branch, upstream,
  worktree and existing changes first; preserve unrelated work. A new branch
  or worktree requires the user's isolation/parallel-work request.
- Complete requested reversible local edits, relevant checks and scoped local
  commits without pausing after a first draft. Search affected APIs and direct
  consumers first; expand for shared protocol changes or demonstrated risk.
- Prefer the current logic and remove obsolete command/routing compatibility;
  do not automatically migrate or delete existing live evidence. Make the
  smallest change for demonstrated needs rather than building general
  registration or migration frameworks for hypothetical public deployments.
- Target current client and provider releases. Check the API, required fields
  and storage behavior used by each operation. Record observed product versions
  for diagnosis and freeze runtime identity in approved operations. Add adapter
  changes for demonstrated incompatibilities and avoid per-release allowlists
  or separate implementations for obsolete formats.
- Preserve frozen approval scope, physical store identity, confirmed native
  completion before frontend cleanup, atomic writes and interruption recovery.
  Retaining frontend references after mixed native results is acceptable.
  Recovery reports must retain cumulative mutations and completed batches.
- Test data isolation and optional runtime validation are separate: preserve
  runtime locations before isolating data paths, and let broken optional
  dependencies fail only their dependent tests, not ordinary test collection.
- Documentation needs link/diff checks. Code changes use relevant existing
  tests with temporary stores; shared deletion/authorization changes need the
  corresponding protocol regression coverage. Never use live cleanup as a test.
- Environment inventory stays metadata-only and does not authorize record or
  runtime deletion. Keep installation evidence, active runtime selection, native
  store identity, and authentication state distinct. Inject host facts in tests.
- Resolve each frontend/provider's BIN, Agent home and session directory
  independently from its actual per-machine/account configuration. This applies
  to Codex, Claude Code, Antigravity (agy) and Pi across T3, Paseo, Cindy and other
  adapters. Never turn a handoff's local paths or shared Cindy/T3 homes into fixed
  relationships. Account bindings and successful login do not prove historical
  session ownership; record unsupported mappings explicitly. See
  [BIN and data-root boundaries](docs/adapters.md#bin-与数据目录的配置边界).
- Report commit, validation and unresolved limits. Push/release/live cleanup
  follows the user's existing authorization; prepare local work before asking
  for any missing external or destructive authorization.

## Live record operations

Only when asked to inspect/delete actual records, read [operation contract](docs/agent-operation-contract.md)
and follow its task-specific reading routes and execution constraints.

## 文档归属

- README（含子目录）说明当前功能、用法、必要限制和文档入口，不追加执行流水或测试通过数量。
- 普通代码、配置、文档修改及常规验证由 Git 提交说明、PR 或 CI 记录；不为每次任务新建维护记录、目录或索引。
- 长期有效的设计、兼容性结论和操作方法更新已有专题文档；版本历史沿用已有 CHANGELOG。只有 Git/CI 无法还原、后续仍需排障或恢复的外部证据，才补充必要记录。
- 纯文档修改检查链接和 diff，不运行部署或业务全量测试。
