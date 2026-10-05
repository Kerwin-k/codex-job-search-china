async (page) => {
  const lane = __LANE_JSON__;
  const context = page.context();
  const isAllowed51JobUrl = (value) => /^https:\/\/(?:we|jobs)\.51job\.com(?:\/|$)/i.test(String(value || ""));
  if (!lane || lane.platform !== "51job") return { ok: false, error: "platform_mismatch" };
  if (context.__codexCleanup51JobDetails) await context.__codexCleanup51JobDetails([page]);
  const choose51job = async (title, label) => {
    const resolveWrapper = () => page.locator(".custom-select-wrapper:visible")
      .filter({ has: page.locator(".fixed-text").filter({ hasText: title }) }).first();
    let wrapper = resolveWrapper();
    if (!(await wrapper.count().catch(() => 0))) return false;
    const container = wrapper.locator(".custom-select-container");
    let option = wrapper.locator(".custom-option").filter({ hasText: label }).first();
    if (!(await option.isVisible().catch(() => false))) {
      await container.evaluate((node) => node.click());
      option = wrapper.locator(".custom-option").filter({ hasText: label }).first();
      await option.waitFor({ state: "visible", timeout: 3000 }).catch(() => {});
    }
    if (!(await option.count().catch(() => 0))) return false;
    if ((await option.textContent().catch(() => "") || "").trim() !== label) return false;
    let isSelected = /(?:^|\s)selected(?:\s|$)/.test(await option.getAttribute("class").catch(() => "") || "");
    if (!isSelected) {
      await option.evaluate((node) => node.click());
      await page.waitForTimeout(800);
    }
    wrapper = resolveWrapper();
    option = wrapper.locator(".custom-option").filter({ hasText: label }).first();
    const currentTitle = (await wrapper.locator(".fixed-text").first().textContent().catch(() => "") || "").trim();
    isSelected = await wrapper.evaluate((node, expected) => [...node.querySelectorAll(".custom-option")]
      .some((candidate) => (candidate.textContent || "").trim() === expected && candidate.classList.contains("selected")), label).catch(() => false);
    if (!isSelected && currentTitle.startsWith(`${title}·${label}`)) isSelected = true;
    if (!isSelected && !(await option.isVisible().catch(() => false))) {
      await container.evaluate((node) => node.click());
      await option.waitFor({ state: "visible", timeout: 3000 }).catch(() => {});
      isSelected = await wrapper.evaluate((node, expected) => [...node.querySelectorAll(".custom-option")]
        .some((candidate) => (candidate.textContent || "").trim() === expected && candidate.classList.contains("selected")), label).catch(() => false);
    }
    return isSelected;
  };
  const queryParam = (input, key) => {
    const query = String(input).split("?")[1]?.split("#")[0] || "";
    for (const part of query.split("&")) {
      const [rawKey, ...rawValue] = part.split("=");
      if (decodeURIComponent(rawKey || "") === key) return decodeURIComponent(rawValue.join("=") || "");
    }
    return "";
  };
  const fullTimeSearchUrl = `${String(lane.searchUrl || "")}${String(lane.searchUrl || "").includes("?") ? "&" : "?"}jobType=01`;
  await page.goto(fullTimeSearchUrl, { waitUntil: "commit", timeout: 15000 }).catch(() => {});
  await page.locator(".custom-select-wrapper:visible").first().waitFor({ state: "visible", timeout: 5000 }).catch(() => {});
  await page.waitForTimeout(200);
  const initialUrl = page.url();
  const fullTime = queryParam(initialUrl, "jobType") === "01" || await choose51job("工作类型", "全职");
  await page.waitForTimeout(600);
  const finalUrl = page.url();
  const verifiedUrl = finalUrl || initialUrl;
  const platformOk = isAllowed51JobUrl(verifiedUrl);
  const cityOk = queryParam(verifiedUrl, "jobArea") === lane.cityCode;
  const actual = {
    city: cityOk ? lane.city : "",
    fullTime,
    salaryFloor: lane.salaryFloor,
    salaryMode: "card_gate",
  };
  const filters = platformOk
    ? await page.evaluate(({ actual: observed, lane: expected }) => window.__codexJobPolicy.validateFilters(observed, expected), { actual, lane })
    : { ok: false, errors: ["platform_mismatch"] };
  context.__codexLane = { ...lane, verified: filters.ok, pageCalls: Math.max(0, Number(lane.startPage || 1) - 1), actual, filters, listUrl: verifiedUrl };
  context.__codexListUrl = verifiedUrl;
  context.__codexListPage = page;
  context.__codexVerifyLane = null;
  if (!filters.ok) return { ok: false, error: "filters_unverified", platform: "51job", lane: lane.id, actual, missing: filters.errors };
  return { ok: true, platform: "51job", lane: lane.id, round: lane.round, city: lane.city, keyword: lane.keyword, source: lane.source || "keyword", startPage: lane.startPage || 1, pageBudget: lane.pageBudget, filters: actual };
}
