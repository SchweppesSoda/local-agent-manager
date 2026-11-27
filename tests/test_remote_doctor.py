"""Exercise the public diagnostic and its read-only worker contract."""
from contextlib import ExitStack
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from local_agent_manager import __version__, remote_doctor, remote_records
from local_agent_manager.cli import main


class RemoteDoctorTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.shell = (0, remote_doctor.SHELL_MARKER.encode() + b'\r\n')
        self.manager = (0, __version__.encode() + b'\r\n')
        self.worker_reply = None
        self.ssh = stack.enter_context(patch.object(remote_records, '_ssh', side_effect=self.transport))
        for name in ('_records', '_operation', 'manager_state_base'):
            stack.enter_context(patch.object(remote_records, name, side_effect=AssertionError(name)))

    def transport(self, host, payload, *, command='local-agent-manager remote-worker', timeout, limit):
        self.assertEqual(host, 'qa-host')
        self.assertGreater(timeout, 0)
        self.assertLess(limit, remote_records.REPLY_LIMIT)
        if command.startswith('echo '):
            return self.shell
        if command == 'local-agent-manager --version':
            return self.manager
        self.assertEqual(command, 'local-agent-manager remote-worker')
        output = io.BytesIO()
        code = remote_records.worker(io.BytesIO(payload), output)
        raw = output.getvalue()
        return self.worker_reply(code, raw) if self.worker_reply else (code, raw)

    def call(self, *extra, root=None):
        out = io.StringIO()
        code = main(['remote', 'doctor', '--ssh-host', 'qa-host', '--client', 'claude',
                     '--root', str(root or self.root), *extra], stdout=out, stderr=io.StringIO())
        return code, json.loads(out.getvalue())

    def test_observed_owner_matches_inventory_identity_without_enumerating_records(self):
        owner = remote_records._owner('claude', self.root)
        code, result = self.call('--owner', owner['store_id'])
        self.assertEqual(code, 0, result)
        self.assertEqual(result['owner'], owner)
        self.assertEqual(set(result['checks'].values()), {'ok'})
        self.assertEqual(result['protocol'], remote_records.PROTOCOL)
        self.assertEqual(result['manager_version'], __version__)
        self.assertTrue(result['execution_host'])
        self.assertFalse(list(self.root.iterdir()))

    def test_ssh_manager_and_store_fail_independently(self):
        self.shell = (255, b'private diagnostics')
        code, result = self.call()
        self.assertEqual((code, result['checks']['manager'], self.ssh.call_count), (1, 'not_checked', 1))
        self.assertEqual(result['error'], 'ssh_connection_unverified')
        self.shell = (0, remote_doctor.SHELL_MARKER.encode())
        self.manager = (127, b'private login banner')
        code, result = self.call()
        self.assertEqual((code, result['error']), (1, 'remote_manager_not_runnable'))
        self.assertEqual(result['checks']['protocol'], 'not_checked')
        self.assertNotIn('private', json.dumps(result))
        self.manager = (0, __version__.encode())
        code, result = self.call(root=self.root/'absent')
        self.assertEqual((code, result['error']), (1, 'store_path_or_identity_file_missing'))
        self.assertEqual(result['checks']['protocol'], 'ok')
        self.assertTrue(result['execution_host'])
        code, result = self.call('--owner', '0' * 64)
        self.assertEqual((code, result['error']), (1, 'store_identity_changed'))
        self.assertNotEqual(result['owner']['store_id'], '0' * 64)

    def test_unbound_replies_or_an_old_worker_never_confirm_a_store(self):
        def old_worker(code, raw):
            reply = json.loads(raw)
            return 1, json.dumps({k: reply[k] for k in ('protocol', 'request_sha256', 'destination')}
                                 | {'error': 'remote_request_invalid'}).encode()

        for transform, reason in (
            (lambda code, raw: (code, raw.replace(b'qa-host', b'other-host')), 'worker_response_unverified'),
            (lambda code, raw: (code, b'private invalid reply'), 'worker_response_unverified'),
            (old_worker, 'worker_diagnostic_unsupported'),
        ):
            with self.subTest(reason=reason):
                self.worker_reply = transform
                code, result = self.call()
                self.assertEqual((code, result['error']), (1, reason))
                self.assertEqual(result['checks']['store'], 'not_checked')
                self.assertNotIn('owner', result)

    def test_peer_version_is_observed_and_the_report_is_bound_to_the_requested_root(self):
        self.manager = (0, b'12.34.0\n')
        with patch.object(remote_doctor, '__version__', '12.34.0'):
            code, result = self.call()
            self.assertEqual((code, result['manager_version']), (0, '12.34.0'))

            def changed_root(code, raw):
                reply = json.loads(raw)
                reply['owner']['root'] = str(self.root / 'different')
                return code, json.dumps(reply).encode()

            self.worker_reply = changed_root
            code, result = self.call()
            self.assertEqual((code, result['error']), (1, 'store_identity_unverified'))
            self.assertNotIn('owner', result)

    def test_invalid_owner_or_timeout_stops_before_the_worker(self):
        self.assertEqual(self.call('--owner', 'invalid')[0], 1)
        self.ssh.assert_not_called()
        self.ssh.side_effect = subprocess.TimeoutExpired('ssh', 20)
        code, result = self.call()
        self.assertEqual((code, result['error']), (1, 'ssh_probe_timed_out'))

    def test_diagnostic_request_rejects_mutation_fields(self):
        request = {'protocol': remote_records.PROTOCOL, 'verb': 'doctor', 'client': 'claude',
                   'destination': 'qa-host', 'root': str(self.root), 'clients_closed': True}
        output = io.BytesIO()
        self.assertEqual(remote_records.worker(io.BytesIO(json.dumps(request).encode()), output), 1)
        self.assertEqual(json.loads(output.getvalue())['error'], 'remote_request_invalid')
