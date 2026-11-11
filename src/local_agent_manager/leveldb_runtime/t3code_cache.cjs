"use strict";
// The installed v5 persistence contract stores shell/thread wrappers as JSON strings.
function transform(content, environment, ids) {
  for (const database of content) {
    if (database.name === 't3code:connection-runtime') continue;
    const schema = {
      't3code:cloud-auth': [1, 'keys'],
      't3code:project-favicons': [2, 'images']
    }
    [database.name];
    if (!schema || database.version !== schema[0] || database.stores.length !== 1 || database.stores[0].name !== schema[1]) throw Error('other_database_schema_unqualified');
    const store = database.stores[0];
    if (store.keyPath !== null || store.autoIncrement || store.indexes.length) throw Error('other_database_store_unqualified');
    if (ids.some(id => JSON.stringify(store.records).includes(id))) throw Error('other_database_target_reference_unqualified');
  }
  const selected = new Set(ids),
  keys = new Set(ids.map(id => `${environment}:${id}`));
  const expected = structuredClone(content),
  actions = [],
  counts = {
    thread:0,
    shell:0,
    query:0
  };
  const db = expected.find(db => db.name === 't3code:connection-runtime');
  if (!db) return {
    expected,
    actions,
    counts
  };
  if (db.version !== 5 || db.stores.map(s=>s.name).sort().join(',') !== 'catalog,query,server-config,shell,thread,vcs-refs') throw Error('cache_schema_unqualified');
  const store = name => db.stores.find(s=>s.name===name);
  for (const s of db.stores) {
    if (s.keyPath !== null || s.autoIncrement) throw Error('cache_keypath_unqualified');
    const wanted = s.name==='query' ? [{
      name:'environment-updated',
      keyPath:['environmentId', 'updatedAt', 'bytes'],
      unique:false,
      multiEntry:false
    }] : [];
    if (JSON.stringify(s.indexes)!==JSON.stringify(wanted)) throw Error('cache_indexes_unqualified');
  }
  const parse = raw => {
    if(typeof raw!=='string') throw Error('cache_value_unqualified');
    return JSON.parse(raw);
  };
  store('thread').records = store('thread').records.filter(row => {
    if (!keys.has(row.key)) return true;
    const value = parse(row.value),
    id = row.key.slice(environment.length+1);
    if (value.schemaVersion!==5 || value.environmentId!==environment || value.threadId!==id || value.snapshot?.projection?.thread?.id!==id) throw Error('thread_wrapper_unqualified');
    counts.thread++;
    actions.push({
      store:'thread',
      key:row.key,
      delete:true
    });
    return false;
  });
  for (const row of store('shell').records) {
    if (row.key!==environment) continue;
    const value=parse(row.value);
    if (value.schemaVersion!==3 || value.environmentId!==environment || !Array.isArray(value.snapshot?.threads) || !Array.isArray(value.snapshot?.archivedThreads)) throw Error('shell_wrapper_unqualified');
    for (const field of ['threads', 'archivedThreads']) {
      const before=value.snapshot[field];
      value.snapshot[field]=before.filter(item=>!selected.has(item.id));
      counts.shell+=before.length-value.snapshot[field].length;
    }
    if (counts.shell) {
      row.value=JSON.stringify(value);
      actions.push({
        store:'shell',
        key:row.key,
        value:row.value
      });
    }
  }
  // Query keys are environment:v1:JSON([RPC tag,input]). Only a target-owned
  // request is removable. Shared response references need a schema-specific edit.
  const mentions = value => ids.some(id=>JSON.stringify(value).includes(id));
  store('query').records = store('query').records.filter(row => {
    if (typeof row.key!=='string') throw Error('query_key_unqualified');
    if (!row.key.startsWith(`${environment}:`)) return true;
    if (row.key===`${environment}:batch`) return true;
    // batching counter, no restore payload
    const value=row.value;
    if (!row.key.startsWith(`${environment}:v1:`) || value?.environmentId!==environment || typeof value.payload!=='string' || !Number.isFinite(value.updatedAt) || value.bytes!==Buffer.byteLength(value.payload)) throw Error('query_wrapper_unqualified');
    const input=JSON.parse(row.key.slice(`${environment}:v1:`.length));
    if (Array.isArray(input) && input[0]==='pull-request-stat' && input.length===5) {
      if (mentions(input) || mentions(value.payload)) throw Error('query_shared_target_reference_unqualified');
      return true;
    }
    if (!Array.isArray(input) || input.length!==2 || typeof input[0]!=='string' || !input[1] || typeof input[1]!=='object') throw Error('query_request_unqualified');
    if (selected.has(input[1].threadId)) {
      if (Object.keys(input[1]).join(',')!=='threadId') throw Error('query_mixed_request_unqualified');
      counts.query++;
      actions.push({
        store:'query',
        key:row.key,
        delete:true
      });
      return false;
    }
    if (mentions(input) || mentions(value.payload)) throw Error('query_shared_target_reference_unqualified');
    return true;
  });
  return {
    expected,
    actions,
    counts
  };
}
module.exports={
  transform
};
