async (page) => {
  const context = page.context();
  const active = context.__codexActiveDetail;
  if (!active || !active.detailUrl || !active.candidate) return { ok: false, error: "active_detail_required" };
  const expectedId = String(active.candidate[0] || "");
  if (active.mode === "panel") {
    const list = context.__codexListPage;
    if (!list || list.isClosed() || list.url() !== active.detailUrl) return { ok: false, error: "active_detail_page_missing", jobId: expectedId };
    const detail = await list.evaluate(() => window.__codexJobs.zhaopinDetail());
    const binding = await list.evaluate(({ card, detail: actual }) => window.__codexJobPolicy.bindDetail(card, actual), { card: active.candidate, detail });
    if (!detail.ok || !binding.ok) return { ok: false, error: detail.ok ? binding.error : (detail.error || "detail_read_failed"), jobId: expectedId, mismatches: binding.mismatches || [] };
    return { ...detail, binding: true };
  }
  const hrefJobId = (value) => (String(value || "").match(/\/jobdetail\/([^/?]+)\.htm/i) || [])[1] || "";
  const detailPage = context.pages().find((candidate) => !candidate.isClosed() && hrefJobId(candidate.url()) === expectedId);
  if (!detailPage) return { ok: false, error: "active_detail_page_missing", jobId: expectedId };
  const detail = await detailPage.evaluate(() => window.__codexJobs.zhaopinDetail());
  const binding = await detailPage.evaluate(({ card, detail }) => window.__codexJobPolicy.bindDetail(card, detail), { card: active.candidate, detail });
  if (!detail.ok || !binding.ok) return { ok: false, error: detail.ok ? binding.error : (detail.error || "detail_read_failed"), jobId: expectedId, mismatches: binding.mismatches || [] };
  return { ...detail, binding: true };
}
