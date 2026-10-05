---
name: search-boss-jobs
description: Search and screen BOSS 直聘 jobs, draft truthful greetings and verify authorized sends through the dedicated Chrome bridge. Use for BOSS、Boss直聘、招聘沟通、打招呼、去重和恢复；never use Playwright or CDP for BOSS.
---

# BOSS 直聘 / Zhipin 求职工作流

Operate only BOSS 直聘 / Zhipin. This skill is configurable for different careers; never assume the author's preferences, resume, browser paths or models.

## Start

1. Resolve this skill directory. Invoke its own `scripts/jobflow.py doctor --platform boss` and inspect the local effective configuration. If setup is missing, help run `init` and confirm the user's own cities, keywords, salary comparison and default attachment name. Do not edit scripts to personalize the workflow.
2. Read `references/execution.md` before the first live action or after an uncertain submit/send. Use the user's configured model or current Codex model; do not create another chat or prescribe a fixed model.
3. Default to review. Installation, `execution.mode=apply`, a quota in configuration, or a draft is not authorization. Require clear user authorization for this platform, scope and target before calling `authorize`.
4. Read only the necessary confirmed facts from the private profile. Bind every candidate to job ID, company, title and salary. Explain eligibility using actual facts; missing hard requirements remain unresolved rather than invented.

## Always-on contract

- Only this platform's browser binding, runtime, workbench, execution lock and receipts may be operated. Read another platform's minimum duplicate summary only through a deliberate read-only check.
- Card decisions use effective user configuration. Unknown salary, company type or requirements need bound-detail review. Do not transplant fixed occupational exclusions.
- Browser/selector/identity failures are technical events, not rejected jobs or seen records.
- Review and draft may not trigger application or greeting controls. Before the final authorized send, recheck attachment, salary, job binding, duplicates and supporting facts.
- A confirmed result requires direct platform evidence bound to the same job; a click or timeout is not success. Commit the idempotent ledger and workbench only after that evidence.
- Persistent attempts prevent automatic retries after ambiguity. Reconcile actual platform state before classifying an attempt as sent or not sent.
- Stop at the authorized target, user stop, bounded exhaustion, login/CAPTCHA, identity uncertainty or technical circuit. Release the platform lease and close only owned pages; keep verification and unrelated pages.
- Never print tokens, export resumes, upload private runtime data, bypass verification challenges, or count technical errors as job decisions.

## Helpers

`scripts/jobflow.py` provides init, config, validate, doctor, prepare, authorize, revoke and attempt reconciliation. Private files are stored outside the source tree. Use helper `--help` for arguments and `references/execution.md` for platform-specific operation.
