# Contributing

欢迎改进平台适配、配置体验和测试。请先用合成数据重现问题，再提出最小改动。

```powershell
python -m unittest discover -s tests -v
node --test tests/policy.test.cjs
python tools/sync_shared.py --check
python tools/release_check.py
```

修改公共模块时先更新 `tools/` 下的源文件，再运行 `python tools/sync_shared.py`。提交前检查差异，确保未加入任何本地配置、凭据、简历、运行日志或真实沟通内容。

平台接口改版需要补充合成夹具和边界测试。离线测试通过不能写成真实投递成功；受控实测结论只报告环境、版本、结果和限制，不上传真实求职者的数据。

新文件需要人工审阅后加入发行允许清单；不要直接扩大到整个目录或依赖 `.gitignore`。上游依赖升级应单独审阅、固定版本并验证连接行为。
