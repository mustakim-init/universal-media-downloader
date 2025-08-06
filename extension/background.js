// background.js - Enhanced with intelligent media detection

// --- Configuration ---
const MEDIA_DETECTION_CONFIG = {
  // File extensions to monitor
  mediaExtensions: ['.m3u8', '.mpd', '.ts', '.mp4', '.m4a', '.aac', '.mp3', '.webm', '.flv', '.mov', '.avi'],
  
  // Content types to monitor
  mediaContentTypes: [
    'application/x-mpegurl',
    'application/vnd.apple.mpegurl',
    'application/dash+xml',
    'video/mp2t',
    'video/mp4',
    'video/webm',
    'video/x-flv',
    'audio/mp4',
    'audio/aac',
    'audio/mpeg',
    'audio/webm'
  ],
  
  // URL patterns for CDN/temporary media
  cdnPatterns: [
    /fbcdn\.net/i,
    /instagram.*cdn/i,
    /googlevideo\.com/i,
    /videodelivery\.net/i,
    /cloudfront\.net/i,
    /akamaihd\.net/i,
    /fastly\.net/i,
    /cloudflare\.com/i,
    /blob:/i
  ],
  
  // Patterns to ignore
  ignorePatterns: [
    /thumb/i, /preview/i, /thumbnail/i, /avatar/i, /profile/i, /cover/i,
    /\.jpg(\?|$)/i, /\.png(\?|$)/i, /\.jpeg(\?|$)/i, /\.gif(\?|$)/i, /\.svg(\?|$)/i,
    /\.css(\?|$)/i, /\.js(\?|$)/i, /\.woff/i, /\.json(\?|$)/i,
    /google-analytics/i, /doubleclick/i, /facebook\.com\/tr/i, /analytics/i,
    /\.ico(\?|$)/i, /favicon/i
  ],
  
  // Minimum file size to consider (in bytes) - filters out small tracker files
  minFileSize: 100000 // 100KB
};

// --- State Management ---
const tabMediaState = new Map();
const requestSizeTracking = new Map();

// --- Helper Functions ---
function updateBadge(tabId, count) {
  chrome.action.setBadgeText({
    text: count > 0 ? String(count) : '',
    tabId: tabId
  });
  chrome.action.setBadgeBackgroundColor({
    color: '#9d4edd',
    tabId: tabId
  });
}

function shouldIgnoreUrl(url) {
  return MEDIA_DETECTION_CONFIG.ignorePatterns.some(pattern => pattern.test(url));
}

function isMediaUrl(url) {
  // Check if URL contains media extension
  const hasMediaExtension = MEDIA_DETECTION_CONFIG.mediaExtensions.some(ext => {
    const urlLower = url.toLowerCase();
    return urlLower.includes(ext) || urlLower.includes(encodeURIComponent(ext));
  });
  
  // Check if URL matches CDN patterns (often indicates media)
  const isCdnUrl = MEDIA_DETECTION_CONFIG.cdnPatterns.some(pattern => pattern.test(url));
  
  return hasMediaExtension || isCdnUrl;
}

function extractMediaInfo(url, responseHeaders = []) {
  const info = {
    url: url,
    type: 'unknown',
    size: null,
    contentType: null,
    isTemporary: false,
    platform: null
  };
  
  // Check if it's a temporary/CDN URL
  info.isTemporary = MEDIA_DETECTION_CONFIG.cdnPatterns.some(pattern => pattern.test(url));
  
  // Extract content type from headers
  const contentTypeHeader = responseHeaders.find(h => h.name.toLowerCase() === 'content-type');
  if (contentTypeHeader) {
    info.contentType = contentTypeHeader.value.split(';')[0].trim();
    
    if (info.contentType.startsWith('video/')) {
      info.type = 'video';
    } else if (info.contentType.startsWith('audio/')) {
      info.type = 'audio';
    }
  }
  
  // Extract size from headers
  const contentLengthHeader = responseHeaders.find(h => h.name.toLowerCase() === 'content-length');
  if (contentLengthHeader) {
    info.size = parseInt(contentLengthHeader.value, 10);
  }
  
  // Try to detect platform from URL
  if (url.includes('fbcdn.net')) {
    info.platform = 'facebook';
  } else if (url.includes('instagram')) {
    info.platform = 'instagram';
  } else if (url.includes('googlevideo.com')) {
    info.platform = 'youtube';
  } else if (url.includes('tiktok')) {
    info.platform = 'tiktok';
  }
  
  // Guess type from extension if not determined
  if (info.type === 'unknown') {
    const urlLower = url.toLowerCase();
    if (urlLower.includes('.mp4') || urlLower.includes('.webm') || urlLower.includes('.flv')) {
      info.type = 'video';
    } else if (urlLower.includes('.mp3') || urlLower.includes('.m4a') || urlLower.includes('.aac')) {
      info.type = 'audio';
    } else if (urlLower.includes('.m3u8') || urlLower.includes('.mpd')) {
      info.type = 'stream';
    }
  }
  
  return info;
}

function storeMediaForTab(tabId, mediaInfo) {
  if (!tabMediaState.has(tabId)) {
    tabMediaState.set(tabId, {
      pageUrl: '',
      media: [],
      lastUpdated: Date.now()
    });
  }
  
  const tabData = tabMediaState.get(tabId);
  
  // Check if URL already exists
  const existingIndex = tabData.media.findIndex(m => m.url === mediaInfo.url);
  if (existingIndex === -1) {
    tabData.media.push(mediaInfo);
  } else {
    // Update existing entry
    tabData.media[existingIndex] = mediaInfo;
  }
  
  // Keep only the most recent 50 media items
  if (tabData.media.length > 50) {
    tabData.media = tabData.media.slice(-50);
  }
  
  tabData.lastUpdated = Date.now();
  
  // Update badge
  updateBadge(tabId, tabData.media.length);
  
  // Store in session storage for persistence
  const storageKey = `tab_${tabId}_media`;
  chrome.storage.session.set({
    [storageKey]: tabData.media.map(m => ({
      url: m.url,
      type: m.type,
      isTemporary: m.isTemporary
    }))
  });
}

// --- Main Detection Logic ---
chrome.webRequest.onResponseStarted.addListener(
  (details) => {
    const { tabId, url, responseHeaders, statusCode, method } = details;
    
    // Skip if not a successful request or not associated with a tab
    if (tabId < 0 || statusCode !== 200 || method !== 'GET') return;
    
    // Skip if URL should be ignored
    if (shouldIgnoreUrl(url)) return;
    
    // Check if this might be a media URL
    if (!isMediaUrl(url)) {
      // Also check content-type header
      const contentTypeHeader = responseHeaders?.find(h => 
        h.name.toLowerCase() === 'content-type'
      );
      
      if (!contentTypeHeader || !MEDIA_DETECTION_CONFIG.mediaContentTypes.some(type => 
        contentTypeHeader.value.includes(type)
      )) {
        return;
      }
    }
    
    // Extract media information
    const mediaInfo = extractMediaInfo(url, responseHeaders || []);
    
    // Skip if file is too small (likely not actual media)
    if (mediaInfo.size && mediaInfo.size < MEDIA_DETECTION_CONFIG.minFileSize) {
      return;
    }
    
    // Store media information
    storeMediaForTab(tabId, mediaInfo);
    
    // Log detection for debugging
    console.log(`Media detected on tab ${tabId}:`, {
      url: url.substring(0, 100) + '...',
      type: mediaInfo.type,
      size: mediaInfo.size,
      isTemporary: mediaInfo.isTemporary
    });
  },
  { urls: ["<all_urls>"] },
  ["responseHeaders"]
);

// Alternative detection using onBeforeRequest for URLs that might not trigger onResponseStarted
chrome.webRequest.onBeforeRequest.addListener(
  (details) => {
    const { tabId, url, method } = details;
    
    if (tabId < 0 || method !== 'GET') return;
    if (shouldIgnoreUrl(url)) return;
    
    // Only process URLs with clear media indicators
    const hasMediaExtension = MEDIA_DETECTION_CONFIG.mediaExtensions.some(ext => {
      const urlLower = url.toLowerCase();
      const cleanUrl = urlLower.split('?')[0].split('#')[0];
      return cleanUrl.endsWith(ext);
    });
    
    if (hasMediaExtension) {
      const mediaInfo = extractMediaInfo(url);
      storeMediaForTab(tabId, mediaInfo);
    }
  },
  { urls: ["<all_urls>"] },
  []
);

// --- Tab Management ---
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  // Use the 'tab' object as it's the most reliable source for the URL.
  const currentUrl = tab.url;
  
  // We only care about main page navigations with a complete URL
  if (changeInfo.status === 'loading' && currentUrl) {
    const tabData = tabMediaState.get(tabId);
    
    if (tabData) {
      const oldUrlBase = (tabData.pageUrl || '').split('#')[0];
      const newUrlBase = currentUrl.split('#')[0];
      
      // If the base URL has changed, it's a new page, so we clear old media.
      if (oldUrlBase !== newUrlBase) {
        tabMediaState.delete(tabId);
        chrome.storage.session.remove(`tab_${tabId}_media`);
        updateBadge(tabId, 0);
      }
    }
    
    // Update or create the state with the latest definite URL.
    if (!tabMediaState.has(tabId)) {
      tabMediaState.set(tabId, {
        pageUrl: currentUrl,
        media: [],
        lastUpdated: Date.now()
      });
    } else {
      tabMediaState.get(tabId).pageUrl = currentUrl;
    }
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  // Clean up data for closed tab
  tabMediaState.delete(tabId);
  chrome.storage.session.remove(`tab_${tabId}_media`);
  requestSizeTracking.delete(tabId);
});

// --- Message Handling ---
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.type === 'get_media_urls') {
    const tabId = request.tabId;
    const tabData = tabMediaState.get(tabId);
    
    if (tabData && tabData.media.length > 0) {
      // Sort media by type priority and recency
      const sortedMedia = [...tabData.media].sort((a, b) => {
        // Prioritize non-temporary URLs
        if (!a.isTemporary && b.isTemporary) return -1;
        if (a.isTemporary && !b.isTemporary) return 1;
        
        // Then by type (video > audio > stream > unknown)
        const typePriority = { video: 4, audio: 3, stream: 2, unknown: 1 };
        const aPriority = typePriority[a.type] || 0;
        const bPriority = typePriority[b.type] || 0;
        
        return bPriority - aPriority;
      });
      
      sendResponse({
        urls: sortedMedia.map(m => m.url),
        mediaInfo: sortedMedia,
        pageUrl: tabData.pageUrl
      });
    } else {
      // Try to get from session storage
      chrome.storage.session.get(`tab_${tabId}_media`).then(data => {
        const storedMedia = data[`tab_${tabId}_media`] || [];
        sendResponse({
          urls: storedMedia.map(m => m.url),
          mediaInfo: storedMedia,
          pageUrl: ''
        });
      });
      return true; // Will send response asynchronously
    }
  }
  
  if (request.type === 'analyze_tab') {
    const tabId = request.tabId;
    const tabData = tabMediaState.get(tabId);
    
    const analysis = {
      hasMedia: tabData ? tabData.media.length > 0 : false,
      mediaCount: tabData ? tabData.media.length : 0,
      mediaTypes: tabData ? [...new Set(tabData.media.map(m => m.type))] : [],
      hasTemporaryUrls: tabData ? tabData.media.some(m => m.isTemporary) : false,
      platforms: tabData ? [...new Set(tabData.media.filter(m => m.platform).map(m => m.platform))] : []
    };
    
    sendResponse(analysis);
  }
});

// --- Periodic Cleanup ---
setInterval(() => {
  const now = Date.now();
  const maxAge = 30 * 60 * 1000; // 30 minutes
  
  for (const [tabId, data] of tabMediaState.entries()) {
    if (now - data.lastUpdated > maxAge) {
      tabMediaState.delete(tabId);
      chrome.storage.session.remove(`tab_${tabId}_media`);
    }
  }
}, 5 * 60 * 1000); // Run every 5 minutes

console.log('Universal Media Downloader background script loaded with enhanced detection');