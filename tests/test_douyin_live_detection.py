# -*- encoding: utf-8 -*-
"""Regression: 抖音开播检测与自动录制。

Covers the reported failure "主播已开播但 WebUI 一直显示等待直播/未录制":
1. v.douyin.com 短链接跳转到 reflow 后 room_id 的提取。
2. reflow URL 只含 room_id（无 sec_user_id）时仍能解析出真实直播间。
3. 主播未开播（reflow 房间为空）正确返回未开播。
4. 主页状态与直播间状态冲突时不误判（reflow 场景 is_live=None 不阻塞开播检测）。
5. 直播间 API 返回异常时抛出，而不是被静默当成未开播。
6. 直播源存在但音轨探测失败时仍保留原录制源选择策略。
7. 函数参数兼容：main.py 以 probe_results=... 调用不再抛 TypeError。
8. 自动录制启动链路：开播后能取得有效 record_url 且 stream_valid 判定正确。
9. WebUI 状态更新：最近检测时间与错误说明被正确持久化并展示。
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from streamget import room, spider, stream
from streamget.stream import select_douyin_record_url


def make_live_room(room_id="123456789"):
    stream_data = json.dumps({
        "data": {
            "origin": {"main": {
                "hls": f"https://hls.example/{room_id}.m3u8",
                "flv": f"https://flv.example/{room_id}.flv",
            }}
        }
    })
    return {
        "status": 2,
        "title": "测试直播",
        "owner": {"nickname": "苦茶", "web_rid": room_id},
        "stream_url": {
            "live_core_sdk_data": {"pull_data": {"stream_data": stream_data}},
            "pull_datas": {},
            "hls_pull_url_map": {"HD1": f"https://hls.example/hd.m3u8"},
            "flv_pull_url": {"HD1": f"https://flv.example/hd.flv"},
        },
    }


def enter_response(room_data, nickname="苦茶"):
    """Shape returned by live.douyin.com/webcast/room/web/enter/."""
    return json.dumps({"data": {"data": room_data, "user": {"nickname": nickname}}})


class DouyinReflowResolutionTests(unittest.IsolatedAsyncioTestCase):
    """短链接 → reflow → 只拿 room_id 也能解析直播间。"""

    def test_short_url_reflow_extracts_room_id(self):
        resolved = room.extract_douyin_identifiers(
            "https://webcast.amemv.com/douyin/webcast/reflow/123456789?is_reflow=1"
        )
        self.assertEqual(resolved["room_id"], "123456789")
        self.assertIsNone(resolved["sec_user_id"])
        self.assertIsNone(resolved["web_rid"])

    async def test_short_url_to_reflow_resolves_profile_with_room_id(self):
        resolved = {
            "room_id": "123456789",
            "resolved_url": "https://webcast.amemv.com/douyin/webcast/reflow/123456789",
        }
        with patch.object(room, "resolve_douyin_short_url", AsyncMock(return_value=resolved)):
            value = await room.resolve_douyin_profile("https://v.douyin.com/tMdjKGxovtc/")
        self.assertEqual(value["room_id"], "123456789")
        self.assertIsNone(value["is_live"])  # 主页未确认开播，不应被当成离线

    async def test_reflow_room_id_only_detects_live_via_web_rid(self):
        """核心回归：reflow 只给 room_id 且 get_sec_user_id 失败时，
        用 room_id 当 web_rid 查询 enter API，仍能确认已开播并取得直播源。"""
        profile = {"room_id": "123456789", "sec_user_id": None,
                   "web_rid": None, "nickname": "", "is_live": None}
        resp = enter_response([make_live_room("123456789")])
        with patch.object(spider, "resolve_douyin_profile", AsyncMock(return_value=profile)), \
                patch.object(spider, "get_sec_user_id", AsyncMock(return_value=None)), \
                patch.object(spider, "async_req", AsyncMock(return_value=resp)) as req_mock:
            raw = await spider.get_douyin_app_stream_data("https://v.douyin.com/tMdjKGxovtc/")
        self.assertEqual(raw["status"], 2)
        self.assertIn("web_rid=123456789", req_mock.await_args.kwargs["url"])
        result = await stream.get_douyin_stream_url(raw, "OD")
        self.assertTrue(result["is_live"])
        self.assertTrue(result["record_url"])

    async def test_reflow_room_id_only_returns_offline_when_room_empty(self):
        profile = {"room_id": "999", "sec_user_id": None,
                   "web_rid": None, "nickname": "", "is_live": None}
        resp = enter_response([], nickname="苦茶")
        with patch.object(spider, "resolve_douyin_profile", AsyncMock(return_value=profile)), \
                patch.object(spider, "get_sec_user_id", AsyncMock(return_value=None)), \
                patch.object(spider, "async_req", AsyncMock(return_value=resp)):
            raw = await spider.get_douyin_app_stream_data("https://v.douyin.com/offline/")
        self.assertEqual(raw["status"], 4)
        self.assertFalse(raw["is_live"])
        self.assertEqual(raw["anchor_name"], "苦茶")

    async def test_profile_unknown_live_does_not_block_live_detection(self):
        """主页状态为未知（is_live=None）时不得覆盖直播间真实开播状态。"""
        profile = {"room_id": "123456789", "sec_user_id": None,
                   "web_rid": None, "nickname": "主页显示未知", "is_live": None}
        resp = enter_response([make_live_room("123456789")], nickname="苦茶")
        with patch.object(spider, "resolve_douyin_profile", AsyncMock(return_value=profile)), \
                patch.object(spider, "get_sec_user_id", AsyncMock(return_value=None)), \
                patch.object(spider, "async_req", AsyncMock(return_value=resp)):
            raw = await spider.get_douyin_app_stream_data("https://v.douyin.com/live/")
        self.assertEqual(raw["status"], 2)


class DouyinApiFailureTests(unittest.IsolatedAsyncioTestCase):
    """接口失败不能直接当作未开播。"""

    async def test_reflow_api_failure_raises_instead_of_offline(self):
        profile = {"room_id": "999", "sec_user_id": None,
                   "web_rid": None, "nickname": "", "is_live": None}
        with patch.object(spider, "resolve_douyin_profile", AsyncMock(return_value=profile)), \
                patch.object(spider, "get_sec_user_id", AsyncMock(return_value=None)), \
                patch.object(spider, "async_req",
                             AsyncMock(side_effect=ConnectionError("enter API timeout"))):
            with self.assertRaises(ConnectionError):
                await spider.get_douyin_app_stream_data("https://v.douyin.com/fail/")

    async def test_profile_resolver_failure_raises_instead_of_offline(self):
        with patch.object(spider, "resolve_douyin_profile",
                          AsyncMock(side_effect=RuntimeError("profile api down"))):
            with self.assertRaises(RuntimeError):
                await spider.get_douyin_app_stream_data("https://v.douyin.com/fail2/")


class DouyinRecordUrlSelectionTests(unittest.TestCase):
    """函数参数兼容 + 音轨探测失败保留原策略。"""

    def setUp(self):
        self.m3u8 = "https://hls.example/live.m3u8"
        self.flv = "https://flv.example/live.flv"

    def test_main_loop_call_pattern_no_typeerror(self):
        """main.py 的调用形态：probe_results=... 不再抛 TypeError。"""
        probe_results = {}
        selected = select_douyin_record_url(
            self.m3u8, self.flv,
            audio_probe=lambda url: None if url == self.m3u8 else True,
            probe_results=probe_results,
        )
        self.assertEqual(selected, self.flv)
        self.assertEqual(probe_results["m3u8"], None)
        self.assertEqual(probe_results["flv"], True)
        # main.py 的 stream_valid 判定：任一探测有确定结论即为可达。
        self.assertTrue(any(r is not None for r in probe_results.values()))

    def test_probe_failure_keeps_fallback_and_unknown_stream_valid(self):
        probe_results = {}
        selected = select_douyin_record_url(
            self.m3u8, self.flv,
            audio_probe=lambda _u: None,
            probe_results=probe_results,
        )
        self.assertEqual(selected, self.m3u8)  # 保留原有优先级
        self.assertFalse(any(r is not None for r in probe_results.values()))

    def test_no_audio_both_sources_keeps_main_loop_fallback(self):
        selected = select_douyin_record_url(
            self.m3u8, self.flv, audio_probe=lambda _u: False
        )
        self.assertEqual(selected, self.m3u8)


class DouyinAutoRecordingStartTests(unittest.IsolatedAsyncioTestCase):
    """开播 → 有效录制地址 → 自动录制启动链路。"""

    async def test_live_room_produces_record_url_and_stream_valid(self):
        profile = {"room_id": "123456789", "sec_user_id": None,
                   "web_rid": None, "nickname": "", "is_live": None}
        resp = enter_response([make_live_room("123456789")])
        with patch.object(spider, "resolve_douyin_profile", AsyncMock(return_value=profile)), \
                patch.object(spider, "get_sec_user_id", AsyncMock(return_value=None)), \
                patch.object(spider, "async_req", AsyncMock(return_value=resp)):
            raw = await spider.get_douyin_app_stream_data("https://v.douyin.com/live/")
        port_info = await stream.get_douyin_stream_url(raw, "OD")
        self.assertTrue(port_info["is_live"])
        self.assertTrue(port_info["record_url"])

        # 复刻 main.py 的探测选择链路（修复前此处抛 TypeError，阻断录制）。
        probe_results = {}
        selected = select_douyin_record_url(
            port_info.get("m3u8_url"),
            port_info.get("flv_url"),
            audio_probe=lambda url: None,
            probe_results=probe_results,
        )
        port_info["record_url"] = selected
        self.assertTrue(port_info["record_url"])
        # stream_valid: ffprobe 均失败 → 不可达，但 record_url 仍存在以便尝试录制。
        port_info["stream_valid"] = any(r is not None for r in probe_results.values())
        self.assertFalse(port_info["stream_valid"])


class DouyinWebUiStateTests(unittest.TestCase):
    """WebUI 最近检测时间 / 错误说明 / 状态同步。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        import status_runtime
        status_runtime.reset_for_tests(Path(self.temp.name) / "runtime_state.json")
        self.url = "https://v.douyin.com/tMdjKGxovtc/"
        self.name = "苦茶"

    def tearDown(self):
        self.temp.cleanup()

    def test_failed_check_persists_last_checked_and_error(self):
        import status_runtime
        status_runtime.mark_check_failed(self.url, self.name, "抖音 API 超时")
        item = status_runtime.load_state()["monitors"][self.url]
        self.assertEqual(item["monitor_status"], "error")
        self.assertIsNotNone(item["last_checked_at"])  # 最近检测时间不再为 --
        self.assertEqual(item["last_error"], "抖音 API 超时")
        self.assertEqual(item["live_status"], "unknown")  # 失败不谎报为离线

    def test_live_update_sets_last_checked_and_live(self):
        import status_runtime
        status_runtime.update_live_status(self.url, self.name, True, stream_valid=True)
        item = status_runtime.load_state()["monitors"][self.url]
        self.assertEqual(item["live_status"], "live")
        self.assertIsNotNone(item["last_checked_at"])
        self.assertIsNone(item["last_error"])

    def test_snapshot_surfaces_last_checked_and_error(self):
        import status_runtime
        import webui
        status_runtime.mark_check_failed(self.url, self.name, "接口失败")
        item = {
            "name": self.name, "url": self.url, "platform": "抖音",
            "monitor_status": "waiting", "live_status": "unknown", "recording_status": "idle",
            "last_checked_at": None, "last_success_at": None, "live_started_at": None,
            "recording_started_at": None, "recording_file": None, "last_error": None,
        }
        with patch.object(webui, "parse_monitor_lines", return_value=[item]):
            snapshot = webui.get_monitor_snapshot()
        monitor = snapshot["monitors"][0]
        self.assertEqual(monitor["monitor_status"], "error")
        self.assertIsNotNone(monitor["last_checked_at"])
        self.assertEqual(monitor["last_error"], "接口失败")


if __name__ == "__main__":
    unittest.main()
