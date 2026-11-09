'use strict'
// T3 Code 4cb6945: environment-qualified thread keys; private values never emitted.
module.exports = ({ decodeString, encodeString, mapEntries, fail }) => {
  const bad = code => fail('office_leveldb_t3code_' + (code || 'ui_schema_unverified'))
  const map = v => { if (!v || Array.isArray(v) || typeof v !== 'object') bad(); return v }
  const object = (v, fields) => { map(v); if (Object.keys(v).some(k => !fields.includes(k))) bad() }
  const schemas = {
    't3code:composer-drafts:v1': [9, ['draftsByThreadKey', 'draftThreadsByThreadKey',
      'logicalProjectDraftThreadKeyByLogicalProjectKey', 'stickyModelSelectionByProvider',
      'stickyOptionsByModelByProvider', 'stickyActiveProvider'], ['draftsByThreadKey', 'draftThreadsByThreadKey']],
    't3code:terminal-state:v1': [4, ['terminalUiStateByThreadKey'], ['terminalUiStateByThreadKey']],
    't3code:right-panel-state:v2': [14, ['byThreadKey', 'threadPanelVisibilityByThreadKey'], ['byThreadKey', 'threadPanelVisibilityByThreadKey']],
    't3code:diff-panel-state:v1': [2, ['byThreadKey', 'branchBaseRefByThreadKey'], ['byThreadKey', 'branchBaseRefByThreadKey']],
    't3code:closed-views:v2': [2, ['entries'], []],
    't3code:prompt-stash:v2': [2, ['entries'], []],
    't3code:ui-state:v1': [null, ['projectExpandedById', 'projectOrder', 'threadLastVisitedAtById',
      'collapsedProjectCwds', 'expandedProjectCwds', 'projectOrderCwds', 'defaultAdvertisedEndpointKey',
      'sidebarProjectScopeKey', 'sidebarProjectScopeKeys', 'threadChangedFilesExpansionVersion',
      'threadChangedFilesExpandedById', 'pullRequestMergeMethod'], ['threadLastVisitedAtById', 'threadChangedFilesExpandedById']]
  }
  return (name, ids, environment) => {
    if (/^(t3code|codething):renderer-state:/.test(name) ||
        ['t3code:prompt-stash:v1', 't3code:right-panel-state:v1', 't3code:closed-views:v1'].includes(name)) {
      return () => bad('legacy_ui_unqualified')
    }
    const schema = schemas[name]
    if (!schema) return null
    return raw => {
      const text = decodeString(raw)
      mapEntries(text)
      const value = JSON.parse(text)
      const [version, fields, keyed] = schema
      let state = value
      if (version !== null) {
        object(value, ['version', 'state'])
        if (value.version !== version) bad()
        state = value.state
      }
      object(state, fields)
      const wanted = new Map([...ids].map(id => [`${environment}:${id}`, id]))
      const pairs = []
      function safe (v, depth = 0) {
        if (depth > 64) bad()
        if (typeof v === 'number' && (!Number.isFinite(v) || Object.is(v, -0) || Number.isInteger(v) && !Number.isSafeInteger(v))) bad()
        if (v && typeof v === 'object') for (const child of Object.values(v)) safe(child, depth + 1)
      }
      safe(value)
      function attachmentGuard (v) {
        if (!v || typeof v !== 'object') return
        for (const [k, child] of Object.entries(v)) {
          if (['attachments', 'files', 'persistedAttachments'].includes(k) && Array.isArray(child) && child.length ||
              ['attachment', 'attachmentId', 'uploadedAttachmentId'].includes(k) && child != null) bad('attachment_closure_unqualified')
          attachmentGuard(child)
        }
      }
      for (const field of keyed) {
        if (!(field in state)) continue
        const values = map(state[field])
        for (const [key, entry] of Object.entries(values)) {
          if (ids.has(key)) bad('unscoped_ui_reference')
          if (!wanted.has(key)) continue
          attachmentGuard(entry)
          delete values[key]
          pairs.push([wanted.get(key)])
        }
      }
      if (name === 't3code:composer-drafts:v1' && 'logicalProjectDraftThreadKeyByLogicalProjectKey' in state) {
        for (const [key, threadKey] of Object.entries(map(state.logicalProjectDraftThreadKeyByLogicalProjectKey))) {
          if (wanted.has(threadKey)) {
            delete state.logicalProjectDraftThreadKeyByLogicalProjectKey[key]
            pairs.push([wanted.get(threadKey)])
          }
        }
      }
      if (name === 't3code:closed-views:v2') {
        if (!Array.isArray(state.entries)) bad()
        state.entries = state.entries.filter(entry => {
          object(entry, ['kind', 'threadRef', 'surface', 'snapshot', 'id'])
          object(entry.threadRef, ['environmentId', 'threadId'])
          if (entry.threadRef.environmentId !== environment || !ids.has(entry.threadRef.threadId)) return true
          if (!['panel-tab', 'browser'].includes(entry.kind)) bad()
          attachmentGuard(entry)
          if (entry.kind === 'browser') bad('browser_restore_closure_unqualified')
          pairs.push([entry.threadRef.threadId])
          return false
        })
      }
      // Surviving contexts/side chats can refer to a deleted thread. They are
      // shared user work, so block rather than rewrite them by string matching.
      function references (v, scoped = false) {
        if (!v || typeof v !== 'object') return
        if (typeof v.environmentId === 'string' && v.environmentId !== environment) return
        for (const [key, child] of Object.entries(v)) {
          if (scoped && key.includes(':') && key.slice(0, key.indexOf(':')) !== environment) continue
          if (['threadId', 'parentThreadId', 'sourceThreadId', 'targetThreadId'].includes(key) && ids.has(child)) bad('shared_ui_reference')
          if (wanted.has(key)) bad('unknown_ui_reference')
          references(child, keyed.includes(key))
        }
      }
      references(state)
      return { after: pairs.length ? encodeString(JSON.stringify(value), raw[0]) : raw, pairs }
    }
  }
}
