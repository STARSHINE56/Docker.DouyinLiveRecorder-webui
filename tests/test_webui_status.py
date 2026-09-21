import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import status_runtime
import webui


class RuntimeStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        status_runtime.reset_for_tests(Path(self.temp.name) / "runtime_state.json")
        self.url = "https://live.douyin.com/123"
        self.name = "测试主播"

    def tearDown(self):
        self.temp.cleanup()

    def test_global_monitor_count_is_not_live_evidence(self):
        state = webui.classify_status("共监测2个直播中")
        self.assertFalse(state and state[0] == "live")

    def test_negative_recording_is_not_recording(self):
        self.assertEqual(webui.classify_status("没有正在录制的直播")[0], "waiting")

    def test_offline_log_is_waiting(self):
        self.assertEqual(webui.classify_status("序号1 测试主播 未开播")[0], "waiting")

    def test_manual_name_in_arbitrary_log_is_not_live_evidence(self):
        item = {"name": self.name, "url": self.url}
        self.assertFalse(webui.status_applies_to_monitor(f"普通调试输出 {self.name}", item, "live"))

    def test_live_started_event_is_deduplicated_per_session(self):
        status_runtime.update_live_status(self.url, self.name, True, stream_valid=True)
        status_runtime.update_live_status(self.url, self.name, True, stream_valid=True)
        events = status_runtime.load_state()["events"]
        self.assertEqual([e["type"] for e in events].count("LIVE_STARTED"), 1)

    def test_duplicate_detection_task_is_rejected(self):
        self.assertTrue(status_runtime.claim_detection_task(self.url))
        self.assertFalse(status_runtime.claim_detection_task(self.url))
        status_runtime.release_detection_task(self.url)
        self.assertTrue(status_runtime.claim_detection_task(self.url))

    def test_duplicate_recording_task_is_rejected(self):
        self.assertTrue(status_runtime.claim_recording_task(self.url))
        self.assertFalse(status_runtime.claim_recording_task(self.url))
        status_runtime.release_recording_task(self.url)
        self.assertTrue(status_runtime.claim_recording_task(self.url))

    def test_offline_requires_two_confirmations_after_live(self):
        status_runtime.update_live_status(self.url, self.name, True, stream_valid=True)
        first = status_runtime.update_live_status(self.url, self.name, False)
        self.assertEqual(first, "suspected_offline")
        self.assertNotIn("LIVE_ENDED", [e["type"] for e in status_runtime.load_state()["events"]])
        second = status_runtime.update_live_status(self.url, self.name, False)
        self.assertEqual(second, "offline")
        self.assertEqual([e["type"] for e in status_runtime.load_state()["events"]].count("LIVE_ENDED"), 1)

    def test_ffmpeg_failure_while_live_enters_recovery(self):
        status_runtime.update_live_status(self.url, self.name, True, stream_valid=True)
        status_runtime.mark_recording_started(self.url, self.name, 1234, "/downloads/a.mp4")
        result = status_runtime.mark_recording_finished(self.url, self.name, 1)
        self.assertEqual(result, "recovering")
        state = status_runtime.load_state()
        self.assertEqual(state["monitors"][self.url]["recording_status"], "recovering")
        self.assertNotIn("LIVE_ENDED", [e["type"] for e in state["events"]])

        status_runtime.update_live_status(self.url, self.name, True, stream_valid=True)
        status_runtime.mark_recording_started(self.url, self.name, 5678, "/downloads/b.mp4")
        self.assertIn("RECORDING_RECOVERED", [e["type"] for e in status_runtime.load_state()["events"]])

    def test_restart_reconciles_missing_ffmpeg_process(self):
        status_runtime.update_live_status(self.url, self.name, True, stream_valid=True)
        status_runtime.mark_recording_started(self.url, self.name, 999999, "/downloads/a.mp4")
        repaired = status_runtime.reconcile_stale_recordings(lambda _pid: False)
        item = status_runtime.load_state()["monitors"][self.url]
        self.assertEqual(repaired, 1)
        self.assertEqual(item["recording_status"], "interrupted")
        self.assertIsNone(item["recording_pid"])


class WebUiSmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        status_runtime.reset_for_tests(Path(self.temp.name) / "runtime_state.json")
        webui.app.config.update(TESTING=True)
        self.client = webui.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def test_jinja_template_and_mobile_guards(self):
        template = webui.app.jinja_env.get_template("index.html")
        self.assertIsNotNone(template)
        source = Path("templates/index.html").read_text(encoding="utf-8")
        self.assertIn("@media(max-width:360px)", source)
        self.assertIn("overflow-x:hidden", source)
        self.assertNotIn("font-family", source)

    def test_home_smoke(self):
        response = self.client.get("/home")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("最近事件", html)
        self.assertIn("日志 / 调试", html)
        self.assertIn("查看详情", html)

    def test_snapshot_uses_runtime_state_not_logs(self):
        url = "https://live.douyin.com/123"
        status_runtime.update_live_status(url, "测试主播", True, stream_valid=True)
        item = {
            "name": "测试主播", "url": url, "platform": "抖音",
            "monitor_status": "waiting", "live_status": "unknown", "recording_status": "idle",
            "last_checked_at": None, "last_success_at": None, "live_started_at": None,
            "recording_started_at": None, "recording_file": None, "last_error": None,
        }
        misleading = ["共监测2个直播中", "没有正在录制的直播"]
        with patch.object(webui, "parse_monitor_lines", return_value=[item]), patch.object(
            webui, "read_log_lines", return_value=misleading
        ):
            snapshot = webui.get_monitor_snapshot()
        self.assertEqual(snapshot["monitors"][0]["live_status"], "live")
        self.assertEqual(snapshot["counts"]["live"], 1)
        self.assertEqual(snapshot["counts"]["recording"], 0)

    def test_api_status_and_logs(self):
        with patch.object(webui, "parse_monitor_lines", return_value=[]):
            response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["counts"]["total"], 0)
        with patch.object(webui, "read_log_lines", return_value=["debug"]):
            logs = self.client.get("/api/logs")
        self.assertEqual(logs.get_json()["lines"], ["debug"])


if __name__ == "__main__":
    unittest.main()
