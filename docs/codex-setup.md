# 交给 Codex 安装

用户可以将仓库链接与 [README 的安装消息](../README.md#交给-codex-安装推荐) 一起发给 Codex。本文是执行流程：具有本地命令与文件权限的 Codex 应实际完成已授权安装，遇到必要的人工步骤再引导用户。单独发一个链接，不代表授权安装或投递。

## 1. 检查环境与已有安装

- 读取 README、AGENTS 和本文。目标为 Windows；云端或不能操作本机的会话不能替用户连接本机浏览器，应说明需要切换至本地 Codex。
- 检查 Git、Python 3.10+、Node.js 20+ 与 Codex 可执行入口。BOSS 桥接自身只需要 Python；51job / 智联连接需要 Node。缺少依赖时说明缺哪一项；系统软件安装仍遵循用户的授权与客户端权限。
- 选择稳定、用户可写的项目目录。没有仓库时克隆；有仓库时先检查 remote 与工作区，复用匹配的干净副本。不要覆盖本地修改，不要把 MCP 指向会自动清理的临时目录。
- 只读检查现有市场、插件、Skill 和 MCP：按当前 CLI 的 `--help` 选择可用命令。不要打印其他 MCP 的环境变量、令牌或完整配置。不要移除原有求职工作流。
- 检查 `JOBFLOW_HOME` 和现有私人配置；用户数据必须在源码之外。已配置时复用、核对，只补用户要求的内容。活动批次或授权许可存在时，先与用户确认如何处理，不把重新安装当作清空记录的理由。

## 2. 一次收集偏好并初始化

从用户消息中提取已给信息，只一起询问缺少的项：需要的平台、城市、岗位关键词、人民币月薪门槛（可选，默认 0）。平台值为 `51job`、`zhaopin`、`boss`。薪资默认比较下限；用户希望比较上限时使用 `upper_bound`。排除条件可留空，后续再补。

把实际答案作为独立参数传给初始化工具，使用 `--non-interactive`，不要启动交互式向导后等待终端输入。以下参数是合成示例，必须替换为用户答案：

```powershell
python jobflow.py init --non-interactive --platforms "51job,zhaopin" --cities "北京" --keywords "产品设计" --salary-floor 10000 --salary-comparison lower_bound
python jobflow.py config
python jobflow.py doctor
```

`init` 拒绝覆盖已有配置，这是预期行为；已有配置使用 `config` 定位并按用户意图更新，再通过工具校验。保持新安装的 `execution.mode=review`。在 PowerShell 中使用参数数组或可靠的引用方式传入用户文本，不拼接未经引用的 shell 命令。

配置和模板已生成时，初始化已经完成。空事实资料或附件名称会让 `doctor` / `validate` 报告未就绪；应说明具体缺项，不能填造经历或把模板的 `confirmed` 直接改为 true。纯安装阶段可以先保留未确认资料；需要事实匹配、草稿或发送前，再引导用户提供本地材料并逐项确认。

## 3. 自动安装所选工作流

优先使用插件市场。先检查当前 CLI 能力，避免照搬不支持的子命令：

```powershell
codex plugin marketplace --help
codex plugin add --help
```

支持时添加本仓库市场，并只安装用户选择的平台；已有相同市场或插件时检查后复用，避免重复安装。

```powershell
codex plugin marketplace add Kerwin-k/codex-job-search-china --ref main

# 以下三条是平台映射，只执行用户选择的条目
codex plugin add job-search-51job@codex-job-search-china
codex plugin add job-search-zhaopin@codex-job-search-china
codex plugin add job-search-boss@codex-job-search-china
```

旧版 CLI 没有 `plugin add` 时，添加市场后引导用户在插件目录选择 **Codex 国内求职工作流** 并安装所选平台；需要时重启客户端。也可使用环境内的 Skill Installer，按下列 GitHub 路径安装单个 Skill。两条路线选择其一，不重复安装同名能力。

| 平台 | 仓库内 Skill 路径 |
| --- | --- |
| 51job | `plugins/job51/skills/search-51job-jobs` |
| 智联 | `plugins/zhaopin/skills/search-zhaopin-jobs` |
| BOSS | `plugins/boss/skills/search-boss-jobs` |

不要硬编码用户的 Codex 主目录或擅自覆盖已有 Skill。安装成功与当前会话能调用是两个状态；下一会话检查启用状态后再使用。市场与客户端机制参考 [OpenAI 官方插件文档](https://developers.openai.com/plugins/build/plugins)。CLI 的具体安装参数以本机 `--help` 为准。

## 4. 引导浏览器连接，再继续执行

每次只接通一个所选平台。把人工操作压缩成当前需要的几步，等用户完成后继续执行与核验，不一次抛出全部命令。

### 51job / 智联

1. 用户选择 Edge 或 Chrome，在相应平台的独立配置中自行登录，并按 [连接指南](installation.md#3-连接-51job--智联) 安装 Playwright 扩展。
2. 引导用户从 `edge://version` / `chrome://version` 的 Profile Path 确认内部配置目录名，只获取末尾的 `Default` 或 `Profile N` 等名称；不需要上传完整路径或账号。不同平台绑定不同配置。不能猜测 `Profile 1` 就是用户的目标配置。
3. Codex 使用实际配置名执行 `bind-browser`，再检查同名 MCP 是否已存在。不存在时执行 `connect`；已存在时仅核对是否指向本项目的对应平台，不自动覆盖。

```powershell
# 合成示例：替换浏览器与实际配置目录名，只执行对应平台
python jobflow.py bind-browser --platform 51job --browser msedge --profile "Profile 1"
python jobflow.py connect --platform 51job
python jobflow.py prepare --platform 51job
```

注册成功后提示新开会话使 MCP 生效。让用户完成扩展连接确认；在新会话中按对应 Skill 的执行参考确认平台和标签页。只有实际读取到目标平台页面，才能报告“浏览器已连接”；命令成功不证明登录成功。

### BOSS

1. Codex 执行 `python jobflow.py prepare --platform boss`，得到私人扩展目录；不读取或显示令牌。
2. 引导用户在 Chrome 的 `chrome://extensions` 启用开发者模式，加载输出的本地扩展目录，自行登录 BOSS。
3. 按 [连接指南](installation.md#4-连接-boss) 启动 `boss_bridge.py serve`，用独立终端或受控后台进程运行，记录属于本项目的进程与停止方式。Windows 后台进程隐藏窗口，不阻塞 Codex 当前调用。不得终止无关进程；端口冲突先检查并告知，不直接杀进程。
4. 按 BOSS Skill 的执行参考核验桥接认证、目标平台和页面状态。只收到 `/health` 响应不能证明扩展或登录就绪。BOSS 不使用 Playwright、CDP 或远程调试。

## 5. 交付可使用的状态

按每个平台报告真实结果，不用单一的“部署成功”掩盖缺项：

| 项目 | 完成标准 |
| --- | --- |
| 本地初始化 | 私人配置已生成，平台与搜索偏好已核对 |
| 工作流安装 | 所选插件 / Skill 已安装；说明是否要新开会话 |
| 环境检查 | 运行检查结果已读取，缺项有具体操作说明 |
| 浏览器连接 | 连接入口已配置；登录、扩展、目标页面分别确认 |
| 资料就绪 | 用户真实事实已确认，所需平台附件名称已核对；缺项如实标注 |
| 首次只筛选 | 用户要求试运行时，按对应 Skill 验证一轮真实页面筛选，无投递与消息发送 |

完成后给用户一个短结果、一份必要操作清单，以及可直接复制的下一句话：

> 使用我刚安装的求职工作流，按本地配置筛选岗位，先不投递。

安装请求不授权 `authorize`、`render-submit` 或 BOSS 发送。不要为验证安装而提交简历、发送测试消息或确认未知结果。部署自动化减少的是安装操作，登录确认、真实资料确认和投递授权仍由用户控制。
