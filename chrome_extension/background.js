const STLHUB_BASE_URL = "https://stl-hub.top";

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
