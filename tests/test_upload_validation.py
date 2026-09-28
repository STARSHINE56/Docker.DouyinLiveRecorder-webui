import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import upload_after_record as uploader


def probe_result(streams, duration="12.5", returncode=0):
    return SimpleNamespace(
        returncode=returncode,
        stdout=json.dumps({"format": {"duration": duration}, "streams": [{"codec_type": x} for x in streams]}),
        stderr="",
    )


class MediaValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.file = Path(self.temp.name) / "clip.mp4"
        self.file.write_bytes(b"media")

    def tearDown(self):
        self.temp.cleanup()

    def test_video_and_audio_can_upload(self):
        self.assertTrue(uploader.probe_media_file(self.file, runner=Mock(return_value=probe_result(["video", "audio"]))))

    def test_missing_audio_is_rejected(self):
        self.assertFalse(uploader.probe_media_file(self.file, runner=Mock(return_value=probe_result(["video"]))))

    def test_ffprobe_failure_is_rejected(self):
        self.assertFalse(uploader.probe_media_file(self.file, runner=Mock(return_value=probe_result([], returncode=1))))

    def test_segment_wait_requires_stable_complete_set(self):
        second = Path(self.temp.name) / "clip_001.mp4"
        second.write_bytes(b"two")
        first = Path(self.temp.name) / "clip_000.mp4"
        first.write_bytes(b"one")
        with patch.object(uploader, "get_mp4_candidates", side_effect=[[first], [first, second], [first, second]]), patch.object(
            uploader, "probe_media_file", return_value=True
        ), patch.object(uploader.time, "sleep"):
            ready = uploader.wait_for_mp4("clip_%03d.ts", 10, poll_interval=0)
        self.assertEqual(ready, [first, second])


class WebDavValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.file = Path(self.temp.name) / "clip.mp4"
        self.file.write_bytes(b"12345")

    def tearDown(self):
        self.temp.cleanup()

    def session(self, remote_size, put_status=201):
        session = Mock()
        session.put.return_value = SimpleNamespace(status_code=put_status)
        xml = f'<d:multistatus xmlns:d="DAV:"><d:response><d:propstat><d:prop><d:getcontentlength>{remote_size}</d:getcontentlength></d:prop></d:propstat></d:response></d:multistatus>'
        session.request.side_effect = [
            SimpleNamespace(status_code=404, headers={}, content=b""),
            SimpleNamespace(status_code=207, headers={}, content=xml.encode()),
        ]
        return session

    def test_remote_size_match_succeeds(self):
        self.assertTrue(uploader.upload(self.session(5), self.file, "https://dav.test/root", ("u", "p"), 1))

    def test_remote_size_mismatch_fails(self):
        self.assertFalse(uploader.upload(self.session(4), self.file, "https://dav.test/root", ("u", "p"), 1))

    def test_upload_failure_keeps_local_file(self):
        self.assertFalse(uploader.upload(self.session(5, put_status=500), self.file, "https://dav.test/root", ("u", "p"), 1))
        self.assertTrue(self.file.exists())

    def test_existing_verified_remote_skips_duplicate_put(self):
        session = self.session(5)
        session.request.side_effect = [list(session.request.side_effect)[1]]
        self.assertTrue(uploader.upload(session, self.file, 'https://dav.test/root', ('u', 'p'), 1))
        session.put.assert_not_called()

    def test_cleanup_requires_verified_upload_and_closed_source(self):
        source = self.file.with_suffix('.ts')
        source.write_bytes(b'ts')
        settings = {'自动上传录像': '是', 'WebDAV地址': 'https://dav.test',
                    '上传成功后删除本地': '是', '按主播创建文件夹': '否',
                    '上传失败重试次数': '1'}
        arguments = ['--save_file_path', str(source)]
        with patch.object(uploader, 'load_config', return_value=settings), \
             patch.object(uploader, 'wait_for_mp4', return_value=[self.file]), \
             patch.object(uploader, 'ensure_remote_path', return_value='https://dav.test/root'), \
             patch.object(uploader, 'file_is_stable', return_value=True), \
             patch.object(uploader, 'file_is_open', return_value=False), \
             patch.object(uploader, 'upload', return_value=False):
            self.assertEqual(uploader.main(arguments), 1)
            self.assertTrue(source.exists())
            self.assertTrue(self.file.exists())
        with patch.object(uploader, 'load_config', return_value=settings), \
             patch.object(uploader, 'wait_for_mp4', return_value=[self.file]), \
             patch.object(uploader, 'ensure_remote_path', return_value='https://dav.test/root'), \
             patch.object(uploader, 'file_is_stable', return_value=True), \
             patch.object(uploader, 'file_is_open', return_value=True), \
             patch.object(uploader, 'upload', return_value=True):
            self.assertEqual(uploader.main(arguments), 1)
            self.assertTrue(source.exists())
            self.assertTrue(self.file.exists())
        with patch.object(uploader, 'load_config', return_value=settings), \
             patch.object(uploader, 'wait_for_mp4', return_value=[self.file]), \
             patch.object(uploader, 'ensure_remote_path', return_value='https://dav.test/root'), \
             patch.object(uploader, 'file_is_stable', return_value=True), \
             patch.object(uploader, 'file_is_open', return_value=False), \
             patch.object(uploader, 'upload', return_value=True):
            self.assertEqual(uploader.main(arguments), 0)
            self.assertFalse(source.exists())
            self.assertFalse(self.file.exists())

    def test_backfill_is_preview_by_default_and_sequential_when_applied(self):
        first = Path(self.temp.name) / 'clip_001.ts'
        second = Path(self.temp.name) / 'clip_002.ts'
        for file in (first, second):
            file.write_bytes(b'ts')
            os.utime(file, (time.time() - 700, time.time() - 700))
        args = ['--backfill-dir', self.temp.name, '--match', 'clip_*.ts', '--record_name', '测试主播']
        with patch.object(uploader, 'load_config', return_value={'自动上传录像': '是'}), \
             patch.object(uploader, 'convert_backfill_ts') as convert:
            self.assertEqual(uploader.main(args), 0)
            convert.assert_not_called()
            self.assertEqual(uploader.main(args + ['--apply']), 1)
            convert.assert_not_called()
        with patch.object(uploader, 'load_config', return_value={'自动上传录像': '是'}), \
             patch.object(uploader, 'convert_backfill_ts', return_value=True) as convert, \
             patch.object(uploader, 'wait_for_mp4', return_value=[]):
            self.assertEqual(uploader.main(args + ['--apply', '--confirm-recorder-stopped']), 1)
            self.assertEqual(convert.call_args_list[0].args[0], first)
            self.assertEqual(convert.call_args_list[1].args[0], second)
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())

    def test_backfill_rejects_insufficient_space_before_ffmpeg(self):
        source = Path(self.temp.name) / 'large.ts'
        source.write_bytes(b'ts')
        with patch.object(uploader, 'file_is_stable', return_value=True), \
             patch.object(uploader, 'file_is_open', return_value=False), \
             patch.object(uploader.shutil, 'disk_usage', return_value=SimpleNamespace(free=1)), \
             patch.object(uploader.subprocess, 'run') as runner:
            self.assertFalse(uploader.convert_backfill_ts(source))
            runner.assert_not_called()
            self.assertTrue(source.exists())

    def test_backfill_refuses_open_source(self):
        source = Path(self.temp.name) / 'active.ts'
        source.write_bytes(b'ts')
        with source.open('rb') as handle, patch.object(uploader, 'file_is_stable', return_value=True), \
             patch.object(uploader.subprocess, 'run') as runner:
            self.assertFalse(handle.closed)
            self.assertTrue(uploader.file_is_open(source))
            self.assertFalse(uploader.convert_backfill_ts(source))
            runner.assert_not_called()
        self.assertTrue(source.exists())


if __name__ == "__main__":
    unittest.main()
