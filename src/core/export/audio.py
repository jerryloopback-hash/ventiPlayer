"""导出音频管线：解码整轨 → Apollo/FlashSR 离线增强（或直通）→ 16-bit PCM WAV。"""

from __future__ import annotations

import logging
import wave
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


class AudioPipelineMixin:
    """VideoExporter 的音频子管线。宿主需提供 _report/_check_cancel/_tmp_path/
    _enhancer 属性（见 video_export.VideoExporter）。"""

    def _decode_full_audio(self, audio_url: str, http_headers: Optional[dict]) -> tuple:
        """用 PyAV 解码整轨音频为 (channels, samples) float32 + 源采样率。

        复刻 audio_pipe._decode_full_audio：PyAV>=14 移除了 Frame.to_ndarray(format=)，
        故用 AudioResampler(format='fltp') 把每帧规范成 planar float32（声道在前）。"""
        import av
        from av.audio.resampler import AudioResampler

        options = {}
        if http_headers:
            full = dict(http_headers)
            full.setdefault("User-Agent",
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0.0.0 Safari/537.36")
            options["user_agent"] = full["User-Agent"]
            if "Referer" in full:
                options["referer"] = full["Referer"]

        container = av.open(audio_url, options=options)
        try:
            audio_stream = container.streams.audio[0]
            sample_rate = audio_stream.rate
            resampler = AudioResampler(format="fltp")
            frames = []

            def _collect(av_frames):
                for rf in av_frames:
                    arr = rf.to_ndarray()
                    if arr.ndim == 1:
                        arr = arr[np.newaxis, :]
                    frames.append(arr.astype(np.float32))

            for packet in container.demux(audio_stream):
                self._check_cancel()
                for frame in packet.decode():
                    _collect(resampler.resample(frame))
            _collect(resampler.resample(None))
        finally:
            container.close()

        if not frames:
            return None, 0
        nch = max(f.shape[0] for f in frames)
        aligned = []
        for f in frames:
            if f.shape[0] < nch:
                f = np.repeat(f, nch // f.shape[0], axis=0)[:nch]
            aligned.append(f)
        return np.concatenate(aligned, axis=1), sample_rate

    def _prepare_audio(self, audio_url: str, http_headers: Optional[dict],
                       es) -> tuple:
        """产出本地增强(或直通)的 WAV，返回 (wav_path, output_sr)。

        若启用了任一音频模型则跑 Apollo/FlashSR 离线链；否则直通源音频。
        模型加载失败时不致命——退回直通，保证视频导出仍可完成。"""
        self._report(0.02, "解码音频流...")
        audio, src_sr = self._decode_full_audio(audio_url, http_headers)
        if audio is None:
            raise RuntimeError("音频解码失败（无音频流或解码为空）")

        out_sr = src_sr
        enhanced = audio

        if es.any_audio_enabled:
            self._report(0.05, "加载音频增强模型...")
            self._enhancer.set_apollo_enabled(es.apollo_enabled)
            self._enhancer.set_flashsr_enabled(es.flashsr_enabled)
            self._enhancer.set_apollo_fp16(es.apollo_fp16)
            self._enhancer.set_flashsr_fp16(es.flashsr_fp16)
            if self._enhancer.load_models():
                def _cb(p):
                    self._check_cancel()
                    self._report(0.05 + p * 0.20, f"音频增强中... {int(p * 100)}%")
                try:
                    enhanced, out_sr = self._enhancer.enhance_full(audio, src_sr,
                                                                   progress_callback=_cb)
                except InterruptedError:
                    raise
                except Exception as e:
                    logger.warning("音频增强失败，退回原始音频: %s", e)
                    enhanced, out_sr = audio, src_sr
            else:
                logger.warning("音频模型加载失败，退回原始音频")
                enhanced, out_sr = audio, src_sr

        wav_path = self._tmp_path(es.output_path, "_audio.wav")
        self._write_wav(wav_path, enhanced, out_sr)
        self._report(0.27, "音频准备完成")
        return wav_path, out_sr

    @staticmethod
    def _write_wav(path: str, audio: np.ndarray, sr: int):
        """把 (channels, samples) float32 写为 16-bit PCM WAV（标准库 wave，无额外依赖）。"""
        if audio.ndim == 1:
            audio = audio[np.newaxis, :]
        nch = audio.shape[0]
        # 限幅到 [-1,1] 后转 int16，(channels, samples) → 交织 (samples, channels)
        clipped = np.clip(audio, -1.0, 1.0)
        interleaved = (clipped.T * 32767.0).astype("<i2")
        with wave.open(path, "wb") as wf:
            wf.setnchannels(nch)
            wf.setsampwidth(2)
            wf.setframerate(int(sr))
            wf.writeframes(interleaved.tobytes())
