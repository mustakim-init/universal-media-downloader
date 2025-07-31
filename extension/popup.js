// popup.js - REDESIGNED UI & IMPROVED DETECTION

const FLASK_PORT = 5000;
const FLASK_BASE_URL = `http://localhost:${FLASK_PORT}`;

// Get DOM elements
const urlInput = document.getElementById('urlInput'); // Now a single input for detected/manual
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
const retryConnectionBtn = document.getElementById('retryConnectionBtn');

let selectedFormatId = null;
let currentTabId = null;
let isAppConnected = false; // Track connection status

// Function to clean URLs (remove tracking parameters) - moved here for consistency
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

// --- UI State Management Functions ---
function showStatus(message, type = 'info', timeout = 5000) {
    statusMessageDiv.textContent = message;
    statusMessageDiv.className = `status-message status-${type}`;
    statusMessageDiv.classList.remove('hidden');
    if (timeout > 0) { // Only set timeout if > 0
        setTimeout(() => {
            statusMessageDiv.classList.add('hidden');
        }, timeout);
    }
}

function showLoading(show) {
    loadingSpinner.classList.toggle('hidden', !show);
}

function setUIState(isLoading, appConnected = isAppConnected) {
    isAppConnected = appConnected; // Update global connection status
    
    urlInput.disabled = isLoading;
    mediaTypeRadios.forEach(radio => radio.disabled = isLoading);
    
    const hasUrlInInput = urlInput.value.trim() !== '';

    downloadHighestQualityBtn.disabled = isLoading || !appConnected || !hasUrlInInput;
    getFormatsBtn.disabled = isLoading || !appConnected || !hasUrlInInput;
    startDownloadBtn.disabled = isLoading || !appConnected || selectedFormatId === null;
    backToInitialViewBtn.disabled = isLoading;

    // Update app connection status display
    if (appConnected) {
        appConnectionStatusDiv.textContent = "Desktop App Connected";
        appConnectionStatusDiv.className = "connected";
        retryConnectionBtn.classList.add('hidden');
    } else {
        appConnectionStatusDiv.textContent = "Desktop App Disconnected";
        appConnectionStatusDiv.className = "disconnected";
        retryConnectionBtn.classList.remove('hidden');
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
    const endpoints = ['/health', '/status', '/ping']; // Try common endpoints
    
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
                    // If JSON parsing fails but response is ok, consider it connected
                    return true;
                }
            }
        } catch (error) {
            // console.warn(`Connection check failed for ${endpoint}:`, error); // Too verbose for console
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

// Unified URL input handling
urlInput.addEventListener('input', () => {
    // Clear status and re-evaluate buttons when user types
    statusMessageDiv.classList.add('hidden');
    setUIState(false);
});

urlInput.addEventListener('keypress', (e) => {
    if (e.key === 'Enter') {
        // When Enter is pressed in the URL input, act as if 'Get Formats' was clicked
        getFormatsBtn.click();
    }
});

retryConnectionBtn.addEventListener('click', async () => {
    showLoading(true);
    setUIState(true, false); // Show disconnected state while retrying
    showStatus('Retrying connection to desktop app...', 'info', 0); // Don't auto-hide
    await initializePopup(); // Re-run the main initialization logic
});


getFormatsBtn.addEventListener('click', async () => {
    const url = urlInput.value.trim(); // Get URL from the single input field
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
        setUIState(false, false); // Mark app as disconnected
        showInitialView();
    } finally {
        showLoading(false);
        setUIState(false);
    }
});

downloadHighestQualityBtn.addEventListener('click', async () => {
    const url = urlInput.value.trim(); // Get URL from the single input field
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
        setUIState(false, false); // Mark app as disconnected
    } finally {
        showLoading(false);
        setUIState(false);
    }
});

startDownloadBtn.addEventListener('click', async () => {
    const url = urlInput.value.trim(); // Get URL from the single input field
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
        setUIState(false, false); // Mark app as disconnected
    } finally {
        showLoading(false);
        setUIState(false);
    }
});

backToInitialViewBtn.addEventListener('click', showInitialView);

// Main initialization function to be called on DOMContentLoaded and by retry button
async function initializePopup() {
    showLoading(true);
    setUIState(true, false); // Start with UI disabled and app disconnected

    const appConnected = await checkAppConnection();
    setUIState(false, appConnected); // Update UI based on actual connection status

    if (!appConnected) {
        showStatus('Desktop application is not running. Please start the desktop app to use this extension.', 'error', 0); // Don't auto-hide
        urlInput.value = ''; // Clear input
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
        setUIState(true, appConnected); // Keep buttons disabled if no tab info
        return;
    }
    currentTabId = currentTab.id;

    // FIX: Fetch detected URLs AND check current tab URL sequentially
    chrome.runtime.sendMessage({ type: 'get_media_urls', tabId: currentTab.id }, async (response) => {
        let detectedUrls = response.urls || [];
        
        // Explicitly check and add current tab URL if it's a streaming URL
        const currentTabUrlCleaned = cleanUrl(currentTab.url);
        if (isValidUrl(currentTabUrlCleaned)) {
            const isCurrentTabStreaming = await new Promise(resolve => {
                chrome.runtime.sendMessage({ type: 'is_streaming_url', url: currentTabUrlCleaned }, (res) => {
                    resolve(res ? res.isStreaming : false);
                });
            });

            if (isCurrentTabStreaming && !detectedUrls.includes(currentTabUrlCleaned)) {
                detectedUrls.unshift(currentTabUrlCleaned); // Add to the beginning
            }
        }

        // Populate the single urlInput field
        if (detectedUrls.length > 0) {
            urlInput.value = detectedUrls[0]; // Set the first detected URL as the default
            detectionStatus.classList.add('hidden');
        } else {
            urlInput.value = ''; // Clear input if no URLs detected
            detectionStatus.textContent = "No media streams detected on this page. Paste a URL above.";
            detectionStatus.classList.remove('hidden');
        }

        showLoading(false);
        setUIState(false, appConnected); // Enable UI based on app connection and URL presence
    });
}

// Initial setup on DOMContentLoaded
document.addEventListener('DOMContentLoaded', initializePopup);
