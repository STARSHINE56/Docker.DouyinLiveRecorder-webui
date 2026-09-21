# -*- encoding: utf-8 -*-

"""
Author: Hmily
GitHub:https://github.com/ihmily
Date: 2023-07-17 23:52:05
Update: 2025-02-04 04:57:00
Copyright (c) 2023 by Hmily, All Rights Reserved.
"""
import re
import urllib.parse
import execjs
import httpx
import urllib.request
import asyncio
import json
from . import JS_SCRIPT_PATH, utils

no_proxy_handler = urllib.request.ProxyHandler({})
opener = urllib.request.build_opener(no_proxy_handler)

_SHORT_URL_CACHE: dict[str, dict] = {}
_SEC_ID_RE = re.compile(r"MS4wLj[A-Za-z0-9_-]+")


def extract_douyin_identifiers(value: str) -> dict:
    """Extract stable Douyin identifiers without depending on page layout."""
    raw = (value or "").strip()
    result = {"web_rid": None, "room_id": None, "sec_user_id": None, "unique_id": None}
    live = re.search(r"live\.douyin\.com/([^/?#]+)", raw)
    reflow = re.search(r"/reflow/(\d{8,25})", raw)
    user = re.search(r"douyin\.com/(?:user|share/user)/([A-Za-z0-9._-]+)", raw)
    query = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query)
    if live:
        result["web_rid"] = live.group(1)
    if reflow:
        result["room_id"] = reflow.group(1)
    if user:
        user_value = user.group(1)
        if user_value.startswith("MS4wLj"):
            result["sec_user_id"] = user_value
        else:
            result["unique_id"] = user_value
    elif query.get("sec_user_id"):
        result["sec_user_id"] = query["sec_user_id"][0]
    elif _SEC_ID_RE.fullmatch(raw):
        result["sec_user_id"] = raw
    elif re.fullmatch(r"[A-Za-z0-9._-]{2,40}", raw) and not raw.isdigit():
        result["unique_id"] = raw
    elif raw.isdigit():
        result["web_rid"] = raw
    return result


async def resolve_douyin_short_url(url: str, proxy_addr: str | None = None,
                                   headers: dict | None = None) -> dict:
    """Resolve v.douyin via HEAD, then a redirect-following GET fallback."""
    if url in _SHORT_URL_CACHE:
        return dict(_SHORT_URL_CACHE[url])
    headers = headers or HEADERS
    proxy_addr = utils.handle_proxy_addr(proxy_addr)
    final_url = ""
    async with httpx.AsyncClient(proxy=proxy_addr, timeout=15, follow_redirects=False) as client:
        try:
            response = await client.head(url, headers=headers)
            final_url = response.headers.get("location", "")
        except httpx.HTTPError:
            pass
        if not final_url or not any(host in final_url for host in ("live.douyin.com", "webcast.amemv.com", "douyin.com/user")):
            response = await client.get(url, headers=headers, follow_redirects=True)
            response.raise_for_status()
            final_url = str(response.url)
    identifiers = extract_douyin_identifiers(final_url)
    identifiers["resolved_url"] = final_url.split("?", 1)[0]
    # The query is deliberately parsed before tracking parameters are removed.
    _SHORT_URL_CACHE[url] = dict(identifiers)
    return identifiers


class EmptyDouyinProfileError(RuntimeError):
    """Profile API returned HTTP 200 but no usable anchor identifiers."""


def _profile_has_core_info(profile: dict) -> bool:
    """True when at least one stable identifier or nickname is present."""
    if (profile.get("nickname") or "").strip():
        return True
    if profile.get("sec_user_id"):
        return True
    if profile.get("web_rid"):
        return True
    if profile.get("room_id"):
        return True
    return False


async def fetch_douyin_user_profile(sec_user_id: str, proxy_addr: str | None = None,
                                    headers: dict | None = None, retries: int = 3) -> dict:
    """Fetch nickname and live metadata from the stable web profile endpoint.

    HTTP 200 with an empty / incomplete user object is treated as failure so
    callers fall back to page parsing instead of treating an empty profile as
    success.
    """
    headers = headers or HEADERS_PC
    params = {"device_platform": "webapp", "aid": "6383", "sec_user_id": sec_user_id}
    api = "https://www.douyin.com/aweme/v1/web/user/profile/other/"
    proxy_addr = utils.handle_proxy_addr(proxy_addr)
    last_error = None
    async with httpx.AsyncClient(proxy=proxy_addr, timeout=15) as client:
        for attempt in range(retries):
            try:
                response = await client.get(api, params=params, headers=headers)
                response.raise_for_status()
                payload = response.json()
                user = (payload.get("user") or {})
                if not user:
                    raise EmptyDouyinProfileError(
                        f"抖音主播资料接口返回空 user: sec_user_id={sec_user_id}"
                    )
                room_data = user.get("room_data") or {}
                if isinstance(room_data, str):
                    room_data = json.loads(room_data) if room_data.strip() else {}
                web_rid = (room_data.get("owner") or {}).get("web_rid") or room_data.get("web_rid")
                web_rid = web_rid or user.get("web_rid_str") or user.get("web_rid")
                avatar_urls = (user.get("avatar_thumb") or {}).get("url_list") or []
                room_status = room_data.get("status")
                profile = {
                    "sec_user_id": sec_user_id,
                    "nickname": (user.get("nickname") or "").strip(),
                    "avatar": avatar_urls[0] if avatar_urls else "",
                    "room_id": str(room_data.get("id_str") or room_data.get("id") or "") or None,
                    "web_rid": str(web_rid) if web_rid else None,
                    "is_live": None if room_status is None else str(room_status) == "2",
                }
                if not _profile_has_core_info(profile):
                    raise EmptyDouyinProfileError(
                        f"抖音主播资料接口缺少核心字段: sec_user_id={sec_user_id}"
                    )
                return profile
            except EmptyDouyinProfileError:
                raise
            except (httpx.HTTPError, ValueError, json.JSONDecodeError, KeyError, TypeError) as exc:
                last_error = exc
                if attempt + 1 < retries:
                    await asyncio.sleep(0.5)
    raise RuntimeError(f"抖音主播资料接口请求失败: {last_error}")


def _extract_from_page_json(html: str) -> dict:
    """Prefer structured hydration / RENDER_DATA JSON over brittle regexes."""
    result = {
        "sec_user_id": None,
        "unique_id": None,
        "nickname": "",
        "web_rid": None,
        "room_id": None,
        "is_live": None,
    }
    candidates: list[str] = []
    # Common Douyin page embeds
    for pattern in (
        r'<script[^>]*id="RENDER_DATA"[^>]*>(.*?)</script>',
        r'<script[^>]*>window\._SSR_HYDRATED_DATA\s*=\s*(\{.*?\})</script>',
        r'<script[^>]*>window\.__INIT_PROPS__\s*=\s*(\{.*?\})</script>',
        r'"userInfo"\s*:\s*(\{.*?\})\s*,\s*"',
    ):
        for match in re.finditer(pattern, html, re.DOTALL | re.IGNORECASE):
            candidates.append(match.group(1))
    # Unescape common encodings used in RENDER_DATA
    for raw in candidates:
        text = raw
        try:
            text = urllib.parse.unquote(text)
        except Exception:
            pass
        text = text.replace('\\"', '"').replace("\\u002F", "/").replace("\\u0026", "&")
        # Pull individual fields with tolerant patterns
        sec = re.search(r'"secUid"\s*:\s*"([^"]+)"|"sec_uid"\s*:\s*"([^"]+)"|"sec_user_id"\s*:\s*"([^"]+)"', text)
        if sec:
            result["sec_user_id"] = next(g for g in sec.groups() if g)
        uid = re.search(r'"uniqueId"\s*:\s*"([^"]+)"|"unique_id"\s*:\s*"([^"]+)"', text)
        if uid:
            result["unique_id"] = next(g for g in uid.groups() if g)
        nick = re.search(r'"nickname"\s*:\s*"([^"\\]+)"', text)
        if nick:
            result["nickname"] = nick.group(1)
        web = re.search(r'"web_rid"\s*:\s*"?(\d+)"?|"webRid"\s*:\s*"?(\d+)"?', text)
        if web:
            result["web_rid"] = next(g for g in web.groups() if g)
        room = re.search(r'"roomId"\s*:\s*"?(\d+)"?|"room_id"\s*:\s*"?(\d+)"?|"id_str"\s*:\s*"(\d{10,})"', text)
        if room:
            result["room_id"] = next(g for g in room.groups() if g)
        status = re.search(r'"status"\s*:\s*(\d+)', text)
        if status:
            result["is_live"] = status.group(1) == "2"
        if _profile_has_core_info(result):
            return result
    return result


async def fetch_douyin_user_page(value: str, proxy_addr: str | None = None,
                                 headers: dict | None = None) -> dict:
    """Page fallback for profile API changes or transient rejection.

    Tries structured JSON / hydration data first, then falls back to legacy
    regex extraction so a single page layout change does not break everything.
    """
    page_url = value if value.startswith("http") else f"https://www.douyin.com/user/{urllib.parse.quote(value)}"
    proxy = utils.handle_proxy_addr(proxy_addr)
    async with httpx.AsyncClient(proxy=proxy, timeout=15, follow_redirects=True) as client:
        response = await client.get(page_url, headers=headers or HEADERS_PC)
        response.raise_for_status()
        html = response.text

    structured = _extract_from_page_json(html)
    if _profile_has_core_info(structured):
        return structured

    # Legacy regex fallback
    sec_matches = _SEC_ID_RE.findall(html)
    nickname_match = re.search(r'(?:\\?")nickname(?:\\?")\s*:\s*(?:\\?")([^"\\,}]+)', html)
    web_match = re.search(r'(?:\\?")web_rid(?:\\?")\s*:\s*(?:\\?")?(\d+)', html)
    status_match = re.search(r'(?:\\?")status(?:\\?")\s*:\s*(\d+)', html)
    room_match = re.search(r'(?:\\?")(?:roomId|room_id|id_str)(?:\\?")\s*:\s*(?:\\?")?(\d{8,})', html)
    is_live = structured.get("is_live")
    if status_match:
        is_live = status_match.group(1) == "2"
    return {
        "sec_user_id": structured.get("sec_user_id") or (sec_matches[0] if sec_matches else None),
        "unique_id": structured.get("unique_id"),
        "nickname": structured.get("nickname") or (nickname_match.group(1) if nickname_match else ""),
        "web_rid": structured.get("web_rid") or (web_match.group(1) if web_match else None),
        "room_id": structured.get("room_id") or (room_match.group(1) if room_match else None),
        "is_live": is_live,
    }


async def resolve_douyin_profile(value: str, proxy_addr: str | None = None,
                                 headers: dict | None = None) -> dict:
    """Resolve live/user/share/short/sec-id/unique-id inputs to one profile shape."""
    raw = (value or "").strip()
    identifiers = extract_douyin_identifiers(raw)
    if "v.douyin.com" in raw:
        identifiers.update({k: v for k, v in (await resolve_douyin_short_url(
            raw, proxy_addr, headers
        )).items() if v})
    if identifiers.get("web_rid"):
        return {**identifiers, "nickname": "", "is_live": None}
    sec_user_id = identifiers.get("sec_user_id")
    if not sec_user_id and identifiers.get("unique_id"):
        fallback = await fetch_douyin_user_page(identifiers["unique_id"], proxy_addr, headers)
        sec_user_id = fallback.get("sec_user_id")
        if not sec_user_id:
            return {**identifiers, **fallback}
    if sec_user_id:
        try:
            profile = await fetch_douyin_user_profile(sec_user_id, proxy_addr, headers)
        except Exception:
            profile = await fetch_douyin_user_page(
                f"https://www.douyin.com/user/{sec_user_id}", proxy_addr, headers
            )
            profile["sec_user_id"] = sec_user_id
        return {**identifiers, **profile}
    if identifiers.get("room_id"):
        return {**identifiers, "nickname": "", "is_live": None}
    raise ValueError(f"无法识别抖音地址或账号: {raw}")


HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Linux; Android 11; SAMSUNG SM-G973U) AppleWebKit/537.36 (KHTML, like Gecko) '
                  'SamsungBrowser/14.2 Chrome/87.0.4280.141 Mobile Safari/537.36',
    'Accept-Language': 'zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2',
    'Cookie': 's_v_web_id=verify_lk07kv74_QZYCUApD_xhiB_405x_Ax51_GYO9bUIyZQVf'
}

HEADERS_PC = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.5845.97 '
                  'Safari/537.36 Core/1.116.438.400 QQBrowser/13.0.6070.400',
    'Cookie': 'sessionid=7494ae59ae06784454373ce25761e864; __ac_nonce=0670497840077ee4c9eb2; '
              '__ac_signature=_02B4Z6wo00f012DZczQAAIDCJJBb3EjnINdg-XeAAL8-db;  '
              's_v_web_id=verify_m1ztgtjj_vuHnMLZD_iwZ9_4YO4_BdN1_7wLP3pyqXsf2; ',
    }


# X-bogus算法
async def get_xbogus(url: str, headers: dict | None = None) -> str:
    if not headers or 'user-agent' not in (k.lower() for k in headers):
        headers = HEADERS
    query = urllib.parse.urlparse(url).query
    xbogus = execjs.compile(open(f'{JS_SCRIPT_PATH}/x-bogus.js').read()).call(
        'sign', query, headers.get("User-Agent", "user-agent"))
    return xbogus


# 获取房间ID和用户secID
async def get_sec_user_id(url: str, proxy_addr: str | None = None, headers: dict | None = None) -> tuple | None:
    if not headers or all(k.lower() not in ['user-agent', 'cookie'] for k in headers):
        headers = HEADERS

    try:
        proxy_addr = utils.handle_proxy_addr(proxy_addr)
        async with httpx.AsyncClient(proxy=proxy_addr, timeout=15) as client:
            response = await client.get(url, headers=headers, follow_redirects=True)
            redirect_url = response.url
            if 'reflow/' in str(redirect_url):
                match = re.search(r'sec_user_id=([\w_\-]+)&', str(redirect_url))
                if match:
                    sec_user_id = match.group(1)
                    room_id = str(redirect_url).split('?')[0].rsplit('/', maxsplit=1)[1]
                    return room_id, sec_user_id
                else:
                    print("Could not find sec_user_id in the URL.")
            else:
                print("The redirect URL does not contain 'reflow/'.")
    except Exception as e:
        print(f"An error occurred: {e}")
    return None


# 获取抖音号
async def get_unique_id(url: str, proxy_addr: str | None = None, headers: dict | None = None) -> str | None:
    if not headers or all(k.lower() not in ['user-agent', 'cookie'] for k in headers):
        headers = HEADERS_PC

    try:
        proxy_addr = utils.handle_proxy_addr(proxy_addr)
        async with httpx.AsyncClient(proxy=proxy_addr, timeout=15) as client:
            response = await client.get(url, headers=headers, follow_redirects=True)
            redirect_url = str(response.url)
            sec_user_id = redirect_url.split('?')[0].rsplit('/', maxsplit=1)[1]

            user_page_response = await client.get(f'https://www.douyin.com/user/{sec_user_id}', headers=headers)
            matches = re.findall(r'undefined\\"},\\"uniqueId\\":\\"(.*?)\\",\\"customVerify',
                                 user_page_response.text)
            if matches:
                unique_id = matches[-1]
                return unique_id
            else:
                print("Could not find unique_id in the response.")
                return None
    except Exception as e:
        print(f"An error occurred: {e}")
        return None


# 获取直播间webID
async def get_live_room_id(room_id: str, sec_user_id: str, proxy_addr: str | None = None, params: dict | None = None,
                           headers: dict | None = None) -> str:
    if not headers or all(k.lower() not in ['user-agent', 'cookie'] for k in headers):
        headers = HEADERS

    if not params:
        params = {
            "verifyFp": "verify_lk07kv74_QZYCUApD_xhiB_405x_Ax51_GYO9bUIyZQVf",
            "type_id": "0",
            "live_id": "1",
            "room_id": room_id,
            "sec_user_id": sec_user_id,
            "app_id": "1128",
            "msToken": "wrqzbEaTlsxt52-vxyZo_mIoL0RjNi1ZdDe7gzEGMUTVh_HvmbLLkQrA_1HKVOa2C6gkxb6IiY6TY2z8enAkPEwGq--gM"
                       "-me3Yudck2ailla5Q4osnYIHxd9dI4WtQ==",
        }

    api = f'https://webcast.amemv.com/webcast/room/reflow/info/?{urllib.parse.urlencode(params)}'
    xbogus = await get_xbogus(api)
    api = api + "&X-Bogus=" + xbogus

    try:
        proxy_addr = utils.handle_proxy_addr(proxy_addr)
        async with httpx.AsyncClient(proxy=proxy_addr,
                                     timeout=15) as client:
            response = await client.get(api, headers=headers)
            response.raise_for_status()
            json_data = response.json()
            return json_data['data']['room']['owner']['web_rid']
    except httpx.HTTPStatusError as e:
        print(f"HTTP status error occurred: {e.response.status_code}")
        raise
    except Exception as e:
        print(f"An exception occurred during get_live_room_id: {e}")
        raise


if __name__ == '__main__':
    room_url = "https://v.douyin.com/iQLgKSj/"
    _room_id, sec_uid = get_sec_user_id(room_url)
    web_rid = get_live_room_id(_room_id, sec_uid)
    print("return web_rid:", web_rid)
