# VentiPlayer 视频增强面板方案

> 目标平台：Windows 11 + AMD RX 9070 (RDNA 4) + mpv (libmpv)
> 日期：2026-05-20

---

## 1. 画面基础调整 (Basic Image Adjustments)

mpv 提供直接可用的属性（property），可通过 `mpv.set_property()` 实时调整，无需重建滤镜链。

### 1.1 亮度 / 对比度 / 饱和度 / Gamma

| 参数 | mpv 属性 | 范围 | 说明 |
|------|----------|------|------|
| 亮度 | `brightness` | -100 ~ 100 | 加减亮度偏移 |
| 对比度 | `contrast` | -100 ~ 100 | 对比度增益 |
| 饱和度 | `saturation` | -100 ~ 100 | 色彩饱和度 |
| Gamma | `gamma` | -100 ~ 100 | Gamma 校正 |

**mpv 语法示例：**
```
# 命令行
mpv --brightness=10 --contrast=5 --saturation=10 video.mp4

# IPC / libmpv
set_property("brightness", 10)
set_property("contrast", 5)
```

### 1.2 锐化 (Sharpness)

mpv 没有单一的 `sharpness` 属性，需通过视频滤镜实现：

| 方案 | mpv 语法 | 说明 |
|------|----------|------|
| Unsharp Mask | `--vf=lavfi=[unsharp=5:5:1.0:5:5:0.0]` | FFmpeg unsharp 滤镜，参数为 luma_size:luma_size:luma_amount:chroma_size:chroma_size:chroma_amount |
| CAS (Contrast Adaptive Sharpening) | `--glsl-shaders="~~/shaders/CAS.glsl"` | AMD CAS 的 GLSL 移植，自适应锐化，推荐 |
| adaptive-sharpen | `--glsl-shaders="~~/shaders/adaptive-sharpen.glsl"` | 社区维护的自适应锐化 shader |

**推荐方案：** CAS shader，轻量且效果好，可通过 shader 内参数控制强度。

### 1.3 色温 (Color Temperature)

mpv 没有内置 `colortemperature` 滤镜，需要通过以下方式近似实现：

| 方案 | mpv 语法 | 说明 |
|------|----------|------|
| gamma-red/blue 组合 | `--gamma-red=5 --gamma-blue=-5` | 暖色调：增红减蓝；冷色调反之 |
| vf colorlevels | `--vf=lavfi=[colorbalance=rs=0.1:bs=-0.1]` | FFmpeg colorbalance 滤镜 |
| vf colortemperature | `--vf=lavfi=[colortemperature=temperature=6500]` | FFmpeg 5.0+ 的 colortemperature 滤镜，直接指定色温 K 值 |

**推荐方案：** 优先使用 `colortemperature` 滤镜（需 FFmpeg 5.0+），参数范围 1000K~40000K，6500K 为中性白。若 FFmpeg 版本不支持，降级为 `gamma-red` / `gamma-blue` 组合。

**libmpv 动态切换语法：**
```python
# 添加滤镜
player.command("vf", "add", "lavfi=[colortemperature=temperature=5500]")
# 移除滤镜
player.command("vf", "remove", "@ct")
# 带标签方便管理
player.command("vf", "add", "@ct:lavfi=[colortemperature=temperature=5500]")
```

---

## 2. 高级滤镜 (Advanced Filters)

### 2.1 去色带 (Deband)

色带是低码率视频中常见的渐变区域阶梯状伪影。mpv 内置了高质量去色带功能。

| 参数 | mpv 语法 | 说明 |
|------|----------|------|
| 开关 | `--deband=yes` | 启用去色带 |
| 迭代次数 | `--deband-iterations=4` | 默认 1，越高效果越好但越慢 |
| 阈值 | `--deband-threshold=48` | 默认 48，检测色带的灵敏度 |
| 范围 | `--deband-range=16` | 默认 16，采样半径 |
| 颗粒感 | `--deband-grain=48` | 默认 48，添加噪点掩盖残余色带 |

```python
# libmpv
player.set_property("deband", True)
player.set_property("deband-iterations", 2)
player.set_property("deband-threshold", 64)
```

### 2.2 降噪 (Denoise)

| 方案 | mpv 语法 | 说明 |
|------|----------|------|
| hqdn3d | `--vf=lavfi=[hqdn3d=4:3:6:4.5]` | 高质量 3D 降噪，参数：luma_spatial:chroma_spatial:luma_tmp:chroma_tmp |
| nlmeans | `--vf=lavfi=[nlmeans=s=3:p=7:r=15]` | Non-Local Means 降噪，质量更高但更慢；s=降噪强度, p=patch大小, r=搜索范围 |
| atadenoise | `--vf=lavfi=[atadenoise]` | 自适应时域降噪，适合轻度噪点 |

**推荐：** hqdn3d 作为默认选项（性能好），nlmeans 作为高质量选项。

### 2.3 HDR 色调映射 (HDR Tone Mapping)

用于在 SDR 显示器上正确显示 HDR 内容，或进行 HDR 直通。

| 参数 | mpv 语法 | 说明 |
|------|----------|------|
| 色调映射算法 | `--tone-mapping=bt.2390` | 可选：auto, clip, mobius, reinhard, hable, bt.2390, spline, st2094-10, st2094-40 |
| 动态峰值检测 | `--hdr-compute-peak=yes` | 逐帧计算 HDR 峰值亮度，实现动态色调映射 |
| 目标峰值 | `--target-peak=auto` | SDR 显示器通常 100-300 nit |
| 色彩空间提示 | `--target-colorspace-hint=yes` | 让显示器自动切换 HDR 模式 |
| 色域映射 | `--gamut-mapping-mode=perceptual` | 色域压缩方式 |

**推荐配置（SDR 显示器看 HDR）：**
```
vo=gpu-next
tone-mapping=bt.2390
hdr-compute-peak=yes
target-peak=auto
```

**推荐配置（HDR 直通）：**
```
vo=gpu-next
target-colorspace-hint=yes
```

### 2.4 其他实用滤镜

| 滤镜 | mpv 语法 | 说明 |
|------|----------|------|
| 去隔行 | `--vf=lavfi=[yadif=mode=1]` | 将隔行视频转为逐行 |
| 裁剪黑边 | `--vf=lavfi=[cropdetect]` 然后 `--vf=crop=...` | 自动检测并裁剪黑边 |
| 旋转 | `--video-rotate=90` | 视频旋转 |
| 镜像 | `--vf=hflip` 或 `--vf=lavfi=[hflip]` | 水平翻转 |

---

## 3. 插帧方案 (Frame Interpolation)

### 3.1 Anime4K Shaders

**项目地址：** https://github.com/bloc97/Anime4K

Anime4K 是一组 GLSL shader，专为动画内容设计，在 mpv 中实时运行。**注意：Anime4K 不提供帧插值功能，仅做画面增强/超分。**

#### 核心 shader 分类：

| 类别 | 代表文件 | 功能 |
|------|----------|------|
| 高光钳制 | `Anime4K_Clamp_Highlights.glsl` | 防止过曝，建议始终作为第一个 shader 加载 |
| CNN 超分 | `Anime4K_Upscale_CNN_x2_M.glsl` / `_L` / `_VL` / `_UL` | 2x 神经网络超分辨率，M/L/VL/UL 为不同质量档次 |
| GAN 超分 | `Anime4K_Upscale_GAN_x2_M.glsl` / `_x3_L` / `_x4_UL` | GAN 超分，保留纹理颗粒感更好 |
| 画质修复 | `Anime4K_Restore_CNN_M.glsl` / `_S` / `_L` | 去压缩伪影、修复细节 |
| 线条增强 | `Anime4K_Line_Reconstruction_Light_L.glsl` / `_Medium` / `_Heavy` | 线稿增强、加粗/细化 |
| 降噪 | `Anime4K_Upscale_CNN_x2_M_Denoise.glsl` | 超分同时降噪 |
| 自动缩放 | `Anime4K_AutoDownscalePre_x2.glsl` / `_x4` | 防止过度放大 |
| 去振铃 | `Anime4K_DeRing.glsl` | 去除压缩振铃伪影 |

#### mpv 使用方法：

```
# mpv.conf 中静态加载
glsl-shaders-append="~~/shaders/Anime4K_Clamp_Highlights.glsl"
glsl-shaders-append="~~/shaders/Anime4K_Restore_CNN_M.glsl"
glsl-shaders-append="~~/shaders/Anime4K_Upscale_CNN_x2_M.glsl"

# input.conf 中动态切换（推荐）
CTRL+1 no-osd change-list glsl-shaders set "~~/shaders/Anime4K_Clamp_Highlights.glsl:~~/shaders/Anime4K_Restore_CNN_M.glsl:~~/shaders/Anime4K_Upscale_CNN_x2_M.glsl:~~/shaders/Anime4K_Restore_CNN_S.glsl:~~/shaders/Anime4K_AutoDownscalePre_x2.glsl:~~/shaders/Anime4K_AutoDownscalePre_x4.glsl:~~/shaders/Anime4K_Upscale_CNN_x2_S.glsl"
CTRL+0 no-osd change-list glsl-shaders clr ""
```

#### 性能参考（RX 9070 应无压力）：
- M 档：1080p→4K 可在中端 GPU 实时运行
- L/VL 档：需要 GTX 1060+ / RX 580+ 级别
- UL 档：需要高端 GPU

#### 结论：
Anime4K **没有帧插值 shader**，仅用于画面超分和增强。帧插值需要借助其他方案。

### 3.2 AMD FSR (FidelityFX Super Resolution) GLSL Shader

**社区移植地址：** https://gist.github.com/agyild/82219c545228d70c5604f865ce0b0ce5

AMD FSR **1.0** 已被社区移植为 mpv 可用的 GLSL shader，**与 GPU 品牌无关**，在任何 GPU 上均可运行。

#### 为什么只有 FSR 1.0？

> FSR 2.0 / 3.0 是**时域超分辨率**（temporal upscaler），需要游戏引擎提供逐帧运动矢量（motion vectors）。
> 视频播放是"解码-呈现"模式，帧已经预渲染好，不存在引擎级运动矢量。
> **FSR 2/3 在架构上不可能用于视频播放**——这不是"没人移植"，而是原理不允许。
> （同理 DLSS 也无法用于视频超分。）
>
> — agyild (FSR shader 作者) 在 gist 评论区明确说明 (2025-01)

#### 包含两个 shader：
1. **FSR.glsl (EASU)** — Edge-Adaptive Spatial Upsampling，边缘自适应空间超采样
2. **CAS-scaled.glsl (RCAS)** — Contrast Adaptive Sharpening，对比度自适应锐化

#### mpv 使用方法：
```
# 放入 shaders 目录后在 mpv.conf 中加载
glsl-shaders-append="~~/shaders/FSR.glsl"
glsl-shaders-append="~~/shaders/CAS-scaled.glsl"
```

#### 注意事项：
- FSR 1.0 仅做空间超分（类似超采样），**不做帧插值**
- 最大支持 4x 放大（如 1080p→4K）
- 对于 < 2x 放大场景效果最佳
- 比 Anime4K 更适合真人影视内容
- RX 9070 运行毫无压力

#### 更好的替代方案（神经网络空间超分）：
- **FSRCNNX** (`FSRCNNX_x2_16-0-4-1.glsl`) — 质量优于 FSR 1.0，mpv 社区主流选择
- **ArtCNN** — 较新的 CNN 超分 shader
- **RAVU** — 基于 RAISR 的快速超分

### 3.3 mpv 内置插帧 (Built-in Interpolation)

mpv 内置的 interpolation 功能通过 **重新分配已有帧的显示时机** 来减少卡顿感（judder），**不生成新帧**。本质是 frame blending / nearest-neighbor 在时域上的操作。

#### 核心配置：
```
video-sync=display-resample    # 将视频同步到显示器刷新率
interpolation=yes              # 启用帧间时域插值
tscale=oversample              # 时域缩放算法
```

#### tscale 算法对比：

| 算法 | 效果 | 适用场景 |
|------|------|----------|
| `oversample` | 最近邻，无混合，保持锐利 | 动画、像素风 |
| `triangle` | 线性混合，锐利但有轻微重影 | 通用，推荐折中 |
| `mitchell` | 平滑但可能糊 | 追求极致平滑 |
| `gaussian` | 高斯混合 | 较平滑 |
| `bicubic` | 三次混合，最模糊 | 不推荐 |
| `sphinx` | Sphinx 窗口函数 | 曾被建议作为默认 |

#### 关键参数：
```
interpolation-threshold=-1     # 强制始终插值（默认只在帧率差距大时启用）
video-sync-max-video-change=5  # 允许的最大速度调整百分比
tscale-blur=0.7               # 模糊系数，0.1(锐利)~1.2(模糊)
```

#### 局限：
- **不是真正的帧生成**，不会基于运动矢量创建新帧
- 对 24fps→60Hz 的场景可以显著减少 3:2 pulldown judder
- 对 24fps→120Hz 效果更好（整数倍）
- 不适合替代 SVP/RIFE 等真正的运动补偿插帧

### 3.4 SVP (SmoothVideo Project)

**官网：** https://www.svp-team.com/

SVP 是最成熟的实时视频插帧软件，使用运动补偿算法（MVTools）或 AI（RIFE）生成中间帧。

#### 核心信息：
- **价格：** Windows/macOS 需要 SVP Pro（$24.99 终身授权，30 天免费试用）；Linux 免费
- **最新版本：** 4.7.302 (2025-08-20)
- **支持播放器：** mpv, VLC, MPC-HC, PotPlayer, Plex, IINA 等
- **插帧引擎：**
  - MVTools（传统光流法，CPU 为主）
  - NVIDIA Optical Flow（NVIDIA GPU 加速）
  - **RIFE AI**（ncnn/Vulkan，支持 AMD GPU）

#### 与 mpv 集成方式：

1. 安装 SVP 后通过 "Additional programs and features" 安装 mpv 支持
2. mpv.conf 中添加：
```
input-ipc-server=mpvpipe    # Windows
hwdec=auto-copy             # 必须用 copy-back 硬解以兼容 VapourSynth
hwdec-codecs=all
hr-seek-framedrop=no        # 修复音画不同步
no-resume-playback          # 禁用 watch later（与 SVP 冲突）
```
3. SVP 会通过 VapourSynth 滤镜链自动接管帧处理

#### RIFE 引擎（对 AMD 用户关键）：
- 使用 ncnn + Vulkan 后端，AMD GPU 原生支持
- 需要较强 GPU（RX 9070 应能胜任 1080p 实时 RIFE）
- 4K RIFE 实时可能吃力，取决于模型版本
- 推荐模型：rife-v4.6（质量好，速度快）

#### 评估：
- **优点：** 成熟、UI 友好、多引擎可选、与 mpv 深度集成
- **缺点：** 付费软件、引入额外复杂度、动作场景可能有伪影
- **适合用户：** 追求开箱即用的高质量插帧体验

### 3.5 RIFE (VapourSynth 方案)

**项目地址：** https://github.com/styler00dollar/VapourSynth-RIFE-ncnn-Vulkan

不通过 SVP，直接使用 VapourSynth + RIFE 滤镜实现 mpv 实时帧插值。

#### 工作原理：
mpv → VapourSynth 脚本 (.vpy) → RIFE ncnn Vulkan 滤镜 → 输出插值帧

#### 安装需求（Windows）：
1. Python 3.10+
2. VapourSynth R60+
3. vapoursynth-plugin-rife-ncnn-vulkan
4. RIFE 模型文件（rife-v4.6 推荐）
5. mpv 编译时启用 VapourSynth 支持

#### VapourSynth 脚本示例：
```python
import vapoursynth as vs
from vapoursynth import core

clip = video_in  # mpv 传入的视频流
clip = core.rife.RIFE(clip, model=9, factor_num=2, gpu_id=0, gpu_thread=2)
clip.set_output()
```

#### mpv 配置：
```
vf=vapoursynth="~~/scripts/rife.vpy"
hwdec=auto-copy
```

#### 性能评估（RX 9070）：
- 1080p 24fps→48fps：应该可以实时
- 1080p 24fps→60fps：可能边缘（取决于模型）
- 4K 实时插帧：**极其困难**，需降低模型精度
- rife-v4.6 + ncnn/Vulkan 是 AMD GPU 的最佳组合

#### 对比 SVP 中的 RIFE：
- 优点：免费、可深度定制
- 缺点：配置复杂、无 GUI、需要自己管理模型和脚本

---

## 4. 驱动级帧生成 (Driver-Level Frame Generation) — 重点研究

### 4.1 AMD AFMF (Fluid Motion Frames) / AFMV (Fluid Motion Video)

#### 技术背景

AMD 有两个不同但相关的技术：

1. **AMD Fluid Motion Video (AFMV/FMV)**：2014 年随 GCN 2.0 架构发布，专门用于视频播放器的帧插值。利用 GPU 中的专用 ASIC 硬件（与 CyberLink 合作开发）。**仅支持 Polaris/GCN 架构，RDNA 系列已彻底放弃此技术。**

2. **AMD Fluid Motion Frames (AFMF)**：2023 年发布，驱动级帧生成技术。当前版本为 AFMF 2.1，AFMF 3 在开发中。**设计目标是游戏，不是视频播放。**

#### AFMF 技术细节

- **支持硬件：** RX 6000/7000/9000 系列（RX 9070 完全支持）
- **API 支持：** DirectX 11, DirectX 12, Vulkan, OpenGL
- **显示模式：** 支持全屏独占和无边框全屏（AFMF 2.1 新增）
- **工作原理：** 在驱动层面截获渲染帧，通过 AI 生成中间帧插入
- **定位：** 明确定位为游戏技术，通过 Adrenalin 软件的"游戏"标签启用

#### AFMF 能否用于视频播放器？

**官方立场：不支持。** AFMF 是游戏帧生成技术。

**社区 Hack 尝试（不稳定、不推荐）：**

根据 GitHub 讨论（mpv-player/mpv #16613）和社区实验：

1. **MPC-HC + DXVK 方案（历史方法）：**
   - 将 `dxgi.dll` 和 `d3d9.dll`（DXVK）放入 MPC-HC 目录，将 DX9 渲染转译为 Vulkan
   - 在 Adrenalin 中将 MPC-HC 添加为"游戏"
   - 启用 AFMF
   - **问题：** 仅在早期 Preview 2 驱动中有效，正式版驱动和后续预览版均不工作

2. **mpv + Vulkan 方案（理论上）：**
   - `mpv --vo=gpu-next --gpu-api=vulkan --fs`
   - 将 `mpv.exe` 添加到 Adrenalin 游戏列表
   - 启用 AFMF
   - **现实：mpv 讨论明确表示"Nope for mpv"。** AFMF 需要应用以特定方式呈现帧（类似游戏渲染循环），视频播放器的帧呈现模式与游戏不同，驱动无法正确拦截和插入帧。

3. **AMF SDK FRC 方案（最有希望但未实现）：**
   - AMD 的 AMF (Advanced Media Framework) SDK 中存在 FRC (Frame Rate Conversion) 组件
   - 专为视频设计的帧插值 API
   - **问题：** 仅支持 DX12 或 OpenCL 上下文。mpv 没有 DX12 渲染后端，OpenCL 集成过于复杂
   - 尚未集成到 FFmpeg 中
   - mpv 开发者认为短期内不太可能实现

#### 可行性结论

| 方案 | 可行性 | 说明 |
|------|--------|------|
| AFMF 直接用于 mpv | ❌ 不可行 | 驱动不识别视频播放器的帧呈现模式 |
| 将 mpv 添加为游戏 | ❌ 不可行 | 即使识别为游戏，帧时序不匹配 |
| AMF SDK FRC | 🟡 理论可行 | 需要 mpv 增加 DX12 后端或 OpenCL 集成，社区无人推动 |
| 旧版 AFMV (Fluid Motion Video) | ❌ 不可行 | RDNA 架构已移除此硬件，RX 9070 不支持 |

### 4.2 AMD RSR (Radeon Super Resolution)

#### 定位
RSR 是驱动级的空间超分辨率（基于 FSR 1.0 算法），**仅用于画面放大，不做帧生成**。

#### 能否用于视频播放器？

**官方限制：**
- 仅适用于运行在全屏独占或无边框全屏模式下的"游戏"
- 应用分辨率必须低于显示器原生分辨率时才会触发
- 通过 Adrenalin 软件 → Graphics → RSR 启用

**对视频播放器的适用性：**
- RSR 的触发条件是"应用以低于原生分辨率运行"，视频播放器在全屏播放时通常以原生分辨率运行窗口，内部视频缩放由播放器自身完成
- **实际上对视频播放器无意义**——视频播放器不需要 RSR，因为 mpv 自身的缩放器（lanczos、ewa_lanczos、FSRCNNX 等）远优于 FSR 1.0 的质量
- 即使能触发，RSR 作用于整个窗口（包括 UI、字幕），而非仅视频画面

#### 结论
**对 VentiPlayer 无实用价值。** mpv 内置的缩放器和 shader 方案（FSR.glsl、FSRCNNX、Anime4K）在视频场景下效果远优于驱动级 RSR。

### 4.3 NVIDIA DLSS Frame Generation

#### 技术概述
- DLSS Frame Generation（帧生成）需要 RTX 40/50 系列 GPU 的专用 Optical Flow Accelerator 硬件
- DLSS Multi Frame Generation（多帧生成，RTX 50 系列专属）可生成最多 5 帧/渲染帧
- DLSS 4.5 Dynamic Multi Frame Generation 可动态调整生成帧数

#### 能否用于桌面应用/视频播放器？

**不能。** DLSS Frame Generation 需要：
1. 游戏/应用原生集成 DLSS SDK
2. 或通过 NVIDIA App 的 DLSS Override 功能（仅支持已知游戏列表）
3. 需要 GPU 以传统渲染管线（game loop）方式工作
4. 视频播放器的帧呈现方式与游戏完全不同

NVIDIA 的 125+ 支持列表全部是游戏和 3D 应用（如 Blender、DaVinci Resolve），**没有通用视频播放器**。

#### 结论
**对视频播放器完全不可用。** 且用户使用 AMD GPU，此项仅作参考。

### 4.4 综合可行性评估

> **结论：截至 2026 年 5 月，没有任何驱动级帧生成技术可以可靠地用于视频播放器。**

原因分析：
1. 帧生成技术设计时假设应用以"渲染循环"方式工作（每帧 GPU 主动渲染），而视频播放器是"解码-呈现"模式（GPU 仅负责显示已解码的帧）
2. 驱动需要理解帧间的运动信息才能生成中间帧，游戏中这来自渲染管线的运动矢量，视频播放中这些信息不以相同方式暴露
3. AMD 已有专门的视频 FRC 技术（AMF SDK），但尚未被任何开源播放器集成

**对 VentiPlayer 的建议：**
- 帧插值应通过软件层面实现（SVP/RIFE + VapourSynth）
- 不要尝试依赖驱动级方案，不稳定且随时可能失效
- mpv `--vo=gpu-next --gpu-api=vulkan` 是最佳渲染后端选择，但这是为了画质，不是为了触发 AFMF

---

## 5. UI 设计建议

### 5.1 整体布局

视频增强面板应作为一个可折叠的 `QGroupBox`，放置在现有音频增强面板（`src/gui/enhance_panel.py` 中的 `EnhancePanel`）下方。

```
┌─────────────────────────────────┐
│ 🎵 音频增强 (现有)               │  ← EnhancePanel (已实现)
├─────────────────────────────────┤
│ 🎬 视频增强                      │  ← VideoEnhancePanel (新增)
│  ┌─ 基础调整 ──────────────────┐ │
│  │ [✓] 启用                    │ │
│  │ 亮度:  ──●──────── [+10]    │ │
│  │ 对比度: ────●────── [+5]    │ │
│  │ 饱和度: ──────●──── [+15]   │ │
│  │ 色温:   ────●────── [5500K] │ │
│  │ 锐化:   ──●──────── [0.5]  │ │
│  └─────────────────────────────┘ │
│  ┌─ 高级滤镜 ─────────────────┐ │
│  │ [✓] 去色带  强度: ──●──     │ │
│  │ [ ] 降噪    模式: [hqdn3d▼] │ │
│  │ [ ] HDR映射 算法: [bt.2390▼]│ │
│  └─────────────────────────────┘ │
│  ┌─ 插帧 ─────────────────────┐ │
│  │ [✓] 启用                    │ │
│  │ 方案: [mpv内置▼]            │ │
│  │       [SVP (需安装)]        │ │
│  │       [RIFE (VapourSynth)]  │ │
│  │ tscale: [oversample▼]      │ │
│  └─────────────────────────────┘ │
│  ┌─ Shader 预设 ──────────────┐ │
│  │ [ ] Anime4K (动画优化)      │ │
│  │ [ ] FSR (通用超分)          │ │
│  │ [ ] FSRCNNX (神经网络超分)  │ │
│  └─────────────────────────────┘ │
└─────────────────────────────────┘
```

### 5.2 交互设计要点

1. **每个子区域独立开关**：用 `QCheckBox` 控制启用/禁用，禁用时灰化内部控件
2. **滑块实时预览**：亮度/对比度/饱和度/色温的滑块拖动时实时通过 `set_property()` 生效
3. **滤镜延迟应用**：降噪等重滤镜在松开滑块后才应用（避免卡顿）
4. **预设系统**：提供"动画优化"、"影视优化"、"HDR 观影"等一键预设
5. **重置按钮**：每个子区域提供"恢复默认"按钮
6. **状态指示**：显示当前活跃的 shader 数量和 vf 滤镜链

### 5.3 实现架构

```python
# 新文件：src/gui/video_enhance_panel.py
class VideoEnhancePanel(QWidget):
    """视频增强控制面板"""
    
    # 信号
    property_changed = Signal(str, object)  # (property_name, value)
    vf_changed = Signal(list)               # 完整 vf 滤镜列表
    shader_changed = Signal(list)           # 完整 shader 列表
    
    def __init__(self, parent=None): ...
```

与 mpv 的通信通过 libmpv 的 property 系统和命令接口：
- 基础调整 → `player.set_property("brightness", value)` 等
- 滤镜 → `player.command("vf", "set", filter_chain)`
- Shader → `player.command("change-list", "glsl-shaders", "set", shader_list)`

---

## 6. 实现优先级

### Phase 1：基础画面调整（难度：低，价值：高）
**预计工时：1-2 天**

- [x] 亮度/对比度/饱和度/Gamma 滑块（直接 mpv property，零风险）
- [x] 锐化开关 + 强度（CAS shader）
- [x] 去色带开关 + 参数（mpv 内置 deband）
- [x] 重置按钮

**理由：** 全部基于 mpv 已有功能，实现简单，用户感知明显。
**注：** 色温模块使用频率低，暂不实现。

### Phase 2：高级滤镜 + Shader 预设（难度：中，价值：高）
**预计工时：2-3 天**

- [x] 降噪滤镜选择（hqdn3d / nlmeans）+ 强度参数
- [x] HDR 色调映射配置面板
- [x] Anime4K shader 预设（一键加载/卸载）
- [x] FSR / FSRCNNX shader 预设
- [ ] Shader 文件管理（下载、检测是否存在）

**理由：** 需要管理外部 shader 文件，但核心逻辑仍是 mpv 命令调用。

### Phase 3：mpv 内置插帧（难度：低，价值：中）
**预计工时：0.5 天**

- [ ] video-sync=display-resample 开关
- [ ] interpolation 开关
- [ ] tscale 算法选择下拉框
- [ ] interpolation-threshold 参数

**理由：** 仅需设置 mpv property，但效果有限（不是真正帧生成）。

### Phase 4：RIFE/SVP 集成（难度：高，价值：高）— ⏸️ 暂不执行
**预计工时：5-7 天**

- [ ] 检测系统是否安装 VapourSynth
- [ ] 检测/下载 RIFE 模型
- [ ] VapourSynth 脚本生成和管理
- [ ] SVP 检测和配置引导
- [ ] 性能监控（实际帧率显示）

**理由：** 涉及外部依赖管理，需要处理各种安装状态，但这是唯一可靠的真正帧插值方案。
**状态：** 暂不执行，待 Phase 1-3 稳定后再考虑。

### Phase 5：驱动级方案探索（难度：极高，价值：不确定）— ⏸️ 暂不执行
**预计工时：研究性质，不建议投入**

- [ ] 监控 AMD AMF SDK FRC 的 FFmpeg 集成进展
- [ ] 如果 FFmpeg 集成了 AMF FRC，通过 `--vf=lavfi=[amf_frc]` 接入
- [ ] 关注 AFMF 3 是否会开放视频播放器支持

**理由：** 当前不可行，但值得持续关注。AMD 有可能在未来版本中为视频播放器提供官方支持。
**状态：** 暂不执行，纯研究性质。

---

## 附录：推荐的 mpv 渲染后端配置

针对 RX 9070 (RDNA 4) 的最佳基础配置：

```ini
# 渲染后端
vo=gpu-next
gpu-api=vulkan
hwdec=auto-copy-safe

# 缩放器
scale=ewa_lanczos
cscale=ewa_lanczos
dscale=mitchell
correct-downscaling=yes
sigmoid-upscaling=yes

# 去色带
deband=no  # 默认关闭，由 UI 控制开启

# 色彩管理
target-colorspace-hint=auto
```

`gpu-next` + `vulkan` 是 mpv 在 AMD GPU 上的最佳组合，提供最完整的功能支持（HDR、色彩管理、高级 shader 兼容性）。
