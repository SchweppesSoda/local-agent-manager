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
      function closedBrowser (entry) {
        // Installed PreviewSessionSnapshot / closed-view restore: a URL and
        // viewport reopen a new tab. Downloads, external profiles and unknown
        // fields may own additional artifacts and remain unqualified.
        const invalid = () => bad('browser_restore_closure_unqualified')
        const exact = (v, required, optional = []) => {
          if (!v || Array.isArray(v) || typeof v !== 'object' ||
              required.some(k => !(k in v)) || Object.keys(v).some(k => !required.concat(optional).includes(k))) invalid()
        }
        exact(entry, ['kind', 'threadRef', 'snapshot', 'id'])
        const v = entry.snapshot
        exact(v, ['threadId', 'tabId', 'navStatus', 'canGoBack', 'canGoForward', 'updatedAt'],
          ['viewport', 'profileId', 'runtime'])
        if (v.threadId !== entry.threadRef.threadId || typeof v.tabId !== 'string' || !v.tabId ||
            typeof v.updatedAt !== 'string' || !v.updatedAt ||
            typeof v.canGoBack !== 'boolean' || typeof v.canGoForward !== 'boolean' ||
            'profileId' in v && v.profileId !== 'default' ||
            'runtime' in v && !['desktop', 'server'].includes(v.runtime)) invalid()
        const nav = v.navStatus
        if (nav?._tag === 'Idle') exact(nav, ['_tag'])
        else {
          if (!['Loading', 'Success', 'LoadFailed'].includes(nav?._tag)) invalid()
          exact(nav, ['_tag', 'url', 'title'].concat(nav._tag === 'LoadFailed' ? ['code', 'description'] : []))
          if (typeof nav.url !== 'string' || typeof nav.title !== 'string') invalid()
          let url
          try { url = new URL(nav.url) } catch { invalid() }
          if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) invalid()
          if (nav._tag === 'LoadFailed' && (!Number.isInteger(nav.code) || typeof nav.description !== 'string')) invalid()
        }
        if ('viewport' in v) {
          // The fill viewport owns no screenshot, recording or external file.
          exact(v.viewport, ['_tag'])
          if (v.viewport._tag !== 'fill') invalid()
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
          if (typeof entry.threadRef.environmentId !== 'string' || !entry.threadRef.environmentId ||
              typeof entry.threadRef.threadId !== 'string' || !entry.threadRef.threadId || typeof entry.id !== 'string') bad()
          if (entry.threadRef.environmentId !== environment || !ids.has(entry.threadRef.threadId)) return true
          if (!['panel-tab', 'browser'].includes(entry.kind)) bad()
          attachmentGuard(entry)
          if (entry.kind === 'browser') closedBrowser(entry)
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
      // Closed snapshots inherit their explicit threadRef environment. Do not
      // reinterpret another environment's snapshot.threadId as a local link.
      references(name === 't3code:closed-views:v2'
        ? { ...state, entries: state.entries.filter(e => e.threadRef.environmentId === environment) }
        : state)
      return { after: pairs.length ? encodeString(JSON.stringify(value), raw[0]) : raw, pairs }
    }
  }
}
