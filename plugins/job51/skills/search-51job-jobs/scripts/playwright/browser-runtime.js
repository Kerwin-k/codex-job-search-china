(() => {
  const EXTENSION_VERSION = "0.4.0";
  const VERSION = "2.6-51job-controller";
  const clean = (s, n = 80) => String(s || "").replace(/\s+/g, " ").trim().slice(0, n);
  const hashIds = (ids) => {
    const normalized = [...new Set((Array.isArray(ids) ? ids : []).map((value) => String(value || "")).filter(Boolean))].sort();
    let hash = 2166136261;
    for (const text of normalized) {
      for (const char of String(text || "")) { hash ^= char.charCodeAt(0); hash = Math.imul(hash, 16777619); }
      hash ^= 124; hash = Math.imul(hash, 16777619);
    }
    return `cards:${normalized.length}:${(hash >>> 0).toString(16).padStart(8, "0")}`;
  };
  const isAllowed51JobUrl = (value = location.href) => {
    try {
      const parsed = new URL(String(value || ""), location.href);
      return parsed.protocol === "https:" && ["we.51job.com", "jobs.51job.com"].includes(parsed.hostname.toLowerCase());
    } catch { return false; }
  };
  const lines = () => [...new Set(String(document.body?.innerText || "").slice(0, 120000).split("\n").map((x) => clean(x, 260)).filter(Boolean))];
  const first = (xs, re, n = 80) => clean(xs.find((x) => re.test(x)) || "", n);
  const visible = (e) => !!(e && e.getClientRects().length && getComputedStyle(e).visibility !== "hidden");
  const jobPosting = () => {
    for (const node of document.querySelectorAll('script[type="application/ld+json"]')) {
      try {
        const raw = JSON.parse(node.textContent || "null");
        const values = Array.isArray(raw) ? raw : [raw, ...(Array.isArray(raw?.["@graph"]) ? raw["@graph"] : [])];
        const match = values.find((value) => {
          const type = value?.["@type"];
          return type === "JobPosting" || (Array.isArray(type) && type.includes("JobPosting"));
        });
        if (match) return match;
      } catch {}
    }
    return null;
  };
  const postingSalary = (posting) => {
    const salary = posting?.baseSalary;
    if (!salary) return "";
    if (typeof salary === "string" || typeof salary === "number") return clean(salary, 40);
    const value = salary.value ?? salary;
    if (typeof value === "string" || typeof value === "number") return clean(value, 40);
    const minimum = Number(value?.minValue);
    const maximum = Number(value?.maxValue);
    if (!Number.isFinite(minimum)) return "";
    const unit = clean(salary.unitText || value?.unitText, 16).toUpperCase();
    if (unit && !/MONTH|月/.test(unit)) return "";
    return Number.isFinite(maximum) ? `${minimum}-${maximum}元/月` : `${minimum}元/月`;
  };
  const jobId = (url = location.href) => {
    const matched = (String(url).match(/\/(\d+)\.html/i) || [])[1];
    if (matched) return matched;
    try { return new URL(String(url || location.href), location.href).searchParams.get("jobid") || ""; } catch { return ""; }
  };
  const status = () => {
    const t = document.body?.innerText || "";
    return { security: /验证码|安全验证|访问异常|滑块验证|Access Verification|Please slide to verify/i.test(t), loggedOut: /登录后可投递|请先登录|请登录后/.test(t) };
  };
  const detailBase = () => {
    if (!isAllowed51JobUrl()) return { ok: false, platform: "51job", url: location.href, error: "platform_mismatch" };
    const xs = lines();
    const posting = jobPosting();
    let start = xs.findIndex((x) => /^(岗位职责|工作职责|工作内容)[：:]?$/.test(x));
    if (start < 0) start = xs.findIndex((x) => /^(职位描述|职位详情)[：:]?$/.test(x));
    const tail = start >= 0 ? xs.slice(start + 1) : xs.slice(0, 140);
    const end = tail.findIndex((x) => /^(职能类别|工作地址|公司信息|公司介绍|职位招聘官|51Job安全提醒)$/.test(x));
    const section = (end >= 0 ? tail.slice(0, end) : tail.slice(0, 90)).filter((x) => x.length >= 4 && x.length <= 260);
    const companyStart = xs.findIndex((x) => /^(公司信息|公司介绍)$/.test(x));
    const companyBlock = companyStart >= 0 ? xs.slice(companyStart, companyStart + 40) : [];
    const evidenceSeen = new Set();
    const evidenceKey = (value) => clean(value, 260).toLocaleLowerCase().replace(/[\s，。；;：:、,.()（）【】\[\]·•_-]+/g, "");
    const compactUnique = (ys, count, size) => {
      const out = [];
      for (const raw of ys) {
        const value = clean(raw, size);
        const key = evidenceKey(value);
        if (!key || evidenceSeen.has(key)) continue;
        evidenceSeen.add(key);
        out.push(value);
        if (out.length >= count) break;
      }
      return out;
    };
    const hardRe = /(任职|资格|要求|必须|学历|本科|硕士|经验|年以上|熟悉|精通|掌握|证书|英语|出差|驻场|年龄|专业)/;
    const duties = compactUnique(section.filter((x) => !hardRe.test(x)), 5, 110);
    const hard = compactUnique(section.filter((x) => hardRe.test(x)), 5, 110);
    const signal = (re) => compactUnique(section.filter((x) => re.test(x)), 1, 90);
    const selectorText = (selectors) => {
      for (const s of selectors) {
        const e = [...document.querySelectorAll(s)].find(visible);
        const v = clean(e?.innerText, 80);
        if (v) return v;
      }
      return "";
    };
    const stateText = [...document.querySelectorAll("div.apply-btn-new.big,button,a,[role=button]")]
      .filter((e) => visible(e) && /^(立即投递|已投递)$/.test(clean(e.innerText)))
      .slice(0, 16)
      .map((e) => clean(e.innerText));
    const applyButton = [...document.querySelectorAll("div.apply-btn-new.big")].find(visible);
    const pageStatus = status();
    const detail = {
      ok: !pageStatus.security,
      error: pageStatus.security ? "slider_required" : "",
      platform: "51job",
      url: location.href,
      jobId: jobId(),
      title: clean(posting?.title, 80) || selectorText([".job-header-left .job-title", ".job-header .job-name", '[data-testid="job-title"]', '[class*="job-title"]', '[class*="jobName"]', "h1"]),
      company: clean(posting?.hiringOrganization?.name, 80) || selectorText([".job-header-left .company-name", ".job-header .company-name", '[data-testid="company-name"]', '[class*="company-name"]', '[class*="companyName"]', ".cname"]),
      companyScale: first(companyBlock, /(?:少于|不足)?\s*\d+\s*-\s*\d+\s*人|\d+\s*人以上|(?:少于|不足)\s*\d+\s*人/, 40),
      companyNature: first(companyBlock, /(?:民营|国企|国有企业|央企|国有控股|已上市|上市公司|外资|合资|事业单位|政府机关|非营利组织?|股份制)/i, 40),
      salary: postingSalary(posting) || selectorText([".job-header-left .salary", ".job-header .salary", '[data-testid="job-salary"]', '[class*="job-salary"]', '[class*="salary"]', ".sal", "strong"]),
      city: selectorText([".type_2"]),
      experience: selectorText([".type_3"]),
      education: selectorText([".type_4"]),
      state: clean(applyButton?.innerText, 12) || (stateText.includes("已投递") ? "已投递" : stateText.includes("立即投递") ? "立即投递" : ""),
      duties,
      hard,
      vendorSignals: signal(/客户|驻场|售前|交付客户|客户验收|外包/),
      travelSignals: signal(/出差|驻场/),
      devSignals: signal(/前端|后端|二次开发|编码|Java|C#|\.NET|开发语言/i),
      internalSignals: signal(/内部|业务部门|公司各部门|本公司|供应商|主数据|权限|流程|上线|培训|用户支持/),
    };
    detail.autoReject = window.__codexJobPolicy.deterministicDetailReject(detail);
    return detail;
  };
  const job51Cards = (lane = {}) => {
    if (!isAllowed51JobUrl()) return { ok: false, platform: "51job", url: location.href, error: "platform_mismatch", cards: [] };
    const s = status();
    if (s.security || s.loggedOut) return { ok: false, platform: "51job", url: location.href, ...s, cards: [] };
    const cards = [...document.querySelectorAll(".joblist-item")].slice(0, 30).map((node) => {
      let d = {};
      try { d = JSON.parse(node.querySelector("[sensorsdata]")?.getAttribute("sensorsdata") || "{}"); } catch {}
      const xs = (node.innerText || "").split("\n").map((x) => clean(x, 80)).filter(Boolean);
      const dataId = String(d.jobId || d.job_id || "");
      let liveJob = null;
      for (let owner = node, depth = 0; owner && depth < 10 && !liveJob; owner = owner.parentElement, depth++) {
        const list = owner.__vue__?.joblist;
        if (Array.isArray(list)) liveJob = list.find((item) => String(item?.jobId || item?.job_id || "") === dataId) || null;
      }
      const liveHref = String(liveJob?.jobHref || "");
      const exactLiveHref = dataId && isAllowed51JobUrl(liveHref) && jobId(liveHref) === dataId ? liveHref : "";
      const links = [...node.querySelectorAll("a[href]")];
      const link = links.find((anchor) => dataId && jobId(anchor.href) === dataId)
        || links.find((anchor) => /\/\d+\.html(?:[?#]|$)/i.test(anchor.href) && !/\/all\/co/i.test(anchor.href))
        || null;
      const href = exactLiveHref || link?.href || "";
      const id = dataId || jobId(href);
      if (id) node.setAttribute("data-codex-job-id", id);
      const title = clean(d.jobName || d.job_name || node.querySelector(".jname")?.innerText || xs[0], 64);
      const allTags = [...node.querySelectorAll(".tag")].map((e) => clean(e.innerText, 16)).filter(Boolean);
      const domainTags = allTags.filter((tag) => /ERP|MES|WMS|OA|SAP|金蝶|用友|企业应用|应用系统|业务系统|信息系统|信息化|数字化|低代码|主数据|数据治理/i.test(tag)).slice(0, 4);
      const riskTags = allTags.filter((tag) => /外包|代招|猎头|驻场|客户|销售|售前|开发|运维|园区|出差/i.test(tag)).slice(0, 4);
      const meta = [...node.querySelectorAll(".dc")].map((e) => clean(e.innerText, 22)).filter(Boolean).slice(0, 3);
      const tags = [...new Set([...meta, ...domainTags, ...riskTags])].slice(0, 8);
      return [id, title, clean(node.querySelector(".cname")?.innerText || d.companyName, 64), clean(d.jobArea || d.area || (xs.find(x => (globalThis.__jobflowConfig?.cities || []).some(c => x.includes(c))) || ""), 32), clean(node.querySelector(".sal")?.innerText || d.salary || first(xs, /(面议|\d+(?:\.\d+)?[-–—]\d+(?:\.\d+)?[万千kKwW]|\d+元)/, 32), 32), clean(node.querySelector(".btn.apply")?.innerText || first(xs, /^(已投递|立即投递|投递)$/, 12), 12), tags.join("/"), href];
    }).filter((x) => x[0] && x[1]);
    const gated = window.__codexJobPolicy.gateCards(cards, lane);
    return { ok: true, platform: "51job", schema: "id|title|company|city|salary|state|signals|href", url: location.href, pageFingerprint: hashIds(cards.map((card) => card[0])), pageCardCount: cards.length, ...gated };
  };
  const clickApply = (platform) => {
    if (platform !== "51job" || !isAllowed51JobUrl()) return { ok: false, error: "platform_mismatch" };
    const current = jobId();
    let nodes = [...document.querySelectorAll("div.apply-btn-new.big")];
    if (!nodes.length) nodes = [...document.querySelectorAll("button,a,[role=button],div,span")].filter((e) => clean(e.innerText) === "立即投递");
    nodes = nodes.filter(visible).map((e) => ({ e, score: /^(BUTTON|A)$/.test(e.tagName) ? 3 : /apply|deliver|btn/i.test(String(e.className)) ? 2 : e.children.length === 0 ? 1 : 0 })).sort((a, b) => b.score - a.score);
    if (!nodes.length) return { ok: false, error: /已投递/.test(document.body?.innerText || "") ? "already_applied" : "apply_control_missing", jobId: current };
    if (nodes.length > 1 && nodes[0].score === nodes[1].score) return { ok: false, error: "apply_control_ambiguous", count: nodes.length, jobId: current };
    nodes[0].e.click();
    return { ok: true, clicked: true, jobId: current, url: location.href };
  };
  window.__codexJobs = {
    version: VERSION,
    extensionVersion: EXTENSION_VERSION,
    ping: () => ({ ok: isAllowed51JobUrl(), version: VERSION, extensionVersion: EXTENSION_VERSION, platform: "51job", url: location.href, error: isAllowed51JobUrl() ? "" : "platform_mismatch" }),
    job51Cards,
    job51Detail: detailBase,
    clickApply,
  };
})();
