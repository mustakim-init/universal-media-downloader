// HIGHLY RESTRICTIVE background.js - Only detect ACTUALLY PLAYABLE Facebook media

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
  /facebook\.com\/watch\?v=/,  // Only actual video watch pages
  /fb\.watch\//,
  /instagram\.com\/p\//,
  /instagram\.com\/reel\//,
  /tiktok\.com\/@.*\/video/
];

// COMPREHENSIVE ignore patterns - be very aggressive about filtering out non-playable content
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
  // Facebook specific ignore patterns - VERY RESTRICTIVE
  /facebook\.com.*rsrc\.php/i,
  /fbcdn\.net.*\.jpg/i,
  /fbcdn\.net.*\.png/i,
  /fbcdn\.net.*\.gif/i,
  /fbcdn\.net.*\.webp/i,
  /fbstatic/i,
  /static\.xx\.fbcdn\.net/i,
  /graph\.facebook\.com/i,
  /connect\.facebook\.net/i,
  /\/images?\//i,
  /\/img\//i,
  /\/static\//i,
  /\/assets?\//i,
  // Size and quality indicators that suggest thumbnails
  /s\d+x\d+/i,
  /\d+x\d+.*\.jpg/i,
  /\d+x\d+.*\.png/i,
  /small/i,
  /medium/i,
  /tiny/i,
  // Facebook specific non-video patterns
  /p\d+x\d+/i,
  /safe_image\.php/i,
  /external\.php/i
];

// VERY STRICT Facebook video URL patterns - only these will be considered valid
const FACEBOOK_PLAYABLE_PATTERNS = [
  // Direct HLS streams (most reliable for Facebook)
  /video-.*\.fbcdn\.net.*\.m3u8/i,
  /scontent.*\.fbcdn\.net.*\.m3u8/i,
  
  // High-quality direct MP4 files (very specific patterns)
  /video-[a-z0-9-]+\.fbcdn\.net.*\.mp4.*(?:hd|720|1080|high)/i,
  /scontent-[a-z0-9-]+\.xx\.fbcdn\.net.*\.mp4.*(?!.*thumb).*$/i,
  
  // Facebook's progressive download URLs (less common but valid)
  /fbcdn\.net.*\/v\/.*\.mp4/i
];

// FACEBOOK-SPECIFIC: Only accept URLs that match very strict criteria
function isValidFacebookMediaUrl(url, tabUrl) {
  // If not Facebook context, don't apply these strict rules
  if (!tabUrl || (!tabUrl.includes('facebook.com') && !tabUrl.includes('fb.watch'))) {
    return true;
  }

  // FIRST: Must match at least one playable pattern
  const matchesPlayablePattern = FACEBOOK_PLAYABLE_PATTERNS.some(pattern => pattern.test(url));
  if (!matchesPlayablePattern) {
    return false;
  }

  // SECOND: Additional strict checks
  if (url.includes('facebook.com') || url.includes('fbcdn.net')) {
    // Must be reasonably long (short URLs are usually thumbnails)
    if (url.length < 120) return false;
    
    // Must NOT contain any thumbnail indicators
    if (/thumb|preview|small|tiny|safe_image/i.test(url)) return false;
    
    // For MP4 files, must have quality indicators OR be from video subdomain
    if (url.includes('.mp4')) {
      const hasQualityIndicator = /(?:hd|720|1080|high|_hq)/i.test(url);
      const isVideoSubdomain = /video-.*\.fbcdn\.net/i.test(url);
      const isLongEnoughForReal = url.length > 150; // Real video URLs tend to be longer
      
      if (!hasQualityIndicator && !isVideoSubdomain && !isLongEnoughForReal) {
        return false;
      }
    }
    
    // For HLS streams, must be from proper video domains
    if (url.includes('.m3u8')) {
      const isFromVideoDomain = /(?:video-.*\.fbcdn\.net|scontent.*\.fbcdn\.net)/i.test(url);
      if (!isFromVideoDomain) return false;
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

// VERY RESTRICTIVE: Only accept URLs that pass ALL checks
function isRelevantMediaUrl(url, tabUrl) {
  // Basic ignore patterns - if it matches any, reject immediately
  if (shouldIgnoreUrl(url)) {
    return false;
  }
  
  // Facebook-specific validation - MUST pass this for Facebook URLs
  if (!isValidFacebookMediaUrl(url, tabUrl)) {
    return false;
  }
  
  // For streaming platforms, apply additional restrictions
  if (tabUrl && isStreamingUrl(tabUrl)) {
    const hasGoodExtension = MEDIA_FILE_EXTENSIONS.some(ext => 
      url.toLowerCase().includes(ext)
    );
    
    if (!hasGoodExtension) return false;
    
    // Facebook gets EXTRA strict treatment
    if (tabUrl.includes('facebook.com') || tabUrl.includes('fb.watch')) {
      // Must be a very long URL (real video URLs are complex)
      if (url.length < 150) return false;
      
      // Must contain specific video-related domains or patterns
      const hasValidDomain = /(?:video-.*\.fbcdn\.net|scontent-.*\.xx\.fbcdn\.net)/i.test(url);
      const hasHLS = url.includes('.m3u8');
      const hasHighQualityMP4 = url.includes('.mp4') && /(?:hd|720|1080|high)/i.test(url);
      
      if (!hasValidDomain && !hasHLS && !hasHighQualityMP4) {
        return false;
      }
      
      // Final check: must not contain any suspicious patterns
      if (/(?:thumb|preview|small|medium|tiny|safe_image|s\d+x\d+|p\d+x\d+)/i.test(url)) {
        return false;
      }
    }
    
    return true;
  }
  
  return true; // Allow all media for non-streaming sites
}

// Store found media URLs per tab - VERY LIMITED for Facebook
async function addMediaUrlForTab(tabId, url, tabUrl = null) {
  // Must pass relevance check
  if (!isRelevantMediaUrl(url, tabUrl)) {
    return;
  }
  
  const key = `tab_${tabId}_media`;
  const data = await chrome.storage.session.get(key);
  let urls = data[key] || [];
  
  // Clean URL
  const cleanedUrl = cleanUrl(url);
  
  // For Facebook, be EXTREMELY restrictive - only 1 URL maximum
  if (tabUrl && (tabUrl.includes('facebook.com') || tabUrl.includes('fb.watch'))) {
    if (!urls.includes(cleanedUrl)) {
      // For Facebook, only keep the MOST RECENT and LONGEST URL (most likely to be the real video)
      urls = [cleanedUrl]; // Replace everything with just this URL
      
      await chrome.storage.session.set({ [key]: urls });
      updateBadge(tabId, urls.length);
    }
  } 
  // For other streaming platforms, limit to 2 URLs
  else if (tabUrl && isStreamingUrl(tabUrl)) {
    if (!urls.includes(cleanedUrl)) {
      urls.unshift(cleanedUrl);
      if (urls.length > 2) {
        urls = urls.slice(0, 2);
      }
      await chrome.storage.session.set({ [key]: urls });
      updateBadge(tabId, urls.length);
    }
  } 
  // For non-streaming sites, keep original logic
  else {
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
        return url;
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

// Listen to network requests - IGNORE *.fbcdn.net URLs for Facebook
chrome.webRequest.onHeadersReceived.addListener(
  async (details) => {
    if (details.tabId < 0) return;

    const { url, responseHeaders } = details;
    let isMedia = false;

    // Get tab URL first for context
    const tabUrl = await getTabUrl(details.tabId);
    
    // For Facebook, COMPLETELY IGNORE *.fbcdn.net URLs - they are not playable
    if (tabUrl && (tabUrl.includes('facebook.com') || tabUrl.includes('fb.watch'))) {
      // Skip ALL fbcdn.net URLs - they are never the playable video URLs
      if (url.includes('fbcdn.net')) {
        return; // Completely ignore these URLs
      }
      
      // Only process the actual tab URL if it's a video watch page
      if (url === tabUrl && /facebook\.com\/watch\?v=|fb\.watch\//.test(url)) {
        isMedia = true; // The tab URL itself is the media URL for Facebook
      }
    }
    // For non-Facebook sites, use original logic
    else {
      // 1. Check by file extension
      for (const ext of MEDIA_FILE_EXTENSIONS) {
        if (url.toLowerCase().includes(ext)) {
          isMedia = true;
          break;
        }
      }

      // 2. Check by Content-Type header
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

      // 3. Check by streaming platform patterns
      if (!isMedia && isStreamingUrl(url)) {
        const hasMediaExt = MEDIA_FILE_EXTENSIONS.some(ext => 
          url.toLowerCase().includes(ext)
        );
        if (hasMediaExt) {
          isMedia = true;
        }
      }
    }

    if (isMedia) {
      await addMediaUrlForTab(details.tabId, url, tabUrl);
    }
  },
  { urls: ['<all_urls>'] },
  ['responseHeaders']
);

// Clear stored URLs when a tab is closed or navigates to a new page
chrome.tabs.onRemoved.addListener((tabId) => {
  chrome.storage.session.remove(`tab_${tabId}_media`);
  chrome.storage.session.remove(`tab_${tabId}_lastUrl`);
});

// Tab navigation detection
chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
    if (changeInfo.status === 'loading' && changeInfo.url) {
        chrome.storage.session.get(`tab_${tabId}_lastUrl`).then(data => {
            const lastUrl = data[`tab_${tabId}_lastUrl`];
            const newUrlBase = changeInfo.url.split('#')[0].split('?')[0];
            const lastUrlBase = lastUrl ? lastUrl.split('#')[0].split('?')[0] : '';
            
            if (newUrlBase !== lastUrlBase) {
                chrome.storage.session.remove(`tab_${tabId}_media`);
                updateBadge(tabId, 0);
            }
            
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
    return true;
  } else if (request.type === 'is_streaming_url') {
      sendResponse({ isStreaming: isStreamingUrl(request.url) });
      return true;
  } else if (request.type === 'clear_tab_urls') {
      const key = `tab_${request.tabId}_media`;
      chrome.storage.session.remove(key);
      updateBadge(request.tabId, 0);
      sendResponse({ success: true });
      return true;
  }
});