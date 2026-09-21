import unittest
from unittest.mock import AsyncMock, patch

from streamget import room, spider, stream


class DouyinIdentifierTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
