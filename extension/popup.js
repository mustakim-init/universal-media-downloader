// popup.js - Enhanced with intelligent error handling and retry logic

// --- Constants ---
const FLASK_PORT = 5000;
const FLASK_BASE_URL = `http://localhost:${FLASK_PORT}`;

// Enhanced streaming patterns with better detection
const STREAMING_PATTERNS = {
  youtube: /(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/embed\/)/,
  facebook: /facebook\.com\/(?:watch|.*\/videos\/|reel\/|.*\/posts\/)/,
  instagram: /instagram\.com\/(?:p|reel|tv)\//,
  tiktok: /tiktok\.com\/@.*\/video/,
  twitter: /(?:twitter\.com|x\.com)\/.*\/status\//,
  vimeo: /vimeo\.com\/\d+/,
  dailymotion: /dailymotion\.com\/video\//,
  twitch: /twitch\.tv\/videos\//
};

// --- DOM Elements ---
const urlInput = document.getElementById('urlInput');
const urlDropdown = document.getElementById('urlDropdown');
const dropdownArrow = document.getElementById('dropdownArrow');
const detectionStatus = document.getElementById('detectionStatus');
const mediaTypeRadios = document.querySelectorAll('input[name="mediaType"]');
const downloadHighestQualityBtn = document.getElementById('downloadHighestQualityBtn');
const getFormatsBtn = document.getElementById('getFormatsBtn');
const startDownloadBtn = document.getElementById('startDownloadBtn');
const backToInitialViewBtn = document.getElementById('backToInitialViewBtn');
const statusMessageDiv = document.getElementById('statusMessage');
const loadingSpinner = document.getElementById('loadingSpinner');
const initialView = document.getElementById('initialView');
const formatsSection = document.getElementById('formatsSection');
const videoFormatsList = document.getElementById('videoFormatsList');
const audioFormatsList = document.getElementById('audioFormatsList');

// --- State variables ---
let appConnected = false;
let detectedUrls = [];
let detectedMediaInfo = [];
let currentPageUrl = '';
let availableFormats = { video: [], audio: [] };
let selectedFormat = null;
let currentTabId = null;
let lastUrlAnalysis = null;
let retryCount = 0;
const MAX_RETRIES = 2;

// --- Enhanced Utility Functions ---
/**
 * Injects a content script into the current tab to find the permalink of the most
 * relevant video. It prioritizes videos currently in the viewport.
 * @returns {Promise<string>} A promise that resolves to the best found video URL, 
 * or the main page URL as a fallback.
 */
function findVideoPermalink() {
  return new Promise(async (resolve) => {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });

    // This function will be executed in the context of the web page
    const scraper = () => {
      // Find all video elements on the page
      const videos = Array.from(document.querySelectorAll('video'));
      let bestLink = null;

      // Find the video that is most visible in the viewport
      let maxVisibility = 0;
      let mostVisibleVideo = null;

      for (const video of videos) {
        const rect = video.getBoundingClientRect();
        const viewportHeight = window.innerHeight || document.documentElement.clientHeight;
        const viewportWidth = window.innerWidth || document.documentElement.clientWidth;

        // Calculate the area of the video visible in the viewport
        const visibleHeight = Math.max(0, Math.min(rect.bottom, viewportHeight) - Math.max(rect.top, 0));
        const visibleWidth = Math.max(0, Math.min(rect.right, viewportWidth) - Math.max(rect.left, 0));
        const visibleArea = visibleHeight * visibleWidth;
        
        if (visibleArea > maxVisibility) {
          maxVisibility = visibleArea;
          mostVisibleVideo = video;
        }
      }

      if (mostVisibleVideo) {
        // Search for a permalink by traversing up the DOM from the video element
        let parent = mostVisibleVideo.closest('div[role="article"], div[data-visualcompletion="video-player-container"], body');
        if (parent) {
          // Look for links that are likely permalinks (e.g., timestamps, share links)
          const permalinks = parent.querySelectorAll('a[href*="/videos/"], a[href*="/watch/"], a[href*="/reel/"]');
          if (permalinks.length > 0) {
             // Find the link with the most specific path
            bestLink = Array.from(permalinks).sort((a, b) => b.href.length - a.href.length)[0].href;
          }
        }
      }
      
      return bestLink;
    };

    chrome.scripting.executeScript({
      target: { tabId: tab.id },
      function: scraper
    }, (injectionResults) => {
      if (chrome.runtime.lastError || !injectionResults || injectionResults.length === 0) {
        console.warn("Content script injection failed or returned no results. Falling back to page URL.");
        resolve(tab.url); // Fallback to the main page URL
        return;
      }
      
      const foundUrl = injectionResults[0].result;
      if (foundUrl) {
        console.log("Found video permalink:", foundUrl);
        resolve(foundUrl);
      } else {
        console.warn("No specific video permalink found on page. Falling back to page URL.");
        resolve(tab.url); // Fallback to the main page URL
      }
    });
  });
}


function showLoading(show) {
  loadingSpinner.classList.toggle('hidden', !show);
}

function showStatus(message, type = 'info', autohide = true, duration = 5000) {
  statusMessageDiv.textContent = message;
  statusMessageDiv.className = `status-message ${type} visible`;
  
  if (autohide) {
    setTimeout(() => {
      statusMessageDiv.classList.remove('visible');
      setTimeout(() => statusMessageDiv.classList.add('hidden'), 300);
    }, duration);
  }
}

function setUIState(isFormatsView, isConnected) {
  if (isFormatsView) {
    initialView.classList.add('hidden');
    formatsSection.classList.add('active');
  } else {
    initialView.classList.remove('hidden');
    formatsSection.classList.remove('active');
  }

  const disableButtons = !isConnected || !urlInput.value;
  downloadHighestQualityBtn.disabled = disableButtons;
  getFormatsBtn.disabled = disableButtons;
  startDownloadBtn.disabled = disableButtons || !selectedFormat;
  urlInput.disabled = !isConnected;
}

function detectPlatform(url) {
  for (const [platform, pattern] of Object.entries(STREAMING_PATTERNS)) {
    if (pattern.test(url)) {
      return platform;
    }
  }
  return null;
}

function isTemporaryUrl(url) {
  const tempPatterns = [
    /fbcdn\.net/i,
    /instagram.*cdn/i,
    /googlevideo\.com\/videoplayback/i,
    /blob:/i,
    /\.m3u8(\?|$)/i,
    /\.mpd(\?|$)/i,
    /videodelivery\.net/i,
    /cloudfront\.net/i
  ];
  return tempPatterns.some(pattern => pattern.test(url));
}

function formatMediaInfo(info) {
  let label = info.url.length > 50 ? info.url.substring(0, 47) + '...' : info.url;
  
  if (info.type && info.type !== 'unknown') {
    label = `[${info.type.toUpperCase()}] ${label}`;
  }
  
  if (info.isTemporary) {
    label = `📡 ${label}`;
  }
  
  if (info.platform) {
    label = `[${info.platform.toUpperCase()}] ${label}`;
  }
  
  return label;
}

// Enhanced cookie retrieval with better error handling
async function getCookiesForUrl(url) {
  try {
    const urlObj = new URL(url);
    const domain = urlObj.hostname;
    const baseDomain = domain.replace('www.', '');
    
    // Get cookies for exact domain
    const exactCookies = await chrome.cookies.getAll({ domain: baseDomain });
    
    // Get cookies for parent domain
    const parentDomain = '.' + baseDomain.split('.').slice(-2).join('.');
    const parentCookies = await chrome.cookies.getAll({ domain: parentDomain });
    
    // Get cookies for www variant
    const wwwCookies = await chrome.cookies.getAll({ domain: 'www.' + baseDomain });
    
    // Combine and deduplicate
    const allCookies = [...exactCookies, ...parentCookies, ...wwwCookies];
    const uniqueCookies = Array.from(
      new Map(allCookies.map(c => [`${c.name}-${c.domain}-${c.path}`, c])).values()
    );
    
    // Filter out expired cookies
    const now = Date.now() / 1000;
    const validCookies = uniqueCookies.filter(cookie => {
      return !cookie.expirationDate || cookie.expirationDate > now;
    });
    
    console.log(`Found ${validCookies.length} valid cookies for ${url}`);
    return validCookies;
  } catch (error) {
    console.error("Error fetching cookies:", error);
    return [];
  }
}

async function getCurrentTab() {
  let queryOptions = { active: true, lastFocusedWindow: true };
  let [tab] = await chrome.tabs.query(queryOptions);
  return tab;
}

// Enhanced format rendering with better categorization
function renderFormats() {
  videoFormatsList.innerHTML = '';
  audioFormatsList.innerHTML = '';

  const createFormatsList = (formats, listElement, type) => {
    if (!formats || formats.length === 0) {
      listElement.innerHTML = `<li style="text-align: center; color: #868e96; padding: 20px;">
        No ${type} formats available<br>
        <small>Try the "Download Highest Quality" option instead</small>
      </li>`;
      return;
    }

    // Group formats by quality for better organization
    const groupedFormats = {
      best: formats.filter(f => f.quality === 'best' || f.note.includes('best')),
      standard: formats.filter(f => f.quality === 'standard' && !f.note.includes('best') && !f.note.includes('worst')),
      worst: formats.filter(f => f.quality === 'worst' || f.note.includes('worst'))
    };

    // Render each group
    Object.entries(groupedFormats).forEach(([qualityGroup, groupFormats]) => {
      if (groupFormats.length === 0) return;
      
      // Add quality group header
      if (formats.length > 5) { // Only show groups if there are many formats
        const groupHeader = document.createElement('li');
        groupHeader.style.cssText = 'font-weight: bold; color: var(--primary); background: transparent; border: none; cursor: default; padding: 5px 8px; font-size: 12px;';
        groupHeader.textContent = qualityGroup.toUpperCase() + ` (${groupFormats.length})`;
        listElement.appendChild(groupHeader);
      }

      groupFormats.forEach(format => {
        const li = document.createElement('li');
        
        // Enhanced format display
        let displayText = `${format.id}`;
        if (format.ext) displayText += ` • ${format.ext.toUpperCase()}`;
        
        if (format.resolution && format.resolution !== 'audio only' && format.resolution !== 'unknown') {
          displayText += ` • ${format.resolution}`;
        }
        
        if (format.note) {
          let note = format.note.replace(/\s+/g, ' ').trim();
          if (note.length > 40) note = note.substring(0, 37) + '...';
          displayText += ` • ${note}`;
        }
        
        li.textContent = displayText;
        li.dataset.formatId = format.id;
        li.title = `Format ID: ${format.id}\nResolution: ${format.resolution}\nType: ${format.type}\n${format.note || 'No additional info'}`;
        
        // Add quality indicator styling
        if (format.quality === 'best') {
          li.style.borderLeft = '3px solid var(--success)';
        } else if (format.quality === 'worst') {
          li.style.borderLeft = '3px solid var(--warning)';
        }
        
        li.addEventListener('click', () => {
          document.querySelectorAll('#formatsContainer li').forEach(item => {
            item.classList.remove('selected');
          });
          li.classList.add('selected');
          selectedFormat = format.id;
          startDownloadBtn.disabled = false;
        });
        
        listElement.appendChild(li);
      });
    });
  };

  // Add video formats with header
  if (availableFormats.video && availableFormats.video.length > 0) {
    const videoTitle = document.createElement('h4');
    videoTitle.textContent = `Video Formats (${availableFormats.video.length})`;
    videoFormatsList.appendChild(videoTitle);
    createFormatsList(availableFormats.video, videoFormatsList, 'video');
  }
  
  // Add audio formats with header
  if (availableFormats.audio && availableFormats.audio.length > 0) {
    const audioTitle = document.createElement('h4');
    audioTitle.textContent = `Audio Formats (${availableFormats.audio.length})`;
    audioFormatsList.appendChild(audioTitle);
    createFormatsList(availableFormats.audio, audioFormatsList, 'audio');
  }

  // Show helpful message if no formats found
  if ((!availableFormats.video || availableFormats.video.length === 0) && 
      (!availableFormats.audio || availableFormats.audio.length === 0)) {
    const noFormatsMsg = document.createElement('div');
    noFormatsMsg.style.cssText = 'text-align: center; color: var(--warning); padding: 20px; background: rgba(255, 193, 7, 0.1); border-radius: 8px; margin: 10px;';
    noFormatsMsg.innerHTML = `
      <strong>No formats detected</strong><br>
      <small>Try using "Download Highest Quality" instead</small>
    `;
    videoFormatsList.appendChild(noFormatsMsg);
  }
}

// Enhanced error handling with retry logic
async function makeRequestWithRetry(url, options, attempt = 1) {
  try {
    const response = await fetch(url, options);
    const data = await response.json();
    
    if (!response.ok) {
      throw new Error(data.error || `HTTP ${response.status}: ${response.statusText}`);
    }
    
    return { response, data };
  } catch (error) {
    console.error(`Request attempt ${attempt} failed:`, error);
    
    if (attempt < MAX_RETRIES && (
      error.message.includes('403') || 
      error.message.includes('timeout') ||
      error.message.includes('network')
    )) {
      showStatus(`Request failed, retrying... (${attempt}/${MAX_RETRIES})`, 'warning', false);
      await new Promise(resolve => setTimeout(resolve, 1000 * attempt)); // Exponential backoff
      return makeRequestWithRetry(url, options, attempt + 1);
    }
    
    throw error;
  }
}

// --- Event Handlers ---
urlInput.addEventListener('input', () => {
  const hasValue = urlInput.value.trim().length > 0;
  downloadHighestQualityBtn.disabled = !appConnected || !hasValue;
  getFormatsBtn.disabled = !appConnected || !hasValue;
  
  // Reset retry count on URL change
  retryCount = 0;
  lastUrlAnalysis = null;
});

urlDropdown.addEventListener('click', (e) => {
  if (e.target.classList.contains('url-dropdown-item')) {
    const index = parseInt(e.target.dataset.index);
    if (!isNaN(index) && detectedUrls[index]) {
      urlInput.value = detectedUrls[index];
      urlDropdown.classList.add('hidden');
      
      // Trigger input event to update button states
      urlInput.dispatchEvent(new Event('input'));
    }
  }
});

dropdownArrow.addEventListener('click', () => {
  urlDropdown.classList.toggle('hidden');
  if (!urlDropdown.classList.contains('hidden')) {
    urlDropdown.innerHTML = '';
    
    if (detectedUrls.length === 0) {
      const noMedia = document.createElement('div');
      noMedia.className = 'url-dropdown-item';
      noMedia.style.cssText = 'text-align: center; color: #868e96; cursor: default;';
      noMedia.textContent = 'No media detected on this page';
      urlDropdown.appendChild(noMedia);
    } else {
      detectedUrls.forEach((url, index) => {
        const item = document.createElement('div');
        item.className = 'url-dropdown-item';
        item.dataset.index = index;
        
        // Use enhanced media info formatting
        if (detectedMediaInfo[index]) {
          item.textContent = formatMediaInfo(detectedMediaInfo[index]);
        } else {
          const platform = detectPlatform(url);
          item.textContent = platform ? `[${platform.toUpperCase()}] ${url}` : url;
        }
        
        item.title = url; // Show full URL on hover
        urlDropdown.appendChild(item);
      });
    }
  }
});

document.addEventListener('click', (event) => {
  if (!event.target.closest('.input-group')) {
    urlDropdown.classList.add('hidden');
  }
});

// Enhanced format fetching with better error handling
getFormatsBtn.addEventListener('click', async () => {
  showLoading(true);
  
  // *** NEW: First, find the specific video URL on the page ***
  const contextUrl = await findVideoPermalink();
  
  if (!contextUrl) {
    showStatus("Could not determine the video URL.", "error");
    showLoading(false);
    return;
  }
  console.log(`Using context URL for format fetch: ${contextUrl}`);

  const mediaType = document.querySelector('input[name="mediaType"]:checked').value;

  try {
    const { data: analysis } = await makeRequestWithRetry(`${FLASK_BASE_URL}/analyze_url`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: contextUrl })
    });
    lastUrlAnalysis = analysis;
    
    const platform = analysis.platform || 'Unknown';
    showStatus(`Detected ${platform} video. Fetching formats...`, 'info', false);

    let cookies = [];
    if (analysis.needs_cookies) {
      cookies = await getCookiesForUrl(currentPageUrl); // Cookies from the main domain
    }
    
    const { data } = await makeRequestWithRetry(`${FLASK_BASE_URL}/get_formats`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ url: contextUrl, mediaType, cookies })
    });

    const allFormats = data.formats || [];
    availableFormats.video = allFormats.filter(f => f.type === 'video');
    availableFormats.audio = allFormats.filter(f => f.type === 'audio');
    
    renderFormats();
    setUIState(true, true);
    showStatus(`Found ${allFormats.length} formats`, "success");

  } catch (error) {
    console.error('Format fetching error:', error);
    showStatus(error.message || "Failed to fetch formats.", 'error');
  } finally {
    showLoading(false);
  }
});

// Enhanced download with better progress tracking
downloadHighestQualityBtn.addEventListener('click', async () => {
  showLoading(true);
  
  // *** NEW: First, find the specific video URL on the page ***
  const contextUrl = await findVideoPermalink();

  if (!contextUrl) {
    showStatus("Could not determine the video URL.", "error");
    showLoading(false);
    return;
  }
  console.log(`Using context URL for download: ${contextUrl}`);
  
  const mediaType = document.querySelector('input[name="mediaType"]:checked').value;

  try {
    const { data: analysis } = await makeRequestWithRetry(`${FLASK_BASE_URL}/analyze_url`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: contextUrl })
    });
    lastUrlAnalysis = analysis;
    
    let cookies = [];
    if (analysis.needs_cookies) {
      cookies = await getCookiesForUrl(currentPageUrl); // Cookies from the main domain
    }
    
    const { data } = await makeRequestWithRetry(`${FLASK_BASE_URL}/download`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ 
        url: contextUrl, 
        media_type: mediaType, 
        format_id: 'highest', 
        cookies 
      })
    });

    showStatus(data.message || "Download started!", "success");

  } catch (error) {
    console.error('Download error:', error);
    showStatus(error.message || "Failed to start download.", 'error');
  } finally {
    showLoading(false);
  }
});

startDownloadBtn.addEventListener('click', async () => {
  if (!selectedFormat) {
    showStatus("Please select a format.", "error");
    return;
  }
  
  showLoading(true);

  // *** NEW: First, find the specific video URL on the page ***
  const contextUrl = await findVideoPermalink();

  if (!contextUrl) {
    showStatus("Could not determine the video URL.", "error");
    showLoading(false);
    return;
  }
  console.log(`Using context URL for format download: ${contextUrl}`);

  const mediaType = document.querySelector('input[name="mediaType"]:checked').value;

  try {
    let cookies = [];
    if (lastUrlAnalysis && lastUrlAnalysis.needs_cookies) {
      cookies = await getCookiesForUrl(currentPageUrl); // Cookies from the main domain
    }
    
    const { data } = await makeRequestWithRetry(`${FLASK_BASE_URL}/download`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ 
        url: contextUrl, 
        media_type: mediaType, 
        format_id: selectedFormat, 
        cookies 
      })
    });

    showStatus(data.message || "Download started!", "success");
    setUIState(false, appConnected);

  } catch (error) {
    console.error('Specific format download error:', error);
    showStatus(error.message || "Failed to start download.", 'error');
  } finally {
    showLoading(false);
  }
});

backToInitialViewBtn.addEventListener('click', () => {
  setUIState(false, appConnected);
  selectedFormat = null;
  startDownloadBtn.disabled = true;
});

// --- Smart URL Selection Logic ---
function selectBestUrl(mediaInfoList, pageUrl) {
  // If we're on a known streaming platform page, prefer that URL
  if (pageUrl && detectPlatform(pageUrl)) {
    return pageUrl;
  }
  
  // Sort media by enhanced priority
  const sorted = [...mediaInfoList].sort((a, b) => {
    // Prioritize known platforms
    const aPlatform = detectPlatform(a.url);
    const bPlatform = detectPlatform(b.url);
    if (aPlatform && !bPlatform) return -1;
    if (!aPlatform && bPlatform) return 1;
    
    // Prioritize non-temporary URLs
    if (!a.isTemporary && b.isTemporary) return -1;
    if (a.isTemporary && !b.isTemporary) return 1;
    
    // Prioritize video over audio
    if (a.type === 'video' && b.type !== 'video') return -1;
    if (a.type !== 'video' && b.type === 'video') return 1;
    
    // Prioritize larger files
    if (a.size && b.size) return b.size - a.size;
    
    return 0;
  });
  
  return sorted.length > 0 ? sorted[0].url : pageUrl;
}

// --- Enhanced Initialization Logic ---
async function init() {
  showLoading(true);

  // Check app connection with retry
  try {
    const { data: healthData } = await makeRequestWithRetry(`${FLASK_BASE_URL}/health`, {
      method: 'GET'
    });
    
    appConnected = true;
    showStatus(`Connected to Enhanced Media Downloader v${healthData.version || '2.1'}`, "success");
  } catch (error) {
    appConnected = false;
    showStatus("Desktop app not running. Please start the application first.", "error", false);
    setUIState(false, false);
    showLoading(false);
    return;
  }

  // Get current tab ID
  try {
    const currentTab = await getCurrentTab();
    if (!currentTab || currentTab.id === undefined) {
      throw new Error("Could not access current tab");
    }
    currentTabId = currentTab.id;
  } catch (error) {
    showStatus("Could not access current tab information.", "error");
    showLoading(false);
    setUIState(false, appConnected);
    return;
  }
  
  // *** NEW AND IMPROVED LOGIC ***
  // Get all media and the definite page URL from the background script
  chrome.runtime.sendMessage({ type: 'get_media_urls', tabId: currentTabId }, (response) => {
    if (chrome.runtime.lastError) {
      console.error(chrome.runtime.lastError.message);
      showStatus("Error communicating with background script.", "error");
      showLoading(false);
      return;
    }
    
    detectedUrls = response?.urls || [];
    detectedMediaInfo = response?.mediaInfo || [];
    // *** CRITICAL: Set currentPageUrl from the reliable background script response ***
    currentPageUrl = response?.pageUrl || '';

    // Check for restricted URLs
    const isRestrictedUrl = !currentPageUrl || 
      currentPageUrl.startsWith('chrome://') || 
      currentPageUrl.startsWith('edge://') || 
      currentPageUrl.startsWith('about:') ||
      currentPageUrl.startsWith('chrome-extension://') ||
      currentPageUrl.startsWith('moz-extension://');

    if (isRestrictedUrl) {
      urlInput.value = '';
      detectionStatus.textContent = "Cannot access browser internal pages. Please navigate to a media site.";
      detectionStatus.classList.remove('hidden');
      showLoading(false);
      setUIState(false, appConnected);
      return;
    }

    // Now, proceed with the UI logic using the correct currentPageUrl
    const platform = detectPlatform(currentPageUrl);
    
    if (platform) {
      urlInput.value = currentPageUrl;
      detectionStatus.innerHTML = `
        <span style="color: var(--success);">📱 ${platform.charAt(0).toUpperCase() + platform.slice(1)} page detected</span><br>
        <small>Ready for enhanced ${platform} download</small>
      `;
      detectionStatus.classList.remove('hidden');
    } else if (detectedUrls.length > 0) {
      const bestUrl = selectBestUrl(detectedMediaInfo, currentPageUrl);
      urlInput.value = bestUrl;
      
      let statusText = `${detectedUrls.length} media file${detectedUrls.length !== 1 ? 's' : ''} detected`;
      detectionStatus.innerHTML = `<span style="color: var(--primary);">🎯 ${statusText}</span>`;
      detectionStatus.classList.remove('hidden');
      dropdownArrow.style.opacity = '1';
      dropdownArrow.title = `${detectedUrls.length} media file${detectedUrls.length !== 1 ? 's' : ''} detected`;
    } else {
      urlInput.value = currentPageUrl;
      detectionStatus.innerHTML = `
        <span style="color: var(--warning);">⚠️ No media auto-detected</span><br>
        <small>You can still try to download from this page URL directly.</small>
      `;
      detectionStatus.classList.remove('hidden');
      dropdownArrow.style.opacity = '0.3';
      dropdownArrow.title = 'No media detected on this page';
    }
    
    showLoading(false);
    setUIState(false, appConnected);
  });
}

// Enhanced error recovery
window.addEventListener('error', (event) => {
  console.error('Popup error:', event.error);
  showStatus('An unexpected error occurred. Try refreshing the page.', 'error');
});

window.addEventListener('unhandledrejection', (event) => {
  console.error('Unhandled promise rejection:', event.reason);
  showStatus('Connection error. Check if the desktop app is running.', 'error');
});

// Initialize when popup opens
init();