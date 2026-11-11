"""Retained T3 transcript values versus real restoration references."""
import json
import sqlite3
import unittest

from local_agent_manager.t3code_references import require_no_references, ReferenceConflict

TARGET = '12345678-1234-4234-8234-123456789012'


class ReferencePolicyTests(unittest.TestCase):
    def check(self, payload, *, event='turn-item.updated', blocked=False):
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE orchestration_events(event_type TEXT,payload_json TEXT)')
            # Escaping characters must neither hide an edge nor change content.
            raw = json.dumps(payload).replace('12345678', r'\u0031' + '2345678')
            db.execute('INSERT INTO orchestration_events VALUES(?,?)', (event, raw))
            options = dict(code='test_reference', rows=[('orchestration_events', '1', ())], json_only=True)
            if blocked:
                with self.assertRaises(ReferenceConflict) as failure:
                    require_no_references(db, [TARGET], **options)
                self.assertEqual(failure.exception.kind, 'test_reference')
                self.assertNotIn('PRIVATE_CONTENT', str(failure.exception))
            else:
                require_no_references(db, [TARGET], **options)
                require_no_references(db, [TARGET], code='test_cross_profile')
            self.assertEqual(db.execute('SELECT payload_json FROM orchestration_events').fetchone()[0], raw)

    def test_content_mirrors_are_preserved(self):
        for payload in ({'type': 'reasoning', 'text': 'PRIVATE_CONTENT ' + TARGET},
                        {'type': 'file_change', 'diffStr': '+ ' + TARGET},
                        {'type': 'subagent', 'prompt': TARGET, 'result': TARGET}):
            with self.subTest(payload_type=payload['type']):
                self.check(payload)
        self.check({'role': 'user', 'context': {'version': 1, 'records': [
            {'version': 1, 'kind': 'thread', 'threadId': TARGET, 'contextId': 'thread_' + TARGET,
             'label': TARGET, 'title': TARGET}]}}, event='message.updated')

    def test_structural_unknown_and_key_references_remain_protected(self):
        for payload in ({'type': 'reasoning', 'nativeItemRef': {'resume': TARGET}},
                        {'type': 'file_change', 'fileName': 'conversation://' + TARGET},
                        {'type': 'subagent', 'childThreadId': TARGET},
                        {'type': 'future_type', 'text': TARGET},
                        {'type': {}, 'text': TARGET},
                        {'type': 'dynamic_tool', 'toolName': 't3-code.t3_thread_read',
                         'output': {TARGET: 'PRIVATE_CONTENT'}},
                        {'type': 'reasoning', 'savedResume': 'conversation://' + TARGET}):
            with self.subTest(payload_type=str(payload['type'])):
                self.check(payload, blocked=True)
        self.check({'type': 'reasoning', 'text': TARGET}, event='unknown.updated', blocked=True)
        self.check({'role': 'user', 'context': {'records': [
            {'kind': 'thread', 'threadId': TARGET, 'restore': TARGET}]}}, event='message.updated', blocked=True)
