import argparse
import contextlib
import io
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import workflow


class FinalizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.output = self.root / 'output'
        self.role = self.output / '测试角色'
        self.role.mkdir(parents=True)
        t = np.arange(16000, dtype='float32') / 16000
        self.audio = self.role / '测试角色_去配乐.wav'
        sf.write(self.audio, .1 * np.sin(2 * np.pi * 300 * t), 16000)
        workflow.dump(self.role / '剪辑时间表.json', [{'source': 'fetched', 'source_url': 'https://example.org/video', 'source_start': 0, 'source_end': 1}])
        workflow.dump(self.role / '处理记录.json', {'files': [{'path': str(self.audio), 'sha256': workflow.sha256(self.audio)}]})
        self.cache = self.output / '.cache' / 'render-example'
        self.cache.mkdir(parents=True)
        (self.cache / 'clip.wav').write_bytes(b'intermediate')
        frames = self.output / '.cache' / 'frames'
        frames.mkdir()
        (frames / 'frame.jpg').write_bytes(b'image')
        fetched_dir = self.root / 'sources' / 'fetched'
        fetched_dir.mkdir(parents=True)
        self.fetched = fetched_dir / 'source.mp4'
        self.fetched.write_bytes(b'downloaded source')
        workflow.dump(fetched_dir / 'source.json', {'path': str(self.fetched), 'requested_url': 'https://example.org/video'})
        self.user_source = self.root / 'user_source.mp4'
        self.user_source.write_bytes(b'user original')
        self.job = self.root / 'job.json'
        workflow.dump(self.job, {'schema_version': 1, 'output_dir': 'output',
                                 'sources': [{'id': 'fetched', 'path': str(self.fetched), 'url': 'https://example.org/video'},
                                             {'id': 'user', 'path': str(self.user_source)}],
                                 'characters': [{'name': '测试角色', 'clips': [{'source': 'fetched', 'start': 0, 'end': 1}]}]})

    def tearDown(self):
        self.temp.cleanup()

    def run_cleanup(self, apply=False, published_root=None):
        args = argparse.Namespace(job=str(self.job), apply=apply,
                                  published_root=str(published_root) if published_root else None)
        with contextlib.redirect_stdout(io.StringIO()):
            workflow.cleanup(args)

    def test_cleanup_only_after_validation(self):
        self.run_cleanup()
        self.assertTrue(self.fetched.exists())
        self.assertTrue(self.cache.exists())
        self.run_cleanup(apply=True)
        self.assertFalse(self.fetched.exists())
        self.assertFalse(self.cache.exists())
        self.assertTrue((self.fetched.parent / 'source.json').exists())
        self.assertTrue(self.user_source.exists())
        self.assertTrue(self.audio.exists())
        self.assertTrue((self.output / '缓存清理记录.json').exists())

    def test_duplicate_output_requires_byte_identical_copy(self):
        published = self.root / 'published'
        shutil.copytree(self.role, published / self.role.name)
        self.run_cleanup(apply=True, published_root=published)
        self.assertFalse(self.role.exists())
        marker = workflow.load(published / self.role.name / '处理记录.json')
        self.assertEqual(marker['files'][0]['path'], str((published / self.role.name / self.audio.name).resolve()))

    def test_changed_final_audio_blocks_cleanup(self):
        self.audio.write_bytes(b'changed')
        with self.assertRaises(ValueError):
            self.run_cleanup(apply=True)
        self.assertTrue(self.fetched.exists())
        self.assertTrue(self.cache.exists())

    def test_quality_check_does_not_certify_tone_as_speech(self):
        result = workflow.quality_metrics(self.audio, self.audio, self.audio, min_speech=30)
        self.assertFalse(result['checks']['speech_target'])
        self.assertEqual(result['status'], 'review_required')
        self.assertFalse(result['music_free_verified'])


@unittest.skipUnless(os.environ.get('RUN_FILM_VOICE_REAL_SAMPLE') == '1', 'requires local real sample and model')
class RealSampleTests(unittest.TestCase):
    def test_alternate_model_render_and_cleanup(self):
        source = Path('/Users/lianyou/Desktop/角色音频/红楼梦/林黛玉/林黛玉.mp4')
        if not source.is_file():
            self.skipTest('local real sample unavailable')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job = root / 'job.json'
            row = {'source': 'provided', 'start': 22.78, 'end': 26.1, 'verified': True,
                   'review_note': '此前已核对的林黛玉原片对白'}
            workflow.dump(job, {'schema_version': 1, 'title': '短片验证', 'version': '1987版',
                                'output_dir': 'output', 'target_seconds': [2, 180],
                                'sources': [{'id': 'provided', 'path': str(source)}],
                                'characters': [{'name': '林黛玉', 'references': [row], 'clips': [row]}]})
            args = SimpleNamespace(job=str(job), role=None, no_separate=False,
                                   separator_model='Kim_Vocal_2.onnx', runtime='/Users/lianyou/.codex/tools/film-voice')
            with contextlib.redirect_stdout(io.StringIO()):
                workflow.render(args)
            role_dir = root / 'output/林黛玉'
            record = workflow.load(role_dir / '处理记录.json')
            self.assertEqual(record['separation_model'], 'Kim_Vocal_2.onnx')
            self.assertIn('clone_qc', record)
            self.assertIn('speaker_audit', record)
            with contextlib.redirect_stdout(io.StringIO()):
                workflow.cleanup(SimpleNamespace(job=str(job), published_root=None, apply=True))
            self.assertTrue((role_dir / '林黛玉_去配乐.mp3').is_file())
            self.assertFalse((root / 'output/.cache').exists())
            self.assertTrue(source.is_file())


if __name__ == '__main__':
    unittest.main()
