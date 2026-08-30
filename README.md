# VentiPlayer

> [unstable]面向音乐发烧友的 AI 音频修复流媒体播放器：输入 YouTube / Bilibili / Twitch 链接，自动取最高码率音轨，经 AI 修复带宽及分辨率后通过 WASAPI Exclusive 直通 DAC 或数字界面。为保证画面体验加入基本滤镜和超分方案，并内置 Whisper + LLM 的字幕生成管线。

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows%2011-lightgrey)
![Status](https://img.shields.io/badge/status-MVP-orange)

---

## 目录

- [它能做什么](#它能做什么)
- [目前已经实现的工作](#目前已经实现的工作)
- [浏览与播放体验](#浏览与播放体验)
- [视频增强](#视频增强)
- [视频导出](#视频导出)
- [字幕生成](#字幕生成)
- [架构总览](#架构总览)
- [模块说明](#模块说明)
- [部署指南](#部署指南)
- [运行](#运行)
- [仓库布局与 .gitignore 说明](#仓库布局与-gitignore-说明)
- [已知问题与限制](#已知问题与限制)
- [License](#license)

---

## 它能做什么

- 直接粘贴 **YouTube** / **Bilibili** / **Twitch** 链接播放，自动用 yt-dlp 选最高音频码率 + 视频流
- **B 站直播**：粘贴 `live.bilibili.com` 链接即可观看，自动选最高画质，支持视频增强 shader，流地址过期自动静默刷新
- **Twitch**：支持 VOD 回放（`twitch.tv/videos/xxx`）和直播（`twitch.tv/username`），直播走通用直播路径（自动重连、视频增强可用）
- **音频流媒体压缩修复**：修复视频上传 B站 / YouTube 后被有损编解码器切掉的高频与压缩伪影，可选超分到 48 kHz。两套后端可**独立启用或级联**：
  - **编解码修复（Apollo，~36 M 参数）**：频域 band-split GAN，单次前向，专为修复 MP3/AAC 等有损压缩的音乐而训练；工作于 44.1 kHz，原生立体声，~19x 实时
  - **采样率超分（FlashSR）**：AudioSR 的一步蒸馏版，单步扩散把任意采样率重建并升频到 48 kHz，通用含音乐，比 AudioSR 快约 22x
  - 两者同开时按 **Apollo → FlashSR** 链式处理（先修复再升频）
- 增强采用**离线整轨**处理：点播放先用原音频出声，点「修复当前音频」后台跑模型，**修复完成才无缝切换**到增强音轨（位置对齐），失败自动回退原音轨
- 两个模型均提供 **fp16 半精度**独立开关（更快、更省显存，质量略降）
- 增强后的音频经 **mpv WASAPI Exclusive** 输出，绕过 Windows 混音器
- **视频导出**：解析非直播视频后，可在设置中一键把视频连同当前面板配置的音频/画面增强**真实烘焙**导出为 mp4——画面走离屏 GPU 渲染真实烘焙 GLSL 着色器（超分/锐化/去色带/HDR 等），音频离线重跑 Apollo/FlashSR 后混流；导出完成弹窗汇报保存路径、视频信息、所用音频/画面方案
- 自带 **A/V 同步管理器**：切到整轨增强 WAV 后台监测 audio↔video PTS 漂移，仅在漂移持续且确认后做温和的 ±1% speed 软修正，失败时无缝 fallback 回原音轨
- **字幕生成**：Whisper ASR 转写 + 可选 LLM 润色，输出 SRT 并挂载到播放器
- B 站 cookie 自动修复 + 大会员状态嗅探（自动选 Hi-Res / 杜比可用音轨）
- 状态栏内置**资源监视器**（CPU / RAM / GPU 占用 / 显存，AMD 走 ADL，其余走 psutil）
- 配置 / 缓存 / 模型权重落在 `~/.ventiplayer/`，便于多副本共享

> 这是一份 **MVP 实现**，不是产品。已经能从粘贴 URL 一路走到 DAC 出声，但还有很多边界情况需要打磨。

---

## 目前已经实现的工作

按 `PLAN.md` / `DEBUG_PLAN.md` 中的 Phase 划分：

| Phase | 内容 | 状态 |
|---|---|---|
| 1 — 播放骨架 | PySide6 GUI + mpv 嵌入 + WASAPI Exclusive + yt-dlp | ✅ 完成 |
| 2 — AI 增强 | Apollo 编解码修复 + FlashSR 采样率超分 + 可级联离线管线 + fp16 半精度选项 | ✅ 完成 |
| 3 — 同步与稳定 | SyncManager 漂移监测 / cooldown 抑制 / fallback | ✅ 完成 |
| 4 — GUI 完善 | 增强面板 / 状态栏 / 资源监视器 / 配置持久化 / 快捷键 | ✅ 完成 |
| 5 — 视频增强 | CAS / Deband / Denoise / HDR / 超分辨率 (Anime4K, FSR, FSRCNNX) | ✅ 完成 |
| 5.5 — B站直播 | live.bilibili.com 直播播放 + 视频增强 + 流刷新/重连 | ✅ 完成 |
| 5.6 — Twitch | VOD 回放 + 直播，剪贴板自动识别，通用直播路径 | ✅ 完成 |
| 6 — 视频插帧 | display-resample 伪插帧 + 小黄鸭 (Lossless Scaling) 外部全屏补帧 | ✅ 完成 |
| 7 — 字幕生成 | Whisper ASR + LLM 润色 → SRT，挂载到播放器 | ✅ 完成 |
| 8 — 视频导出 | 离屏 GPU 渲染真实烘焙画面增强 + 离线增强音频 → mp4，PyAV 近似为退化回退 | ✅ 完成 |

实测目标硬件：

- CPU：任意支持 AVX2 的 x86_64
- GPU：**AMD Radeon RX 9070** (RDNA 4, gfx1200)
- 推理后端：**Windows 原生 PyTorch 2.9 + ROCm 7.2.1**（首选，已验证）
  - DirectML / CPU 作为 fallback（代码已支持，AMD ROCm 不可用时自动选用）
- OS：Windows 11

---

## 浏览与播放体验

### 内容浏览

右侧面板提供多标签浏览，数据来源于 Bilibili API（需 Cookie 登录态）：

| 标签 | 内容 | 加载方式 |
|---|---|---|
| 推荐 | 个性化推荐视频 | 无限滚动，滑到底部自动加载下一页 |
| 热门 | 全站热门排行 | 无限滚动 |
| 搜索 | 关键词搜索结果 | 无限滚动 |
| 收藏夹 | 用户收藏夹列表 → 文件夹内视频 | 手动刷新（需切换文件夹） |

### 播放列表与历史

- **播放列表**：反映当前播放上下文的来源。从搜索结果播放则列表为搜索结果；从收藏夹播放则为该文件夹内容；从推荐/热门播放则为对应列表；URL 直接解析则仅含单个视频。
- **历史**：记录所有播放过的视频（最近优先，去重，上限 200 条），持久化到 `~/.ventiplayer/history.json`。
- **播放模式**：控制栏单按钮循环切换——顺序 → 单曲循环 → 列表循环 → 随机。

### 快捷键

| 按键 | 功能 |
|---|---|
| `Space` | 播放 / 暂停 |
| `Ctrl+Enter` | 解析并播放 URL 栏内容 |
| `F` | 进入全屏 |
| `Esc` | 退出全屏 |
| `←` / `→` | 后退 / 前进 5 秒 |
| `N` / `P` | 下一个 / 上一个 |

### 缩略图

- 可在设置中开启缩略图模式，所有视频列表（播放列表、历史、浏览各标签）显示封面缩略图
- 缩略图大小可通过滑动条调节（60–160px 宽，高度按 16:9 自动计算）
- 缓存层统一输出固定尺寸画布（160×90），非标准比例的图片等比缩放后居中放置，确保列表文字对齐
- 异步下载 + LRU 缓存（100 条），不阻塞 UI

### 推荐

播放列表下方显示当前视频的相关推荐（至多 5 条），双击即可播放并将推荐列表设为新的播放列表。

### B站直播

粘贴 `https://live.bilibili.com/{room_id}` 到 URL 栏即可播放直播。行为与普通视频的区别：

- 解析后**立即开始播放**（不暂停等待用户操作）
- 进度条禁用，时长显示为 "LIVE"
- **音频增强不可用**（直播流为无限长度，不适用 chunk 离线推理）
- **视频增强正常工作**（CAS / 超分辨率 / Deband 等 shader 对任何输入流均生效）
- 流地址每 25 分钟自动静默刷新（B站直播流 URL 有 TTL）
- 流中断（主播下播或网络中断）时自动尝试重连（至多 3 次），失败后显示"直播已结束"
- 直播不加入播放列表，但会记录到历史
- 需要 Cookie 登录态才能获取最高画质（原画）

### Twitch

粘贴 Twitch 链接到 URL 栏即可播放。支持两种形式：

- **VOD 回放**：`https://www.twitch.tv/videos/123456789` — 与普通视频行为一致（可暂停、拖进度条、音频增强可用）
- **直播**：`https://www.twitch.tv/username` — 走通用直播路径，行为同 B站直播（立即播放、进度条禁用、自动重连）

认证说明：

- 公开 VOD 和直播**不需要 cookie**，直接粘贴即可
- 订阅者专属 VOD 需要 Twitch 登录态 — 在 cookie 文件中包含 `twitch.tv` 的 cookie 即可（与 B站 cookie 共用同一文件或分开均可，yt-dlp 按域名匹配）
- 不需要额外 API key 或 OAuth token

---

## 视频增强

除了音频 AI 超分辨率，VentiPlayer 还提供基于 mpv GLSL shader 的视频增强管线。所有增强在 GPU 上实时渲染，不额外占用 CPU。

### 已实现

| 功能 | 说明 |
|---|---|
| 基础画面调整 | 亮度 / 对比度 / 饱和度 / Gamma，通过 mpv 属性实时调节 |
| CAS 锐化 | AMD FidelityFX CAS shader，强度 0.0–1.0 可调，运行时模板生成 |
| 去色带 (Deband) | mpv 内置 deband 滤镜，可调迭代 / 阈值 / 范围 |
| 降噪 | hqdn3d 或 nlmeans 算法，强度可调 |
| HDR 色调映射 | 支持 mobius / reinhard / hable / bt.2390 / spline 等算法 + 动态峰值检测 |
| 超分辨率 | 统一入口，下拉选择算法，启用后状态栏显示实际输出分辨率 |

#### 超分辨率算法

| 算法 | 参数 | 适用场景 |
|---|---|---|
| **Anime4K** | 模式 (A/B/C/A+A/B+B/C+A) × 倍率 (x2/x4，默认 x4) × 质量 (快速/均衡/质量/极致/极限) | 动画片源 |
| **FSR** | 锐化强度 (0.0–2.0，默认 0.2) + RCAS 降噪开关 | 通用片源，AMD FidelityFX Super Resolution |
| **FSRCNNX** | 无可调参数 | 通用片源，神经网络超分 (FSRCNNX_x2_16-0-4-1) |

- Anime4K 模式说明：A = 1080p/高模糊源，B = 720p/低模糊源，C = 480p/无退化源；A+A / B+B / C+A 为对应加强版（更高质量，更慢）
- Anime4K 质量档（快速 S → 均衡 M → 质量 L → 极致 VL → 极限 UL）对应不同 shader 链长度，越高越慢
- FSR 和 CAS 均采用模板生成模式：运行时将参数写入 `*_active.glsl`，mpv 加载生成后的 shader（CAS 面板强度 0–10 映射为 shader 的 `SHARPNESS`，0 = 不锐化、10 = 最强）

### 视频插帧 / 帧生成

帧生成面板提供「后端下拉」统一入口，二选一（默认 **display-resample 伪插帧**）：

| 后端 | 实现 | 说明 |
|---|---|---|
| **display-resample (伪插帧)** | mpv 内置 interpolation | 启用后设 `video-sync=display-resample` + `tscale`（默认 oversample），按显示器刷新率重采样时间轴。零依赖、零额外算力、始终可用。不是真正补帧，但能消除帧率与刷新率不匹配的微抖动，是日常推荐档。 |
| **小黄鸭 (Lossless Scaling 全屏补帧)** | 外部程序 [Lossless Scaling](https://store.steampowered.com/app/993090/) 的 LSFG（最新 LSFG 3.1） | 调用外部小黄鸭程序对**全屏画面**做神经光流补帧，与 AMD AFMF 同思路，平滑度明显优于块匹配类算法。需在设置中填写 `LosslessScaling.exe` 路径；选中后由 VentiPlayer 懒启动该程序，**进入全屏自动开启缩放、退出全屏自动关闭**（通过向其发送全局快捷键，默认 `Ctrl+Alt+S`，须与小黄鸭内设置一致）。 |

为什么不再内置 SVP / RIFE：早期版本曾内置 SVP（svpflow 块匹配，4K 可实时但平滑感不足且带 DRM 红框）与 PyTorch RIFE（神经光流画质好但 4K 非实时），实测体验均不达预期，已全部移除。现方案分工明确——伪插帧用于零成本消抖，真·补帧交给成熟的外部工具小黄鸭（其 LSFG 在画质与性能上均优于此前的内置方案）。

小黄鸭工作机制：它通过屏幕捕获（DXGI/WGC）对全屏窗口做补帧并叠加输出，因此**只对无边框/窗口化全屏生效**（这也是触发点选在进/退全屏的原因），且会连同字幕/UI 一起插帧。延迟略高，但对被动看视频无影响。VentiPlayer 退出时会自动结束小黄鸭进程。

> 注意：若 `LosslessScaling.exe` 设为「以管理员身份运行」（默认会弹 UAC 确认框），由于 Windows UIPI 限制，VentiPlayer 无法自动最小化其窗口；启动后程序会尽力最小化 LS 并把播放器拉回前台，但若仍盖在上方，可在 LS 属性→兼容性中关闭「以管理员身份运行」即可彻底解决。未配置路径或启动失败时，「小黄鸭」选项自动灰显并退回 display-resample。

### 面板布局

视频增强面板采用双栏布局：

- **左栏**：基础画面调整、CAS 锐化、去色带、降噪、HDR 色调映射
- **右栏**：超分辨率、AI 帧生成 / 插帧

### 状态栏指示

信息栏右侧提供一排灰/绿指示灯（彼此风格统一，灰=未生效、绿=已生效）：

| 指示灯 | 绿色 | 灰色 |
|---|---|---|
| 音频方案 | 音频 AI 增强已激活，显示**实际采用的方案**（如 `Apollo(fp16)+FlashSR(fp32)`） | 播放源音频（`源音频`）|
| 超分 | 视频超分 shader 已加载 | 未启用超分（`未超分`）|
| 帧生成 | 伪插帧已生效，或小黄鸭已开启缩放（`小黄鸭 生效`）；小黄鸭已选中但未进全屏时显示黄色（`小黄鸭 待全屏`） | 未启用（`源帧率`）|
| 独占 | WASAPI Exclusive 已开启（`独占`） | 共享模式（`非独占`）|

- 音频方案标签在点击「修复当前音频」时按当时的勾选/精度状态定格，之后改动勾选不影响当前正在播放音轨的标签显示
- 媒体信息文本统一为 `V-...` / `A-...` 两段、以 ` | ` 分隔，箭头 `→` 统一表示「源 → 增强/输出」：
  - 视频：`V-1920×1080-24fps → 3840×2160-24fps`（源分辨率帧率 → 超分后输出）
  - 音频：`A-44.1kHz-16kHz → 48kHz-24kHz`（源采样率截止 → 增强后采样率截止）

信息栏同时显示**资源监视器**：CPU / 内存占用，以及推理后端的 GPU 占用与显存（AMD 走 ADL 的 `atiadlxx.dll`，读取失败时回落到 psutil 的 CPU/RAM）。

---

## 视频导出

解析一个**非直播**视频后，可在「设置 →视频导出」点「导出当前视频为 MP4」，把视频连同当前面板上配置的全部音频/画面增强**真实烘焙**导出为本地 mp4。

### 落地方式

| 维度 | 处理 |
|---|---|
| **音频** | 离线重跑当前启用的 Apollo / FlashSR 链（含 fp16 设置）产出增强音轨，未启用任何音频模型时直通原音；最终编码为 AAC 混流 |
| **画面（主路径）** | **离屏 GPU 渲染真实烘焙**：用 Qt 离屏 OpenGL 上下文驱动 libmpv 的 render API（`MpvRenderContext`），让每帧走完 mpv 完整 GPU 着色器管线（Anime4K/FSR/FSRCNNX 超分、CAS 锐化、deband、HDR 色调映射、亮度/对比度/饱和度/gamma），回读 framebuffer 后用 PyAV 编码 H.264 |
| **画面（退化回退）** | 若当前环境无法创建离屏 GPU 上下文（无显示设备等），自动退化为 PyAV 近似烘焙：亮度/对比度/饱和度/gamma（numpy）、降噪、按超分倍率做 lanczos 缩放近似；此时 GLSL 着色器无法真实烘焙，弹窗会明确告知 |
| **插帧** | **不烘焙**：display-resample 伪插帧是显示期特性（依赖显示器刷新率）、小黄鸭是外部全屏叠加程序，二者均无法写进文件，导出文件保持源帧率（方案标签里标注「插帧不烘焙」）|

### 流程

1. 设置面板点导出 → 弹文件保存对话框选 `.mp4` 路径（记住上次目录）
2. 后台线程跑导出，进度对话框实时显示（可取消）
3. 完成后弹「导出成功」窗口，汇报：保存路径 + 视频信息（`格式 / 分辨率 / 帧率 / 音频采样率 / 截止频率`，如 `mp4 / 3840×2160 / 23.976fps / 48kHz / 24kHz`）+ 所用音频修复方案 + 所用画面增强方案；若退化为近似烘焙则额外提示

---

## 字幕生成

对没有字幕（或字幕质量差）的视频，VentiPlayer 内置一条 **ASR + LLM** 字幕生成管线，结果输出为 SRT 并直接挂载到 mpv 播放器。

### 工作流程

1. **提取音频**：用 PyAV 从音频流解码并重采样为 16 kHz 单声道 float32
2. **语音识别**：HuggingFace transformers 的 Whisper pipeline 转写，输出带时间戳的分段；长视频按 300 秒分块以控制显存，超长音频会在日志中给出内存占用预警
3. **LLM 润色（可选）**：把识别文本按 80 行一批发给 OpenAI 兼容接口，由 LLM 判断视频类型、修正同音字/断句、补标点、去口语填充词。返回行数不匹配或接口不可用时，自动回退到原始识别结果
4. **生成 SRT**：写出标准 SRT，缓存到 `~/.ventiplayer/subtitles/{video_id}_{语言}.srt`，下次同一视频直接命中缓存
5. **挂载**：通过 `player_widget.load_subtitle()` 加载到 mpv

整条管线运行在后台守护线程，进度通过状态信号回传 GUI（提取 → 识别 → 润色 → 完成），可随时取消（`threading.Event`）。

### 使用要点

- 主界面提供字幕按钮与语言下拉（中 / 英）
- **ASR 模型**：在设置中选择并下载 Whisper（large-v3 / medium / small）。模型下载到 `~/.ventiplayer/models/whisper/`；当 C 盘空间不足 5 GB 时自动改存到备用缓存目录 `~/.ventiplayer/hf_cache/`。Whisper 走 PyTorch，可用 ROCm/CUDA 加速
- **LLM 配置**：在设置中填写 OpenAI 兼容服务的 `base_url` / `api_key` / `model`，可一键测试连通性；不配置 LLM 时管线仍可只用 ASR 结果出字幕
- ASR 模型与 LLM provider 配置持久化在 `~/.ventiplayer/config.json`

---

## 架构总览

```
┌──────────────────────────────── PySide6 GUI ────────────────────────────────┐
│  URL 栏 │  视频画面 (mpv 嵌入)  │  进度条 / 音量 / 状态  │  增强面板         │
└───────────────┬─────────────────────────────────────────────────────────────┘
                │
       ┌────────▼────────┐    ┌──────────────────┐
       │ stream.py       │───▶│ StreamInfo       │  yt-dlp 解析 + cookie 修复 +
       │ StreamResolver  │    │ (audio/video URL,│  B 站 nav API VIP 嗅探
       └─────────────────┘    │  http_headers …) │
                              └────────┬─────────┘
                                       │
        ┌──────────────────────────────┼─────────────────────────────────┐
        │                              │                                 │
        ▼                              ▼                                 ▼
┌──────────────────┐        ┌──────────────────────┐         ┌──────────────────────┐
│ player_widget.py │        │ audio_pipe.py        │         │ sync.py              │
│ (mpv 嵌入)       │        │ AudioPipeline        │         │ SyncManager          │
│ video + 原始音轨 │        │ ┌─ _worker ─────────┐│         │ 后台线程 1 s 检漂   │
│                  │◀───────│ │ PyAV 解码全曲     ││────────▶│ 软修正 ±1 % speed   │
│                  │  增强  │ │ (保留立体声)      ││  switch │ fallback 回原音     │
│                  │  WAV   │ │ → enhance_full →  ││  audio  │                     │
│                  │        │ │ enhanced.wav      ││         └──────────────────────┘
│                  │        │ └───────────────────┘│
└──────────────────┘        └──────────┬───────────┘
                                       │
                                       ▼
                            ┌──────────────────────┐
                            │ enhancer.py          │
                            │ Enhancer (设备检测 + │
                            │ 组合处理链)          │
                            └──────┬─────────┬─────┘
                                   │ Apollo  │ FlashSR (可级联)
                       ┌───────────▼──┐  ┌───▼──────────────┐
                       │ apollo_model │  │ flashsr_model    │
                       │ band-split   │  │ 1-step diffusion │
                       │ GAN @44.1k   │  │ → 48 kHz         │
                       └──────────────┘  └──────────────────┘
```

### 增强数据流（离线整轨）

```
URL ──▶ yt-dlp 解析（约 2-3 s）
        │
        ├─▶ 视频/原音轨 URL ──▶ mpv 渲染、原音 WASAPI 出声（先放原音，不阻塞）
        │
        └─▶ 用户点「修复当前音频」
                │
                ▼
        PyAV 解码全曲（保留立体声）──▶ Enhancer.enhance_full
                │
                ▼
        启用的后端串行处理：Apollo(44.1k 修复) ─▶ FlashSR(超分 48k)
        （可只跑其一；两者皆为单次/单步前向，整轨几秒~几十秒）
                │
                ▼
        合并写出 enhanced_quality.wav (FLOAT, 多声道)
                │
                ▼
        READY → SyncManager.activate_enhanced(file, current_pos)
        无缝切到增强音轨（位置对齐），失败 fallback 回原音
```

---

## 模块说明

### `src/main.py`
QApplication 入口。负责：
- Splash screen + 后台线程预热设备检测（避免主窗口冻结）
- Windows 终端 UTF-8 编码切换、`PATH` 注入 `libmpv` / `deno`
- 顶层异常拦截，避免静默崩溃

### `src/gui/main_window.py`
主窗口。聚合 `MpvPlayerWidget` + `EnhancePanel` + `VideoEnhancePanel` + `ContentBrowser` + `PlaylistPanel`，负责：
- URL 解析触发、Stream 信息回填到 UI；剪贴板自动识别支持的链接并填入 URL 栏（不自动播放）
- 把 `Enhancer` / `AudioPipeline` / `SyncManager` 串起来
- `PipelineState` 状态机回调到 GUI（进度、错误提示、自动 fallback 提示）
- 驱动字幕生成（`SubtitlePipeline`）并把生成的 SRT 挂载到播放器
- 驱动**视频导出**（`VideoExporter`）：从设置面板信号触发，拼装 `ExportSettings`、起后台导出、进度对话框与「导出成功」结果弹窗
- 启动资源监视器，按推理后端在状态栏刷新 CPU/RAM/GPU/显存
- 快捷键：`Space` 暂停 / `Ctrl+Enter` 解析播放 / `F` 全屏 / `Esc` 退出全屏 / `←→` ±5 s seek / `N`/`P` 上下一个

### `src/gui/player_widget.py`
mpv 嵌入组件。
- 通过 `python-mpv` + `wid=widget.winId()` 把视频画到 Qt 窗口
- 用 `QTimer` 周期轮询 `time-pos` / `audio-pts` / `duration`，**不**走 mpv 的事件回调（避免 PySide6 跨线程信号问题）
- `load_subtitle()` 把生成的 SRT 加载为 mpv 字幕轨

### `src/gui/enhance_panel.py`
音频增强面板。两个**独立勾选框**——「编解码修复 (Apollo)」与「采样率超分 (FlashSR)」，可单独或同时勾选（同开则链式 Apollo→FlashSR）；每个模型下挂一个缩进的 **fp16 半精度**子勾选框（仅在父模型勾选时可用）；权重缺失项自动灰显。一个「修复当前音频」运行按钮触发后台离线处理，内置带播放标记的进度条。源采样率 ≥ 48 kHz 时仅禁用 FlashSR（无采样率可超分），Apollo 仍可用（伪影修复与采样率无关）。`get_settings()` 返回 `{apollo_enabled, flashsr_enabled, apollo_fp16, flashsr_fp16}`。

### `src/gui/video_enhance_panel.py`
视频增强面板（双栏）。基础画面调整、CAS 锐化、去色带、降噪、HDR 色调映射、超分辨率（Anime4K / FSR / FSRCNNX）、视频帧生成（display-resample 伪插帧 / 小黄鸭外部全屏补帧）。FSR / CAS 用运行时模板生成 `*_active.glsl`。`get_cas_sharpness()` 返回滑块值 /10（0.0–1.0），写入 shader 前再取 `1 - x` 反转（shader 中 0 = 最强锐化）。`get_export_state()` 把当前画面增强的完整可复现状态（着色器路径列表 / mpv render 属性 / 降噪 vf / 超分倍率 / 中文方案标签）打包给视频导出模块，复用与 live-apply 同一套纯构建函数（`_build_*`），不触发任何信号。

### `src/core/video_export.py`
**视频导出引擎**（后台线程，进度/完成走回调）。`VideoExporter.export()` 把视频连同音频/画面增强真实烘焙为 mp4：
- 音频：复用 `Enhancer.enhance_full` 离线重跑 Apollo/FlashSR（或直通原音），写临时 WAV
- 画面（主路径 `_bake_video_gpu`）：Qt 离屏 OpenGL 上下文 + libmpv `MpvRenderContext`，逐帧 `frame-step` → `render()` 进 FBO → `toImage()` 回读 → PyAV 编码 H.264；着色器/render 属性/降噪 vf 与 main_window 的 live-apply 对齐（**仅允许 lavfi 降噪 vf，绝不注入 vapoursynth**）
- 画面（退化 `_bake_video_pyav`）：GPU 上下文不可用时退化为 PyAV 解码 → numpy eq + nlmeans + lanczos 缩放近似，`gpu_baked=False` 并明确告知
- 混流：video-only mp4 的 H.264 流原样 remux + AAC 编码音频 → 最终 mp4
- `ExportSettings.from_states()` 一行合并面板/画面/Stream 状态；`ExportResult` 提供 `video_info_label` 等给成功弹窗

### `src/gui/settings_dialog.py`
设置对话框（Cookie / 显示 / LLM / 字幕 ASR / 帧生成小黄鸭 / **视频导出**）。「视频导出」分组的按钮点击发 `export_video_requested` 信号，门控、文件对话框与导出逻辑由 main_window 接管。

### `src/core/frame_gen.py`
帧生成后端依赖检测。返回 display-resample（始终可用）与小黄鸭（`detect_lossless_scaling()` 静态校验 `LosslessScaling.exe` 路径是否存在）的可用性，供面板灰显不可用项。不直接执行补帧——伪插帧走 mpv 属性，小黄鸭由外部程序完成。

### `src/core/lossless_scaling.py`
小黄鸭（Lossless Scaling）外部程序控制器，不依赖 Qt（便于单测）。负责懒启动 `LosslessScaling.exe`、通过 Win32 `keybd_event` 发送全局缩放快捷键（解析 `ctrl+alt+s` 之类组合为 VK 序列、按下后逆序释放）、维护 `_scaling` 状态去重发送、尽力最小化 LS 窗口（受 UIPI 限制，仅 LS 非提权时有效）、退出时 `taskkill` 兜底结束所有实例。

### `src/gui/content_browser.py`
内容浏览器（推荐 / 热门 / 收藏 三个标签）。切到空标签时自动加载对应内容；从浏览器播放时带上下文（兄弟视频）构建播放列表。

### `src/gui/playlist_panel.py`
播放列表 / 历史面板。支持拖拽重排（`reorder()` 同步更新 current_index），当前播放项高亮采用主题感知配色（清除前景色 role 回退系统默认，兼容暗色主题）。

### `src/gui/thumbnail_cache.py`
缩略图缓存。`ThreadPoolExecutor`（4 worker）异步下载，LRU 缓存 100 条，统一输出 160×90 画布，`shutdown()` 在退出时回收线程池。

### `src/core/stream.py`
yt-dlp Python API 封装：
- `fix_cookie_file()`：Netscape cookie 文件中 `domain ` 与 `domain_flag` 不一致会让 Python cookiejar 拒绝加载，写一份临时修复版到 `tempdir/ventiplayer/`
- `check_cookie_status()` → 调 `https://api.bilibili.com/x/web-interface/nav` 返回 VIP 状态、用户名
- `StreamResolver.resolve()` 同步 / `resolve_async()` 后台线程；cookie 失败时自动 retry 一次裸请求并把 `cookie_failed=True` 上报给 UI
- 解析 `requested_formats` 时分别处理 video-only / audio-only / muxed（单流含视频+音频）三类格式

### `src/core/bilibili_api.py`
B 站 Web API 客户端，主要解决 **WBI 签名**：
- 从 cookie 文件读取 `SESSDATA` / `buvid3`
- `_ensure_wbi_keys()` 从 nav API 拉 `img_key` / `sub_key`（缓存 1 小时），用 `threading.Lock` + double-check 保证并发安全
- `_sign_wbi()` 按 mixin key 置换表生成 `w_rid` / `wts` 签名

### `src/core/audio_pipe.py`
单个后台 worker 线程（**daemon**，cancel 时 `.join(timeout=0.5)` 不等卡死的推理）：
- `_worker`：PyAV demux 解全曲（`frame.to_ndarray(format='fltp')`，**保留立体声**，按 `(channels, samples)` 拼接）→ `enhancer.enhance_full()` → `soundfile` 一次写出多声道 `enhanced_quality.wav`（转 `(samples, channels)`）
- 进度 0.3→0.95 映射推理进度；遇 VRAM 不足 / 任意异常报 recoverable 错误供上层 fallback
- 首次实例化时（非 import 时）`_cleanup_stale_temp_dirs()` 回收上次崩溃留下的 `tempdir/ventiplayer_*`

### `src/core/enhancer.py`
- `detect_device()`：torch.cuda(ROCm) → onnxruntime DML → CPU 三路探测
- 两个独立开关 `set_apollo_enabled` / `set_flashsr_enabled` + 两个 fp16 开关 `set_apollo_fp16` / `set_flashsr_fp16`；`load_models()` 只加载被启用的后端、卸载被关闭的，并在某后端精度（fp16/fp32）变化时强制重载
- `enhance_full()` 把启用的后端按 **Apollo → FlashSR** 串行成处理链，返回 `(audio, output_sr)`
- `available()` 检查各后端权重是否就位（Apollo ckpt / FlashSR 三件套），供 GUI 灰显

### `src/core/sync.py`
`SyncManager`（设计理念：mpv 自身已做 A/V 同步，本管理器只在增强音轨场景做温和兜底，避免与 mpv 抢同步导致抖动）：
- `SOFT_DRIFT_MS = 50`、`SYNC_CHECK_INTERVAL = 1.0`、`DRIFT_CONFIRM_COUNT = 3`
- seek / 切音轨 / resume 后各有 cooldown（3 / 8 / 3 秒）抑制误判
- 仅做 ±1 % speed 软修正；增强音轨范围内**绝不 seek**（避免增长中的 WAV 触发 mpv 重读 header 形成反馈环）
- `fallback_to_original(reason)` 在任何失败路径上把音轨换回 mpv 自带的原音轨

### `src/core/subtitle.py`
字幕生成管线（后台守护线程）：PyAV 提取 16 kHz 单声道音频 → Whisper ASR（>300 s 分块）→ LLM 按 80 行批量润色（失败回退原文）→ 生成 SRT 并缓存到 `~/.ventiplayer/subtitles/`。状态通过 `SubtitleStatus`（extracting/transcribing/refining/done/error）回传，`threading.Event` 支持取消。

### `src/core/asr_engine.py`
Whisper ASR 封装（HuggingFace transformers pipeline）。管理模型下载（large-v3 / medium / small）、设备选择，C 盘空间不足时切到备用缓存目录。

### `src/core/llm.py`
OpenAI 兼容 LLM 客户端。封装 provider 配置（base_url / api_key / model）、`call()` 单轮请求、连通性测试，供字幕润色调用。

### `src/core/resource_monitor.py`
系统资源监视。AMD 走 ADL（`atiadlxx.dll`）读 GPU 占用 / 显存，读取偏移有边界校验和 try/except 保护；其余环境回退 psutil 的 CPU / RAM。`format_stats()` 输出状态栏文本。

### `src/core/playlist.py`
播放列表与历史管理。维护播放上下文、current_index、播放模式（顺序/单曲/列表循环/随机），`reorder()` 拖拽重排，历史去重并上限 200 条持久化到 `history.json`。

### `src/models/apollo_model.py`
**Apollo 编解码修复 wrapper**（模型源码 vendored 在 `src/models/apollo_src/`，License CC-BY-SA 4.0）：
- 架构：频域 band-split GAN（RMSNorm + Roformer 序列建模 + 频带重建），单次前向，~36 M 参数
- 工作于 44.1 kHz、**原生立体声**；输入≠44.1k 时先 `scipy.signal.resample_poly` 逐声道重采样进
- 25 s chunk + 线性淡入淡出 overlap-add（reflect padding 抑制边界伪影），逐块 `progress_callback`
- 先 CPU 建模再 `.to("cuda")`、`load()` 后跑 dummy 推理 **预热**，规避 ROCm `LLVM ERROR: Can't get available size`
- 推理前嗅探剩余 VRAM 不足直接抛 `VRAM 不足`（让上层 fallback）；**Apollo 不改采样率**，仅修复频带/伪影
- **fp16 半精度**：用 `torch.autocast` 包推理（非 `model.half()`）；因 Apollo 的 STFT `n_fft=882` 非 2 的幂、cuFFT 半精度只支持 2 的幂，故在其 STFT/ISTFT 边界强制回退 fp32（`autocast(enabled=False)` + `.float()`），其余卷积层走 fp16

### `src/models/flashsr_model.py`
**FlashSR 采样率超分 wrapper**（模型源码 vendored 在 `src/models/flashsr_src/`，含 `FlashSR/` + `TorchJaekwon/`，推理码 Apache-2.0、权重承袭 AudioSR/MIT）：
- 一步扩散（distilled latent diffusion + SR-Vocoder + VAE），把任意采样率重建并升频到 **48 kHz**
- 固定 245760 样本（5.12 s）窗 + cos² 淡变 overlap-add；**逐声道串行**处理立体声（每声道 ~6 GB，避免 batch 撑爆 16 GB 显存）
- 加载三件套 `student_ldm.pth` / `sr_vocoder.pth` / `vae.pth`；`load()` 后 dummy 预热；VRAM 预检
- **fp16 半精度**：`torch.autocast` 包裹推理，FFT 边界按需回退 fp32；实测较 fp32 进一步提速、降显存
- 通过 `sys.path` 注入 vendored 包目录，避免污染宿主全局 import

### `src/config/settings.py`
JSON 落盘（`~/.ventiplayer/config.json`）+ 1 秒防抖 timer，避免拖滚动条/调音量时频繁写文件。退出时 `flush()` 强制落盘。

---

## 部署指南

> 这是一个 **MVP**，仓库故意不打包模型权重和原生运行时（权重 ≈ 3.3 GB + 运行时 ≈ 240 MB）。
> 部署流程是：clone 仓库 → 建虚拟环境 → 装依赖 → 运行 `download_models.py` → 手动放置 `libmpv-2.dll` 和 `deno.exe` → 启动。

### 0. 前置条件

- **OS**：Windows 11 (x64)
- **Python**：3.12 及以上（项目 venv 命名 `.venv312`；`run.py` 启动时校验 `>= 3.12`，低于则给出引导并退出）
- **GPU（推荐）**：AMD Radeon RX 7000/9000 系列 + Adrenalin Edition 26.1.1+ 驱动；ROCm 7.2.1 Windows 原生 wheel
  - 没有合适 GPU 时会自动 fallback 到 DirectML 或 CPU；CPU 模式下 Apollo 勉强可用、FlashSR 较慢
- **磁盘**：≈ 2 GB（venv ≈ 13 GB 另算）；字幕用的 Whisper 模型按需另占数百 MB 至数 GB

### 1. 克隆仓库 & 建虚拟环境

```powershell
git clone <你的远端 URL> VentiPlayer
cd VentiPlayer

py -3.12 -m venv .venv312
.\.venv312\Scripts\activate
python -m pip install -U pip
```

### 2. 安装 PyTorch

ROCm 路线（推荐，AMD 用户）：

```powershell
# 参考 AMD 官方页：https://rocm.docs.amd.com/projects/install-on-windows/
# 形如：
pip install torch==2.9 --index-url https://repo.radeon.com/rocm/manylinux/...
```

> ROCm Windows wheel 的具体 index-url 会随版本更新，请去 AMD 文档拿最新链接。

CUDA / CPU 路线：照 https://pytorch.org/get-started/locally/ 选对应命令。

### 3. 安装其他依赖

```powershell
pip install -r requirements.txt
pip install gdown          # download_models.py 需要
pip install onnxruntime    # 可选：让设备检测能识别 DirectML
```

### 4. 下载模型权重

```powershell
python download_models.py
```

这会下载两套增强模型的权重：

| 模型 | 大小 | 落盘位置 |
|---|---|---|
| Apollo `apollo_model_uni.ckpt` + 配置 | ≈ 70 MB | `~/.ventiplayer/models/apollo/` |
| FlashSR `student_ldm.pth` / `sr_vocoder.pth` / `vae.pth` | ≈ 3.2 GB | `~/.ventiplayer/models/flashsr/` |

> Apollo 权重来自 GitHub release（deton24 的 Universal Lossy Enhancer），FlashSR 三件套走 hf-mirror。两套模型的**推理源码已 vendored 在仓库** `src/models/{apollo_src,flashsr_src}/`，无需 pip 安装。已存在的文件跳过，不会重复下载。
>
> 字幕功能用到的 Whisper 模型不在此脚本内，首次在设置中选择 ASR 模型时按需下载到 `~/.ventiplayer/models/whisper/`。

### 5. 放置原生运行时

仓库不上传以下两个二进制（`.gitignore` 会拦），需要自己放到仓库根目录：

| 文件 | 大小 | 用途 | 来源 |
|---|---|---|---|
| `libmpv-2.dll` | ≈ 114 MB | mpv 核心，提供视频解码 + WASAPI Exclusive 输出 | https://mpv.io/installation/ → "libmpv build for Windows" |
| `deno.exe` | ≈ 123 MB | yt-dlp 在 YouTube JS 挑战时调用 | https://deno.com/ → 官方 Windows 下载 |

放好后目录应该长这样：

```
VentiPlayer/
├── libmpv-2.dll      ← 你刚放的
├── deno.exe          ← 你刚放的
├── run.py
├── start.bat
├── src/...
└── ...
```

`run.py` 会自动把仓库根目录加进 `PATH`，所以 mpv 和 deno 在这就能被找到。

### 6. （可选）准备 cookies

需要 Bilibili 大会员 Hi-Res 音轨 / 登录态时：

- 浏览器导出 Netscape 格式 cookies.txt（Edge / Chrome / Firefox 都行）
- 默认放 `cookies/cookies_all.txt`，或在 GUI 设置里改
- 程序会自动 `fix_cookie_file()` 修复格式问题再喂给 yt-dlp

> `cookies/` 目录已在 `.gitignore` 中，不会上传。

### 7. （可选）启用小黄鸭全屏补帧

想用真·补帧（而非零成本的 display-resample 伪插帧）时：

- 安装 [Lossless Scaling](https://store.steampowered.com/app/993090/)（Steam 付费应用，俗称「小黄鸭」）
- 在小黄鸭中把缩放类型设为 **LSFG**（帧生成），并记下其「缩放开关」全局快捷键
- 在 VentiPlayer 设置 →「帧生成 / 小黄鸭」里填写 `LosslessScaling.exe` 的绝对路径，并把快捷键填成与小黄鸭内一致（默认 `Ctrl+Alt+S`）
- 之后在视频增强面板的帧生成下拉选「小黄鸭」，进入全屏即自动开始补帧、退出全屏自动停止
- 建议在 `LosslessScaling.exe` 属性→兼容性里**关闭「以管理员身份运行」**，否则会弹 UAC 框、且其窗口因 Windows UIPI 限制无法被自动最小化

---

## 运行

```powershell
.\start.bat
```

或者手动：

```powershell
.\.venv312\Scripts\python.exe run.py
```

`start.bat` 多做了几件事，建议用它：

- 设 `MIOPEN_USER_DB_PATH` / `MIOPEN_CUSTOM_CACHE_DIR` 到 `%TEMP%\miopen_cache`（避免 ROCm MIOpen 在含中文的用户目录写缓存炸）
- 设 `HF_ENDPOINT=https://hf-mirror.com` 让 huggingface 走国内镜像
- 设 `PYTORCH_ALLOC_CONF=expandable_segments:True` 缓解显存碎片

> 直接 `python run.py` 启动时，`src/main.py` 会用 `tempfile.gettempdir()/miopen_cache` 作为默认（`os.environ.setdefault`），与 `start.bat` 设的 `%TEMP%\miopen_cache` 一致。

启动后：

1. 主窗口出现，状态栏显示检测到的推理后端（如 `ROCm: AMD Radeon RX 9070`）
2. 粘贴 URL 到顶部输入框，回车
3. 默认走原音轨先播；想增强就在右侧面板勾选模式 → 等待 enhanced.wav 写够 → 自动切换音轨

启动时及窗口重新获得焦点时，程序会自动检测剪贴板中受支持的链接（YouTube / Bilibili 视频与直播 / b23.tv 短链 / Twitch VOD 与直播）并填入 URL 栏（仅填入、不自动播放）。用户可先在右侧面板启用音频增强，再手动点击播放。

---

## 仓库布局与 .gitignore 说明

```
VentiPlayer/
├── run.py                # 入口
├── start.bat             # Windows 启动脚本
├── download_models.py    # 一次性下载所有模型
├── requirements.txt
├── PLAN.md               # 设计计划（已与实现对齐）
├── DEBUG_PLAN.md         # 调试历程（已清理完毕，归档）
├── README.md             # 当前文件
├── LICENSE               # MIT
├── src/
│   ├── main.py
│   ├── gui/{main_window,player_widget,enhance_panel,video_enhance_panel,content_browser,playlist_panel,settings_dialog,thumbnail_cache}.py
│   ├── core/{stream,audio_pipe,enhancer,sync,playlist,bilibili_api,subtitle,asr_engine,llm,resource_monitor,frame_gen,lossless_scaling,video_export}.py
│   ├── models/{apollo_model,flashsr_model}.py + {apollo_src,flashsr_src}/  # vendored 模型源码
│   └── config/settings.py
│
├── shaders/                  # GLSL shader 模板与 Anime4K/FSR/FSRCNNX 文件
├── models/.gitkeep           # 占位（实际权重在 ~/.ventiplayer）
│
├── .gitignore
├── .gitattributes
├── .vscode/settings.json # 仅保留 interpreter 路径
│
│ —————— 以下都不入库 ——————
├── .venv312/             ≈ 13 GB
├── libmpv-2.dll          ≈ 114 MB
├── deno.exe              ≈ 123 MB
└── cookies/              用户登录态
```

### `.gitignore` 取舍逻辑

需要平衡两件事：**仓库要小** vs **别人能照着部署**。我的取舍是：

| 类别 | 处理方式 | 理由 |
|---|---|---|
| `.venv312/` | 完全忽略 | 13 GB，且 venv 不可移植，必须各自重建 |
| `libmpv-2.dll` / `deno.exe` | 忽略二进制本体，README 留下载链接 | 总计 240 MB，且 mpv 有官方分发；redistribute 也要遵守 LGPL，让用户自己装更干净 |
| 模型权重（`*.pth` / `*.ckpt` / `*.bin` / `*.safetensors`） | 忽略 + 提供 `download_models.py` | FlashSR 三件套就 3.2 GB，git 里塞会让 clone 永远不结束（模型推理源码 vendored 入库，仅权重不入库） |
| `~/.ventiplayer/` / `~/.cache/` | 忽略（也确实不在 repo 里） | 用户态运行时，多副本共享 |
| `cookies/` | 忽略 | 含登录 token，**绝对不能进 git** |
| `*.wav` / `*.mp4` / `*.log` 等 | 忽略 | 测试时容易顺手扔进仓库 |
| `.vscode/` | 仅保留 `settings.json` | 别人 clone 后 IDE 自动指向 `.venv312`，不需要其他 launch/profiles |
| `models/.gitkeep` | 入库一个空文件 | 让目录结构与代码里的引用对齐，但不传任何权重 |

### 别人 clone 后，部署"补全"流程

仓库里 **不会有** 的东西，`download_models.py` 和「部署指南」都讲清楚了怎么补：

1. **venv** → `py -3.12 -m venv .venv312` + `pip install`
2. **PyTorch** → 按 GPU 类型选官方 wheel
3. **模型权重** → `python download_models.py`（Apollo 走 GitHub release，FlashSR 三件套走 hf-mirror）
4. **`libmpv-2.dll`** → mpv 官网下载
5. **`deno.exe`** → deno 官网下载
6. **cookies**（可选） → 浏览器导出，按需放入 `cookies/`

---

## 已知问题与限制

- **首次 ROCm 推理可能崩** (`LLVM ERROR: Can't get available size`)：Apollo / FlashSR 都在 `load()` 里做 dummy warm-up（先 CPU 建模再 `.to("cuda")`）规避；首次跑较大文件前建议先用小段试一次。
- **FlashSR 显存峰值高**：5.12 s 固定窗 + 逐声道串行处理，立体声仍可能在 RX 9070（16 GB）上吃较多显存；若 OOM 会抛 `VRAM 不足` 并 fallback 回原音轨。
- **字幕生成是内存密集型**：识别用的音频以 16 kHz 单声道 float32 全量驻留内存，超长视频（>200 MB 估算）会在日志预警；LLM 润色依赖外部 OpenAI 兼容服务，未配置时只输出原始 ASR 文本。
- **B 站非大会员**：能播 1080P 但只有 192 kbps AAC——这正是 Apollo 编解码修复最该上的场景（采样率仍 44.1k，但高频被切+有压缩伪影）。
- **YouTube 反爬**：yt-dlp 偶尔需要 deno + cookies 才能解析；解析失败会在 GUI 提示并打日志。
- **Linux / macOS 未测**：依赖中的 ROCm Windows wheel + libmpv-2.dll 都是 Windows 专属；理论上 PyAV / yt-dlp 跨平台可用，但 SyncManager 的 mpv 嵌入和音频后端没有在其他平台验证过。
- **多语言支持**：UI 文案、日志混合中英文。
- **视频导出的 GPU 烘焙路径待真机验证**：离屏 GPU 渲染依赖 Qt 离屏 OpenGL 上下文 + libmpv render API，在无显示设备的 headless 环境会自动退化为 PyAV 近似烘焙（此路径已验证可产出有效 mp4）。GPU 真实烘焙路径的若干细节——`get_proc_address` 取 GL 函数、`flip_y` 帧方向、`frame-step` + `update_cb` 的逐帧确定性、EOF 收尾——需在真实 AMD GPU 上确认无上下颠倒/丢帧。导出耗时随分辨率与超分倍率上升，4K + 超分会明显变慢。
- **B 站直播依赖 yt-dlp BilibiliLive extractor**：如果 yt-dlp 版本过旧或 B站 API 变动导致 extractor 失效，直播功能会暂时不可用。保持 `pip install -U yt-dlp` 更新即可。

---

## License

MIT — see [LICENSE](LICENSE).

第三方组件保留各自许可：

- mpv / libmpv：GPLv2+ / LGPLv2.1+
- yt-dlp：Unlicense
- Apollo (JusperLee/Look2Hear, Tsinghua)：模型源码 CC-BY-SA 4.0（vendored 在 `src/models/apollo_src/`），权重原作者保留版权
- FlashSR (Im & Nam, KAIST)：推理源码 Apache 2.0（laion 再分发，vendored 在 `src/models/flashsr_src/`），权重承袭 AudioSR / MIT
- PySide6：LGPLv3 / GPLv2 / 商业三可选

> 注意 Apollo 为 CC-BY-SA 4.0（copyleft + 署名 + 相同方式共享），个人播放器内使用没问题，若你打算再分发其权重或衍生需遵守 share-alike 条款。

如果你打算 redistribute 二进制构建，请自行核对 mpv / Qt 的 LGPL 合规性。
