# BOSS 岗位采集与招呼语发送

本项目通过本机已登录 BOSS 直聘的 Chrome 浏览器采集岗位信息，并向筛选后的岗位发送 `greeting.txt` 中的招呼语。岗位数据保存在本地 SQLite 数据库 `boss_jobs.db` 中，该文件不会提交到 Git。

## 环境要求

- Python 3.10 或更高版本
- `requests`：`pip install requests`
- Google Chrome
- Node.js 22 或更高版本

## 1. 开启 Chrome 远程调试

在 Chrome 地址栏打开：

```text
chrome://inspect/#remote-debugging
```

也可以尝试点击：[打开 Chrome 远程调试设置](chrome://inspect/#remote-debugging)。如果浏览器或 Markdown 阅读器禁止打开 `chrome://` 内部链接，请复制上面的地址到 Chrome 地址栏。

开启页面中的远程调试选项，并确认允许本机的远程调试连接。然后在这个 Chrome 用户配置中登录 [BOSS 直聘](https://www.zhipin.com/)。脚本必须连接到这个已登录的 Chrome，不能使用另一个未登录的浏览器窗口。

如果当前 Chrome 版本无法通过上述页面开启，也可以完全退出 Chrome 后，通过独立用户目录启动调试端口：

```powershell
chrome.exe --remote-debugging-port=9222 --user-data-dir="C:\chrome-debug"
```

随后在新打开的 Chrome 中登录 BOSS 直聘。

## 2. 启动浏览器桥

在项目目录打开一个 PowerShell 窗口并保持运行：

```powershell
$env:BOSSHUNTER_CHROME_PORTS="9222,9229,9333"
node .\runtime\cdp-proxy.mjs
```

桥默认监听 `http://127.0.0.1:3456`，并自动查找可用的 Chrome 调试端口。也可以先运行检查程序：

```powershell
node .\runtime\check-runtime.mjs
```

## 3. 采集岗位

在 `testcollect.py` 顶部修改关键词、城市和页数：

```python
KEYWORDS = ["AI大模型"]
CITIES = ["深圳"]
MAX_PAGES = 2
```

然后运行：

```powershell
python .\testcollect.py
```

采集结果写入 `boss_jobs.db`。新岗位状态为 `pending`，公司和职位名称都相同时视为重复岗位；相同公司下的不同职位仍会分别保存。

## 4. 筛选岗位

使用 SQLite 工具打开 `boss_jobs.db`：

- `pending`：发送脚本会处理。
- `filter`：已人工筛除，发送脚本会跳过。
- `sent`：已成功发送。

把不希望发送的岗位状态从 `pending` 修改为 `filter`。

## 5. 编辑招呼语

用 UTF-8 编码编辑项目根目录下的 `greeting.txt`。文件中的全部文字会作为同一条消息发送。

## 6. 发送招呼语

发送参数集中在 `boss_sender.py` 的 `build_parser()` 函数中。可以直接修改各个 `add_argument()` 的 `default`：

- `--limit`：本次最多处理的岗位数；默认值改为 `None` 表示全部。
- `--interval-min` / `--interval-max`：不同岗位之间的随机等待秒数。
- `--browser-pause-min` / `--browser-pause-max`：浏览器操作之间的随机等待秒数。
- `--message-file`：招呼语文件。
- `--db`：SQLite 数据库。

直接运行：

```powershell
python .\boss_sender.py
```

也可以临时覆盖默认值：

```powershell
python .\boss_sender.py --limit 2 --interval-min 60 --interval-max 180
```

脚本只查询 `status='pending'` 的岗位。确认消息发送成功后更新为 `sent`；无法确认时保持 `pending`，并记录 `attempts` 和 `last_error`，以便人工检查。

## 注意事项

- 运行期间不要关闭已登录的 Chrome 或浏览器桥。
- 首次使用时建议把发送数量设为 1，确认页面流程正常后再逐步增加。
- 自动化操作可能触发平台验证或频率限制，请控制数量和间隔，并遵守平台规则。
