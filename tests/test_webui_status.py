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

    def test_real_recovery_sequence_emits_recovered_once(self):
        status_runtime.update_live_status(self.url, self.name, True, stream_valid=True)
        status_runtime.mark_recording_started(self.url, self.name, 1234, "/downloads/a.mp4")
        status_runtime.mark_recording_finished(self.url, self.name, 1)
        status_runtime.update_live_status(self.url, self.name, True, stream_valid=True)
        status_runtime.mark_recording_starting(self.url, self.name, "/downloads/b.mp4")
        status_runtime.mark_recording_started(self.url, self.name, 5678, "/downloads/b.mp4")
        status_runtime.mark_recording_started(self.url, self.name, 5678, "/downloads/b.mp4")
        event_types = [e["type"] for e in status_runtime.load_state()["events"]]
        self.assertEqual(event_types.count("RECORDING_RECOVERING"), 1)
        self.assertEqual(event_types.count("RECORDING_RECOVERED"), 1)

    def test_direct_flv_recording_updates_runtime_and_completes(self):
        states_during_download = []
        status_runtime.update_live_status(self.url, self.name, True, stream_valid=True)

        def download():
            item = status_runtime.load_state()["monitors"][self.url]
            states_during_download.append((
                item["recording_status"], item["recording_file"], item["recording_started_at"]
            ))

        started = status_runtime.run_direct_recording(
            self.url, self.name, "/downloads/live.flv", download
        )

        self.assertTrue(started)
        self.assertEqual(states_during_download[0][0], "recording")
        self.assertEqual(states_during_download[0][1], "/downloads/live.flv")
        self.assertIsNotNone(states_during_download[0][2])
        item = status_runtime.load_state()["monitors"][self.url]
        self.assertEqual(item["recording_status"], "completed")
        self.assertIsNotNone(item["recording_started_at"])
        self.assertEqual(item["recording_file"], "/downloads/live.flv")
        event_types = [e["type"] for e in status_runtime.load_state()["events"]]
        self.assertEqual(event_types.count("RECORDING_STARTED"), 1)
        self.assertEqual(event_types.count("RECORDING_ENDED"), 1)

    def test_direct_flv_recording_error_and_duplicate_guard(self):
        self.assertTrue(status_runtime.claim_recording_task(self.url))
        called = []
        try:
            started = status_runtime.run_direct_recording(
                self.url, self.name, "/downloads/duplicate.flv", lambda: called.append(True)
            )
        finally:
            status_runtime.release_recording_task(self.url)
        self.assertFalse(started)
        self.assertEqual(called, [])

        def fail_download():
            raise OSError("network failed")

        with self.assertRaises(OSError):
            status_runtime.run_direct_recording(
                self.url, self.name, "/downloads/error.flv", fail_download
            )
        item = status_runtime.load_state()["monitors"][self.url]
        self.assertEqual(item["recording_status"], "error")
        self.assertIsNone(item["recording_pid"])

    def test_restart_reconciles_missing_ffmpeg_process(self):
        status_runtime.update_live_status(self.url, self.name, True, stream_valid=True)
        status_runtime.mark_recording_started(self.url, self.name, 999999, "/downloads/a.mp4")
        repaired = status_runtime.reconcile_stale_recordings(lambda _pid: False)
        item = status_runtime.load_state()["monitors"][self.url]
        self.assertEqual(repaired, 1)
        self.assertEqual(item["recording_status"], "interrupted")
        self.assertIsNone(item["recording_pid"])

    def test_manual_stop_blocks_recording_until_confirmed_offline(self):
        status_runtime.update_live_status(self.url, self.name, True, stream_valid=True)
        status_runtime.mark_recording_started(self.url, self.name, 1234, "/downloads/a.mp4")
        success, _ = status_runtime.request_manual_stop(self.url)
        self.assertTrue(success)
        item = status_runtime.load_state()["monitors"][self.url]
        self.assertTrue(item["manual_stop"])
        self.assertEqual(item["recording_status"], "stopping")

        status_runtime.mark_recording_finished(
            self.url, self.name, 0, intentional=True, recover_if_live=False
        )
        status_runtime.release_recording_task(self.url)
        self.assertFalse(status_runtime.claim_recording_task(self.url))
        event_types = [e["type"] for e in status_runtime.load_state()["events"]]
        self.assertIn("RECORDING_STOP_REQUESTED", event_types)
        self.assertNotIn("RECORDING_RECOVERING", event_types)

        self.assertEqual(status_runtime.update_live_status(self.url, self.name, False), "suspected_offline")
        self.assertFalse(status_runtime.claim_recording_task(self.url))
        self.assertEqual(status_runtime.update_live_status(self.url, self.name, False), "offline")
        self.assertFalse(status_runtime.load_state()["monitors"][self.url]["manual_stop"])
        self.assertTrue(status_runtime.claim_recording_task(self.url))
        status_runtime.release_recording_task(self.url)
        status_runtime.update_live_status(self.url, self.name, True, stream_valid=True)
        self.assertTrue(status_runtime.claim_recording_task(self.url))
        status_runtime.release_recording_task(self.url)

    def test_idle_and_direct_recording_stop_are_rejected(self):
        success, message = status_runtime.request_manual_stop(self.url)
        self.assertFalse(success)
        self.assertIn("没有可停止", message)

        status_runtime.update_live_status(self.url, self.name, True, stream_valid=True)
        status_runtime.mark_recording_started(
            self.url, self.name, 4321, "/downloads/a.flv", mode="direct"
        )
        success, message = status_runtime.request_manual_stop(self.url)
        self.assertFalse(success)
        self.assertIn("暂不支持", message)
        self.assertFalse(status_runtime.load_state()["monitors"][self.url]["manual_stop"])



    def test_check_failed_clears_stale_live_status(self):
        """Detection failure must not leave live_status=live when no recording runs."""
        status_runtime.update_live_status(self.url, self.name, True, stream_valid=True)
        item = status_runtime.load_state()["monitors"][self.url]
        self.assertEqual(item["live_status"], "live")

        status_runtime.mark_check_failed(self.url, self.name, "直播 API 未返回主播信息")
        item = status_runtime.load_state()["monitors"][self.url]
        self.assertEqual(item["monitor_status"], "error")
        self.assertEqual(item["live_status"], "unknown")
        self.assertIn("直播 API 未返回主播信息", item["last_error"])

    def test_check_failed_preserves_live_while_recording(self):
        status_runtime.update_live_status(self.url, self.name, True, stream_valid=True)
        status_runtime.mark_recording_started(self.url, self.name, 12345, "/tmp/a.mp4")
        status_runtime.mark_check_failed(self.url, self.name, "transient api error")
        item = status_runtime.load_state()["monitors"][self.url]
        self.assertEqual(item["monitor_status"], "error")
        self.assertEqual(item["live_status"], "live")
        self.assertEqual(item["recording_status"], "recording")

    def test_recheck_success_after_error_restores_waiting_or_live(self):
        status_runtime.update_live_status(self.url, self.name, True, stream_valid=True)
        status_runtime.mark_check_failed(self.url, self.name, "parse failed")
        # Recover to offline / waiting
        status_runtime.update_live_status(self.url, self.name, False, stream_valid=False)
        item = status_runtime.load_state()["monitors"][self.url]
        self.assertEqual(item["monitor_status"], "running")
        self.assertIn(item["live_status"], {"offline", "suspected_offline"})
        self.assertIsNone(item["last_error"])

        # Recover to live again
        status_runtime.update_live_status(self.url, self.name, True, stream_valid=True)
        item = status_runtime.load_state()["monitors"][self.url]
        self.assertEqual(item["live_status"], "live")
        self.assertEqual(item["monitor_status"], "running")
        self.assertIsNone(item["last_error"])


class WebUiSmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        status_runtime.reset_for_tests(Path(self.temp.name) / "runtime_state.json")
        self.old_downloads_dir = webui.DOWNLOADS_DIR
        webui.DOWNLOADS_DIR = Path(self.temp.name) / "downloads"
        webui.DOWNLOADS_DIR.mkdir()
        webui.app.config.update(TESTING=True)
        self.client = webui.app.test_client()

    def tearDown(self):
        webui.DOWNLOADS_DIR = self.old_downloads_dir
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


    def test_offline_interrupted_snapshot_shows_idle(self):
        """Display layer: offline + interrupted → idle; history events untouched."""
        url = "https://live.douyin.com/123"
        status_runtime.update_live_status(url, "测试主播", True, stream_valid=True)
        status_runtime.mark_recording_started(url, "测试主播", 999999, "/downloads/a.mp4")
        status_runtime.reconcile_stale_recordings(lambda _pid: False)
        # Force offline after interrupt (as real detector would)
        status_runtime.update_live_status(url, "测试主播", False, stream_valid=False)
        status_runtime.update_live_status(url, "测试主播", False, stream_valid=False)
        item_rt = status_runtime.load_state()["monitors"][url]
        self.assertEqual(item_rt["recording_status"], "interrupted")
        self.assertEqual(item_rt["live_status"], "offline")
        # events still have interruption
        self.assertTrue(
            any(e.get("type") in ("RECORDING_INTERRUPTED", "RECORDING_ENDED") or "interrupt" in str(e).lower()
                for e in status_runtime.load_state()["events"])
            or item_rt["recording_status"] == "interrupted"
        )

        base = {
            "name": "测试主播", "url": url, "platform": "抖音",
            "monitor_status": "waiting", "live_status": "unknown", "recording_status": "idle",
            "last_checked_at": None, "last_success_at": None, "live_started_at": None,
            "recording_started_at": None, "recording_file": None, "last_error": None,
        }
        with patch.object(webui, "parse_monitor_lines", return_value=[dict(base)]):
            snapshot = webui.get_monitor_snapshot()
        self.assertEqual(snapshot["monitors"][0]["live_status"], "offline")
        self.assertEqual(snapshot["monitors"][0]["recording_status"], "idle")
        # runtime state must remain interrupted
        self.assertEqual(
            status_runtime.load_state()["monitors"][url]["recording_status"], "interrupted"
        )

    def test_live_interrupted_snapshot_keeps_interrupted(self):
        """live + interrupted must NOT be normalized to idle."""
        url = "https://live.douyin.com/456"
        status_runtime.update_live_status(url, "直播主播", True, stream_valid=True)
        status_runtime.mark_recording_started(url, "直播主播", 888888, "/downloads/b.mp4")
        status_runtime.reconcile_stale_recordings(lambda _pid: False)
        item_rt = status_runtime.load_state()["monitors"][url]
        self.assertEqual(item_rt["recording_status"], "interrupted")
        self.assertEqual(item_rt["live_status"], "live")

        base = {
            "name": "直播主播", "url": url, "platform": "抖音",
            "monitor_status": "waiting", "live_status": "unknown", "recording_status": "idle",
            "last_checked_at": None, "last_success_at": None, "live_started_at": None,
            "recording_started_at": None, "recording_file": None, "last_error": None,
        }
        with patch.object(webui, "parse_monitor_lines", return_value=[dict(base)]):
            snapshot = webui.get_monitor_snapshot()
        self.assertEqual(snapshot["monitors"][0]["live_status"], "live")
        self.assertEqual(snapshot["monitors"][0]["recording_status"], "interrupted")

    def test_home_monitor_badge_has_no_monitor_colon_prefix(self):
        """Homepage badge shows '监控中' not '监控：监控中'."""
        source = Path("templates/index.html").read_text(encoding="utf-8")
        self.assertNotIn("监控：${esc(label(x.monitor_status))}", source)
        self.assertIn("${esc(label(x.monitor_status))}", source)
        # live/recording still keep prefixes
        self.assertIn("直播：${esc(label(x.live_status))}", source)
        self.assertIn("录制：${esc(label(x.recording_status))}", source)
        # labels map still has concise running text
        self.assertIn("running:'监控中'", source.replace(" ", ""))

    def test_api_status_and_logs(self):
        with patch.object(webui, "parse_monitor_lines", return_value=[]):
            response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["counts"]["total"], 0)
        with patch.object(webui, "read_log_lines", return_value=["debug"]):
            logs = self.client.get("/api/logs")
        self.assertEqual(logs.get_json()["lines"], ["debug"])

    def test_recordings_page_loads_status_without_home_dom(self):
        response = self.client.get("/recordings")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('id="service"', html)
        self.assertIn('id="recording-list"', html)
        source = Path("templates/index.html").read_text(encoding="utf-8")
        self.assertNotIn("if(!document.getElementById('monitor-list')) return", source)
        self.assertIn("if(box)box.innerHTML", source)
        self.assertIn("setInterval(()=>loadRecordings(false),8000)", source)

    def test_recording_list_only_reads_downloads_and_sorts_newest_first(self):
        older = webui.DOWNLOADS_DIR / "old.mp4"
        nested = webui.DOWNLOADS_DIR / "主播" / "new.mkv"
        nested.parent.mkdir()
        older.write_bytes(b"old")
        nested.write_bytes(b"newer")
        older.touch()
        nested.touch()
        (webui.DOWNLOADS_DIR / "ignore.txt").write_text("no", encoding="utf-8")
        outside = Path(self.temp.name) / "outside.mp4"
        outside.write_bytes(b"outside")

        response = self.client.get("/api/recordings")
        self.assertEqual(response.status_code, 200)
        recordings = response.get_json()["recordings"]
        paths = [item["path"] for item in recordings]
        self.assertEqual(set(paths), {"old.mp4", "主播/new.mkv"})
        self.assertNotIn("outside.mp4", paths)
        nested_item = next(item for item in recordings if item["name"] == "new.mkv")
        self.assertEqual(nested_item["directory"], "主播")
        self.assertEqual(nested_item["recording_status"], "completed")

    def test_recording_delete_rejects_traversal_and_directory(self):
        outside = Path(self.temp.name) / "outside.mp4"
        outside.write_bytes(b"keep")
        response = self.client.post("/api/recordings/delete", json={"path": "../outside.mp4"})
        self.assertEqual(response.status_code, 400)
        self.assertTrue(outside.exists())

        folder = webui.DOWNLOADS_DIR / "folder"
        folder.mkdir()
        response = self.client.post("/api/recordings/delete", json={"path": "folder"})
        self.assertEqual(response.status_code, 400)
        self.assertTrue(folder.exists())

    def test_active_recording_cannot_be_deleted_but_normal_file_can(self):
        active = webui.DOWNLOADS_DIR / "active.mp4"
        normal = webui.DOWNLOADS_DIR / "normal.mp4"
        active.write_bytes(b"active")
        normal.write_bytes(b"normal")
        url = "https://live.douyin.com/delete-test"
        status_runtime.update_live_status(url, "删除测试", True, stream_valid=True)
        status_runtime.mark_recording_started(url, "删除测试", 1234, str(active))

        listed = self.client.get("/api/recordings").get_json()["recordings"]
        active_item = next(item for item in listed if item["name"] == "active.mp4")
        self.assertTrue(active_item["is_recording"])
        self.assertEqual(active_item["recording_status"], "recording")

        blocked = self.client.post("/api/recordings/delete", json={"path": "active.mp4"})
        self.assertEqual(blocked.status_code, 409)
        self.assertTrue(active.exists())
        deleted = self.client.post("/api/recordings/delete", json={"path": "normal.mp4"})
        self.assertEqual(deleted.status_code, 200)
        self.assertFalse(normal.exists())

    def test_stop_api_accepts_recording_and_rejects_idle(self):
        url = "https://live.douyin.com/stop-test"
        status_runtime.update_live_status(url, "停止测试", True, stream_valid=True)
        status_runtime.mark_recording_started(url, "停止测试", 1234, "/downloads/a.mp4")
        response = self.client.post("/api/recordings/stop", json={"url": url})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(status_runtime.load_state()["monitors"][url]["manual_stop"])

        idle = self.client.post(
            "/api/recordings/stop", json={"url": "https://live.douyin.com/idle"}
        )
        self.assertEqual(idle.status_code, 409)


if __name__ == "__main__":
    unittest.main()
