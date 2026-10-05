(() => {
  "use strict";

  const BASE = "__JOBFLOW_BRIDGE_URL__";
  const TOKEN = "__JOBFLOW_BRIDGE_TOKEN__";
  const BRIDGE_VERSION = "0.5.9";
  const CLIENT_ID = `boss-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  let busy = false;
  let lastHeartbeat = 0;
  let activeFeedLabel = "";
  let lastClickedJob = null;
  const feedCaches = new Map();

  const headers = {
    "Content-Type": "application/json",
    "X-Boss-Bridge-Token": TOKEN
  };

  function cleanText(value, limit = 12000) {
    return String(value || "")
      .replace(/\u00a0/g, " ")
      .replace(/[ \t]+\n/g, "\n")
      .replace(/\n{3,}/g, "\n\n")
      .trim()
      .slice(0, limit);
  }

  function visible(element) {
    if (!(element instanceof HTMLElement)) return false;
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.visibility !== "hidden" &&
      style.display !== "none" &&
      rect.width > 1 &&
      rect.height > 1;
  }

  function elementText(element, limit = 500) {
    return cleanText(
      element.innerText ||
      element.getAttribute("aria-label") ||
      element.getAttribute("title") ||
      element.value ||
      "",
      limit
    );
  }

  function interactiveElements() {
    return Array.from(document.querySelectorAll(
      "button,a,[role='button'],input,textarea,[contenteditable='true']"
    )).filter(visible);
  }

  function findByText(text, exact = true) {
    const wanted = cleanText(text, 500).toLowerCase();
    const elements = interactiveElements();
    const exactMatch = elements.find(
      (element) => elementText(element).toLowerCase() === wanted
    );
    if (exact || exactMatch) return exactMatch || null;
    return elements.find(
      (element) => elementText(element).toLowerCase().includes(wanted)
    ) || null;
  }

  function findPromptAction(text) {
    const labels = allExactTextCandidates(text);
    for (const label of labels) {
      let action = label;
      for (let depth = 0; action && depth < 5; depth++, action = action.parentElement) {
        const role = action.getAttribute?.("role") || "";
        const className = String(action.className || "");
        if (visible(action) && (
          /^(BUTTON|A)$/.test(action.tagName) || role === "button" || /btn|button/i.test(className)
        )) {
          let root = action.parentElement;
          for (let rootDepth = 0; root && rootDepth < 7; rootDepth++, root = root.parentElement) {
            const promptText = elementText(root, 3000);
            if (promptText.includes("温馨提示") && promptText.includes("取消") &&
                promptText.includes("沟通新职位")) {
              return action;
            }
          }
        }
      }
    }
    return null;
  }

  function allExactTextCandidates(text) {
    const wanted = cleanText(text, 500).toLowerCase();
    return Array.from(document.querySelectorAll("body *"))
      .filter(visible)
      .filter((element) => elementText(element).toLowerCase() === wanted)
      .sort((left, right) => {
        const score = (element) => {
          const hint = `${element.className || ""} ${element.parentElement?.className || ""}`;
          const filterHint = /filter|select|option|dropdown|menu/i.test(hint) ? -10000 : 0;
          const tagHint = /^(LI|BUTTON|A)$/.test(element.tagName) ? -1000 : 0;
          return filterHint + tagHint +
            element.childElementCount * 100 +
            Math.max(0, element.getBoundingClientRect().top);
        };
        return score(left) - score(right);
      });
  }

  function clickLikeUser(element) {
    element.scrollIntoView({block: "nearest", inline: "nearest"});
    for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
      element.dispatchEvent(new MouseEvent(type, {
        bubbles: true,
        cancelable: true,
        composed: true,
        view: window,
        button: 0,
        buttons: type.endsWith("down") ? 1 : 0
      }));
    }
  }

  function visibleFilterOptions(excludeText = "") {
    const elements = Array.from(document.querySelectorAll(
      "li,[role='option'],[class*='option'],[class*='menu-item']," +
      "[class*='dropdown'] a,[class*='dropdown'] span,[class*='select'] li"
    )).filter(visible);
    const seen = new Set();
    const options = [];
    for (const element of elements) {
      const text = elementText(element, 160);
      if (!text || text === excludeText || text.length > 40 || text.includes("\n")) continue;
      if (seen.has(text)) continue;
      seen.add(text);
      options.push(text);
      if (options.length >= 40) break;
    }
    return options;
  }

  function cityFilterTrigger() {
    const neighbor = allExactTextCandidates("求职类型")[0];
    if (!neighbor) return null;
    const anchor = neighbor.getBoundingClientRect();
    const anchorCenter = anchor.top + anchor.height / 2;
    const candidates = Array.from(document.querySelectorAll("body *"))
      .filter(visible)
      .map((element) => ({element, rect: element.getBoundingClientRect()}))
      .filter(({element, rect}) => {
        const text = elementText(element, 80);
        const center = rect.top + rect.height / 2;
        const gap = anchor.left - rect.right;
        return text && !text.includes("\n") && text.length <= 12 &&
          Math.abs(center - anchorCenter) <= 12 &&
          gap >= -2 && gap <= 36 &&
          rect.width >= 42 && rect.width <= 180 &&
          rect.height >= 24 && rect.height <= 58;
      })
      .sort((left, right) => {
        const leftGap = anchor.left - left.rect.right;
        const rightGap = anchor.left - right.rect.right;
        const leftHint = /city|filter|select/i.test(String(left.element.className || "")) ? -100 : 0;
        const rightHint = /city|filter|select/i.test(String(right.element.className || "")) ? -100 : 0;
        return (leftHint + leftGap + left.element.childElementCount * 2) -
          (rightHint + rightGap + right.element.childElementCount * 2);
      });
    return candidates[0]?.element || null;
  }

  function cityDialogRoot() {
    const tab = allExactTextCandidates("ABCDE").filter(visible)[0];
    if (!tab) return null;
    const candidates = [];
    for (let element = tab.parentElement; element && element !== document.body; element = element.parentElement) {
      const text = elementText(element, 4000);
      const rect = element.getBoundingClientRect();
      if (text.includes("FGHJ") && text.includes("KLMN") && text.includes("PQRST") &&
          text.includes("WXYZ") && rect.width >= 500 && rect.height >= 180) {
        candidates.push(element);
      }
    }
    return candidates[0] || null;
  }

  function cityGroupFor(city) {
    if (["北京", "成都", "重庆"].includes(city)) return "ABCDE";
    if (["广州", "杭州"].includes(city)) return "FGHJ";
    if (["南京"].includes(city)) return "KLMN";
    if (["上海", "深圳", "苏州", "天津"].includes(city)) return "PQRST";
    if (["无锡", "武汉", "西安"].includes(city)) return "WXYZ";
    return "";
  }

  function exactWithin(root, text) {
    return allExactTextCandidates(text)
      .filter((element) => root?.contains(element))
      .sort((left, right) => left.childElementCount - right.childElementCount)[0] || null;
  }

  function dismissCityDialog(root) {
    const close = Array.from(root?.querySelectorAll("button,[role='button'],[class*='close']") || [])
      .filter(visible)
      .filter((element) => /close/i.test(`${element.getAttribute("aria-label") || ""} ${element.className || ""}`))[0];
    if (close) clickLikeUser(close);
  }

  async function waitForCityDialog(timeoutMs = 3000) {
    const started = Date.now();
    while (Date.now() - started < timeoutMs) {
      const dialog = cityDialogRoot();
      if (dialog) return dialog;
      await new Promise((resolve) => setTimeout(resolve, 150));
    }
    return null;
  }

  function visibleCityGroupLabels() {
    return ["热门城市", "ABCDE", "FGHJ", "KLMN", "PQRST", "WXYZ"]
      .filter((label) => allExactTextCandidates(label).some(visible));
  }

  async function setCityFilter(city) {
    const trigger = cityFilterTrigger();
    if (!trigger) throw new Error("city_filter_control_not_found");
    const beforeLabel = elementText(trigger, 40);
    const triggerRect = trigger.getBoundingClientRect();
    clickLikeUser(trigger);
    const dialog = await waitForCityDialog(3000);
    if (!dialog) {
      const labels = visibleCityGroupLabels();
      throw new Error(`city_filter_dialog_not_found: trigger=${beforeLabel}; groups=${labels.join(",") || "none"}`);
    }
    let group = "热门城市";
    let option = exactWithin(dialog, city);
    if (!option) {
      group = cityGroupFor(city);
      const groupTab = exactWithin(dialog, group);
      if (!group || !groupTab) {
        dismissCityDialog(dialog);
        throw new Error(`city_filter_group_not_found: ${city}`);
      }
      clickLikeUser(groupTab);
      await new Promise((resolve) => setTimeout(resolve, 350));
      option = exactWithin(dialog, city);
    }
    if (!option || option === trigger || option.getBoundingClientRect().top < triggerRect.bottom - 4) {
      dismissCityDialog(dialog);
      throw new Error(`city_filter_option_not_found: ${city}`);
    }
    clickLikeUser(option);
    await new Promise((resolve) => setTimeout(resolve, 900));
    return {
      before_label: beforeLabel,
      selected: city,
      city_group: group,
      title: document.title,
      url: location.href
    };
  }

  function findListItemByText(text) {
    const wanted = cleanText(text, 500).toLowerCase();
    const candidates = Array.from(document.querySelectorAll(
      "li,[role='listitem'],.friend-item,.chat-list-item"
    ))
      .filter(visible)
      .map((element) => ({element, text: elementText(element, 2000)}))
      .filter((item) => item.text.toLowerCase().includes(wanted))
      .sort((left, right) => left.text.length - right.text.length);
    return candidates[0]?.element || null;
  }

  function clickListItem(element, text) {
    const wanted = cleanText(text, 500).toLowerCase();
    const descendants = Array.from(element.querySelectorAll("*"))
      .filter(visible)
      .map((candidate) => ({
        element: candidate,
        text: elementText(candidate, 2000)
      }))
      .filter((item) => item.text.toLowerCase().includes(wanted))
      .sort((left, right) => left.text.length - right.text.length);
    const target = descendants[0]?.element || element;
    target.scrollIntoView({block: "nearest", inline: "nearest"});
    for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
      target.dispatchEvent(new MouseEvent(type, {
        bubbles: true,
        cancelable: true,
        composed: true,
        view: window,
        button: 0,
        buttons: type.endsWith("down") ? 1 : 0
      }));
    }
    return target;
  }

  function findMessageInput() {
    const candidates = Array.from(document.querySelectorAll(
      "textarea,[contenteditable='true'],input[type='text']"
    )).filter(visible).filter((element) => {
      const hint = cleanText(
        `${element.getAttribute("placeholder") || ""} ` +
        `${element.getAttribute("aria-label") || ""}`
      );
      return !/搜索|联系人/.test(hint) &&
        !element.closest(".boss-search-container,.boss-search-top,[class*='contact-search']");
    });
    const hinted = candidates.find((element) => {
      const hint = cleanText(
        `${element.getAttribute("placeholder") || ""} ${element.getAttribute("aria-label") || ""}`
      );
      return /消息|沟通|输入|回复|招呼|简短描述|问题/.test(hint);
    });
    if (hinted) return hinted;
    const editable = candidates.find((element) => element.isContentEditable);
    if (editable) return editable;
    const ownsSendButton = (input) => {
      let element = input.parentElement;
      for (let depth = 0; depth < 10 && element; depth += 1) {
        const hasSend = Array.from(element.querySelectorAll("button,[role='button']"))
          .some((candidate) => visible(candidate) && elementText(candidate) === "发送");
        if (hasSend) return true;
        element = element.parentElement;
      }
      return false;
    };
    const sendOwned = candidates.find(ownsSendButton);
    if (sendOwned) return sendOwned;
    return null;
  }

  function inputText(element) {
    if (!element) return "";
    return cleanText(
      element.isContentEditable
        ? (element.innerText || element.textContent)
        : element.value,
      3000
    );
  }

  function setInputValue(element, text) {
    element.focus();
    if (element.isContentEditable) {
      element.textContent = "";
      document.execCommand("insertText", false, text);
    } else {
      const prototype = element instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype
        : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
      if (setter) setter.call(element, text);
      else element.value = text;
    }
    element.dispatchEvent(new InputEvent("input", {
      bubbles: true,
      inputType: "insertText",
      data: text
    }));
    element.dispatchEvent(new Event("change", {bubbles: true}));
  }

  function countOccurrences(text, needle) {
    if (!needle) return 0;
    let count = 0;
    let start = 0;
    while (true) {
      const index = text.indexOf(needle, start);
      if (index < 0) return count;
      count += 1;
      start = index + needle.length;
    }
  }

  function pageSignals(body) {
    return {
      security_check: /安全验证|验证码|访问异常|账号异常/.test(body),
      logged_in: /消息|简历/.test(body) && !/登录\/注册/.test(body),
      has_chat_input: Boolean(findMessageInput()),
      // The detail-page modal renders its visible send control as a styled
      // non-button element until a draft is present; UIA exposes it as the
      // same semantic Send control after filling.
      has_send_button: Boolean(
        findByText("发送", true) || allExactTextCandidates("发送").length
      )
    };
  }

  function findConversationRoot(input) {
    if (!input) return null;
    let element = input.parentElement;
    let best = null;
    for (let depth = 0; depth < 12 && element; depth += 1) {
      const text = cleanText(element.innerText, 30000);
      const hint = `${element.getAttribute("role") || ""} ` +
        `${element.getAttribute("aria-modal") || ""} ` +
        `${element.className || ""}`;
      // The detail-page composer is a modal.  Its textarea and send control can
      // be rendered in separate descendants, so recognize the dialog before
      // requiring a send button in the same ancestor.
      if (/dialog|modal/i.test(hint) && /简短描述|发送/.test(text)) {
        return element;
      }
      // On /web/geek/chat the send control can be mounted outside the editor
      // subtree.  Stop at the first ancestor that owns the selected right-hand
      // conversation, before reaching the left conversation list or page body.
      if (location.pathname.includes("/web/geek/chat") &&
          text.includes("查看职位") && text.includes("按Enter键发送")) {
        return element;
      }
      const hasSend = Array.from(element.querySelectorAll("button,[role='button']"))
        .some((candidate) => visible(candidate) && elementText(candidate) === "发送");
      if (hasSend && text.length <= 30000) {
        best = element;
      }
      element = element.parentElement;
    }
    return best;
  }

  function pageSummary(verbose = false) {
    const body = cleanText(document.body?.innerText, verbose ? 6000 : 1400);
    return {
      title: document.title,
      url: location.href,
      text: body,
      signals: pageSignals(body)
    };
  }

  function expectationTabs() {
    const tabs = [];
    const seen = new Set();
    for (const element of document.querySelectorAll("a,button,[role='tab'],li,span,div")) {
      if (!visible(element)) continue;
      const rect = element.getBoundingClientRect();
      if (rect.top < 55 || rect.top > 230 || rect.width > 280) continue;
      const label = elementText(element, 80);
      if (
        !(label === "推荐" || /^[^()\n]{2,24}\([^()\n]{2,10}\)$/.test(label)) ||
        seen.has(label)
      ) continue;
      seen.add(label);
      tabs.push({
        label,
        selected: element.getAttribute("aria-selected") === "true" ||
          /active|selected|current/i.test(String(element.className || ""))
      });
      if (tabs.length >= 8) break;
    }
    return {title: document.title, url: location.href, active_feed: activeFeedLabel, tabs};
  }

  function findJobSearchInput() {
    const candidates = Array.from(document.querySelectorAll(
      "input[type='text'],input[type='search'],input:not([type])"
    )).filter(visible).filter((element) => element !== findMessageInput());
    const score = (element) => {
      const hint = cleanText(
        `${element.getAttribute("placeholder") || ""} ` +
        `${element.getAttribute("aria-label") || ""} ` +
        `${element.getAttribute("title") || ""}`,
        300
      );
      let value = 0;
      if (/职位|公司|岗位|搜索/.test(hint)) value += 100;
      if (element.closest("header,[class*='search'],[class*='filter']")) value += 30;
      value -= Math.max(0, element.getBoundingClientRect().top) / 100;
      return value;
    };
    return candidates.sort((left, right) => score(right) - score(left))[0] || null;
  }

  function searchState() {
    const input = findJobSearchInput();
    return {
      title: document.title,
      url: location.href,
      keyword: inputText(input),
      placeholder: cleanText(input?.getAttribute("placeholder"), 200),
      search_input_found: Boolean(input),
      signals: pageSignals(cleanText(document.body?.innerText, 3000))
    };
  }

  function jobIdFromLink(link) {
    return link?.href?.match(/\/job_detail\/([^/?#]+?)(?:\.html)?(?:[?#]|$)/)?.[1] || "";
  }

  function currentFeedKey() {
    const input = findJobSearchInput();
    const query = inputText(input);
    return `${location.pathname}|${location.search}|${activeFeedLabel || query || "recommend"}`;
  }

  function currentFeedCache() {
    const key = currentFeedKey();
    if (!feedCaches.has(key)) feedCaches.set(key, new Map());
    return {key, jobs: feedCaches.get(key)};
  }

  function cardFieldText(card, selectors, limit = 120) {
    if (!card) return "";
    for (const element of card.querySelectorAll(selectors)) {
      const value = elementText(element, limit);
      if (value && value.length <= limit) return value;
    }
    return "";
  }

  function cardAttributeText(card, names, limit = 120) {
    if (!card) return "";
    const elements = [card, ...card.querySelectorAll("*")];
    for (const element of elements) {
      for (const name of names) {
        const value = cleanText(element.getAttribute?.(name), limit);
        if (value && value.length <= limit) return value;
      }
    }
    return "";
  }

  function cardStructuredFields(card, cardText) {
    const salary = cardAttributeText(card, ["data-salary", "data-job-salary"], 80) ||
      cardFieldText(card, "[class*='salary'],[class*='job-limit'],[class*='red']", 80);
    const company = cardAttributeText(card, ["data-company", "data-company-name"], 120) ||
      cardFieldText(card, "[class*='company-name'],[class*='companyName']", 120);
    const city = cardAttributeText(card, ["data-city", "data-location"], 80) ||
      cardFieldText(card, "[class*='city'],[class*='location']", 80);
    const tags = Array.from(card?.querySelectorAll("[class*='tag'],[class*='label'],[class*='geek-tag']") || [])
      .map((element) => elementText(element, 40))
      .filter(Boolean)
      .filter((value, index, values) => values.indexOf(value) === index)
      .slice(0, 8);
    return {
      salary,
      company,
      city,
      tags,
      recruiter_marker: /猎头/.test(cardText)
    };
  }

  function jobFromLink(link) {
    const jobId = jobIdFromLink(link);
    if (!jobId || jobId.length < 8) return null;
    let card = link.closest("li,.job-card-wrapper,.job-list-item,.job-card-box");
    if (!card) {
      card = link.parentElement;
      for (let i = 0; i < 4 && card?.parentElement; i += 1) {
        const text = cleanText(card.innerText, 1200);
        if (text.length >= 40) break;
        card = card.parentElement;
      }
    }
    const title = elementText(link, 200);
    if (
      !title ||
      title.length > 100 ||
      /^(查看更多信息|立即沟通|继续沟通|查看职位|举报|微信扫码分享)$/.test(title)
    ) return null;
    const cardText = cleanText(card?.innerText, 320);
    const summary = cardText.startsWith(title)
      ? cleanText(cardText.slice(title.length), 240)
      : cardText;
    return {job_id: jobId, title, summary, href: link.href, ...cardStructuredFields(card, cardText)};
  }

  function scanJobCards() {
    const feed = currentFeedCache();
    for (const link of document.querySelectorAll("a[href*='/job_detail/']")) {
      const job = jobFromLink(link);
      if (job) feed.jobs.set(job.job_id, job);
    }
    return feed;
  }

  function listJobs(offset = 0, limit = 20) {
    const feed = scanJobCards();
    const jobs = Array.from(feed.jobs.values());
    const safeOffset = Math.max(0, Number(offset) || 0);
    const safeLimit = Math.min(Math.max(Number(limit) || 20, 1), 20);
    const page = jobs.slice(safeOffset, safeOffset + safeLimit);
    return {
      title: document.title,
      url: location.href,
      feed: feed.key,
      count: page.length,
      offset: safeOffset,
      limit: safeLimit,
      total_loaded: jobs.length,
      has_more: safeOffset + page.length < jobs.length,
      jobs: page
    };
  }

  function jobIdsInDom() {
    const ids = new Set();
    for (const link of document.querySelectorAll("a[href*='/job_detail/']")) {
      const jobId = jobIdFromLink(link);
      if (jobId && jobId.length >= 8) ids.add(jobId);
    }
    return ids;
  }

  function findJobLinkById(jobId) {
    const wanted = cleanText(jobId, 160);
    return Array.from(document.querySelectorAll("a[href*='/job_detail/']"))
      .find((link) => jobIdFromLink(link) === wanted) || null;
  }

  function findJobScrollContainers() {
    const candidates = new Map();
    const links = Array.from(document.querySelectorAll("a[href*='/job_detail/']"));
    for (const link of links) {
      let element = link.parentElement;
      for (let depth = 0; depth < 12 && element; depth += 1) {
        const style = getComputedStyle(element);
        const range = element.scrollHeight - element.clientHeight;
        if (/auto|scroll/.test(style.overflowY) && range > 80) {
          const previous = candidates.get(element) || {element, links: 0, range};
          previous.links += 1;
          previous.range = Math.max(previous.range, range);
          candidates.set(element, previous);
        }
        element = element.parentElement;
      }
    }
    const scrolling = document.scrollingElement || document.documentElement;
    candidates.set(scrolling, {
      element: scrolling,
      links: links.length,
      range: Math.max(0, scrolling.scrollHeight - scrolling.clientHeight)
    });
    return Array.from(candidates.values())
      .sort((left, right) =>
        (right.links * 100000 + right.range) - (left.links * 100000 + left.range)
      )
      .slice(0, 3)
      .map((item) => item.element);
  }

  async function waitForJobGrowth(previousCount, timeoutMs = 3200) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 250));
      const feed = scanJobCards();
      if (feed.jobs.size > previousCount) return true;
    }
    return false;
  }

  async function loadMoreJobs(maxRounds = 4) {
    const feed = scanJobCards();
    const beforeCount = feed.jobs.size;
    let noGrowth = 0;
    let rounds = 0;
    for (; rounds < Math.min(Math.max(Number(maxRounds) || 4, 1), 6); rounds += 1) {
      const previousCount = feed.jobs.size;
      const links = Array.from(document.querySelectorAll("a[href*='/job_detail/']"));
      links.at(-1)?.scrollIntoView({block: "end", inline: "nearest"});
      for (const container of findJobScrollContainers()) {
        const step = Math.max(Math.floor(container.clientHeight * 1.8), 900);
        container.scrollTop = Math.min(container.scrollTop + step, container.scrollHeight);
        container.dispatchEvent(new Event("scroll", {bubbles: true}));
      }
      window.dispatchEvent(new Event("scroll"));
      const grew = await waitForJobGrowth(previousCount);
      noGrowth = grew ? 0 : noGrowth + 1;
      if (feed.jobs.size >= 120 || noGrowth >= 2) {
        rounds += 1;
        break;
      }
    }
    return {
      feed: feed.key,
      before_count: beforeCount,
      after_count: feed.jobs.size,
      current_dom_count: jobIdsInDom().size,
      new_count: Math.max(0, feed.jobs.size - beforeCount),
      rounds,
      no_growth_rounds: noGrowth,
      reached_cap: feed.jobs.size >= 120,
      may_have_more: noGrowth < 2 && feed.jobs.size < 120
    };
  }

  function jobDetail(full = false, expectedJobId = "") {
    const candidates = Array.from(document.querySelectorAll(
      "main,article,section,div"
    ))
      .filter(visible)
      .map((element) => ({
        element,
        text: cleanText(element.innerText, 18000)
      }))
      .filter((item) =>
        item.text.includes("职位描述") &&
        /立即沟通|继续沟通/.test(item.text) &&
        item.text.length >= 250 &&
        item.text.length <= 18000
      )
      .sort((left, right) => left.text.length - right.text.length);
    let detail = candidates[0]?.text || "";
    if (!detail) {
      const body = cleanText(document.body?.innerText, 30000);
      const marker = body.lastIndexOf("职位描述");
      const start = marker >= 0 ? Math.max(0, marker - 420) : 0;
      let end = body.indexOf("求职工具", marker >= 0 ? marker : 0);
      if (end < 0) end = Math.min(body.length, start + 12000);
      detail = body.slice(start, Math.min(end, start + 12000));
    }
    const marker = detail.indexOf("职位描述");
    const companyIntroMarker = detail.indexOf("\n公司介绍", Math.max(0, marker));
    const addressMarker = detail.indexOf("\n工作地址", Math.max(0, marker));
    const jdStops = [
      detail.indexOf("\n竞争力分析", Math.max(0, marker)),
      detail.indexOf("\nBOSS 安全提示", Math.max(0, marker)),
      companyIntroMarker,
      detail.indexOf("\n工商信息", Math.max(0, marker)),
      addressMarker,
      detail.indexOf("\n更多职位", Math.max(0, marker))
    ].filter((index) => index > marker);
    const jdEnd = jdStops.length ? Math.min(...jdStops) : detail.length;
    const pageJobId = location.href.match(/\/job_detail\/([^/?#]+?)(?:\.html)?(?:[?#]|$)/)?.[1] || "";
    const jobId = pageJobId || lastClickedJob?.job_id || "";
    const headerText = marker >= 0 ? detail.slice(0, marker) : detail.slice(0, 1400);
    const rawJdText = marker >= 0 ? detail.slice(marker, jdEnd) : detail;
    const jdLimit = full ? 10000 : 4200;
    const jdText = rawJdText.slice(0, jdLimit);
    const companyIntro = companyIntroMarker >= 0
      ? detail.slice(
        companyIntroMarker,
        Math.min(
          detail.length,
          companyIntroMarker + 700,
          addressMarker > companyIntroMarker ? addressMarker : detail.length
        )
      )
      : "";
    const workAddress = addressMarker >= 0
      ? detail.slice(addressMarker, Math.min(detail.length, addressMarker + 250))
      : "";
    if (expectedJobId && jobId !== expectedJobId) {
      throw new Error(`job_identity_mismatch: expected=${expectedJobId} actual=${jobId}`);
    }
    if (expectedJobId && lastClickedJob?.title) {
      const normalizeTitle = (value) => cleanText(value, 200)
        .toLowerCase()
        .replace(/[^\p{L}\p{N}]/gu, "");
      const clickedTitle = normalizeTitle(lastClickedJob.title);
      const headerTitle = normalizeTitle(cleanText(headerText, 900).split("\n")[0]);
      const compatible = clickedTitle.length >= 4 && headerTitle.length >= 4 &&
        (clickedTitle.includes(headerTitle) || headerTitle.includes(clickedTitle));
      if (!compatible) throw new Error(`job_title_mismatch: ${lastClickedJob.title}`);
    }
    return {
      title: document.title,
      url: location.href,
      job_id: jobId,
      expected_job_id: expectedJobId,
      header_text: cleanText(headerText, full ? 1600 : 900),
      jd_text: cleanText(jdText, jdLimit),
      jd_source_length: rawJdText.length,
      jd_truncated: rawJdText.length > jdLimit,
      company_intro: cleanText(companyIntro, 500),
      work_address: cleanText(workAddress, 180),
      signals: pageSignals(detail)
    };
  }

  function companyCheck(expectedJobId = "") {
    const detail = jobDetail(false, expectedJobId);
    return {
      title: detail.title,
      url: detail.url,
      job_id: detail.job_id,
      header_text: detail.header_text,
      company_intro: detail.company_intro,
      work_address: detail.work_address,
      signals: detail.signals
    };
  }

  async function waitForJobSelection(jobId, timeoutMs = 10000) {
    const deadline = Date.now() + timeoutMs;
    let lastError = "detail_not_ready";
    while (Date.now() < deadline) {
      try {
        const detail = jobDetail(false, jobId);
        if (detail.header_text && detail.jd_text.length >= 120) {
          return {
            selection_ready: true,
            job_id: detail.job_id,
            header_title: cleanText(detail.header_text, 240).split("\n")[0],
            jd_source_length: detail.jd_source_length
          };
        }
        lastError = "jd_body_not_ready";
      } catch (error) {
        lastError = String(error?.message || error);
      }
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    throw new Error(`job_selection_timeout: ${jobId}: ${lastError}`);
  }

  function chatState(message = "", verbose = false) {
    const input = findMessageInput();
    const root = findConversationRoot(input);
    const conversation = cleanText(root?.innerText, 30000);
    const onDetailPage = location.pathname.includes("/job_detail/");
    const pageContext = onDetailPage ? jobDetail(false).header_text : "";
    const occurrenceCount = countOccurrences(conversation, message);
    const index = message ? conversation.lastIndexOf(message) : -1;
    const messageContext = index >= 0
      ? conversation.slice(
        Math.max(0, index - 120),
        Math.min(conversation.length, index + message.length + 180)
      )
      : "";
    const identityContext = conversation.length <= 1400
      ? conversation
      : `${conversation.slice(0, 950)}\n…\n${conversation.slice(-350)}`;
    const result = {
      title: document.title,
      url: location.href,
      draft_text: inputText(input),
      exact_message_count: occurrenceCount,
      page_context: pageContext,
      identity_context: identityContext,
      message_context: messageContext,
      conversation_length: conversation.length,
      signals: {
        ...pageSignals(cleanText(document.body?.innerText, 6000)),
        conversation_selected: Boolean(root && input),
        conversation_modal: Boolean(root && input && onDetailPage)
      }
    };
    if (verbose) result.tail = conversation.slice(-1200);
    return result;
  }

  function exactSendControls(root) {
    if (!root) return [];
    const controls = [];
    const seen = new Set();
    const disabled = (element) =>
      element.getAttribute("aria-disabled") === "true" ||
      element.disabled === true ||
      /(^|\b)(disabled|disable)(\b|$)/i.test(String(element.className || ""));
    const exactLabel = (element) => elementText(element, 80) === "发送";
    const hasExactLabel = (element) =>
      exactLabel(element) ||
      Array.from(element.querySelectorAll("*"))
        .some((candidate) => visible(candidate) && exactLabel(candidate));
    const clickableHint = (element) => {
      const role = element.getAttribute("role") || "";
      const className = String(element.className || "");
      return /^(BUTTON|A)$/.test(element.tagName) ||
        role === "button" ||
        /(^|[-_ ])(send|btn|button|action)([-_ ]|$)/i.test(className) ||
        /send-message/i.test(className);
    };
    const add = (element, source) => {
      if (!element || seen.has(element) || !visible(element) || disabled(element)) return;
      if (!hasExactLabel(element)) return;
      seen.add(element);
      controls.push({element, source});
    };

    // Native buttons and role=button controls are the preferred path.
    root.querySelectorAll("button,[role='button']").forEach((element) => {
      if (exactLabel(element)) add(element, "exact_button");
    });

    // BOSS has used a visible text node inside a styled DIV/SPAN for the
    // detail/chat send action. Walk upward from each exact label and select
    // the nearest enabled semantic send parent. This keeps the result unique
    // while covering both the overlay and redirected chat layouts.
    const labels = Array.from(root.querySelectorAll("*") )
      .filter((element) => visible(element) && exactLabel(element));
    for (const label of labels) {
      let candidate = label;
      for (let depth = 0; candidate && depth < 7; depth += 1, candidate = candidate.parentElement) {
        if (candidate === root.parentElement) break;
        if (clickableHint(candidate)) {
          add(candidate, "send_text_parent");
          break;
        }
      }
    }
    return controls;
  }

  function backgroundSend(message, expectedContext = {}) {
    const input = findMessageInput();
    const root = findConversationRoot(input);
    if (!input || !root) throw new Error("background_send_conversation_not_ready");
    const draft = inputText(input);
    if (draft !== message) throw new Error("background_send_draft_mismatch");
    const conversation = cleanText(root.innerText, 30000);
    const existing = countOccurrences(conversation, message) - (draft === message ? 1 : 0);
    if (existing > 0) throw new Error("background_send_exact_message_already_present");
    const body = cleanText(document.body?.innerText, 8000);
    if (/安全验证|验证码|访问异常|账号异常/.test(body)) throw new Error("background_send_security_check");
    // A detail-page chat overlay's own root often omits the job header
    // (including city). The surrounding visible page context is already
    // identity-checked by chat_state; include it here so the guarded
    // background pilot does not reject a valid overlay on a missing city.
    const context = `${cleanText(document.title, 300)}\n${cleanText(document.body?.innerText, 20000)}\n${cleanText(root.innerText, 30000)}\n${cleanText(location.href, 1000)}`;
    for (const [label, value] of Object.entries(expectedContext || {})) {
      if (value && !context.includes(String(value))) {
        throw new Error(`background_send_context_mismatch: ${label}`);
      }
    }
    const controls = exactSendControls(root);
    if (controls.length !== 1) {
      throw new Error(controls.length ? "background_send_control_ambiguous" : "background_send_control_missing");
    }
    clickLikeUser(controls[0].element);
    return {
      triggered: true,
      source: "background_dom_exact_send",
      control_source: controls[0].source,
      draft_text: inputText(input),
      url: location.href
    };
  }

  function chatDebug() {
    const input = findMessageInput();
    const ancestors = [];
    let element = input;
    for (let depth = 0; depth < 8 && element; depth += 1) {
      const rect = element.getBoundingClientRect();
      const text = cleanText(element.innerText, 30000);
      ancestors.push({
        depth,
        tag: element.tagName,
        id: element.id || "",
        class_name: cleanText(String(element.className || ""), 300),
        role: element.getAttribute("role") || "",
        aria_modal: element.getAttribute("aria-modal") || "",
        rect: {
          left: Math.round(rect.left),
          top: Math.round(rect.top),
          width: Math.round(rect.width),
          height: Math.round(rect.height)
        },
        text_length: text.length,
        has_view_job: text.includes("查看职位"),
        has_enter_hint: text.includes("按Enter键发送"),
        has_recruiter: /女士|先生|招聘者|HR/.test(text),
        sample: text.slice(-120)
      });
      element = element.parentElement;
    }
    return {url: location.href, ancestors};
  }

  async function post(path, data) {
    const response = await fetch(`${BASE}${path}`, {
      method: "POST",
      headers,
      body: JSON.stringify(data)
    });
    return response.json();
  }

  async function sendEvent(type, commandId, data, ok = true, error = null) {
    return post("/api/event", {
      type,
      command_id: commandId || null,
      ok,
      error,
      data,
      client: {
        id: CLIENT_ID,
        bridge_version: BRIDGE_VERSION,
        title: document.title,
        url: location.href,
        visible: document.visibilityState === "visible",
        focused: document.hasFocus()
      },
      sent_at: Date.now() / 1000
    });
  }

  async function execute(command) {
    const {id, action, payload = {}} = command;
    try {
      if (action === "page_summary") {
        await sendEvent("result", id, pageSummary(Boolean(payload.verbose)));
        return;
      }
      if (action === "expectation_tabs") {
        await sendEvent("result", id, expectationTabs());
        return;
      }
      if (action === "list_jobs") {
        await sendEvent(
          "result",
          id,
          listJobs(payload.offset, payload.limit)
        );
        return;
      }
      if (action === "load_more_jobs") {
        await sendEvent("result", id, await loadMoreJobs(payload.rounds));
        return;
      }
      if (action === "job_detail") {
        await sendEvent(
          "result",
          id,
          jobDetail(Boolean(payload.full), cleanText(payload.expected_job_id || "", 160))
        );
        return;
      }
      if (action === "company_check") {
        await sendEvent(
          "result",
          id,
          companyCheck(cleanText(payload.expected_job_id || "", 160))
        );
        return;
      }
      if (action === "search_state") {
        await sendEvent("result", id, searchState());
        return;
      }
      if (action === "search_jobs") {
        activeFeedLabel = "";
        const keyword = cleanText(payload.keyword || "", 120);
        if (!keyword) throw new Error("empty_search_keyword");
        const cityCode = cleanText(payload.city_code || "", 20);
        if (cityCode) {
          const target = new URL("/web/geek/jobs", location.origin);
          target.searchParams.set("city", cityCode);
          target.searchParams.set("query", keyword);
          await sendEvent("result", id, {
            keyword,
            city_code: cityCode,
            target_url: target.href,
            navigation_expected: true
          });
          location.assign(target.href);
          return;
        }
        const input = findJobSearchInput();
        if (!input) throw new Error("job_search_input_not_found");
        setInputValue(input, keyword);
        if (inputText(input) !== keyword) throw new Error("search_keyword_mismatch");
        const submit = findByText("搜索", true) ||
          input.closest("form,[class*='search']")?.querySelector("button,[role='button']");
        if (!submit || !visible(submit)) throw new Error("job_search_submit_not_found");
        clickLikeUser(submit);
        await new Promise((resolve) => setTimeout(resolve, 1000));
        await sendEvent("result", id, {
          keyword,
          title: document.title,
          url: location.href
        });
        return;
      }
      if (action === "filter_options") {
        const trigger = allExactTextCandidates(payload.filter || "")[0];
        if (!trigger) throw new Error(`filter_not_found: ${payload.filter}`);
        clickLikeUser(trigger);
        await new Promise((resolve) => setTimeout(resolve, 450));
        await sendEvent("result", id, {
          filter: payload.filter,
          options: visibleFilterOptions(payload.filter),
          title: document.title,
          url: location.href
        });
        return;
      }
      if (action === "set_filter") {
        const option = allExactTextCandidates(payload.value || "")[0];
        if (!option) throw new Error(`filter_option_not_found: ${payload.value}`);
        clickLikeUser(option);
        await new Promise((resolve) => setTimeout(resolve, 900));
        await sendEvent("result", id, {
          selected: payload.value,
          title: document.title,
          url: location.href
        });
        return;
      }
      if (action === "set_city_filter") {
        await sendEvent("result", id, await setCityFilter(cleanText(payload.city || "", 20)));
        return;
      }
      if (action === "reset_filters") {
        const reset = allExactTextCandidates("清空")[0];
        if (!reset) throw new Error("filter_reset_not_found");
        clickLikeUser(reset);
        await new Promise((resolve) => setTimeout(resolve, 900));
        await sendEvent("result", id, {
          reset: true,
          title: document.title,
          url: location.href
        });
        return;
      }
      if (action === "chat_state") {
        await sendEvent(
          "result",
          id,
          chatState(payload.message || "", Boolean(payload.verbose))
        );
        return;
      }
      if (action === "chat_debug") {
        await sendEvent("result", id, chatDebug());
        return;
      }
      if (action === "click_text") {
        const element = findByText(payload.text, payload.exact !== false);
        if (!element) throw new Error(`text_not_found: ${payload.text}`);
        const matched = elementText(element);
        const expectedJobId = cleanText(payload.expected_job_id || "", 160);
        if (
          matched === "查看更多信息" &&
          (!expectedJobId || lastClickedJob?.job_id !== expectedJobId)
        ) {
          throw new Error(
            `detail_click_identity_mismatch: expected=${expectedJobId} selected=${lastClickedJob?.job_id || ""}`
          );
        }
        if (
          cleanText(payload.text, 80) === "推荐" ||
          /^[^()\n]{2,24}\([^()\n]{2,10}\)$/.test(cleanText(payload.text, 80))
        ) {
          activeFeedLabel = cleanText(payload.text, 80);
        }
        const navigationExpected =
          /^(消息|职位)(\s|\d|$)/.test(matched) ||
          /^(立即沟通|继续沟通|查看职位|查看更多信息)$/.test(matched);
        element.scrollIntoView({block: "center", inline: "center"});
        if (navigationExpected) {
          await sendEvent("result", id, {
            matched_text: matched,
            title: document.title,
            before_url: location.href,
            expected_job_id: expectedJobId,
            navigation_expected: true
          });
          clickLikeUser(element);
          return;
        }
        clickLikeUser(element);
        await new Promise((resolve) => setTimeout(resolve, 900));
        await sendEvent("result", id, {
          matched_text: matched,
          title: document.title,
          url: location.href,
          navigation_expected: false
        });
        return;
      }
      if (action === "click_prompt_action") {
        const target = findPromptAction(cleanText(payload.text || "", 80));
        if (!target) throw new Error(`prompt_action_not_found: ${payload.text || ""}`);
        await sendEvent("result", id, {
          matched_text: elementText(target, 200),
          title: document.title,
          before_url: location.href,
          navigation_expected: false
        });
        clickLikeUser(target);
        return;
      }
      if (action === "click_job_id") {
        const jobId = cleanText(payload.job_id || "", 160);
        const feed = scanJobCards();
        const link = findJobLinkById(jobId);
        const job = link ? jobFromLink(link) : feed.jobs.get(jobId);
        if (!job || job.job_id !== jobId) throw new Error(`job_id_not_loaded: ${jobId}`);
        lastClickedJob = job;
        let selection = {selection_ready: false};
        if (link) {
          clickLikeUser(link);
          selection = await waitForJobSelection(jobId);
        } else if (job.href) {
          const cachedLink = document.createElement("a");
          cachedLink.href = job.href;
          cachedLink.target = "_blank";
          cachedLink.rel = "noopener";
          cachedLink.style.position = "fixed";
          cachedLink.style.left = "-20px";
          cachedLink.style.top = "0";
          document.body.appendChild(cachedLink);
          clickLikeUser(cachedLink);
          cachedLink.remove();
        } else {
          throw new Error(`job_id_not_in_dom: ${jobId}`);
        }
        await new Promise((resolve) => setTimeout(resolve, 1100));
        await sendEvent("result", id, {
          clicked_job_id: job.job_id,
          clicked_title: job.title,
          opened_cached_link: !link,
          ...selection,
          title: document.title,
          url: location.href
        });
        return;
      }
      if (action === "click_item_text") {
        const element = findListItemByText(payload.text);
        if (!element) throw new Error(`list_item_not_found: ${payload.text}`);
        const matched = elementText(element, 1000);
        const target = clickListItem(element, payload.text);
        await new Promise((resolve) => setTimeout(resolve, 1100));
        await sendEvent("result", id, {
          matched_text: matched,
          clicked_text: elementText(target, 500),
          title: document.title,
          url: location.href
        });
        return;
      }
      if (action === "fill_message") {
        const input = findMessageInput();
        if (!input) throw new Error("message_input_not_found");
        setInputValue(input, payload.text || "");
        await sendEvent("result", id, {
          draft_text: inputText(input),
          title: document.title,
          url: location.href
        });
        return;
      }
      if (action === "send_background") {
        const result = backgroundSend(
          payload.message || "",
          payload.expected_context || {}
        );
        await sendEvent("result", id, result);
        return;
      }
      if (action === "close_current_tab") {
        const closeable = location.pathname.includes("/web/geek/chat") ||
          location.pathname.includes("/job_detail/");
        if (!closeable) throw new Error(`refuse_to_close_primary_page: ${location.pathname}`);
        await sendEvent("result", id, {
          title: document.title,
          url: location.href,
          closing: true
        });
        chrome.runtime.sendMessage({type: "close-current-tab"});
        return;
      }
      throw new Error(`unknown_action: ${action}`);
    } catch (error) {
      await sendEvent("result", id, null, false, String(error?.message || error));
    }
  }

  async function poll() {
    if (busy) return false;
    busy = true;
    let failed = false;
    try {
      const now = Date.now();
      if (now - lastHeartbeat > 3000) {
        lastHeartbeat = now;
        await sendEvent("heartbeat", null, {
          title: document.title,
          url: location.href
        });
      }
      const response = await fetch(
        `${BASE}/api/command/next?token=${encodeURIComponent(TOKEN)}&client_id=${encodeURIComponent(CLIENT_ID)}&wait=20`,
        {cache: "no-store"}
      );
      const result = await response.json();
      if (result.command) await execute(result.command);
    } catch (_) {
      // Retry quietly while the local bridge is stopped or restarting.
      failed = true;
    } finally {
      busy = false;
    }
    return failed;
  }

  async function pollLoop() {
    while (true) {
      const failed = await poll();
      if (failed) await new Promise((resolve) => setTimeout(resolve, 1500));
    }
  }

  document.addEventListener("visibilitychange", poll);
  window.addEventListener("pageshow", poll);
  window.addEventListener("focus", poll);
  pollLoop();
})();
