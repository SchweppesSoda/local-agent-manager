import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from local_agent_record_janitor import t3code_localstorage as storage, office_leveldb as level


def local_storage(root, *, inspect=False, attachment=False):
    node, dependency, helper = level.runtime()
    path = Path(root) / storage.RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    script = r'''
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const h=require(process.argv[1]),q=JSON.parse(fs.readFileSync(0,'utf8'));
const req=require('node:module').createRequire(path.join(process.env.LARJ_LEVELDB_RUNTIME,'package.json'));
const {ClassicLevel}=req('classic-level');
const prefix=Buffer.from('_t3code://app\0'),key=n=>Buffer.concat([prefix,h.encodeString(n)]);
const draft={version:9,state:{draftsByThreadKey:{'env-1:thread-1':{prompt:'T3_PRIVATE',files:q.attachment?[{}]:[]},
 'env-1:keeper':{prompt:'KEEP'},'env-2:thread-1':{prompt:'REMOTE'}},draftThreadsByThreadKey:{},
 logicalProjectDraftThreadKeyByLogicalProjectKey:{'env-1:project-1':'env-1:thread-1'},stickyActiveProvider:'codex'}};
const db=new ClassicLevel(q.path,{keyEncoding:'buffer',valueEncoding:'buffer',createIfMissing:!q.inspect});
(async()=>{await db.open();if(!q.inspect){await db.batch([
 {type:'put',key:Buffer.from('VERSION'),value:Buffer.from('1')},
 {type:'put',key:key('t3code:composer-drafts:v1'),value:h.encodeString(JSON.stringify(draft))},
 {type:'put',key:key('opaque-settings'),value:Buffer.from('KEEP_OPAQUE')},
 {type:'put',key:Buffer.concat([Buffer.from('_https://other.example\0'),h.encodeString('foreign')]),value:Buffer.from('KEEP_FOREIGN')},
 {type:'put',key:Buffer.from('META:t3code://app'),value:Buffer.concat([Buffer.from([8]),h.varint(13000000000000000n),Buffer.from([16]),h.varint(100000n)])}
 ],{sync:true});await db.compactRange(Buffer.from(''),Buffer.from([255]),{keyEncoding:'buffer'});
}else{
 delete draft.state.draftsByThreadKey['env-1:thread-1'];delete draft.state.logicalProjectDraftThreadKeyByLogicalProjectKey['env-1:project-1'];
 assert.deepEqual(JSON.parse(h.decodeString(await db.get(key('t3code:composer-drafts:v1')))),draft);
 assert.equal((await db.get(key('opaque-settings'))).toString(),'KEEP_OPAQUE');
 assert.equal((await db.get(Buffer.concat([Buffer.from('_https://other.example\0'),h.encodeString('foreign')]))).toString(),'KEEP_FOREIGN');
}await db.close();console.log(JSON.stringify({verified:true}));})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run([str(node), "-e", script, str(helper)],
        input=json.dumps({"path": str(path), "inspect": inspect, "attachment": attachment}).encode(),
        capture_output=True, cwd=dependency, env=level._environment(dependency), timeout=30)
    if result.returncode or json.loads(result.stdout).get("verified") is not True:
        raise AssertionError("Synthetic T3 storage seed/readback failed: " + result.stderr.decode(errors="replace"))


class T3CodeUiTests(unittest.TestCase):
    def test_scoped_ui_preservation_and_rejection_vectors(self):
        result = subprocess.run([str(level._node()), str(Path(__file__).parent / 'fixtures/t3code_ui_vectors.cjs')],
            capture_output=True, timeout=30, env=level._environment())
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.assertTrue(json.loads(result.stdout)['passed'])


class T3CodeLocalStorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            level.runtime()
        except level.OfficeLevelDBError:
            if os.environ.get("LARJ_REQUIRE_LEVELDB_TESTS") == "1":
                raise
            raise unittest.SkipTest("Pinned LevelDB runtime not configured")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(strict=True)

    def test_native_rewrite_remote_preservation_and_cold_readback(self):
        local_storage(self.root)
        evidence = storage.freeze(self.root, "env-1", ["thread-1"])
        self.assertNotIn("T3_PRIVATE", json.dumps(evidence))
        self.assertGreater(storage.remaining(evidence), 0)
        phases = []
        with storage.prepared(evidence) as family:
            storage.install(evidence, family, phase_callback=phases.append)
        self.assertEqual(storage.remaining(evidence), 0)
        self.assertEqual(phases, ["mutation_started"])
        with storage.prepared(evidence) as family:
            self.assertIsNone(family)
        local_storage(self.root, inspect=True)

    def test_attachment_blocks_without_touching_original(self):
        local_storage(self.root, attachment=True)
        before = level._snapshot(self.root, storage.RELATIVE)
        with self.assertRaisesRegex(level.OfficeLevelDBError, "attachment_closure_unqualified"):
            storage.freeze(self.root, "env-1", ["thread-1"])
        self.assertEqual(level._snapshot(self.root, storage.RELATIVE), before)


if __name__ == '__main__':
    unittest.main()
