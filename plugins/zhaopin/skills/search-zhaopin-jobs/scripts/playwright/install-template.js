async (page) => {
  const context = page.context();
  if (!context.__jobflowExistingPages) context.__jobflowExistingPages = new Set(context.pages());
  if (!context.__jobflowOwnedPages) context.__jobflowOwnedPages = new Set();
  const runtimePath = "__RUNTIME_PATH__";
  const policyPath = "__POLICY_PATH__";
  const sourceFingerprint = "__SOURCE_FINGERPRINT__";
  await context.addInitScript({ path: policyPath });
  await context.addInitScript({ path: runtimePath });
  let currentPageInjected = false;
  for (const target of context.pages()) {
    if (!/https?:\/\/(?:www\.|sou\.)?zhaopin\.com\//i.test(target.url())) continue;
    try {
      await target.addScriptTag({ path: policyPath });
      await target.addScriptTag({ path: runtimePath });
      currentPageInjected = true;
    } catch {}
  }
  context.__codexZhaopinRuntimeFingerprint = sourceFingerprint;
  const recordSubmit = () => {
    const active = context.__codexActiveDetail;
    const run = context.__codexJobRun;
    if (!active || !run) return;
    const laneState = run.lanes[active.laneId];
    const cityState = run.cities[active.cityKey];
    if (laneState) {
      laneState.submits += 1;
      laneState.jdsSinceSubmit = 0;
    }
    if (cityState) {
      cityState.submits += 1;
      cityState.jdsSinceSubmit = 0;
    }
  };
  context.__codexJobSubmit = async (source, platform) => {
    if (platform !== "zhaopin") return { ok: false, error: "platform_mismatch" };
    if (!/https?:\/\/(?:www\.)?zhaopin\.com\//i.test(source.url())) return { ok: false, error: "platform_mismatch", url: source.url() };
    const restoreZhaopinList = async (target, successUrl) => {
      if (platform !== "zhaopin" || target !== source || !/zhaopin\.com\/job-applied/i.test(successUrl || "")) return;
      await target.goBack({ waitUntil: "domcontentloaded", timeout: 8000 }).catch(() => {});
      await target.waitForTimeout(500);
      if (/zhaopin\.com\/job-applied/i.test(target.url())) {
        const lane = context.__codexLane;
        if (lane?.source === "expectation_pool") await target.goto("https://www.zhaopin.com/jobs/?pageMode=recommend", { waitUntil: "domcontentloaded" }).catch(() => {});
        else if (lane?.searchUrl) await target.goto(lane.searchUrl, { waitUntil: "domcontentloaded" }).catch(() => {});
      }
      context.__codexListPage = target;
    };
    const settleStayOnPage = async (target) => {
      const result = await target.evaluate(() => {
        const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
        const visible = (node) => !!(node && node.getClientRects().length && getComputedStyle(node).visibility !== "hidden");
        const matches = [...document.querySelectorAll("button,a,[role=button],div,span")]
          .filter((node) => visible(node) && clean(node.innerText || node.textContent) === "留在此页")
          .map((node) => ({ node, score: /^(BUTTON|A)$/.test(node.tagName) || node.getAttribute("role") === "button" ? 3 : /btn|button/i.test(String(node.className)) ? 2 : node.children.length === 0 ? 1 : 0 }))
          .sort((a, b) => b.score - a.score);
        if (!matches.length) return { action: "stay_control_absent", count: 0 };
        const top = matches.filter((item) => item.score === matches[0].score && item.score > 0);
        if (top.length !== 1) return { action: "stay_control_ambiguous", count: top.length || matches.length };
        top[0].node.click();
        return { action: "stay_on_page", count: 1 };
      }).catch(() => ({ action: "stay_control_absent", count: 0 }));
      if (result.action === "stay_on_page") await target.waitForTimeout(200);
      return result;
    };
    const click = await source.evaluate((p) => window.__codexJobs.clickApply(p), platform);
    if (!click.ok) return click;
    for (let i = 0; i < 16; i++) {
      await source.waitForTimeout(250);
      for (const p of context.pages()) {
        const url = p.url();
        if (!/https?:\/\/(?:www\.)?zhaopin\.com\//i.test(url)) continue;
        if (platform === "zhaopin" && /zhaopin\.com\/job-applied/i.test(url) && (!click.jobId || url.includes(click.jobId))) {
          if (p !== source && context.__jobflowOwnedPages.has(p)) await p.close().catch(() => {});
          await restoreZhaopinList(p, url);
          recordSubmit();
          return { ok: true, platform, jobId: click.jobId, evidence: "job-applied", successUrl: url };
        }
        const boundSourceSuccess = p === source && String(context.__codexActiveDetail?.candidate?.[0] || "") === String(click.jobId || "");
        const sentResumeGreeting = await p.getByText("已向对方发送简历和打招呼语", { exact: false }).first().isVisible().catch(() => false);
        if (sentResumeGreeting && boundSourceSuccess) {
          const settled = await settleStayOnPage(p);
          recordSubmit();
          return { ok: true, platform, jobId: click.jobId, evidence: "resume-and-greeting-sent", successUrl: url, successPageIsCurrent: true, resumeMode: "platform_default", postSuccessAction: settled.action, stayControlCount: settled.count };
        }
        const success = await p.getByText("投递成功", { exact: false }).first().isVisible().catch(() => false);
        if (success && ((!click.jobId || url.includes(click.jobId)) || boundSourceSuccess)) {
          const settled = p === source ? await settleStayOnPage(p) : { action: "stay_control_absent", count: 0 };
          if (platform === "zhaopin" && p !== source && context.__jobflowOwnedPages.has(p)) await p.close().catch(() => {});
          await restoreZhaopinList(p, url);
          recordSubmit();
          return { ok: true, platform, jobId: click.jobId, evidence: "投递成功", successUrl: url, successPageIsCurrent: p === source, resumeMode: "platform_default", postSuccessAction: settled.action, stayControlCount: settled.count };
        }
      }
    }
    const boundSourceSuccess = String(context.__codexActiveDetail?.candidate?.[0] || "") === String(click.jobId || "");
    const appliedText = await source.getByText(/^(已投递|已申请|继续沟通)$/, { exact: true }).first().textContent().catch(() => "");
    if (boundSourceSuccess && /^(已投递|已申请|继续沟通)$/.test(String(appliedText || "").trim())) {
      const settled = await settleStayOnPage(source);
      recordSubmit();
      return { ok: true, platform, jobId: click.jobId, evidence: "applied-state", successUrl: source.url(), sourceState: String(appliedText || "").trim(), resumeMode: "platform_default", postSuccessAction: settled.action, stayControlCount: settled.count };
    }
    return { ok: false, pending: true, platform, jobId: click.jobId, url: source.url(), sourceState: "unknown" };
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

  return { ok: true, version: "2.2", sourceFingerprint, initScripts: true, currentPageInjected };
}
