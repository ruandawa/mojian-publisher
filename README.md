# 墨笺 · 个人公众号发布助手

面向 Windows 的本地微信公众号创作与发布工作台，当前版本 **v1.2.7**。支持现有文章导入和 AI 写作，两种入口共用图片准备、手机阅读排版和发布队列。工作台使用独立桌面窗口；正常发布任务在后台浏览器执行，进度、平台回执和需要处理的事项显示在软件内。

项目代码采用 [MIT 许可证](LICENSE)。依赖及运行时组件遵循各自许可证，见 [NOTICE](NOTICE.md)。

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows%20x64-lightgrey.svg)](https://github.com/ruandawa/mojian-publisher)
[![Release](https://img.shields.io/badge/Release-v1.2.7-brightgreen.svg)](https://github.com/ruandawa/mojian-publisher/releases)

> 💡 **快速下载免安装版**：普通用户无需配置 Python 环境，前往 [Releases 发布页](https://github.com/ruandawa/mojian-publisher/releases/latest) 直接下载 `mojian-publisher-v1.2.7-Windows-x64-public.zip`，解压后双击 `墨笺公众号助手.exe` 即可使用。


## 能做什么

- 导入 Markdown、TXT、HTML 和 ZIP 图文包，清理正文结构，下载公开图片并保存在本机。
- 编辑标题、作者、摘要、正文和封面；提供完整阅读预览、图片准备检查和 HTML 导出。
- 使用兼容 OpenAI Chat Completions 的模型服务辅助写作；模型地址、名称和密钥在软件设置中配置。
- 保存微信草稿、立即发表、安排发送；排期保存当时的正文、图片和创作来源，不随后续编辑变化。
- 在软件内查看登录状态、任务步骤、失败原因以及微信要求的扫码或本人确认。
- 核对微信发表记录和无需登录的正式文章，检查正文及配图后记录已发表；保存草稿与正式发表分别记录。
- 在已提交任务具备完整核对记录时，重启后恢复查询原任务结果；结果不明时不自动重新提交。

## 自动化边界

首次登录、登录过期、微信要求管理员扫码或手机确认时，需要本人处理。软件不会代替管理员确认、处理验证码、绕过账号权限或保证平台审核通过。文章是否可以发表由账号权限、真实内容、平台规则及审核结果决定。

发表流程默认关闭群发通知。**发表与向关注者发送群发通知是不同操作。** 正常任务在后台执行；需要本人处理时，软件内会显示连接或验证面板。

当前适配微信公众平台网页后台。页面更新可能需要修改适配代码；应以微信正式文章和平台状态核对结果。测试通过、草稿已保存、点击过发表或手机已确认，都不能单独作为正式发表成功的依据。

## 运行环境

- Windows 10/11 x64。
- Python 3.11 x64，用于运行源码、测试和构建；打包后的 EXE 不要求用户另装 Python。
- 已安装 Microsoft Edge 或 Google Chrome，发布执行器优先使用 Edge；浏览器自动测试固定使用 Edge。
- Microsoft Edge WebView2 Runtime，用于桌面工作台窗口。
- 联网环境及本人拥有管理权限的微信公众号。AI 写作还需要自行配置可用的模型服务。
- Node.js 仅用于 JavaScript 语法检查，不是软件运行或 EXE 构建的必需依赖。

## 从源码启动

在项目根目录执行以下 PowerShell 命令：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-lock.txt
python launcher.py
```

`requirements-lock.txt` 包含当前版本使用的精确运行与构建依赖；`requirements.txt` 描述运行依赖的版本范围。若 PowerShell 不允许激活虚拟环境，可直接使用 `.\.venv\Scripts\python.exe` 替代命令中的 `python`，无需更改系统策略。

首次使用：

1. 在「账号与设置」填写公众号完整名称，点击「连接公众号」，按软件内提示扫码。
2. 在「文章与编辑」导入文章或图文包，或到 AI 写作功能中生成文章。
3. 检查标题、摘要、正文、图片准备状态和完整阅读预览，按实际情况确认创作来源。
4. 保存文章，选择「保存微信草稿」「立即发表」或安排发送，在任务列表查看结果。
5. 定时发送需要开启定时队列，电脑保持开机、联网，软件保持运行。最小化可继续执行；退出会停止本机服务和执行器。

## 导入与排版

完整格式、资源限制和可复制示例见 [导入格式说明](docs/IMPORT_FORMAT.md)。带本地图片的文章请使用 ZIP 图文包。软件会先整理正文并把图片转为本地资源，资源未准备完整时不进入发送队列。

生成随项目提供的示例图文包：

```powershell
python -m samples.build_example
```

根目录会生成 `日常记录整理-示例图文包.zip`，可直接导入。示例明确标注 AI 辅助生成，配图是软件绘制的文字封面，用于验证导入与排版流程。

正文按手机阅读排版，保留标题层级、段落、列表、引用、强调、代码、链接、配图和图注；宽表格转换为纵向卡片。预览、任务保存的排版、微信写入及 HTML 导出共用排版逻辑。微信可能调整部分样式，最终呈现以正式文章页面为准。

## 构建 Windows EXE

在上述虚拟环境中执行：

```powershell
python build.py
python -m samples.build_example
python package_release.py
```

`build.py` 调用 PyInstaller，生成 `dist/墨笺公众号助手.exe`。`package_release.py` 将 EXE、使用说明、验证说明、示例图文包、源码和依赖许可证整理到 `release/`。请在干净的虚拟环境中构建，避免将无关依赖混入依赖记录。

## 数据与升级

源码运行默认将数据放在项目根目录的 `data/`；EXE 默认放在当前 Windows 用户的 `AppData/Local/MojianPublisher/`。需要隔离开发数据时，可在启动前设置 `MOJIAN_DATA`：

```powershell
$env:MOJIAN_DATA = Join-Path $PWD 'dev-data'
python launcher.py
```

数据包括 SQLite 文章与任务记录、本地图片、导入原件、专用微信浏览器会话、诊断和截图。模型密钥在 Windows 上使用 DPAPI 加密。服务仅监听本机 `127.0.0.1`，默认端口为 `8731`。

升级前正常退出旧版本，同时只运行一个版本。备份时先退出程序，再复制整个数据目录。**数据目录、登录会话、密钥、原始文章和诊断截图都不应提交到公开仓库。** 此开源包只包含程序源码、测试、构建脚本和公开示例。

## 开发与验证

准备公开仓库、推荐简介和源码包生成方法见 [GitHub 上传说明](docs/GITHUB.md)。源码分发使用显式白名单；重新打包运行 `python package_source.py`。

```powershell
python -m unittest discover -s tests -q
node --check web/app.js
node --check web/control.js
```

浏览器测试使用本地模拟微信页面，不需要真实公众号账号，但要求安装 Edge。验证分层及 v1.2.7 的实际平台验证范围见 [VALIDATION.md](VALIDATION.md)。参与开发请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)，安全问题处理见 [SECURITY.md](SECURITY.md)。

## 项目结构

```text
launcher.py           桌面与本机服务入口
publisher/            导入、排版、存储、队列、浏览器适配与回执核对
web/                  本地工作台界面
tests/                离线及本地浏览器自动测试
samples/              示例 Markdown 与图文包生成脚本
docs/                 导入格式等公共文档
build.py              Windows EXE 构建
package_release.py    发布包整理
```

## 🌐 创作生态与技术社区

本项目致力于为创作者提供纯净、高效的公众号创作与自动化发布体验。如需进一步拓展创作工作流或获取设计灵感，欢迎访问：

- **技术指南与最佳实践**：[DevNotes 极客博客 (blog.bt116.com)](https://blog.bt116.com) —— 提供 AI 辅助写作环境配置、自动化发布脚本架构与自媒体提效技术专栏。
- **视觉美学与配图灵感**：[美研智享 MeiNavi (meinvnv.com)](https://www.meinvnv.com) —— 精选 74 组东方人像、商业摄影与排版配色灵感库，助力公众号图文打造爆款封面与阅读美感。

