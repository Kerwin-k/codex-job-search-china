# Project instructions

This repository contains configurable Codex job-search plugins for 51job,
Zhaopin and BOSS Zhipin. Preserve the user's requested career direction.

When the user asks to install or configure this project, read `docs/codex-setup.md`
and execute its setup flow. Collect missing preferences together, use non-interactive
initialization, preserve existing configuration, and report actual readiness.
Do not respond with installation instructions alone when local execution is available.

- Keep user configuration, resumes, credentials and runtime state outside the repository.
- Use synthetic people, companies, messages and job records in examples and tests.
- Never copy local skill directories, browser profiles, backups or application ledgers wholesale.
- Keep platforms isolated; cross-platform dedupe reads minimum normalized fields only.
- Preserve exact job binding, durable attempts, execution leases and verified receipts.
- Never retry an uncertain submit/send before reconciliation.
- Default to review. Configuration and installation do not authorize real applications.
- Update shared code in `tools/`, run `tools/sync_shared.py`, and check the generated copies.
- Run Python unit tests, Node policy/guard tests and `tools/release_check.py` before packaging.
- Every public file must appear in `release-files.json`. Review additions, then use `tools/release_check.py --refresh` to update the list.
- Do not store private identifier scan dictionaries, scan matches or raw diagnostics in this repository.
- Keep documentation truthful about offline tests, supported environments and live verification.
- Use project-only commit identity; never import a developer's real name or email.
