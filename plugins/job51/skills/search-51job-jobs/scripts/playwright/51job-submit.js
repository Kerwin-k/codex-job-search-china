async (page) => {
  const context = page.context();
  const active = context.__codexActiveDetail;
  const detail = active && context.pages().find((candidate) => !candidate.isClosed() && candidate.url() === active.detailUrl);
  if (!active || !detail) return { ok: false, error: "bound_detail_required" };
  return await context.__codexJobSubmit(detail, "51job");
}
