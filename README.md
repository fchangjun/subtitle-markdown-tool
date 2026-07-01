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
```

如果仍然频繁触发封禁，可以把 `SUBTITLE_REQUEST_MIN_INTERVAL_SECONDS` 调到 `15` 或 `30`，或者把 `SUBTITLE_AUTO_BATCH_SIZE` 调小、`SUBTITLE_AUTO_BATCH_COOLDOWN_SECONDS` 调大。

## 输出目录

Web 页面生成的任务结果默认保存在：

```text
web_outputs/
```

命令行脚本默认输出到：

```text
markdown/
```

这些目录属于运行产物，默认不会提交到 GitHub。
