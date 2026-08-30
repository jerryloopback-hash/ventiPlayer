# VentiPlayer — 实时音频增强流媒体播放器

## 项目定位

面向音乐发烧友的流媒体播放工具。输入 YouTube/B站 URL，自动获取最高质量音频流，经 AI 音频超分辨率增强后，通过 WASAPI Exclusive 输出到 DAC，实现接近 Hi-Res 的听感。

## 核心约束

- GPU: AMD RX 9070 (RDNA 4, gfx1200)
- 推理后端: **Windows 原生 PyTorch + ROCm 7.2.1**（首选）；DirectML 作为 fallback
- 最大启动延迟: 15 秒（FastWave 实时模式）；AudioSR 精听模式不受此限制
- 主要场景: 音乐类视频（MV、live、翻唱等）
- 界面: PySide6 GUI（带音频增强面板）
- 操作系统: Windows 11

## 推理后端决策（2026-05 更新）

经搜索确认，**ROCm 7.2.1 已官方支持 RX 9070 在 Windows 11 上原生运行 PyTorch 2.9**。

| 方案 | 状态 | 性能 | 配置复杂度 |
|------|------|------|-----------|
| **Windows + PyTorch ROCm 7.2.1** | 官方支持 (gfx1200) | 最高 | 需安装 Adrenalin 26.1.1 驱动 |
| WSL2 + ROCm | 官方列表支持，但有已知 bug | 高 | 中等（有 GPU 检测问题报告） |
| Windows + DirectML | 维护模式⚠️ | 中等 | 零配置 |
| Windows + ONNX Runtime DirectML | 活跃维护 | 中等 | 低 |

**结论**：首选 Windows 原生 ROCm。安装步骤：
1. 更新 AMD 驱动到 Adrenalin Edition 26.1.1+
2. `pip install torch==2.9 --index-url` (ROCm 7.2.1 Windows wheel)
3. 代码中使用 `torch.device("cuda")` 即可（ROCm 在 Windows 上也映射为 cuda）

如果 ROCm 路线遇到问题，fallback 到 ONNX Runtime + DirectML（仍然活跃维护，不同于 torch-directml）。

## 双模式设计

| 模式 | 模型 | 延迟 | 适用场景 |
|------|------|------|----------|
| **实时模式** | FastWave (1.3M params) | ≤15s 启动，之后流式 | 日常听歌、看 MV |
| **精听模式** | AudioSR (~400M params) | 预处理完整音频后播放 | 精听一首歌，愿意等 |

## 架构设计

```
┌──────────────────────────────────────────────────────────────┐
│                        VentiPlayer GUI                        │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │  URL 输入栏 | [播放] [停止]                              │ │
│  │  视频画面 (mpv 嵌入窗口)                                 │ │
│  │  进度条 / 音量                                           │ │
│  │  状态栏: 缓冲进度 | 推理速度 | 源码率 | 输出采样率        │ │
│  └─────────────────────────────────────────────────────────┘ │
│  ┌──────────────── 音频增强面板 ───────────────────────────┐ │
│  │  [x] 启用音频增强                                       │ │
│  │  模式: ○ 实时 (FastWave) ○ 精听 (AudioSR)              │ │
│  │  输出采样率: 48kHz                                      │ │
│  │  WASAPI Exclusive: [x] 启用                             │ │
│  │  输出设备: [下拉选择 DAC]                                │ │
│  │  推理后端: DirectML ▼ (预留 ROCm)                       │ │
│  └─────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────┘
```

## 数据流

### 实时模式 (FastWave)

```
用户输入 URL → yt-dlp 解析 (~2-3s)
    │
    ├── 视频流 URL ──→ mpv 嵌入窗口渲染
    │
    └── 音频流 URL ──→ FFmpeg 解码为 PCM float32
                          │
                          ▼
                    分块缓冲器（chunk = 5s, 预缓冲 2 chunks）
                          │
                          ▼
                    FastWave 推理 (DirectML, ~2-4s/chunk)
                          │
                          ▼
                    增强后 PCM 48kHz → WASAPI Exclusive
                    同步到 mpv 视频时间轴
```

启动延迟预算: yt-dlp 解析 3s + 首 chunk 解码 1s + 推理 4s + 缓冲 2s ≈ 10s（留 5s 余量）

### 精听模式 (AudioSR)

```
用户输入 URL → yt-dlp 解析
    │
    └── 音频流 URL ──→ FFmpeg 解码完整音频
                          │
                          ▼
                    AudioSR 全量推理 (显示进度条)
                          │
                          ▼
                    增强后完整音频缓存到内存/临时文件
                          │
                          ▼
                    mpv 播放增强后音频 + 视频流 (WASAPI Exclusive)
```

## 技术选型

| 组件 | 选择 | 理由 |
|------|------|------|
| 流解析 | yt-dlp (subprocess) | B站/YouTube 全支持，cookie 注入 |
| 视频渲染 | mpv (python-mpv / libmpv) | 嵌入 Qt 窗口，WASAPI 原生支持 |
| 音频解码 | PyAV (FFmpeg binding) | 流式解码，纯 Python |
| AI 模型 (实时) | FastWave | 1.3M 参数，50 GFLOPs，适合流式 |
| AI 模型 (精听) | AudioSR | 高质量扩散模型，any→48kHz |
| GPU 推理 (首选) | PyTorch 2.9 + ROCm 7.2.1 (Windows) | 官方支持 RX 9070，性能最高 |
| GPU 推理 (fallback) | ONNX Runtime + DirectML | 如 ROCm 有兼容问题时使用 |
| 音频输出 | mpv WASAPI Exclusive | 通过 libmpv 的 ao=wasapi + exclusive |
| GUI 框架 | PySide6 (Qt6) | 成熟，mpv 窗口嵌入方案成熟 |
| 音视频同步 | mpv 内部同步 + 外部 PTS 管理 | mpv 本身有强大的同步能力 |

## 分阶段实现

### Phase 1: 基础播放器骨架
- PySide6 GUI 框架搭建（主窗口 + mpv 嵌入）
- yt-dlp 集成（URL 解析、B站 cookie、自动选最高音频码率）
- mpv 配置 WASAPI Exclusive 输出
- 基本播放控制（播放/暂停/停止/进度/音量）
- 输出设备选择
- **交付物**: 能播放 B站/YouTube 链接，WASAPI Exclusive 输出，无 AI 增强
- **验收标准**: 输入 B站/YouTube URL，15 秒内开始播放，音频独占 DAC

### Phase 2: AI 音频增强引擎
- FastWave 模型部署 + DirectML 推理验证
- AudioSR 模型部署 + DirectML 推理验证
- 音频分块推理管道（解码 → chunk 切分 → GPU 推理 → 拼接）
- 实时模式：预缓冲 + 流式推理
- 精听模式：全量推理 + 进度显示
- **交付物**: 两种模式均可工作，音频增强效果可听
- **验收标准**: FastWave 实时模式 ≤15s 启动；AudioSR 精听模式有进度条

### Phase 3: 音视频同步 + 稳定性
- 实时模式下音频 PTS 与视频 PTS 对齐
- 动态缓冲区管理（推理速度波动时自适应）
- seek 操作时缓冲区重建
- 音频增强开/关无缝切换（不中断播放）
- 错误恢复（网络中断、推理失败时 fallback 到原始音频）
- **交付物**: 稳定的同步播放体验
- **验收标准**: 连续播放 30 分钟无 A/V 不同步

### Phase 4: GUI 完善 + 打磨
- 音频增强面板完整实现
- 状态栏（推理速度 RTF、缓冲深度、源/输出采样率）
- 配置持久化（QSettings）
- 播放列表支持（多 URL 连续播放）
- 快捷键绑定
- **交付物**: 完整可用的播放器

## B站大会员支持

- 通过 yt-dlp 的 `--cookies-from-browser` 或 `--cookies` 参数注入登录态
- 自动选择最高可用音频质量（Hi-Res > 杜比 > 高码率 > 标准）
- 即使源已是高码率，AI 增强仍可选择性启用（面板控制）
- 大会员 Hi-Res 音频 + WASAPI Exclusive 已经是很好的基线

## 推理后端策略

```python
# 后端选择逻辑
def get_device():
    # 首选：Windows 原生 ROCm (PyTorch 2.9 + ROCm 7.2.1)
    if torch.cuda.is_available():
        return torch.device("cuda")  # ROCm 在 Windows 上映射为 cuda
    
    # Fallback：ONNX Runtime + DirectML
    # 将模型导出为 ONNX，使用 onnxruntime-directml 推理
    return "onnx-directml"
```

环境要求：
- AMD Adrenalin Edition 驱动 ≥ 26.1.1
- PyTorch 2.9 (ROCm 7.2.1 Windows wheel)
- Python 3.12

## 已知风险与缓解

| 风险 | 影响 | 缓解方案 |
|------|------|----------|
| RX 9070 + ROCm Windows 兼容问题 | 推理不可用 | Fallback 到 ONNX Runtime DirectML；或 CPU 推理 |
| AudioSR 模型过大无法加载 | 精听模式不可用 | 提供模型量化版本；或仅保留 FastWave |
| 音视频不同步 | 听感差 | mpv 内部同步 + 外部 PTS 校正双保险 |
| AI 高频引入不自然谐波 | 音乐听感变差 | 提供 dry/wet 混合比例滑块 |
| yt-dlp B站接口变动 | 无法解析 | yt-dlp 自动更新机制 |

## 目录结构（实现后）

```
20260517-VentiPlayer/
├── run.py                   # 顶层启动脚本（设置 PATH/环境后调用 src.main）
├── start.bat                # Windows 启动脚本（设 MIOPEN/HF/PYTORCH 环境变量）
├── download_models.py       # 一次性下载模型到 ~/.ventiplayer 与 HF cache
├── src/
│   ├── main.py              # QApplication 启动 + 启动画面 + 后台预热设备检测
│   ├── gui/
│   │   ├── main_window.py   # 主窗口：URL 栏、视频区、增强面板、状态栏
│   │   ├── player_widget.py # mpv 嵌入组件（QTimer 轮询代替事件回调）
│   │   └── enhance_panel.py # 音频增强面板（含播放标记进度条）
│   ├── core/
│   │   ├── stream.py        # yt-dlp 解析 + 多进程 cookie 修复 + B 站 VIP 嗅探
│   │   ├── audio_pipe.py    # 解码→分块→增强 worker（实时 / 精听两条管道）
│   │   ├── enhancer.py      # 设备检测 + 模型懒加载分发
│   │   └── sync.py          # A/V 漂移监测 + 软速度修正 + 硬重定位
│   ├── models/
│   │   ├── fastwave.py      # FastWave 内置实现（NuWave2+EDM）
│   │   └── audiosr_model.py # 调用 audiosr pip 包，含若干 monkey patch
│   └── config/
│       └── settings.py      # JSON 配置 + 防抖落盘
├── libmpv-2.dll             # mpv 运行时（不入库，需用户自行放置）
├── deno.exe                 # yt-dlp 解析 YouTube JS 用的 JS runtime（不入库）
├── cookies/                 # 用户 cookies.txt（不入库）
├── models/                  # 占位目录（实际权重在 ~/.ventiplayer/models）
├── requirements.txt
├── PLAN.md / DEBUG_PLAN.md
└── README.md
```

模型实际落盘位置：

```
~/.ventiplayer/
├── config.json                       # 用户设置
└── models/fastwave/checkpoint.pth    # FastWave 权重（约 16 MB）

~/.cache/huggingface/hub/
├── models--haoheliu--audiosr_basic/  # AudioSR 权重（约 1.6 GB）
└── models--roberta-base/             # CLAP tokenizer 依赖
```

> ⚠ `download_models.py` 当前把 FastWave 权重写为 `fastwave_checkpoint.pt`，
> 而 `enhancer.py` / `fastwave.py` 期望 `checkpoint.pth`，
> 部署时需要手动把文件 rename 一次（README 已记录）。

## 依赖清单（实际 requirements.txt）

```
PySide6>=6.6       # GUI
python-mpv>=1.0    # mpv 控制
yt-dlp>=2024.0     # 流解析
numpy>=1.24

# Phase 2: AI Audio Enhancement
torch>=2.4         # 推理框架（实测使用 2.9 + ROCm 7.2.1 Windows wheel）
audiosr>=0.0.7    # 精听模式模型（haoheliu/versatile_audio_super_resolution）
av>=12.0          # PyAV 音频解码
soundfile>=0.12   # 增强音频写 WAV
```

未列出但被间接需要：`gdown`（download_models 用）、`onnxruntime`（DirectML 后端探测时尝试导入）。

## 已完成 vs Plan 偏差

实际开发后与最初设计的主要差异：

1. AudioSR 改为直接调用上游 `audiosr` pip 包，并加了大量 monkey patch
   解决 Windows ROCm 下 `torch.distributed`、`torchaudio` 加载、CLAP 的
   `roberta-base` 解析、以及 lowpass `cutoff >= nyquist` 时的 ZPK 不稳定。
2. FastWave 用纯 PyTorch 内置（NuWave2 backbone + EDM precond + EDM Euler ODE 采样器），
   单文件 `fastwave.py`，不依赖外部 pip 包。
3. 同步采用 SyncManager 在后台线程每 0.5 秒抓 `audio_pts` vs `time_pos`，按
   30/80 ms 阈值做软速度修正 / 硬 seek，并在 enhanced 边界附近自动 fallback。
4. 用户态环境放在 `~/.ventiplayer/` 而不是 repo 内 `models/`，便于多副本共享权重。
5. 启动通过 `start.bat` 设置 MIOPEN cache、HF mirror、PyTorch allocator 等。
6. 增加了 cookie 自动 fix（Netscape 格式 domain flag 不一致导致 Python 3.14
   cookiejar 拒绝加载）和 B 站 nav API VIP 嗅探。
