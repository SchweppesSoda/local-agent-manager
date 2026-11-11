"""Transcript preservation versus actual restoration edges, synthetic stores."""
from contextlib import closing
import json
import sqlite3
import tempfile
from pathlib import Path
import unittest

from local_agent_manager import antigravity_cli_cleanup as native
from local_agent_manager import antigravity_cli_references as refs
from local_agent_manager.antigravity_cli_current import CurrentNative, _reject_reference
from tests.test_antigravity_cli_cleanup import fixture, TARGET, KEEP


def varint(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    return bytes(result)+bytes([value])


def field(number, value):
    if isinstance(value, int):
        return varint(number*8)+varint(value)
    return varint(number*8+2)+varint(len(value))+value


def tool(name='run_command', arguments=None):
    arguments = arguments or {'CommandLine': 'inspect '+TARGET}
    return field(2, name.encode())+field(3, json.dumps(arguments).encode())


def step(kind, payload, **columns):
    return dict(idx=1, step_type=kind, step_format=0, has_subtrajectory=0,
                step_payload=field(1, kind)+payload, **columns)


class ReferenceTests(unittest.TestCase):
    def check(self, row):
        for reject in (native._reject_trajectory_reference, _reject_reference):
            refs.check_step(row, [TARGET], reject)

    def test_known_text_fields_and_tool_audit_preserve_target_mentions(self):
        metadata = field(4, tool())
        result = field(1, TARGET.encode())+field(6,
            field(1, b'type.googleapis.com/exa.cortex.core.tools.RunCommand')+
            field(2, field(6, TARGET.encode())))
        generic = field(1, field(1, b'CommandLine')+field(2, TARGET.encode()))+field(2, result)
        rows = [step(15, field(20, field(3, TARGET.encode())+field(7, tool()))),
                step(23, field(30, field(5, TARGET.encode()))),
                step(132, field(5, metadata)+field(140, generic), metadata=metadata),
                step(101, field(114, field(4, field(4, TARGET.encode()))))]
        for row in rows:
            with self.subTest(kind=row['step_type']): self.check(row)
        refs.check_generation(field(1, field(2, TARGET.encode())), [TARGET], _reject_reference)

    def test_unknown_tools_fields_relationships_and_malformed_payloads_block(self):
        rows = [
            step(15, field(20, field(7, tool('send_message', {'conversationId': TARGET})))),
            step(15, field(20, field(7, tool('manage_subagents', {'conversationId': TARGET})))),
            step(15, field(20, field(7, tool(arguments={'resumeId': TARGET})))),
            step(15, field(20, field(99, TARGET.encode()))),
            step(23, field(30, field(20, field(1, TARGET.encode())))),
            step(101, field(114, field(4, field(3, TARGET.encode())))),
            step(127, field(143, field(5, TARGET.encode()))),
            step(132, field(5, field(4, tool('unknown')))+field(140, field(2, field(1, TARGET.encode())))),
            step(15, field(20, field(3, TARGET.encode())), render_info=field(10, field(4, field(1, TARGET.encode())))),
            step(15, field(20, field(3, TARGET.encode())), task_details=TARGET.encode()),
            step(15, field(20, field(3, TARGET.encode()))+b'\x80'),
            step(15, field(20, field(3, TARGET.encode()))+field(1, 132)),
        ]
        rows.append({**rows[0], 'step_payload': field(1, 132)+field(20, field(3, TARGET.encode()))})
        for index, row in enumerate(rows):
            with self.subTest(index=index), self.assertRaises(native.CleanupError): self.check(row)
        with self.assertRaises(native.CleanupError):
            refs.check_generation(field(3, field(1, TARGET.encode())), [TARGET], _reject_reference)

    def test_permission_audit_is_scoped_to_known_actions(self):
        def grant(action):
            return field(2, field(1, field(1, action)+field(2, TARGET.encode()))+field(6, TARGET.encode()))
        self.check(step(132, b'', permissions=grant(b'command')))
        with self.assertRaises(native.CleanupError):
            self.check(step(132, b'', permissions=grant(b'invoke_subagent')))

    def test_escaped_targets_and_unknown_any_are_protected(self):
        escaped = TARGET.replace('1', r'\u0031').encode()
        with self.assertRaises(native.CleanupError):
            self.check(step(15, field(20, field(99, escaped))))
        metadata = field(4, tool())
        unknown = field(1, b'type.googleapis.com/UnknownRestore')+field(2, TARGET.encode())
        with self.assertRaises(native.CleanupError):
            self.check(step(132, field(5, metadata)+field(140, field(2, field(6, unknown)))))

    def test_exact_diagnostic_leaves_and_message_content_only(self):
        base = 'brain/'+KEEP+'/.system_generated/'
        for leaf in ('logs/transcript.jsonl', 'logs/chunks/transcript_full/00000001.jsonl',
                     'steps/3/output.txt', 'tasks/task-812.log'):
            refs.check_artifact(base+leaf, TARGET.encode(), [TARGET], _reject_reference)
        for leaf in ('resume.json', 'steps/3/state.json', 'tasks/task-812.json',
                     'logs/restore.pb', '../artifact.md'):
            with self.subTest(leaf=leaf), self.assertRaises(native.CleanupError):
                refs.check_artifact(base+leaf, TARGET.encode(), [TARGET], _reject_reference)
        message = {'id': KEEP, 'sender': 'system', 'content': TARGET}
        path = base+'messages/'+KEEP+'.json'
        refs.check_artifact(path, json.dumps(message).encode(), [TARGET], _reject_reference)
        for key in ('sender', 'recipient', 'unknown_restore'):
            with self.subTest(key=key), self.assertRaises(native.CleanupError):
                refs.check_artifact(path, json.dumps({**message, key: TARGET}).encode(), [TARGET], _reject_reference)

    def test_plan_and_current_inspection_preserve_all_unselected_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root, binary = fixture(Path(directory))
            db_path = root/'conversations'/(KEEP+'.db')
            row = step(15, field(20, field(3, TARGET.encode())))
            with closing(sqlite3.connect(db_path)) as db:
                db.execute('INSERT INTO steps(idx,step_type,step_format,step_payload) VALUES(?,?,?,?)',
                           (1, row['step_type'], 0, row['step_payload']))
                db.execute('INSERT INTO gen_metadata(idx,data) VALUES(1,?)',
                           (field(1, field(2, TARGET.encode())),))
                db.commit()
            with closing(sqlite3.connect(root/native.SUMMARY)) as db:
                db.execute(native.SEARCH_VERSION_SQL)
                db.execute('INSERT INTO search_schema_version VALUES(3)')
                db.execute(native.SEARCH_SQL)
                db.execute('INSERT INTO conversations_fts(conversation_id,user_body) VALUES(?,?)', (KEEP, TARGET))
                db.commit()
            diagnostic = root/'brain'/KEEP/'.system_generated/steps/1/output.txt'
            diagnostic.parent.mkdir(parents=True)
            diagnostic.write_text(TARGET)
            before = db_path.read_bytes(), diagnostic.read_bytes()
            evidence = native.freeze(root, [TARGET], binary=binary)
            self.assertEqual(evidence['ids'], [TARGET])
            # Materialize the synthetic after-image; never use a live cleanup.
            import shutil
            for change in evidence['changes']:
                path = root/change['path']
                shutil.rmtree(path) if path.is_dir() else path.unlink()
            with closing(sqlite3.connect(root/native.SUMMARY)) as db:
                native._summary_transform(db, [TARGET])
            (root/native.CACHE).write_bytes(native.cache_after((root/native.CACHE).read_bytes(), [TARGET]))
            class Reader:
                read = staticmethod(native.ff.read_file)
            current = CurrentNative(evidence, Reader())
            current.trees = {name: native.ff.freeze_remove(root, name) for name in current.names
                             if name not in native.CONTROL and (root/name).is_dir()}
            proof = current.inspect()
            self.assertEqual(proof['blockers'], [])
            self.assertEqual(before, (db_path.read_bytes(), diagnostic.read_bytes()))


if __name__ == '__main__':
    unittest.main()
