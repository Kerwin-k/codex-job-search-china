async (page) => {
  const url = page.url();
  const platformOk = /^https:\/\/(?:we|jobs)\.51job\.com(?:\/|$)/i.test(String(url || ""));
  if (!platformOk || /\/pc\/search(?:[/?#]|$)/i.test(url) || /\/all\/co[^/?#]*\.html(?:[?#]|$)/i.test(url)) {
    return { ok: false, platform: "51job", error: "detail_page_required", url };
  }
  const detail = await page.evaluate(() => window.__codexJobs.job51Detail());
  if (!detail?.ok) return detail;
  const missing = ["jobId", "title", "company", "salary"].filter((key) => !String(detail?.[key] || "").trim());
  if (missing.length) return { ...detail, ok: false, error: "detail_fields_missing", missing };
  return detail;
}
