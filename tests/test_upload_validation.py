import json
import tempfile
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
        session.request.return_value = SimpleNamespace(status_code=207, headers={}, content=xml.encode())
        return session

    def test_remote_size_match_succeeds(self):
        self.assertTrue(uploader.upload(self.session(5), self.file, "https://dav.test/root", ("u", "p"), 1))

    def test_remote_size_mismatch_fails(self):
        self.assertFalse(uploader.upload(self.session(4), self.file, "https://dav.test/root", ("u", "p"), 1))

    def test_upload_failure_keeps_local_file(self):
        self.assertFalse(uploader.upload(self.session(5, put_status=500), self.file, "https://dav.test/root", ("u", "p"), 1))
        self.assertTrue(self.file.exists())


if __name__ == "__main__":
    unittest.main()
