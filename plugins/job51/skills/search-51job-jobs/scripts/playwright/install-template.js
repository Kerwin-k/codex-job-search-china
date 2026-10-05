async (page) => {
  const context = page.context();
  if (!context.__jobflowExistingPages) context.__jobflowExistingPages = new Set(context.pages());
  if (!context.__jobflowOwnedPages) context.__jobflowOwnedPages = new Set();
  const runtimePath = "__RUNTIME_PATH__";
  const policyPath = "__POLICY_PATH__";
  const isAllowed51JobUrl = (value) => {
    return /^https:\/\/(?:we|jobs)\.51job\.com(?:\/|$)/i.test(String(value || ""));
  };
  const isClosable51JobDetail = (value) => {
    const url = String(value || "");
    return /^https:\/\/jobs\.51job\.com\/(?:[^/?#]+\/)?\d+\.html(?:[?#]|$)/i.test(url)
      || /^https:\/\/jobs\.51job\.com\/jobdetail(?:[/?#]|$)/i.test(url)
      || /^https:\/\/jobs\.51job\.com\/applysuccess\.php(?:[?#]|$)/i.test(url);
  };
  const close51JobDetailBounded = async (candidatePage, timeoutMs = 2000) => {
    if (!candidatePage || candidatePage.isClosed() || !isClosable51JobDetail(candidatePage.url())) return false;
    const closeOperation = Promise.resolve()
      .then(() => candidatePage.close({ runBeforeUnload: false }))
      .then(() => true)
      .catch(() => false);
    const timeout = candidatePage.waitForTimeout(timeoutMs).then(() => false).catch(() => false);
    return await Promise.race([closeOperation, timeout]);
  };
  const readListPageState = async (target) => target.evaluate(() => {
    const pager = Number((document.querySelector(".bottom-page .pageation .el-pager li.number.active, .el-pager li.number.active")?.textContent || "").trim()) || 0;
    let sensor = 0;
    try { sensor = Number(JSON.parse(document.querySelector(".joblist-item [sensorsdata]")?.getAttribute("sensorsdata") || "{}").pageNum || 0); } catch {}
    return { pager, sensor };
  }).catch(() => ({ pager: 0, sensor: 0 }));
  context.__codexRestore51JobListPage = async (target) => {
    const targetPage = Math.max(1, Number(context.__codexListPageNumber || 1));
    if (!target || target.isClosed() || !isAllowed51JobUrl(target.url()) || isClosable51JobDetail(target.url())) return false;
    await target.locator(".joblist-item").first().waitFor({ state: "visible", timeout: 5000 }).catch(() => {});
    let observed = await readListPageState(target);
    if ((observed.pager === targetPage || observed.sensor === targetPage) && (observed.pager === 0 || observed.pager === targetPage) && (observed.sensor === 0 || observed.sensor === targetPage)) return true;
    const pages = target.locator(".bottom-page .pageation .el-pager li.number, .el-pager li.number");
    const count = await pages.count().catch(() => 0);
    let match = null;
    for (let index = 0; index < count; index++) {
      const item = pages.nth(index);
      if ((await item.textContent().catch(() => "") || "").trim() === String(targetPage) && await item.isVisible().catch(() => false)) { match = item; break; }
    }
    if (!match) return targetPage === 1;
    await match.evaluate((node) => node.click());
    for (let attempt = 0; attempt < 12; attempt++) {
      await target.waitForTimeout(250);
      observed = await readListPageState(target);
      if ((observed.pager === targetPage || observed.sensor === targetPage) && (observed.pager === 0 || observed.pager === targetPage) && (observed.sensor === 0 || observed.sensor === targetPage)) return true;
    }
    return false;
  };
  context.__codexCleanup51JobDetails = async (keepPages = []) => {
    const keep = new Set((Array.isArray(keepPages) ? keepPages : []).filter(Boolean));
    const configuredList = context.__codexListPage;
    const listUrl = String(context.__codexListUrl || "");
    let restoredList = false;
    if (configuredList && !configuredList.isClosed() && isClosable51JobDetail(configuredList.url()) && isAllowed51JobUrl(listUrl) && !isClosable51JobDetail(listUrl)) {
      restoredList = await configuredList.goto(listUrl, { waitUntil: "commit", timeout: 6000 }).then(() => true).catch(() => false);
      if (restoredList) {
        await configuredList.waitForLoadState("domcontentloaded", { timeout: 3000 }).catch(() => {});
        restoredList = await context.__codexRestore51JobListPage(configuredList);
      }
    }
    if (configuredList && !configuredList.isClosed() && isAllowed51JobUrl(configuredList.url()) && !isClosable51JobDetail(configuredList.url())) keep.add(configuredList);
    if (!keep.size) {
      const safePage = context.pages().find((candidatePage) => !candidatePage.isClosed() && isAllowed51JobUrl(candidatePage.url()) && !isClosable51JobDetail(candidatePage.url()))
        || context.pages().find((candidatePage) => !candidatePage.isClosed());
      if (safePage) keep.add(safePage);
    }
    let closedCount = 0;
    const cleanupDeadline = Date.now() + 10000;
    for (const candidatePage of context.pages()) {
      if (!context.__jobflowOwnedPages.has(candidatePage) || keep.has(candidatePage) || !isClosable51JobDetail(candidatePage.url())) continue;
      if (Date.now() >= cleanupDeadline) break;
      if (await close51JobDetailBounded(candidatePage, Math.min(2000, Math.max(250, cleanupDeadline - Date.now())))) closedCount += 1;
    }
    return { ok: true, closedCount, restoredList };
  };
  await context.addInitScript({ path: policyPath });
  await context.addInitScript({ path: runtimePath });
  const queryParam = (input, key) => {
    const query = String(input || "").split("?")[1]?.split("#")[0] || "";
    for (const part of query.split("&")) {
      const [rawKey, ...rawValue] = part.split("=");
      if (decodeURIComponent(rawKey || "") === key) return decodeURIComponent(rawValue.join("=") || "");
    }
    return "";
  };
  context.__codexRecover51JobLane = async (expectedLane) => {
    if (!expectedLane || expectedLane.platform !== "51job") return { ok: false, error: "recovery_lane_missing" };
    const canonicalSearch = /^https:\/\/we\.51job\.com\/pc\/search(?:[?#]|$)/i;
    let target = context.pages().find((candidatePage) => !candidatePage.isClosed() && canonicalSearch.test(candidatePage.url()));
    if (!target && page && !page.isClosed()) target = page;
    if (!target) return { ok: false, error: "recovery_list_page_missing" };
    const base = String(expectedLane.searchUrl || "");
    if (!/^https:\/\/we\.51job\.com\/pc\/search\?/i.test(base)) return { ok: false, error: "recovery_search_url_invalid" };
    const fullTimeSearchUrl = `${base}${base.includes("?") ? "&" : "?"}jobType=01`;
    await target.goto(fullTimeSearchUrl, { waitUntil: "commit", timeout: 15000 }).catch(() => {});
    await target.waitForLoadState("domcontentloaded", { timeout: 8000 }).catch(() => {});
    await target.waitForTimeout(250);
    const verifiedUrl = target.url();
    const platformOk = isAllowed51JobUrl(verifiedUrl) && canonicalSearch.test(verifiedUrl);
    const cityOk = queryParam(verifiedUrl, "jobArea") === String(expectedLane.cityCode || "");
    let fullTime = queryParam(verifiedUrl, "jobType") === "01";
    if (!fullTime) {
      const wrapper = target.locator(".custom-select-wrapper:visible")
        .filter({ has: target.locator(".fixed-text").filter({ hasText: "工作类型" }) }).first();
      const container = wrapper.locator(".custom-select-container");
      let option = wrapper.locator(".custom-option").filter({ hasText: "全职" }).first();
      if (!(await option.isVisible().catch(() => false))) {
        await container.evaluate((node) => node.click()).catch(() => {});
        await option.waitFor({ state: "visible", timeout: 3000 }).catch(() => {});
      }
      if ((await option.textContent().catch(() => "") || "").trim() === "全职") {
        await option.evaluate((node) => node.click()).catch(() => {});
        await target.waitForTimeout(600);
        fullTime = queryParam(target.url(), "jobType") === "01"
          || await wrapper.evaluate((node) => [...node.querySelectorAll(".custom-option")].some((candidate) => (candidate.textContent || "").trim() === "全职" && candidate.classList.contains("selected"))).catch(() => false);
      }
    }
    const actual = {
      city: cityOk ? String(expectedLane.city || "") : "",
      fullTime,
      salaryFloor: Number(expectedLane.salaryFloor ?? 0),
      salaryMode: "card_gate",
    };
    const filters = platformOk
      ? await target.evaluate(({ actual: observed, lane: expected }) => window.__codexJobPolicy?.validateFilters?.(observed, expected) || { ok: false, errors: ["policy_missing"] }, { actual, lane: expectedLane }).catch(() => ({ ok: false, errors: ["policy_missing"] }))
      : { ok: false, errors: ["platform_mismatch"] };
    if (!filters.ok) return { ok: false, error: "filters_unverified", actual, missing: filters.errors || [] };
    const startPage = Math.max(1, Number(expectedLane.startPage || 1));
    context.__codexListUrl = verifiedUrl;
    context.__codexListPage = target;
    context.__codexListPageNumber = startPage;
    const restoredPage = await context.__codexRestore51JobListPage(target);
    if (!restoredPage) return { ok: false, error: "recovery_page_restore_failed", expectedPage: startPage };
    context.__codexLane = { ...expectedLane, verified: true, pageCalls: startPage - 1, actual, filters, listUrl: verifiedUrl };
    return { ok: true, recovered: true, lane: expectedLane.id, page: startPage, url: target.url() };
  };
  context.__codexTry51JobSliderOnce = async (target) => {
    const verificationText = /\u9a8c\u8bc1\u7801|\u5b89\u5168\u9a8c\u8bc1|\u8bbf\u95ee\u5f02\u5e38|\u6ed1\u5757\u9a8c\u8bc1|Access Verification|Please slide to verify|\u6309\u4f4f.*\u6ed1\u5757|\u62d6\u52a8.*\u6ed1\u5757|\u6ed1\u5757.*\u6700\u53f3|\u5411\u53f3\u6ed1\u52a8.*\u9a8c\u8bc1/i;
    if (!target || target.isClosed() || !isAllowed51JobUrl(target.url())) {
      return { ok: false, detected: false, required: false, attempted: false, cleared: false, error: "platform_mismatch" };
    }
    const pageText = await target.locator("body").innerText({ timeout: 5000 }).catch(() => "");
    const frames = target.frames();
    const primarySelectors = [
      ".aliyunCaptcha-sliding-slider",
      ".nc_iconfont.btn_slide",
      ".btn_slide",
      '[id^="nc_"][id$="_n1z"]',
      ".nc_btn",
      "[class*='slider-btn']",
      "[class*='slide-btn']",
      "[class*='slider'] [role='button']",
    ];
    let sliderFrame = null;
    let handle = null;
    for (const frame of frames) {
      for (const selector of primarySelectors) {
        const candidate = frame.locator(selector).first();
        if (await candidate.isVisible().catch(() => false)) { sliderFrame = frame; handle = candidate; break; }
      }
      if (handle) break;
    }
    if (!handle && !verificationText.test(pageText)) {
      return { ok: true, detected: false, required: false, attempted: false, cleared: false, url: target.url() };
    }
    let trace = (pageText.match(/TraceID:\s*([^\s]+)/i) || [])[1] || "";
    if (!trace && sliderFrame) {
      const frameText = await sliderFrame.locator("body").innerText({ timeout: 3000 }).catch(() => "");
      trace = (frameText.match(/TraceID:\s*([^\s]+)/i) || [])[1] || "";
    }
    let challengeKey = "";
    try { challengeKey = await target.evaluate((traceId) => `${location.origin}${location.pathname}|${traceId || "unknown"}`, trace); } catch {}
    if (!challengeKey) {
      return { ok: false, detected: true, required: true, attempted: false, cleared: false, error: "slider_challenge_identity_missing", url: target.url() };
    }
    context.__codexPending51jobVerification = {
      challengeKey,
      traceId: trace,
      url: target.url(),
      detectedAt: Date.now(),
    };
    return {
      ok: false,
      detected: true,
      required: true,
      attempted: false,
      cleared: false,
      userActionRequired: true,
      challengeKey,
      url: target.url(),
      error: "slider_manual_required",
    };
  };
  const restoreListAfterSubmit = async (source) => {
    const configuredList = context.__codexListPage;
    const listUrl = String(context.__codexListUrl || "");
    if (!source || source.isClosed() || configuredList !== source || !listUrl || !isAllowed51JobUrl(listUrl)) return;
    if (!isClosable51JobDetail(source.url())) return;
    await source.goto(listUrl, { waitUntil: "commit", timeout: 15000 }).catch(() => {});
    await source.waitForLoadState("domcontentloaded", { timeout: 8000 }).catch(() => {});
    await context.__codexRestore51JobListPage(source);
  };
  context.__codexJobSubmit = async (source, platform) => {
    if (platform !== "51job") return { ok: false, error: "platform_mismatch" };
    if (!isAllowed51JobUrl(source.url())) return { ok: false, error: "platform_mismatch", url: source.url() };
    const click = await source.evaluate((value) => window.__codexJobs.clickApply(value), platform);
    if (!click.ok) return click;
    const detectResumeChooser = async () => {
      for (const candidatePage of context.pages()) {
        if (!isAllowed51JobUrl(candidatePage.url())) continue;
        const dialogs = candidatePage.locator('[role="dialog"],.el-dialog,.modal,[class*="dialog"],[class*="modal"]').filter({ hasText: /简历/ });
        if (await dialogs.first().isVisible().catch(() => false)) return true;
      }
      return false;
    };
    let slider = null;
    for (let i = 0; i < 16; i++) {
      await source.waitForTimeout(250);
      if (await detectResumeChooser()) return { ok: false, userActionRequired: true, platform, jobId: click.jobId, error: "resume_selection_required", resumeMode: "chooser", url: source.url() };
      if (slider === null) {
        const result = await context.__codexTry51JobSliderOnce(source);
        if (result.detected) {
          slider = result;
          if (!result.cleared) return { ok: false, userActionRequired: true, platform, jobId: click.jobId, error: result.error, slider: result, url: source.url() };
        }
      }
      for (const candidatePage of context.pages()) {
        const url = candidatePage.url();
        if (!isAllowed51JobUrl(url)) continue;
        if (/applysuccess\.php/i.test(url) && (!click.jobId || url.includes(click.jobId))) {
          await restoreListAfterSubmit(source);
          await context.__codexCleanup51JobDetails();
          return { ok: true, platform, jobId: click.jobId, evidence: "applysuccess.php", successUrl: url, successPageIsCurrent: candidatePage === source, resumeMode: "platform_default", slider };
        }
        const success = await candidatePage.getByText("投递成功", { exact: false }).first().isVisible().catch(() => false);
        if (success) {
          await restoreListAfterSubmit(source);
          await context.__codexCleanup51JobDetails();
          return { ok: true, platform, jobId: click.jobId, evidence: "投递成功", successUrl: url, successPageIsCurrent: candidatePage === source, resumeMode: "platform_default", slider };
        }
      }
    }
    const applied = await source.getByText(/^(已投递|已申请)$/, { exact: true }).first().isVisible().catch(() => false);
    if (!slider?.detected) {
      await restoreListAfterSubmit(source);
      await context.__codexCleanup51JobDetails();
    }
    return { ok: false, pending: true, platform, jobId: click.jobId, url: source.url(), sourceState: applied ? "已投递" : "unknown", slider };
  };

  const jobflowRawSubmit = context.__codexJobSubmit;
  if (!context.__jobflowAttempts) context.__jobflowAttempts = new Map();
  context.__codexJobSubmit = async (source, platform) => {
    const permit = context.__jobflowPermit;
    if (!permit || permit.platform !== platform || permit.expires_at * 1000 <= Date.now()) return {ok:false,error:"apply_authorization_required"};
    const id = String(context.__codexActiveDetail?.candidate?.[0] || "");
    if (!id) return {ok:false,error:"bound_detail_required"};
    const attempt = context.__jobflowAttempt;
    if (!attempt || attempt.job_id !== id || attempt.permit_id !== permit.permit_id || attempt.expires_at * 1000 <= Date.now()) return {ok:false,error:"prepared_send_attempt_required"};
    let attempts = context.__jobflowAttempts.get(permit.permit_id);
    if (!attempts) { attempts = new Set(); context.__jobflowAttempts.set(permit.permit_id, attempts); }
    if (attempts.has(id)) return {ok:false,error:"prior_send_attempt_requires_reconciliation",jobId:id};
    if (attempts.size >= permit.target) return {ok:false,error:"authorized_target_reached"};
    const detail = await source.evaluate(p => p === "51job" ? window.__codexJobs.job51Detail() : window.__codexJobs.zhaopinDetail(), platform).catch(() => null);
    if (!detail?.salary) return {ok:false,error:"detail_salary_unverified"};
    const eligible = await source.evaluate(({salary, lane}) => {
      const policy=window.__codexJobPolicy; const band=policy.salaryBand(salary);
      if (!band) return false;
      const selected=lane.salaryComparison === "upper_bound" ? band.upper : band.lower;
      return selected >= lane.salaryFloor;
    }, {salary:detail.salary,lane:context.__codexLane || {}}).catch(() => false);
    if (!eligible) return {ok:false,error:"detail_salary_below_floor_or_unknown"};
    attempts.add(id);
    return await jobflowRawSubmit(source, platform);
  };

  return { ok: true, version: "2.6-51job-controller",  initScripts: true, currentPageInjected: false, platform: "51job" };
}
