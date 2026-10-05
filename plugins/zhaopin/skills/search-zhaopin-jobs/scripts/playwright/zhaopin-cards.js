async (page) => {
  const context = page.context();
  const lane = context.__codexLane;
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
  if (!lane || !lane.verified || lane.platform !== "zhaopin") return { ok: false, error: "filters_unverified", cards: [] };
  const list = context.__codexListPage;
  const listRouteOk = list && !list.isClosed() && (lane.source === "expectation_pool"
    ? isRecommendUrl(list.url())
    : isKeywordUrl(list.url()));
  if (!listRouteOk) {
    return { ok: false, error: "list_page_missing", cards: [] };
  }
  const currentUrl = list.url();
  if (lane.source === "expectation_pool" && !isRecommendUrl(currentUrl)) {
    return { ok: false, error: "expectation_context_lost", cards: [], url: currentUrl };
  }
  const isNewCardUi = await list.locator(".job-card").count().catch(() => 0) > 0;
  const recommendCityControl = isNewCardUi
    ? list.locator(".filter-region-box").first()
    : list.locator(".query-location > .content-s > .content-s__item").first();
  const nestedRecommendCity = isNewCardUi
    ? (await recommendCityControl.locator(".filter-select-box__label").first().textContent().catch(() => "") || "").trim()
    : (await recommendCityControl.locator(".content-s__item__text").first().textContent().catch(() => "") || "").trim();
  const rawRecommendCity = nestedRecommendCity || (await recommendCityControl.textContent().catch(() => "") || "").trim();
  const recommendCity = rawRecommendCity === `全${lane.city}` ? lane.city : rawRecommendCity;
  const recommendCityOk = isRecommendUrl(currentUrl) && lane.actual?.city === lane.city
    && recommendCity === lane.city;
  const cityOk = currentUrl.includes(`/jl${lane.cityCode}/`) || new RegExp(`[?&]jl=${lane.cityCode}(?:&|$)`).test(currentUrl) || recommendCityOk;
  const fullTimeOk = lane.source === "expectation_pool" || /[?&]et=2(?:&|$)/.test(currentUrl);
  const salaryOk = !/[?&]sl=[^&]+/i.test(currentUrl);
  if (!cityOk || !fullTimeOk || !salaryOk) return { ok: false, error: "filter_state_drift", cards: [] };
  if (lane.pageCalls >= lane.pageBudget) return { ok: false, action: "switch_lane", terminal: false, reason: "page_budget" };
  const hydrateNewCards = async () => {
    if (!isNewCardUi) return { ok: true, preSkipped: 0, preSkips: {} };
    const clean = (value, n = 100) => String(value || "").replace(/\s+/g, " ").trim().slice(0, n);
    const salaryRe = /(面议|\d+(?:\.\d+)?\s*[-–—~至]\s*\d+(?:\.\d+)?\s*[Kk万千元](?:·\d+薪)?|\d+\s*元)/;
    const cards = await list.locator(".job-card").all().catch(() => []);
    const preSkips = {};
    const hydrationTechnical = [];
    let preSkipped = 0;
    for (let index = 0; index < cards.length && index < 30; index++) {
      const card = cards[index];
      const existingId = await card.getAttribute("data-codex-job-id").catch(() => "") || "";
      const existingHref = await card.getAttribute("data-codex-job-href").catch(() => "") || "";
      if (existingId && existingHref) continue;
      const expected = await list.evaluate(({ index, salarySource }) => {
        const node = [...document.querySelectorAll(".job-card")][index];
        if (!node) return {};
        const clean = (value, n = 100) => String(value || "").replace(/\s+/g, " ").trim().slice(0, n);
        const salaryRe = new RegExp(salarySource, "i");
        const lines = (node.innerText || "").split("\n").map((x) => clean(x, 100)).filter(Boolean);
        const titleRow = clean(node.querySelector(".job-card__title-row")?.innerText, 120);
        const salary = clean(node.querySelector(".job-card__salary,[class*='salary']")?.innerText || "", 40)
          || clean((titleRow.match(salaryRe) || [""])[0], 40)
          || clean((lines.join(" ").match(salaryRe) || [""])[0], 40);
        const title = clean(node.querySelector(".job-card__title-main,.job-card__title-clamp,.job-card__title,[class*='job-title']")?.innerText
          || (titleRow && titleRow.replace(salary, "")) || lines[0], 64);
        const companyNode = node.querySelector(".job-card__company-name,.companyinfo__name,[class*='company-name']");
        const companyLine = lines.find((x) => /(?:有限公司|股份有限公司|集团|银行|医院|学校|研究院)$/.test(x)) || "";
        const company = clean(companyNode?.innerText || (companyLine.match(/^.*?(?:有限公司|股份有限公司|集团|银行|医院|学校|研究院)$/) || [""])[0], 64);
        const city = clean(node.querySelector(".job-card__location")?.innerText
          || (lines.find((x) => /^(成都|重庆|西安|南京|苏州|无锡|广州|深圳|上海|杭州)(?:\s|$)/.test(x)) || ""), 32);
        const state = clean(lines.find((x) => /^(已投递|立即投递|立即沟通|继续沟通)$/.test(x)) || "", 12);
        const tags = [...node.querySelectorAll(".job-card__skill-tags *,.job-card__skill-tags")].map((e) => clean(e.innerText, 24)).filter(Boolean).slice(0, 3);
        return { title, company, city, salary, state, signals: tags.join("/") };
      }, { index, salarySource: salaryRe.source });
      if (!expected.title || !expected.company || !expected.salary) {
        return { ok: false, error: "card_summary_unreadable", index, expected };
      }
      const preflightReason = await list.evaluate(({ summary, activeLane }) => window.__codexJobPolicy.preflightCard([
        "", summary.title, summary.company, summary.city || activeLane.city,
        summary.salary, summary.state, summary.signals,
      ], activeLane), { summary: expected, activeLane: lane });
      if (preflightReason) {
        preSkips[preflightReason] = (preSkips[preflightReason] || 0) + 1;
        preSkipped += 1;
        continue;
      }
      const directHref = await list.evaluate((cardIndex) => {
        const node = [...document.querySelectorAll(".job-card")][cardIndex];
        return node?.querySelector('a[href*="/jobdetail/"]')?.href || "";
      }, index).catch(() => "") || "";
      const directId = (directHref.match(/\/jobdetail\/([^/?]+)\.htm/i) || [])[1] || "";
      if (directId) {
        await list.evaluate(({ index: cardIndex, id: valueId, href: valueHref }) => {
          const node = [...document.querySelectorAll(".job-card")][cardIndex];
          if (node) {
            node.setAttribute("data-codex-job-id", valueId);
            node.setAttribute("data-codex-job-href", valueHref);
          }
        }, { index, id: directId, href: directHref });
        continue;
      }
      const clickCardTrigger = async () => list.evaluate((cardIndex) => {
        const node = [...document.querySelectorAll(".job-card")][cardIndex];
        if (!node) return false;
        node.scrollIntoView({ block: "center", inline: "nearest" });
        const trigger = node.querySelector(".job-card__title-main,.job-card__title-clamp,.job-card__title,.job-card__title-row,[class*='job-title']") || node;
        trigger.click();
        return true;
      }, index).catch(() => false);
      const readPanelState = async () => list.evaluate(() => {
        const visible = (node) => !!(node && node.getClientRects().length && getComputedStyle(node).visibility !== "hidden");
        const panel = [...document.querySelectorAll(".job-detail-panel,[class*='job-detail-panel']")].find(visible);
        if (!panel) return { href: "", text: "" };
        const link = panel.querySelector("a.job-company-info__view-all[href*='/jobdetail/'],a[href*='/jobdetail/']");
        return { href: link?.href || "", text: (panel.innerText || panel.textContent || "").replace(/\s+/g, " ").slice(0, 6000) };
      }).catch(() => ({ href: "", text: "" }));
      const beforeState = await readPanelState();
      let clicked = await clickCardTrigger();
      let href = "";
      let anchorsReady = false;
      for (let attempt = 0; attempt < 8; attempt++) {
        await list.waitForTimeout(150);
        if (attempt === 4 && !anchorsReady) clicked = await clickCardTrigger() || clicked;
        const panelState = await readPanelState();
        href = panelState.href;
        const titleReady = expected.title && panelState.text.includes(expected.title);
        const companyReady = expected.company && panelState.text.includes(expected.company);
        const salaryReady = expected.salary && panelState.text.includes(expected.salary);
        anchorsReady = [titleReady, companyReady, salaryReady].filter(Boolean).length >= 2;
        if (href && anchorsReady) break;
      }
      if (!clicked || !href || !anchorsReady) {
        hydrationTechnical.push({
          index,
          reason: "card_detail_update_unverified",
          expected: { title: expected.title, company: expected.company, salary: expected.salary },
          actualHref: href,
          previousHref: beforeState.href,
        });
        if (hydrationTechnical.length >= 3) {
          return { ok: false, error: "card_hydration_circuit", hydrationTechnical, preSkipped, preSkips };
        }
        continue;
      }
      const id = (href.match(/\/jobdetail\/([^/?]+)\.htm/i) || [])[1] || "";
      if (!id) {
        hydrationTechnical.push({ index, reason: "card_detail_identity_missing", href });
        if (hydrationTechnical.length >= 3) {
          return { ok: false, error: "card_hydration_circuit", hydrationTechnical, preSkipped, preSkips };
        }
        continue;
      }
      const detail = await list.evaluate(() => window.__codexJobs.zhaopinDetail());
      const candidate = [id, expected.title, expected.company, expected.city || lane.city, expected.salary, expected.state, expected.signals, href];
      const binding = await list.evaluate(({ candidate: value, detail: actual }) => window.__codexJobPolicy.bindDetail(value, actual), { candidate, detail });
      const wrapperCompany = binding.mismatches?.length === 1 && binding.mismatches[0] === "company"
        && /客户公司|客户企业|代招|猎头|人才服务|人力资源/i.test(`${expected.company} ${detail.company || ""}`);
      if (!detail.ok || !binding.ok) {
        if (detail.ok && wrapperCompany) {
          await list.evaluate(({ index: cardIndex, id: valueId, href: valueHref }) => {
            const node = [...document.querySelectorAll(".job-card")][cardIndex];
            if (node) {
              node.setAttribute("data-codex-job-id", valueId);
              node.setAttribute("data-codex-job-href", valueHref);
              node.setAttribute("data-codex-detail-risk", "客户公司包装");
            }
          }, { index, id, href });
          continue;
        }
        return { ok: false, error: detail.ok ? "card_detail_binding_mismatch" : "card_detail_read_failed", index,
          jobId: id, mismatches: binding.mismatches || [], expected: { title: expected.title, company: expected.company, salary: expected.salary },
          actual: { title: detail.title || "", company: detail.company || "", salary: detail.salary || "", jobId: detail.jobId || "" } };
      }
      await list.evaluate(({ index: cardIndex, id: valueId, href: valueHref }) => {
        const node = [...document.querySelectorAll(".job-card")][cardIndex];
        if (node) {
          node.setAttribute("data-codex-job-id", valueId);
          node.setAttribute("data-codex-job-href", valueHref);
        }
      }, { index, id, href });
    }
    return { ok: true, preSkipped, preSkips, hydrationTechnical };
  };
  if (lane.pageCalls > 0) {
    const readTransitionFingerprint = () => list.evaluate((newUi) => {
      const clean = (value, n = 100) => String(value || "").replace(/\s+/g, " ").trim().slice(0, n);
      if (newUi) return [...document.querySelectorAll(".job-card")].slice(0, 30).map((node) => {
        const title = clean(node.querySelector(".job-card__title-main,.job-card__title-clamp,.job-card__title,[class*='job-title']")?.textContent, 64);
        const company = clean(node.querySelector(".job-card__company-name,.companyinfo__name,[class*='company-name']")?.textContent, 64);
        const salary = clean(node.querySelector(".job-card__salary,[class*='salary']")?.textContent, 40);
        return [title, company, salary].join("~");
      }).filter((value) => value !== "~~");
      return [...document.querySelectorAll('.joblist-box__item a[href*="/jobdetail/"]')].slice(0, 30).map((node) => {
        const href = node.href || "";
        return (href.match(/\/jobdetail\/([^/?]+)\.htm/i) || [])[1] || "";
      }).filter(Boolean);
    }, isNewCardUi).catch(() => []);
    const beforeFingerprint = (await readTransitionFingerprint()).join("|");
    const current = Number((list.url().match(/\/p(\d+)/i) || [])[1] || lane.pageCalls);
    const pages = list.getByText(String(current + 1), { exact: true });
    const count = await pages.count().catch(() => 0);
    let next = null;
    if (count > 0) next = pages.nth(0);
    if (!next) return { ok: false, action: "switch_lane", terminal: false, reason: "pager_exhausted" };
    await next.click({ timeoutMs: 5000 }).catch(() => {});
    let stableFingerprint = "";
    let stableReads = 0;
    const deadline = Date.now() + 10000;
    while (Date.now() < deadline) {
      await list.waitForTimeout(400);
      const fingerprint = (await readTransitionFingerprint()).join("|");
      if (fingerprint && fingerprint !== beforeFingerprint) {
        stableReads = fingerprint === stableFingerprint ? stableReads + 1 : 1;
        stableFingerprint = fingerprint;
        if (stableReads >= 2) break;
      }
    }
    if (stableReads < 2) return { ok: false, error: "page_transition_unverified", terminal: false, cards: [] };
  }
  await list.locator(isNewCardUi ? ".job-card" : ".joblist-box__item").first().waitFor({ state: "visible", timeout: 8000 }).catch(() => {});
  const hydrated = await hydrateNewCards();
  if (!hydrated.ok) return { ok: false, ...hydrated, lane: lane.id, pageCall: lane.pageCalls, pageBudget: lane.pageBudget, cards: [] };
  lane.pageCalls += 1;
  lane.listUrl = list.url();
  const result = await list.evaluate((activeLane) => window.__codexJobs.zhaopinCards(activeLane), lane);
  for (const [reason, count] of Object.entries(hydrated.preSkips || {})) {
    result.skips[reason] = (result.skips[reason] || 0) + count;
  }
  result.rawCount += Number(hydrated.preSkipped || 0) + Number(hydrated.hydrationTechnical?.length || 0);
  const pageFingerprint = (result.cards || []).map((card) => card[0]).join("|");
  return { ...result, lane: lane.id, pageCall: lane.pageCalls, pageBudget: lane.pageBudget, pageFingerprint,
    hydrationTechnical: hydrated.hydrationTechnical || [], technicalCount: Number(hydrated.hydrationTechnical?.length || 0) };
}
