"""Opt-in OpenSSH loopback acceptance. Run with python -m tests.remote_ssh_acceptance."""
import asyncio
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from local_agent_manager import remote_doctor, remote_records
from local_agent_manager.cli import main


class LoopbackSSH(unittest.IsolatedAsyncioTestCase):
    async def test_doctor_and_lost_apply_reply(self):
        import asyncssh  # Only this opt-in test needs the SSH server library.

        work = tempfile.TemporaryDirectory(prefix='lam-ssh-')
        self.addCleanup(work.cleanup)
        base = Path(work.name).resolve()
        agent = base / 'agent'
        sessions = agent / 'sessions'
        project = sessions / 'fixture'
        project.mkdir(parents=True)
        for sid in ('chosen', 'keeper'):
            (project / (sid + '.jsonl')).write_text(json.dumps({
                'type': 'session', 'id': sid, 'version': 3, 'cwd': str(project),
                'timestamp': '2026-09-01T00:00:00Z'}) + '\n', encoding='utf-8')
        keeper = (project / 'keeper.jsonl').read_bytes()
        env = {k: v for k, v in os.environ.items() if k.upper() in {
            'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATH', 'PATHEXT', 'PYTHONPATH', '_LAM_TEST_INSTALLATION'}}
        env.update({k: str(base/k) for k in ('USERPROFILE', 'HOME', 'APPDATA', 'LOCALAPPDATA', 'XDG_CONFIG_HOME',
                                               'CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'T3CODE_HOME', 'PASEO_HOME')})
        env.update(PI_CODING_AGENT_DIR=str(agent), TEMP=str(base), TMP=str(base), TMPDIR=str(base))
        host_key = asyncssh.generate_private_key('ssh-ed25519')
        identity = base / 'client-key'
        # The native client creates its required private-key ACL on Windows.
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(identity)],
                       stdin=subprocess.DEVNULL, check=True, timeout=15)
        client_public = asyncssh.read_public_key(str(identity) + '.pub')

        class Server(asyncssh.SSHServer):
            def public_key_auth_supported(self):
                return True

            def validate_public_key(self, username, key):
                return username == 'fixture' and key == client_public

        applied = []

        async def handle(process):
            if process.command == 'echo ' + remote_doctor.SHELL_MARKER:
                process.stdout.write(remote_doctor.SHELL_MARKER.encode() + b'\n')
                process.exit(0)
                return
            if process.command not in ('local-agent-manager --version', 'local-agent-manager remote-worker'):
                process.exit(127)
                return
            argument = process.command.split()[-1]
            raw = await process.stdin.read()
            child = await asyncio.create_subprocess_exec(
                sys.executable, '-m', 'local_agent_manager', argument, env=env, cwd=base,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            output, errors = await child.communicate(raw)
            if argument == 'remote-worker' and json.loads(raw)['verb'] == 'apply' and child.returncode == 0:
                applied.append(json.loads(output)['goal_status'])
                process.channel.get_connection().abort()  # The worker already persisted its receipt.
                return
            process.stdout.write(output)
            process.stderr.write(errors)
            process.exit(child.returncode)

        async with asyncssh.listen('127.0.0.1', 0, server_factory=Server, server_host_keys=[host_key],
                                   process_factory=handle, encoding=None) as listener:
            port = listener.get_port()
            known = base / 'known_hosts'
            known.write_bytes(f'[127.0.0.1]:{port} '.encode() + host_key.export_public_key())
            config = base / 'ssh_config'
            config.write_text(f'Host lam-loopback\n HostName 127.0.0.1\n Port {port}\n User fixture\n'
                              f' IdentityFile "{identity.as_posix()}"\n IdentitiesOnly yes\n IdentityAgent none\n'
                              f' UserKnownHostsFile "{known.as_posix()}"\n GlobalKnownHostsFile none\n', encoding='utf-8')
            real_run = subprocess.run

            def use_fixture_config(command, **kwargs):
                self.assertEqual(command[0], 'ssh')
                kwargs['stderr'] = subprocess.PIPE
                result = real_run(['ssh', '-F', str(config), *command[1:]], **kwargs)
                if result.returncode == 255 and not applied:
                    self.fail(result.stderr.decode(errors='replace'))
                return result

            async def call(verb, *options, ok=True):
                out = io.StringIO()
                code = await asyncio.to_thread(main, [
                    'remote', verb, '--ssh-host', 'lam-loopback', '--client', 'pi', '--root', str(sessions),
                    '--pi-agent-dir', str(agent), *options], stdout=out, stderr=io.StringIO())
                result = json.loads(out.getvalue())
                self.assertEqual(code == 0, ok, result)
                return result

            with patch.object(remote_records.subprocess, 'run', use_fixture_config):
                doctor = await call('doctor')
                inventory = await call('records')
                self.assertEqual(doctor['owner'], inventory['owner'])
                owner_id = doctor['owner']['store_id']
                plan = await call('plan', '--owner', owner_id, '--record-id', 'chosen')
                self.assertEqual(plan['goal_status'], 'ready', plan)
                binding = ('--owner', owner_id, '--operation-id', plan['operation_id'], '--approval', plan['approval'])
                lost = await call('apply', *binding, '--clients-closed', ok=False)
                self.assertEqual(lost['goal_status'], 'unknown', lost)
                for verb in ('status', 'verify'):
                    self.assertEqual((await call(verb, *binding))['goal_status'], 'complete')
                self.assertEqual(applied, ['complete'])
                self.assertFalse((project/'chosen.jsonl').exists())
                self.assertEqual((project/'keeper.jsonl').read_bytes(), keeper)


if __name__ == '__main__':
    unittest.main()
