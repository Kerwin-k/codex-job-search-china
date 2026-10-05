# Sources and dependencies

| Component | Source | Distribution |
| --- | --- | --- |
| Configuration, CLI, policy, release checks | Project code | MIT |
| Durable workbenches, browser wrappers, BOSS bridge and UIA helper | Existing workflow code adapted into configurable project modules | MIT |
| Playwright MCP and browser extension | [Microsoft Playwright MCP](https://github.com/microsoft/playwright-mcp), [Playwright extension](https://github.com/microsoft/playwright/tree/main/packages/extension) | External dependencies, not vendored; upstream licenses apply |
| Banner and documentation | Newly authored project resources | MIT |
| Examples and test fixtures | Synthetic data | MIT |

The project does not redistribute the modified upstream Playwright extension or installed browser runtimes. The launcher pins Playwright MCP at `0.0.83`; changing this version requires a separate compatibility review.

Original workflow state, credentials, personal profiles, resumes, diagnostics and history were not imported into this repository.
