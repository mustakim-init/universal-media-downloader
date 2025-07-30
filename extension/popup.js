// popup.js

const FLASK_PORT = 5000;
const FLASK_BASE_URL = `http://localhost:${FLASK_PORT}`;

// Get DOM elements
const urlSelect = document.getElementById('urlSelect');
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

let selectedFormatId = null;

// --- UI State Management Functions ---
function showStatus(message, type = 'info') {
    statusMessageDiv.textContent = message;
    statusMessageDiv.className = `status-message status-${type}`;
    statusMessageDiv.classList.remove('hidden');
    setTimeout(() => {
        statusMessageDiv.classList.add('hidden');
    }, 5000);
}

function showLoading(show) {
    loadingSpinner.classList.toggle('hidden', !show);
}

function setUIState(isLoading) {
    urlSelect.disabled = isLoading;
    mediaTypeRadios.forEach(radio => radio.disabled = isLoading);
    const hasUrl = urlSelect.value && urlSelect.options.length > 0;
    downloadHighestQualityBtn.disabled = isLoading || !hasUrl;
    getFormatsBtn.disabled = isLoading || !hasUrl;
    startDownloadBtn.disabled = isLoading || selectedFormatId === null;
    backToInitialViewBtn.disabled = isLoading;
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

getFormatsBtn.addEventListener('click', async () => {
    const url = urlSelect.value;
    if (!url) {
        showStatus('No media URL selected.', 'error');
        return;
    }
    const mediaType = document.querySelector('input[name="mediaType"]:checked').value;
    videoFormatsList.innerHTML = '';
    audioFormatsList.innerHTML = '';
    selectedFormatId = null;
    showLoading(true);
    setUIState(true);
    showStatus('Fetching formats...', 'info');

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
                showStatus("No downloadable formats found for this URL.", "info");
            }
        } else {
            showStatus(`Error: ${data.message}`, 'error');
        }
    } catch (error) {
        showStatus('Could not connect to desktop app. Is it running?', 'error');
    } finally {
        showLoading(false);
        setUIState(false);
    }
});

downloadHighestQualityBtn.addEventListener('click', async () => {
    const url = urlSelect.value;
    if (!url) {
        showStatus('No media URL selected.', 'error');
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
            showStatus('Download initiated! Check desktop app.', 'success');
        } else {
            // Check for specific error message from the app
            if (data.message && data.message.includes("disabled")) {
                showStatus('Error: Browser monitoring is disabled in the desktop app.', 'error');
            } else {
                showStatus(`Download failed: ${data.message}`, 'error');
            }
        }
    } catch (error) {
        showStatus('Could not connect to desktop app. Is it running?', 'error');
    } finally {
        showLoading(false);
        setUIState(false);
    }
});

startDownloadBtn.addEventListener('click', async () => {
    const url = urlSelect.value;
    if (!url) {
        showStatus('URL has been lost. Please go back and select it again.', 'error');
        return;
    }
    if (selectedFormatId === null) {
        showStatus('Please select a format.', 'error');
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
            showStatus('Download initiated! Check desktop app.', 'success');
            showInitialView();
        } else {
            showStatus(`Download failed: ${data.message}`, 'error');
        }
    } catch (error) {
        showStatus('Could not connect to desktop app. Is it running?', 'error');
    } finally {
        showLoading(false);
        setUIState(false);
    }
});

backToInitialViewBtn.addEventListener('click', showInitialView);

// Initial setup on DOMContentLoaded
document.addEventListener('DOMContentLoaded', async () => {
    setUIState(true);
    showLoading(true);

    const currentTab = await getCurrentTab();
    if (!currentTab) {
        detectionStatus.textContent = "Could not get current tab info.";
        detectionStatus.classList.remove('hidden');
        showLoading(false);
        setUIState(true); // Disable everything if no tab
        return;
    }

    // Ask the background script for detected URLs
    chrome.runtime.sendMessage({ type: 'get_media_urls', tabId: currentTab.id }, (response) => {
        const detectedUrls = response.urls || [];
        
        // Add the main page URL if it looks like a direct YouTube link
        if (currentTab.url && (currentTab.url.includes("youtube.com/watch") || currentTab.url.includes("youtube.com/playlist"))) {
            if (!detectedUrls.includes(currentTab.url)) {
                detectedUrls.unshift(currentTab.url); // Add to the beginning
            }
        }

        urlSelect.innerHTML = ''; // Clear dropdown

        if (detectedUrls.length > 0) {
            detectedUrls.forEach(url => {
                const option = document.createElement('option');
                option.value = url;
                // Truncate long URLs for display
                option.textContent = url.length > 60 ? url.substring(0, 57) + '...' : url;
                urlSelect.appendChild(option);
            });
            detectionStatus.classList.add('hidden');
        } else {
            detectionStatus.textContent = "No media streams detected on this page. Try playing a video to capture its URL.";
            detectionStatus.classList.remove('hidden');
        }

        showLoading(false);
        setUIState(false); // Enable UI based on whether URLs were found
    });
});