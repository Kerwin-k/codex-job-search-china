# 安装与连接

首版面向 Windows + Codex；Python 3.10+ 与 Node.js 20+ 需可从命令行调用。公共配置工具只依赖 Python 标准库。51job、智联连接上游 Playwright MCP，BOSS 使用本项目的 Chrome 桥接。

## 1. 下载并配置

```powershell
git clone https://github.com/Kerwin-k/codex-job-search-china.git
cd codex-job-search-china
python jobflow.py init
python jobflow.py doctor
```

先编辑生成的本地事实资料和简历附件名称，再运行 `python jobflow.py validate`。初始化默认只筛选，空事实资料会阻止投递。

## 2. 安装需要的平台插件

```powershell
codex plugin marketplace add ./
```

在 Codex 插件目录选择 **Codex 国内求职工作流**，安装需要的平台。支持仓库市场的客户端也可以使用：

```powershell
codex plugin marketplace add Kerwin-k/codex-job-search-china --ref main
```

仅需单个 Skill 时，可在 Codex 中请求 Skill Installer 从相应路径安装：

- [51job Skill](https://github.com/Kerwin-k/codex-job-search-china/tree/main/plugins/job51/skills/search-51job-jobs)
- [智联 Skill](https://github.com/Kerwin-k/codex-job-search-china/tree/main/plugins/zhaopin/skills/search-zhaopin-jobs)
- [BOSS Skill](https://github.com/Kerwin-k/codex-job-search-china/tree/main/plugins/boss/skills/search-boss-jobs)

每个 Skill 自带脚本，不依赖本仓库的其他平台插件。已安装副本更新与本地克隆更新是两个步骤；更新连接入口前检查现有 MCP 指向，`connect` 不会覆盖同名配置。

## 3. 连接 51job / 智联

在 Edge 或 Chrome 建立两个独立浏览器配置，分别自行登录平台并安装上游 [Playwright Extension](https://github.com/microsoft/playwright/tree/main/packages/extension)。不要导入别人的配置或令牌。

通过 `edge://version` 或 `chrome://version` 的 Profile Path 确认配置目录名。下面的 `Profile 1`、`Profile 2` 是示例，必须替换成实际目录名：

```powershell
python jobflow.py bind-browser --platform 51job --browser msedge --profile "Profile 1"
python jobflow.py bind-browser --platform zhaopin --browser msedge --profile "Profile 2"
python jobflow.py connect --platform 51job
python jobflow.py connect --platform zhaopin
```

`connect` 是显式安装动作，只新增 `jobflow-51job` 或 `jobflow-zhaopin`，不改动其他 MCP。没有 Codex CLI 时，手动将相应平台插件内的 `launch_playwright.py --platform <平台>` 注册为 MCP 启动命令。Skill 运行时只使用对应平台的 MCP。

启动器将 `@playwright/mcp@0.0.83` 缓存在各平台的私人数据目录，安装时禁用 npm 包脚本，再通过 Node 直接运行其入口，以兼容 Windows 含空格或 `&` 的路径。启动参数为 `--extension` 并绑定用户指定的配置；不会启动 Playwright 独立配置。首次调用需要联网下载上游包，连接时可能显示浏览器确认。已验证依赖安装和启动参数；实际浏览器连接仍需在使用者环境验证。

## 4. 连接 BOSS

```powershell
python jobflow.py prepare --platform boss
python plugins/boss/skills/search-boss-jobs/scripts/boss_bridge.py serve
```

按 `prepare` 输出在 Chrome 的扩展管理页面加载本地扩展目录，自行登录 BOSS。默认端口为 17897；使用 `prepare --platform boss --port <端口>` 可指定端口。运行中的桥接不要直接换端口。扩展和本地桥接需同步更新。

桥接前台运行便于查看启动与停止，按 Ctrl+C 停止。工具不会触碰无关浏览器。BOSS 不使用 Playwright、CDP 或远程调试。

## 5. 从只筛选开始

在 Codex 里输入：

> 使用 51job 工作流，按我的配置筛选岗位，先不投递。

已安装且启用的 Skill 根据描述匹配任务；也可以显式使用 `$search-51job-jobs`、`$search-zhaopin-jobs` 或 `$search-boss-jobs`。

公开 GitHub 仓库便于搜索与按链接安装，不代表 Codex 会自动抓取全网仓库，也不代表已进入官方插件目录。
