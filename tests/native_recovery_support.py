"""Synthetic reopen changes. Never launches an Agent or touches a user home."""
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3

from local_agent_manager import antigravity_cli_cleanup as native
from local_agent_manager import antigravity_cli_restore as restore

NEW_NATIVE = '77777777-7777-4777-8777-777777777777'
LATER_NATIVE = '88888888-8888-4888-8888-888888888888'


def field(n, raw):
    size = len(raw)
    encoded = bytearray()
    while size >= 128:
        encoded.append((size & 127) | 128)
        size >>= 7
    encoded.append(size)
    return bytes([n*8+2]) + bytes(encoded) + raw


def mirrors(root):
    with closing(sqlite3.connect(root/native.SUMMARY)) as db:
        rows = list(db.execute('SELECT conversation_id,raw_summary FROM conversation_summaries ORDER BY conversation_id'))
    (root/restore.PROTO).write_bytes(b''.join(field(1, field(1, sid.encode())+field(2, raw)) for sid, raw in rows))
    records = {}
    for sid, _ in rows:
        summary = dict.fromkeys(('Title', 'Preview', 'ProjectID', 'AgentName', 'GroupID'), '')
        summary.update(ID=sid, NumSteps=0, Loaded=True, UpdatedAt='', AppDataDir='antigravity-cli',
                       Internal=False, Cloud=False, WorkspaceURIs=[], CloudProject={'ID': '', 'Name': ''},
                       PermissionPreset=0)
        records[sid] = {'summary': summary, 'is_internal': False, 'last_modified_time': ''}
    (root/restore.METADATA).write_text(json.dumps({'conversations': records}), encoding='utf-8')


def initial_sources(root):
    (root/'implicit').mkdir(exist_ok=True)
    (root/'implicit/original.pb').write_bytes(b'opaque unchanged original implicit evidence')
    (root/'cache/onboarding.json').write_text(json.dumps(dict.fromkeys(
        ('consumerOnboardingComplete', 'enterpriseOnboardingComplete', 'onboardingComplete'), False)))
    for name in ('log', 'crashes'):
        (root/name).mkdir(exist_ok=True)
    (root/'log/cli-original.log').write_bytes(b'original diagnostic')
    (root/'cli.log').symlink_to('log/cli-original.log')
    mirrors(root)


def reopen(root, keep, sid=NEW_NATIVE):
    shutil.copy2(root/'conversations'/(keep+'.db'), root/'conversations'/(sid+'.db'))
    with closing(sqlite3.connect(root/'conversations'/(sid+'.db'))) as db:
        db.execute('UPDATE trajectory_meta SET trajectory_id=?, cascade_id=?', (sid, sid))
        db.commit()
    with closing(sqlite3.connect(root/native.SUMMARY)) as db:
        columns = [r[1] for r in db.execute('PRAGMA table_info(conversation_summaries)')]
        values = list(db.execute('SELECT * FROM conversation_summaries WHERE conversation_id=?', (keep,)).fetchone())
        values[columns.index('conversation_id')] = sid
        db.execute('INSERT INTO conversation_summaries VALUES('+','.join('?' for _ in values)+')', values)
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='conversations_fts'").fetchone():
            db.execute('INSERT INTO conversations_fts(conversation_id,chunk_index,title,user_body) VALUES(?,0,?,?)',
                       (sid, 'new safe session', 'safe content'))
        db.commit()
    (root/'brain'/sid).mkdir()
    (root/'brain'/sid/'artifact.md').write_text('new protected artifact')
    (root/'annotations'/(sid+'.pbtxt')).write_text('title: "new protected session"\n')
    (root/'presence'/(sid+'.lock')).touch()
    mirrors(root)
    (root/native.CACHE).write_text(json.dumps({str(root.parent/'work'): sid}))
    (root/'cache/onboarding.json').write_text(json.dumps({
        'consumerOnboardingComplete': True, 'enterpriseOnboardingComplete': False, 'onboardingComplete': True}))
    name = 'cli-'+sid+'.log'
    (root/'log'/name).write_text('new diagnostics may mention historical IDs')
    (root/'crashes'/('crash-'+sid+'.log')).touch()
    (root/'cli.log').unlink()
    (root/'cli.log').symlink_to('log/'+name)
