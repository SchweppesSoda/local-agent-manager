import tempfile
from pathlib import Path
import unittest

from local_agent_manager import paseo_managed_attachments as images


PNG = b"\x89PNG\r\n\x1a\n" + b"SYNTHETIC_IMAGE" + b"\x00\x00\x00\x00IEND\xaeB`\x82"
IMAGE = "att_msg_1794040100204_fixture.png"


class ManagedImageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.directory = self.root / images.RELATIVE
        self.directory.mkdir()
        (self.directory / IMAGE).write_bytes(PNG)

    def test_full_profile_disposal_and_cold_absence_preserve_external_original(self):
        original = self.root / "original.png"
        original.write_bytes(PNG)
        proof = images.freeze(self.root, ["selected"], ["selected"])
        self.assertEqual(images.remaining(proof), 1)
        images.apply(proof, phase_callback=lambda _: None)
        self.assertEqual(images.remaining(proof), 0)
        self.assertFalse(self.directory.exists())
        self.assertEqual(original.read_bytes(), PNG)

    def test_partial_profile_cannot_authorize_unreferenced_images(self):
        with self.assertRaisesRegex(ValueError, "partial_scope"):
            images.freeze(self.root, ["selected"], ["selected", "keep"])
        self.assertEqual((self.directory / IMAGE).read_bytes(), PNG)

    def test_unknown_file_or_changed_preview_length_blocks(self):
        (self.directory / "unknown.png").write_bytes(PNG)
        with self.assertRaisesRegex(ValueError, "format_unqualified"):
            images.freeze(self.root, ["selected"], ["selected"])
        (self.directory / "unknown.png").unlink()
        (self.directory / "preview_999_fixture.jpg").write_bytes(b"\xff\xd8SYNTHETIC\xff\xd9")
        with self.assertRaisesRegex(ValueError, "size_unverified"):
            images.freeze(self.root, ["selected"], ["selected"])

    def test_changed_namespace_never_deletes_new_files(self):
        proof = images.freeze(self.root, ["selected"], ["selected"])
        new = self.directory / "att_msg_1794040100205_new.png"
        new.write_bytes(PNG)
        with self.assertRaisesRegex(ValueError, "state_unknown"):
            images.apply(proof, phase_callback=lambda _: self.fail("changed namespace reached writer"))
        self.assertEqual(new.read_bytes(), PNG)

    def test_new_directory_invalidates_absent_snapshot(self):
        (self.directory / IMAGE).unlink()
        self.directory.rmdir()
        proof = images.freeze(self.root, ["selected"], ["selected"])
        self.directory.mkdir()
        with self.assertRaisesRegex(ValueError, "closure_changed"):
            images.remaining(proof)
