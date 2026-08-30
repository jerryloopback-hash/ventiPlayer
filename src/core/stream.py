import threading
import tempfile
import os
import urllib.request
import json as json_mod
from dataclasses import dataclass
from typing import Optional

import yt_dlp


@dataclass
class StreamInfo:
    title: str
    url: str
    audio_url: str
    video_url: str
    duration: Optional[float]
    audio_codec: str
    audio_sample_rate: Optional[int]
    audio_bitrate: Optional[int]
    thumbnail: Optional[str]
    http_headers: dict | None = None
    video_resolution: str = ""
    video_width: Optional[int] = None
    video_height: Optional[int] = None
    video_fps: Optional[float] = None
    cookie_failed: bool = False
    is_live: bool = False


@dataclass
class CookieStatus:
    platform: str  # "bilibili", "youtube", "unknown"
    logged_in: bool
    is_vip: bool
    username: str


def fix_cookie_file(path: str) -> str:
    """Fix Netscape cookie file format issues for Python 3.14+ compatibility."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except (OSError, UnicodeDecodeError):
        return path

    fixed = False
    result = []
    for line in lines:
        if line.startswith("#") or line.strip() == "":
            result.append(line)
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            domain = parts[0]
            domain_flag = parts[1].strip()
            if domain.startswith(".") and domain_flag.upper() == "FALSE":
                parts[1] = "TRUE"
                fixed = True
            elif not domain.startswith(".") and domain_flag.upper() == "TRUE":
                parts[1] = "FALSE"
                fixed = True
        result.append("\t".join(parts))

    if not fixed:
        return path

    tmp_dir = os.path.join(tempfile.gettempdir(), "ventiplayer")
    os.makedirs(tmp_dir, exist_ok=True)
    fixed_path = os.path.join(tmp_dir, "cookies_fixed.txt")
    with open(fixed_path, "w", encoding="utf-8") as f:
        f.writelines(result)
    return fixed_path


def check_cookie_status(cookie_file: str) -> CookieStatus:
    """Check cookie file to determine platform and VIP status."""
    if not cookie_file or not os.path.exists(cookie_file):
        return CookieStatus("unknown", False, False, "")

    try:
        with open(cookie_file, "r", encoding="utf-8") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError):
        return CookieStatus("unknown", False, False, "")

    has_bilibili = "bilibili.com" in content
    has_youtube = "youtube.com" in content

    if has_bilibili:
        return _check_bilibili_vip(cookie_file)
    elif has_youtube:
        return CookieStatus("youtube", True, False, "")
    return CookieStatus("unknown", False, False, "")


def _check_bilibili_vip(cookie_file: str) -> CookieStatus:
    """Query Bilibili nav API to check VIP status."""
    sessdata = ""
    try:
        fixed = fix_cookie_file(cookie_file)
        with open(fixed, "r", encoding="utf-8") as f:
            for line in f:
                if "SESSDATA" in line:
                    parts = line.strip().split("\t")
                    if len(parts) >= 7:
                        sessdata = parts[6]
                    break
    except (OSError, UnicodeDecodeError):
        pass

    if not sessdata:
        return CookieStatus("bilibili", False, False, "")

    try:
        req = urllib.request.Request(
            "https://api.bilibili.com/x/web-interface/nav",
            headers={
                "Cookie": f"SESSDATA={sessdata}",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://www.bilibili.com",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json_mod.loads(resp.read().decode("utf-8"))

        if data.get("code") != 0:
            return CookieStatus("bilibili", False, False, "")

        nav = data.get("data", {})
        uname = nav.get("uname", "")
        vip = nav.get("vip", {})
        vip_status = vip.get("status", 0)
        vip_type = vip.get("type", 0)
        is_vip = vip_status == 1 and vip_type >= 1

        return CookieStatus("bilibili", True, is_vip, uname)
    except Exception:
        return CookieStatus("bilibili", True, False, "")


class StreamResolver:
    """yt-dlp Python API based stream URL resolver for YouTube/Bilibili."""

    def __init__(self, cookie_file: str = "", cookie_browser: str = "edge"):
        self.cookie_file = cookie_file
        self.cookie_browser = cookie_browser

    def _build_opts(self, format_spec: str = "bv*+ba/b") -> dict:
        opts = {
            "format": format_spec,
            "format_sort": ["res", "abr", "vbr"],
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
        }
        if self.cookie_file:
            opts["cookiefile"] = fix_cookie_file(self.cookie_file)
        elif self.cookie_browser:
            opts["cookiesfrombrowser"] = (self.cookie_browser,)
        return opts

    def resolve(self, url: str) -> StreamInfo:
        opts = self._build_opts()
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            if "cookie" in str(e).lower():
                opts_no_cookie = dict(opts)
                opts_no_cookie.pop("cookiesfrombrowser", None)
                opts_no_cookie.pop("cookiefile", None)
                with yt_dlp.YoutubeDL(opts_no_cookie) as ydl:
                    info = ydl.extract_info(url, download=False)
                result = self._parse_info(info)
                result.cookie_failed = True
                return result
            raise
        return self._parse_info(info)

    def resolve_async(self, url: str, callback):
        """Resolve in background thread, call callback(StreamInfo) or callback(Exception)."""
        def _worker():
            try:
                stream_info = self.resolve(url)
                callback(stream_info)
            except Exception as e:
                callback(e)

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        return t

    def _parse_info(self, info: dict) -> StreamInfo:
        audio_url = ""
        video_url = ""
        audio_codec = ""
        audio_sr = None
        audio_br = None
        video_res = ""
        video_w = None
        video_h = None
        video_fps = None

        formats = info.get("requested_formats", [])
        if formats:
            for fmt in formats:
                if fmt.get("vcodec", "none") != "none" and fmt.get("acodec", "none") == "none":
                    video_url = fmt["url"]
                    video_w = fmt.get("width")
                    video_h = fmt.get("height")
                    video_fps = fmt.get("fps")
                    if video_w and video_h:
                        video_res = f"{video_w}×{video_h}"
                    elif video_h:
                        video_res = f"{video_h}p"
                elif fmt.get("vcodec", "none") != "none" and fmt.get("acodec", "none") != "none":
                    video_url = fmt["url"]
                    audio_url = fmt["url"]
                    video_w = fmt.get("width")
                    video_h = fmt.get("height")
                    video_fps = fmt.get("fps")
                    audio_codec = fmt.get("acodec", "")
                    audio_sr = fmt.get("asr")
                    audio_br = fmt.get("abr")
                    if video_w and video_h:
                        video_res = f"{video_w}×{video_h}"
                    elif video_h:
                        video_res = f"{video_h}p"
                elif fmt.get("acodec", "none") != "none":
                    audio_url = fmt["url"]
                    audio_codec = fmt.get("acodec", "")
                    audio_sr = fmt.get("asr")
                    audio_br = fmt.get("abr")
            if not video_url and not audio_url:
                video_url = info.get("url", "")
                audio_url = video_url
        else:
            video_url = info.get("url", "")
            audio_url = video_url
            audio_codec = info.get("acodec", "")
            audio_sr = info.get("asr")
            audio_br = info.get("abr")
            video_w = info.get("width")
            video_h = info.get("height")
            video_fps = info.get("fps")
            if video_w and video_h:
                video_res = f"{video_w}×{video_h}"
            elif video_h:
                video_res = f"{video_h}p"

        return StreamInfo(
            title=info.get("title", "Unknown"),
            url=info.get("webpage_url", ""),
            audio_url=audio_url,
            video_url=video_url,
            duration=info.get("duration"),
            audio_codec=audio_codec,
            audio_sample_rate=audio_sr,
            audio_bitrate=int(audio_br) if audio_br else None,
            thumbnail=info.get("thumbnail"),
            http_headers=info.get("http_headers"),
            video_resolution=video_res,
            video_width=video_w,
            video_height=video_h,
            video_fps=video_fps,
            is_live=bool(info.get("is_live", False)),
        )

    def get_best_audio_url(self, url: str) -> str:
        """Get only the best audio stream URL (for audio-only playback)."""
        opts = self._build_opts(format_spec="ba")
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        return info.get("url", "")

    def resolve_live_refresh(self, url: str, callback):
        """Re-resolve a live stream URL in background for stream refresh.

        callback receives StreamInfo on success, None on failure.
        """
        def _worker():
            try:
                stream_info = self.resolve(url)
                callback(stream_info)
            except Exception:
                callback(None)

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        return t
