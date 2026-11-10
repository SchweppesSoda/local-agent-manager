"""Qualified local CLI summary mirrors; preserve unselected map entries."""
import hashlib
import json

from .antigravity_cli_cleanup import fail, native_id, _json

PROTO = 'jetbox_summaries_proto.pb'
METADATA = 'cache/conversation_metadata.json'


def _varint(raw, index):
    value = 0
    for shift in range(0, 70, 7):
        if index >= len(raw):
            fail('summary_proto_truncated')
        byte = raw[index]; index += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, index
    fail('summary_proto_varint_invalid')


def _fields(raw):
    index, fields = 0, []
    while index < len(raw):
        begin = index
        tag, index = _varint(raw, index)
        number, wire = tag >> 3, tag & 7
        if number < 1 or wire != 2:
            fail('summary_proto_map_unqualified')
        size, index = _varint(raw, index)
        if size > len(raw)-index:
            fail('summary_proto_truncated')
        payload = raw[index:index+size]; index += size
        fields.append((number, payload, raw[begin:index]))
        if len(fields) > 20000:
            fail('summary_proto_budget_exceeded')
    return fields


def proto_entries(raw):
    result = {}
    for number, entry, encoded in _fields(raw):
        if number != 1:
            fail('summary_proto_map_unqualified')
        fields = _fields(entry)
        if len(fields) != 2 or {f[0] for f in fields} != {1, 2}:
            fail('summary_proto_map_unqualified')
        values = {n: value for n, value, _ in fields}
        try:
            sid = native_id(values[1].decode('ascii'))
        except UnicodeError:
            fail('summary_proto_map_unqualified')
        if sid in result:
            fail('summary_proto_duplicate_id')
        result[sid] = (values[2], encoded)
    return result


def proto_after(raw, ids, summaries):
    entries = proto_entries(raw)
    for sid, (body, _) in entries.items():
        if sid not in summaries or hashlib.sha256(body).hexdigest() != summaries[sid]:
            fail('summary_proto_mirror_mismatch')
        if sid not in ids and any(target.encode() in body for target in ids):
            fail('summary_proto_restore_reference')
    return b''.join(encoded for sid, (_, encoded) in entries.items() if sid not in ids)


def metadata_after(raw, ids, summaries):
    data = _json(raw)
    if not isinstance(data, dict) or set(data) != {'conversations'} or not isinstance(data['conversations'], dict):
        fail('conversation_metadata_unqualified')
    fields = {'ID', 'Title', 'Preview', 'NumSteps', 'Loaded', 'UpdatedAt', 'WorkspaceURIs', 'AppDataDir',
              'ProjectID', 'AgentName', 'GroupID', 'Internal', 'Cloud', 'CloudProject', 'PermissionPreset'}
    for sid, record in data['conversations'].items():
        native_id(sid)
        if (sid not in summaries or not isinstance(record, dict)
                or set(record) != {'summary', 'is_internal', 'last_modified_time'}
                or not isinstance(record['summary'], dict) or set(record['summary']) != fields):
            fail('conversation_metadata_unqualified')
        summary = record['summary']
        if (summary['ID'] != sid or summary['AppDataDir'] not in ('', 'antigravity-cli')
                or summary['Cloud'] is not False or summary['GroupID']):
            fail('conversation_metadata_external_reference')
        if sid not in ids and any(target in json.dumps(record) for target in ids):
            fail('conversation_metadata_restore_reference')
    data['conversations'] = {sid: row for sid, row in data['conversations'].items() if sid not in ids}
    return (json.dumps(data, ensure_ascii=False, indent=2)+'\n').encode('utf-8')


def transform(path, raw, evidence):
    if path == PROTO:
        return proto_after(raw, evidence['ids'], evidence['summary_raw_hashes'])
    if path == METADATA:
        return metadata_after(raw, evidence['ids'], evidence['summary_raw_hashes'])
    fail('restore_path_unqualified')
