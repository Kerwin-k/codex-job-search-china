(() => {
  const VERSION = "2.1";
  const clean = (s, n = 80) => String(s || "").replace(/\s+/g, " ").trim().slice(0, n);
  const lines = () => [...new Set((document.body?.innerText || "").split("\n").map((x) => clean(x, 260)).filter(Boolean))];
  const first = (xs, re, n = 80) => clean(xs.find((x) => re.test(x)) || "", n);
  const visible = (e) => !!(e && e.getClientRects().length && getComputedStyle(e).visibility !== "hidden");
  const jobId = (platform, url = location.href) => platform === "zhaopin"
    ? (url.match(/jobdetail\/([^/?]+)\.htm/i) || [])[1] || ""
    : (url.match(/\/(\d+)\.html/i) || [])[1] || new URL(url).searchParams.get("jobid") || "";
  const status = () => {
    const t = document.body?.innerText || "";
    const securityLine = t.split(/\r?\n/).map((line) => clean(line, 120)).some((line) =>
      /^(?:访问异常|异常访问|检测到异常访问)$/.test(line));
    return { security: /验证码|安全验证|滑块验证/.test(t) || securityLine, loggedOut: /登录后可投递|请先登录|请登录后/.test(t) };
  };
  const detailBase = (platform, stops) => {
    const z = platform === "zhaopin";
    const detailRoot = z
      ? [...document.querySelectorAll(".job-detail-panel,[class*='job-detail-panel']")].find(visible) || document
      : document;
    const scopedLines = () => [...new Set((detailRoot.innerText || "").split("\n").map((x) => clean(x, 260)).filter(Boolean))];
    const xs = z && detailRoot !== document ? scopedLines() : lines();
    let start = xs.findIndex((x) => /^(岗位职责|工作职责|工作内容)[：:]?$/.test(x));
    if (start < 0) start = xs.findIndex((x) => /^(职位描述|职位详情)[：:]?$/.test(x));
    const tail = start >= 0 ? xs.slice(start + 1) : xs.slice(0, 140);
    const end = tail.findIndex((x) => stops.test(x));
    const section = (end >= 0 ? tail.slice(0, end) : tail.slice(0, 90)).filter((x) => x.length >= 4 && x.length <= 260);
    const compact = (ys, count, size) => ys.slice(0, count).map((x) => clean(x, size));
    const hardRe = /(任职|资格|要求|必须|学历|本科|硕士|经验|年以上|熟悉|精通|掌握|证书|英语|出差|驻场|年龄|专业)/;
    const signal = (re) => compact(section.filter((x) => re.test(x)), 3, 100);
    const selectorText = (selectors, root = document) => {
      for (const s of selectors) {
        const e = [...root.querySelectorAll(s)].find(visible);
        const v = clean(e?.innerText, 80);
        if (v) return v;
      }
      return "";
    };
    const panelDetailLink = z ? detailRoot.querySelector('a.job-company-info__view-all[href*="/jobdetail/"],a[href*="/jobdetail/"]') : null;
    const panelJobId = (panelDetailLink?.href?.match(/jobdetail\/([^/?]+)\.htm/i) || [])[1] || "";
    const panelHeadline = z && detailRoot !== document
      ? xs.find((x) => /(面议|\d+(?:\.\d+)?\s*[-–—~至]\s*\d+(?:\.\d+)?\s*[Kk万千元])/.test(x)) || ""
      : "";
    const panelSalary = first(xs, /(面议|\d+(?:\.\d+)?\s*[-–—~至]\s*\d+(?:\.\d+)?\s*[Kk万千元]|\d+\s*元)/);
    const panelTitle = clean(panelHeadline.replace(panelSalary, ""), 64);
    const stateText = [...detailRoot.querySelectorAll("button,a,[role=button],div,span")]
      .filter((e) => visible(e) && /^(立即投递|已投递)$/.test(clean(e.innerText)))
      .map((e) => clean(e.innerText));
    const apply51 = !z ? [...document.querySelectorAll("div.apply-btn-new.big")].find(visible) : null;
    const detail = {
      ok: !status().security,
      platform,
      url: location.href,
      jobId: jobId(platform) || panelJobId,
      title: selectorText(z ? [".job-detail-summary__job-name", ".job-detail-summary__title", "h1", '[class*="job-name"]'] : ["h1", ".cn h1", ".jobinfo__name", '[class*="job-name"]'], detailRoot) || panelTitle || clean(document.title.split("招聘")[0]),
      company: selectorText(z ? [".job-detail-summary__company-name", ".job-company-info__name", '[class*="company-name"]'] : [".companyinfo__name", '[class*="company-name"]'], detailRoot) || first(xs, /(公司|集团|银行|医院|学校|研究院|科技培训学校)$/),
      salary: z ? (selectorText(["[class*='salary']"], detailRoot) || panelSalary) : selectorText(["strong"]),
      city: z ? (selectorText([".workCity-link", '[class*="job-address"]'], detailRoot) || first(xs, /(成都|重庆|西安|南京|苏州|无锡|广州|深圳|上海|杭州)[·\s][^\n]{0,24}/)) : selectorText([".type_2"]),
      experience: z ? first(xs, /(经验不限|无经验|\d+[-–—]\d+年|\d+年以上)/) : selectorText([".type_3"]),
      education: z ? first(xs, /(学历不限|大专|本科|硕士|博士)/) : selectorText([".type_4"]),
      state: z ? (stateText.includes("已投递") ? "已投递" : stateText.includes("立即投递") ? "立即投递" : "") : clean(apply51?.innerText, 12),
      duties: compact(section.filter((x) => !hardRe.test(x)), 7, 120),
      hard: compact(section.filter((x) => hardRe.test(x)), 7, 120),
      vendorSignals: signal(/客户|驻场|售前|交付客户|客户验收|外包/),
      travelSignals: signal(/出差|驻场/),
      devSignals: signal(/前端|后端|二次开发|编码|Java|C#|\.NET|开发语言/i),
      internalSignals: signal(/内部|业务部门|公司各部门|本公司|供应商|主数据|权限|流程|上线|培训|用户支持/),
    };
    detail.autoReject = window.__codexJobPolicy.deterministicDetailReject(detail);
    return detail;
  };

  const zhaopinCards = (lane = {}) => {
    const s = status();
    if (s.security || s.loggedOut) return { ok: false, platform: "zhaopin", url: location.href, ...s, cards: [] };
    const isNew = document.querySelectorAll(".job-card").length > 0;
    const cards = [...document.querySelectorAll(isNew ? ".job-card" : ".joblist-box__item")].slice(0, 30).map((node) => {
      const xs = (node.innerText || "").split("\n").map((x) => clean(x, 80)).filter(Boolean);
      const link = node.querySelector('a[href*="/jobdetail/"]');
      const href = node.getAttribute("data-codex-job-href") || link?.href || "";
      const id = node.getAttribute("data-codex-job-id") || jobId("zhaopin", href);
      if (id) node.setAttribute("data-codex-job-id", id);
      const titleRow = isNew ? clean(node.querySelector(".job-card__title-row")?.innerText, 100) : "";
      const cardSalary = clean(node.querySelector(".job-card__salary,[class*='salary']")?.innerText || first([titleRow, ...xs], /(面议|\d+(?:\.\d+)?[-–—~至]\d+(?:\.\d+)?\s*[Kk万千元])/), 32);
      const titleSelectors = [".job-card__title-main", ".job-card__title-clamp", ".job-card__title", "[class*='job-title']", ".jobinfo__name"];
      const titleFromDom = titleSelectors.map((selector) => clean(node.querySelector(selector)?.innerText, 64))
        .find((value) => value && value !== cardSalary && !/^(?:面议|\d+(?:\.\d+)?\s*[-–—~至]\s*\d+(?:\.\d+)?\s*[Kk万千元](?:·\d+薪)?|\d+\s*元)$/.test(value)) || "";
      const title = clean(titleFromDom || (titleRow && titleRow.replace(cardSalary, "")) || link?.innerText || xs.find((value) => value !== cardSalary) || "", 64);
      const tags = [...node.querySelectorAll(isNew ? ".job-card__skill-tags *,.job-card__skill-tags" : ".joblist-box__item-tag")].map((e) => clean(e.innerText, 24)).filter(Boolean);
      const risks = [node.getAttribute("data-codex-detail-risk") || "", ...xs.filter((x) => /外包|代招|猎头|驻场|客户现场|现场支持|交付|验收|顾问|售前|AI|人工智能|大模型|算法|部长|首席|总师|CIO|CTO|总监|架构师|专家|二次开发|Java开发/i.test(x))].filter(Boolean).slice(0, 4);
      const companyNode = node.querySelector(".job-card__company-name,.companyinfo__name,[class*='company-name']");
      const company = clean(companyNode?.innerText || xs.find((x) => /(?:有限公司|股份|集团|银行|医院|学校|研究院)$/.test(x)) || "", 64);
      const location = clean(node.querySelector(".job-card__location")?.innerText
        || xs.find((x) => /^(成都|重庆|西安|南京|苏州|无锡|广州|深圳|上海|杭州)(?:\s|$)/.test(x)) || "", 32);
      return [id, title, company, location, cardSalary || first(xs, /(面议|\d+(?:\.\d+)?[-–—]\d+(?:\.\d+)?[Kk万千元])/, 32), first(xs, /^(已投递|立即投递|立即沟通|继续沟通)$/, 12), [...tags.slice(0, 3), ...risks].join("/").slice(0, 240), href];
    }).filter((x) => x[0] && x[1]);
    const gated = window.__codexJobPolicy.gateCards(cards, lane);
    return { ok: true, platform: "zhaopin", schema: "id|title|company|city|salary|state|signals|href", url: location.href, page: Number((location.href.match(/\/p(\d+)/i) || [])[1] || 1), ...gated };
  };


  const clickApply = (platform) => {
    if (platform !== "zhaopin") return { ok: false, error: "platform_mismatch" };
    const current = jobId("zhaopin") || (([...document.querySelectorAll(".job-detail-panel a[href*='/jobdetail/']")].find(visible)?.href || "").match(/jobdetail\/([^/?]+)\.htm/i) || [])[1] || "";
    let nodes = [...document.querySelectorAll(".job-detail-summary__apply")].filter(visible);
    if (!nodes.length) nodes = [...document.querySelectorAll("button,a,[role=button],div,span")].filter((e) => clean(e.innerText) === "立即投递");
    nodes = nodes.filter(visible).map((e) => ({ e, score: /^(BUTTON|A)$/.test(e.tagName) ? 3 : /apply|deliver|btn/i.test(String(e.className)) ? 2 : e.children.length === 0 ? 1 : 0 })).sort((a, b) => b.score - a.score);
    if (!nodes.length) return { ok: false, error: /已投递/.test(document.body?.innerText || "") ? "already_applied" : "apply_control_missing", jobId: current };
    if (nodes.length > 1 && nodes[0].score === nodes[1].score) return { ok: false, error: "apply_control_ambiguous", count: nodes.length, jobId: current };
    nodes[0].e.click();
    return { ok: true, clicked: true, jobId: current, url: location.href };
  };

  window.__codexJobs = {
    version: VERSION,
    ping: () => ({ ok: true, version: VERSION }),
    zhaopinCards,
    zhaopinDetail: () => detailBase("zhaopin", /^(工作地址|公司介绍|公司信息|职位发布者|相关推荐|相似职位)$/),
    clickApply,
  };
})();
