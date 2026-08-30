# VentiPlayer Debug & Cleanup Plan

## 架构概览

```
run.py (入口, Python 3.12 检查)
  └── src/main.py (env setup, splash, QApplication)
        └── MainWindow (GUI主窗口)
              ├── MpvPlayerWidget (mpv嵌入播放)
              ├── EnhancePanel (增强控制面板)
              ├── StreamResolver (yt-dlp流解析)
              ├── Enhancer (AI推理抽象层)
              │     ├── FastWaveModel (实时模式)
              │     └── AudioSRModel (精听模式)
              ├── AudioPipeline (解码→增强→输出)
              └── SyncManager (音视频同步)
```

**数据流**: URL → yt-dlp解析 → mpv播放视频+原始音频 → 用户点击增强 → PyAV解码音频流 → AI推理 → 写入WAV → mpv切换到增强音频文件 → SyncManager监控A/V drift

---

## 发现的问题

### 🔴 Bugs (会导致运行时错误或功能异常)

#### B1: `audio_pipe.py` 实时模式每个chunk都重写整个WAV文件
- **位置**: `src/core/audio_pipe.py:292-293`
- **问题**: 每处理一个chunk就 `np.concatenate(enhanced_chunks)` + `sf.write()` 全量重写。O(n²) 时间复杂度，且长音频时会严重拖慢。如果 mpv 正在读取该文件，可能导致播放中断或损坏。
- **修复**: 使用 `soundfile.SoundFile` 的 append 模式，或分离"正在写"和"可播放"文件（双缓冲）。

#### B2: `enhancer.py` 中 MIOpen cache 设置重复且位置不当
- **位置**: `src/core/enhancer.py:20-23` 和 `src/main.py:12-14`
- **问题**: 同样的 env vars 在两处设置。`enhancer.py` 是模块级副作用（import 时执行），不可控。
- **修复**: 只在 `src/main.py` 入口统一设置，`enhancer.py` 删除。

#### B3: `AudioSRModel.enhance()` 无条件调用 `torch.cuda.empty_cache()` 即使设备是 CPU
- **位置**: `src/models/audiosr_model.py:244, 269`
- **问题**: 当 `self._device == "cpu"` 时仍然调用 `torch.cuda.empty_cache()`，若 torch 没有 CUDA 支持会报错。
- **修复**: 加 `if self._device == "cuda":` 守卫。

#### B4: `_check_bilibili_vip` 解析 nav API 字段名错误
- **位置**: `src/core/stream.py:134-135`
- **问题**: B站 nav API 返回的 VIP 信息在 `data.vipStatus` / `data.vipType`，但实际 API 结构是 `data.vip.status` / `data.vip.type`（嵌套在 vip 对象中）。代码用 `nav.get("vipStatus")` 会始终返回 0/None，大会员检测永远失败。
- **修复**: 改为 `vip_info = nav.get("vip", {})`, `status = vip_info.get("status", 0)`, `vip_type = vip_info.get("type", 0)`.

#### B5: `main_window.py` 的 `closeEvent` 没有调用 `event.accept()`
- **位置**: `src/gui/main_window.py:710-714`
- **问题**: Qt 的 closeEvent 需要调用 `event.accept()` 或 `super().closeEvent(event)` 才能正确关闭窗口。当前实现可能导致窗口无法正常关闭或资源泄漏。
- **修复**: 末尾加 `event.accept()` 或 `super().closeEvent(event)`。

#### B6: `player_widget.py` `play_av` 设置 `audio_files` 可能时序问题
- **位置**: `src/gui/player_widget.py:121-122`
- **问题**: `self._player.play(video_url)` 是异步的，紧接着 `self._player.audio_files = [audio_url]` 可能在 play 还没准备好时设置，导致音频轨道不生效。
- **修复**: 改用 mpv 的 `--audio-file` 选项，或在 play 前设置 `audio_files`。

#### B7: `SyncManager._check_drift` 在 lock 内部调用可能阻塞的外部函数
- **位置**: `src/core/sync.py:229`
- **问题**: 在 `with self._lock:` 块内调用 `self._seek_fn(video_pos)`，这会调用 mpv seek，可能阻塞。如果主线程同时尝试获取 lock（如 `notify_seek`），会死锁。
- **修复**: 在 lock 外部执行 seek 操作，只在 lock 内部更新状态。

#### B8: `FastWaveModel.enhance` 短音频 Hann window 衰减
- **位置**: `src/models/fastwave.py:398`
- **问题**: `hop = chunk_size // 2`，如果音频长度 < chunk_size，pad 后只有一个 chunk 但没有 overlap，输出边缘会有 Hann window 衰减（首尾静音）。
- **修复**: 对短音频（< chunk_size）跳过 overlap-add，直接单次推理不加窗。

---

### 🟡 逻辑问题 (不会崩溃但行为不符预期)

#### L1: `EnhancePanel.set_enhance_blocked` 阈值逻辑过于简单
- **位置**: `src/gui/main_window.py:422`
- **问题**: `if stream.audio_sample_rate >= 48000` 就 block 增强。但 48kHz 的有损压缩音频（如 Opus 48kHz 128kbps）实际带宽截止在 ~20kHz，仍然可以受益于超分。应该检查的是"有效带宽"而非采样率。
- **建议**: 改为基于估算的 cutoff vs target_sr/2 比较，或者仅对无损 ≥48kHz 才 block。

#### L2: 实时模式并非"实时" — 实际是后台异步增强
- **位置**: `src/core/audio_pipe.py:224-338`
- **问题**: `_realtime_worker` 是流式解码+逐块增强，但用户要等到 10s buffer 才切换到增强音频（main_window.py:849），并非 PLAN.md 描述的"启动延迟 15s"体验。实际上是"先播原始音频，后台增强完一段后切换"。
- **建议**: 接受当前实现是"后台异步增强"，UI 文案和 PLAN.md 应同步更新。

#### L3: `SyncManager` 的 drift 计算基准可能不一致
- **位置**: `src/core/sync.py:220`
- **问题**: `audio_pos` 来自 `mpv.audio_pts`，`video_pos` 来自 `mpv.time_pos`。当使用 `audio-add` 切换音频后，两者的时间基准可能不同。
- **建议**: 验证 mpv 在 audio-add 后两个属性的实际行为。

#### L4: 重新启用增强不会自动切换回已有的增强音频
- **位置**: `src/gui/main_window.py:720-725`
- **问题**: 用户取消勾选"启用增强"会切回原始音频，但重新勾选不会自动切换回增强音频（如果已经增强完成）。
- **建议**: 如果增强文件已存在且覆盖当前播放位置，重新启用时直接切换。

---

### 🟢 冗余 / 代码清理

#### C1: 环境变量设置三处重复
- `MIOPEN_*`, `HF_ENDPOINT`, `PYTORCH_ALLOC_CONF` 在 `start.bat`、`src/main.py`、`src/core/enhancer.py` 三处设置。
- **建议**: 只在 `src/main.py` 统一设置，`enhancer.py` 删除模块级设置。

#### C2: `stream.py` 中 `kill_edge_and_read_cookies()` 已废弃
- **位置**: `src/core/stream.py:142-169`
- **问题**: 功能已被教程对话框替代，函数不再被调用。
- **建议**: 删除该函数。

#### C3: `_cookie_auto_done` signal 和空 handler 未使用
- **位置**: `src/gui/main_window.py:29, 235, 376-377`
- **建议**: 删除 signal 声明、connect 和空 handler。

#### C4: `download_models.py` 没有下载 FastWave checkpoint
- **问题**: 只处理了 AudioSR 和 roberta-base，FastWave 需要从 Google Drive 手动下载。
- **建议**: 加入 FastWave 下载逻辑或明确提示。

#### C5: `player_widget.py` 的 `_mpv_log` 是空函数
- **位置**: `src/gui/player_widget.py:57-58`
- **建议**: 实现日志转发（至少 error 级别），或移除 `log_handler` 参数。

#### C6: `audiosr_model.py` 中 `_patch_audiosr_download` 路径与 `download_models.py` 不一致
- **位置**: `src/models/audiosr_model.py:76-78`
- **问题**: 硬编码 `models--haoheliu--audiosr_basic/pytorch_model.bin`，但 `download_models.py` 放在 `snapshots/0000.../pytorch_model.bin`。
- **建议**: 统一路径逻辑。

#### C7: `main_window.py` 中 `_handle_auto_cookie_done` 是空函数
- **位置**: `src/gui/main_window.py:376-377`
- **建议**: 随 C3 一起删除。

---

### 🔵 架构建议 (非 bug，值得考虑)

#### A1: 缺少全局异常处理
- 后台线程中的异常如果没被 catch，会静默消失。
- **建议**: 添加 `sys.excepthook` 和 `threading.excepthook`。

#### A2: 临时文件清理不完整
- `AudioPipeline._temp_dir` 在异常退出时会残留。
- **建议**: 使用 `atexit` 注册清理，或下次启动时清理旧 `ventiplayer_*` 目录。

#### A3: `Settings.set()` 每次修改都写磁盘
- **位置**: `src/config/settings.py:41-43`
- **问题**: 拖动音量滑块会高频触发写 JSON。
- **建议**: debounce 写入，或在 closeEvent 统一保存。

---

## 优先级排序

| 优先级 | 项目 | 状态 | 原因 |
|--------|------|------|------|
| **P0** | B4 (B站VIP检测字段错误) | ✅ 已修复 | 核心功能：大会员检测永远失败 |
| **P0** | B1 (实时模式O(n²)重写WAV) | ✅ 已修复 | 长音频会严重卡顿+可能损坏播放 |
| **P0** | B7 (SyncManager死锁风险) | ✅ 已修复 | 可能导致程序卡死 |
| **P1** | B5 (closeEvent缺失) | ✅ 已修复 | 影响正常退出 |
| **P1** | B3 (CPU设备torch.cuda调用) | ✅ 已修复 | CPU fallback会崩溃 |
| **P1** | B6 (play_av时序) | ✅ 已修复 | 分离音视频流播放可能失败 |
| **P2** | B8 (短音频Hann衰减) | ✅ 已修复 | 边缘case |
| **P2** | B2 (env重复设置) | ✅ 已修复 | 代码卫生 |
| **P2** | L1 (增强阈值逻辑) | ✅ 已修复 | 仅对无损≥48kHz block，有损仍可增强 |
| **P2** | L2 (UI文案"实时"→"快速") | ✅ 已修复 | 准确描述行为 |
| **P2** | L3 (drift计算基准) | ⚠️ 已确认 | mpv external audio 下 time_pos 行为合理，无需改动 |
| **P2** | L4 (重新启用增强自动切换) | ✅ 已修复 | 用户体验 |
| **P3** | C1 (env三处重复) | ✅ 已清理 | 集中到 src/main.py |
| **P3** | C2 (kill_edge废弃函数) | ✅ 已清理 | 删除死代码 |
| **P3** | C3 (空signal/handler) | ✅ 已清理 | 删除未使用代码 |
| **P3** | C4 (download_models缺FastWave) | ✅ 已修复 | 加入 gdown 下载逻辑 |
| **P3** | C5 (_mpv_log空函数) | ✅ 已修复 | 实现 error/warn 日志转发 |
| **P3** | C6 (audiosr路径不一致) | ✅ 已修复 | 搜索 snapshots 子目录 |
| **P3** | C7 (空handler) | ✅ 已清理 | 随C3一起删除 |
| **P3** | A1 (全局异常处理) | ✅ 已修复 | sys.excepthook + threading.excepthook |
| **P3** | A2 (临时文件清理) | ✅ 已修复 | atexit + 启动时清理旧残留 |
| **P3** | A3 (Settings高频写盘) | ✅ 已修复 | 1s debounce + flush() on exit |
