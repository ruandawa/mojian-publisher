# 上传到 GitHub

解压源码 ZIP 后，将内层 `mojian-publisher-v1.2.7` 文件夹作为仓库根目录。README、LICENSE、publisher 和 .github 应直接位于仓库根目录；源码 ZIP 本身可以放到 Releases，不能替代仓库中的源码。

在 GitHub 新建空仓库，例如 `mojian-publisher`。不要预先生成另一份 README 或许可证。然后在本地源码根目录运行下面的命令，把 `YOUR_NAME` 换成自己的 GitHub 用户名；GitHub 登录使用自己已有的 Git 凭据管理器。

```powershell
git init -b main
git add .
git status --short
git commit -m "Initial open-source release v1.2.7"
git remote add origin https://github.com/YOUR_NAME/mojian-publisher.git
git push -u origin main
```

提交前检查 `git status`。这里只应出现源码、公共文档和测试；不要加入实际数据目录、环境变量文件、密钥、浏览器会话、导入文章或个人截图。

项目已附 `.github/workflows/test.yml`：在 Windows runner 上安装 Python 3.11、锁定依赖和 Edge，检查 JavaScript 并运行本地模拟测试。配置在上传后由 GitHub 执行，本地测试通过不代表已经跑过 GitHub CI。

推荐仓库简介：**Windows 微信公众号创作与发布工作台，支持文章导入、AI 写作、本地图文排版、后台执行和真实发表回执核对。**

推荐 topics：`wechat`、`wechat-official-account`、`windows`、`python`、`fastapi`、`playwright`、`markdown`。

仓库采用 MIT 许可证，当前版权行使用 `Mojian Publisher contributors`。如有明确的权利人署名，应在首次公开前将该行调整为相应署名；第三方许可保持原文。发布文章仍需实际管理权限，并按平台要求完成本人验证和来源声明。

重新整理公开源码包：

```powershell
python package_source.py
```

脚本仅打包明确列出的源码与公共文档，生成 ZIP、ZIP 的 SHA-256 校验文件，以及包内逐文件清单。它不打包数据库、浏览器会话、诊断截图、虚拟环境、构建产物或用户文章。输出在 `release/` 中。
