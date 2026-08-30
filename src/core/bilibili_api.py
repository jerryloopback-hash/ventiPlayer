"""Bilibili web API client with WBI signature support."""

import hashlib
import time
import re
import threading
import urllib.request
import urllib.parse
import json
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class BiliVideoInfo:
    bvid: str
    aid: int
    title: str
    duration: int  # seconds
    thumbnail: str
    owner_mid: int
    owner_name: str
    # Season/collection info (if video belongs to one)
    season_id: Optional[int] = None
    season_title: Optional[str] = None


@dataclass
class BiliVideoItem:
    """Lightweight video reference for lists."""
    bvid: str
    title: str
    duration: int
    thumbnail: str
    owner_name: str


# The mixin key encoding table (64 entries, indices into the combined key)
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


class BilibiliAPI:
    """Bilibili web API client with WBI signature support."""

    BASE = "https://api.bilibili.com"

    def __init__(self, sessdata: str = "", buvid3: str = ""):
        self._sessdata = sessdata
        self._buvid3 = buvid3
        self._img_key = ""
        self._sub_key = ""
        self._wbi_updated = 0.0  # timestamp of last WBI key fetch
        self._wbi_lock = threading.Lock()

    def set_cookies_from_file(self, cookie_file: str):
        """Extract SESSDATA and buvid3 from a Netscape cookie file."""
        if not cookie_file:
            return
        try:
            with open(cookie_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("\t")
                    if len(parts) < 7:
                        continue
                    name = parts[5]
                    value = parts[6]
                    if name == "SESSDATA":
                        self._sessdata = value
                    elif name == "buvid3":
                        self._buvid3 = value
        except (OSError, UnicodeDecodeError) as e:
            logger.warning("Failed to read cookie file: %s", e)

    def _get_mixin_key(self, raw: str) -> str:
        """Apply the mixin key encoding table permutation."""
        return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]

    def _ensure_wbi_keys(self):
        """Fetch WBI keys from nav API if not cached or expired (cache 1 hour)."""
        now = time.time()
        if self._img_key and self._sub_key and (now - self._wbi_updated) < 3600:
            return

        with self._wbi_lock:
            # Double-check after acquiring lock
            now = time.time()
            if self._img_key and self._sub_key and (now - self._wbi_updated) < 3600:
                return

            try:
                data = self._request("/x/web-interface/nav", need_wbi=False)
                if not data:
                    return
                wbi_img = data.get("wbi_img", {})
                img_url = wbi_img.get("img_url", "")
                sub_url = wbi_img.get("sub_url", "")
                if img_url:
                    self._img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
                if sub_url:
                    self._sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
                self._wbi_updated = now
            except Exception as e:
                logger.warning("Failed to fetch WBI keys: %s", e)

    def _sign_wbi(self, params: dict) -> dict:
        """Add WBI signature (w_rid, wts) to params."""
        self._ensure_wbi_keys()
        if not self._img_key or not self._sub_key:
            # Can't sign without keys, return params as-is
            return params

        mixin_key = self._get_mixin_key(self._img_key + self._sub_key)
        params = dict(params)
        params["wts"] = int(time.time())

        # Filter out values with special characters, keep str/int/float
        filtered = {}
        for k, v in sorted(params.items()):
            if not isinstance(v, (str, int, float)):
                continue
            # Remove characters that break the signature
            v_str = str(v)
            v_str = re.sub(r"[!'()*]", "", v_str)
            filtered[k] = v_str

        query = urllib.parse.urlencode(filtered)
        w_rid = hashlib.md5((query + mixin_key).encode()).hexdigest()
        filtered["w_rid"] = w_rid
        return filtered

    def _request(self, path: str, params: dict = None, need_wbi: bool = False) -> Optional[dict]:
        """Make a GET request to the Bilibili API.

        Returns the 'data' field from the response if code == 0, else None.
        """
        if params is None:
            params = {}

        if need_wbi:
            params = self._sign_wbi(params)

        url = self.BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)

        headers = {
            "User-Agent": _USER_AGENT,
            "Referer": "https://www.bilibili.com",
        }
        if self._sessdata or self._buvid3:
            cookie_parts = []
            if self._sessdata:
                cookie_parts.append(f"SESSDATA={self._sessdata}")
            if self._buvid3:
                cookie_parts.append(f"buvid3={self._buvid3}")
            headers["Cookie"] = "; ".join(cookie_parts)

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.warning("Bilibili API request failed [%s]: %s", path, e)
            return None

        if body.get("code") != 0:
            logger.debug("Bilibili API error [%s]: code=%s msg=%s",
                         path, body.get("code"), body.get("message"))
            return None

        return body.get("data")

    def get_video_info(self, bvid: str) -> Optional[BiliVideoInfo]:
        """GET /x/web-interface/view?bvid=xxx

        Returns video details including season/collection membership.
        """
        data = self._request("/x/web-interface/view", {"bvid": bvid}, need_wbi=True)
        if not data:
            return None

        season_id = None
        season_title = None
        ugc_season = data.get("ugc_season")
        if ugc_season:
            season_id = ugc_season.get("id")
            season_title = ugc_season.get("title")

        owner = data.get("owner", {})
        return BiliVideoInfo(
            bvid=data.get("bvid", bvid),
            aid=data.get("aid", 0),
            title=data.get("title", ""),
            duration=data.get("duration", 0),
            thumbnail=data.get("pic", ""),
            owner_mid=owner.get("mid", 0),
            owner_name=owner.get("name", ""),
            season_id=season_id,
            season_title=season_title,
        )

    def get_related_videos(self, bvid: str) -> list[BiliVideoItem]:
        """GET /x/web-interface/archive/related?bvid=xxx

        Returns up to 40 related videos.
        """
        data = self._request("/x/web-interface/archive/related", {"bvid": bvid}, need_wbi=True)
        if not data or not isinstance(data, list):
            return []

        results = []
        for item in data:
            owner = item.get("owner", {})
            results.append(BiliVideoItem(
                bvid=item.get("bvid", ""),
                title=item.get("title", ""),
                duration=item.get("duration", 0),
                thumbnail=item.get("pic", ""),
                owner_name=owner.get("name", ""),
            ))
        return results

    def get_season_videos(self, mid: int, season_id: int) -> list[BiliVideoItem]:
        """GET /x/polymer/web-space/seasons_archives_list

        Returns all videos in a collection/season.
        """
        params = {
            "mid": mid,
            "season_id": season_id,
            "page_num": 1,
            "page_size": 100,
            "sort_reverse": "false",
        }
        data = self._request("/x/polymer/web-space/seasons_archives_list", params, need_wbi=True)
        if not data:
            return []

        archives = data.get("archives", [])
        results = []
        for item in archives:
            results.append(BiliVideoItem(
                bvid=item.get("bvid", ""),
                title=item.get("title", ""),
                duration=item.get("duration", 0),
                thumbnail=item.get("pic", ""),
                owner_name="",  # not provided in this endpoint
            ))
        return results

    def get_popular(self, page: int = 1) -> list[BiliVideoItem]:
        """GET /x/web-interface/popular?pn=<page>&ps=20

        Returns popular/trending videos.
        """
        data = self._request("/x/web-interface/popular", {"pn": page, "ps": 20}, need_wbi=True)
        if not data:
            return []

        video_list = data.get("list", [])
        results = []
        for item in video_list:
            owner = item.get("owner", {})
            results.append(BiliVideoItem(
                bvid=item.get("bvid", ""),
                title=item.get("title", ""),
                duration=item.get("duration", 0),
                thumbnail=item.get("pic", ""),
                owner_name=owner.get("name", ""),
            ))
        return results

    def get_user_mid(self) -> int:
        """Get logged-in user's mid from nav API."""
        data = self._request("/x/web-interface/nav", need_wbi=False)
        if not data:
            return 0
        return data.get("mid", 0)

    def get_user_favorites(self, mid: int) -> list[dict]:
        """GET /x/v3/fav/folder/created/list-all?up_mid=xxx

        Returns list of {id, title, media_count} dicts.
        """
        data = self._request(
            "/x/v3/fav/folder/created/list-all",
            {"up_mid": mid},
            need_wbi=False,
        )
        if not data:
            return []

        folder_list = data.get("list", [])
        results = []
        for item in folder_list:
            results.append({
                "id": item.get("id", 0),
                "title": item.get("title", ""),
                "media_count": item.get("media_count", 0),
            })
        return results

    def get_favorite_content(self, media_id: int, page: int = 1) -> list[BiliVideoItem]:
        """GET /x/v3/fav/resource/list?media_id=xxx&pn=1&ps=20

        Returns videos in a favorites folder.
        """
        data = self._request(
            "/x/v3/fav/resource/list",
            {"media_id": media_id, "pn": page, "ps": 20},
            need_wbi=False,
        )
        if not data:
            return []

        medias = data.get("medias") or []
        results = []
        for item in medias:
            upper = item.get("upper", {})
            results.append(BiliVideoItem(
                bvid=item.get("bvid", ""),
                title=item.get("title", ""),
                duration=item.get("duration", 0),
                thumbnail=item.get("cover", ""),
                owner_name=upper.get("name", ""),
            ))
        return results

    def search_videos(self, keyword: str, page: int = 1) -> list[BiliVideoItem]:
        """GET /x/web-interface/wbi/search/type?search_type=video&keyword=xxx

        Returns video search results.
        """
        data = self._request(
            "/x/web-interface/wbi/search/type",
            {"search_type": "video", "keyword": keyword, "page": page, "page_size": 20},
            need_wbi=True,
        )
        if not data:
            return []

        result_list = data.get("result") or []
        results = []
        for item in result_list:
            bvid = item.get("bvid", "")
            title = item.get("title", "")
            # Search results have HTML tags in title, strip them
            title = re.sub(r"<[^>]+>", "", title)
            duration_str = item.get("duration", "0:0")
            # Parse "mm:ss" to seconds
            duration = 0
            if isinstance(duration_str, str) and ":" in duration_str:
                parts = duration_str.split(":")
                try:
                    duration = int(parts[0]) * 60 + int(parts[1])
                except (ValueError, IndexError):
                    pass
            elif isinstance(duration_str, int):
                duration = duration_str
            pic = item.get("pic", "")
            if pic.startswith("//"):
                pic = "https:" + pic
            results.append(BiliVideoItem(
                bvid=bvid,
                title=title,
                duration=duration,
                thumbnail=pic,
                owner_name=item.get("author", ""),
            ))
        return results
