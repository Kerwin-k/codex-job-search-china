async (page) => {
  const context = page.context();
  const active = context.__codexActiveDetail;
  if (active?.mode === "panel") {
    const list = context.__codexListPage;
    if (!list || list.isClosed() || list.url() !== active.detailUrl) return { ok: false, error: "bound_detail_required" };
    const current = await list.evaluate(() => window.__codexJobs.zhaopinDetail());
    if (!current.ok || current.jobId !== String(active.candidate?.[0] || "")) return { ok: false, error: "detail_binding_mismatch", jobId: current.jobId || "" };
    return await context.__codexJobSubmit(list, "zhaopin");
  }
  const detail = active && context.pages().find((candidate) => !candidate.isClosed() && candidate.url() === active.detailUrl);
  if (!active || !detail) return { ok: false, error: "bound_detail_required" };
  return await context.__codexJobSubmit(detail, "zhaopin");
}
