'use strict'
const path = require('node:path')
const assert = require('node:assert/strict')
const dir = path.resolve(__dirname, '../../src/local_agent_manager/leveldb_runtime')
const helper = require(path.join(dir, 'office_drafts.cjs'))
const cleaner = require(path.join(dir, 't3code_ui.cjs'))({ ...helper, fail: code => { throw new Error(code) } })
const selected = new Set(['thread-1'])
function transform (name, value, raw = false) {
  const before = helper.encodeString(raw ? value : JSON.stringify(value))
  const result = cleaner(name, selected, 'env-1')(before)
  return { ...result, value: JSON.parse(helper.decodeString(result.after)) }
}
const keyed = [
  ['t3code:composer-drafts:v1', 9, 'draftsByThreadKey'],
  ['t3code:composer-drafts:v1', 9, 'draftThreadsByThreadKey'],
  ['t3code:terminal-state:v1', 4, 'terminalUiStateByThreadKey'],
  ['t3code:right-panel-state:v2', 14, 'byThreadKey'],
  ['t3code:right-panel-state:v2', 14, 'threadPanelVisibilityByThreadKey'],
  ['t3code:diff-panel-state:v1', 2, 'byThreadKey'],
  ['t3code:diff-panel-state:v1', 2, 'branchBaseRefByThreadKey']
]
for (const [name, version, field] of keyed) {
  const state = { [field]: { 'env-1:thread-1': { text: 'PRIVATE' }, 'env-1:keeper': { text: 'keep' }, 'env-2:thread-1': { threadId: 'thread-1' } } }
  const result = transform(name, { version, state })
  assert.equal(result.pairs.length, 1)
  assert.equal(result.value.state[field]['env-1:thread-1'], undefined)
  assert.equal(result.value.state[field]['env-1:keeper'].text, 'keep')
  assert.deepEqual(result.value.state[field]['env-2:thread-1'], { threadId: 'thread-1' })
  assert.throws(() => transform(name, { version: version + 1, state }), /schema_unverified/)
  assert.throws(() => transform(name, { version, state: { [field]: { 'env-1:thread-1': { files: [{}] } } } }), /attachment_closure_unqualified/)
}
const ui = transform('t3code:ui-state:v1', { projectOrder: ['p'], threadLastVisitedAtById: { 'env-1:thread-1': 't', 'env-2:thread-1': 'k' } }).value
assert.deepEqual(ui, { projectOrder: ['p'], threadLastVisitedAtById: { 'env-2:thread-1': 'k' } })
const ref = { environmentId: 'env-1', threadId: 'thread-1' }
const closed = transform('t3code:closed-views:v2', { version: 2, state: { entries: [
  { kind: 'panel-tab', id: 'a', threadRef: ref, surface: { kind: 'diff', id: 'diff' } },
  { kind: 'panel-tab', id: 'b', threadRef: { ...ref, environmentId: 'env-2' }, surface: { kind: 'diff' } }
] } }).value
assert.deepEqual(closed.state.entries.map(e => e.id), ['b'])
assert.throws(() => transform('t3code:closed-views:v2', { version: 2, state: { entries: [{ kind: 'browser', id: 'a', threadRef: ref, snapshot: {} }] } }), /browser_restore_closure_unqualified/)
assert.throws(() => transform('t3code:right-panel-state:v2', { version: 14, state: { byThreadKey: { 'env-1:keeper': { kind: 'side-chat', threadId: 'thread-1' } } } }), /shared_ui_reference/)
assert.throws(() => transform('t3code:composer-drafts:v1', { version: 9, state: { draftsByThreadKey: { 'thread-1': {} } } }), /unscoped_ui_reference/)
assert.throws(() => transform('t3code:renderer-state:v8', {}), /legacy_ui_unqualified/)
assert.throws(() => transform('t3code:terminal-state:v1', '{"version":4,"state":{"terminalUiStateByThreadKey":{},"terminalUiStateByThreadKey":{}}}', true))
console.log(JSON.stringify({ passed: true }))
