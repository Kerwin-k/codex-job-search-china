async (page) => {
  const candidate = __CANDIDATE_JSON__;
  const recoveryLane = __RECOVERY_LANE_JSON__;
  const context = page.context();
  const isAllowed51JobUrl = (value) => /^https:\/\/(?:we|jobs)\.51job\.com(?:\/|$)/i.test(String(value || ""));
  let lane = context.__codexLane;
  let recovery = null;
  if ((!lane || !lane.verified || lane.platform !== "51job") && recoveryLane && typeof context.__codexRecover51JobLane === "function") {
    recovery = await context.__codexRecover51JobLane(recoveryLane);
    lane = context.__codexLane;
  }
  if (!lane || !lane.verified || lane.platform !== "51job") {
    return { ok: false, error: "filters_unverified", recoveryError: recovery?.error || "", recoveryAttempted: !!recoveryLane };
  }
  const list = context.__codexListPage;
  const listUrl = String(context.__codexListUrl || lane.listUrl || list?.url() || "");
  if (!list || list.isClosed() || !isAllowed51JobUrl(listUrl)) return { ok: false, error: "list_page_missing" };
  if (list.url() !== listUrl) {
    await list.goto(listUrl, { waitUntil: "commit", timeout: 15000 }).catch(() => {});
    await list.waitForLoadState("domcontentloaded", { timeout: 8000 }).catch(() => {});
    if (context.__codexRestore51JobListPage) await context.__codexRestore51JobListPage(list);
  }
  await list.locator(".joblist-item").first().waitFor({ state: "visible", timeout: 8000 }).catch(() => {});
  if (context.__codexCleanup51JobDetails) await context.__codexCleanup51JobDetails([list]);
  await list.bringToFront();
  const id = String(candidate[0]);
  const normalizeHref = (value) => {
    const raw = String(value || "").trim();
    if (isAllowed51JobUrl(raw)) return raw;
    if (raw.startsWith("/")) return `https://we.51job.com${raw}`;
    return "";
  };
  const pageJobId = (value) => (String(value || "").match(/\/(\d+)\.html(?:[?#]|$)/i) || [])[1]
    || (String(value || "").match(/[?&]jobid=(\d+)(?:&|$)/i) || [])[1] || "";
  const exactJobHref = (value) => {
    const normalized = normalizeHref(value);
    return pageJobId(normalized) === id ? normalized : "";
  };
  let routeHref = exactJobHref(candidate[7]);
  const restoreList = async () => {
    if (list.url() !== listUrl) {
      await list.goto(listUrl, { waitUntil: "commit", timeout: 15000 }).catch(() => {});
      await list.waitForLoadState("domcontentloaded", { timeout: 8000 }).catch(() => {});
    }
    if (context.__codexRestore51JobListPage) await context.__codexRestore51JobListPage(list);
    await list.locator(".joblist-item").first().waitFor({ state: "visible", timeout: 8000 }).catch(() => {});
  };
  const recoverToList = async (detailPage) => {
    if (detailPage && detailPage !== list) {
      void (context.__jobflowOwnedPages?.has(detailPage) ? detailPage.close({ runBeforeUnload: false }).catch(() => {}) : Promise.resolve());
      return;
    }
    await restoreList();
    await list.bringToFront().catch(() => {});
  };
  const boundedEvaluate = async (target, expression, argument, timeoutMs, timeoutError) => {
    const operation = target.evaluate(expression, argument);
    const timeout = target.waitForTimeout(timeoutMs).then(() => { throw new Error(timeoutError); });
    return await Promise.race([operation, timeout]);
  };
  const hasVerificationText = async (target) => target.evaluate(() => /验证码|安全验证|访问异常|滑块验证|Access Verification|Please slide to verify/i.test(document.body?.innerText || "")).catch(() => false);
  const locateCard = async () => {
    let located = list.locator(`[data-codex-job-id="${id.replace(/"/g, "\\\"")}"]`).first();
    await located.waitFor({ state: "visible", timeout: 3000 }).catch(() => {});
    if (await located.isVisible().catch(() => false)) return located;
    located = list.locator(`.joblist-item:has(a[href*="${id}"])`).first();
    await located.waitFor({ state: "visible", timeout: 3000 }).catch(() => {});
    if (await located.isVisible().catch(() => false)) return located;
    const title = String(candidate[1] || "").trim();
    const company = String(candidate[2] || "").trim();
    if (title && company) {
      located = list.locator(".joblist-item").filter({ hasText: title }).filter({ hasText: company }).first();
      await located.waitFor({ state: "visible", timeout: 3000 }).catch(() => {});
    }
    return located;
  };
  const resolveExactCardRoute = async (located) => {
    if (!(await located.isVisible().catch(() => false))) return { href: "", alreadyOpen: false };
    const titleTarget = located.locator(".jname,[class*='job-name'],[class*='jobName'],[class*='job-title']").first();
    if (!(await titleTarget.isVisible().catch(() => false))) return { href: "", alreadyOpen: false };
    const armed = await list.evaluate(() => {
      if (window.__codexOriginalWindowOpen) return false;
      window.__codexCapturedWindowOpen = "";
      window.__codexOriginalWindowOpen = window.open;
      window.open = (value) => {
        window.__codexCapturedWindowOpen = String(value || "");
        return null;
      };
      return true;
    }).catch(() => false);
    if (!armed) return { href: "", alreadyOpen: false };
    try {
      await titleTarget.evaluate((node) => node.click());
      await list.waitForTimeout(1000);
      const current = exactJobHref(list.url());
      const captured = await list.evaluate(() => String(window.__codexCapturedWindowOpen || "")).catch(() => "");
      return { href: current || exactJobHref(captured), alreadyOpen: !!current };
    } finally {
      await list.evaluate(() => {
        if (window.__codexOriginalWindowOpen) window.open = window.__codexOriginalWindowOpen;
        delete window.__codexOriginalWindowOpen;
        delete window.__codexCapturedWindowOpen;
      }).catch(() => {});
    }
  };
  const openExactDetail = async (href, maxAttempts = 2) => {
    if (!href) return null;
    for (let attempt = 0; attempt < maxAttempts; attempt++) {
      // The installed Edge extension rejects the transient blank target used
      // by browserContext.newPage(). Keep the workflow single-tab and reuse
      // the verified 51job list page for detail review instead.
      await list.goto(href, { waitUntil: "commit", timeout: 12000 }).catch(() => {});
      if (isAllowed51JobUrl(list.url()) && pageJobId(list.url()) === id) {
        await list.waitForFunction(() => {
          const body = document.body?.innerText || "";
          return /验证码|安全验证|访问异常|滑块验证|Access Verification|Please slide to verify/i.test(body)
            || !!document.querySelector('script[type="application/ld+json"], .job-header-left .job-title, [data-testid="job-title"], [class*="jobName"]')
            || /职位描述|岗位职责|工作职责|工作内容/.test(body);
        }, { timeout: 5000 }).catch(() => {});
        if (await hasVerificationText(list)) return list;
        await list.waitForLoadState("domcontentloaded", { timeout: 4000 }).catch(() => {});
        await list.waitForTimeout(200);
        return list;
      }
      await restoreList();
    }
    return null;
  };
  let card = null;
  // New batches already carry an exact jobHref in candidate[7]. Only pay the
  // DOM recovery cost for older batches that do not have that route.
  if (!routeHref) {
    card = await locateCard();
    if (await card.isVisible().catch(() => false)) {
      const cardHref = await card.locator(`a[href*="${id}"]`).first().getAttribute("href").catch(() => "");
      const vueHref = await card.evaluate((node, expectedId) => {
        for (let owner = node, depth = 0; owner && depth < 10; owner = owner.parentElement, depth++) {
          const jobs = owner.__vue__?.joblist;
          if (!Array.isArray(jobs)) continue;
          const match = jobs.find((item) => String(item?.jobId || item?.job_id || "") === String(expectedId));
          if (match?.jobHref) return String(match.jobHref);
        }
        return "";
      }, id).catch(() => "");
      routeHref = exactJobHref(vueHref) || exactJobHref(cardHref) || routeHref;
    }
  }
  let cardRoute = { href: "", alreadyOpen: false };
  if (!routeHref && card) cardRoute = await resolveExactCardRoute(card);
  routeHref = cardRoute.href || routeHref || `https://jobs.51job.com/all/${id}.html`;
  let detailPage = cardRoute.alreadyOpen ? list : (routeHref ? await openExactDetail(routeHref, 1) : null);
  if (!detailPage && card && !(await card.isVisible().catch(() => false))) {
    await restoreList();
    card = await locateCard();
  }
  if (!detailPage) {
    if (list.url() !== listUrl) await list.goto(listUrl, { waitUntil: "domcontentloaded", timeout: 8000 }).catch(() => {});
    await list.bringToFront();
    return { ok: false, error: "detail_identity_timeout", jobId: id, routeHref, currentUrl: list.url(), currentPageJobId: pageJobId(list.url()), currentUrlAllowed: isAllowed51JobUrl(list.url()) };
  }
  const url = detailPage.url();
  if (await hasVerificationText(detailPage)) {
    context.__codexPending51jobVerification = { detailUrl: url, listUrl, jobId: id };
    return { ok: false, error: "slider_required", jobId: id, detailUrl: url };
  }
  await detailPage.waitForLoadState("domcontentloaded", { timeout: 5000 }).catch(() => {});
  await detailPage.waitForFunction(() => typeof window.__codexJobs?.job51Detail === "function", { timeout: 3000 }).catch(() => {});
  await detailPage.waitForTimeout(200);
  const detailPlatformOk = isAllowed51JobUrl(url);
  if (!detailPlatformOk || /\/pc\/search/i.test(url) || /\/all\/co/i.test(url) || pageJobId(url) !== id) {
    if (detailPage !== list) void (context.__jobflowOwnedPages?.has(detailPage) ? detailPage.close({ runBeforeUnload: false }).catch(() => {}) : Promise.resolve());
    else await list.goto(listUrl, { waitUntil: "domcontentloaded", timeout: 8000 }).catch(() => {});
    await list.bringToFront();
    return { ok: false, error: "detail_page_required", url, jobId: id };
  }
  let detail;
  try {
    detail = await boundedEvaluate(detailPage, () => window.__codexJobs.job51Detail(), undefined, 9000, "detail_read_timeout");
  } catch (error) {
    await recoverToList(detailPage);
    const message = String(error?.message || "");
    return { ok: false, error: message === "detail_read_timeout" ? "detail_read_timeout" : "detail_read_failed", jobId: id };
  }
  let binding;
  try {
    binding = await boundedEvaluate(
      detailPage,
      ({ card: selected, detail: value }) => window.__codexJobPolicy.bindDetail(selected, value),
      { card: candidate, detail },
      5000,
      "detail_binding_timeout",
    );
  } catch (error) {
    await recoverToList(detailPage);
    const message = String(error?.message || "");
    return { ok: false, error: message === "detail_binding_timeout" ? "detail_binding_timeout" : "detail_binding_failed", jobId: id };
  }
  binding = binding || { ok: false, error: "detail_binding_failed", mismatches: [] };
  if (!detail.ok || !binding.ok) {
    if (detail.error === "slider_required") {
      context.__codexPending51jobVerification = { detailUrl: url, listUrl, jobId: id };
      return { ok: false, error: "slider_required", jobId: id, detailUrl: url };
    }
    if (detailPage !== list) void (context.__jobflowOwnedPages?.has(detailPage) ? detailPage.close({ runBeforeUnload: false }).catch(() => {}) : Promise.resolve());
    else await list.goto(listUrl, { waitUntil: "domcontentloaded", timeout: 8000 }).catch(() => {});
    await list.bringToFront();
    return {
      ok: false,
      error: detail.ok ? binding.error : (detail.error || "detail_read_failed"),
      mismatches: binding.mismatches || [],
      jobId: id,
      detailUrl: url,
      detailJobId: detail.jobId || "",
      detailTitle: detail.title || "",
      detailCompany: detail.company || "",
      ...(detail.ok && binding.mismatches?.includes("salary") ? { cardSalary: candidate[4] || "", detailSalary: detail.salary || "" } : {}),
    };
  }
  context.__codexListUrl = listUrl;
  if (!context.__jobflowExistingPages?.has(detailPage)) context.__jobflowOwnedPages?.add(detailPage);
  context.__codexActiveDetail = { detailUrl: detailPage.url(), candidate, laneId: lane.id, cityKey: `51job:${lane.city}` };
  if (detail.autoReject) {
    return {
      ok: true,
      platform: "51job",
      url: detail.url,
      jobId: detail.jobId,
      title: detail.title,
      company: detail.company,
      companyNature: detail.companyNature || "",
      salary: detail.salary || "",
      state: detail.state || "",
      autoReject: detail.autoReject,
      deterministic: true,
      binding: true,
      bindingMeta: binding,
    };
  }
  return { ...detail, binding: true, bindingMeta: binding };
}
