import unittest

from streamget.stream import select_douyin_record_url


class DouyinAudioSourceTests(unittest.TestCase):
    def setUp(self):
        self.m3u8_url = "https://example.test/live.m3u8"
        self.flv_url = "https://example.test/live.flv"

    def test_m3u8_without_audio_and_flv_with_audio_selects_flv(self):
        audio = {self.m3u8_url: False, self.flv_url: True}
        selected = select_douyin_record_url(
            self.m3u8_url, self.flv_url, audio_probe=audio.get
        )
        self.assertEqual(selected, self.flv_url)

    def test_m3u8_with_audio_is_selected(self):
        audio = {self.m3u8_url: True, self.flv_url: True}
        selected = select_douyin_record_url(
            self.m3u8_url, self.flv_url, audio_probe=audio.get
        )
        self.assertEqual(selected, self.m3u8_url)

    def test_no_audio_warns_and_keeps_main_loop_fallback(self):
        warnings = []
        selected = select_douyin_record_url(
            self.m3u8_url,
            self.flv_url,
            audio_probe=lambda _url: False,
            warning=warnings.append,
        )
        self.assertEqual(selected, self.m3u8_url)
        self.assertEqual(len(warnings), 1)
        self.assertIn("均未检测到音轨", warnings[0])


if __name__ == "__main__":
    unittest.main()
