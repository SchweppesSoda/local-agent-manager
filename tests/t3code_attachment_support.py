"""Synthetic upload, generated HTML and their metadata mirrors."""
from contextlib import closing
import json
import re
import sqlite3

from tests.t3code_support import V2, insert


def seed_attachments(root, owner):
    segment = re.sub(r'[^a-z0-9_-]+', '-', owner.lower()).strip('-_')[:80].rstrip('-_')
    upload = segment+'-aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa-pptx'
    render = segment+'-bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb-html'
    directory = root/'userdata/attachments'
    directory.mkdir(parents=True, exist_ok=True)
    paths = [directory/(upload+'.pptx'), directory/(upload+'.pptx.txt'), directory/(render+'.html')]
    for path in paths:
        path.write_bytes(b'synthetic attachment')
    attachment = dict(type='file', id=upload, name='example.pptx', mimeType='application/vnd.openxmlformats-officedocument.presentationml.presentation', sizeBytes=20)
    context = dict(version=1, contextId='file:synthetic', label='synthetic', kind='file', attachmentId=upload,
                   name=attachment['name'], mimeType=attachment['mimeType'], sizeBytes=20)
    message = dict(id='message-'+owner,threadId=owner,role='user',text='synthetic',attachments=[attachment],context={'records':[context]})
    tool = dict(id='render-'+owner,threadId=owner,type='dynamic_tool',toolName='t3-code.html_render',status='completed',
                output={'htmlRender':{'attachmentId':render,'title':'synthetic','height':250,'heights':{'800':250}}})
    with closing(sqlite3.connect(root/'userdata/statev2.sqlite')) as db:
        db.execute(f'UPDATE {V2}messages SET payload_json=? WHERE thread_id=?',(json.dumps(message),owner))
        insert(db,V2+'turn_items',turn_item_id='upload-'+owner,thread_id=owner,type='user_message',status='completed',
               payload_json=json.dumps({**message,'type':'user_message'}))
        insert(db,V2+'turn_items',turn_item_id='render-'+owner,thread_id=owner,type='dynamic_tool',status='completed',payload_json=json.dumps(tool))
        insert(db,'orchestration_events',event_id='render-event-'+owner,stream_id=owner,aggregate_kind='thread',stream_version=1,
               event_type='turn-item.updated',payload_json=json.dumps(tool),metadata_json='{}')
        db.commit()
    return paths, attachment, message, tool
