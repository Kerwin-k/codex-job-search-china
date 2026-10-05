async (page) => {
  const context = page.context();
  const labels = ["系统工程师", "IT技术支持", "信息技术经理/主管"];
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
  const isEntryUrl = (value) => /zhaopin\.com\/sou(?:[/?#]|$)/i.test(value) || isKeywordUrl(value) || isRecommendUrl(value);
  const waitForVisiblePools = async (attempts = 24) => {
    let found = await visiblePools();
    for (let attempt = 0; attempt < attempts && found.length !== labels.length; attempt++) {
      await page.waitForTimeout(500);
      found = await visiblePools();
    }
    return found;
  };
  const closeExtraRecommendPages = async () => {
    for (const other of context.pages()) {
      if (other === page || other.isClosed() || !isRecommendUrl(other.url())) continue;
      await other.close().catch(() => {});
    }
  };
  const visiblePools = async () => {
    const tabTexts = await page.locator(".intention-tabs__item").allTextContents().catch(() => []);
    const exactTabs = labels.filter((label) => tabTexts.some((text) => (text || "").replace(/\s+/g, " ").trim() === label));
    if (exactTabs.length === labels.length) return exactTabs;
    const found = [];
    for (const label of labels) {
      const candidate = page.getByText(label, { exact: true }).first();
      try {
        await candidate.waitFor({ state: "visible", timeout: 8000 });
        found.push(label);
      } catch {}
    }
    return found;
  };
  if (isKeywordUrl(page.url()) || /zhaopin\.com\/sou(?:[/?#]|$)/i.test(page.url())) {
    await page.goto("https://www.zhaopin.com/jobs/?pageMode=recommend", { waitUntil: "domcontentloaded" }).catch(() => {});
  }
  if (isEntryUrl(page.url())) {
    await page.waitForTimeout(500);
  }
  let visible = isEntryUrl(page.url()) ? await visiblePools() : [];
  if (isEntryUrl(page.url()) && visible.length !== labels.length) visible = await waitForVisiblePools();
  // Some saved recommendation tabs retain a city query (for example jl=763)
  // but render without the expectation pools. Rebase that same page once to
  // the canonical recommendation route before considering manual entry.
  if (isRecommendUrl(page.url()) && visible.length !== labels.length && /[?&]jl=\d+/.test(page.url())) {
    await page.goto("https://www.zhaopin.com/jobs/?pageMode=recommend", { waitUntil: "domcontentloaded" }).catch(() => {});
    visible = await waitForVisiblePools(24);
  }
  if (isRecommendUrl(page.url()) && visible.length !== labels.length) {
    const peers = context.pages().filter((other) => other !== page && !other.isClosed() && isRecommendUrl(other.url()))
      .sort((left, right) => Number(/[?&]jl=\d+/.test(right.url())) - Number(/[?&]jl=\d+/.test(left.url())));
    for (const peer of peers) {
      const peerUrl = peer.url();
      const currentHasCity = /[?&]jl=\d+/.test(page.url());
      if (!currentHasCity && /[?&]jl=\d+/.test(peerUrl)) {
        await page.goto(peerUrl, { waitUntil: "domcontentloaded" }).catch(() => {});
        visible = await waitForVisiblePools(24);
        if (visible.length === labels.length) {
          await peer.close().catch(() => {});
          break;
        }
        continue;
      }
      const peerLabels = await peer.evaluate((expected) => {
        const visible = (node) => {
          const rect = node.getBoundingClientRect();
          const style = getComputedStyle(node);
          return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
        };
        const exactText = (node) => (node.innerText || node.textContent || "").replace(/\s+/g, " ").trim();
        const nodes = [...document.querySelectorAll("*")].filter((node) => {
          if (!visible(node)) return false;
          const text = exactText(node);
          return text && ![...node.children].some((child) => exactText(child) === text);
        });
        return expected.filter((label) => nodes.some((node) => exactText(node) === label));
      }, labels).catch(() => []);
      if (peerLabels.length !== labels.length) continue;
      await page.goto(peerUrl, { waitUntil: "domcontentloaded" }).catch(() => {});
      visible = await waitForVisiblePools(24);
      if (visible.length === labels.length) {
        await peer.close().catch(() => {});
        break;
      }
    }
  }
  // A recommendation tab can retain the SPA URL while its initial pool request
  // was aborted. One bounded same-URL navigation is a safe recovery before
  // asking the user to intervene; it does not create a page or a second context.
  if (visible.length !== labels.length && isRecommendUrl(page.url())) {
    const retryUrl = page.url();
    await page.goto(retryUrl, { waitUntil: "domcontentloaded" }).catch(() => {});
    visible = await waitForVisiblePools(24);
    if (visible.length !== labels.length && page.url() !== retryUrl) {
      await page.goto(retryUrl, { waitUntil: "domcontentloaded" }).catch(() => {});
      visible = await waitForVisiblePools(12);
    }
  }
  if (visible.length === labels.length) {
    await closeExtraRecommendPages();
    context.__codexZhaopinEntry = { verified: true, url: page.url(), labels: visible };
    return { ok: true, platform: "zhaopin", adopted: true, url: page.url(), pools: visible };
  }
  context.__codexZhaopinEntry = null;
  return { ok: false, error: "manual_job_entry_required", url: page.url(), pools: visible };
}
