"""One T3 content/reference policy for discovery, planning and cross-client guards.

Only producer-defined transcript fields are content. IDs, source/target links,
unknown JSON fields, keys and opaque values remain protection sources.
See docs/operation-cli.md for the qualified producer schema and boundaries.
"""
import json
import re

from .providers.t3 import strict_json
from .t3code_store import V2

# Scalar text fields of OrchestrationV2TurnItem. A file path, nativeItemRef,
# childThreadId, fork source or arbitrary dynamic-tool output is NOT text.
_ITEM_TEXT_FIELDS = {
    'user_message': {'text'}, 'assistant_message': {'text'}, 'reasoning': {'text'},
    'proposed_plan': {'markdown'}, 'todo_list': {'explanation'},
    'file_change': {'diffStr', 'oldStr', 'newStr'},
    'command_execution': {'input', 'output'},
    'approval_request': {'prompt', 'reason'},
    'run_interrupt_request': {'message'}, 'run_interrupt_result': {'message'},
    'system_notice': {'message'}, 'compaction': {'summary'}, 'handoff': {'summary'},
    'subagent': {'prompt', 'progress', 'result'},
}


_READ_ONLY_THREAD_TOOLS = frozenset({'t3_thread_read', 't3_thread_list', 't3_thread_search'})
_TOOL_TEXT_INPUTS = {'delegate_task': 'task', 't3_thread_send': 'message'}


def _tool_content_reference(value, path):
    name = value.get('toolName')
    if not isinstance(name, str):
        return False
    tool = None
    for prefix in ('t3-code.', 'mcp_t3-code_', 'Run mcp_t3-code_'):
        if name.startswith(prefix):
            tool = name[len(prefix):]
            break
    if tool in _READ_ONLY_THREAD_TOOLS:
        return path.startswith(('$.input.', '$.input[', '$.output.', '$.output['))
    field = _TOOL_TEXT_INPUTS.get(tool)
    if (field and path == '$.input.' + field and isinstance(value.get('input'), dict)
            and isinstance(value['input'].get(field), str)):
        return True
    # CUA's MCP result contains displayed text/image blocks. Only an exact
    # text block is content; JS inputs, image paths, extra fields and unknown
    # tools retain protection. Dynamic-tool output itself is producer Unknown.
    if name == 'cua_repl.js':
        match = re.fullmatch(r'\$\.output\[([0-9]+)\]\.text', path)
        output = value.get('output')
        if match and isinstance(output, list) and int(match[1]) < len(output):
            block = output[int(match[1])]
            return (isinstance(block, dict) and set(block) == {'type', 'text'}
                    and block.get('type') == 'text' and isinstance(block.get('text'), str))
    return False


def content_reference(table, column, payload, path, event_type):
    """Classify known dialogue text, mentions and read-tool audit locations."""
    if column != 'payload_json' or not isinstance(payload, str) or not isinstance(path, str):
        return False
    if table in {'orchestration_v2_events', 'orchestration_events'}:
        table = {'message.updated': V2 + 'messages', 'turn-item.updated': V2 + 'turn_items',
                 'context-handoff.updated': V2 + 'context_handoffs',
                 'subagent.updated': V2 + 'subagents'}.get(event_type)
    try:
        value = strict_json(payload)
    except (ValueError, TypeError):
        return False
    if not isinstance(value, dict):
        return False
    if table == V2 + 'context_handoffs':
        if path == '$.summaryText' and isinstance(value.get('summaryText'), str):
            return True
        match = re.fullmatch(r'\$\.history\.messages\[([0-9]+)\]\.text', path)
        history = value.get('history')
        messages = history.get('messages') if isinstance(history, dict) else None
        if match and isinstance(messages, list) and int(match[1]) < len(messages):
            message = messages[int(match[1])]
            return (isinstance(message, dict) and message.get('role') in {'user', 'assistant'}
                    and isinstance(message.get('text'), str))
    if (table == V2 + 'messages' and value.get('role') in {'user', 'assistant'}
            and path == '$.text' and isinstance(value.get('text'), str)):
        return True
    if table == V2 + 'subagents' and path == '$.prompt' and isinstance(value.get('prompt'), str):
        return True
    if table == V2 + 'turn_items':
        kind = value.get('type')
        fields = _ITEM_TEXT_FIELDS.get(kind, ()) if isinstance(kind, str) else ()
        if path.startswith('$.') and path[2:] in fields and isinstance(value.get(path[2:]), str):
            return True
        if kind == 'dynamic_tool':
            return _tool_content_reference(value, path)
    user_message = table == V2 + 'messages' and value.get('role') == 'user'
    user_item = table == V2 + 'turn_items' and value.get('type') == 'user_message'
    if user_message or user_item:
        match = re.fullmatch(r'\$\.context\.records\[([0-9]+)\]\.(threadId|contextId|label|title)', path)
        context = value.get('context')
        records = context.get('records') if isinstance(context, dict) else None
        if match and isinstance(records, list) and int(match[1]) < len(records):
            record = records[int(match[1])]
            return (isinstance(record, dict) and record.get('kind') == 'thread'
                    and isinstance(record.get(match[2]), str))
    return False


class ReferenceConflict(ValueError):
    def __init__(self, code, location):
        self.kind = code
        self.location = location
        super().__init__(code + ': ' + json.dumps(location, ensure_ascii=True))


def require_no_references(db, ids, *, rows=None, json_only=False, code):
    """Scan retained rows, returning only the location of a protected match.

    Both embedded and escaped IDs count. JSON keys can never be exempted by
    a content-value rule. ``rows`` contains trusted SQL ownership predicates,
    not user text; absent predicates scan the complete private after-image.
    """
    ids = tuple(sorted(set(ids)))
    if not ids:
        return
    text_pattern = re.compile('|'.join(re.escape(s) for s in ids))
    byte_pattern = re.compile(b'|'.join(re.escape(s.encode()) for s in ids))
    def has_reference(value):
        if isinstance(value, str):
            return bool(text_pattern.search(value))
        return bool(byte_pattern.search(value)) if isinstance(value, bytes) else False
    db.create_function('lam_has_reference', 1, has_reference, deterministic=True)
    db.create_function('lam_content_reference', 5, content_reference, deterministic=True)
    if rows is None:
        rows = [(r[0], '1', ()) for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for table, predicate, args in rows:
        quoted = '"' + table.replace('"', '""') + '"'
        columns = [r[1] for r in db.execute('PRAGMA table_info(' + quoted + ')')]
        event = 't.event_type' if 'event_type' in columns else 'NULL'
        for column in columns:
            if json_only and not column.endswith('_json'):
                continue
            if table == 'projection_thread_messages' and column == 'text':
                continue
            qcol = '"' + column.replace('"', '""') + '"'
            location = {'table': table, 'column': column}
            if db.execute('SELECT 1 FROM ' + quoted + ' AS t WHERE (' + predicate +
                          ') AND NOT json_valid(' + qcol + ') AND lam_has_reference(' +
                          qcol + ') LIMIT 1', args).fetchone():
                raise ReferenceConflict(code, location)
            hit = db.execute('SELECT j.fullkey FROM ' + quoted +
                ' AS t,json_tree(CASE WHEN json_valid(t.' + qcol + ') THEN t.' + qcol +
                " ELSE 'null' END) AS j WHERE (" + predicate + ') AND (' +
                "lam_has_reference(j.key) OR (j.type='text' AND lam_has_reference(j.value) " +
                'AND NOT lam_content_reference(?,?,t.' + qcol + ',j.fullkey,' + event + '))) LIMIT 1',
                (*args, table, column)).fetchone()
            if hit:
                # A bounded path is metadata; never report a matched value.
                raise ReferenceConflict(code, {**location, 'json_path': str(hit[0])[:256]})
