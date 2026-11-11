"use strict";
const fs=require('node:fs'), path=require('node:path'), crypto=require('node:crypto');
const {
  readLogical
}
=require('./idb_audit.cjs'), idb=require('./idb_keys.cjs'), physical=require('./physical.cjs').indexedDB();
const {
  transform
}
=require('./t3code_cache.cjs');
const digest=bytes=>crypto.createHash('sha256').update(bytes).digest('hex');
const text=bytes=>Buffer.from(bytes).swap16().toString('utf16le');
const keyOf=row=>row.decoded.suffix[0]?.type===1?text(row.decoded.suffix[0].data):null;
function inspect(request){
  const {
    logical
  }
  =readLogical(request.leveldb);
  const rows=logical.map(row=>({
    ...row,
    decoded:idb.decode(row.key)
  })),
  blobs=[];
  const databaseNames=rows.filter(row=>row.decoded.prefix[0]===0n&&row.decoded.suffix[0]===201).map(row=>({
    name:text(row.decoded.suffix[2]),
    origin:text(row.decoded.suffix[1]),
    db:String(physical.cursor(row.value).integer())
  }));
  const cache=databaseNames.find(db=>db.name==='t3code:connection-runtime');
  for(const row of rows){
    if(row.decoded.prefix[0]===0n&&[3, 4].includes(row.decoded.suffix[0])&&row.value.length)throw Error('blob_journal_pending');
    if(row.decoded.prefix[0]===0n&&row.decoded.suffix[0]===50&&!(row.key.equals(Buffer.from([0, 0, 0, 0, 50, 0]))&&row.value.equals(Buffer.from([8, 1]))))throw Error('scope_recovery_pending');
  }
  const stores=rows.filter(row=>row.decoded.prefix[0]!==0n&&row.decoded.prefix[1]===0n&&row.decoded.suffix[0]===50&&row.decoded.suffix[2]===0).map(row=>({
    db:String(row.decoded.prefix[0]),
    id:String(row.decoded.suffix[1]),
    name:text(row.value)
  }));
  function decode(row) {
    const version = physical.cursor(row.value);
    version.vi();
    let bytes = version.slice(row.value.length - version.at());
    if (bytes.subarray(0, 3).equals(Buffer.from([255, 17, 1]))) {
      const external = rows.find(other =>
        other.decoded.prefix[0] === row.decoded.prefix[0] &&
        other.decoded.prefix[1] === row.decoded.prefix[1] &&
        other.decoded.prefix[2] === 3n && keyOf(other) === keyOf(row));
      if (!external) throw Error('external_object_missing');
      const cursor = physical.cursor(external.value);
      if (cursor.byte() !== 0) throw Error('external_type_unqualified');
      const number = cursor.integer();
      const mimeLength = cursor.integer();
      cursor.slice(mimeLength * 2);
      const size = cursor.integer();
      if (!cursor.end()) throw Error('multiple_external_objects_unqualified');
      const directory = Math.floor(number / 256).toString(16).padStart(2, '0');
      const relative = `${row.decoded.prefix[0]}/${directory}/${number.toString(16)}`;
      bytes = fs.readFileSync(path.join(request.blob, relative));
      if (bytes.length !== size) throw Error('external_size_mismatch');
      blobs.push({ownerKeySha256: digest(external.key), threadKey: keyOf(row),
        relative, bytes: size, sha256: digest(bytes)});
    }
    if (bytes.subarray(0, 3).equals(Buffer.from([255, 17, 2]))) {
      bytes = physical.snappy(bytes.subarray(3));
    }
    // Other databases may contain Blink CryptoKey host objects. Read them in
    // Chromium, and preserve their original data/external bytes independently.
    if (String(row.decoded.prefix[0]) !== cache?.db) return null;
    if (bytes[0] !== 255 || bytes[1] > 21) throw Error('serialized_blink_version_unqualified');
    let offset = 2;
    if (bytes[offset] === 254) offset += 13;
    if (bytes[offset] !== 255 || ![15, 16].includes(bytes[offset + 1])) {
      throw Error('serialized_v8_version_unqualified');
    }
    const value = require('node:v8').deserialize(bytes.subarray(offset));
    if (value === undefined || typeof value === 'bigint' || value instanceof ArrayBuffer || ArrayBuffer.isView(value)) {
      throw Error('value_codec_unqualified');
    }
    return value;
  }
  const data=rows.filter(row=>row.decoded.prefix[0]!==0n&&row.decoded.prefix[1]!==0n&&row.decoded.prefix[2]===1n);
  // Decode every data record, including all external owners. Unused physical
  // blobs can only be reclaimed by a complete cold logical family rewrite.
  const values=data.map(row=>{
    try{
      return {
        row,
        value:decode(row)
      }
    }
    catch(e){
      throw Error('decode_db_'+row.decoded.prefix[0]+'_store_'+row.decoded.prefix[1]+'_'+e.name)
    }
  });
  for(const row of rows.filter(row=>row.decoded.prefix[2]===3n&&row.decoded.prefix[1]!==0n)){
    if(!blobs.some(owner=>owner.ownerKeySha256===digest(row.key)))throw Error('external_owner_missing');
  }
  let counts={
    thread:0,
    shell:0,
    query:0
  };
  if(cache){
    const definition={
      name:cache.name,
      version:5,
      stores:stores.filter(s=>s.db===cache.db).map(s=>({
        name:s.name,
        keyPath:null,
        autoIncrement:false,
        indexes:s.name==='query'?[{
          name:'environment-updated',
          keyPath:['environmentId', 'updatedAt', 'bytes'],
          unique:false,
          multiEntry:false
        }]:[],
        records:values.filter(({
          row
        })=>String(row.decoded.prefix[0])===cache.db&&String(row.decoded.prefix[1])===s.id).map(({
          row,
          value
        })=>({
          key:keyOf(row),
          value
        }))
      }))
    };
    counts=transform([definition], request.environment, request.ids).counts;
  }
  const owners=blobs.map(b=>({
    relative:b.relative,
    bytes:b.bytes,
    sha256:b.sha256
  }));
  const hash=crypto.createHash('sha256');
  for(const row of logical){
    const n=Buffer.alloc(8);
    n.writeUInt32LE(row.key.length);
    n.writeUInt32LE(row.value.length, 4);
    hash.update(n).update(row.key).update(row.value);
  }
  return {
    counts,
    databaseNames,
    stores,
    owners,
    logicalSha256:hash.digest('hex'),
    dataRows:rows.filter(row=>row.decoded.prefix[0]!==0n&&row.decoded.prefix[1]!==0n&&row.decoded.prefix[2]<30n).map(row=>({
      keySha256:digest(row.key),
      valueSha256:digest(row.value),
      db:String(row.decoded.prefix[0]),
      store:String(row.decoded.prefix[1]),
      key:keyOf(row)
    })),
    logical,
    rows
  };
}
function vi(value){
  value=BigInt(value);
  const out=[];
  do{
    let b=Number(value&127n);
    value>>=7n;
    out.push(b|(value?128:0));
  }
  while(value);
  return Buffer.from(out);
}
function str(raw){
  return Buffer.concat([vi(raw.length), raw]);
}
function logRecords(records){
  const chunks=[];
  let at=0;
  for(const raw of records){
    let offset=0,
    first=true;
    do{
      let room=32768-at%32768;
      if(room<7){
        chunks.push(Buffer.alloc(room));
        at+=room;
        room=32768;
      }
      const size=Math.min(room-7, raw.length-offset),
      last=offset+size===raw.length,
      type=first?(last?1:2):(last?4:3);
      const body=raw.subarray(offset, offset+size),
      head=Buffer.alloc(7);
      head.writeUInt32LE(physical.mask(physical.crc32c(Buffer.concat([Buffer.from([type]), body]))));
      head.writeUInt16LE(size, 4);
      head[6]=type;
      chunks.push(head, body);
      at+=7+size;
      offset+=size;
      first=false;
    }
    while(offset<raw.length);
  }
  return Buffer.concat(chunks);
}
function rewrite(request){
  const state=inspect(request),
  out=request.output;
  fs.mkdirSync(out, {
    recursive:false
  });
  const batches=[];
  let sequence=1n;
  for(let at=0;at<state.logical.length;){
    const parts=[];
    let bytes=0,
    count=0;
    while(at<state.logical.length&&(bytes<4*1024*1024||!count)){
      const row=state.logical[at++],
      part=Buffer.concat([Buffer.from([1]), str(row.key), str(row.value)]);
      parts.push(part);
      bytes+=part.length;
      count++;
    }
    const head=Buffer.alloc(12);
    head.writeBigUInt64LE(sequence);
    head.writeUInt32LE(count, 8);
    batches.push(Buffer.concat([head, ...parts]));
    sequence+=BigInt(count);
  }
  const manifest=Buffer.concat([vi(1), str(Buffer.from('idb_cmp1')), vi(2), vi(2), vi(9), vi(0), vi(3), vi(3), vi(4), vi(sequence-1n)]);
  for(const [name, bytes] of [['MANIFEST-000001', logRecords([manifest])], ['000002.log', logRecords(batches)], ['CURRENT', Buffer.from('MANIFEST-000001\n')], ['LOCK', Buffer.alloc(0)]])fs.writeFileSync(path.join(out, name), bytes, {
    flag:'wx'
  });
  const unique=new Map(state.owners.map(o=>[o.relative, o]));
  for(const owner of unique.values()){
    const target=path.join(request.outputBlob, owner.relative);
    fs.mkdirSync(path.dirname(target), {
      recursive:true
    });
    fs.copyFileSync(path.join(request.blob, owner.relative), target, fs.constants.COPYFILE_EXCL);
  }
  const cold=inspect({
    ...request,
    leveldb:out,
    blob:request.outputBlob
  });
  if(cold.logicalSha256!==state.logicalSha256)throw Error('cold_rewrite_changed');
  return cold;
}
module.exports={
  inspect,
  rewrite
};
if(require.main===module){
  try{
    const request=JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
    const result=request.output?rewrite(request):inspect(request);
    const {
      logical,
      rows,
      ...safe
    }
    =result;
    console.log(JSON.stringify({
      status:'verified',
      ...safe
    }));
  }
  catch(error){
    console.log(JSON.stringify({
      status:'blocked',
      code:/^[a-zA-Z0-9_]+$/.test(error.message)?error.message:'physical_unqualified'
    }));
    process.exitCode=1;
  }
}
