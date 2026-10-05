async (page) => {
  const lane = __LANE_JSON__;
  if (!lane || lane.platform !== "zhaopin") return { ok: false, error: "platform_mismatch" };
  const context = page.context();
  const isRecommendUrl = (value) => {
    const match = String(value || "").match(/^https?:\/\/www\.zhaopin\.com(\/[^?#]*)(?:\?([^#]*))?/i);
    if (!match) return false;
    return match[1] === "/recommend"
      || (match[1] === "/jobs/" && /(?:^|&)pageMode=recommend(?:&|$)/i.test(match[2] || ""));
  };
  const exactIndex = async (locator, selector, text) => {
    const values = await locator.locator(selector).allTextContents().catch(() => []);
    return values.findIndex((value) => value.trim() === text);
  };
  const wrapperByTitle = async (wrapperSelector, titleSelector, title, optionSelector = "", anchorLabel = "") => {
    const wrappers = page.locator(wrapperSelector);
    const count = await wrappers.count().catch(() => 0);
    for (let index = 0; index < count; index++) {
      const wrapper = wrappers.nth(index);
      const currentTitle = (await wrapper.locator(titleSelector).first().textContent().catch(() => "") || "").trim();
      const options = optionSelector
        ? await wrapper.locator(optionSelector).allTextContents().catch(() => [])
        : [];
      if (currentTitle === title || currentTitle.startsWith(`${title}·`) || options.some((text) => text.trim() === anchorLabel)) return wrapper;
    }
    return null;
  };
  const chooseZhaopin = async (title, label) => {
    const wrapper = await wrapperByTitle(
      ".query-select-comp",
      ".query-select-comp__content__text",
      title,
      ".query-select-comp__list__item",
      label,
    );
    if (!wrapper) return false;
    await wrapper.locator(".query-select-comp__content__hotzone").click({ timeoutMs: 3000 }).catch(() => {});
    await page.waitForTimeout(120);
    const index = await exactIndex(wrapper, ".query-select-comp__list__item", label);
    if (index < 0) return false;
    const item = wrapper.locator(".query-select-comp__list__item").nth(index);
    const link = item.locator("a").first();
    if (await link.count()) await link.click({ timeoutMs: 3000 }).catch(() => {});
    else await item.click({ timeoutMs: 3000 }).catch(() => {});
    await page.waitForTimeout(450);
    return true;
  };
  const queryParam = (input, key) => {
    const query = String(input).split("?")[1]?.split("#")[0] || "";
    for (const part of query.split("&")) {
      const [rawKey, ...rawValue] = part.split("=");
      if (decodeURIComponent(rawKey || "") === key) return decodeURIComponent(rawValue.join("=") || "");
    }
    return "";
  };
  const readPool = async (label) => page.evaluate((expected) => {
    const visible = (node) => {
      const rect = node.getBoundingClientRect();
      const style = getComputedStyle(node);
      return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
    };
    const exactText = (node) => (node?.innerText || node?.textContent || "").replace(/\s+/g, " ").trim();
    const items = [...document.querySelectorAll(".intention-tabs__item")].filter(visible);
    const matches = items.filter((item) => exactText(item.querySelector(".intention-tabs__text") || item) === expected);
    if (matches.length !== 1) return { count: matches.length, active: false };
    const item = matches[0];
    const attrs = ["class", "aria-selected", "aria-pressed", "aria-current"].map((name) => item.getAttribute(name) || "");
    return { count: 1, active: attrs.some((value) => /(?:^|[-_\s])(active|selected|current|checked|on)(?:$|[-_\s])/i.test(value) || value === "true") };
  }, label).catch(() => ({ count: 0, active: false }));
  const clickPool = async (label) => page.evaluate((expected) => {
    const visible = (node) => {
      const rect = node.getBoundingClientRect();
      const style = getComputedStyle(node);
      return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
    };
    const exactText = (node) => (node?.innerText || node?.textContent || "").replace(/\s+/g, " ").trim();
    const target = [...document.querySelectorAll(".intention-tabs__item")]
      .find((item) => visible(item) && exactText(item.querySelector(".intention-tabs__text") || item) === expected);
    if (!target) return false;
    target.click();
    return true;
  }, label).catch(() => false);
  const chooseZhaopinPool = async (label) => {
    let state = await readPool(label);
    for (let attempt = 0; attempt < 8 && state.count !== 1; attempt++) {
      await page.waitForTimeout(150);
      state = await readPool(label);
    }
    if (state.count !== 1) return false;
    if (state.active) return true;
    if (!(await clickPool(label))) return false;
    for (let attempt = 0; attempt < 8; attempt++) {
      await page.waitForTimeout(150);
      state = await readPool(label);
      if (state.count === 1 && state.active) return true;
    }
    return false;
  };
  const chooseRecommendCity = async (city, cityCode) => {
    if (!isRecommendUrl(page.url())) return true;
    const isNewFilter = await page.evaluate(() => !!document.querySelector(".filter-region-box")).catch(() => false);
    const currentValue = async () => page.evaluate((newFilter) => {
      const control = newFilter
        ? document.querySelector(".filter-region-box")
        : document.querySelector(".query-location > .content-s > .content-s__item");
      if (!control) return "";
      return (newFilter
        ? control.querySelector(".filter-select-box__label")?.textContent
        : control.querySelector(".content-s__item__text")?.textContent || control.textContent || "").trim();
    }, isNewFilter).catch(() => "");
    const selectedCity = (value) => {
      const text = String(value || "").replace(/\s+/g, "").trim();
      return text === city || text === `全${city}`;
    };
    const selectedNewCity = async () => page.evaluate((args) => {
      const norm = (value) => String(value || "").replace(/\s+/g, "").trim();
      const codeOk = new URL(location.href).searchParams.get("jl") === String(args.cityCode || "");
      const cityNode = [...document.querySelectorAll(".s-cascader__option--selected,.s-cascader__option--active")]
        .find((node) => norm(node.textContent) === args.city);
      const allNode = [...document.querySelectorAll(".s-checkbutton__item--selected,[class*='checkbutton'][class*='selected']")]
        .find((node) => norm(node.textContent) === `全${args.city}`);
      const regionSelected = !!document.querySelector(".filter-region-box--selected");
      return codeOk && !!cityNode && !!allNode && regionSelected;
    }, { city, cityCode }).catch(() => false);
    if (isNewFilter ? await selectedNewCity() : selectedCity(await currentValue())) return true;
    const opened = await page.evaluate((newFilter) => {
      const control = newFilter
        ? document.querySelector(".filter-region-box")
        : document.querySelector(".query-location > .content-s > .content-s__item");
      const trigger = newFilter
        ? control?.querySelector(".filter-select-box__trigger")
        : control?.querySelector(".content-s__item__text");
      if (!trigger) return false;
      trigger.click();
      return true;
    }, isNewFilter).catch(() => false);
    if (!opened) return false;
    await page.waitForTimeout(250);
    const selected = await page.evaluate((args) => {
      const visible = (node) => {
        const rect = node.getBoundingClientRect();
        const style = getComputedStyle(node);
        return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
      };
      const selectors = args.newFilter
        ? ["li.s-cascader__option", ".s-cascader__option-content", "[role=option]", "[class*='region'] [class*='item']", "[class*='city'] [class*='item']", ".filter-region-box [class*='option']"]
        : [".query-location > .query-city .list__item__text"];
      const nodes = selectors.flatMap((selector) => [...document.querySelectorAll(selector)]);
      const target = nodes.find((node) => visible(node) && (node.textContent || "").trim() === args.city);
      if (!target) return false;
      target.click();
      return true;
    }, { newFilter: isNewFilter, city }).catch(() => false);
    if (!selected) return false;
    if (isNewFilter) {
      await page.waitForTimeout(250);
      const selectedAllCity = await page.evaluate((args) => {
        const visible = (node) => {
          const rect = node.getBoundingClientRect();
          const style = getComputedStyle(node);
          return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
        };
        const selectors = [
          "li.s-checkbutton__item",
          ".s-checkbutton__item",
          "[class*='checkbutton'] [class*='item']",
          "[class*='check-button'] [class*='item']",
        ];
        const target = selectors.flatMap((selector) => [...document.querySelectorAll(selector)])
          .find((node) => visible(node) && (node.textContent || "").replace(/\s+/g, "").trim() === args.fullCity);
        if (!target) return false;
        target.click();
        return true;
      }, { fullCity: `全${city}` }).catch(() => false);
      if (!selectedAllCity) return false;
    }
    await page.waitForTimeout(500);
    if (!isRecommendUrl(page.url())) return false;
    return isNewFilter ? await selectedNewCity() : selectedCity(await currentValue());
  };
  for (const p of context.pages()) if (p !== page && /zhaopin\.com\/jobdetail/i.test(p.url())) await p.close().catch(() => {});
  const isExpectationLane = lane.source === "expectation_pool";
  const enteredPoolPage = isExpectationLane && context.__codexZhaopinEntry?.verified
    && isRecommendUrl(page.url());
  if (isExpectationLane && !enteredPoolPage) {
    return { ok: false, error: "expectation_entry_required", platform: lane.platform, lane: lane.id, url: page.url() };
  }
  if (!isExpectationLane) await page.goto(lane.searchUrl, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(500);
  const poolOk = lane.source !== "expectation_pool"
    ? true
    : await chooseZhaopinPool(lane.poolLabel);
  const fullTimeUrlOk = isExpectationLane || queryParam(page.url(), "et") === "2";
  const recommendCitySelected = lane.source !== "expectation_pool"
    ? true
    : await chooseRecommendCity(lane.city, lane.cityCode);
  await page.waitForTimeout(600);
  const url = page.url();
  const expectationContextOk = !isExpectationLane || isRecommendUrl(url);
  if (!expectationContextOk) {
    return { ok: false, error: "expectation_context_lost", platform: lane.platform, lane: lane.id, url };
  }
  const poolTexts = !isExpectationLane ? [] : await page.evaluate(() => {
    const exactText = (node) => (node.innerText || node.textContent || "").replace(/\s+/g, " ").trim();
    return [...document.querySelectorAll("*")].map(exactText).filter(Boolean);
  }).catch(() => []);
  const poolVisibility = !isExpectationLane ? [true] : ["系统工程师", "IT技术支持", "信息技术经理/主管"]
    .map((label) => poolTexts.some((value) => value.trim() === label));
  const poolsStillVisible = poolVisibility.every(Boolean);
  let activePoolState = !isExpectationLane ? { count: 1, active: true } : await readPool(lane.poolLabel);
  if (isExpectationLane && !(activePoolState.count === 1 && activePoolState.active)) {
    for (let attempt = 0; attempt < 6; attempt++) {
      await page.waitForTimeout(200);
      activePoolState = await readPool(lane.poolLabel);
      if (activePoolState.count === 1 && activePoolState.active) break;
    }
  }
  const poolStillActive = !isExpectationLane || activePoolState.count === 1 && activePoolState.active;
  const visibleZhaopinCities = await page.locator(".filter-region-box .filter-select-box__label, .query-select-comp__content__text").allTextContents().catch(() => []);
  const cityOk = queryParam(url, "jl") === lane.cityCode || url.includes(`/jl${lane.cityCode}/`)
    || visibleZhaopinCities.some((text) => text.trim() === lane.city) || recommendCitySelected;
  const fullTimeOk = isExpectationLane ? true : fullTimeUrlOk;
  const salaryOk = queryParam(url, "sl") === "";
  const actual = {
    city: cityOk ? lane.city : "",
    fullTime: fullTimeOk,
    salaryFloor: salaryOk ? lane.salaryFloor : 0,
    salaryMode: salaryOk ? "card_gate" : "",
  };
  const filters = await page.evaluate(({ actual, lane }) => window.__codexJobPolicy.validateFilters(actual, lane), { actual, lane });
  context.__codexJobRun = context.__codexJobRun || { cities: {}, lanes: {} };
  const expectationVerified = poolOk && poolsStillVisible && poolStillActive;
  context.__codexLane = { ...lane, verified: filters.ok && expectationVerified, pageCalls: Math.max(0, Number(lane.startPage || 1) - 1), actual: { ...actual, pool: expectationVerified ? lane.poolLabel || "" : "" }, filters, listUrl: page.url() };
  context.__codexListPage = page;
  context.__codexVerifyLane = null;
  if (!recommendCitySelected) return { ok: false, error: "city_control_unverified", platform: lane.platform, lane: lane.id, city: lane.city };
  if (!filters.ok || !expectationVerified) return { ok: false, error: expectationVerified ? "filters_unverified" : "pool_unverified", platform: lane.platform, lane: lane.id, actual, pool: lane.poolLabel || "", missing: filters.errors };
  return { ok: true, platform: lane.platform, lane: lane.id, round: lane.round, city: lane.city, keyword: lane.keyword, source: lane.source || "keyword", pool: lane.poolLabel || "", startPage: lane.startPage || 1, pageBudget: lane.pageBudget, filters: actual };
}
