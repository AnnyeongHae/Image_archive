import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

FILE = Path(__file__).resolve().parents[1] / 'platform/v2/local/intake.py'
spec = importlib.util.spec_from_file_location('v2_intake_under_test', FILE)
intake = importlib.util.module_from_spec(spec)
spec.loader.exec_module(intake)


def row(identifier, prompt='same', file=None, pixel=None, order=0):
    return dict(item_id=identifier, original_prompt=prompt, prompt_sha256=intake.digest(prompt.encode()),
                prompt_nonblank=bool(prompt.strip()), file_sha256=file, pixel_sha256=pixel,
                pixel_policy='rgba-exif-v2', ingested_order=order)


class V2IntakeTests(unittest.TestCase):
    @staticmethod
    def image_bytes(color):
        from PIL import Image
        buffer = io.BytesIO()
        Image.new('RGB', (2, 2), color).save(buffer, format='BMP')
        return buffer.getvalue()

    @staticmethod
    def image_path(raw, final_raw=None):
        path = Mock(spec=Path)
        path.is_file.return_value = True
        path.is_symlink.return_value = False
        path.stat.return_value = SimpleNamespace(st_dev=1, st_ino=2, st_size=len(raw),
                                                st_mtime_ns=3, st_ctime_ns=4)
        path.open.side_effect = [io.BytesIO(raw), io.BytesIO(raw if final_raw is None else final_raw)]
        return path

    def test_image_file_and_pixel_hash_use_the_same_bounded_buffer(self):
        from PIL import Image
        raw = self.image_bytes('red')
        path = self.image_path(raw)
        original_open = Image.open
        decoded_inputs = []
        def checked_open(value, *args, **kwargs):
            self.assertIsInstance(value, io.BytesIO)
            decoded_inputs.append(value.getvalue())
            return original_open(value, *args, **kwargs)
        with patch.object(Image, 'open', side_effect=checked_open):
            result = intake.image_hashes(path)
        self.assertEqual(decoded_inputs, [raw])
        self.assertEqual(result['file_sha256'], intake.digest(raw))
        expected_pixels = Image.new('RGBA', (2, 2), 'red').tobytes()
        expected_hash = intake.digest(b'rgba-exif-v2\0' + intake.struct.pack('>II', 2, 2) + expected_pixels)
        self.assertEqual(result['pixel_sha256'], expected_hash)
        self.assertEqual(path.open.call_count, 2)

    def test_same_size_unchanged_stat_byte_replacement_is_rejected_by_final_hash(self):
        first, replacement = self.image_bytes('red'), self.image_bytes('blue')
        self.assertEqual(len(first), len(replacement))
        path = self.image_path(first, replacement)
        with self.assertRaisesRegex(ValueError, 'media_changed_during_hash'):
            intake.image_hashes(path)

    def test_image_identity_change_is_rejected_even_with_same_size_and_mtime(self):
        raw = self.image_bytes('red')
        path = self.image_path(raw)
        before = path.stat.return_value
        after = SimpleNamespace(**{**vars(before), 'st_ino': 999})
        path.stat.side_effect = [before, before, after]
        with self.assertRaisesRegex(ValueError, 'media_changed_during_hash'):
            intake.image_hashes(path)

    def test_oversized_image_file_is_rejected_before_content_read(self):
        path = self.image_path(b'not-opened')
        path.stat.return_value.st_size = 15 * 1024**2 + 1
        with self.assertRaisesRegex(ValueError, 'invalid_local_media'):
            intake.image_hashes(path)
        path.open.assert_not_called()

    def test_exact_file_even_when_prompt_differs(self):
        result=intake.dedupe_plan([row('a','plain',file='f'),row('b','{"style":"json"}',file='f',order=1)])
        self.assertEqual(result['active_ids'],['b'])
        self.assertEqual(result['physical_deletions'],0)

    def test_pixel_only_does_not_exclude(self):
        result=intake.dedupe_plan([row('a','first',pixel='p'),row('b','second',pixel='p')])
        self.assertEqual(result['aliases'],[])

    def test_pixel_and_nonblank_exact_prompt_excludes(self):
        result=intake.dedupe_plan([row('a',pixel='p'),row('b',pixel='p',order=1)])
        self.assertEqual(result['active_ids'],['a'])

    def test_blank_prompt_not_identity(self):
        self.assertIsNone(intake.exact_reason(row('a','',pixel='p'),row('b','',pixel='p')))

    def test_prompt_exact_only_groups_candidates(self):
        result=intake.dedupe_plan([row('BST-001',file='a'),row('BST-002',file='b')])
        self.assertEqual(result['aliases'],[])
        self.assertEqual(result['prompt_group_candidates'],[['BST-001','BST-002']])
        self.assertFalse(result['human_approved'])

    def test_pixel_policy_version_cannot_mix(self):
        a,b=row('a',pixel='p'),row('b',pixel='p');b['pixel_policy']='thumbnail'
        self.assertIsNone(intake.exact_reason(a,b))

    def test_duplicate_ids_rejected(self):
        with self.assertRaises(ValueError): intake.dedupe_plan([row('a'),row('a')])


if __name__ == '__main__': unittest.main()
