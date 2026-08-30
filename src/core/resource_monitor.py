"""Real-time GPU/CPU and VRAM/RAM resource monitor.

Uses AMD ADL (AMD Display Library) via atiadlxx.dll for GPU monitoring
on AMD GPUs. Falls back to psutil CPU/RAM if ADL is unavailable.
"""

import ctypes
import logging
from dataclasses import dataclass
from typing import Optional

from src.core.enhancer import Backend

logger = logging.getLogger(__name__)


@dataclass
class ResourceStats:
    """Snapshot of resource utilization."""
    utilization_pct: float
    memory_used_gb: float
    memory_total_gb: float
    is_gpu: bool


ADL_MAIN_MALLOC_CALLBACK = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_int)

_adl_buffers: list = []


@ADL_MAIN_MALLOC_CALLBACK
def _adl_malloc_safe(size):
    buf = (ctypes.c_byte * size)()
    _adl_buffers.append(buf)
    return ctypes.addressof(buf)


class _ADLMonitor:
    """GPU monitoring via AMD Display Library (atiadlxx.dll)."""

    # PMLog sensor indices
    _SENSOR_GFX_ACTIVITY = 19
    _SENSOR_MEM_ACTIVITY = 20

    def __init__(self):
        self._adl = None
        self._context = ctypes.c_void_p()
        self._adapter_idx = 0
        self._total_vram_gb = 0.0
        self._available = False

        try:
            self._adl = ctypes.CDLL("atiadlxx.dll")
        except OSError:
            logger.debug("atiadlxx.dll not found")
            return

        try:
            status = self._adl.ADL2_Main_Control_Create(
                _adl_malloc_safe, 1, ctypes.byref(self._context)
            )
            if status != 0:
                logger.debug(f"ADL2_Main_Control_Create failed: {status}")
                return

            self._find_adapter()
            if self._total_vram_gb > 0:
                self._available = True
                logger.info(
                    f"ADL GPU monitor active — adapter {self._adapter_idx}, "
                    f"VRAM {self._total_vram_gb:.1f}GB"
                )
        except Exception as e:
            logger.debug(f"ADL init failed: {e}")

    def _find_adapter(self):
        """Find the discrete GPU adapter and get its VRAM size."""
        # Get VRAM info — all adapters report the same for single-GPU systems
        class ADLMemoryInfoX4(ctypes.Structure):
            _fields_ = [
                ("iMemorySize", ctypes.c_longlong),
                ("strMemoryType", ctypes.c_char * 256),
                ("iMemoryBandwidth", ctypes.c_longlong),
                ("iHyperMemorySize", ctypes.c_longlong),
                ("iInvisibleMemorySize", ctypes.c_longlong),
                ("iVisibleMemorySize", ctypes.c_longlong),
                ("iVramVendorRevId", ctypes.c_longlong),
            ]

        mem_info = ADLMemoryInfoX4()
        status = self._adl.ADL2_Adapter_MemoryInfoX4_Get(
            self._context, 0, ctypes.byref(mem_info)
        )
        if status == 0 and mem_info.iMemorySize > 0:
            self._total_vram_gb = mem_info.iMemorySize / (1024 ** 3)
        else:
            self._total_vram_gb = 16.0

    @property
    def available(self) -> bool:
        return self._available

    @property
    def total_vram_gb(self) -> float:
        return self._total_vram_gb

    def get_utilization(self) -> float:
        """Get GPU utilization percentage via PMLog."""
        if not self._available:
            return 0.0
        try:
            pmlog = (ctypes.c_uint * 1024)()
            pmlog[0] = ctypes.sizeof(pmlog)
            status = self._adl.ADL2_New_QueryPMLogData_Get(
                self._context, self._adapter_idx, ctypes.byref(pmlog)
            )
            if status == 0:
                offset = 1 + self._SENSOR_GFX_ACTIVITY * 2
                if offset + 1 >= 1024:
                    return 0.0
                valid = pmlog[offset]
                value = pmlog[offset + 1]
                if valid and 0 <= value <= 100:
                    return float(value)
        except (OSError, ValueError, IndexError) as e:
            logger.debug(f"ADL PMLog query failed: {e}")
        return 0.0

    def get_vram_used_gb(self) -> float:
        """Get dedicated VRAM usage in GB."""
        if not self._available:
            return 0.0
        try:
            usage_mb = ctypes.c_int()
            status = self._adl.ADL2_Adapter_DedicatedVRAMUsage_Get(
                self._context, self._adapter_idx, ctypes.byref(usage_mb)
            )
            if status == 0:
                return usage_mb.value / 1024.0
        except Exception as e:
            logger.debug(f"ADL VRAM usage query failed: {e}")
        return 0.0

    def close(self):
        if self._adl and self._context:
            try:
                self._adl.ADL2_Main_Control_Destroy(self._context)
            except (OSError, Exception):
                pass
            self._context = ctypes.c_void_p()
            self._available = False


class ResourceMonitor:
    """Monitors GPU or CPU utilization.

    For AMD GPUs: uses ADL (AMD Display Library) for utilization and VRAM.
    Falls back to psutil CPU/RAM if ADL is unavailable.
    """

    def __init__(self, backend: Backend):
        self._backend = backend
        self._gpu_mode = False
        self._adl_monitor: Optional[_ADLMonitor] = None
        self._psutil_available = False

        try:
            import psutil  # noqa: F401
            self._psutil_available = True
        except ImportError:
            logger.warning("psutil not available — CPU/RAM monitoring disabled")

        if backend in (Backend.ROCM,):
            self._init_gpu_monitoring()

    def _init_gpu_monitoring(self):
        """Initialize ADL-based GPU monitoring."""
        self._adl_monitor = _ADLMonitor()
        if self._adl_monitor.available:
            self._gpu_mode = True
        else:
            logger.info("ADL GPU monitoring unavailable, falling back to CPU mode")
            self._adl_monitor = None

    @property
    def is_gpu_mode(self) -> bool:
        return self._gpu_mode

    def get_stats(self) -> Optional[ResourceStats]:
        if self._gpu_mode and self._adl_monitor:
            return self._get_gpu_stats()
        return self._get_cpu_stats()

    def _get_gpu_stats(self) -> Optional[ResourceStats]:
        util = self._adl_monitor.get_utilization()
        vram_used = self._adl_monitor.get_vram_used_gb()
        vram_total = self._adl_monitor.total_vram_gb
        return ResourceStats(
            utilization_pct=util,
            memory_used_gb=vram_used,
            memory_total_gb=vram_total,
            is_gpu=True,
        )

    def _get_cpu_stats(self) -> Optional[ResourceStats]:
        if not self._psutil_available:
            return None
        try:
            import psutil
            cpu_pct = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory()
            return ResourceStats(
                utilization_pct=cpu_pct,
                memory_used_gb=mem.used / (1024 ** 3),
                memory_total_gb=mem.total / (1024 ** 3),
                is_gpu=False,
            )
        except Exception as e:
            logger.debug(f"CPU stats failed: {e}")
            return None

    def format_stats(self) -> str:
        stats = self.get_stats()
        if stats is None:
            return ""
        if stats.is_gpu:
            return (
                f"GPU: {stats.utilization_pct:.0f}% | "
                f"VRAM: {stats.memory_used_gb:.1f}/{stats.memory_total_gb:.1f} GB"
            )
        return (
            f"CPU: {stats.utilization_pct:.0f}% | "
            f"RAM: {stats.memory_used_gb:.1f}/{stats.memory_total_gb:.1f} GB"
        )

    def close(self):
        if self._adl_monitor:
            self._adl_monitor.close()
            self._adl_monitor = None
