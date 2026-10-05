async (page) => {
  const context = page.context();
  const isAllowed51JobUrl = (value) => /^https:\/\/(?:we|jobs)\.51job\.com(?:\/|$)/i.test(String(value || ""));
  const lane = context.__codexLane;
  if (!lane || !lane.verified || lane.platform !== "51job") return { ok: false, error: "filters_unverified", cards: [] };
  const list = context.pages().find((candidate) => !candidate.isClosed() && candidate.url() === lane.listUrl) || page;
  const currentUrl = list.url();
  const platformOk = isAllowed51JobUrl(currentUrl);
  const cityOk = platformOk && new RegExp(`[?&]jobArea=${lane.cityCode}(?:&|$)`).test(currentUrl);
  const filterStateOk = lane.actual && lane.actual.fullTime === true && lane.actual.salaryMode === "card_gate";
  if (!cityOk || !filterStateOk) return { ok: false, error: "filter_state_drift", cards: [] };
  if (lane.pageCalls >= lane.pageBudget) return { ok: false, action: "switch_lane", terminal: false, reason: "page_budget" };
  const readPageState = async () => list.evaluate(() => {
    const pager = Number((document.querySelector(".bottom-page .pageation .el-pager li.number.active, .el-pager li.number.active")?.textContent || "").trim()) || 0;
    let sensor = 0;
    try { sensor = Number(JSON.parse(document.querySelector(".joblist-item [sensorsdata]")?.getAttribute("sensorsdata") || "{}").pageNum || 0); } catch {}
    return { pager, sensor };
  });
  const targetPage = Math.max(1, Number(lane.pageCalls || 0) + 1);
  const before = await readPageState();
  if (before.pager !== targetPage && before.sensor !== targetPage) {
    const pages = list.locator(".bottom-page .pageation .el-pager li.number, .el-pager li.number");
    const count = await pages.count().catch(() => 0);
    let target = null;
    for (let index = 0; index < count; index++) {
      const item = pages.nth(index);
      const text = (await item.textContent().catch(() => "") || "").trim();
      if (text === String(targetPage) && await item.isVisible().catch(() => false)) { target = item; break; }
    }
    if (!target) return { ok: false, action: "switch_lane", terminal: false, reason: "pager_exhausted", expectedPage: targetPage };
    await target.evaluate((node) => node.click());
    let observed = { pager: 0, sensor: 0 };
    for (let attempt = 0; attempt < 12; attempt++) {
      await list.waitForTimeout(250);
      observed = await readPageState();
      const pagerOk = observed.pager === 0 || observed.pager === targetPage;
      const sensorOk = observed.sensor === 0 || observed.sensor === targetPage;
      if ((observed.pager === targetPage || observed.sensor === targetPage) && pagerOk && sensorOk) break;
    }
    if (!((observed.pager === targetPage || observed.sensor === targetPage)
      && (observed.pager === 0 || observed.pager === targetPage)
      && (observed.sensor === 0 || observed.sensor === targetPage))) {
      return { ok: false, error: "pager_postcondition_failed", expectedPage: targetPage, observedPage: observed };
    }
  }
  await list.locator(".joblist-item").first().waitFor({ state: "visible", timeout: 8000 }).catch(() => {});
  const pageState = await readPageState();
  if ((pageState.pager && pageState.pager !== targetPage) || (pageState.sensor && pageState.sensor !== targetPage)) {
    return { ok: false, error: "pager_state_drift", expectedPage: targetPage, observedPage: pageState };
  }
  lane.pageCalls = targetPage;
  lane.listUrl = list.url();
  context.__codexListUrl = lane.listUrl;
  context.__codexListPageNumber = targetPage;
  const result = await list.evaluate((activeLane) => window.__codexJobs.job51Cards(activeLane), lane);
  return { ...result, lane: lane.id, pageCall: lane.pageCalls, browserPage: targetPage, pageBudget: lane.pageBudget };
}
