# VentiPlayer Debug Report

> 生成日期: 2025-05-28
> 审查范围: 全部 `src/` 源码、`run.py`、`start.bat`、`download_models.py`、`requirements.txt`

---

## 目录

- [P0 — 必须修复的 Bug](#p0--必须修复的-bug)
- [P1 — 高优先级问题](#p1--高优先级问题)
- [P2 — 中优先级改进](#p2--中优先级改进)
- [P3 — 低优先级 / 代码质量](#p3--低优先级--代码质量)

---

## P0 — 必须修复的 Bug

### 1. FastWave checkpoint 文件名不一致（已知但未修复）

- `download_models.py:84` 下载到 `fastwave_checkpoint.pt`
- `src/core/enhancer.py:181` 和 `src/models/fastwave.py:285` 期望 `checkpoint.pth`
- **影响**: 首次部署后 FastWave 模型不可用，用户必须手动 rename
- **修复**: 统一为一个名字，建议改 `download_models.py` 输出为 `checkpoint.pth`

### 2. `_quality_worker` 不设置 `enhanced_duration_s`

- `src/core/audio_pipe.py:220-230`: quality worker 完成后 `_update_status()` 没有传 `enhanced_duration_s`
- 导致 `main_window.py:1591` 中 `status.enhanced_duration_s` 始终为 0
- **影响**: quality 模式增强完成后，`_enhanced_duration_s` 不更新，seek 超出范围的保护逻辑失效；状态栏也无法正确显示增强时长
- **修复**: 在 quality worker 的 `_update_status(state=READY, ...)` 中加入 `enhanced_duration_s=len(enhanced)/output_sr`

### 3. `SyncManager._check_drift` 中 `target_speed` 可能未定义就被引用

- `src/core/sync.py:286-303`: `target_speed` 在 `with self._lock:` 块内赋值，但在块外 `if self._correction_active and self._set_speed_fn:` 处使用
- 如果 `all_above and same_direction` 为 False 但 `self._correction_active` 之前已经为 True（从上一次迭代遗留），则 `target_speed` 未定义 → `NameError`
- 实际上代码逻辑中 `elif self._correction_active:` 分支会 return，所以只有 `all_above and same_direction` 为 True 时才会走到外面。但如果 `_drift_history` 长度不足 `DRIFT_CONFIRM_COUNT` 时直接 return，`_correction_active` 可能仍为 True 而 `target_speed` 未赋值
- **修复**: 将 `target_speed` 初始化为 `None`，在块外检查 `if target_speed is not None and self._set_speed_fn:`

### 4. 剪贴板自动解析在窗口获得焦点时无条件触发播放

- `main_window.py:1151`: `_check_clipboard_url()` 在 URL 与当前输入不同时直接调用 `_on_play()`
- **影响**: 用户复制了一个 B站链接到剪贴板后切换回 VentiPlayer 窗口，会**立即中断当前播放**并开始解析新 URL
- 这是一个 UX 问题但也可能导致数据丢失（正在增强的音频被取消）
- **修复**: 只自动填入 URL 但不自动播放；或者仅在当前无播放时才自动触发

---

## P1 — 高优先级问题

### 5. `end_of_file` 信号误触发

- `src/gui/player_widget.py:76-80`: 当 `idle_active` 为 True 且 `_position != 0.0` 时发射 `end_of_file`
- 但 mpv 在加载新文件时也会短暂进入 idle 状态，可能导致误触发 `_play_next()`
- **建议**: 增加一个 `_file_loaded` 标志，只有在文件确实播放过之后才允许发射 `end_of_file`

### 6. `AudioPipeline` 的 `cancel()` 不能真正中止 PyAV 解码

- `src/core/audio_pipe.py:131-138`: `cancel()` 设置 `_cancel` event 后只 join 0.5s
- 如果 worker 卡在 `container.demux()` 的网络 I/O 上，线程不会退出
- 下次 `start_realtime_enhance()` 调用 `cancel()` 后立即启动新线程，可能出现两个 worker 同时写同一个 WAV 文件
- **修复**: 给 `_worker_thread` 加一个唯一 ID 或 generation counter，写文件前检查是否是当前 generation

### 7. `play_av()` 设置 `audio_files` 后不清理

- `src/gui/player_widget.py:144-146`: `self._player.audio_files = [audio_url]` 设置了外部音频
- 但后续 `play_url()` / `play_live()` 不会清除 `audio_files`，导致下一次播放可能仍然加载上一次的外部音频轨
- **修复**: 在 `play_url()` 和 `play_live()` 开头加 `self._player.audio_files = []`

### 8. `ResourceMonitor` 的 ADL PMLog 结构体假设不安全

- `src/core/resource_monitor.py:113-127`: 使用固定偏移量 `1 + N*2` 读取 PMLog 数据
- ADL PMLog 的实际布局因驱动版本而异，硬编码偏移量可能读到错误数据或越界
- **影响**: 在某些驱动版本上 GPU 利用率显示为 0% 或随机值
- **建议**: 添加 try/except 保护并在读取失败时 fallback 到 0

### 9. `_on_rows_moved` 直接操作 `_playlist._queue` 内部状态

- `src/gui/playlist_panel.py:148-154`: 直接赋值 `self._playlist._queue` 和 `self._playlist._current_index`
- 绕过了 `PlaylistManager` 的信号机制，可能导致 UI 与内部状态不一致
- **修复**: 在 `PlaylistManager` 中添加 `reorder(new_indices)` 方法

### 10. 直播流刷新时不检查当前是否仍在直播状态

- `main_window.py:1181-1187`: `_on_live_refresh()` 在 timer 触发时直接 re-resolve
- 如果用户已经手动停止或切换到其他视频，`_live_url` 仍然保留旧值，刷新结果会覆盖当前播放
- **修复**: 在 `_on_live_refresh()` 开头检查 `if not self._is_live: return`

---

## P2 — 中优先级改进

### 11. `_check_clipboard_url` 的 URL 提取逻辑脆弱

- `main_window.py:1143`: `url = text.split()[0]` — 如果剪贴板内容是 `"看这个视频 https://..."` 则取到的是 `"看这个视频"` 而非 URL
- **修复**: 用 `_CLIPBOARD_URL_RE.search(text)` 的 match 结果作为 URL

### 12. `Enhancer.enhance_chunk` 直接修改内部属性

- `src/core/enhancer.py:155`: `self._fastwave._num_steps = self._num_steps`
- 每次调用都直接写模型对象的内部属性，不是线程安全的
- **建议**: 将 `num_steps` 作为参数传入 `enhance()` 方法

### 13. `BilibiliAPI` 的 WBI 签名缓存无线程保护

- `src/core/bilibili_api.py:94-113`: `_ensure_wbi_keys()` 在多线程环境下可能并发执行，导致重复请求或部分写入
- **建议**: 加一个简单的 `threading.Lock`

### 14. `ThumbnailCache` 的 `ThreadPoolExecutor` 未在退出时 shutdown

- `src/gui/thumbnail_cache.py:35`: 创建了 `ThreadPoolExecutor(max_workers=4)` 但没有 `close()` / `shutdown()` 调用
- **影响**: 程序退出时可能有挂起的下载线程阻止进程退出
- **修复**: 在 `MainWindow.closeEvent` 中调用 `self._thumbnail_cache._pool.shutdown(wait=False)`

### 15. `content_browser.py` 推荐标签的 "加载更多" 实际调用 `get_popular()`

- `content_browser.py:372-378`: `_load_more_recommendations()` 调用的是 `self._api.get_popular()` 而非获取更多推荐
- **影响**: 推荐标签无限滚动加载的内容实际上是热门视频，与标签名不符
- **修复**: 如果没有真正的 "更多推荐" API，应禁用推荐标签的无限滚动，或改为标注 "热门补充"

### 16. `settings_dialog.py` 中 ASR combo 的 `currentIndexChanged` 重复连接

- `settings_dialog.py:389`: 每次调用 `_refresh_asr_models()` 都会 `connect` 一次 `_on_asr_model_changed`
- **影响**: 多次刷新后，选择模型会触发多次回调
- **修复**: 在 `_setup_ui` 中连接一次，或在 `_refresh_asr_models` 开头 `disconnect`

### 17. `SubtitlePipeline._cancel` 是普通 bool 而非 `threading.Event`

- `src/core/subtitle.py:145`: `self._cancel = False`
- 在 worker 线程中读取 `self._cancel` 没有内存屏障保护
- CPython 的 GIL 使得这在实践中通常安全，但语义上不正确
- **建议**: 改为 `threading.Event`

### 18. `video_enhance_panel.py` 的 CAS sharpness 语义反转

- `video_enhance_panel.py:752`: `get_cas_sharpness()` 返回 `1.0 - (value / 10.0)`
- 滑块值 0 → sharpness 1.0（最锐），值 10 → sharpness 0.0（无锐化）
- 但 UI 标签显示的是 `value / 10`（如 "0.6"），用户看到 0.6 以为是中等锐化，实际 CAS 参数是 0.4
- **注意**: CAS shader 中 `SHARPNESS` 越小越锐利（0=最锐，1=无锐化），所以代码逻辑是对的，但 UI 显示的数值含义与用户直觉相反
- **建议**: UI 标签改为显示 "锐化强度" 而非原始参数值，或反转滑块方向

### 19. `audio_pipe.py` 中 `_decode_full_audio` 的 `to_ndarray()` 格式假设

- `audio_pipe.py:179`: `arr = frame.to_ndarray()` — PyAV 默认返回 planar 格式
- 对于 planar 多声道音频，`arr.ndim > 1` 时 `arr.mean(axis=0)` 是正确的 downmix
- 但对于 packed 格式（如 s16），`to_ndarray()` 返回 `(samples,)` 形状的交错数据，此时 mono downmix 不正确
- **影响**: 极少数情况下（packed 立体声）音频数据会被错误处理
- **建议**: 显式指定 `frame.to_ndarray(format='fltp')` 确保 planar float

---

## P3 — 低优先级 / 代码质量

### 20. `run.py` 硬编码 Python 3.12 版本检查

- `run.py:5`: `if sys.version_info[:2] != (3, 12)` — 未来升级到 3.13 时需要手动改
- **建议**: 改为 `>= (3, 12)` 或移除此检查

### 21. `main.py` 硬编码 `C:/temp/miopen_cache`

- `src/main.py:41`: `os.makedirs("C:/temp/miopen_cache", exist_ok=True)`
- 在非 C 盘系统或权限受限环境下可能失败
- **建议**: 使用 `tempfile.gettempdir()` 下的子目录

### 22. `_cleanup_stale_temp_dirs()` 在模块导入时执行

- `src/core/audio_pipe.py:39`: 模块级别调用 `_cleanup_stale_temp_dirs()`
- 这意味着 `import src.core.audio_pipe` 就会触发文件系统操作
- **影响**: 增加导入时间，且如果 temp 目录有权限问题会在导入时报错
- **建议**: 延迟到 `AudioPipeline.__init__` 中执行

### 23. `playlist_panel.py` 中 `_highlight_current` 使用硬编码颜色

- `playlist_panel.py:244-250`: 使用 `Qt.GlobalColor.black` 作为非当前项前景色
- **影响**: 在暗色主题下文字不可见
- **建议**: 使用 `QPalette` 的默认前景色或不设置前景色

### 24. `stream.py` 的 `_parse_info` 对 `requested_formats` 的判断逻辑

- `stream.py:208-225`: 遍历 `requested_formats` 时，如果一个 format 同时有 vcodec 和 acodec（muxed），则不会被任何分支匹配
- **影响**: 对于某些 muxed 格式（如 YouTube 的 best fallback），`video_url` 和 `audio_url` 都为空
- 后面有 `if not video_url and not audio_url:` 的 fallback，但此时丢失了 codec/sr 信息
- **建议**: 增加一个 `elif fmt.get("vcodec") != "none" and fmt.get("acodec") != "none":` 分支处理 muxed

### 25. `enhancer.py:186-190` 中 `is_model_available(QUALITY)` 的副作用

- 调用 `is_model_available(EnhanceMode.QUALITY)` 会执行 `_patch_torch_distributed()` 和 `import audiosr`
- 这在后台检测线程中运行，可能与后续的 `load_model()` 产生 monkey-patch 竞争
- **建议**: `is_model_available` 应该只检查文件是否存在，不执行 import

### 26. `download_models.py` 中 AudioSR checkpoint 路径不在 snapshots 下

- `download_models.py:89`: 检查 `models--haoheliu--audiosr_basic/pytorch_model.bin`（直接在 repo 目录下）
- 但 `download_file()` 实际下载到 `snapshots/0000.../pytorch_model.bin`
- `audiosr_model.py:78-91` 的 `_patch_audiosr_download` 会搜索 snapshots 子目录，所以能找到
- 但 `download_models.py:89-91` 的存在性检查永远为 False（路径不对），导致每次运行都重新下载
- **修复**: 修正检查路径为 `snapshots/*/pytorch_model.bin`

### 27. `content_browser.py` 中 `_load_recommendations` 未被调用

- `content_browser.py:346-366`: `_load_recommendations()` 方法存在但没有任何地方调用它
- 推荐标签的初始数据依赖外部 `set_recommendations()` 调用
- **影响**: 如果 `_fetch_homepage_recommendations` 失败，推荐标签永远显示 "加载中..."
- **建议**: 在 `_on_tab_changed(0)` 时如果列表为空则调用 `_load_recommendations()`

### 28. `main_window.py` 中 `_on_browser_play_with_context` 的 `duration` 设为 `None`

- `main_window.py:1415`: `duration=v.duration or None` — 当 `v.duration == 0` 时变为 `None`
- `VideoItem.duration` 类型是 `Optional[float]`，但 `BiliVideoItem.duration` 是 `int`
- 0 时长的视频（如直播回放片段）会丢失时长信息
- **建议**: 直接用 `duration=v.duration`，让 0 保持为 0

### 29. `settings.py` 的 `_schedule_save` 中 Timer 可能在程序退出后触发

- `src/config/settings.py:63`: `threading.Timer` 是 daemon 线程，程序退出时会被强制终止
- 如果 `flush()` 没有被调用（如异常退出），最后一次设置变更可能丢失
- **现状**: `closeEvent` 中调用了 `flush()`，正常退出没问题；异常退出时丢失是可接受的

### 30. `subtitle.py` 中 `_extract_audio_from_url` 对长视频的内存占用

- `subtitle.py:78-109`: 将整个音频解码到内存（16kHz mono float32）
- 一个 2 小时视频 ≈ 2h × 3600 × 16000 × 4 bytes ≈ 460 MB
- **建议**: 对于超长视频，考虑分段解码或流式处理

---

## 架构层面建议（非 Bug）

### A. 信号/线程模型

当前项目大量使用 `threading.Thread(daemon=True)` + Qt Signal 跨线程通信。这个模式可行但有几个隐患：

1. 后台线程中的异常如果没有被 catch，会被 `threading.excepthook` 记录但不会通知 UI
2. 多个后台操作（resolve、enhance、subtitle、bili_api）可能并发修改 `_current_stream`
3. 建议引入一个简单的任务队列或使用 `QThread` + `QRunnable` 统一管理

### B. mpv 属性访问

`player_widget.py` 通过 250ms 轮询读取 mpv 属性。这比 mpv 的 observe_property 回调更安全（避免跨线程问题），但：

1. 250ms 间隔对于 seek 精度和 end-of-file 检测来说偏粗
2. 建议对 `idle-active` 使用 mpv 的 event 机制（`@player.event_callback`）来精确检测播放结束

### C. 错误恢复

增强管线的错误恢复路径（VRAM 不足 → fallback）设计合理，但缺少：

1. 重试机制（如 VRAM 不足后等待一段时间再尝试）
2. 用户可见的 "重新尝试" 按钮（当前只能手动再次点击增强）

---

## 总结

| 严重度 | 数量 | 关键问题 |
|--------|------|----------|
| P0 | 4 | checkpoint 文件名、quality 模式 duration 缺失、sync 变量未定义、剪贴板自动播放 |
| P1 | 6 | EOF 误触发、cancel 竞争、audio_files 残留、ADL 偏移、内部状态直接操作、直播刷新 |
| P2 | 9 | URL 提取、线程安全、信号重复连接、语义反转等 |
| P3 | 11 | 硬编码、代码质量、内存占用等 |
