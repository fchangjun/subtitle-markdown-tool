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
