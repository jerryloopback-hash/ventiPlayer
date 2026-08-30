"""导出混流：烘焙出的 video-only mp4 + 增强 WAV → 最终 mp4（视频 remux + AAC 编码）。"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class MuxMixin:
    """VideoExporter 的混流子管线。"""

    # ─── (c) 混流：烘焙视频 + 增强音频 → 最终 mp4 ───────────────────────

    def _mux(self, video_path: str, audio_wav: str, audio_sr: int, out_path: str):
        """把 video-only mp4 的 H.264 流原样 remux + AAC 编码音频 → 最终 mp4。"""
        import av

        out = av.open(out_path, mode="w")
        vin = av.open(video_path)
        ain = av.open(audio_wav)
        try:
            in_vstream = vin.streams.video[0]
            # 视频：直接 remux（不重编码，无损、快）
            out_vstream = out.add_stream_from_template(in_vstream)

            # 音频：编码 AAC
            out_astream = out.add_stream("aac", rate=audio_sr)
            try:
                in_achannels = ain.streams.audio[0].channels or 2
            except Exception:
                in_achannels = 2
            try:
                out_astream.codec_context.layout = "stereo" if in_achannels >= 2 else "mono"
            except Exception:
                pass

            # 先写视频包
            for pkt in vin.demux(in_vstream):
                if pkt.dts is None:
                    continue
                pkt.stream = out_vstream
                out.mux(pkt)

            # 再编码音频
            in_astream = ain.streams.audio[0]
            for frame in ain.decode(in_astream):
                frame.pts = None
                for pkt in out_astream.encode(frame):
                    out.mux(pkt)
            for pkt in out_astream.encode():
                out.mux(pkt)
        finally:
            try:
                out.close()
            except Exception:
                pass
            vin.close()
            ain.close()






