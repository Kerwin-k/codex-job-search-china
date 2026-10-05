async (page) => ({
  ok: true,
  sourceFingerprint: page.context().__codexZhaopinRuntimeFingerprint || "",
})
