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
  const url = urlInput.value.trim();
  const mediaType = document.querySelector('input[name="mediaType"]:checked').value;
  
  if (!url) {
    showStatus("Please enter a URL.", "error");
    return;
  }

  showLoading(true);
  retryCount = 0;

  try {
    // First analyze the URL for better context
    showStatus("Analyzing URL and platform...", 'info', false);
    
    const { data: analysis } = await makeRequestWithRetry(`${FLASK_BASE_URL}/analyze_url`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ url })
    });
    
    lastUrlAnalysis = analysis;
    console.log('Enhanced URL Analysis:', analysis);
    
    // Show platform-specific status
    const platform = analysis.platform || 'Unknown';
    showStatus(`Detected ${platform} content. Fetching formats...`, 'info', false);
    
    // Get cookies with enhanced filtering
    let cookies = [];
    if (analysis.needs_cookies) {
      cookies = await getCookiesForUrl(url);
      console.log(`Retrieved ${cookies.length} cookies for ${platform}`);
      
      if (cookies.length === 0) {
        showStatus(`Warning: ${platform} content may need authentication. Try logging in first.`, 'warning', false);
      }
    }
    
    // Fetch formats with retry logic
    const { data } = await makeRequestWithRetry(`${FLASK_BASE_URL}/get_formats`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ url, mediaType, cookies })
    });

    // Process and categorize formats
    const allFormats = data.formats || [];
    availableFormats = { 
      video: allFormats.filter(f => f.type === 'video' || (f.resolution && f.resolution !== 'audio only')), 
      audio: allFormats.filter(f => f.type === 'audio' || f.resolution === 'audio only') 
    };
    
    renderFormats();
    setUIState(true, true);
    
    // Enhanced success message
    const formatCount = allFormats.length;
    const platformInfo = data.platform ? ` from ${data.platform}` : '';
    const cookieInfo = data.used_cookies ? ' (using authentication)' : '';
    const methodInfo = data.approach ? ` via ${data.approach}` : '';
    
    showStatus(
      `Found ${formatCount} format${formatCount !== 1 ? 's' : ''}${platformInfo}${cookieInfo}${methodInfo}`, 
      "success"
    );

  } catch (error) {
    console.error('Format fetching error:', error);
    
    // Enhanced error messages based on error type
    let errorMessage = "Failed to fetch formats.";
    let errorType = "error";
    let suggestions = [];
    
    if (error.message.includes('403') || error.message.includes('forbidden')) {
      errorMessage = "Access denied - content may be private or require authentication.";
      suggestions.push("Try logging into the platform first");
      suggestions.push("Check if the content is publicly accessible");
    } else if (error.message.includes('timeout')) {
      errorMessage = "Request timed out - server may be slow.";
      errorType = "warning";
      suggestions.push("Try again in a moment");
    } else if (error.message.includes('network') || error.message.includes('connection')) {
      errorMessage = "Network error - check your internet connection.";
      errorType = "warning";
    } else if (error.message.includes('unsupported')) {
      errorMessage = "This URL format is not supported.";
    } else {
      // Try to extract more specific error from server response
      const serverError = error.message.match(/error":\s*"([^"]+)"/);
      if (serverError) {
        errorMessage = serverError[1];
      }
    }
    
    showStatus(errorMessage, errorType);
    
    // Show suggestions if any
    if (suggestions.length > 0) {
      setTimeout(() => {
        showStatus(`💡 Suggestions: ${suggestions.join(' • ')}`, 'info', true, 8000);
      }, 2000);
    }
    
  } finally {
    showLoading(false);
  }
});

// Enhanced download with better progress tracking
downloadHighestQualityBtn.addEventListener('click', async () => {
  const url = urlInput.value.trim();
  const mediaType = document.querySelector('input[name="mediaType"]:checked').value;

  if (!url) {
    showStatus("Please enter a URL.", "error");
    return;
  }

  showLoading(true);

  try {
    // Use cached analysis if available, otherwise analyze
    let analysis = lastUrlAnalysis;
    if (!analysis) {
      showStatus("Analyzing URL...", 'info', false);
      const { data } = await makeRequestWithRetry(`${FLASK_BASE_URL}/analyze_url`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ url })
      });
      analysis = data;
    }
    
    const platform = analysis.platform || 'Unknown';
    showStatus(`Preparing ${mediaType} download from ${platform}...`, 'info', false);
    
    // Get cookies with enhanced error handling
    let cookies = [];
    if (analysis.needs_cookies) {
      cookies = await getCookiesForUrl(url);
      console.log(`Using ${cookies.length} cookies for ${platform} download`);
      
      if (cookies.length === 0) {
        showStatus(`Warning: No cookies found for ${platform}. Download may fail for private content.`, 'warning', true, 3000);
      }
    }
    
    // Start download with retry capability
    const { data } = await makeRequestWithRetry(`${FLASK_BASE_URL}/download`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ 
        url, 
        media_type: mediaType, 
        format_id: 'highest', 
        cookies 
      })
    });

    // Enhanced success message
    const platformInfo = data.platform ? ` ${data.platform}` : '';
    const authInfo = data.url_analysis?.used_enhanced_cookies ? ' (authenticated)' : '';
    showStatus(`${platformInfo} download started successfully!${authInfo}`, "success");
    
    // Show additional info for temporary URLs
    if (data.url_analysis?.is_temporary) {
      setTimeout(() => {
        showStatus("ℹ️ Using temporary URL with enhanced cookie handling", "info", true, 6000);
      }, 2000);
    }

  } catch (error) {
    console.error('Download error:', error);
    
    // Enhanced error handling for downloads
    let errorMessage = "Failed to start download.";
    
    if (error.message.includes('403') || error.message.includes('forbidden')) {
      errorMessage = "Download blocked - content may require authentication or be geo-restricted.";
    } else if (error.message.includes('private')) {
      errorMessage = "Cannot download private content. Please ensure you have access.";
    } else if (error.message.includes('unsupported')) {
      errorMessage = "This content type is not supported for download.";
    } else {
      const serverError = error.message.match(/error":\s*"([^"]+)"/);
      if (serverError) {
        errorMessage = serverError[1];
      }
    }
    
    showStatus(errorMessage, 'error');
    
  } finally {
    showLoading(false);
  }
});

startDownloadBtn.addEventListener('click', async () => {
  const url = urlInput.value.trim();
  const mediaType = document.querySelector('input[name="mediaType"]:checked').value;
  
  if (!selectedFormat) {
    showStatus("Please select a format.", "error");
    return;
  }

  showLoading(true);

  try {
    // Use cached analysis
    const analysis = lastUrlAnalysis || {};
    const platform = analysis.platform || 'Unknown';
    
    showStatus(`Starting ${platform} download with format ${selectedFormat}...`, 'info', false);

    // Get cookies if needed
    let cookies = [];
    if (analysis.needs_cookies) {
      cookies = await getCookiesForUrl(url);
    }
    
    const { data } = await makeRequestWithRetry(`${FLASK_BASE_URL}/download`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ 
        url, 
        media_type: mediaType, 
        format_id: selectedFormat, 
        cookies 
      })
    });

    showStatus(`Format ${selectedFormat} download started successfully!`, "success");
    
    // Reset UI state
    selectedFormat = null;
    setUIState(false, appConnected);
    
  } catch (error) {
    console.error('Specific format download error:', error);
    
    let errorMessage = "Failed to start download with selected format.";
    const serverError = error.message.match(/error":\s*"([^"]+)"/);
    if (serverError) {
      errorMessage = serverError[1];
    }
    
    showStatus(errorMessage, 'error');
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

  // Get current tab with enhanced error handling
  try {
    const currentTab = await getCurrentTab();
    if (!currentTab) {
      throw new Error("Could not access current tab");
    }
    
    currentTabId = currentTab.id;
    currentPageUrl = currentTab.url;
  } catch (error) {
    showStatus("Could not access current tab information.", "error");
    showLoading(false);
    setUIState(false, appConnected);
    return;
  }
  
  // Check for restricted URLs
  const isRestrictedUrl = !currentPageUrl || 
    currentPageUrl.startsWith('chrome://') || 
    currentPageUrl.startsWith('edge://') || 
    currentPageUrl.startsWith('about:') ||
    currentPageUrl.startsWith('chrome-extension://') ||
    currentPageUrl.startsWith('moz-extension://');

  if (isRestrictedUrl) {
    urlInput.value = '';
    detectionStatus.textContent = "Cannot access browser internal pages. Please navigate to a media site and paste URLs manually.";
    detectionStatus.classList.remove('hidden');
    showLoading(false);
    setUIState(false, appConnected);
    return;
  }

  // Get media URLs from background script with enhanced processing
  chrome.runtime.sendMessage({ type: 'get_media_urls', tabId: currentTabId }, (response) => {
    if (chrome.runtime.lastError) {
      console.error(chrome.runtime.lastError.message);
      detectedUrls = [];
      detectedMediaInfo = [];
    } else {
      detectedUrls = response?.urls || [];
      detectedMediaInfo = response?.mediaInfo || [];
      currentPageUrl = response?.pageUrl || currentPageUrl;
    }
    
    // Enhanced tab analysis
    chrome.runtime.sendMessage({ type: 'analyze_tab', tabId: currentTabId }, (analysis) => {
      console.log('Enhanced tab analysis:', analysis);
      
      // Determine what URL to show with enhanced logic
      const platform = detectPlatform(currentPageUrl);
      
      if (platform) {
        // We're on a streaming platform page
        urlInput.value = currentPageUrl;
        detectionStatus.innerHTML = `
          <span style="color: var(--success);">📱 ${platform.charAt(0).toUpperCase() + platform.slice(1)} page detected</span><br>
          <small>Ready for enhanced ${platform} download</small>
        `;
        detectionStatus.classList.remove('hidden');
      } else if (detectedUrls.length > 0) {
        // We have detected media
        const bestUrl = selectBestUrl(detectedMediaInfo, currentPageUrl);
        urlInput.value = bestUrl;
        
        let statusText = `${detectedUrls.length} media file${detectedUrls.length !== 1 ? 's' : ''} detected`;
        if (analysis?.hasTemporaryUrls) {
          statusText += ' • Enhanced cookie handling enabled';
        }
        if (analysis?.platforms && analysis.platforms.length > 0) {
          statusText += ` • Platforms: ${analysis.platforms.join(', ')}`;
        }
        
        detectionStatus.innerHTML = `
          <span style="color: var(--primary);">🎯 ${statusText}</span>
        `;
        detectionStatus.classList.remove('hidden');
        
        // Enable dropdown arrow with enhanced styling
        dropdownArrow.style.opacity = '1';
        dropdownArrow.title = `${detectedUrls.length} media file${detectedUrls.length !== 1 ? 's' : ''} detected`;
      } else {
        // No media detected, show current page URL with suggestions
        urlInput.value = currentPageUrl;
        detectionStatus.innerHTML = `
          <span style="color: var(--warning);">⚠️ No media auto-detected</span><br>
          <small>You can paste media URLs manually or try navigating to video pages</small>
        `;
        detectionStatus.classList.remove('hidden');
        
        // Disable dropdown arrow
        dropdownArrow.style.opacity = '0.3';
        dropdownArrow.title = 'No media detected on this page';
      }
      
      showLoading(false);
      setUIState(false, appConnected);
    });
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