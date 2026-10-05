# 执行与恢复

## 投递前

1. 只读检查配置、平台连接和默认附件名称。
2. 建立本平台 workbench，获取执行锁，并按 configured lanes 搜索。
3. 卡片筛选使用用户偏好，未知情况读绑定的详情。
4. 核对岗位 ID、公司、标题、薪资；用本地确认的事实支持匹配理由。
5. 查询同平台台账，调用 `scripts/jobflow.py dedupe --company <公司> --title <岗位>` 读取跨平台最少字段摘要，不修改其他台账。
6. 获得用户对本次平台、范围和目标的明确授权后，才调用 `authorize`。

配置为 `apply`、安装插件或生成草稿都不是投递授权。公开版本默认没有作者的职业偏好，也不固定执行模型。

## 51job / 智联脚本

`scripts/jobflow.py prepare --platform <平台>` 把运行时写到私有目录，输出路径。先用专属 MCP 的 `browser_run_code` 执行 `install.js` 的完整函数，再操作该平台唯一绑定的列表页。调用前检查全部标签，拒绝错误配置或多目标歧义。

`runtime_config.py lane` 按城市、关键词或智联期望池生成搜索函数；`runtime_config.py candidate` 根据位置卡片生成详情函数。先设置本地环境变量 `CODEX_ENTERPRISE_ACTIVE_CANDIDATE_JSON`，不要把完整详情重复发给模型。输出文件必须放到 `prepare` 返回的私有运行目录中。

平台脚本分别提供卡片、详情、投递和检查工具。读取 `runtime_config.py --help` 与本平台 workbench 的 `--help` 获取参数；操作前确认所有路径都属于本平台。

51job 使用 `job51_workbench.py`，智联使用 `zhaopin_workbench.py`。保留原有租约、不可变批次、技术隔离、页指纹和幂等回执。不要把技术错误写成拒绝。

## 单次发送保障

51job / 智联最终投递前：

```powershell
python scripts/jobflow.py authorize --platform 51job --target 5
python scripts/jobflow.py prepare --platform 51job
python scripts/jobflow.py render-submit --platform 51job --job-id "SYNTHETIC-JOB-ID"
```

示例 ID 仅用于说明。`authorize` 必须来自用户明确授权，`render-submit` 必须使用已核对的真实岗位 ID。生成的单次脚本五分钟内有效；在执行前已保留持久尝试记录，即使工具超时或进程退出也不会默默生成第二次发送。

执行输出的函数一次后，以直接证据调用 `resolve-attempt --outcome sent`，再提交本平台成功台账与 workbench。只有实际成功才计数，不能只根据点击或脚本无异常计数。

BOSS 使用 `boss_flow.py send-verified --send-mode background`。必须先有当前批次、有效执行租约和本地投递许可；流程核对会话、草稿、唯一发送控件、发送后的精确消息及清空的草稿。UIA 是有条件的恢复方式，不把未验证触发算成功。

## 结果不明确

停止再次触发，保留当前岗位和尝试记录。核对平台实际状态或消息；确实成功就提交回执。只有直接证据确认未发送、且用户授权继续时，才能使用 `resolve-attempt --outcome not_sent --evidence <证据>` 允许重试。超时、断连或缺少成功提示本身不是“未发送”的证据。

到达授权目标、用户停止、验证码、登录、身份歧义或技术熔断时释放本平台租约。只清理本次拥有的详情页，保留无关页面和验证页面。

撤销许可使用 `revoke`。51job / 智联随后必须重新准备并执行 `install.js`，清除浏览器内旧许可。BOSS 发送时读取本地许可。若无法更新浏览器内许可，就暂停并断开本次平台连接。

## 首版边界

这是可安装的源码测试版。公共配置、持久状态与安全边界通过离线测试，平台连接和页面选择器需要在使用者环境进行受控验证。不要仅凭自动测试推断真实投递可用。首版以单平台串行运行为推荐方式。


## 51job / 前程无忧 commands

Use only MCP `jobflow-51job`. Before connecting, require its explicitly configured browser profile and exactly one allowlisted list page. Do not attach a foreign page or share the other platform's profile. Execute prepared `install.js` once, generate a configured lane, read `51job-cards.js`, and inspect only exact-ID candidates. Use `51job-detail.js` to recheck binding before submission.

Use `scripts/job51_workbench.py --help` to maintain the batch, acquire/release a lease, and persist each decision. Use `scripts/runtime_config.py lane --platform 51job --city <configured city> --keyword <configured keyword> --output <private runtime>/active-lane.js`. Generate a candidate wrapper using `candidate --candidate-env --output <private runtime>/active-inspect.js`. Do not hand-edit generated runtime files.

Immediately before a final send, use `scripts/jobflow.py render-submit --platform 51job --job-id <exact ID>` and execute only the returned file's function. Never call the unprepared static submit helper directly. Resolve the persistent attempt using direct evidence, then commit the engine receipt. At an uncertain stop, reconcile before retrying, even after a new browser connection.
