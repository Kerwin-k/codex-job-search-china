async (page) => {
  const url = page.url();
  const platformOk = /^https:\/\/(?:we|jobs)\.51job\.com(?:\/|$)/i.test(String(url || ""));
  if (!platformOk) return { error: "unsupported_probe_page", url, platform: "51job" };
  return page.evaluate(() => {
    const visible = (node) => {
      const box = node.getBoundingClientRect();
      const style = getComputedStyle(node);
      return box.width > 0 && box.height > 0 && style.display !== "none" && style.visibility !== "hidden";
    };
    const wrappers = [...document.querySelectorAll(".custom-select-wrapper")].filter(visible);
    return {
      platform: "51job",
      url: location.href,
      controls: wrappers.map((wrapper) => ({
        title: (wrapper.querySelector(".fixed-text")?.textContent || "").trim(),
        wrapperClass: String(wrapper.className || ""),
        options: [...wrapper.querySelectorAll(".custom-option")].filter(visible)
          .map((node) => ({ text: (node.textContent || "").trim(), class: String(node.className || ""), ariaSelected: node.getAttribute("aria-selected") || "" }))
          .filter((x) => ["全职", "兼职", "实习"].includes(x.text)),
      })).filter((x) => x.title.includes("工作类型") || x.options.length).slice(0, 6),
    };
  });
}
