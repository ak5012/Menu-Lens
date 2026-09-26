// Clicking the toolbar icon opens the side panel and grants access to the current tab
// (activeTab), so MenuLens only ever reads a page the user asked it to read.

chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: false }).catch(() => {});

chrome.action.onClicked.addListener((tab) => {
  // open() must run inside the click, before any await, or the browser refuses it.
  // Opened per window, so the panel stays put and follows whichever tab is active.
  chrome.sidePanel.open({ windowId: tab.windowId }).catch(() => {});
  // A panel that is already open rescans now that it has access to this tab.
  chrome.runtime.sendMessage({ type: "menulens:rescan", tabId: tab.id }).catch(() => {});
});
