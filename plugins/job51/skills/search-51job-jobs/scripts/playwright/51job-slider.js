async (page) => {
  const context = page.context();
  const pending = context.__codexPending51jobVerification;
  const allowed = (value) => /^https:\/\/(?:we|jobs)\.51job\.com(?:\/|$)/i.test(String(value || ""));
  const currentUrl = page.url();
  if (!allowed(currentUrl)) return { ok: false, error: "platform_mismatch", attempted: false, url: currentUrl };
  if (typeof context.__codexTry51JobSliderOnce !== "function") {
    return { ok: false, error: "slider_runtime_missing", attempted: false, url: currentUrl };
  }
  const result = await context.__codexTry51JobSliderOnce(page);
  if (!result?.detected) {
    return { ok: result?.ok !== false, required: false, attempted: false, url: page.url(), error: result?.error || "" };
  }
  return {
    ...result,
    jobId: String(pending?.jobId || ""),
    url: result.url || page.url(),
  };
}
