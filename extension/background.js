// IMPROVED background.js - Only detect ACTIVE/PLAYING media

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

// Patterns for common streaming video platforms
const STREAMING_PATTERNS = [
  /youtube\.com\/watch/,
  /youtu\.be\//,
  /facebook\.com\/.*\/videos/,
  /fb\.watch\//,
  /instagram\.com\/p\//,
  /instagram\.com\/reel\//,
  /tiktok\.com\/@.*\/video/
];

// NEW: Patterns for URLs that should be IGNORED (thumbnails, previews, etc.)
const IGNORE_PATTERNS = [
  /thumb/i,
  /preview/i,
  /thumbnail/i,
  /avatar/i,
  /profile/i,
  /cover/i,
  /safe_image/i,
  /scontent.*\.jpg/i,
  /scontent.*\.png/i,
  /\.jpg(\?|$)/i,
  /\.png(\?|$)/i,
  /\.jpeg(\?|$)/i,
  /\.gif(\?|$)/i,
  /\.webp(\?|$)/i
];

// NEW: Function to check if URL should be ignored
function shouldIgnoreUrl(url) {
  return IGNORE_PATTERNS.some(pattern => pattern.test(url));
}

// Function to check if a URL matches a streaming pattern
function isStreamingUrl(url) {
  return STREAMING_PATTERNS.some(pattern => pattern.test(url));
}

// NEW: Enhanced function to filter relevant media URLs
function isRelevantMediaUrl(url, tabUrl) {
  // Ignore thumbnails and preview images
  if (shouldIgnoreUrl(url)) {
    return false;
  }
  
  // For streaming platforms, be more selective
  if (tabUrl && isStreamingUrl(tabUrl)) {
    // Only allow high-quality video formats and streaming manifests
    const hasGoodExtension = MEDIA_FILE_EXTENSIONS.some(ext => 
      url.toLowerCase().includes(ext)
    );
    
    // For Facebook specifically, ignore very short URLs (usually thumbnails)
    if (tabUrl.includes('facebook.com') || tabUrl.includes('fb.watch')) {
      if (url.length < 50) return false; // Short URLs are usually thumbnails
      if (url.includes('safe_image')) return false;
      if (url.includes('static.')) return false;
    }
    
    return hasGoodExtension;
  }
  
  return true; // Allow all media for non-streaming sites
}

// Store found media URLs per tab using session storage (clears when browser closes)
async function addMediaUrlForTab(tabId, url, tabUrl = null) {
  // NEW: Check if this URL is relevant before adding
  if (!isRelevantMediaUrl(url, tabUrl)) {
    return; // Don't add irrelevant URLs
  }
  
  const key = `tab_${tabId}_media`;
  const data = await chrome.storage.session.get(key);
  let urls = data[key] || [];
  
  // Clean and deduplicate URL before adding
  const cleanedUrl = cleanUrl(url);
  
  // NEW: For streaming platforms, limit to maximum 3 URLs to prevent spam
  if (tabUrl && isStreamingUrl(tabUrl)) {
    if (!urls.includes(cleanedUrl)) {
      urls.unshift(cleanedUrl); // Add to beginning
      
      // Keep only the 3 most recent URLs for streaming platforms
      if (urls.length > 3) {
        urls = urls.slice(0, 3);
      }
      
      await chrome.storage.session.set({ [key]: urls });
      updateBadge(tabId, urls.length);
    }
  } else {
    // For non-streaming sites, keep the original logic
    if (!urls.includes(cleanedUrl)) {
      urls.push(cleanedUrl);
      await chrome.storage.session.set({ [key]: urls });
      updateBadge(tabId, urls.length);
    }
  }
}

function updateBadge(tabId, count) {
  const text = count > 0 ? String(count) : '';
  chrome.action.setBadgeText({ tabId: tabId, text: text });
  chrome.action.setBadgeBackgroundColor({ color: '#9d4edd' });
}

// Function to clean URLs (remove tracking parameters)
function cleanUrl(url) {
    try {
        const urlObj = new URL(url);
        // Remove common tracking parameters
        ['utm_source', 'utm_medium', 'fbclid', 'gclid', 'feature'].forEach(param => {
            urlObj.searchParams.delete(param);
        });
        // Remove YouTube specific parameters that don't affect content
        if (urlObj.hostname.includes('youtube.com') || urlObj.hostname.includes('youtu.be')) {
            ['index', 'list', 't', 'start', 'end'].forEach(param => {
                urlObj.searchParams.delete(param);
            });
        }
        return urlObj.toString();
    } catch {
        return url; // Return original URL if it's not a valid URL
    }
}

// NEW: Function to get tab URL for filtering purposes
async function getTabUrl(tabId) {
  try {
    const tab = await chrome.tabs.get(tabId);
    return tab.url;
  } catch {
    return null;
  }
}

// Listen to network requests
chrome.webRequest.onHeadersReceived.addListener(
  async (details) => {
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

    // 3. Check by streaming platform patterns (but be more selective)
    if (!isMedia) {
        if (isStreamingUrl(url)) {
            // Only consider it media if it has proper media extensions
            const hasMediaExt = MEDIA_FILE_EXTENSIONS.some(ext => 
              url.toLowerCase().includes(ext)
            );
            if (hasMediaExt) {
              isMedia = true;
            }
        }
    }

    if (isMedia) {
      // NEW: Get tab URL for filtering
      const tabUrl = await getTabUrl(details.tabId);
      addMediaUrlForTab(details.tabId, url, tabUrl);
    }
  },
  { urls: ['<all_urls>'] },
  ['responseHeaders']
);

// Clear stored URLs when a tab is closed or navigates to a new page
chrome.tabs.onRemoved.addListener((tabId) => {
  chrome.storage.session.remove(`tab_${tabId}_media`);
  chrome.storage.session.remove(`tab_${tabId}_lastUrl`); // Clean up
});

// NEW: Improved tab navigation detection
chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
    if (changeInfo.status === 'loading' && changeInfo.url) {
        // Get the previous URL to compare
        chrome.storage.session.get(`tab_${tabId}_lastUrl`).then(data => {
            const lastUrl = data[`tab_${tabId}_lastUrl`];
            const newUrlBase = changeInfo.url.split('#')[0].split('?')[0];
            const lastUrlBase = lastUrl ? lastUrl.split('#')[0].split('?')[0] : '';
            
            // Only clear if it's a significant navigation (not just hash/param changes)
            if (newUrlBase !== lastUrlBase) {
                chrome.storage.session.remove(`tab_${tabId}_media`);
                updateBadge(tabId, 0);
            }
            
            // Store the current URL for next comparison
            chrome.storage.session.set({ [`tab_${tabId}_lastUrl`]: changeInfo.url });
        });
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
  } else if (request.type === 'is_streaming_url') {
      sendResponse({ isStreaming: isStreamingUrl(request.url) });
      return true;
  } else if (request.type === 'clear_tab_urls') {
      // NEW: Allow popup to manually clear URLs for current tab
      const key = `tab_${request.tabId}_media`;
      chrome.storage.session.remove(key);
      updateBadge(request.tabId, 0);
      sendResponse({ success: true });
      return true;
  }
});