# VentiPlayer 播放体验升级计划

> 日期：2026-05-20
> 目标：消除"每个视频都要从浏览器复制 URL"的糟糕体验，实现连续播放、内容发现、队列管理

---

## 现状

当前播放流程：浏览器复制 URL → 粘贴到输入框 → 解析播放。无播放列表、无"下一个"、无循环、无内容发现。

## B站内容组织模型

| 概念 | 说明 | 场景 |
|------|------|------|
| 分P | 单个视频的多个分段 | 教程、长视频分段 |
| 合集 (Season) | UP主创建的有序视频集 | 番剧式连载、系列教程 |
| 系列 (Series) | UP主的松散视频列表 | 同主题视频归类 |
| 收藏夹 (Favorites) | 用户自己收藏的视频 | 个人歌单、稍后看 |
| 相关推荐 | 每个视频的推荐列表 | 发现类似内容 |
| 首页推荐 | 个性化推荐流 | 无目标浏览 |

## 核心 API 端点

```
# 视频详情（含分P列表、所属合集信息）
GET https://api.bilibili.com/x/web-interface/view?bvid=xxx

# 相关视频推荐
GET https://api.bilibili.com/x/web-interface/archive/related?bvid=xxx

# 合集视频列表
GET https://api.bilibili.com/x/polymer/web-space/seasons_archives_list?mid=xxx&season_id=xxx&page_num=1&page_size=30

# 系列视频列表
GET https://api.bilibili.com/x/series/archives?mid=xxx&series_id=xxx&sort=desc&pn=1&ps=30

# 收藏夹内容
GET https://api.bilibili.com/x/v3/fav/resource/list?media_id=xxx&pn=1&ps=20

# 用户收藏夹列表
GET https://api.bilibili.com/x/v3/fav/folder/created/list-all?up_mid=xxx

# 首页推荐
GET https://api.bilibili.com/x/web-interface/index/top/rcmd?fresh_type=3&ps=10

# 热门视频
GET https://api.bilibili.com/x/web-interface/popular?pn=1&ps=20

# 观看历史（游标分页）
GET https://api.bilibili.com/x/web-interface/history/cursor
```

注意：2025年5月起强制 WBI 签名 + buvid3 cookie。

## 技术要点

- WBI 签名：从 /x/web-interface/nav 获取 img_key/sub_key，MD5混淆生成 w_rid
- Cookie 复用现有 stream.py 机制
- API 只用于获取列表/元数据，实际播放仍走 yt-dlp
- 自动播放优先级：合集下一个 > 相关推荐第一个
- 请求频率控制 300-500ms 间隔

---

## Phase A：播放队列与基础控制

**目标**：有一个可管理的播放队列，支持上/下一个、循环模式。

### 新增文件
- `src/core/playlist.py` — PlaylistManager
- `src/gui/playlist_panel.py` — 播放列表面板 UI

### PlaylistManager
- VideoItem 数据类：bvid, title, duration, thumbnail_url, source_type, url
- 队列操作：add, remove, clear, reorder
- 播放模式：顺序(sequential) / 单曲循环(single_loop) / 列表循环(list_loop) / 随机(shuffle)
- next() / prev() 根据模式返回下一个 VideoItem
- 播放历史栈

### playlist_panel.py
- QListWidget 显示队列，当前播放项高亮
- 拖拽排序
- 右键菜单：移除、移到顶部、复制链接
- 底部：播放模式切换按钮组

### MainWindow 改造
- Transport bar 增加 ⏮ ⏭ 按钮
- end_of_file 信号连接 _play_next()
- 快捷键：N = 下一个，P = 上一个
- 当前播放的视频自动加入队列（如果不在队列中）

### 分P 支持
- 解析视频时检测分P，自动将所有分P加入队列

---

## Phase D：UI 布局重构（Tab 化）

**目标**：右侧面板改为 Tab 结构，容纳播放列表、增强、浏览三个面板。

### 布局
```
右侧面板 QTabWidget:
  Tab 0: 播放列表 (playlist_panel)
  Tab 1: 音频增强 (enhance_panel + 设备/cookie 配置)
  Tab 2: 视频增强 (video_enhance_panel)
  Tab 3: 浏览 (content_browser) — Phase C 再填充
```

### 改造点
- 现有 right_panel 的布局代码重构为 Tab
- 输出设备、WASAPI Exclusive、Cookie 配置移入"音频增强"Tab 顶部
- 全屏隐藏右侧面板的逻辑不变

---

## Phase B：B站 API 集成

**目标**：用户不离开播放器就能发现新内容。

### 新增文件
- `src/core/bilibili_api.py` — B站 API 客户端

### 功能
- WBI 签名实现
- get_video_info(bvid) → 视频详情 + 分P + 所属合集
- get_related_videos(bvid) → 相关推荐列表
- get_season_videos(mid, season_id) → 合集全部视频
- get_series_videos(mid, series_id) → 系列全部视频
- get_user_favorites(mid) → 收藏夹列表
- get_favorite_content(media_id) → 收藏夹内视频
- get_homepage_rcmd() → 首页推荐
- get_popular() → 热门视频

### 集成到播放流程
- 播放视频时自动查询是否属于合集，提示加载
- 当前视频播放时后台获取相关推荐
- 队列为空时自动从推荐取下一个（可配置）
- playlist_panel 底部显示"接下来播放"推荐区

---

## Phase C：内容浏览面板

**目标**：轻量内容浏览界面，替代"打开浏览器找视频"。

### 新增文件
- `src/gui/content_browser.py` — 内容浏览面板

### 布局
Tab 式：推荐 | 热门 | 收藏夹 | 历史 | 搜索

### 功能
- 视频卡片列表（缩略图 + 标题 + UP主 + 时长）
- 双击 = 加入队列并播放，右键 = 仅加入队列
- 收藏夹浏览（需要登录态）
- 搜索（关键词 → B站搜索 API）
- 异步缩略图加载 + LRU 缓存

---

## 实施顺序

A → D → B → C

## 不做的事

- YouTube 播放体验优化
- B站直播、弹幕、评论
- 登录流程（继续用导入 cookie）
