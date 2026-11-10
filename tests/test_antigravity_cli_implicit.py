"""The authenticated empty subset, including actual Windows held-file recovery."""
import hashlib
import os
from pathlib import Path
import unittest

from local_agent_manager import antigravity_cli_implicit as implicit
from tests.implicit_support import plaintext, encrypted
from tests.native_recovery_support import field

OWNER = '41414141-4141-4141-8141-414141414141'
TARGET = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'


class EmptyTests(unittest.TestCase):
    def test_empty_shape_and_real_reference_fields(self):
        record = implicit.empty_trajectory(plaintext(OWNER), OWNER, {TARGET})
        self.assertEqual(record['restoration_edges'], [])
        cases = {
            'target-trajectory': plaintext(OWNER, trajectory=TARGET),
            'target-state': plaintext(OWNER, state=TARGET),
            'parent-reference': plaintext(OWNER, trajectory_extra=field(5, field(1, TARGET.encode()))),
            'parent-conversation': plaintext(OWNER, metadata_extra=field(5, TARGET.encode())),
            'root-conversation': plaintext(OWNER, metadata_extra=field(6, TARGET.encode())),
            'base-identifier': plaintext(OWNER, outer_extra=field(2, field(1, TARGET.encode()))),
            'nonempty-steps': plaintext(OWNER, trajectory_extra=field(2, b'')),
            'unknown-field': plaintext(OWNER, outer_extra=field(31, b'')),
            'weak-id': plaintext(OWNER, trajectory='short-id'),
            'external-uri': plaintext(OWNER, uri=b'https://example.invalid/restore'),
            'unc-uri': plaintext(OWNER, uri=b'file://server/share/restore'),
            'traversal-uri': plaintext(OWNER, uri=b'file:///D:/work/../restore'),
            'duplicate-field': plaintext(OWNER, outer_extra=field(5, b'')),
            'truncated-proto': plaintext(OWNER)[:-1],
        }
        for label, raw in cases.items():
            with self.subTest(label=label), self.assertRaises(ValueError):
                implicit.empty_trajectory(raw, OWNER, {TARGET})
        with self.assertRaises(ValueError):
            implicit.empty_trajectory(plaintext(TARGET), TARGET, {TARGET})
        with self.assertRaises(ValueError):
            implicit.empty_trajectory(plaintext(OWNER), TARGET, {TARGET})

    @unittest.skipUnless(os.name == 'nt', 'Windows CNG')
    def test_gcm_known_vector_and_bad_authentication(self):
        # AES-256 GCM, zero key/nonce, zero-length plaintext, standard tag.
        raw = bytes(12)+bytes.fromhex('530f8afbc74536b9a963b4f1c4cb738b')
        self.assertEqual(implicit._decrypt(bytes(32), raw), b'')
        for damaged in (raw[:-1], raw[:-1]+bytes([raw[-1]^1]), b'not protobuf', bytes(16385)):
            with self.subTest(length=len(damaged)), self.assertRaises(ValueError):
                implicit._decrypt(bytes(32), damaged)


@unittest.skipUnless(os.name == 'nt' and os.environ.get('LAM_REHEARSAL_AGY_BINARY'),
                     'opt-in exact installed image, synthetic stores only')
class ProducerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.image = Path(os.environ['LAM_REHEARSAL_AGY_BINARY']).read_bytes()
        assert hashlib.sha256(cls.image).hexdigest() == implicit.RUNTIME_SHA256

    def test_actual_producer_authenticated_shape_and_failure_cases(self):
        proof = {'size': len(self.image), 'sha256': hashlib.sha256(self.image).hexdigest()}
        for label, options in (
                ('safe', {}), ('parent', {'trajectory_extra':field(5, field(1, TARGET.encode()))}),
                ('base', {'outer_extra':field(2, field(1, TARGET.encode()))}),
                ('unknown', {'outer_extra':field(31,b'')}),
                ('external', {'uri':b'file://outside/restore'})):
            raw = encrypted(self.image, plaintext(OWNER, **options))
            file = {'path':'implicit/'+OWNER+'.pb', 'sha256':hashlib.sha256(raw).hexdigest()}
            run = lambda: implicit.qualify(lambda:(proof,self.image), [file], lambda p:(file,raw), {TARGET})
            if label == 'safe':
                self.assertTrue(run()['target_absent'])
            else:
                with self.subTest(label=label), self.assertRaises(ValueError): run()
        with self.assertRaisesRegex(ValueError, 'producer_unqualified'):
            implicit.qualify(lambda:({'size':0,'sha256':'wrong'},b''), [], None, {TARGET})


@unittest.skipUnless(os.name == 'nt' and os.environ.get('LAM_REHEARSAL_AGY_BINARY'),
                     'opt-in installed image, synthetic stores only')
class RecoveryTests(unittest.TestCase):
    def setUp(self):
        import shutil
        from tests.test_joint_cli_recovery_topology import TopologyTests
        from tests.implicit_support import seed_implicit
        self.topology = t = TopologyTests('runTest')
        def configure():
            shutil.copy2(os.environ['LAM_REHEARSAL_AGY_BINARY'], t.f.agy)
            (t.f.native/'implicit/original.pb').unlink()
            seed_implicit(t.f.native, t.f.agy, OWNER)
        t.configure_implicit = configure
        t.setUp()
        self.addCleanup(t.doCleanups)
        self.image = t.f.agy.read_bytes()
        self.owner = '52525252-5252-4252-8252-525252525252'
        (t.f.native/'implicit'/(OWNER+'.pb')).unlink()
        self.path = t.f.native/'implicit'/(self.owner+'.pb')
        self.path.write_bytes(encrypted(self.image, plaintext(self.owner)))

    def test_first_and_second_cold_rotation(self):
        t = self.topology
        result, payload = t.cold('implicit-first-cold')
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace')+str(payload))
        self.assertTrue(payload['goal_satisfied'])
        self.assertFalse(t.store.lock_exists())
        audit = next(t.store.directory.glob('recovery-*.json')).read_bytes()
        self.path.unlink()
        owner = '63636363-6363-4363-8363-636363636363'
        self.path = self.path.with_name(owner+'.pb')
        self.path.write_bytes(encrypted(self.image, plaintext(owner)))
        result, payload = t.cold('implicit-second-cold')
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace')+str(payload))
        self.assertTrue(payload['recovery_current'][0]['current_native']['implicit']['target_absent'])
        self.assertEqual(next(t.store.directory.glob('recovery-*.json')).read_bytes(), audit)

    def test_negative_cold_cases_keep_original_lock_and_no_audit(self):
        from tests.test_antigravity_cli_joint import TARGET as selected
        cases = {
            'trajectory': plaintext(self.owner, trajectory=selected),
            'cascade': plaintext(selected),
            'initialization': plaintext(self.owner, state=selected),
            'parent': plaintext(self.owner, trajectory_extra=field(5,field(1,selected.encode()))),
            'parent-metadata': plaintext(self.owner, metadata_extra=field(5,selected.encode())),
            'base': plaintext(self.owner, outer_extra=field(2,field(1,selected.encode()))),
            'unknown': plaintext(self.owner, outer_extra=field(31,b'')),
            'nonempty': plaintext(self.owner, trajectory_extra=field(2,b'')),
            'external': plaintext(self.owner, uri=b'file://outside/restore'),
            'weak': plaintext(self.owner, trajectory='weak'),
            'malformed-protobuf': b'\x0a\xff',
        }
        for label, raw in cases.items():
            self.path.write_bytes(encrypted(self.image, raw))
            self.topology.assert_blocked('implicit-'+label)
        good = encrypted(self.image, plaintext(self.owner))
        for label, raw in (('truncated',good[:-1]), ('bad-tag',good[:-1]+bytes([good[-1]^1])),
                           ('opaque-format',b'unknown format')):
            self.path.write_bytes(raw)
            self.topology.assert_blocked('implicit-'+label)
        self.path.write_bytes(good)
        other = next(p for p in self.path.parent.iterdir() if p != self.path)
        other.unlink()
        self.topology.assert_blocked('implicit-shrunken-window')

    def test_third_party_handle_and_continuous_leases(self):
        from local_agent_manager.windows_held_files import exclusive_fd
        from local_agent_manager.joint_cli_recovery import recovery_scope, native_evolution
        from local_agent_manager import antigravity_cli_joint as joint
        t = self.topology
        fd = exclusive_fd(self.path)
        try:
            t.assert_blocked('implicit-third-party-handle')
        finally:
            os.close(fd)
        with recovery_scope(t.document, t.f.coordinator(), t.plan['plan_sha256']):
            self.assertTrue(native_evolution(joint.targets(t.evidence)[0]['native_files']))
            with self.assertRaises(PermissionError): self.path.read_bytes()
            with self.assertRaises(PermissionError): self.path.unlink()
            with self.assertRaises(PermissionError): t.f.agy.read_bytes()
        self.assertEqual(t.store.lock_path.read_bytes(), t.original_lock)

    def test_known_root_conversation_base_reference_is_rejected(self):
        from tests.test_antigravity_cli_joint import TARGET as selected
        self.path.write_bytes(encrypted(self.image, plaintext(self.owner,
            metadata_extra=field(6, selected.encode()))))
        self.topology.assert_blocked('implicit-known-root-base')


if __name__ == '__main__':
    unittest.main()
