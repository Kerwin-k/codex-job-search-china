async (page) => {
  const candidate = __CANDIDATE_JSON__;
  const context = page.context();
  const lane = context.__codexLane;
  if (!lane || lane.platform !== "zhaopin") return { ok: false, error: "platform_mismatch" };
  const isRecommendUrl = (value) => {
    const match = String(value || "").match(/^https?:\/\/www\.zhaopin\.com(\/[^?#]*)(?:\?([^#]*))?/i);
    if (!match) return false;
    return match[1] === "/recommend"
      || (match[1] === "/jobs/" && /(?:^|&)pageMode=recommend(?:&|$)/i.test(match[2] || ""));
  };
  const isKeywordUrl = (value) => {
    const text = String(value || "");
    const match = text.match(/^https?:\/\/(sou|www)\.zhaopin\.com(\/[^?#]*)(?:\?([^#]*))?/i);
    if (!match) return false;
    const host = String(match[1] || "").toLowerCase();
    const path = match[2] || "/";
    const query = match[3] || "";
    const pathOk = host === "sou" || (host === "www" && (path === "/jobs" || path === "/jobs/"));
    return pathOk && /(?:^|&)kw=[^&]+(?:&|$)/i.test(query);
  };
  if (!lane || !lane.verified) return { ok: false, error: "filters_unverified" };
  const list = context.__codexListPage;
  const zhaopinListOk = lane.source === "expectation_pool"
    ? isRecommendUrl(list?.url() || "")
    : isKeywordUrl(list?.url() || "");
  if (!list || list.isClosed() || !zhaopinListOk) {
    return { ok: false, error: "list_page_missing" };
  }
  const run = context.__codexJobRun || (context.__codexJobRun = { cities: {}, lanes: {} });
  const laneState = run.lanes[lane.id] || (run.lanes[lane.id] = { jds: 0, submits: 0, jdsSinceSubmit: 0 });
  const cityState = run.cities[`${lane.platform}:${lane.city}`] || (run.cities[`${lane.platform}:${lane.city}`] = { jds: 0, submits: 0, jdsSinceSubmit: 0 });
  const guard = await list.evaluate((s) => window.__codexJobPolicy.budgetDecision(s), {
    laneJdsSinceSubmit: laneState.jdsSinceSubmit || 0,
    cityJdsSinceSubmit: cityState.jdsSinceSubmit || 0,
  });
  if (guard.action !== "continue") return { ok: false, ...guard, platform: lane.platform, lane: lane.id, city: lane.city };
  for (const p of context.pages()) if (p !== list && /zhaopin\.com\/jobdetail/i.test(p.url())) await p.close().catch(() => {});
  await list.bringToFront();
  const id = String(candidate[0]);
  const expectedHref = String(candidate[7] || "");
  const hrefJobId = (value) => (String(value || "").match(/\/jobdetail\/([^/?]+)\.htm/i) || [])[1] || "";
  if (!expectedHref || hrefJobId(expectedHref) !== id) {
    return { ok: false, error: "candidate_href_unverified", jobId: id };
  }
  let card = list.locator(`[data-codex-job-id="${id.replace(/"/g, "\\\"")}"]`).first();
  await card.waitFor({ state: "visible", timeout: 3000 }).catch(() => {});
  if (!(await card.count().catch(() => 0))) return { ok: false, error: "stale_card", jobId: id };
  const newCardUi = await list.locator(".job-card").count().catch(() => 0) > 0;
  if (newCardUi) {
    const resolveCardIndex = async () => list.evaluate(({ expectedId, title, company }) => {
      const cards = [...document.querySelectorAll(".job-card")];
      const exact = cards.findIndex((node) => node.getAttribute("data-codex-job-id") === expectedId);
      if (exact >= 0) return exact;
      const matches = cards.map((node, index) => ({ index, text: (node.innerText || node.textContent || "").replace(/\s+/g, " ") }))
        .filter((item) => (!title || item.text.includes(title)) && (!company || item.text.includes(company)));
      return matches.length === 1 ? matches[0].index : -1;
    }, { expectedId: id, title: String(candidate[1] || ""), company: String(candidate[2] || "") }).catch(() => -1);
    const clickCardTrigger = async () => {
      const cardIndex = await resolveCardIndex();
      if (cardIndex < 0) return false;
      return await list.evaluate((index) => {
        const node = [...document.querySelectorAll(".job-card")][index];
        if (!node) return false;
        node.scrollIntoView({ block: "center", inline: "nearest" });
        const trigger = node.querySelector(".job-card__title-main,.job-card__title-clamp,.job-card__title,.job-card__title-row,[class*='job-title']") || node;
        trigger.click();
        return true;
      }, cardIndex).catch(() => false);
    };
    const readPanelState = async () => list.evaluate(() => {
      const visible = (node) => !!(node && node.getClientRects().length && getComputedStyle(node).visibility !== "hidden");
      const panel = [...document.querySelectorAll(".job-detail-panel,[class*='job-detail-panel']")].find(visible);
      if (!panel) return { href: "", text: "" };
      const link = panel.querySelector("a.job-company-info__view-all[href*='/jobdetail/'],a[href*='/jobdetail/']");
      return { href: link?.href || "", text: (panel.innerText || panel.textContent || "").replace(/\s+/g, " ").slice(0, 8000) };
    }).catch(() => ({ href: "", text: "" }));
    const readPanel = async () => {
      const clicked = await clickCardTrigger();
      if (!clicked) return { href: "", actualId: "", detail: { ok: false, error: "stale_card" }, binding: { ok: false, mismatches: ["jobId"] } };
      let href = "";
      let anchorsReady = false;
      for (let attempt = 0; attempt < 8; attempt++) {
        await list.waitForTimeout(150);
        if (attempt === 4 && !anchorsReady) await clickCardTrigger();
        const panelState = await readPanelState();
        href = panelState.href;
        const titleReady = candidate[1] && panelState.text.includes(String(candidate[1]));
        const companyReady = candidate[2] && panelState.text.includes(String(candidate[2]));
        const salaryReady = candidate[4] && panelState.text.includes(String(candidate[4]));
        anchorsReady = [titleReady, companyReady, salaryReady].filter(Boolean).length >= 2;
        if (hrefJobId(href) === id && anchorsReady) break;
      }
      const actualId = hrefJobId(href);
      if (actualId !== id || !anchorsReady) {
        return { href, actualId, detail: { ok: false, error: "detail_identity_timeout" }, binding: { ok: false, mismatches: ["jobId"] } };
      }
      const detail = await list.evaluate(() => window.__codexJobs.zhaopinDetail());
      const binding = await list.evaluate(({ card: value, detail: actual }) => window.__codexJobPolicy.bindDetail(value, actual), { card: candidate, detail });
      return { href, actualId, detail, binding };
    };
    let panelRead = await readPanel();
    let bindingRetry = false;
    if (!panelRead.detail?.ok || panelRead.actualId !== id || !panelRead.binding?.ok) {
      bindingRetry = true;
      await list.reload({ waitUntil: "domcontentloaded", timeout: 8000 }).catch(() => {});
      await list.locator(".job-card").first().waitFor({ state: "visible", timeout: 8000 }).catch(() => {});
      const cards = await list.locator(".job-card").all().catch(() => []);
      let rebound = null;
      for (const possible of cards) {
        const text = (await possible.textContent().catch(() => "") || "").replace(/\s+/g, " ");
        if (candidate[1] && text.includes(candidate[1]) && (!candidate[2] || text.includes(candidate[2]))) { rebound = possible; break; }
      }
      if (!rebound) return { ok: false, error: "stale_card", jobId: id, bindingRetry: true };
      panelRead = await (async () => {
        card = rebound;
        return await readPanel();
      })();
    }
    if (!panelRead.detail?.ok || panelRead.actualId !== id || !panelRead.binding?.ok) {
      const evidence = {
        expected: { jobId: id, title: candidate[1], company: candidate[2], salary: candidate[4], href: expectedHref },
        actual: { jobId: panelRead.detail?.jobId || panelRead.actualId || "", title: panelRead.detail?.title || "", company: panelRead.detail?.company || "", salary: panelRead.detail?.salary || "", url: list.url() },
        mismatches: panelRead.binding?.mismatches || [], lane: lane.id, page: lane.pageCalls, bindingRetry,
      };
      return { ok: false, error: panelRead.detail?.ok ? "detail_binding_mismatch" : (panelRead.detail?.error || "detail_read_failed"), mismatches: evidence.mismatches, jobId: id, technicalEvidence: evidence };
    }
    const detail = panelRead.detail;
    const laneState = run.lanes[lane.id];
    const cityState = run.cities[`${lane.platform}:${lane.city}`];
    laneState.jds += 1;
    laneState.jdsSinceSubmit = (laneState.jdsSinceSubmit || 0) + 1;
    cityState.jds += 1;
    cityState.jdsSinceSubmit = (cityState.jdsSinceSubmit || 0) + 1;
    context.__codexActiveDetail = { detailUrl: list.url(), candidate, laneId: lane.id, cityKey: `${lane.platform}:${lane.city}`, mode: "panel", expectedJobId: id };
    return { ...detail, binding: true, laneJds: laneState.jds, cityJds: cityState.jds, laneJdsSinceSubmit: laneState.jdsSinceSubmit, cityJdsSinceSubmit: cityState.jdsSinceSubmit };
  }
  const before = new Set(context.pages());
  const link = card.locator(`a[href*="/jobdetail/${id}.htm"]`).first();
  const currentHref = await link.getAttribute("href").catch(() => "");
  if (!(await link.count().catch(() => 0)) || hrefJobId(currentHref) !== id) {
    return { ok: false, error: "candidate_href_unverified", jobId: id };
  }
  await link.click({ timeoutMs: 5000 }).catch(() => {});
  let detailPage = null;
  const deadline = Date.now() + 8000;
  while (Date.now() < deadline && !detailPage) {
    detailPage = context.pages().find((candidatePage) => !candidatePage.isClosed() && hrefJobId(candidatePage.url()) === id) || null;
    if (!detailPage) await list.waitForTimeout(200);
  }
  if (!detailPage) {
    for (const candidatePage of context.pages()) {
      if (context.__jobflowOwnedPages?.has(candidatePage) && !before.has(candidatePage) && candidatePage !== list) await candidatePage.close().catch(() => {});
    }
    await list.bringToFront();
    return { ok: false, error: "detail_identity_timeout", jobId: id };
  }
  await detailPage.waitForLoadState("domcontentloaded", { timeout: 8000 }).catch(() => {});
  const url = detailPage.url();
  if (/\/pc\/search/i.test(url) || /\/all\/co/i.test(url)) return { ok: false, error: "detail_page_required", url };
  const method = "zhaopinDetail";
  let detail = await detailPage.evaluate((m) => window.__codexJobs[m](), method);
  let binding = await detailPage.evaluate(({ card, detail }) => window.__codexJobPolicy.bindDetail(card, detail), { card: candidate, detail });
  let bindingRetry = false;
  if (detail.ok && !binding.ok) {
    bindingRetry = true;
    await detailPage.reload({ waitUntil: "domcontentloaded", timeout: 8000 }).catch(() => {});
    await detailPage.waitForTimeout(400);
    detail = await detailPage.evaluate((m) => window.__codexJobs[m](), method);
    binding = await detailPage.evaluate(({ card, detail }) => window.__codexJobPolicy.bindDetail(card, detail), { card: candidate, detail });
  }
  if (!detail.ok || !binding.ok) {
    const evidence = {
      expected: { jobId: id, title: candidate[1], company: candidate[2], salary: candidate[4], href: expectedHref },
      actual: { jobId: detail.jobId || "", title: detail.title || "", company: detail.company || "", salary: detail.salary || "", url: detailPage.url() },
      mismatches: binding.mismatches || [],
      lane: lane.id,
      page: lane.pageCalls,
      bindingRetry,
    };
    if (detailPage !== list) await (context.__jobflowOwnedPages?.has(detailPage) ? detailPage.close().catch(() => {}) : Promise.resolve());
    await list.bringToFront();
    return { ok: false, error: detail.ok ? binding.error : (detail.error || "detail_read_failed"), mismatches: binding.mismatches || [], jobId: id, technicalEvidence: evidence };
  }
  laneState.jds += 1;
  laneState.jdsSinceSubmit = (laneState.jdsSinceSubmit || 0) + 1;
  cityState.jds += 1;
  cityState.jdsSinceSubmit = (cityState.jdsSinceSubmit || 0) + 1;
  if (!context.__jobflowExistingPages?.has(detailPage)) context.__jobflowOwnedPages?.add(detailPage);
  context.__codexActiveDetail = { detailUrl: detailPage.url(), candidate, laneId: lane.id, cityKey: `${lane.platform}:${lane.city}` };
  return { ...detail, binding: true, laneJds: laneState.jds, cityJds: cityState.jds, laneJdsSinceSubmit: laneState.jdsSinceSubmit, cityJdsSinceSubmit: cityState.jdsSinceSubmit };
}
