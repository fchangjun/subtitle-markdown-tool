# 视频字幕转 Markdown 工具

一个本地 Web 工具，用于批量抓取视频字幕并导出 Markdown 文件。当前主要面向 YouTube 链接，支持任务进度、历史任务、Markdown 预览和 ZIP 下载。

## Windows 部署

### 1. 安装 Python

在 Windows 电脑安装 Python 3.10 或更高版本：

https://www.python.org/downloads/windows/

安装时勾选 `Add python.exe to PATH`。

### 2. 下载代码

如果已经上传到 GitHub，可在 Windows PowerShell 中执行：

```powershell
git clone <你的 GitHub 仓库地址>
cd <仓库目录>
```

也可以在 GitHub 页面点击 `Code` -> `Download ZIP`，解压后进入目录。

### 3. 安装依赖

双击运行：

```text
install_windows.bat
```

或者在 PowerShell 中手动执行：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 4. 启动服务

双击运行：

```text
start_windows.bat
```

浏览器打开：

```text
http://127.0.0.1:8765
```

如需让同一局域网内其他电脑访问，可运行：

```text
start_windows_lan.bat
```

然后在其他设备访问：

```text
http://<Windows电脑IP>:8765
```

如果 Windows 防火墙弹窗，请允许 Python 访问专用网络。

## macOS / Linux 启动

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python web_app.py --host 127.0.0.1 --port 8765
```

然后打开 `http://127.0.0.1:8765`。

## 命令行用法

也可以不启动 Web 页面，直接导出字幕：

```bash
python youtube_subtitles_to_md.py "https://www.youtube.com/watch?v=VIDEO_ID" -o markdown
```

## 频道发现

Web 页面分为“查找”和“导入下载”两个页面。“查找”页先维护关注频道库，例如添加 `https://www.youtube.com/@Figma`；查找时勾选要参与的频道，再选择 `1年`、`13个月`、`90天`、`1年1个月1周` 这类时间范围发现候选视频。候选结果默认全选，可以取消部分视频后导入到“导入下载”页。

“查找”页还会维护一个关注频道库。可以添加新的博主主页，系统会按规范化后的频道 `/videos` 链接去重，因此同一频道重复添加不会生成多条记录。点击关注频道可以查看该频道已有内容数量、最近更新时间、日期未知数量和已抓到的视频列表；也可以单独更新该频道。

每次查找都会保存为一条查找记录，记录保存在 `web_outputs/discoveries/`。之后可以从“查找记录”打开历史结果，也可以点击“更新当前记录”，用同样的频道和时间范围重新抓取候选视频。

关注频道数据保存在 `web_outputs/channels/`。

发现阶段会优先使用频道列表里的轻量信息。YouTube 有些频道列表不会返回发布日期；页面提供“每频道补日期”，会对每个频道最近一批缺少日期的视频逐条补查详情，拿到发布时间后再按时间范围过滤。补查后仍然没有日期的视频会被跳过，并在页面统计里标记为“日期未知已跳过”。补日期越多越准确，但也越慢、更容易触发 YouTube 限流。

如果配置 `YOUTUBE_DATA_API_KEY`，频道发现会优先使用 YouTube Data API：先通过频道拿 uploads playlist，再按 50 条/页读取 `playlistItems` 的发布时间。这样通常只需要几十次 API 请求就能扫描上千条，比逐个打开视频详情快很多；API 不可用时会自动降级到 yt-dlp。yt-dlp 降级路径会按批次扫描频道列表，默认每批 100 条；每批处理完都会按日期过滤，遇到早于时间范围的内容就提前停止。

## 降低限流 / 封 IP 风险

工具默认启用慢速模式：Web 任务串行处理，每次外部请求之间会等待一段时间；检测到 403、429、验证码、not a bot、too many requests 等疑似限流信号后，会自动长时间退避。

可以直接粘贴最多 1000 条 URL。后端会自动排队处理，并默认每 30 条非缓存 URL 休息 5 分钟；如果检测到 YouTube 封禁/限流信号，会自动冷却 15 分钟后继续。页面会显示当前冷却原因和剩余时间。

如果已经被封，先停止任务，等待封禁恢复后再重新运行。不要在封禁期间反复重试，否则会拉长恢复时间。

可用环境变量调整速度：

```bash
# 每次外部请求至少间隔 8 秒，再叠加 0-4 秒随机抖动
export SUBTITLE_REQUEST_MIN_INTERVAL_SECONDS=8
export SUBTITLE_REQUEST_JITTER_SECONDS=4

# 检测到限流/封禁信号后，至少退避 180 秒
export SUBTITLE_LIMIT_BACKOFF_SECONDS=180

# Web 批量任务自动分批：每 30 条休息 300 秒
export SUBTITLE_AUTO_BATCH_SIZE=30
export SUBTITLE_AUTO_BATCH_COOLDOWN_SECONDS=300

# 遇到明确 IP block 后，Web 任务冷却 900 秒再继续
export SUBTITLE_IP_BLOCK_COOLDOWN_SECONDS=900

# 单条任务失败后的任务级重试退避
export SUBTITLE_TASK_RETRY_BASE_SECONDS=15
export SUBTITLE_TASK_RETRY_MAX_SECONDS=180

# 频道发现：每次最多输入 20 个频道，每频道默认扫描前 120 条，硬上限 1000 条
export SUBTITLE_DISCOVERY_MAX_SOURCES=20
export SUBTITLE_DISCOVERY_MAX_PER_SOURCE=120
export SUBTITLE_DISCOVERY_HARD_MAX_PER_SOURCE=1000

# 频道发现：列表没有日期时，默认最多补查最近 120 条视频详情
export YOUTUBE_DATA_API_KEY=你的YouTubeDataAPIKey
export YOUTUBE_API_TIMEOUT_SECONDS=20
export SUBTITLE_DISCOVERY_DETAIL_LOOKUP_LIMIT=120
export SUBTITLE_DISCOVERY_DETAIL_LOOKUP_HARD_LIMIT=1000
export SUBTITLE_DISCOVERY_DETAIL_LOOKUP_WORKERS=4
export SUBTITLE_DISCOVERY_DETAIL_TIMEOUT_SECONDS=18
export SUBTITLE_DISCOVERY_DETAIL_SOCKET_TIMEOUT_SECONDS=8

# 没有 YouTube API key 或 API 降级时，yt-dlp 默认也按页面选择数量执行；可按需设置更低环境上限
export SUBTITLE_DISCOVERY_YTDLP_MAX_PER_SOURCE_WITHOUT_API=1000
export SUBTITLE_DISCOVERY_YTDLP_LIST_TIMEOUT_SECONDS=600
export SUBTITLE_DISCOVERY_YTDLP_BATCH_SIZE=100
```

如果仍然频繁触发封禁，可以把 `SUBTITLE_REQUEST_MIN_INTERVAL_SECONDS` 调到 `15` 或 `30`，或者把 `SUBTITLE_AUTO_BATCH_SIZE` 调小、`SUBTITLE_AUTO_BATCH_COOLDOWN_SECONDS` 调大。

## 输出目录

Web 页面生成的任务结果默认保存在：

```text
web_outputs/
```

每个任务目录会包含 `job_summary.json` 和 `job_metrics.csv`；其中会记录单条视频时长、总视频时长、提取耗时、重试次数、字幕来源等统计信息。

命令行脚本默认输出到：

```text
markdown/
```

这些目录属于运行产物，默认不会提交到 GitHub。
