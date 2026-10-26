const STLHUB_BASE_URL = "http://127.0.0.1:8000";

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "stlhub-search-image",
    title: "🔍 Знайти цю 3D-модель у STL-HUB",
    contexts: ["image"]
  });
});

chrome.contextMenus.onClicked.addListener((info) => {
  if (info.menuItemId === "stlhub-search-image" && info.srcUrl) {
    const targetUrl = `${STLHUB_BASE_URL}/?image_url=${encodeURIComponent(info.srcUrl)}`;
    chrome.tabs.create({ url: targetUrl });
  }
});
