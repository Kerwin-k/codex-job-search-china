# 配置指南

运行 `python jobflow.py init` 后，根据命令输出打开本地配置文件。配置与数据必须位于源码树之外；`JOBFLOW_HOME` 可指定另一个私有目录。覆盖已有配置被拒绝，更新时直接编辑该文件。

| 字段 | 含义 |
| --- | --- |
| `platforms` | 启用的 `51job`、`zhaopin`、`boss` |
| `search.cities` | 按用户顺序搜索的城市 |
| `search.keywords` | 任意职业方向的岗位关键词 |
| `search.expectation_pools` | 智联账号实际存在的职位期望池名称，留空即使用关键词搜索 |
| `search.salary.monthly_floor` | 人民币月薪门槛，0 表示不设置金额下限 |
| `search.salary.comparison` | `lower_bound` 比较薪资下限，`upper_bound` 比较上限 |
| `search.salary.unknown` | 固定 `review`：未知薪资复核，投递前仍须核实 |
| `search.excluded_keywords` | 明确排除的关键词，默认不附带职业排除项 |
| `search.excluded_employer_types` | 可选 `public`、`headhunter`、`outsourcing` |
| `search.required_keywords` | 缺少这些词时进入详情复核，不直接从卡片拒绝 |
| `search.page_budget` | 每个城市与搜索词的页数预算 |
| `search.city_codes` | 本平台的城市名称到代码覆盖；未收录城市须提供对应平台代码 |
| `execution.mode` | `review`、`draft`、`apply`；不是投递授权凭证 |
| `execution.target_count` | 本次计划目标；真正投递许可以用户明确授权的目标为准 |
| `execution.model` / `reasoning_effort` | `inherit` 继承当前 Codex 环境，不固化模型 |
| `candidate.profile_file` | 本地已确认事实 JSON 文件 |
| `candidate.greeting_file` | 本地文案文件；为空时由用户事实生成并审阅草稿 |
| `candidate.resume_labels` | 各平台实际使用的默认简历名称，投递前核对 |
| `platform_overrides` | 平台级 `search`、`execution` 覆盖 |

当前 51job / 智联的搜索适配以全职岗位为目标；其他用工形式尚待适配。岗位职业方向可自定义，未知页面版本仍需受控验证。

## 薪资如何判断

薪资 8–12K、门槛 10K：下限模式不满足，上限模式满足。月薪与明确的年薪可解析；日薪、时薪、无法识别币种和面议进入复核。未知薪资不能直接通过最终自动投递检查。

## 不同平台采用不同偏好

```json
{
  "platform_overrides": {
    "boss": {
      "search": {
        "salary": {"comparison": "upper_bound"},
        "excluded_employer_types": ["headhunter"]
      }
    }
  }
}
```

覆盖与公共配置合并后重新校验。用 `python jobflow.py config --platform boss` 查看最终设置；未知字段会报错，不会悄悄忽略拼写错误。

## 确认简历事实

```json
{
  "confirmed": true,
  "facts": [
    {"id": "fact-001", "text": "合成示例：参与过内部工具维护。", "source": "合成资料，仅用于演示"}
  ]
}
```

这是合成示例。实际使用请填自己的真实资料与来源，确认后才设置 `confirmed`。岗位要求不构成个人经历证据；求职意向不能转写成已做过的工作。

在 `candidate.resume_labels` 填写各平台实际附件名称。项目不自动上传或替换附件，遇到未映射的简历选择器会暂停。

修改配置、事实或文案会使后续本地授权校验失败。先撤销许可并重新准备运行时，再按用户的新范围重新授权；浏览器中已加载的旧许可不能只靠编辑配置自动撤回。
