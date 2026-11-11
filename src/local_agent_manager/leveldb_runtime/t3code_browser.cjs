"use strict";
const fs=require('node:fs'), crypto=require('node:crypto');
const {
  app,
  BrowserWindow,
  protocol,
  session
}
=require('electron');
const {
  transform
}
=require('./t3code_cache.cjs');
const request=JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
if (!['inspect', 'apply', 'seed'].includes(request.mode)) throw Error('mode_invalid');
if(process.versions.electron!=='44.10.1'||process.versions.chrome!=='152.0.7977.169') throw Error('runtime_unqualified');
app.setPath('userData', request.controlPath);
for(const flag of ['disable-background-networking', 'disable-component-update', 'disable-sync', 'disable-gpu']) app.commandLine.appendSwitch(flag);
protocol.registerSchemesAsPrivileged([{
  scheme:'t3code',
  privileges:{
    standard:true,
    secure:true,
    supportFetchAPI:true
  }
}]);
const fullSnapshot = async function () {
  let items=0;
  const checkJson = (value, seen=new Set(), depth=0) => {
    if (++items>200000 || depth>64) throw new Error('idb_snapshot_budget_exceeded');
    if (value===null || typeof value==='string' || typeof value==='boolean') return;
    if (typeof value==='number') {
      if (!Number.isFinite(value) || Object.is(value, -0)) throw new Error('idb_value_codec_unverified');
      return;
    }
    if (!value || typeof value!=='object' || seen.has(value)) throw new Error('idb_value_codec_unverified');
    if (!Array.isArray(value) && Object.getPrototypeOf(value)!==Object.prototype) throw new Error('idb_value_codec_unverified');
    seen.add(value);
    if (Array.isArray(value)) {
      if (Object.keys(value).length!==value.length) throw new Error('idb_value_codec_unverified');
      for (let i=0;i<value.length;i++) {
        if (!(i in value)) throw new Error('idb_value_codec_unverified');
        checkJson(value[i], seen, depth+1);
      }
    }
    else for (const key of Object.keys(value)) checkJson(value[key], seen, depth+1);
    // Plain JSON trees have neither cycles nor repeated object references.
    // Retain seen nodes so JSON.stringify cannot erase shared-reference identity.
  };
  const normalize = value => {
    if (value instanceof ArrayBuffer) return {
      binaryType:'ArrayBuffer',
      bytes:Array.from(new Uint8Array(value))
    };
    if (ArrayBuffer.isView(value)) return {
      binaryType:value.constructor.name,
      bytes:Array.from(new Uint8Array(value.buffer, value.byteOffset, value.byteLength))
    };
    if (value instanceof CryptoKey) return {
      opaqueType:'CryptoKey',
      type:value.type,
      algorithm:value.algorithm,
      extractable:value.extractable,
      usages:value.usages
    };
    if (value instanceof Date) return {
      dateValue:value.toISOString()
    };
    if (Array.isArray(value)) return value.map(normalize);
    if (value && Object.getPrototypeOf(value)===Object.prototype) return Object.fromEntries(Object.entries(value).map(([key, value])=>[key, normalize(value)]));
    return value;
  };
  const requestResult = request => new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(new Error('idb_request_failed'));
  });
  const definitions = (await indexedDB.databases()).sort((a, b) => a.name < b.name ? -1 : a.name > b.name ? 1 : 0);
  const databases = [];
  for (const definition of definitions) {
    const opening = indexedDB.open(definition.name);
    opening.onupgradeneeded = () => opening.transaction.abort();
    const db = await requestResult(opening),
    stores = [];
    for (const name of [...db.objectStoreNames].sort()) {
      const tx = db.transaction(name, 'readonly'),
      store = tx.objectStore(name);
      if (store.autoIncrement) throw new Error('idb_key_generator_unqualified');
      const indexes = [...store.indexNames].sort().map(name => {
        const index = store.index(name);
        return {
          name,
          keyPath: index.keyPath,
          unique: index.unique,
          multiEntry: index.multiEntry
        };
      });
      const records = [];
      const cursor = store.openCursor();
      await new Promise((resolve, reject) => {
        cursor.onerror = () => reject(new Error('idb_cursor_failed'));
        cursor.onsuccess = () => {
          const item = cursor.result;
          if (!item) return resolve();
          try {
            checkJson(item.key);
            const value=normalize(item.value);
            checkJson(value);
            records.push({
              key: item.key,
              value
            });
            item.continue();
          }
          catch(error) {
            reject(error);
          }
        };
      });
      stores.push({
        name,
        keyPath: store.keyPath,
        autoIncrement: store.autoIncrement,
        indexes,
        records
      });
    }
    databases.push({
      name: db.name,
      version: db.version,
      stores
    });
    db.close();
  }
  return databases;
};
const seedDatabases = async function(definitions){
  for(const def of definitions){
    const db=await new Promise((resolve, reject)=>{
      const r=indexedDB.open(def.name, def.version);
      r.onupgradeneeded=()=>{
        for(const s of def.stores){
          const store=r.result.createObjectStore(s.name);
          for(const i of s.indexes)store.createIndex(i.name, i.keyPath, i);
        }
      };
      r.onsuccess=()=>resolve(r.result);
      r.onerror=reject;
    });
    const tx=db.transaction([...db.objectStoreNames], 'readwrite');
    const done=new Promise((resolve, reject)=>{
      tx.oncomplete=resolve;
      tx.onabort=reject;
    });
    for(const s of def.stores)for(const row of s.records)tx.objectStore(s.name).put(row.value, row.key);
    await done;
    db.close();
  }
};
const applyTransaction = async function(actions, abort){
  const db=await new Promise((resolve, reject)=>{
    const r=indexedDB.open('t3code:connection-runtime');
    r.onupgradeneeded=()=>r.transaction.abort();
    r.onsuccess=()=>resolve(r.result);
    r.onerror=reject;
  });
  const tx=db.transaction(['shell', 'thread', 'query'], 'readwrite', {
    durability:'strict'
  });
  const done=new Promise((resolve, reject)=>{
    tx.oncomplete=resolve;
    tx.onabort=()=>reject(Error('transaction_aborted'));
  });
  for(const a of actions){
    const s=tx.objectStore(a.store);
    if(a.delete)s.delete(a.key);
    else s.put(a.value, a.key);
  }
  if(abort)tx.abort();
  await done;
  db.close();
};
const digest=value=>crypto.createHash('sha256').update(JSON.stringify(value)).digest('hex');
let attempted=false;
app.whenReady().then(async()=>{
  const storage=session.fromPath(request.sessionPath, {
    cache:false
  });
  storage.webRequest.onBeforeRequest((details, callback)=>callback({
    cancel:!details.url.startsWith('t3code://app/')
  }));
  await storage.protocol.handle('t3code', ()=>new Response('<!doctype html><title>Isolated storage</title>', {
    headers:{
      'content-type':'text/html',
      'content-security-policy':"default-src 'none'"
    }
  }));
  const win=new BrowserWindow({
    show:false,
    webPreferences:{
      session:storage,
      nodeIntegration:false,
      contextIsolation:true,
      sandbox:true
    }
  });
  win.webContents.setWindowOpenHandler(()=>({
    action:'deny'
  }));
  await win.loadURL('t3code://app/');
  const evaluate=s=>win.webContents.executeJavaScript(s);
  if(request.mode==='seed') {
    await evaluate(`(${seedDatabases.toString()})(${JSON.stringify(request.seed)})`);
  }
  const before=await evaluate(`(${fullSnapshot.toString()})()`);
  const {
    expected,
    actions,
    counts
  }
  =transform(before, request.environment, request.ids);
  if(request.mode==='apply'){
    if(digest(before)!==request.beforeSha256||digest(expected)!==request.afterSha256)throw Error('frozen_state_changed');
    attempted=true;
    await evaluate(`(${applyTransaction.toString()})(${JSON.stringify(actions)},${request.abortTransaction===true})`);
  }
  const after=await evaluate(`(${fullSnapshot.toString()})()`);
  if(request.mode==='apply'&&digest(after)!==digest(expected))throw Error('after_unverified');
  const metadata=before.map(db=>({
    name:db.name,
    version:db.version,
    stores:db.stores.map(s=>({
      name:s.name,
      keyPath:s.keyPath,
      indexes:s.indexes,
      records:s.records.length
    }))
  }));
  const queries=before.find(d=>d.name==='t3code:connection-runtime')?.stores.find(s=>s.name==='query').records.map(r=>({
    keySha256:digest(r.key),
    environmentId:r.value?.environmentId,
    bytes:r.value?.bytes
  }));
  console.log(JSON.stringify({
    status:'verified',
    beforeSha256:digest(before),
    expectedAfterSha256:digest(expected),
    afterSha256:digest(after),
    counts,
    actions:actions.map(({
      store,
      key,
      delete:remove
    })=>({
      store,
      key,
      delete:remove===true
    })),
    metadata,
    queries,
    attempted
  }));
  await storage.flushStorageData();
  win.destroy();
  app.quit();
}).catch(error=>{
  console.log(JSON.stringify({
    status:attempted?'unknown':'blocked',
    code:/^[a-z_]+$/.test(error.message)?error.message:'native_read_failed',
    attempted
  }));
  app.exit(1);
});
