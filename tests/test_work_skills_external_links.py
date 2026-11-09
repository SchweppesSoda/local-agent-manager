from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

try:
    from tests.test_work_skills_v3_e2e import ClientRegistryFixture, create_real_directory_link, manager
except ModuleNotFoundError:
    from test_work_skills_v3_e2e import ClientRegistryFixture, create_real_directory_link, manager


class ExternalLinkPreservationTests(unittest.TestCase):
    def prepare(self, base):
        fixture = ClientRegistryFixture(base / 'repo')
        fixture.add_skill('alpha')
        external = base / 'external'
        external.mkdir()
        final = external / 'versioned-skill'
        final.mkdir()
        (final / 'SKILL.md').write_text('# External skill\n', encoding='utf-8')
        sentinel = final / 'keep.txt'
        sentinel.write_text('external owner controls this file\n', encoding='utf-8')
        return fixture, external, final, sentinel

    def chain(self, fixture, middle, final, name='external-personal'):
        create_real_directory_link(final, middle)
        consumer = fixture.mounts['agents-shared'] / name
        create_real_directory_link(middle, consumer)
        return consumer

    def assert_blocked(self, fixture, consumer):
        with fixture.context():
            plan = manager.build_plan(fixture.options(['cindy']))
        actions = [a for a in plan.actions if a.path == consumer]
        self.assertTrue(any(a.blocking for a in actions), [a.kind for a in actions])
        self.assertNotIn('PRESERVE_UNMANAGED_LINK', [a.kind for a in actions])

    def test_external_chain_survives_real_apply_without_ownership_or_native_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture, external, final, sentinel = self.prepare(Path(tmp))
            middle = external / 'active'
            consumer = self.chain(fixture, middle, final)
            snapshots = (manager.snapshot_link(consumer), manager.snapshot_link(middle), sentinel.read_bytes())
            options = fixture.options(['cindy'], yes=True)
            with fixture.context():
                plan = manager.build_plan(options)
                self.assertFalse(plan.blockers)
                actions = [a for a in plan.actions if a.path == consumer]
                self.assertEqual([a.kind for a in actions], ['PRESERVE_UNMANAGED_LINK'])
                options.plan_id = plan.plan_id
                with redirect_stdout(io.StringIO()):
                    manager.apply_plan(plan, options)
                state = fixture.manifest()
                self.assertIsNone(manager.manifest_consumer(state, 'agents-shared', 'external-personal'))
                self.assertFalse(manager.manifest_tracks_consumer_path(state, consumer))
                alpha = manager.manifest_consumer(state, 'agents-shared', 'alpha')
                self.assertTrue(alpha['deploymentOwned'])
                self.assertFalse(alpha['nativeVerified'])
            self.assertEqual(snapshots, (manager.snapshot_link(consumer), manager.snapshot_link(middle), sentinel.read_bytes()))
            self.assertTrue(manager.direct_link_to(fixture.mounts['agents-shared'] / 'alpha', fixture.skills / 'alpha'))

    def test_expected_source_name_still_requires_a_direct_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture, external, final, _ = self.prepare(Path(tmp))
            consumer = self.chain(fixture, external / 'active', final, name='alpha')
            self.assert_blocked(fixture, consumer)

    def test_registered_path_is_protected_under_current_or_retired_target(self):
        for target in ('agents-shared', 'retired-target'):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as tmp:
                fixture, external, final, _ = self.prepare(Path(tmp))
                consumer = self.chain(fixture, external / 'active', final)
                with fixture.context():
                    state = manager.empty_manifest(fixture.root)
                    state['consumers'][target] = {'skills': {'external-personal': {'path': str(consumer), 'deploymentOwned': True}}}
                    fixture.write_manifest(state)
                self.assert_blocked(fixture, consumer)

    def test_intermediate_repository_hop_cannot_escape_to_external_final(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture, _, final, _ = self.prepare(Path(tmp))
            consumer = self.chain(fixture, fixture.root / 'temporary-alias', final)
            self.assert_blocked(fixture, consumer)

    def test_final_repository_directory_is_not_external(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture, external, _, _ = self.prepare(Path(tmp))
            final = fixture.root / 'docs' / 'not-a-source-skill'
            final.mkdir(parents=True)
            consumer = self.chain(fixture, external / 'active', final)
            self.assert_blocked(fixture, consumer)

    def test_directory_alias_resolving_into_repository_is_not_external(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture, external, _, _ = self.prepare(Path(tmp))
            alias = external / 'repo-alias'
            create_real_directory_link(fixture.root, alias)
            consumer = self.chain(fixture, external / 'active', alias / 'skills' / 'alpha')
            self.assert_blocked(fixture, consumer)

    def test_broken_and_looping_external_links_keep_existing_diagnostics(self):
        for mode, expected in (('broken', 'BROKEN_LINK'), ('loop', 'LINK_LOOP')):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                fixture, external, _, _ = self.prepare(Path(tmp))
                middle = external / 'active'
                final = external / 'missing-or-loop'
                create_real_directory_link(final, middle)
                if mode == 'loop':
                    create_real_directory_link(middle, final)
                consumer = fixture.mounts['agents-shared'] / 'external-personal'
                create_real_directory_link(middle, consumer)
                with fixture.context():
                    plan = manager.build_plan(fixture.options(['cindy']))
                self.assertEqual([a.kind for a in plan.actions if a.path == consumer], [expected])


if __name__ == '__main__':
    unittest.main()
