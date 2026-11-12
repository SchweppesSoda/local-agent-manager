"""Exact T3 upload/HTML ownership; retained references remain database guards."""
from pathlib import Path
import re
from contextlib import closing

from . import frozen_files
from .providers.t3 import strict_json
from .sqlite_utils import connect_readonly
from .t3code_store import database_path, V2, rows, fail


def names_for(row):
    segment = re.sub(r'[^a-z0-9_-]+', '-', row['thread_id'].strip().lower())
    segment = re.sub('-+', '-', segment).strip('-_')[:80].rstrip('-_')
    segment = '_pending' if segment == 'pending' else segment
    pattern = re.escape(segment) + r'-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}(?:-([a-z0-9]{1,10}))?'
    match = re.fullmatch(pattern, row['id'], re.IGNORECASE) if isinstance(row.get('id'), str) else None
    if not segment or match is None:
        return set()
    extension = match[1]
    if extension is None:
        extension = Path(row['name']).suffix.lstrip('.').lower() if isinstance(row.get('name'), str) else ''
        if row['type'] != 'image' or extension not in {
                'avif', 'bmp', 'gif', 'heic', 'heif', 'ico', 'jpeg', 'jpg', 'png', 'svg', 'tiff', 'webp'}:
            return set()
    primary = row['id'] + '.' + extension.lower()
    names = {primary}
    if extension.lower() in {'docx', 'xlsx', 'pptx', 'odt', 'ods', 'odp', 'rtf', 'epub', 'ipynb'}:
        names.add(primary + '.txt')
    return names


def selected_upload(value, owner):
    # Installed ChatFileAttachment; external/snapshot sources and images are
    # still unsupported for removal. They must never be treated as plain files.
    fields = {'type', 'id', 'name', 'mimeType', 'sizeBytes'}
    if (not isinstance(value, dict) or set(value) != fields or value['type'] != 'file'
            or any(not isinstance(value[k], str) or not value[k] for k in ('id', 'name', 'mimeType'))
            or type(value['sizeBytes']) is not int or value['sizeBytes'] <= 0):
        fail('attachment_closure_unqualified')
    row = dict(value, thread_id=owner)
    names = names_for(row)
    extension = Path(value['name']).suffix.lower()
    if extension == '.part' or not re.fullmatch(r'\.[a-z0-9]{1,10}', extension):
        extension = '.bin'
    if not names or value['id'] + extension not in names:
        fail('attachment_closure_unqualified')
    return row


def check_reference(row, declared, html):
    item = strict_json(row['payload_json'])
    table = row['table']
    if table in {'orchestration_events', 'orchestration_v2_events'}:
        table = {'message.updated': V2+'messages', 'turn-item.updated': V2+'turn_items'}.get(row['event_type'])
    owner, path = row['owner'], row['fullkey']
    if not isinstance(item, dict):
        fail('attachment_closure_unqualified')
    user = (table == V2+'messages' and item.get('role') == 'user'
            or table == V2+'turn_items' and item.get('type') == 'user_message')
    if user and path == '$.attachments' and isinstance(item.get('attachments'), list):
        for value in item['attachments']:
            upload = selected_upload(value, owner)
            if declared.get((owner, upload['id'])) != value:
                fail('attachment_closure_unqualified')
        return
    match = re.fullmatch(r'\$\.context\.records\[([0-9]+)\]\.attachmentId', path)
    if user and match:
        record = item['context']['records'][int(match[1])]
        if (not isinstance(record, dict) or set(record) != {'version','contextId','label','kind','attachmentId','name','mimeType','sizeBytes'}
                or type(record['version']) is not int or record['version'] != 1 or record['kind'] != 'file'
                or not isinstance(record['contextId'], str) or not isinstance(record['label'], str)):
            fail('attachment_closure_unqualified')
        value = dict(type=record['kind'], id=record['attachmentId'], name=record['name'],
                     mimeType=record['mimeType'], sizeBytes=record['sizeBytes'])
        upload = selected_upload(value, owner)
        if declared.get((owner, upload['id'])) != value:
            fail('attachment_closure_unqualified')
        return
    output = item.get('output')
    if (table == V2+'turn_items' and path == '$.output.htmlRender.attachmentId'
            and item.get('threadId') == owner and item.get('type') == 'dynamic_tool'
            and item.get('toolName') == 't3-code.html_render' and item.get('status') == 'completed'
            and isinstance(output, dict) and not set(output)-{'htmlRender','message','isError'}
            and output.get('isError', False) is False):
        render = output.get('htmlRender')
        if (isinstance(render, dict) and not set(render)-{'attachmentId','title','height','heights'}
                and isinstance(render.get('title'), str) and type(render.get('height')) in (int,float)
                and isinstance(render.get('attachmentId'), str)
                and (owner, render.get('attachmentId')) in html):
            heights = render.get('heights', {})
            if isinstance(heights, dict) and all(type(v) in (int,float) for v in heights.values()):
                return
    fail('attachment_closure_unqualified')


def freeze(root, directory, ids):
    from .t3code_cleanup_files import _html_render_attachments
    from .t3code_database import THREAD_TABLES, _where
    from .t3code_references import content_reference
    declared, protected, selected_names, allowed = {}, set(), set(), set()
    owners = {}
    with closing(connect_readonly(database_path(root))) as db:
        db.execute('BEGIN')
        db.create_function('lam_content_reference', 5, content_reference, deterministic=True)
        attachments = rows(db, f'''SELECT t.thread_id,a.value AS metadata
            FROM {V2}messages AS t,json_each(t.payload_json,'$.attachments') AS a WHERE a.type='object' ''')
        normalized = []
        for row in attachments:
            value = strict_json(row['metadata'])
            if row['thread_id'] in ids:
                upload = selected_upload(value, row['thread_id'])
                key = row['thread_id'], value['id']
                if key in declared and declared[key] != value:
                    fail('attachment_closure_unqualified')
                declared[key] = value
                normalized.append(upload)
            elif value.get('type') in ('file','image'):
                normalized.append(dict(value, thread_id=row['thread_id']))
        renders = _html_render_attachments(db)
        html = {(r['thread_id'],r['id']) for r in renders if r['thread_id'] in ids}
        normalized.extend(renders)
        for row in normalized:
            names = names_for(row)
            allowed.update(names)
            for name in names:
                owners.setdefault(name, set()).add(row['thread_id'])
            if row['thread_id'] in ids:
                if not names:
                    fail('attachment_closure_unqualified')
                selected_names.update(names)
                protected.add(row['id'])
                if 'sizeBytes' in row:
                    primary = next(name for name in names if not name.endswith('.txt') or len(names) == 1)
                    path = root/directory['path']/primary
                    if path.is_file() and path.stat().st_size != row['sizeBytes']:
                        fail('attachment_size_changed')
        if any(len(owners[name]) != 1 for name in selected_names):
            fail('attachment_owner_ambiguous')
        for table in (*THREAD_TABLES, 'orchestration_events'):
            columns = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
            if 'payload_json' not in columns:
                continue
            owner = 'stream_id' if table == 'orchestration_events' else 'thread_id'
            event = 't.event_type' if 'event_type' in columns else 'NULL'
            hits = rows(db, f'''SELECT t.{owner} AS owner,t.payload_json,j.fullkey,{event} AS event_type
                FROM {table} AS t,json_tree(t.payload_json) AS j WHERE {_where('t.'+owner,ids)} AND
                ((j.key='attachments' AND (j.type!='array' OR json_array_length(j.value)>0))
                 OR (j.key='attachmentId' AND j.type!='null'))
                AND NOT lam_content_reference(?,'payload_json',t.payload_json,j.fullkey,{event})''', tuple(ids)+(table,))
            for row in hits:
                check_reference(dict(row, table=table), declared, html)
    entries = set(directory.get('entries', ()))
    if entries - allowed:
        fail('attachment_closure_unqualified')
    changes = [frozen_files.freeze_remove(root, directory['path']+'/'+name) for name in sorted(entries & selected_names)]
    preserved = [frozen_files.read_file(root, directory['path']+'/'+name)[0] for name in sorted(entries-selected_names)]
    return changes, preserved, sorted(protected)
