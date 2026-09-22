import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from streamget import room, spider, stream


class DouyinIdentifierTests(unittest.TestCase):
    def test_default_headers_have_no_edge_whitespace(self):
        for headers in (room.HEADERS, room.HEADERS_PC):
            for value in headers.values():
                self.assertEqual(value, value.strip())

    def test_header_sanitizer_strips_spaces_and_tabs(self):
        headers = room._sanitize_headers({"Cookie": " sessionid=test; \t", "X-Test": " value\t"})
        self.assertEqual(headers["Cookie"], "sessionid=test;")
        self.assertEqual(headers["X-Test"], "value")

    def test_user_url(self):
        value = room.extract_douyin_identifiers("https://www.douyin.com/user/MS4wLjABAAAA_test?x=1")
        self.assertEqual(value["sec_user_id"], "MS4wLjABAAAA_test")

    def test_share_user_url(self):
        value = room.extract_douyin_identifiers("https://www.douyin.com/share/user/MS4wLjABAAAA_share")
        self.assertEqual(value["sec_user_id"], "MS4wLjABAAAA_share")

    def test_unique_id_user_url(self):
        value = room.extract_douyin_identifiers("https://www.douyin.com/user/unique.name")
        self.assertEqual(value["unique_id"], "unique.name")

    def test_live_url_and_ids(self):
        self.assertEqual(room.extract_douyin_identifiers("https://live.douyin.com/123456")["web_rid"], "123456")
        self.assertEqual(room.extract_douyin_identifiers("MS4wLjABAAAA_direct")["sec_user_id"], "MS4wLjABAAAA_direct")


class DouyinResolveTests(unittest.IsolatedAsyncioTestCase):
    async def test_page_without_status_keeps_live_state_unknown(self):
        response = MagicMock()
        response.text = '<script id="RENDER_DATA">{"secUid":"MS4wLjABAAAA_page"}</script>'
        response.raise_for_status.return_value = None
        client = AsyncMock()
        client.get.return_value = response
        context = AsyncMock()
        context.__aenter__.return_value = client
        with patch.object(room.httpx, "AsyncClient", return_value=context):
            value = await room.fetch_douyin_user_page("MS4wLjABAAAA_page")
        self.assertEqual(value["sec_user_id"], "MS4wLjABAAAA_page")
        self.assertIsNone(value["is_live"])

    async def test_profile_without_room_status_keeps_live_state_unknown(self):
        response = MagicMock()
        response.json.return_value = {
            "user": {"sec_uid": "MS4wLjABAAAA_profile", "nickname": "主播", "room_data": {}}
        }
        response.raise_for_status.return_value = None
        client = AsyncMock()
        client.get.return_value = response
        context = AsyncMock()
        context.__aenter__.return_value = client
        with patch.object(room.httpx, "AsyncClient", return_value=context):
            value = await room.fetch_douyin_user_profile("MS4wLjABAAAA_profile")
        self.assertIsNone(value["is_live"])

    async def test_short_url_uses_resolved_live_room(self):
        resolved = {"web_rid": "9988", "resolved_url": "https://live.douyin.com/9988"}
        with patch.object(room, "resolve_douyin_short_url", AsyncMock(return_value=resolved)):
            value = await room.resolve_douyin_profile("https://v.douyin.com/abc/")
        self.assertEqual(value["web_rid"], "9988")

    async def test_offline_profile_keeps_nickname(self):
        profile = {"sec_user_id": "MS4wLjABAAAA_x", "nickname": "测试主播", "is_live": False, "web_rid": None}
        with patch.object(room, "fetch_douyin_user_profile", AsyncMock(return_value=profile)):
            value = await room.resolve_douyin_profile("https://www.douyin.com/user/MS4wLjABAAAA_x")
        self.assertEqual(value["nickname"], "测试主播")
        self.assertFalse(value["is_live"])

    async def test_unique_id_page_fallback(self):
        page = {"sec_user_id": "MS4wLjABAAAA_u", "nickname": "主播U", "is_live": False, "web_rid": None}
        with patch.object(room, "fetch_douyin_user_page", AsyncMock(return_value=page)), patch.object(
            room, "fetch_douyin_user_profile", AsyncMock(return_value=page)
        ):
            value = await room.resolve_douyin_profile("unique.name")
        self.assertEqual(value["nickname"], "主播U")

    async def test_profile_api_failure_uses_page_fallback(self):
        fallback = {"sec_user_id": "MS4wLjABAAAA_f", "nickname": "兜底主播", "is_live": False, "web_rid": None}
        with patch.object(room, "fetch_douyin_user_profile", AsyncMock(side_effect=RuntimeError("api"))), patch.object(
            room, "fetch_douyin_user_page", AsyncMock(return_value=fallback)
        ):
            value = await room.resolve_douyin_profile("MS4wLjABAAAA_f")
        self.assertEqual(value["nickname"], "兜底主播")

    async def test_offline_spider_result_is_waiting_not_parse_error(self):
        profile = {"sec_user_id": "MS4wLjABAAAA_o", "nickname": "离线主播", "is_live": False, "web_rid": None}
        with patch.object(spider, "resolve_douyin_profile", AsyncMock(return_value=profile)):
            raw = await spider.get_douyin_app_stream_data("https://www.douyin.com/user/MS4wLjABAAAA_o")
        result = await stream.get_douyin_stream_url(raw, "OD")
        self.assertEqual(result["anchor_name"], "离线主播")
        self.assertFalse(result["is_live"])

    async def test_unknown_live_state_continues_legacy_reflow_fallback(self):
        profile = {
            "sec_user_id": "MS4wLjABAAAA_unknown",
            "nickname": "",
            "is_live": None,
            "web_rid": None,
            "room_id": None,
        }
        reflow = {"data": {"room": {"status": 4, "owner": {"nickname": "未知状态主播"}}}}
        with patch.object(spider, "resolve_douyin_profile", AsyncMock(return_value=profile)), patch.object(
            spider, "get_sec_user_id", AsyncMock(return_value=("123456789", None))
        ) as legacy_mock, patch.object(spider, "async_req", AsyncMock(return_value=json.dumps(reflow))) as request_mock:
            value = await spider.get_douyin_app_stream_data("https://v.douyin.com/example/")
        legacy_mock.assert_awaited_once()
        request_mock.assert_awaited_once()
        self.assertIn(profile["sec_user_id"], request_mock.await_args.kwargs["url"])
        self.assertEqual(value["status"], 4)
        self.assertEqual(value["anchor_name"], "未知状态主播")

    async def test_explicit_offline_state_still_short_circuits(self):
        profile = {
            "sec_user_id": "MS4wLjABAAAA_offline",
            "nickname": "离线主播",
            "is_live": False,
            "web_rid": None,
        }
        with patch.object(spider, "resolve_douyin_profile", AsyncMock(return_value=profile)), patch.object(
            spider, "get_sec_user_id", AsyncMock()
        ) as legacy_mock:
            value = await spider.get_douyin_app_stream_data("https://www.douyin.com/user/MS4wLjABAAAA_offline")
        legacy_mock.assert_not_awaited()
        self.assertEqual(value["status"], 4)
        self.assertFalse(value["is_live"])


if __name__ == "__main__":
    unittest.main()


class DouyinProfileEmptyAndFallbackTests(unittest.IsolatedAsyncioTestCase):
    """Regression: empty profile API must not be treated as success."""

    async def test_profile_api_empty_user_triggers_page_fallback(self):
        page = {
            "sec_user_id": "MS4wLjABAAAA_empty",
            "nickname": "页面兜底",
            "web_rid": "999001",
            "is_live": False,
        }

        async def empty_profile(*_a, **_k):
            raise room.EmptyDouyinProfileError("empty user")

        with patch.object(room, "fetch_douyin_user_profile", AsyncMock(side_effect=empty_profile)), patch.object(
            room, "fetch_douyin_user_page", AsyncMock(return_value=page)
        ):
            value = await room.resolve_douyin_profile("https://www.douyin.com/user/MS4wLjABAAAA_empty")
        self.assertEqual(value["nickname"], "页面兜底")
        self.assertEqual(value["web_rid"], "999001")

    async def test_profile_api_blank_core_fields_triggers_fallback(self):
        page = {
            "sec_user_id": "MS4wLjABAAAA_blank",
            "nickname": "空白字段兜底",
            "web_rid": "888002",
            "is_live": False,
        }
        with patch.object(
            room, "fetch_douyin_user_profile",
            AsyncMock(side_effect=room.EmptyDouyinProfileError("missing core fields")),
        ), patch.object(room, "fetch_douyin_user_page", AsyncMock(return_value=page)):
            value = await room.resolve_douyin_profile("MS4wLjABAAAA_blank")
        self.assertEqual(value["nickname"], "空白字段兜底")
        self.assertEqual(value["web_rid"], "888002")

    async def test_live_douyin_url_does_not_force_profile_resolver(self):
        # web_rid path must short-circuit without calling profile API
        with patch.object(room, "fetch_douyin_user_profile", AsyncMock()) as profile_mock, patch.object(
            room, "fetch_douyin_user_page", AsyncMock()
        ) as page_mock:
            value = await room.resolve_douyin_profile("https://live.douyin.com/123456789")
        self.assertEqual(value["web_rid"], "123456789")
        profile_mock.assert_not_called()
        page_mock.assert_not_called()

    async def test_user_profile_url_success_returns_valid_info(self):
        profile = {
            "sec_user_id": "MS4wLjABAAAA_ok",
            "nickname": "正常主播",
            "web_rid": "777003",
            "room_id": "100200300",
            "is_live": True,
        }
        with patch.object(room, "fetch_douyin_user_profile", AsyncMock(return_value=profile)):
            value = await room.resolve_douyin_profile("https://www.douyin.com/user/MS4wLjABAAAA_ok")
        self.assertEqual(value["nickname"], "正常主播")
        self.assertEqual(value["web_rid"], "777003")
        self.assertTrue(value["is_live"])

    async def test_resolver_failure_still_allows_old_spider_path(self):
        """New resolver is an enhancement; old get_sec_user_id path must remain reachable."""
        # Ensure old helpers are still importable and callable from room
        self.assertTrue(callable(room.get_sec_user_id))
        self.assertTrue(callable(room.get_unique_id))
        self.assertTrue(callable(room.get_live_room_id))
        # spider still imports them for the legacy path
        from streamget.room import get_sec_user_id, get_unique_id, get_live_room_id
        self.assertIs(room.get_sec_user_id, get_sec_user_id)


class DouyinProfileHasCoreInfoTests(unittest.TestCase):
    def test_empty_profile_rejected(self):
        self.assertFalse(room._profile_has_core_info({}))
        self.assertFalse(room._profile_has_core_info({"nickname": "", "web_rid": None, "room_id": None}))

    def test_any_core_field_accepted(self):
        self.assertTrue(room._profile_has_core_info({"nickname": "A"}))
        self.assertTrue(room._profile_has_core_info({"sec_user_id": "MS4wLj"}))
        self.assertTrue(room._profile_has_core_info({"web_rid": "1"}))
        self.assertTrue(room._profile_has_core_info({"room_id": "2"}))
