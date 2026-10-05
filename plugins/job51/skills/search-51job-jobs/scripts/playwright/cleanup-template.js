async (page) => {
  const context = page.context();
  if (typeof context.__codexCleanup51JobDetails !== "function") return { ok: false, error: "cleanup_not_initialized" };
  return await context.__codexCleanup51JobDetails();
}
