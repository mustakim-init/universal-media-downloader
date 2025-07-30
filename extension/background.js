// background.js

// List of media content types and extensions to look for.
const MEDIA_CONTENT_TYPES = [
  'application/x-mpegurl',
  'application/vnd.apple.mpegurl',
  'video/mp2t',
  'video/mp4',
  'audio/mp4',
  'audio/aac',
  'audio/mpeg'
];

const MEDIA_FILE_EXTENSIONS = ['.m3u8', '.ts', '.mp4', '.m4a', '.aac', '.mp3'];

// Store found media URLs per tab using session storage (clears when browser closes)
async function addMediaUrlForTab(tabId, url) {
  const key = `tab_${tabId}_media`;
  const data = await chrome.storage.session.get(key);
  const urls = data[key] || [];
  if (!urls.includes(url)) {
    urls.push(url);
    await chrome.storage.session.set({ [key]: urls });
    updateBadge(tabId, urls.length);
  }
}

function updateBadge(tabId, count) {
  const text = count > 0 ? String(count) : '';
  chrome.action.setBadgeText({ tabId: tabId, text: text });
  chrome.action.setBadgeBackgroundColor({ color: '#9d4edd' });
}

// Listen to network requests
chrome.webRequest.onHeadersReceived.addListener(
  (details) => {
    if (details.tabId < 0) return; // Ignore requests not associated with a tab

    const { url, responseHeaders } = details;
    let isMedia = false;

    // 1. Check by file extension
    for (const ext of MEDIA_FILE_EXTENSIONS) {
      if (url.toLowerCase().includes(ext)) {
        isMedia = true;
        break;
      }
    }

    // 2. Check by Content-Type header (more reliable)
    if (!isMedia) {
      const contentTypeHeader = responseHeaders.find(
        (header) => header.name.toLowerCase() === 'content-type'
      );
      if (contentTypeHeader) {
        const contentType = contentTypeHeader.value.toLowerCase();
        for (const type of MEDIA_CONTENT_TYPES) {
          if (contentType.includes(type)) {
            isMedia = true;
            break;
          }
        }
      }
    }

    if (isMedia) {
      addMediaUrlForTab(details.tabId, url);
    }
  },
  { urls: ['<all_urls>'] },
  ['responseHeaders']
);

// Clear stored URLs when a tab is closed or navigates to a new page
chrome.tabs.onRemoved.addListener((tabId) => {
  chrome.storage.session.remove(`tab_${tabId}_media`);
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
    // A navigation is considered complete when the status is 'complete' and it has a URL
    if (changeInfo.status === 'loading' && changeInfo.url) {
        chrome.storage.session.remove(`tab_${tabId}_media`);
        updateBadge(tabId, 0);
    }
});


// Message listener for communication with the popup
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.type === 'get_media_urls') {
    const key = `tab_${request.tabId}_media`;
    chrome.storage.session.get(key).then(data => {
      sendResponse({ urls: data[key] || [] });
    });
    return true; // Indicates that the response is sent asynchronously
  }
});