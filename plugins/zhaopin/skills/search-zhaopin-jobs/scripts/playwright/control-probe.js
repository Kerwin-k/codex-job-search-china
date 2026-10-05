async (page) => {
  const url = page.url();
  const visible = (node) => {
    const box = node.getBoundingClientRect();
    const style = getComputedStyle(node);
    return box.width > 0 && box.height > 0 && style.display !== "none" && style.visibility !== "hidden";
  };
  const attrs = (node) => ({
    tag: node.tagName.toLowerCase(),
    text: (node.innerText || node.textContent || "").replace(/\s+/g, " ").trim(),
    href: node.getAttribute("href") || "",
    class: String(node.className || ""),
    role: node.getAttribute("role") || "",
    ariaSelected: node.getAttribute("aria-selected") || "",
    ariaPressed: node.getAttribute("aria-pressed") || "",
    ariaCurrent: node.getAttribute("aria-current") || "",
  });
  if (/zhaopin\.com/i.test(url)) {
    const labels = ["系统工程师", "IT技术支持", "信息技术经理/主管"];
    const isRecommendUrl = (value) => {
      const match = String(value || "").match(/^https?:\/\/www\.zhaopin\.com(\/[^?#]*)(?:\?([^#]*))?/i);
      if (!match) return false;
      return match[1] === "/recommend"
        || (match[1] === "/jobs/" && /(?:^|&)pageMode=recommend(?:&|$)/i.test(match[2] || ""));
    };
    await page.getByText("系统工程师", { exact: true }).first().waitFor({ state: "visible", timeout: 8000 }).catch(() => {});
    if (isRecommendUrl(url)) {
      const cityStructure = await page.evaluate((labels) => {
        const visible = (node) => { const r=node.getBoundingClientRect(),s=getComputedStyle(node); return r.width>0&&r.height>0&&s.display!=="none"&&s.visibility!=="hidden"; };
        const pools=[...document.querySelectorAll("*")].filter(node=>{const r=node.getBoundingClientRect();return visible(node)&&r.top<=260&&labels.includes((node.textContent||"").trim())&&![...node.children].some(child=>labels.includes((child.textContent||"").trim()));});
        if(pools.length<3) return {error:"pool_anchor_missing",poolCount:pools.length};
        const bottom=Math.max(...pools.map(node=>node.getBoundingClientRect().bottom));
        const compact=(node)=>{const r=node.getBoundingClientRect(),s=getComputedStyle(node);return {tag:node.tagName.toLowerCase(),text:(node.innerText||node.textContent||"").replace(/\s+/g," ").trim().slice(0,80),class:String(node.className||""),role:node.getAttribute("role")||"",ariaExpanded:node.getAttribute("aria-expanded")||"",ariaSelected:node.getAttribute("aria-selected")||"",cursor:s.cursor,box:{x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height)}};};
        return [...document.querySelectorAll("*")].filter(node=>{if(!visible(node))return false;const r=node.getBoundingClientRect();return r.top>=bottom+2&&r.top<=bottom+100&&r.left>=20&&r.left<=800&&r.width>=20&&r.height<=80;}).map(node=>({node:compact(node),parent:node.parentElement?compact(node.parentElement):null,children:[...node.children].slice(0,5).map(compact)})).slice(0,15);
      }, labels);
      const cardStructure = await page.evaluate(() => {
        const visible = (node) => { const r=node.getBoundingClientRect(),s=getComputedStyle(node); return r.width>0&&r.height>0&&s.display!=="none"&&s.visibility!=="hidden"; };
        const compact = (node) => ({
          tag: node.tagName.toLowerCase(),
          text: (node.innerText || node.textContent || "").replace(/\s+/g," ").trim().slice(0,120),
          class: String(node.className || ""),
          attrs: [...node.attributes].filter((attr) => /^(data-|id$|role$|aria-)/i.test(attr.name)).slice(0,12).reduce((out, attr) => { out[attr.name] = attr.value.slice(0,180); return out; }, {}),
        });
        const cards = [...document.querySelectorAll(".job-card")].filter(visible).slice(0,3).map((node) => ({
          card: compact(node),
          links: [...node.querySelectorAll("a[href]")].slice(0,6).map((link) => ({text:(link.innerText || "").replace(/\s+/g," ").trim().slice(0,80),href:link.href.slice(0,220),class:String(link.className || "")})),
          buttons: [...node.querySelectorAll("button,[role=button]")].slice(0,6).map(compact),
          children: [...node.children].slice(0,8).map(compact),
        }));
        const panels = [...document.querySelectorAll(".job-detail-panel,[class*='job-detail-panel'],[class*='detail-panel']")].filter(visible).slice(0,2).map((node) => ({
          panel: compact(node),
          links: [...node.querySelectorAll("a[href]")].slice(0,8).map((link) => ({text:(link.innerText || "").replace(/\s+/g," ").trim().slice(0,80),href:link.href.slice(0,220),class:String(link.className || "")})),
          buttons: [...node.querySelectorAll("button,[role=button]")].slice(0,8).map(compact),
        }));
        return {cardSelector:".job-card", cardCount:document.querySelectorAll(".job-card").length, cards, panels};
      });
      return { platform:"zhaopin", url:page.url(), cityStructure, cardStructure };
    }
    const comparison = await page.evaluate((labels) => {
      const visible = (node) => { const b=node.getBoundingClientRect(),s=getComputedStyle(node); return b.width>0&&b.height>0&&b.y<=300&&s.display!=="none"&&s.visibility!=="hidden"; };
      const style = (node, pseudo = null) => { const s=getComputedStyle(node,pseudo); return {display:s.display,width:s.width,height:s.height,fontWeight:s.fontWeight,color:s.color,background:s.backgroundColor,borderBottom:`${s.borderBottomWidth} ${s.borderBottomStyle} ${s.borderBottomColor}`,content:s.content}; };
      const result = {};
      for (const label of labels) {
        const exact=[...document.querySelectorAll("*")].filter(node=>visible(node)&&(node.innerText||node.textContent||"").replace(/\s+/g," ").trim()===label);
        result[label]=exact.slice(0,3).map(node=>{
          const chain=[]; let current=node;
          for(let depth=0;current&&depth<6;depth++,current=current.parentElement){chain.push({depth,tag:current.tagName.toLowerCase(),class:String(current.className||""),role:current.getAttribute("role")||"",ariaSelected:current.getAttribute("aria-selected")||"",ariaCurrent:current.getAttribute("aria-current")||"",style:style(current),before:style(current,"::before"),after:style(current,"::after"),children:[...current.children].slice(0,6).map(child=>({tag:child.tagName.toLowerCase(),class:String(child.className||""),text:(child.innerText||child.textContent||"").replace(/\s+/g," ").trim().slice(0,40),style:style(child)}))});}
          return chain;
        });
      }
      return result;
    }, labels);
    return { platform:"zhaopin", url:page.url(), comparison };
  }
  return { error:"unsupported_probe_page", url };
}
