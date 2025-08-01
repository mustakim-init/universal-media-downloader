// popup.js - REDESIGNED UI & IMPROVED DETECTION with Dropdown

const FLASK_PORT = 5000;
const FLASK_BASE_URL = `http://localhost:${FLASK_PORT}`;

// Get DOM elements
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
const appConnectionStatusDiv = document.getElementById('appConnectionStatus');
const connectionText = document.getElementById('connectionText');
const retryIcon = document.getElementById('retryIcon');

let selectedFormatId = null;
let currentTabId = null;
let isAppConnected = false;
let detectedUrls = [];
let isDropdownOpen = false;

// Function to clean URLs (remove tracking parameters)
function cleanUrl(url) {
    try {
        const urlObj = new URL(url);
        ['utm_source', 'utm_medium', 'fbclid', 'gclid', 'feature'].forEach(param => {
            urlObj.searchParams.delete(param);
        });
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

// --- UI State Management Functions ---
function showStatus(message, type = 'info', timeout = 5000) {
    statusMessageDiv.textContent = message;
    statusMessageDiv.className = `status-message status-${type}`;
    statusMessageDiv.classList.remove('hidden');
    if (timeout > 0) {
        setTimeout(() => {
            statusMessageDiv.classList.add('hidden');
        }, timeout);
    }
}

function showLoading(show) {
    loadingSpinner.classList.toggle('hidden', !show);
}

function setUIState(isLoading, appConnected = isAppConnected) {
    isAppConnected = appConnected;
    
    urlInput.disabled = isLoading;
    mediaTypeRadios.forEach(radio => radio.disabled = isLoading);
    
    const hasUrlInInput = urlInput.value.trim() !== '';

    downloadHighestQualityBtn.disabled = isLoading || !appConnected || !hasUrlInInput;
    getFormatsBtn.disabled = isLoading || !appConnected || !hasUrlInInput;
    startDownloadBtn.disabled = isLoading || !appConnected || selectedFormatId === null;
    backToInitialViewBtn.disabled = isLoading;

    // Update app connection status display
    if (appConnected) {
        connectionText.textContent = "Desktop App Connected";
        appConnectionStatusDiv.className = "connected";
        retryIcon.classList.add('hidden');
    } else {
        connectionText.textContent = "Desktop App Disconnected";
        appConnectionStatusDiv.className = "disconnected";
        retryIcon.classList.remove('hidden');
    }
    appConnectionStatusDiv.classList.remove('hidden');
}

function showInitialView() {
    initialView.classList.remove('hidden');
    formatsSection.classList.add('hidden');
    videoFormatsList.innerHTML = '';
    audioFormatsList.innerHTML = '';
    selectedFormatId = null;
    setUIState(false);
}

function showFormatsView() {
    initialView.classList.add('hidden');
    formatsSection.classList.remove('hidden');
    setUIState(false);
}

// --- Dropdown Functions ---
function toggleDropdown() {
    isDropdownOpen = !isDropdownOpen;
    urlDropdown.classList.toggle('hidden', !isDropdownOpen);
    dropdownArrow.classList.toggle('open', isDropdownOpen);
    
    if (isDropdownOpen) {
        populateDropdown();
    }
}

function closeDropdown() {
    isDropdownOpen = false;
    urlDropdown.classList.add('hidden');
    dropdownArrow.classList.remove('open');
}

function populateDropdown() {
    urlDropdown.innerHTML = '';
    
    // Add manual entry option
    const manualOption = document.createElement('div');
    manualOption.className = 'url-option manual-entry';
    manualOption.textContent = '✏️ Manual Entry (clear field)';
    manualOption.addEventListener('click', () => {
        urlInput.value = '';
        urlInput.focus();
        closeDropdown();
        setUIState(false);
    });
    urlDropdown.appendChild(manualOption);
    
    // Add detected URLs
    if (detectedUrls.length > 0) {
        detectedUrls.forEach((url, index) => {
            const option = document.createElement('div');
            option.className = 'url-option';
            
            // Truncate long URLs for display
            let displayUrl = url;
            if (url.length > 60) {
                displayUrl = url.substring(0, 30) + '...' + url.substring(url.length - 27);
            }
            
            option.textContent = `📺 ${displayUrl}`;
            option.title = url; // Full URL on hover
            option.addEventListener('click', () => {
                urlInput.value = url;
                closeDropdown();
                setUIState(false);
            });
            urlDropdown.appendChild(option);
        });
    } else {
        const noUrlsOption = document.createElement('div');
        noUrlsOption.className = 'url-option';
        noUrlsOption.style.opacity = '0.6';
        noUrlsOption.textContent = 'No URLs detected on this page';
        urlDropdown.appendChild(noUrlsOption);
    }
}

// Function to get the current tab's info
async function getCurrentTab() {
    return new Promise((resolve) => {
        if (chrome.tabs) {
            chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
                resolve(tabs && tabs[0] ? tabs[0] : null);
            });
        } else {
            resolve(null);
        }
    });
}

// Function to check desktop app connection with multiple endpoints
async function checkAppConnection() {
    const endpoints = ['/health', '/status', '/ping'];
    
    for (const endpoint of endpoints) {
        try {
            const response = await fetch(`${FLASK_BASE_URL}${endpoint}`, {
                method: 'GET',
                signal: AbortSignal.timeout(2000)
            });
            
            if (response.ok) {
                try {
                    const data = await response.json();
                    return data.status === 'healthy' || data.status === 'ok' || data.status === 'success';
                } catch {
                    return true;
                }
            }
        } catch (error) {
            continue;
        }
    }
    return false;
}

// Basic URL validation
function isValidUrl(string) {
    try {
        const url = new URL(string);
        return url.protocol === "http:" || url.protocol === "https:";
    } catch (_) {
        return false;
    }
}

// Setup format selection listeners
function setupFormatSelection() {
    document.querySelectorAll('input[name="format"]').forEach(radio => {
        radio.addEventListener('change', function() {
            selectedFormatId = this.value;
            setUIState(false);
        });
    });
}

// --- Event Handlers ---

// Dropdown arrow click
dropdownArrow.addEventListener('click', (e) => {
    e.stopPropagation();
    toggleDropdown();
});

// Close dropdown when clicking outside
document.addEventListener('click', (e) => {
    if (!e.target.closest('.input-group') && isDropdownOpen) {
        closeDropdown();
    }
});

// URL input handling
urlInput.addEventListener('input', () => {
    statusMessageDiv.classList.add('hidden');
    setUIState(false);
    if (isDropdownOpen) {
        closeDropdown();
    }
});

urlInput.addEventListener('keypress', (e) => {
    if (e.key === 'Enter') {
        getFormatsBtn.click();
    }
});

// Retry icon click
retryIcon.addEventListener('click', async () => {
    showLoading(true);
    setUIState(true, false);
    showStatus('Retrying connection to desktop app...', 'info', 0);
    await initializePopup();
});

getFormatsBtn.addEventListener('click', async () => {
    const url = urlInput.value.trim();
    if (!url) {
        showStatus('Please enter or select a media URL.', 'error');
        return;
    }
    if (!isValidUrl(url)) {
        showStatus('Invalid URL format. Please enter a valid http:// or https:// URL.', 'error');
        return;
    }

    const mediaType = document.querySelector('input[name="mediaType"]:checked').value;
    videoFormatsList.innerHTML = '';
    audioFormatsList.innerHTML = '';
    selectedFormatId = null;
    showLoading(true);
    setUIState(true);
    showStatus('Fetching available formats...', 'info');

    try {
        const response = await fetch(`${FLASK_BASE_URL}/formats`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url: url, media_type: mediaType }),
        });
        const data = await response.json();

        if (data.status === 'success') {
            const formats = data.formats;
            if (formats && formats.length > 0) {
                showFormatsView();
                showStatus("Formats loaded. Select a format to download.", "success");
                const list = mediaType === 'video' ? videoFormatsList : audioFormatsList;
                const headerText = mediaType === 'video' ? 'Video Formats' : 'Audio Formats';
                list.innerHTML = `<h3 class="section-title">${headerText}</h3>`;
                
                formats.forEach(format => {
                    const li = document.createElement("li");
                    li.innerHTML = `
                        <label>
                            <input type="radio" name="format" value="${format.id}" id="format-${format.id}">
                            ${format.label}
                        </label>`;
                    list.appendChild(li);
                });
                setupFormatSelection();
            } else {
                showStatus("No downloadable formats found for this URL. Try a different URL.", "info");
                showInitialView();
            }
        } else {
            showStatus(`Error from app: ${data.message}`, 'error');
            showInitialView();
        }
    } catch (error) {
        showStatus('Could not connect to desktop app. Please check if it\'s running and try again.', 'error');
        setUIState(false, false);
        showInitialView();
    } finally {
        showLoading(false);
        setUIState(false);
    }
});

downloadHighestQualityBtn.addEventListener('click', async () => {
    const url = urlInput.value.trim();
    if (!url) {
        showStatus('Please enter or select a media URL.', 'error');
        return;
    }
    if (!isValidUrl(url)) {
        showStatus('Invalid URL format. Please enter a valid http:// or https:// URL.', 'error');
        return;
    }

    const mediaType = document.querySelector('input[name="mediaType"]:checked').value;
    showLoading(true);
    setUIState(true);
    showStatus(`Initiating highest quality ${mediaType} download...`, 'info');

    try {
        const response = await fetch(`${FLASK_BASE_URL}/download`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url, media_type: mediaType, download_type: 'highest_quality' }),
        });
        const data = await response.json();

        if (data.status === 'success') {
            showStatus('Download initiated! Check desktop app for progress.', 'success');
        } else {
            if (data.message && data.message.includes("disabled")) {
                showStatus('Error: Browser monitoring is disabled in the desktop app.', 'error');
            } else {
                showStatus(`Download failed: ${data.message}`, 'error');
            }
        }
    } catch (error) {
        showStatus('Could not connect to desktop app. Please check if it\'s running and try again.', 'error');
        setUIState(false, false);
    } finally {
        showLoading(false);
        setUIState(false);
    }
});

startDownloadBtn.addEventListener('click', async () => {
    const url = urlInput.value.trim();
    if (!url) {
        showStatus('URL has been lost. Please go back and enter it again.', 'error');
        return;
    }
    if (selectedFormatId === null) {
        showStatus('Please select a format.', 'error');
        return;
    }
    if (!isValidUrl(url)) {
        showStatus('Invalid URL format. Please enter a valid http:// or https:// URL.', 'error');
        return;
    }

    const mediaType = document.querySelector('input[name="mediaType"]:checked').value;
    showLoading(true);
    setUIState(true);
    showStatus(`Initiating download for selected format...`, 'info');

    try {
        const response = await fetch(`${FLASK_BASE_URL}/download`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url, format_id: selectedFormatId, media_type: mediaType, download_type: 'specific_format' }),
        });
        const data = await response.json();

        if (data.status === 'success') {
            showStatus('Download initiated! Check desktop app for progress.', 'success');
            showInitialView();
        } else {
            showStatus(`Download failed: ${data.message}`, 'error');
        }
    } catch (error) {
        showStatus('Could not connect to desktop app. Please check if it\'s running and try again.', 'error');
        setUIState(false, false);
    } finally {
        showLoading(false);
        setUIState(false);
    }
});

backToInitialViewBtn.addEventListener('click', showInitialView);

// Main initialization function
async function initializePopup() {
    showLoading(true);
    setUIState(true, false);

    const appConnected = await checkAppConnection();
    setUIState(false, appConnected);

    if (!appConnected) {
        showStatus('Desktop application is not running. Please start the desktop app to use this extension.', 'error', 0);
        urlInput.value = '';
        detectedUrls = [];
        detectionStatus.textContent = "Cannot detect URLs. Desktop app is not running.";
        detectionStatus.classList.remove('hidden');
        showLoading(false);
        return;
    }

    const currentTab = await getCurrentTab();
    if (!currentTab) {
        detectionStatus.textContent = "Could not get current tab info.";
        detectionStatus.classList.remove('hidden');
        showLoading(false);
        setUIState(true, appConnected);
        return;
    }
    currentTabId = currentTab.id;

    // Fetch detected URLs and check current tab URL
    chrome.runtime.sendMessage({ type: 'get_media_urls', tabId: currentTab.id }, async (response) => {
        detectedUrls = response.urls || [];
        
        // For Facebook, also add the current tab URL if it's a video watch page
        const currentTabUrlCleaned = cleanUrl(currentTab.url);
        if (isValidUrl(currentTabUrlCleaned)) {
            const isCurrentTabStreaming = await new Promise(resolve => {
                chrome.runtime.sendMessage({ type: 'is_streaming_url', url: currentTabUrlCleaned }, (res) => {
                    resolve(res ? res.isStreaming : false);
                });
            });

            if (isCurrentTabStreaming && !detectedUrls.includes(currentTabUrlCleaned)) {
                detectedUrls.unshift(currentTabUrlCleaned);
            }
        }

        // Set default URL and update status
        if (detectedUrls.length > 0) {
            urlInput.value = detectedUrls[0];
            detectionStatus.classList.add('hidden');
        } else {
            urlInput.value = '';
            detectionStatus.textContent = "No media streams detected on this page. Click the dropdown to manually enter a URL.";
            detectionStatus.classList.remove('hidden');
        }

        showLoading(false);
        setUIState(false, appConnected);
    });
}

// Initial setup on DOMContentLoaded
document.addEventListener('DOMContentLoaded', initializePopup);