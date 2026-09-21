import unittest
from unittest.mock import patch

import webui


class StatusClassificationTests(unittest.TestCase):
    def test_negative_recording_is_waiting(self):
        self.assertEqual(webui.classify_status("没有正在录制的直播")[0], "waiting")

    def test_monitor_count_is_not_live(self):
        state = webui.classify_status("共监测2个直播中")
        self.assertFalse(state and state[0] == "live")

    def test_waiting_is_waiting(self):
        self.assertEqual(webui.classify_status("序号1 测试主播 等待直播")[0], "waiting")

    def test_explicit_recording_is_recording(self):
        line = "2026-09-21 08:00:00 主播：测试主播 开始录制"
        self.assertEqual(webui.classify_status(line)[0], "recording")

    def test_explicit_anchor_live_is_live(self):
        line = "2026-09-21 08:00:00 主播：测试主播 已开播"
        self.assertEqual(webui.classify_status(line)[0], "live")

    def test_preparing_to_record_is_not_recording(self):
        state = webui.classify_status("测试主播 准备开始录制视频: /downloads")
        self.assertFalse(state and state[0] == "recording")

    def test_manual_name_in_unstructured_text_is_not_evidence(self):
        item = {"name": "直播", "url": "https://example.com/room"}
        self.assertFalse(
            webui.status_applies_to_monitor("共监测2个直播中", item, "live")
        )

    def test_repeated_polling_events_are_deduplicated(self):
        lines = [
            "2026-09-21 08:00:00 序号1 测试主播 等待直播",
            "2026-09-21 08:00:05 序号1 测试主播 等待直播",
            "2026-09-21 08:00:10 共监测2个直播中",
            "2026-09-21 08:00:15 没有正在录制的直播",
        ]
        with patch.object(webui, "parse_monitor_lines", return_value=[]), patch.object(
            webui, "read_log_lines", return_value=lines
        ):
            events = webui.get_monitor_snapshot()["events"]
        self.assertEqual(len(events), 1)
        self.assertIn("等待直播", events[0])

    def test_false_global_lines_keep_monitor_waiting(self):
        item = {
            "name": "测试主播",
            "url": "https://example.com/room",
            "platform": "直播",
            "status": "waiting",
            "status_text": "等待直播",
            "updated_at": "--",
        }
        lines = [
            "2026-09-21 08:00:00 共监测2个直播中",
            "2026-09-21 08:00:05 没有正在录制的直播",
        ]
        with patch.object(webui, "parse_monitor_lines", return_value=[item]), patch.object(
            webui, "read_log_lines", return_value=lines
        ):
            snapshot = webui.get_monitor_snapshot()
        self.assertEqual(snapshot["monitors"][0]["status"], "waiting")
        self.assertEqual(snapshot["counts"]["live"], 0)
        self.assertEqual(snapshot["counts"]["recording"], 0)

    def test_explicit_anchor_logs_drive_real_transitions(self):
        item = {
            "name": "测试主播",
            "url": "https://example.com/room",
            "platform": "直播",
            "status": "waiting",
            "status_text": "等待直播",
            "updated_at": "--",
        }
        lines = [
            "2026-09-21 08:00:00 序号1 测试主播 等待直播",
            "2026-09-21 08:01:00 序号1 测试主播 正在直播中",
            "2026-09-21 08:02:00 序号1 测试主播[原画] 正在录制中 0:01:00",
            "2026-09-21 08:02:05 序号1 测试主播[原画] 正在录制中 0:01:05",
        ]
        with patch.object(webui, "parse_monitor_lines", return_value=[item]), patch.object(
            webui, "read_log_lines", return_value=lines
        ):
            snapshot = webui.get_monitor_snapshot()
        self.assertEqual(snapshot["monitors"][0]["status"], "recording")
        self.assertEqual(snapshot["counts"]["recording"], 1)
        self.assertEqual(len(snapshot["events"]), 3)


class WebUiSmokeTests(unittest.TestCase):
    def setUp(self):
        webui.app.config.update(TESTING=True)
        self.client = webui.app.test_client()

    def test_jinja_template_parses(self):
        webui.app.jinja_env.get_template("index.html")

    def test_home_smoke(self):
        response = self.client.get("/home")
        self.assertEqual(response.status_code, 200)
        self.assertIn("最近事件", response.get_data(as_text=True))

    def test_api_status(self):
        with patch.object(webui, "parse_monitor_lines", return_value=[]), patch.object(
            webui, "read_log_lines", return_value=[]
        ):
            response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["counts"]["total"], 0)
        self.assertEqual(payload["events"], [])


if __name__ == "__main__":
    unittest.main()
