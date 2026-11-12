from pathlib import Path
import tempfile
import unittest

from local_agent_manager.t3code_occupancy import resources


class OccupancyResourceTests(unittest.TestCase):
    def test_only_pending_data_resources_not_runtime_or_preserved_files(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            for name in ('userdata/statev2.sqlite', 'userdata/server-owner.sqlite',
                         'native/conversation_summaries.db', 'native/selected.json',
                         'native/retained.json', 'native/agy.exe', 'codex/state_5.sqlite'):
                path = root/name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'synthetic')
            def proof(path): return {'path': path, 'sha256': '0'*64}
            evidence = dict(root=str(root), files={'changes': []}, desktop=[], native_actions=[], native_targets=[
                dict(engine='codex', root=str(root/'codex'), action_ids=[], paths=[]),
                dict(engine='antigravity', root=str(root/'native'), action_ids=[], paths=[], native_files={
                    'summary_files': [proof('conversation_summaries.db')],
                    'changes': [{'files': [proof('selected.json')]}],
                    'preserved': [proof('retained.json')], 'runtime': proof('agy.exe')})])
            found = {Path(path).name for paths in resources(evidence).values() for path in paths}
            self.assertEqual(found, {'statev2.sqlite', 'server-owner.sqlite', 'conversation_summaries.db', 'selected.json'})
            evidence['native_targets'][1]['paths'] = [str(root/'codex/state_5.sqlite')]
            with self.assertRaisesRegex(ValueError, 'outside_store'):
                resources(evidence)
