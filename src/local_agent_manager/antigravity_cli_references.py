"""Reference checks for qualified CLI transcript fields, never deletion ownership.

Field numbers come from the producer's trajectory/cortex/tools descriptors;
see docs/antigravity-implicit-recovery.md. Unknown fields and tools remain
protection sources. The original bytes are always preserved and fingerprinted.
"""
import json
import re


def contains_reference(raw, ids):
    # Include escaped UUID characters, even inside protobuf string fields.
    decoded = re.sub(rb'\\u([0-9a-fA-F]{4})',
                     lambda m: chr(int(m[1], 16)).encode('utf-8', errors='surrogatepass'), raw)
    return any(s.encode() in raw or s.encode() in decoded for s in ids)


def _fields(raw):
    index = 0

    def varint():
        nonlocal index
        value = 0
        for shift in range(0, 70, 7):
            if index >= len(raw):
                raise ValueError('truncated protobuf')
            byte = raw[index]
            index += 1
            if shift == 63 and byte > 1:
                raise ValueError('oversized varint')
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ValueError('oversized varint')

    result = []
    while index < len(raw):
        tag = varint()
        number, wire = tag >> 3, tag & 7
        if not 0 < number < 2**29 or len(result) >= 100000:
            raise ValueError('invalid protobuf field')
        if wire == 0:
            value = varint()
        elif wire in (1, 2, 5):
            size = varint() if wire == 2 else 8 if wire == 1 else 4
            if size > len(raw) - index:
                raise ValueError('truncated protobuf field')
            value = raw[index:index+size]
            index += size
        else:
            raise ValueError('unknown protobuf wire type')
        result.append((number, wire, value))
    return result


def _text_field(raw, number):
    values = [v for n, w, v in _fields(raw) if n == number and w == 2]
    if len(values) > 1:
        raise ValueError('duplicate discriminator')
    return values[0].decode('utf-8') if values else None


def _check(raw, schema, ids, reject, depth=0):
    if not contains_reference(raw, ids):
        return
    if depth > 20:
        reject(raw, ids)
        return
    try:
        fields = _fields(raw)
    except ValueError:
        reject(raw, ids)
        return
    for number, wire, value in fields:
        if not isinstance(value, bytes) or not contains_reference(value, ids):
            continue
        rule = schema.get(number, False)
        if wire != 2 or rule is False:
            reject(value, ids)
        elif rule is None:  # Precisely identified passive string/prompt field.
            continue
        elif callable(rule):
            rule(value, ids, reject)
        else:
            _check(value, rule, ids, reject, depth+1)


# Tool arguments are historical input, not native resume handles. Unknown
# tools/argument keys and original_name overrides keep their strict protection.
_TOOLS = {
    'run_command': {'CommandLine', 'Cwd', 'WaitMsBeforeAsync', 'toolSummary'},
    'read_file': {'Path', 'StartLine', 'EndLine', 'ByteOffset', 'toolSummary'},
    'write_file': {'Path', 'Content', 'Overwrite', 'toolSummary'},
    'edit_file': {'Path', 'Target', 'Replacement', 'toolSummary'},
    'command_status': {'CommandId', 'WaitDurationSeconds', 'OutputCharacterCount', 'toolSummary'},
    'mcp_t3-code_t3_thread_read': {'threadId', 'environmentId', 'view', 'limit', 'maxCharsPerItem', 'afterPosition', 'toolSummary'},
    'mcp_t3-code_t3_thread_list': {'environmentId', 'limit', 'toolSummary'},
    'mcp_t3-code_t3_thread_search': {'environmentId', 'query', 'limit', 'toolSummary'},
}


def _arguments(raw, allowed, ids, reject):
    try:
        pairs = json.loads(raw, object_pairs_hook=lambda pairs: pairs)
        if not isinstance(pairs, list) or any(not isinstance(p, tuple) or len(p) != 2 for p in pairs):
            raise ValueError('not an argument object')
        for key, value in pairs:
            reject(str(key).encode(), ids)
            if key not in allowed or not isinstance(value, (str, int, float, bool, type(None))):
                reject(json.dumps(value, ensure_ascii=False).encode(), ids)
    except (ValueError, UnicodeError, RecursionError):
        reject(raw, ids)


def _tool_call(raw, ids, reject):
    try:
        name = _text_field(raw, 2)
        original = _text_field(raw, 9)
    except (ValueError, UnicodeError):
        reject(raw, ids)
        return
    if name not in _TOOLS or original not in (None, name):
        reject(raw, ids)
        return
    args = lambda data, ids, reject: _arguments(data, _TOOLS[name], ids, reject)
    _check(raw, {3: args, 8: args}, ids, reject)


_RENDER = {1: None, 2: None, 5: None,
           8: {3: None}, 9: {1: None, 3: {2: None, 3: None}},
           10: {1: None, 3: {1: None, 4: None}}}
_METADATA = {4: _tool_call, 29: _tool_call, 16: _tool_call, 17: None, 30: None}
_AGENT_MESSAGE = {4: None}  # sender, recipient and nested steps stay protected.


def _permission_resource(raw, ids, reject):
    try:
        action = _text_field(raw, 1)
    except (ValueError, UnicodeError):
        action = None
    _check(raw, {2: None} if action in {'command', 'read_file', 'write_file'} else {}, ids, reject)


def _permission_grant(raw, ids, reject):
    try:
        action = _text_field(raw, 5)
        resource = next((v for n, w, v in _fields(raw) if n == 1 and w == 2), b'')
        resource_action = _text_field(resource, 1)
    except (ValueError, UnicodeError):
        action = resource_action = None
    allowed = {'command', 'read_file', 'write_file'}
    passive = (action or resource_action) in allowed and action in (None, resource_action)
    _check(raw, {1: _permission_resource, **({6: None} if passive else {})}, ids, reject)


def _permission_request(raw, ids, reject):
    try:
        resource = next((v for n, w, v in _fields(raw) if n == 1 and w == 2), b'')
        passive = _text_field(resource, 1) in {'command', 'read_file', 'write_file'}
    except (ValueError, UnicodeError):
        passive = False
    _check(raw, {1: _permission_resource, **({2: None, 4: None, 5: None} if passive else {})}, ids, reject)


_PERMISSIONS = {2: _permission_grant}
_REQUEST = {21: _permission_request}


def _generic(tool):
    def args(raw, ids, reject):
        try:
            key = _text_field(raw, 1)
        except (ValueError, UnicodeError):
            key = None
        _check(raw, {2: None} if key in _TOOLS[tool] else {}, ids, reject)

    def payload(raw, ids, reject):
        try:
            name = _text_field(raw, 1)
        except (ValueError, UnicodeError):
            name = None
        formats = {
            'run_command': ('RunCommand', {6: None}),
            'read_file': ('ReadFile', {}),
            'write_file': ('FileEdit', {3: {3: {3: {1: None}}}}),
            'edit_file': ('FileEdit', {3: {3: {3: {1: None}}}}),
        }
        expected, schema = formats.get(tool, ('', {}))
        _check(raw, {2: schema} if name == 'type.googleapis.com/exa.cortex.core.tools.'+expected else {}, ids, reject)

    return {1: args, 2: {1: None, 3: _RENDER, 6: payload}}


def check_step(row, ids, reject):
    values = (row[key] for key in row.keys())
    if not any(contains_reference(value.encode() if isinstance(value, str) else value, ids)
               for value in values if isinstance(value, (str, bytes))):
        return
    raw = row['step_payload'] or b''
    schema = {}
    # Payload is a complete gemini_coder.Step, not just its oneof value.
    # Exempt only the oneof matching the SQL discriminator and protobuf type.
    try:
        fields = _fields(raw)
        types = [v for n, w, v in fields if n == 1 and w == 0]
        metadata = [v for n, w, v in fields if n == 5 and w == 2]
        calls = [v for n, w, v in _fields(metadata[0]) if n == 4 and w == 2] if len(metadata) == 1 else []
        tool = _text_field(calls[0], 2) if len(calls) == 1 else None
        if calls and _text_field(calls[0], 9) not in (None, tool):
            tool = None
        qualified = row['step_format'] == 0 and types == [row['step_type']]
    except (ValueError, UnicodeError, TypeError):
        qualified, tool = False, None
    if qualified:
        schema = {5: _METADATA, 133: _PERMISSIONS, 56: _REQUEST, 147: {1: _REQUEST}}
        payloads = {
            14: (19, {1: None, 2: None}),
            15: (20, {1: None, 3: None, 8: None, 16: None, 7: _tool_call}),
            23: (30, {4: None, 5: None, 6: None, 8: None, 10: None, 19: None}),
            101: (114, {1: None, 2: _RENDER, 4: _AGENT_MESSAGE}),
        }
        if row['step_type'] in payloads:
            field, shape = payloads[row['step_type']]
            schema[field] = shape
        elif row['step_type'] == 132 and tool in _TOOLS:
            schema[140] = _generic(tool)
    for key in row.keys():
        value = row[key]
        value = value.encode() if isinstance(value, str) else value
        if not isinstance(value, bytes):
            continue
        shape = {'step_payload': schema, 'metadata': _METADATA,
                 'permissions': _PERMISSIONS, 'render_info': _RENDER}.get(key, {}) if qualified else {}
        _check(value, shape, ids, reject)


def check_generation(raw, ids, reject):
    # Stored model input is a prompt snapshot. Config/execution/other fields
    # still receive the same reference check as structural metadata.
    _check(raw, {1: {1: None, 2: None}, 9: None}, ids, reject)


def check_artifact(path, raw, ids, reject):
    reject(path.encode(), ids)
    parts = path.split('/')
    if len(parts) < 5 or parts[0] != 'brain' or parts[1] in ids or parts[2] != '.system_generated':
        reject(raw, ids)
        return
    relative = '/'.join(parts[3:])
    # Exact diagnostic leaves, not a blanket exemption for this directory.
    if (relative in {'logs/transcript.jsonl', 'logs/transcript_full.jsonl'}
            or re.fullmatch(r'logs/chunks/transcript(?:_full)?/[0-9]{8}\.jsonl', relative)
            or re.fullmatch(r'steps/[0-9]+/output\.txt', relative)
            or re.fullmatch(r'tasks/task-[0-9]+\.log', relative)):
        return
    if re.fullmatch(r'messages/[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.json', relative):
        try:
            from .environment_config import _unique_object
            message = json.loads(raw, object_pairs_hook=_unique_object)
            if (not isinstance(message, dict) or message.get('id') != parts[-1][:-5]
                    or not isinstance(message.get('content', ''), str)):
                raise ValueError('message identity mismatch')
            # A retained message can mention a deleted conversation. Its
            # sender/recipient/source metadata can still be real references.
            protected = {k: v for k, v in message.items() if k != 'content'}
            reject(json.dumps(protected, ensure_ascii=False).encode(), ids)
            return
        except (ValueError, UnicodeError, RecursionError):
            pass
    reject(raw, ids)
