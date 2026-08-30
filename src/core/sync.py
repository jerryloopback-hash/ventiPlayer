"""Audio-video synchronization manager.

Handles PTS alignment between enhanced audio and video playback,
dynamic buffer management, seek recovery, and seamless enhance toggle.

Design philosophy: mpv handles A/V sync internally. This manager only
applies gentle speed corrections when drift is persistent and large.
It NEVER seeks when the current position is within the enhanced audio
range — seeking a growing WAV file causes mpv to re-read the header
and reset audio_pts, creating a feedback loop.
"""

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class SyncState(Enum):
    IDLE = "idle"
    SYNCED = "synced"
    CORRECTING = "correcting"
    FALLBACK = "fallback"


@dataclass
class SyncStatus:
    state: SyncState = SyncState.IDLE
    drift_ms: float = 0.0
    enhanced_active: bool = False
    fallback_reason: str = ""


# Drift threshold for soft correction (speed adjustment)
SOFT_DRIFT_MS = 50.0
# How often to check sync (seconds)
SYNC_CHECK_INTERVAL = 1.0
# Number of consecutive drift readings before acting
DRIFT_CONFIRM_COUNT = 3
# Frame-gen 过渡期：vf 链重建后 video_pos 推进恢复平稳所需的缓冲时间（秒）
FRAME_GEN_SETTLE_S = 4.0


class SyncManager:
    """Manages audio-video synchronization for enhanced audio playback.

    Strategy:
    - mpv handles A/V sync internally — it's good at this
    - We NEVER seek when video_pos is within the enhanced audio range
    - We only apply gentle speed corrections for persistent, confirmed drift
    - Seeking a growing WAV causes mpv to re-read the header and reset
      audio_pts, creating a feedback loop — so we avoid it entirely
    """

    def __init__(self):
        self._state = SyncState.IDLE
        self._lock = threading.Lock()
        self._enhanced_active = False
        self._original_audio_url: Optional[str] = None
        self._enhanced_audio_path: Optional[str] = None
        self._video_position_fn: Optional[Callable[[], float]] = None
        self._audio_position_fn: Optional[Callable[[], float]] = None
        self._seek_fn: Optional[Callable[[float], None]] = None
        self._switch_audio_fn: Optional[Callable[[str], None]] = None
        self._set_speed_fn: Optional[Callable[[float], None]] = None
        self._get_speed_fn: Optional[Callable[[], float]] = None
        self._status_callback: Optional[Callable[[SyncStatus], None]] = None

        self._base_speed = 1.0
        self._correction_active = False
        self._last_switch_time = 0.0
        self._switch_cooldown = 8.0  # seconds after audio switch before checking drift
        self._last_seek_time = 0.0
        self._seek_cooldown = 3.0  # seconds after seek before checking drift
        self._last_resume_time = 0.0
        self._resume_cooldown = 3.0
        self._enhanced_duration_s = 0.0
        self._drift_history: list[float] = []  # recent drift readings for confirmation

        # 帧生成 (RIFE 真插帧) 状态：vf 链重建过渡期内抑制漂移确认
        self._frame_gen_active = False
        self._frame_gen_multiplier = 1.0
        self._frame_gen_settle_until = 0.0  # monotonic 时间戳

        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def configure(self, *,
                  video_position_fn: Callable[[], float],
                  audio_position_fn: Callable[[], float],
                  seek_fn: Callable[[float], None],
                  switch_audio_fn: Callable[[str], None],
                  set_speed_fn: Callable[[float], None],
                  get_speed_fn: Callable[[], float],
                  status_callback: Optional[Callable[[SyncStatus], None]] = None):
        """Wire up the sync manager to player functions."""
        self._video_position_fn = video_position_fn
        self._audio_position_fn = audio_position_fn
        self._seek_fn = seek_fn
        self._switch_audio_fn = switch_audio_fn
        self._set_speed_fn = set_speed_fn
        self._get_speed_fn = get_speed_fn
        self._status_callback = status_callback

    def set_original_audio(self, url: str):
        """Store the original audio URL for fallback."""
        self._original_audio_url = url

    def activate_enhanced(self, enhanced_path: str, position_at_switch: float = 0.0):
        """Switch to enhanced audio and start sync monitoring."""
        with self._lock:
            self._enhanced_audio_path = enhanced_path
            self._enhanced_active = True
            self._state = SyncState.SYNCED
            self._last_switch_time = time.monotonic()
            self._last_seek_time = time.monotonic()
            self._drift_history.clear()
            self._correction_active = False

        if self._switch_audio_fn:
            self._switch_audio_fn(enhanced_path)

        self._start_monitor()
        self._emit_status()
        logger.info(f"Enhanced audio activated at position {position_at_switch:.1f}s")

    def deactivate_enhanced(self):
        """Switch back to original audio."""
        self._stop_monitor()
        with self._lock:
            self._enhanced_active = False
            self._state = SyncState.IDLE
            self._correction_active = False
            self._drift_history.clear()

        if self._original_audio_url and self._switch_audio_fn:
            self._switch_audio_fn(self._original_audio_url)

        self._restore_speed()
        self._emit_status()
        logger.info("Switched back to original audio")

    def notify_seek(self, target_position: float):
        """Called when user seeks. Records timing to suppress drift checks."""
        with self._lock:
            self._last_seek_time = time.monotonic()
            self._drift_history.clear()
            if self._correction_active:
                self._correction_active = False
                self._restore_speed()

    def notify_resume(self):
        """Called when playback resumes (unpause). Suppresses drift checks briefly."""
        with self._lock:
            self._last_resume_time = time.monotonic()
            self._drift_history.clear()

    def update_enhanced_duration(self, duration_s: float):
        """Update how many seconds of enhanced audio are currently available."""
        with self._lock:
            self._enhanced_duration_s = duration_s

    def notify_speed_change(self, speed: float):
        """Called when user changes playback speed."""
        self._base_speed = speed

    def set_frame_gen_active(self, active: bool, multiplier: float = 1.0):
        """通知同步管理器 RIFE 真插帧的启停（display-resample 不调用本方法）。

        active=True 时 vf 链刚重建，video_pos 在过渡期内推进不稳，需短暂抑制漂移
        确认并清空历史，避免把 vf 重建抖动误读为 A/V 漂移。稳态判据不变（插帧不改变
        PTS 时长，稳态漂移仍应为 0）。multiplier 仅用于状态记录/日志。
        """
        with self._lock:
            self._frame_gen_active = active
            self._frame_gen_multiplier = multiplier if active else 1.0
            # 进入/退出插帧都视作一次时序扰动：清空历史并开启过渡抑制窗口
            self._drift_history.clear()
            self._frame_gen_settle_until = time.monotonic() + FRAME_GEN_SETTLE_S
            if self._correction_active:
                self._correction_active = False
                self._restore_speed()
        logger.info("Frame-gen sync adapt: active=%s, multiplier=%.2fx", active, multiplier)

    def fallback_to_original(self, reason: str = ""):
        """Emergency fallback to original audio."""
        logger.warning(f"Falling back to original audio: {reason}")
        self._stop_monitor()
        with self._lock:
            self._state = SyncState.FALLBACK
            self._enhanced_active = False
            self._correction_active = False
            self._drift_history.clear()

        if self._original_audio_url and self._switch_audio_fn:
            self._switch_audio_fn(self._original_audio_url)

        self._restore_speed()
        self._emit_status(fallback_reason=reason)

    @property
    def is_enhanced_active(self) -> bool:
        return self._enhanced_active

    @property
    def state(self) -> SyncState:
        return self._state

    def cleanup(self):
        self._stop_monitor()

    def _start_monitor(self):
        """Start the sync monitoring thread."""
        self._stop_monitor()
        self._stop_event.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, daemon=True
        )
        self._monitor_thread.start()

    def _stop_monitor(self):
        self._stop_event.set()
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=1.0)
        self._monitor_thread = None

    def _monitor_loop(self):
        """Periodically check A/V drift and apply gentle corrections only."""
        while not self._stop_event.is_set():
            self._stop_event.wait(SYNC_CHECK_INTERVAL)
            if self._stop_event.is_set():
                break

            with self._lock:
                if not self._enhanced_active:
                    break
                now = time.monotonic()
                if now - self._last_seek_time < self._seek_cooldown:
                    continue
                if now - self._last_switch_time < self._switch_cooldown:
                    continue
                if now - self._last_resume_time < self._resume_cooldown:
                    continue
                if now < self._frame_gen_settle_until:  # 插帧 vf 重建过渡期
                    continue

            try:
                self._check_drift()
            except Exception as e:
                logger.warning(f"Sync check error: {e}")

    def _check_drift(self):
        """Measure A/V drift and apply ONLY gentle speed corrections.

        Key rule: NEVER seek when video_pos is within the enhanced audio range.
        mpv handles A/V sync internally. We only nudge speed if drift is
        persistent and confirmed over multiple readings.
        """
        if not self._video_position_fn or not self._audio_position_fn:
            return

        try:
            video_pos = self._video_position_fn()
            audio_pos = self._audio_position_fn()
        except Exception:
            return

        if video_pos is None or audio_pos is None:
            return
        if video_pos <= 0 or audio_pos <= 0:
            return

        # If video position is within the enhanced audio range, mpv can handle
        # sync internally. We only apply gentle speed corrections for persistent drift.
        with self._lock:
            frontier = self._enhanced_duration_s

        # Near the write frontier — mpv reads erratic data, skip entirely
        if frontier > 0 and video_pos > frontier - 5.0:
            if self._correction_active:
                self._correction_active = False
                self._restore_speed()
            return

        drift_ms = (audio_pos - video_pos) * 1000.0

        target_speed = None
        avg_drift = 0.0

        # Record drift reading for confirmation
        with self._lock:
            self._drift_history.append(drift_ms)
            # Keep only recent readings
            if len(self._drift_history) > DRIFT_CONFIRM_COUNT:
                self._drift_history = self._drift_history[-DRIFT_CONFIRM_COUNT:]

            # Only act if we have enough confirmed readings all showing drift
            # in the same direction and above threshold
            if len(self._drift_history) < DRIFT_CONFIRM_COUNT:
                return

            all_above = all(abs(d) > SOFT_DRIFT_MS for d in self._drift_history)
            same_direction = (
                all(d > 0 for d in self._drift_history) or
                all(d < 0 for d in self._drift_history)
            )

            if all_above and same_direction:
                avg_drift = sum(self._drift_history) / len(self._drift_history)
                # Apply gentle speed correction (1% adjustment)
                if not self._correction_active:
                    self._correction_active = True
                correction = 1.0 + (0.01 if avg_drift < 0 else -0.01)
                target_speed = self._base_speed * correction
                self._state = SyncState.CORRECTING
            elif self._correction_active:
                # Drift resolved — restore normal speed
                self._correction_active = False
                self._drift_history.clear()
                self._state = SyncState.SYNCED
                self._restore_speed()
                self._emit_status()
                return
            else:
                return

        # Apply speed correction outside lock
        if target_speed is not None and self._set_speed_fn:
            self._set_speed_fn(target_speed)
            logger.debug(f"Soft correction: avg_drift={avg_drift:.0f}ms, speed={target_speed:.3f}")

        self._emit_status()

    def _restore_speed(self):
        if self._set_speed_fn:
            self._set_speed_fn(self._base_speed)

    def _emit_status(self, fallback_reason: str = ""):
        if self._status_callback:
            status = SyncStatus(
                state=self._state,
                drift_ms=0.0,
                enhanced_active=self._enhanced_active,
                fallback_reason=fallback_reason,
            )
            self._status_callback(status)
