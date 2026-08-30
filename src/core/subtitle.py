"""Subtitle generation pipeline: audio extraction → ASR → LLM refinement → SRT."""

import hashlib
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from .asr_engine import WhisperASR, KNOWN_MODELS
from .llm import LLMClient, LLMProvider

logger = logging.getLogger(__name__)

SUBTITLE_CACHE_DIR = Path.home() / ".ventiplayer" / "subtitles"
CHUNK_DURATION_S = 300  # 5 minutes per chunk for long videos


@dataclass
class SubtitleStatus:
    state: str  # "extracting", "transcribing", "refining", "done", "error"
    progress: float  # 0.0 - 1.0
    message: str
    srt_path: Optional[str] = None


REFINE_PROMPT_TEMPLATE = """\
你是一个视频字幕校对助手。以下是从视频音频中通过 ASR 识别出的字幕片段（每行一段，按时间顺序排列）。

请你：
1. 判断这个视频的类型（游戏直播、攻略讲解、生活vlog、美食分享、技术教程等）
2. 根据视频类型的语境，修正 ASR 识别错误（同音字、断句错误）
3. 添加标点符号
4. 去除口语填充词（嗯、啊、那个、就是说）
5. 保持每行对应一个原始片段，不要合并或拆分行数

原始识别结果（共 {n} 段）：
{segments}

请直接输出修正后的 {n} 行文本，不要输出其他内容。"""


def _format_srt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _generate_srt(segments: list[dict]) -> str:
    lines = []
    for i, seg in enumerate(segments, 1):
        lines.append(str(i))
        lines.append(f"{_format_srt_time(seg['start'])} --> {_format_srt_time(seg['end'])}")
        lines.append(seg["text"])
        lines.append("")
    return "\n".join(lines)


def extract_video_id(url: str) -> str:
    """Extract a stable ID from a video URL for caching."""
    bv_match = re.search(r"(BV[\w]+)", url, re.IGNORECASE)
    if bv_match:
        return bv_match.group(1)
    yt_match = re.search(r"(?:v=|youtu\.be/)([\w-]{11})", url)
    if yt_match:
        return yt_match.group(1)
    twitch_match = re.search(r"twitch\.tv/videos/(\d+)", url)
    if twitch_match:
        return f"twitch_{twitch_match.group(1)}"
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _extract_audio_from_url(audio_url: str, http_headers: Optional[dict] = None) -> np.ndarray:
    """Download and decode audio URL to 16kHz mono float32 numpy array."""
    import av

    options = {}
    if http_headers:
        full_headers = dict(http_headers)
        # yt-dlp 顶层 http_headers 对 B站 通常只带 Referer，不含 User-Agent，
        # 缺失时补浏览器 UA。否则 av.open 会用 FFmpeg 默认的 "Lavf" UA，
        # 触发 B站 CDN 反盗链返回 403。
        if "User-Agent" not in full_headers:
            full_headers["User-Agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        # User-Agent / Referer 用 FFmpeg 专用选项设置最可靠，其余头才放进
        # headers blob，避免同一个头同时出现在 blob 和专用选项里导致行为不一致。
        options["user_agent"] = full_headers["User-Agent"]
        if "Referer" in full_headers:
            options["referer"] = full_headers["Referer"]
        extra = {
            k: v for k, v in full_headers.items()
            if k not in ("User-Agent", "Referer")
        }
        if extra:
            options["headers"] = "".join(f"{k}: {v}\r\n" for k, v in extra.items())

    container = av.open(audio_url, options=options)
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)

    audio_stream = container.streams.audio[0]
    if audio_stream.duration and audio_stream.time_base:
        est_duration_s = float(audio_stream.duration * audio_stream.time_base)
        est_mem_mb = est_duration_s * 16000 * 4 / (1024 * 1024)
        if est_mem_mb > 200:
            logger.warning(
                "Long audio (%.0fs, ~%.0f MB RAM). Consider shorter clips for subtitle generation.",
                est_duration_s, est_mem_mb,
            )

    frames = []
    for frame in container.decode(audio=0):
        resampled = resampler.resample(frame)
        for r in resampled:
            arr = r.to_ndarray().flatten()
            frames.append(arr)

    container.close()

    if not frames:
        raise RuntimeError("No audio frames decoded")

    audio_int16 = np.concatenate(frames)
    return audio_int16.astype(np.float32) / 32768.0


class SubtitlePipeline:
    """Orchestrates subtitle generation in a background thread."""

    def __init__(
        self,
        model_id: str,
        llm_provider: LLMProvider,
        progress_callback: Callable[[SubtitleStatus], None],
    ):
        self._model_id = model_id
        self._llm_provider = llm_provider
        self._progress_cb = progress_callback
        self._asr: Optional[WhisperASR] = None
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def generate(
        self,
        audio_url: str,
        video_url: str,
        language: str = "zh",
        http_headers: Optional[dict] = None,
    ):
        """Start subtitle generation in a background thread."""
        self._cancel.clear()
        self._thread = threading.Thread(
            target=self._worker,
            args=(audio_url, video_url, language, http_headers),
            daemon=True,
        )
        self._thread.start()

    def cancel(self):
        self._cancel.set()

    def _report(self, state: str, progress: float, message: str, srt_path: str = None):
        self._progress_cb(SubtitleStatus(state, progress, message, srt_path))

    def _worker(
        self,
        audio_url: str,
        video_url: str,
        language: str,
        http_headers: Optional[dict],
    ):
        try:
            video_id = extract_video_id(video_url)
            cache_path = SUBTITLE_CACHE_DIR / f"{video_id}_{language}.srt"

            if cache_path.exists():
                self._report("done", 1.0, "字幕已缓存", str(cache_path))
                return

            # Step 1: Extract audio
            self._report("extracting", 0.05, "正在提取音频...")
            audio = _extract_audio_from_url(audio_url, http_headers)
            if self._cancel.is_set():
                return

            total_duration = len(audio) / 16000
            self._report("extracting", 0.15, f"音频提取完成 ({total_duration:.0f}s)")

            # Step 2: Transcribe with Whisper
            self._report("transcribing", 0.20, "正在加载 ASR 模型...")
            if not self._asr:
                self._asr = WhisperASR(model_id=self._model_id)
            if not self._asr.is_loaded:
                self._asr.load()
            if self._cancel.is_set():
                return

            self._report("transcribing", 0.25, "正在识别语音...")
            segments = self._transcribe_chunked(audio, language, total_duration)
            if self._cancel.is_set():
                return

            if not segments:
                self._report("error", 0.0, "未识别到任何语音内容")
                return

            self._report("transcribing", 0.65, f"识别完成，共 {len(segments)} 段")

            # Step 3: LLM refinement
            self._report("refining", 0.70, "正在优化字幕文本...")
            segments = self._refine_with_llm(segments)
            if self._cancel.is_set():
                return

            # Step 4: Generate SRT
            self._report("refining", 0.95, "正在生成字幕文件...")
            srt_content = _generate_srt(segments)
            SUBTITLE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(srt_content, encoding="utf-8")

            self._report("done", 1.0, "字幕生成完成", str(cache_path))

        except Exception as e:
            logger.exception("Subtitle generation failed")
            self._report("error", 0.0, f"字幕生成失败: {e}")

    def _transcribe_chunked(
        self, audio: np.ndarray, language: str, total_duration: float
    ) -> list[dict]:
        """Transcribe audio in chunks of CHUNK_DURATION_S to manage memory."""
        chunk_samples = CHUNK_DURATION_S * 16000
        segments = []

        if total_duration <= CHUNK_DURATION_S:
            return self._asr.transcribe(audio, sample_rate=16000, language=language)

        offset = 0
        chunk_idx = 0
        while offset < len(audio):
            if self._cancel.is_set():
                return segments
            chunk = audio[offset : offset + chunk_samples]
            time_offset = offset / 16000

            chunk_segs = self._asr.transcribe(chunk, sample_rate=16000, language=language)
            for seg in chunk_segs:
                seg["start"] += time_offset
                seg["end"] += time_offset
                segments.append(seg)

            offset += chunk_samples
            chunk_idx += 1
            progress = 0.25 + 0.40 * min(offset / len(audio), 1.0)
            self._report(
                "transcribing",
                progress,
                f"正在识别语音... ({time_offset + len(chunk) / 16000:.0f}/{total_duration:.0f}s)",
            )

        return segments

    def _refine_with_llm(self, segments: list[dict]) -> list[dict]:
        """Send segments to LLM for refinement. Falls back to raw on failure."""
        try:
            client = LLMClient(self._llm_provider)
            raw_texts = [seg["text"] for seg in segments]

            # Batch into groups to avoid exceeding token limits
            batch_size = 80
            refined_texts = []

            for i in range(0, len(raw_texts), batch_size):
                if self._cancel.is_set():
                    return segments
                batch = raw_texts[i : i + batch_size]
                prompt = REFINE_PROMPT_TEMPLATE.format(
                    n=len(batch), segments="\n".join(batch)
                )
                response = client.call(prompt)
                lines = [l.strip() for l in response.strip().split("\n") if l.strip()]

                if len(lines) == len(batch):
                    refined_texts.extend(lines)
                else:
                    logger.warning(
                        f"LLM returned {len(lines)} lines, expected {len(batch)}. Using raw."
                    )
                    refined_texts.extend(batch)

                progress = 0.70 + 0.20 * min((i + batch_size) / len(raw_texts), 1.0)
                self._report("refining", progress, f"正在优化字幕... ({min(i + batch_size, len(raw_texts))}/{len(raw_texts)})")

            for seg, text in zip(segments, refined_texts):
                seg["text"] = text

        except Exception as e:
            logger.warning(f"LLM refinement failed, using raw ASR output: {e}")
            self._report("refining", 0.90, f"LLM 不可用，使用原始识别结果")

        return segments
