"use strict";

chrome.runtime.onMessage.addListener((message, sender) => {
  if (message?.type !== "close-current-tab" || !sender.tab?.id) return;
  chrome.tabs.remove(sender.tab.id);
});
