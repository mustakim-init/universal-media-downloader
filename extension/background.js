// IMPROVED background.js - Better Facebook media detection

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

// IMPROVED: More comprehensive ignore patterns for Facebook
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
  /\.webp(\?|$)/i,
  // Facebook specific ignore patterns
  /facebook\.com.*static_map/i,
  /facebook\.com.*rsrc\.php/i,
  /fbcdn\.net.*\.jpg/i,
  /fbcdn\.net.*\.png/i,
  /fbcdn\.net.*\.gif/i,
  /fbcdn\.net.*\.webp/i,
  /fbstatic-a\.akamaihd\.net/i,
  /static\.xx\.fbcdn\.net/i,
  /graph\.facebook\.com/i,
  // Generic image/static content patterns
  /\/images?\//i,
  /\/img\//i,
  /\/static\//i,
  /\/assets?\//i
];

// NEW: Facebook specific validation patterns
const FACEBOOK_VALID_PATTERNS = [
  // Facebook video URLs that are likely playable
  /facebook\.com\/.*\/videos\/\d+/,
  /fb\.watch\/[a-zA-Z0-9]+/,
  // Direct video file patterns
  /video-.*\.fbcdn\.net.*\.mp4/i,
  /scontent.*\.fbcdn\.net.*\.mp4/i,
  // HLS streams
  /.*\.fbcdn\.net.*\.m3u8/i
];

// NEW: Function to validate Facebook URLs more strictly
function isValidFacebookMediaUrl(url, tabUrl) {
  // If not a Facebook context, don't apply Facebook-specific rules
  if (!tabUrl || (!tabUrl.includes('facebook.com') && !tabUrl.includes('fb.watch'))) {
    return true;
  }

  // Check if URL matches any valid Facebook media patterns
  const isValidPattern = FACEBOOK_VALID_PATTERNS.some(pattern => pattern.test(url));
  
  if (!isValidPattern) {
    return false;
  }

  // Additional size-based filtering for Facebook
  if (url.includes('facebook.com') || url.includes('fbcdn.net')) {
    // Reject very short URLs (usually thumbnails or metadata)
    if (url.length < 80) return false;
    
    // Reject URLs with typical thumbnail indicators
    if (/s\d+x\d+/i.test(url)) return false; // Size indicators like s320x240
    if (/\d+x\d+/i.test(url) && url.length < 150) return false; // Dimension patterns in short URLs
    
    // Look for quality indicators that suggest actual video content
    const hasQualityIndicators = /(?:hd|720p|1080p|_hq|high)/i.test(url);
    const hasVideoFormat = /\.mp4/i.test(url);
    const isHLS = /\.m3u8/i.test(url);
    
    // For Facebook, be more strict - require either quality indicators, proper format, or HLS
    if (!hasQualityIndicators && !hasVideoFormat && !isHLS) {
      return false;
    }
  }

  return true;
}

// Function to check if URL should be ignored
function shouldIgnoreUrl(url) {
  return IGNORE_PATTERNS.some(pattern => pattern.test(url));
}

// Function to check if a URL matches a streaming pattern
function isStreamingUrl(url) {
  return STREAMING_PATTERNS.some(pattern => pattern.test(url));
}

// IMPROVED: Enhanced function to filter relevant media URLs
function isRelevantMediaUrl(url, tabUrl) {
  // First check basic ignore patterns
  if (shouldIgnoreUrl(url)) {
    return false;
  }
  
  // Apply Facebook-specific validation
  if (!isValidFacebookMediaUrl(url, tabUrl)) {
    return false;
  }
  
  // For streaming platforms, be more selective
  if (tabUrl && isStreamingUrl(tabUrl)) {
    // Only allow high-quality video formats and streaming manifests
    const hasGoodExtension = MEDIA_FILE_EXTENSIONS.some(ext => 
      url.toLowerCase().includes(ext)
    );
    
    // Facebook specific additional filtering
    if (tabUrl.includes('facebook.com') || tabUrl.includes('fb.watch')) {
      if (!hasGoodExtension) return false;
      
      // Require minimum URL length for Facebook
      if (url.length < 100) return false;
      
      // Must not contain obvious thumbnail indicators
      if (/thumb|preview|small|tiny/i.test(url)) return false;
      
      // Should contain video-related domains for Facebook
      const hasFacebookVideoDomain = /video.*\.fbcdn\.net|scontent.*\.fbcdn\.net.*\.mp4/i.test(url);
      if (!hasFacebookVideoDomain && !url.includes('.m3u8')) {
        return false;
      }
    }
    
    return hasGoodExtension;
  }
  
  return true; // Allow all media for non-streaming sites
}

// NEW: Function to check if media is likely active/playing
async function isMediaLikelyActive(url, tabId) {
  // Check if there are active media elements in the tab
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId: tabId },
      func: () => {
        const videos = document.querySelectorAll('video');
        const audios = document.querySelectorAll('audio');
        
        // Check for playing media
        const playingMedia = [...videos, ...audios].filter(media => 
          !media.paused && media.currentTime > 0 && media.readyState > 2
        );
        
        return {
          hasActiveVideo: videos.length > 0 && [...videos].some(v => !v.paused),
          hasActiveAudio: audios.length > 0 && [...audios].some(a => !a.paused),
          totalMedia: videos.length + audios.length,
          playingCount: playingMedia.length
        };
      }
    });
    
    if (results && results[0] && results[0].result) {
      const mediaInfo = results[0].result;
      return mediaInfo.hasActiveVideo || mediaInfo.hasActiveAudio || mediaInfo.playingCount > 0;
    }
  } catch (error) {
    // If we can't check, assume it might be active
    return true;
  }
  
  return true;
}

// Store found media URLs per tab using session storage (clears when browser closes)
async function addMediaUrlForTab(tabId, url, tabUrl = null) {
  // Check if this URL is relevant before adding
  if (!isRelevantMediaUrl(url, tabUrl)) {
    return; // Don't add irrelevant URLs
  }
  
  const key = `tab_${tabId}_media`;
  const data = await chrome.storage.session.get(key);
  let urls = data[key] || [];
  
  // Clean and deduplicate URL before adding
  const cleanedUrl = cleanUrl(url);
  
  // For Facebook, do additional active media check
  if (tabUrl && (tabUrl.includes('facebook.com') || tabUrl.includes('fb.watch'))) {
    const isLikelyActive = await isMediaLikelyActive(cleanedUrl, tabId);
    if (!isLikelyActive && urls.length > 0) {
      // If media doesn't seem active and we already have URLs, skip this one
      return;
    }
  }
  
  // For streaming platforms, limit to maximum 2 URLs to prevent spam
  if (tabUrl && isStreamingUrl(tabUrl)) {
    if (!urls.includes(cleanedUrl)) {
      urls.unshift(cleanedUrl); // Add to beginning
      
      // Keep only the 2 most recent URLs for streaming platforms
      if (urls.length > 2) {
        urls = urls.slice(0, 2);
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

// Function to get tab URL for filtering purposes
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
      // Get tab URL for filtering
      const tabUrl = await getTabUrl(details.tabId);
      await addMediaUrlForTab(details.tabId, url, tabUrl);
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

// Improved tab navigation detection
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
      // Allow popup to manually clear URLs for current tab
      const key = `tab_${request.tabId}_media`;
      chrome.storage.session.remove(key);
      updateBadge(request.tabId, 0);
      sendResponse({ success: true });
      return true;
  }
});