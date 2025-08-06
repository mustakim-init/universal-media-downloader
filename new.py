import sys
import os
import threading
import subprocess
import queue
import re
import time
import json
import requests
from datetime import datetime
import logging
import tempfile
from urllib.parse import urlparse, parse_qs
import random
import base64


# --- Flask Server ---
from flask import Flask, request, jsonify, g, make_response
from flask_cors import CORS
from werkzeug.serving import run_simple
from werkzeug.middleware.dispatcher import DispatcherMiddleware


# --- Configure Logging ---
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Console handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# --- Path Helper Function ---
def resource_path(relative_path):
    """Get absolute path to resource, works for dev and for PyInstaller"""
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)

# --- Configuration ---
FLASK_PORT = 5000
app = Flask(__name__)
CORS(app)

# Message queue for inter-thread communication (Flask to GUI)
gui_message_queue = queue.Queue()

# Path to the yt-dlp executable
if getattr(sys, 'frozen', False):
    YTDLP_PATH = os.path.join(sys._MEIPASS, 'yt-dlp')
else:
    YTDLP_PATH = 'yt-dlp'

startupinfo = None
if sys.platform == 'win32':
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE

# Use DEVNULL for subprocess stdout/stderr
DEVNULL = subprocess.DEVNULL
SUBPROCESS_CREATION_FLAGS = subprocess.DETACHED_PROCESS if sys.platform == 'win32' else 0

YT_DLP_BIN = resource_path('yt-dlp.exe') if sys.platform == 'win32' else resource_path('yt-dlp')
FFMPEG_BIN = resource_path(os.path.join('ffmpeg', 'bin', 'ffmpeg.exe')) if sys.platform == 'win32' else resource_path(os.path.join('ffmpeg', 'bin', 'ffmpeg'))
EXTENSION_SOURCE_DIR_BUNDLE = resource_path('extension')

# --- Smart User Agent Manager ---
class UserAgentManager:
    """Manages user agents for different platforms"""
    
    USER_AGENTS = {
        'chrome_windows': [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36',
        ],
        'firefox_windows': [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:119.0) Gecko/20100101 Firefox/119.0',
        ],
        'safari_mac': [
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15',
        ]
    }
    
    @classmethod
    def get_random_user_agent(cls, platform_hint=None):
        """Get a random user agent, optionally filtered by platform"""
        if platform_hint:
            agents = cls.USER_AGENTS.get(platform_hint, cls.USER_AGENTS['chrome_windows'])
        else:
            all_agents = []
            for agents_list in cls.USER_AGENTS.values():
                all_agents.extend(agents_list)
            agents = all_agents
        return random.choice(agents)

# --- Enhanced URL Intelligence ---
class URLAnalyzer:
    """Enhanced URL analyzer with platform-specific handling"""
    
    # Platform-specific patterns and requirements
    PLATFORM_CONFIG = {
        'facebook': {
            'patterns': [r'facebook\.com', r'fb\.watch'],
            'temp_patterns': [r'fbcdn\.net', r'video.*\.xx\.fbcdn\.net'],
            'needs_cookies': True,
            'required_headers': ['User-Agent', 'Accept-Language'],
            'user_agent_type': 'chrome_windows'
        },
        'instagram': {
            'patterns': [r'instagram\.com'],
            'temp_patterns': [r'instagram.*\.fbcdn\.net', r'scontent.*\.cdninstagram\.com'],
            'needs_cookies': True,
            'required_headers': ['User-Agent', 'Accept-Language', 'X-IG-App-ID'],
            'user_agent_type': 'chrome_windows'
        },
        'youtube': {
            'patterns': [r'youtube\.com', r'youtu\.be', r'googlevideo\.com'],
            'temp_patterns': [r'googlevideo\.com/videoplayback'],
            'needs_cookies': False,
            'required_headers': ['User-Agent'],
            'user_agent_type': 'chrome_windows'
        },
        'twitter': {
            'patterns': [r'twitter\.com', r'x\.com'],
            'temp_patterns': [r'video\.twimg\.com'],
            'needs_cookies': True,
            'required_headers': ['User-Agent', 'Authorization'],
            'user_agent_type': 'chrome_windows'
        },
        'tiktok': {
            'patterns': [r'tiktok\.com'],
            'temp_patterns': [r'muscdn\.com', r'tiktokcdn\.com'],
            'needs_cookies': True,
            'required_headers': ['User-Agent', 'Referer'],
            'user_agent_type': 'chrome_windows'
        }
    }
    
    @classmethod
    def detect_platform(cls, url):
        """Detect which platform a URL belongs to"""
        url_lower = url.lower()
        for platform, config in cls.PLATFORM_CONFIG.items():
            if any(re.search(pattern, url_lower) for pattern in config['patterns']):
                return platform
        return 'generic'
    
    @classmethod
    def is_temporary_url(cls, url):
        """Enhanced temporary URL detection"""
        url_lower = url.lower()
        
        # Generic temporary patterns
        generic_patterns = [
            r'blob:', r'\.m3u8(\?|$)', r'\.mpd(\?|$)', r'manifest\.',
            r'videoplayback\?', r'/hls/', r'/dash/'
        ]
        
        if any(re.search(pattern, url_lower) for pattern in generic_patterns):
            return True
        
        # Platform-specific temporary patterns
        for platform, config in cls.PLATFORM_CONFIG.items():
            temp_patterns = config.get('temp_patterns', [])
            if any(re.search(pattern, url_lower) for pattern in temp_patterns):
                return True
        
        return False
    
    @classmethod
    def needs_cookies(cls, url):
        """Determine if URL needs cookies"""
        platform = cls.detect_platform(url)
        if platform in cls.PLATFORM_CONFIG:
            return cls.PLATFORM_CONFIG[platform]['needs_cookies']
        
        # Default behavior for unknown platforms
        return cls.is_temporary_url(url)
    
    @classmethod
    def get_platform_config(cls, url):
        """Get platform-specific configuration"""
        platform = cls.detect_platform(url)
        return cls.PLATFORM_CONFIG.get(platform, {
            'needs_cookies': False,
            'required_headers': ['User-Agent'],
            'user_agent_type': 'chrome_windows'
        })

# --- Enhanced Cookie Manager ---
class CookieManager:
    """Enhanced cookie management with validation and filtering"""
    
    ESSENTIAL_COOKIE_PATTERNS = {
        'facebook': [r'c_user', r'xs', r'datr', r'sb', r'fr'],
        'instagram': [r'sessionid', r'csrftoken', r'ds_user_id', r'shbid', r'rur'],
        'youtube': [r'VISITOR_INFO1_LIVE', r'YSC', r'PREF'],
        'twitter': [r'auth_token', r'ct0', r'personalization_id'],
        'tiktok': [r'sessionid', r'tt_csrf_token', r'tt_webid']
    }
    
    @classmethod
    def filter_essential_cookies(cls, cookies, platform):
        """Filter cookies to only essential ones for the platform"""
        if not cookies or platform not in cls.ESSENTIAL_COOKIE_PATTERNS:
            return cookies
        
        essential_patterns = cls.ESSENTIAL_COOKIE_PATTERNS[platform]
        filtered_cookies = []
        
        for cookie in cookies:
            cookie_name = cookie.get('name', '').lower()
            if any(re.search(pattern.lower(), cookie_name) for pattern in essential_patterns):
                filtered_cookies.append(cookie)
        
        logger.info(f"Filtered {len(cookies)} cookies to {len(filtered_cookies)} essential ones for {platform}")
        return filtered_cookies if filtered_cookies else cookies  # Fallback to all cookies if no essential ones found
    
    @classmethod
    def validate_cookies(cls, cookies):
        """Validate and clean cookies"""
        if not cookies:
            return []
        
        valid_cookies = []
        current_time = time.time()
        
        for cookie in cookies:
            # Skip expired cookies
            expiry = cookie.get('expirationDate')
            if expiry and expiry < current_time:
                continue
            
            # Ensure required fields
            if not cookie.get('name') or not cookie.get('domain'):
                continue
            
            # Clean the cookie
            cleaned_cookie = {
                'name': str(cookie['name']),
                'value': str(cookie.get('value', '')),
                'domain': cookie['domain'],
                'path': cookie.get('path', '/'),
                'secure': cookie.get('secure', False),
                'httpOnly': cookie.get('httpOnly', False),
                'expirationDate': expiry
            }
            
            valid_cookies.append(cleaned_cookie)
        
        return valid_cookies
    
    @classmethod
    def convert_to_netscape(cls, cookies):
        """Convert cookies to Netscape format with better formatting"""
        if not cookies:
            return ""
        
        netscape_cookies = [
            "# Netscape HTTP Cookie File",
            "# This file was generated by Universal Media Downloader",
            "# Enhanced cookie format for better compatibility",
            ""
        ]
        
        for cookie in cookies:
            try:
                domain = cookie.get('domain', '').strip()
                if not domain:
                    continue
                
                # Ensure proper domain format
                if not domain.startswith('.') and not domain.startswith('http'):
                    domain = '.' + domain
                
                include_subdomains = 'TRUE'
                path = cookie.get('path', '/')
                secure = 'TRUE' if cookie.get('secure') else 'FALSE'
                
                # Handle expiration
                expires = cookie.get('expirationDate')
                if expires is None:
                    expires_int = 0  # Session cookie
                else:
                    expires_int = int(float(expires))
                
                name = cookie.get('name', '')
                value = str(cookie.get('value', ''))
                
                # Escape special characters in value
                value = value.replace('\t', '\\t').replace('\n', '\\n').replace('\r', '\\r')
                
                cookie_line = "\t".join([
                    domain, include_subdomains, path, secure, 
                    str(expires_int), name, value
                ])
                netscape_cookies.append(cookie_line)
                
            except Exception as e:
                logger.warning(f"Skipping invalid cookie: {e}")
                continue
        
        return "\n".join(netscape_cookies)

# --- Settings Manager ---
class SettingsManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._settings = {
            'browser_monitor_enabled': True,
            'download_save_directory': os.path.join(os.path.expanduser("~"), 'Downloads'),
            'overwrite_existing_file': False,
            'double_click_action': "Open folder",
            'use_cookies_smartly': True,
            'retry_attempts': 3,
            'use_fallback_methods': True
        }
        # Ensure download directory exists on startup
        if not os.path.exists(self._settings['download_save_directory']):
            os.makedirs(self._settings['download_save_directory'], exist_ok=True)

    def get(self, key):
        with self._lock:
            return self._settings.get(key)

    def set(self, key, value):
        with self._lock:
            self._settings[key] = value
            logger.info(f"Setting '{key}' updated to: {value}")

settings = SettingsManager()

# Ensure binaries exist
if not os.path.exists(YT_DLP_BIN):
    logger.warning(f"yt-dlp binary not found at {YT_DLP_BIN}.")
if not os.path.exists(FFMPEG_BIN):
    logger.warning(f"ffmpeg binary not found at {FFMPEG_BIN}.")

def run_yt_dlp_command(args, cookies=None, timeout=None, platform_config=None, retry_count=0):
    """Enhanced yt-dlp command runner with smart retry logic"""
    temp_cookie_file_path = None
    env = os.environ.copy()
    max_retries = settings.get('retry_attempts')
    
    try:
        # Get platform-specific user agent
        if platform_config:
            user_agent_type = platform_config.get('user_agent_type', 'chrome_windows')
            user_agent = UserAgentManager.get_random_user_agent(user_agent_type)
        else:
            user_agent = UserAgentManager.get_random_user_agent()
        
        # Enhanced base arguments
        base_args = [
            '--no-check-certificate',
            '--no-warnings',
            '--user-agent', user_agent,
            '--add-header', 'Accept-Language:en-US,en;q=0.9',
            '--add-header', 'Accept:text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            '--add-header', 'Accept-Encoding:gzip, deflate',
            '--add-header', 'DNT:1',
            '--add-header', 'Connection:keep-alive',
            '--socket-timeout', '60',
            '--retries', '3'
        ]
        
        # Add platform-specific headers
        if platform_config:
            required_headers = platform_config.get('required_headers', [])
            if 'X-IG-App-ID' in required_headers:
                base_args.extend(['--add-header', 'X-IG-App-ID:936619743392459'])
            if 'Referer' in required_headers and 'tiktok' in str(args):
                base_args.extend(['--add-header', 'Referer:https://www.tiktok.com/'])
        
        # Handle cookies with validation and filtering
        if cookies:
            # Validate cookies first
            valid_cookies = CookieManager.validate_cookies(cookies)
            
            # Filter to essential cookies if we have platform info
            if platform_config and 'platform' in platform_config:
                valid_cookies = CookieManager.filter_essential_cookies(
                    valid_cookies, platform_config['platform']
                )
            
            if valid_cookies:
                netscape_cookies_str = CookieManager.convert_to_netscape(valid_cookies)
                if netscape_cookies_str:
                    fd, temp_cookie_file_path = tempfile.mkstemp(suffix='.txt', text=True)
                    try:
                        with os.fdopen(fd, 'w', encoding='utf-8') as temp_f:
                            temp_f.write(netscape_cookies_str)
                        
                        if sys.platform != 'win32':
                            os.chmod(temp_cookie_file_path, 0o600)
                        
                        base_args.extend(['--cookies', temp_cookie_file_path])
                        logger.debug(f"Using {len(valid_cookies)} cookies from file: {temp_cookie_file_path}")
                    except Exception as e:
                        logger.error(f"Error writing cookie file: {e}")
                        if temp_cookie_file_path and os.path.exists(temp_cookie_file_path):
                            os.unlink(temp_cookie_file_path)
                        raise
        
        # Combine arguments
        command = [YT_DLP_BIN] + base_args + args
        
        logger.debug(f"Running yt-dlp (attempt {retry_count + 1}/{max_retries + 1}): {' '.join(command[:5])}...")
        
        # Enhanced environment
        env.update({
            'PYTHONIOENCODING': 'utf-8',
            'LANG': 'en_US.UTF-8',
            'LC_ALL': 'en_US.UTF-8'
        })
        
        result = subprocess.run(
            command, 
            capture_output=True, 
            text=True, 
            check=False, 
            startupinfo=startupinfo, 
            timeout=timeout,
            env=env
        )
        
        # Enhanced error handling with retry logic
        if result.returncode != 0:
            error_msg = result.stderr.lower() if result.stderr else ""
            
            # Check if we should retry
            should_retry = (
                retry_count < max_retries and
                settings.get('use_fallback_methods') and
                any(error_phrase in error_msg for error_phrase in [
                    '403', 'forbidden', 'unauthorized', 'private', 'requires login',
                    'unable to download', 'http error', 'connection', 'timeout'
                ])
            )
            
            if should_retry:
                logger.warning(f"Attempt {retry_count + 1} failed, retrying with different approach...")
                time.sleep(2 ** retry_count)  # Exponential backoff
                
                # FIX: Removed the broken --extract-flat logic that was here
                # On retry, we'll just use the same arguments but with a fresh connection.
                modified_args = args.copy()
                
                return run_yt_dlp_command(
                    modified_args, cookies, timeout, platform_config, retry_count + 1
                )
            
            logger.error(f"yt-dlp command failed after {retry_count + 1} attempts with exit code {result.returncode}")
            logger.error(f"Stderr: {result.stderr}")
            return None, result.stderr
        
        return result.stdout, result.stderr
        
    except FileNotFoundError:
        error_msg = f"yt-dlp executable not found at {YT_DLP_BIN}."
        logger.error(error_msg)
        return None, error_msg
    except subprocess.TimeoutExpired:
        error_msg = f"yt-dlp command timed out after {timeout} seconds."
        logger.error(error_msg)
        return None, error_msg
    finally:
        if temp_cookie_file_path and os.path.exists(temp_cookie_file_path):
            try:
                os.unlink(temp_cookie_file_path)
                logger.debug(f"Cleaned up cookie file: {temp_cookie_file_path}")
            except Exception as e:
                logger.error(f"Error removing cookie file: {e}")

@app.after_request
def after_request(response):
    header = response.headers
    header['Access-Control-Allow-Origin'] = '*'
    header['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    header['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response

def run_command_in_bundle(command_parts, **kwargs):
    """Runs a subprocess command, ensuring yt-dlp and ffmpeg paths are used."""
    env_vars = os.environ.copy()
    ffmpeg_dir = os.path.dirname(FFMPEG_BIN)
    if ffmpeg_dir not in env_vars.get('PATH', '').split(os.pathsep):
        env_vars['PATH'] = ffmpeg_dir + os.pathsep + env_vars.get('PATH', '')
    if command_parts and command_parts[0] == 'yt-dlp':
        command_parts[0] = YT_DLP_BIN
    logger.debug(f"Running command: {' '.join(command_parts[:3])}...")
    try:
        return subprocess.run(command_parts, env=env_vars, startupinfo=startupinfo, creationflags=SUBPROCESS_CREATION_FLAGS, **kwargs)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.error(f"Command execution error: {e}")
        raise

def sanitize_filename(filename):
    """Sanitize filename for the current OS."""
    if not filename:
        return "download"
    
    # Remove query parameters and fragments
    filename = filename.split('?')[0].split('#')[0]
    
    invalid_chars_regex = r'[<>:"/\\|?*\x00-\x1F]'
    sanitized = re.sub(invalid_chars_regex, '_', filename)
    if sys.platform == 'win32':
        reserved_names = re.compile(r'^(con|prn|aux|nul|com[1-9]|lpt[1-9])$', re.IGNORECASE)
        name_without_ext = os.path.splitext(sanitized)[0]
        if reserved_names.match(name_without_ext):
            sanitized = '_' + sanitized
        sanitized = sanitized.rstrip('. ')
    max_length = 200
    if len(sanitized) > max_length:
        name, ext = os.path.splitext(sanitized)
        sanitized = name[:max_length - len(ext)] + ext
    return sanitized or "download"

def _perform_yt_dlp_download(command_template, url, is_playlist, media_type, is_merged_download, download_dir, cancel_event, cookie_string=None, platform_config=None):
    """Enhanced download with platform-specific handling"""
    filename = "Playlist Download" if is_playlist else os.path.basename(url)
    current_process = None
    temp_cookie_file = None
    
    try:
        # Build enhanced command with platform-specific optimizations
        enhanced_command = command_template.copy()
        
        # Add platform-specific arguments
        if platform_config:
            user_agent = UserAgentManager.get_random_user_agent(
                platform_config.get('user_agent_type', 'chrome_windows')
            )
            enhanced_command.extend(['--user-agent', user_agent])
            
            # Add platform-specific headers
            required_headers = platform_config.get('required_headers', [])
            enhanced_command.extend(['--add-header', 'Accept-Language:en-US,en;q=0.9'])
            
            if 'X-IG-App-ID' in required_headers:
                enhanced_command.extend(['--add-header', 'X-IG-App-ID:936619743392459'])
            if 'Referer' in required_headers and 'tiktok' in url.lower():
                enhanced_command.extend(['--add-header', 'Referer:https://www.tiktok.com/'])
        
        # Handle cookies with enhanced processing
        cookie_args = []
        if cookie_string:
            fd, temp_cookie_file = tempfile.mkstemp(suffix='.txt', text=True)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    f.write(cookie_string)
                if sys.platform != 'win32':
                    os.chmod(temp_cookie_file, 0o600)
                cookie_args = ['--cookies', temp_cookie_file]
                logger.info(f"Using enhanced cookies for download: {url}")
            except Exception as e:
                logger.error(f"Error creating cookie file: {e}")
                if temp_cookie_file and os.path.exists(temp_cookie_file):
                    os.unlink(temp_cookie_file)
                raise
        
        # --- FIX #3: Overhauled path and filename prediction ---
        predicted_filename = filename
        final_output_path = ""  # Will store the final path for verification

        if is_playlist:
            # For playlists, get the playlist title to use as a directory name
            info_command = [YT_DLP_BIN, '--get-filename', '--no-warnings', *cookie_args, '-o', '%(playlist_title)s', '--playlist-end', '1', url]
            info_result = run_command_in_bundle(info_command, capture_output=True, text=True, check=False, timeout=30)
            
            raw_title = info_result.stdout.strip().split('\n')[0] if info_result.stdout.strip() else "Playlist"
            predicted_filename = sanitize_filename(raw_title)
            filename = predicted_filename  # This name is sent to the GUI
            
            # The output template uses the predicted directory name
            final_output_template = os.path.join(download_dir, predicted_filename, '%(title)s.%(ext)s')
            # The path to verify later is the directory itself
            final_output_path = os.path.join(download_dir, predicted_filename)
            # Ensure the target directory for the playlist exists, as yt-dlp can fail on this
            os.makedirs(os.path.dirname(final_output_template), exist_ok=True)
        else:  # For single files
            info_command = [YT_DLP_BIN, '--get-filename', '--no-warnings', *cookie_args, '-o', '%(title)s.%(ext)s', '--no-playlist', url]
            info_result = run_command_in_bundle(info_command, capture_output=True, text=True, check=False, timeout=30)
            
            predicted_filename_raw = info_result.stdout.strip()
            if predicted_filename_raw and predicted_filename_raw != 'NA':
                predicted_filename = sanitize_filename(predicted_filename_raw)
            else:
                # *** NEW: Smart fallback for direct media URLs without a title ***
                try:
                    # Attempt to parse the URL and get a cleaner name from the path
                    parsed_url = urlparse(url)
                    base_name_from_path = os.path.basename(parsed_url.path)
                    # If the name is useful (not a hash, has an extension), use it
                    if base_name_from_path and '.' in base_name_from_path:
                         predicted_filename = sanitize_filename(base_name_from_path)
                    else:
                        # Otherwise, create a clean, timestamped generic name
                        predicted_filename = f"media_download_{int(time.time())}"
                except Exception:
                    # Final fallback if parsing fails for any reason
                    predicted_filename = f"media_download_{int(time.time())}"
            
            filename = predicted_filename # This name is sent to the GUI

            # Handle existing files for single downloads
            base_name, ext = os.path.splitext(predicted_filename)

            if not ext:
                ext = '.mp4' if media_type == 'video' else '.mp3'
            current_filename = base_name + ext
            counter = 0
            while not settings.get('overwrite_existing_file') and os.path.exists(os.path.join(download_dir, current_filename)):
                counter += 1
                current_filename = f"{base_name} ({counter}){ext}"
            
            final_output_template = os.path.join(download_dir, current_filename)
            final_output_path = final_output_template  # The path to verify is the file itself
            filename = current_filename  # This name is sent to the GUI

        enhanced_command.extend(['-o', final_output_template])
        
        # Add anti-detection measures
        enhanced_command.extend([
            '--no-check-certificate',
            '--no-warnings',
            '--socket-timeout', '60',
            '--retries', '3'
        ])
        
        gui_message_queue.put({'type': 'update_download_status', 'url': url, 'filename': filename, 'status': 'Initializing'})
        
        # Insert cookie args and run process
        final_command = enhanced_command[:1] + cookie_args + enhanced_command[1:]
        
        current_process = subprocess.Popen(
            final_command, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE, 
            text=True, 
            bufsize=1, 
            universal_newlines=True, 
            creationflags=SUBPROCESS_CREATION_FLAGS, 
            startupinfo=startupinfo
        )
        
        gui_message_queue.put({'type': 'register_process', 'url': url, 'process': current_process, 'cancel_event': cancel_event})
        
        # Enhanced output processing
        stdout_lines = []
        error_lines = []
        while True:
            if cancel_event.is_set():
                logger.info(f"Cancellation event set for {url}. Breaking download loop.")
                break
            
            stdout_line = current_process.stdout.readline()
            stderr_line = current_process.stderr.readline()
            
            if not stdout_line and not stderr_line and current_process.poll() is not None:
                break
            
            if stdout_line:
                stdout_lines.append(stdout_line)
                if '[download]' in stdout_line and '%' in stdout_line:
                    match = re.search(r'(\d+\.\d+)%', stdout_line)
                    if match:
                        progress = match.group(1) + '%'
                        gui_message_queue.put({'type': 'update_download_status', 'url': url, 'status': 'Downloading', 'progress': progress})
                elif '[ExtractAudio]' in stdout_line or '[ffmpeg]' in stdout_line:
                    gui_message_queue.put({'type': 'update_download_status', 'url': url, 'status': 'Processing'})
            
            if stderr_line:
                error_lines.append(stderr_line)
                logger.debug(f"yt-dlp stderr: {stderr_line.strip()}")

        return_code = current_process.poll()
        
        # Handle cancellation FIRST
        if cancel_event.is_set():
            gui_message_queue.put({'type': 'update_download_status', 'url': url, 'status': 'Cancelled', 'message': 'Download cancelled by user.'})
            return  # Exit immediately after cancellation
            
        # Then handle process return codes
        if return_code not in [0, -15]:  # -15 is SIGTERM on Unix
            error_msg = '\n'.join(error_lines[-10:])
            raise Exception(f"yt-dlp download failed (exit code {return_code}): {error_msg}")
        
        # --- FIX #3: Overhauled file verification logic ---
        actual_filesize_bytes = 0
        actual_filename = os.path.basename(final_output_path)  # Use the predicted name
        final_path_for_gui = final_output_path  # The path to be sent to GUI

        if is_playlist:
            if os.path.isdir(final_output_path):
                try:
                    # Calculate total size of all files in the playlist directory
                    actual_filesize_bytes = sum(os.path.getsize(os.path.join(dirpath, f)) for dirpath, _, filenames in os.walk(final_output_path) for f in filenames)
                except Exception as e:
                    logger.warning(f"Could not calculate total size of playlist directory {final_output_path}: {e}")
                    actual_filesize_bytes = 0  # Report 0 if error
            else:
                raise Exception(f"Downloaded playlist directory not found at expected location: {final_output_path}")
        else:  # single file
            if os.path.exists(final_output_path):
                actual_filesize_bytes = os.path.getsize(final_output_path)
            else:
                raise Exception(f"Downloaded file not found at expected location: {final_output_path}")

        detected_filetype = 'playlist' if is_playlist else media_type

        gui_message_queue.put({
            'type': 'update_download_status', 
            'url': url, 
            'status': 'Completed', 
            'progress': '100%', 
            'filename': actual_filename, 
            'filesize_bytes': actual_filesize_bytes
        })
        gui_message_queue.put({
            'type': 'add_completed', 
            'url': url, 
            'filetype': detected_filetype, 
            'filename': actual_filename, 
            'is_playlist': is_playlist, 
            'path': final_path_for_gui,  # Use the corrected final path
            'timestamp': time.time(), 
            'filesize_bytes': actual_filesize_bytes
        })
    
    except Exception as e:
        error_message = str(e)
        gui_message_queue.put({'type': 'update_download_status', 'url': url, 'status': 'Failed', 'message': error_message, 'filename': filename})
        logger.error(f"Error during download for {url}: {error_message}")
    
    finally:
        # ONLY terminate if process is still running AND we're canceling
        if current_process and current_process.poll() is None and cancel_event.is_set():
            try:
                current_process.terminate()
                current_process.wait(timeout=5)
            except Exception as e:
                logger.error(f"Error terminating process: {e}")
                
        gui_message_queue.put({'type': 'remove_process', 'url': url})
        
        if temp_cookie_file and os.path.exists(temp_cookie_file):
            try:
                os.unlink(temp_cookie_file)
            except Exception as e:
                logger.error(f"Error removing temp cookie file: {e}")

@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "healthy", "version": "2.1"})

@app.route('/shutdown', methods=['POST'])
def shutdown():
    # FIX #2: Gracefully handle missing shutdown function
    func = request.environ.get('werkzeug.server.shutdown')
    if func is None:
        logger.warning("Shutdown command received, but not running with the Werkzeug Server. Cannot self-terminate.")
        return jsonify({'status': 'error', 'message': 'Not running with the Werkzeug Server'}), 500
    func()
    return jsonify({'status': 'success', 'message': 'Server is shutting down.'})

@app.route("/analyze_url", methods=["POST"])
def analyze_url():
    """Enhanced URL analysis with platform detection"""
    data = request.json
    url = data.get("url")
    if not url:
        return jsonify({"error": "URL is required"}), 400
    
    platform = URLAnalyzer.detect_platform(url)
    platform_config = URLAnalyzer.get_platform_config(url)
    
    analysis = {
        "platform": platform,
        "is_temporary": URLAnalyzer.is_temporary_url(url),
        "needs_cookies": URLAnalyzer.needs_cookies(url),
        "required_headers": platform_config.get('required_headers', []),
        "user_agent_type": platform_config.get('user_agent_type', 'chrome_windows'),
        "suggestions": []
    }
    
    if analysis["is_temporary"]:
        analysis["suggestions"].append("This appears to be a temporary/CDN URL. Enhanced cookies and headers will be used.")
    if analysis["needs_cookies"]:
        analysis["suggestions"].append(f"This {platform} content requires authentication. Platform-specific cookies will be filtered and applied.")
    if platform != 'generic':
        analysis["suggestions"].append(f"Platform detected: {platform}. Using optimized extraction methods.")
    
    return jsonify(analysis)

@app.route("/get_formats", methods=["POST"])
def get_formats():
    """Enhanced format retrieval with smart fallbacks"""
    data = request.json
    url = data.get("url")
    cookies = data.get("cookies")
    if not url:
        return jsonify({"error": "URL is required"}), 400
    
    platform = URLAnalyzer.detect_platform(url)
    platform_config = URLAnalyzer.get_platform_config(url)
    platform_config['platform'] = platform  # Add platform info for cookie filtering
    
    # Filter cookies if we have them
    if cookies and URLAnalyzer.needs_cookies(url):
        cookies = CookieManager.validate_cookies(cookies)
        if platform != 'generic':
            cookies = CookieManager.filter_essential_cookies(cookies, platform)
        logger.info(f"Using {len(cookies)} filtered cookies for {platform}")
    
    # FIX #1 & #4: Removed unsupported/redundant flags from approaches
    format_approaches = [
        # Standard approach
        ["--list-formats", "--ignore-errors", url],
        # Last resort - try to get basic info
        ["--dump-json", "--ignore-errors", url]
    ]
    
    for approach_idx, args in enumerate(format_approaches):
        logger.info(f"Trying format approach {approach_idx + 1}/{len(format_approaches)} for {platform}")
        
        stdout, stderr = run_yt_dlp_command(
            args, 
            cookies if URLAnalyzer.needs_cookies(url) else None, 
            timeout=60,  # Increased timeout
            platform_config=platform_config
        )
        
        if stdout:
            # FIX #1: Adjusted index check after removing an approach
            if approach_idx == 0:  # Standard format list approach
                formats = stdout.strip().split('\n')
                parsed_formats = parse_format_list(formats)
                if parsed_formats:
                    return jsonify({
                        "formats": parsed_formats, 
                        "used_cookies": bool(cookies),
                        "platform": platform,
                        "approach": f"method_{approach_idx + 1}"
                    })
            else:  # JSON dump approach
                try:
                    import json
                    video_info = json.loads(stdout)
                    formats = extract_formats_from_json(video_info)
                    if formats:
                        return jsonify({
                            "formats": formats, 
                            "used_cookies": bool(cookies),
                            "platform": platform,
                            "approach": "json_fallback"
                        })
                except json.JSONDecodeError:
                    logger.warning("Failed to parse JSON output")
    
    # If all approaches failed, provide helpful error
    error_msg = "Unable to retrieve formats."
    if stderr:
        stderr_lower = stderr.lower()
        if "403" in stderr_lower or "forbidden" in stderr_lower:
            error_msg = f"Access denied. This {platform} content may be private or require different authentication."
        elif "private" in stderr_lower or "login" in stderr_lower:
            error_msg = f"This {platform} content is private. Please ensure you're logged in to the correct account."
        elif "not available" in stderr_lower:
            error_msg = "This content is not available in your region or has been removed."
        elif "unsupported" in stderr_lower:
            error_msg = "This URL format is not supported."
        elif "timeout" in stderr_lower:
            error_msg = "Request timed out. The server may be overloaded."
        else:
            # Extract the actual error message
            error_match = re.search(r'ERROR: (.+)', stderr)
            if error_match:
                error_msg = f"{platform.title()} error: {error_match.group(1)}"
    
    return jsonify({
        "error": error_msg, 
        "details": stderr,
        "platform": platform,
        "suggestions": get_error_suggestions(platform, stderr)
    }), 500

def parse_format_list(formats):
    """Enhanced format parsing with better error handling"""
    parsed_formats = []
    header_found = False
    
    for line in formats:
        line = line.strip()
        if not line:
            continue
            
        # Skip info lines
        if line.startswith('[') or line.startswith('WARNING') or line.startswith('ERROR'):
            continue
            
        # Find header
        if 'ID' in line and ('ext' in line or 'resolution' in line):
            header_found = True
            continue
            
        if not header_found:
            continue
        
        # Parse format line
        try:
            # Split by whitespace but be careful with the note field
            parts = line.split()
            if len(parts) >= 3:
                format_info = {
                    "id": parts[0],
                    "ext": parts[1],
                    "resolution": parts[2] if len(parts) > 2 else "unknown"
                }
                
                # Handle the note field (everything after resolution)
                if len(parts) > 3:
                    note_parts = parts[3:]
                    # Clean up common yt-dlp format descriptions
                    note = ' '.join(note_parts)
                    note = re.sub(r'\s+', ' ', note).strip()  # Normalize whitespace
                    format_info["note"] = note[:100] + "..." if len(note) > 100 else note
                else:
                    format_info["note"] = ""
                
                # Enhanced type detection
                resolution = format_info["resolution"].lower()
                note = format_info.get("note", "").lower()
                
                if 'audio only' in resolution or 'audio only' in note:
                    format_info["type"] = "audio"
                elif any(vid_indicator in note for vid_indicator in ['video', 'mp4', 'webm', 'mkv']):
                    format_info["type"] = "video"
                elif resolution != "unknown" and resolution not in ['audio', 'none']:
                    format_info["type"] = "video"
                else:
                    format_info["type"] = "audio" if format_info["ext"] in ['m4a', 'mp3', 'aac', 'opus'] else "video"
                
                # Add quality indicators
                if 'best' in note:
                    format_info["quality"] = "best"
                elif 'worst' in note:
                    format_info["quality"] = "worst"
                else:
                    format_info["quality"] = "standard"
                
                parsed_formats.append(format_info)
                
        except Exception as e:
            logger.debug(f"Skipping unparseable format line: {line} - {e}")
            continue
    
    # Sort formats by quality (best first)
    parsed_formats.sort(key=lambda f: (
        f["type"] == "video",  # Video formats first
        f["quality"] == "best",  # Best quality first
        f["resolution"] != "audio only",  # Non-audio formats first
        f["id"]
    ), reverse=True)
    
    return parsed_formats

def extract_formats_from_json(video_info):
    """Extract formats from JSON dump as fallback"""
    formats = []
    
    try:
        if 'formats' in video_info:
            for fmt in video_info['formats']:
                format_info = {
                    "id": str(fmt.get('format_id', 'unknown')),
                    "ext": fmt.get('ext', 'unknown'),
                    "resolution": fmt.get('resolution', 'unknown'),
                    "note": fmt.get('format_note', ''),
                    "type": "video" if fmt.get('vcodec') != 'none' else "audio",
                    "quality": "standard"
                }
                formats.append(format_info)
        
        # If no formats found, create basic ones
        if not formats:
            formats = [
                {"id": "best", "ext": "mp4", "resolution": "best", "note": "Best available quality", "type": "video", "quality": "best"},
                {"id": "worst", "ext": "mp4", "resolution": "worst", "note": "Lowest available quality", "type": "video", "quality": "worst"}
            ]
    
    except Exception as e:
        logger.error(f"Error extracting formats from JSON: {e}")
        return []
    
    return formats

def get_error_suggestions(platform, stderr):
    """Provide platform-specific error suggestions"""
    suggestions = []
    
    if not stderr:
        return suggestions
    
    stderr_lower = stderr.lower()
    
    if platform == 'facebook':
        if '403' in stderr_lower or 'forbidden' in stderr_lower:
            suggestions.extend([
                "Try logging into Facebook in your browser first",
                "Make sure the video privacy settings allow viewing",
                "Check if the video is still available"
            ])
    elif platform == 'instagram':
        if 'private' in stderr_lower or '403' in stderr_lower:
            suggestions.extend([
                "Ensure you're following this Instagram account",
                "Try logging into Instagram in your browser",
                "Check if the content is still available"
            ])
    elif platform == 'youtube':
        if 'private' in stderr_lower:
            suggestions.append("This YouTube video is private or unlisted")
        elif 'copyright' in stderr_lower:
            suggestions.append("This video may be blocked due to copyright restrictions")
    elif platform == 'tiktok':
        if '403' in stderr_lower:
            suggestions.extend([
                "Try accessing TikTok in your browser first",
                "Some TikTok videos require account access"
            ])
    
    # General suggestions
    if 'timeout' in stderr_lower:
        suggestions.append("Try again - the server may be temporarily overloaded")
    elif 'network' in stderr_lower or 'connection' in stderr_lower:
        suggestions.append("Check your internet connection")
    
    return suggestions

@app.route("/download", methods=["POST"])
def download():
    """Enhanced download with platform-specific optimizations"""
    data = request.json
    url = data.get("url")
    format_id = data.get("format_id")
    media_type = data.get("media_type", "video")
    cookies = data.get("cookies")
    
    if not url:
        return jsonify({"error": "URL is required"}), 400
    
    # Get platform configuration
    platform = URLAnalyzer.detect_platform(url)
    platform_config = URLAnalyzer.get_platform_config(url)
    platform_config['platform'] = platform
    
    # Process cookies with platform-specific filtering
    cookie_string = None
    if URLAnalyzer.needs_cookies(url) and cookies:
        validated_cookies = CookieManager.validate_cookies(cookies)
        if platform != 'generic':
            validated_cookies = CookieManager.filter_essential_cookies(validated_cookies, platform)
        cookie_string = CookieManager.convert_to_netscape(validated_cookies)
        logger.info(f"Processed {len(validated_cookies)} cookies for {platform} download")
    
    # Build enhanced command template
    command_template = [YT_DLP_BIN]
    
    # Add format selection with platform-specific optimizations
    if format_id and format_id != "highest":
        command_template.extend(["-f", format_id])
    elif media_type == "video":
        if platform == 'youtube':
            # YouTube-specific format selection
            command_template.extend(["-f", "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4]/best"])
        elif platform in ['facebook', 'instagram']:
            # Social media platforms often have simpler format structures
            command_template.extend(["-f", "best[ext=mp4]/best"])
        else:
            # Generic best quality
            command_template.extend(["-f", "bestvideo+bestaudio/best"])
    elif media_type == "audio":
        # Enhanced audio extraction
        command_template.extend([
            "-x", "--audio-format", "mp3", 
            "--audio-quality", "0",  # Best quality
            "--embed-metadata"  # Include metadata
        ])
    
    # Add platform-specific arguments
    if platform == 'youtube':
        command_template.extend(["--write-description", "--write-info-json"])
    elif platform in ['facebook', 'instagram']:
        command_template.extend(["--no-check-certificate"])
    
    # Add progress and output options
    command_template.extend([
        "--newline", "--no-color", "--no-warnings",
        "--socket-timeout", "60"
    ])
    
    # Add URL
    command_template.append(url)
    
    # Detect playlist
    is_playlist = any(pattern in url.lower() for pattern in [
        'playlist?list=', '/playlist/', '/sets/', '/collection/',
        'album', 'playlist', 'list='
    ])
    
    download_dir = settings.get('download_save_directory')
    cancel_event = threading.Event()
    
    # Queue the download
    gui_message_queue.put({
        'type': 'add_download',
        'url': url,
        'status': 'Queued',
        'filename': f'Preparing {platform} download...',
        'timestamp': time.time(),
        'cancel_event': cancel_event
    })
    
    # Start enhanced download in background thread
    threading.Thread(
        target=_perform_yt_dlp_download,
        args=(command_template, url, is_playlist, media_type, False, download_dir, cancel_event, cookie_string, platform_config),
        daemon=True
    ).start()
    
    return jsonify({
        "message": f"Download started successfully for {platform} content!",
        "platform": platform,
        "url_analysis": {
            "platform": platform,
            "is_temporary": URLAnalyzer.is_temporary_url(url),
            "needs_cookies": URLAnalyzer.needs_cookies(url),
            "used_enhanced_cookies": bool(cookie_string)
        }
    })

@app.route('/set_browser_monitor_status', methods=['POST'])
def set_browser_monitor_status():
    data = request.json
    status = data.get('enabled')
    if isinstance(status, bool):
        settings.set('browser_monitor_enabled', status)
        return jsonify({'status': 'success', 'message': f'Browser monitoring set to {status}'})
    return jsonify({'status': 'error', 'message': 'Invalid status provided'}), 400

def start_flask_server():
    """Starts the Flask server."""
    try:
        app_wrapper = DispatcherMiddleware(app)
        run_simple('127.0.0.1', FLASK_PORT, app_wrapper, use_reloader=False, use_debugger=False, threaded=True)
    except Exception as e:
        logger.critical(f"Failed to start Flask server: {e}")
        gui_message_queue.put({'type': 'exit'})

def wait_for_flask_server(max_retries=20, delay=0.2):
    """Wait for Flask server to be ready by polling the health endpoint."""
    for i in range(max_retries):
        try:
            response = requests.get(f"http://localhost:{FLASK_PORT}/health", timeout=0.5)
            if response.status_code == 200 and response.json().get('status') == 'healthy':
                logger.info(f"Flask server is ready after {i+1} retries.")
                return True
        except requests.exceptions.RequestException:
            logger.debug(f"Attempt {i+1}/{max_retries}: Flask server not yet reachable.")
        time.sleep(delay)
    logger.error(f"Flask server did not become ready after {max_retries} retries.")
    return False

def format_bytes(bytes_val):
    if bytes_val == 0: return "0 B"
    units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
    i = 0
    while bytes_val >= 1024 and i < len(units) - 1:
        bytes_val /= 1024
        i += 1
    return f"{bytes_val:.2f} {units[i]}"

# --- Merged GUI Code ---

# gui.py

import sys
import os
import threading
import subprocess
import shutil
import queue
import requests
import time
from datetime import datetime
import logging

# Import from server file

# PySide6 imports
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QStackedWidget, QFrame, QFileDialog, QComboBox, 
    QLineEdit, QRadioButton, QButtonGroup, QDialog, QStatusBar,
    QTableView, QHeaderView, QAbstractItemView, QSplitter, QCheckBox
)
from PySide6.QtCore import (
    Qt, QTimer, QUrl, Signal, QModelIndex, QRect, QSize, QPoint,  QEasingCurve, Property, QPropertyAnimation
)
from PySide6.QtGui import QColor, QFont, QDesktopServices, QIcon, QPixmap, QPalette, QPaintEvent, QPainter

# Setup logger for the GUI
logger = logging.getLogger(__name__)

# --- Custom Widgets ---
# Custom Button for Sidebar with Hover Effect
class SidebarButton(QPushButton):
    def __init__(self, text, icon_svg=None, parent=None):
        super().__init__("", parent) # Initialize with empty text, will use icon
        self.original_text = text # Store original text for tooltip or accessibility
        self.icon_svg = icon_svg # Store SVG data
        self.setFixedSize(180, 40) # Fixed size for consistency
        self.setCursor(Qt.PointingHandCursor) # Hand cursor on hover
        
        # Layout for icon and text
        h_layout = QHBoxLayout(self)
        h_layout.setContentsMargins(15, 0, 0, 0) # Padding for icon/text
        h_layout.setSpacing(10)

        if self.icon_svg:
            self.icon_label = QLabel()
            self.set_svg_icon(self.icon_svg) # Set initial icon
            self.icon_label.setFixedSize(20, 20) # Fixed size for icon
            h_layout.addWidget(self.icon_label)
        else:
            self.icon_label = None

        self.text_label = QLabel(text)
        self.text_label.setStyleSheet("color: #adb5bd; font-size: 14px; font-weight: bold;")
        h_layout.addWidget(self.text_label)
        h_layout.addStretch(1) # Push icon/text to left

        self.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                border: none;
                text-align: left;
                border-radius: 8px; /* Rounded corners */
            }
            QPushButton:hover {
                background-color: #343a40; /* Darker gray on hover */
            }
            QPushButton:pressed {
                background-color: #2b3035; /* Even darker on press */
            }
            QPushButton:checked { /* Style for selected/active button */
                background-color: #495057; /* Slightly lighter dark for active */
                border-left: 3px solid #4dabf7; /* Accent border on left */
            }
            QPushButton:focus {
                outline: none; /* Remove dotted outline on focus */
            }
        """)
        self.setCheckable(True) # Make buttons checkable for selection feedback

    def set_svg_icon(self, svg_data, color="#adb5bd"):
        """Sets an SVG icon for the button, coloring it."""
        if not self.icon_label:
            return
        
        # Replace fill color in SVG data
        colored_svg = svg_data.replace('currentColor', color)
        
        pixmap = QPixmap()
        pixmap.loadFromData(colored_svg.encode('utf-8'))
        
        # Scale pixmap to desired icon size
        scaled_pixmap = pixmap.scaled(20, 20, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.icon_label.setPixmap(scaled_pixmap)

    def setChecked(self, checked):
        super().setChecked(checked)
        # Update icon color based on checked state
        if self.icon_label:
            if checked:
                self.set_svg_icon(self.icon_svg, color="#4dabf7") # Accent color when checked
                self.text_label.setStyleSheet("color: #4dabf7; font-size: 14px; font-weight: bold;")
            else:
                self.set_svg_icon(self.icon_svg, color="#adb5bd") # Original color when unchecked
                self.text_label.setStyleSheet("color: #adb5bd; font-size: 14px; font-weight: bold;")


# Define SVG Icons
ICONS = {
    "download": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-download"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" x2="12" y1="15" y2="3"/></svg>""",
    "film": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-film"><rect width="18" height="18" x="3" y="3" rx="2"/><path d="M7 3v18"/><path d="M3 7.5h18"/><path d="M3 12h18"/><path d="M3 16.5h18"/><path d="M17 3v18"/></svg>""",
    "headphones": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-headphones"><path d="M3 14h3a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-7a9 9 0 0 1 18 0v7a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3"/></svg>""",
    "list": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucude-list"><line x1="8" x2="21" y1="6" y2="6"/><line x1="8" x2="21" y1="12" y2="12"/><line x1="8" x2="21" y1="18" y2="18"/><line x1="3" x2="3.01" y1="6" y2="6"/><line x1="3" x2="3.01" y1="12" y2="12"/><line x1="3" x2="3.01" y1="18" y2="18"/></svg>""",
    "shuffle": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-shuffle"><path d="M21 16v-4a2 2 0 0 0-2-2H7l-4-4v14l4-4h12a2 2 0 0 0 2-2z"/></svg>""", # Using shuffle for convert
    "plus": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-plus"><line x1="12" x2="12" y1="5" y2="19"/><line x1="5" x2="19" y1="12" y2="12"/></svg>""",
    "menu": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-menu"><line x1="4" x2="20" y1="12" y2="12"/><line x1="4" x2="20" y1="6" y2="6"/><line x1="4" x2="20" y1="18" y2="18"/></svg>""",
    "search": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-search"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>""",
    "trash": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-trash-2"><path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/><line x1="10" x2="10" y1="11" y2="17"/><line x1="14" x2="14" y1="11" y2="17"/></svg>""",
    "play": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-play"><polygon points="5 3 19 12 5 21 5 3"/></svg>""",
    "folder-open": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-folder-open"><path d="M6 17a3 3 0 0 0 3 3h10a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-8a2 2 0 0 1-2-2V4a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2v10a3 3 0 0 0 3 3Z"/><path d="m10 12 1.293 1.293a1 1 0 0 0 1.414 0L14 12"/></svg>""",
    "refresh": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-refresh-cw"><path d="M21 12a9 9 0 0 0-9-9c-2.7 0-5.1 1.07-6.9 2.89M3 12a9 9 0 0 0 9 9c2.7 0 5.1-1.07 6.9-2.89M3 2v6h6M21 22v-6h-6"/></svg>""",
    "x-circle": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-x-circle"><circle cx="12" cy="12" r="10"/><path d="m15 9-6 6"/><path d="m9 9 6 6"/></svg>""",
    "arrow-up": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-arrow-up"><path d="M12 19V5"/><path d="m5 12 7-7 7 7"/></svg>""",
    "arrow-down": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-arrow-down"><path d="M12 5v14"/><path d="m19 12-7 7-7-7"/></svg>"""
}

# Custom QLineEdit with an integrated search icon
class SearchLineEdit(QLineEdit):
    # Added main_window_instance parameter
    def __init__(self, main_window_instance, parent=None):
        super().__init__(parent)
        self.main_window_instance = main_window_instance # Store the MainWindow instance
        self.setPlaceholderText("Search...")
        # Removed setFixedSize to allow dynamic resizing
        self.setFixedHeight(30) # Keep fixed height for consistency

        # Create a QPushButton for the icon
        self.search_icon_button = QPushButton(self)
        self.set_search_icon() # Set the icon
        self.search_icon_button.setFixedSize(24, 24) # Icon button size (slightly larger for click target)
        self.search_icon_button.setCursor(Qt.PointingHandCursor) # Hand cursor on hover
        self.search_icon_button.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                border: none;
                padding: 0px; /* Remove padding from button itself */
            }
            QPushButton:hover {
                background-color: #495057; /* Subtle hover effect */
                border-radius: 12px; /* Make it round on hover */
            }
            QPushButton:pressed {
                background-color: #3a3a3a;
            }
        """)

        # Set text margins to make space for the icon button
        # Left margin for text, right margin for icon button
        self.setTextMargins(10, 1, self.search_icon_button.width() + 5, 1)

        # Apply stylesheet for the QLineEdit itself
        self.setStyleSheet("""
            QLineEdit {
                background-color: #3a3a3a;
                border: 1px solid #495057;
                border-radius: 7px; /* Rounded corners for search bar */
                padding: 5px 15px; /* Adjust padding for text within the line edit */
                color: #e0e0e0;
            }
            QLineEdit:focus {
                border: 1px solid #4dabf7;
            }
        """)
        
        # Connect text changed signal for live filtering
        self.textChanged.connect(self.main_window_instance.filter_displayed_items)
        # Connect the icon button's clicked signal to trigger search explicitly
        self.search_icon_button.clicked.connect(lambda: self.main_window_instance.filter_displayed_items(self.text()))

    def set_search_icon(self, color="#adb5bd"):
        """Sets the search icon for the QPushButton."""
        search_icon_svg = ICONS["search"]
        colored_svg = search_icon_svg.replace('currentColor', color)
        
        pixmap = QPixmap()
        pixmap.loadFromData(colored_svg.encode('utf-8'))
        
        # Scale pixmap to desired icon size (slightly smaller than button for padding)
        scaled_pixmap = pixmap.scaled(16, 16, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.search_icon_button.setIcon(QIcon(scaled_pixmap))
        self.search_icon_button.setIconSize(scaled_pixmap.size())

    def resizeEvent(self, event):
        """Override resizeEvent to position the icon button correctly."""
        super().resizeEvent(event)
        # Position the icon button at the right, with some padding
        icon_x = self.width() - self.search_icon_button.width() - 5 # 5px from right edge
        icon_y = (self.height() - self.search_icon_button.height()) // 2 # Vertically center
        self.search_icon_button.move(icon_x, icon_y)



# --- Custom Checkbox (from QCustomCheckBox.py) ---
# Update the QCustomCheckBox class with the fixed paintEvent method
class QCustomCheckBox(QCheckBox):
    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)

        # COLORS - Adjusted to match the dark theme
        self.bgColor = QColor("#3a3a3a") # Background color of the unchecked track
        self._circleColor = QColor("#e0e0e0") # Color of the circle (thumb)
        self._activeColor = QColor("#4dabf7") # Color of the checked track

        # Animation
        self.animationEasingCurve = QEasingCurve.Type.InOutQuad
        self.animationDuration = 200
        self.pos = 0.0 # Changed to float for smoother animation
        self.animation = QPropertyAnimation(self, b"position")
        self.animation.setEasingCurve(self.animationEasingCurve)
        self.animation.setDuration(self.animationDuration)
        self.stateChanged.connect(self.setup_animation)

        # Label for the text
        self.label = QLabel(self)
        self.label.setWordWrap(True)
        self.label.setStyleSheet("color: #e0e0e0;") # Text color for the label
        self.setText(text) # Set initial text

        # Icon (not used in this specific implementation, but kept for future use)
        self.icon = QIcon()
        self._iconSize = QSize(0, 0)

        self.setFixedHeight(25) # Set a default height for the checkbox

    @Property(QColor)
    def backgroundColor(self):
        return self.bgColor

    @backgroundColor.setter
    def backgroundColor(self, color):
        self.bgColor = color
        self.update()

    @Property(QColor)
    def circleColor(self):
        return self._circleColor

    @circleColor.setter
    def circleColor(self, color):
        self._circleColor = color
        self.update()

    @Property(QColor)
    def activeColor(self):
        return self._activeColor

    @activeColor.setter
    def activeColor(self, color):
        self._activeColor = color
        self.update()

    def setIcon(self, icon):
        self.icon = icon
        self.update()

    def setIconSize(self, size):
        self._iconSize = size
        self.update()

    def customizeQCustomCheckBox(self, **customValues):
        if "bgColor" in customValues:
            self.bgColor = customValues["bgColor"]
        if "circleColor" in customValues:
            self._circleColor = customValues["circleColor"]
        if "activeColor" in customValues:
            self._activeColor = customValues["activeColor"]
        if "animationEasingCurve" in customValues:
            self.animationEasingCurve = customValues["animationEasingCurve"]
            self.animation.setEasingCurve(self.animationEasingCurve)
        if "animationDuration" in customValues:
            self.animationDuration = customValues["animationDuration"]
            self.animation.setDuration(self.animationDuration)
        self.update()

    def showEvent(self, e):
        super().showEvent(e)
        self.adjustWidgetSize()
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.adjustWidgetSize()

    def adjustWidgetSize(self):
        # Calculate dimensions based on height for a consistent look
        track_width = self.height() * 2.0 # Slightly adjusted for better proportion
        thumb_size = self.height() - 6 # Padding for the thumb inside the track
        
        # Position the label next to the track
        label_margin_left = 10 # Space between track and label
        label_x = int(track_width + label_margin_left)
        label_width = max(0, self.width() - label_x)
        self.label.setGeometry(label_x, 0, label_width, self.height())
        self.update()

    def setText(self, text):
        self.label.setText(text)
        self.adjustWidgetSize()

    def text(self):
        return self.label.text()

    @Property(float)
    def position(self):
        return self.pos

    @position.setter
    def position(self, pos):
        self.pos = pos
        self.update()

    def setup_animation(self, value):
        # Calculate start and end values for the thumb's position
        # Thumb moves from left (margin) to right (track_width - thumb_size - margin)
        margin = 3 # Margin from the edge of the track
        track_width = self.height() * 2.0
        thumb_size = self.height() - 6

        start_pos = margin
        end_pos = track_width - thumb_size - margin

        self.animation.stop()
        self.animation.setStartValue(float(self.pos)) # Start from current position
        self.animation.setEndValue(float(end_pos if value else start_pos))
        self.animation.start()

    def hitButton(self, pos: QPoint):
        # Make the entire widget clickable
        return self.contentsRect().contains(pos)

    def paintEvent(self, e: QPaintEvent):
        # Create and configure painter
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.NoPen)
        
        # Fill background with transparent color
        painter.setBrush(QColor(0, 0, 0, 0))  # Fully transparent
        painter.drawRect(self.rect())
        
        # Draw the track
        track_width = self.height() * 2.0
        track_height = self.height()
        track_radius = track_height / 2
        
        # Draw the thumb (circle)
        thumb_size = self.height() - 6  # 3px padding on each side
        thumb_y = (track_height - thumb_size) / 2

        # FIX #8: Cast float position to int for drawing
        thumb_x = int(self.pos)

        if self.isChecked():
            # Checked state: active color for track, circle on the right
            painter.setBrush(self._activeColor)
            painter.drawRoundedRect(0, 0, track_width, track_height, track_radius, track_radius)
            painter.setBrush(self._circleColor)
            painter.drawEllipse(thumb_x, thumb_y, thumb_size, thumb_size)
        else:
            # Unchecked state: background color for track, circle on the left
            painter.setBrush(self.bgColor)
            painter.drawRoundedRect(0, 0, track_width, track_height, track_radius, track_radius)
            painter.setBrush(self._circleColor)
            painter.drawEllipse(thumb_x, thumb_y, thumb_size, thumb_size)

        painter.end()


# New CustomHeaderView class to manage sort indicator as a QLabel
class CustomHeaderView(QHeaderView):
    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self.setSectionsClickable(True)
        self.setSortIndicatorShown(False) # Hide the default sort indicator
        
        self.sort_indicator_label = QLabel(self) # Create a label to hold the arrow icon
        self.sort_indicator_label.hide() # Hide it initially
        self.sort_indicator_label.setFixedSize(10, 10) # Size for the arrow icon
        self.sort_indicator_label.setAlignment(Qt.AlignCenter) # Center the icon in the label
        self.sort_indicator_label.setStyleSheet("background-color: transparent;") # Ensure no background interferes
        
        # Connect to the section clicked signal to update the sort indicator
        self.sectionClicked.connect(self._update_sort_indicator_position)
        self.sectionResized.connect(self._update_sort_indicator_position)
        self.sectionMoved.connect(self._update_sort_indicator_position)
        
        print("DEBUG: CustomHeaderView initialized.")

    def _update_sort_indicator_position(self):
        sort_column = self.sortIndicatorSection() 
        sort_order = self.sortIndicatorOrder()

        if sort_column != -1: # If a column is sorted
            # Get the full rectangle for the sorted section
            section_x = self.sectionViewportPosition(sort_column)
            section_y = 0 # Relative to the header view's own top edge
            section_width = self.sectionSize(sort_column)
            section_height = self.height() # The height of the entire header view
            
            section_rect = QRect(section_x, section_y, section_width, section_height)

            # Calculate position for the label:
            # X: Center the label horizontally within the section
            label_x = section_rect.left() + (section_rect.width() - self.sort_indicator_label.width()) // 2
            
            # Y: Position the label at the top of the section with a small margin
            # We want the arrow to be at the very top, and the text below it.
            # Let's say we want 5px padding from the top of the header.
            label_y = section_rect.top() + 2 # 5px from the top of the section
            
            self.sort_indicator_label.move(label_x, label_y)
            
            # Set the appropriate arrow icon
            if sort_order == Qt.AscendingOrder:
                self._set_arrow_icon(ICONS["arrow-up"])
            elif sort_order == Qt.DescendingOrder:
                self._set_arrow_icon(ICONS["arrow-down"])
            
            self.sort_indicator_label.show()
            
            # Request a repaint of the header to ensure the label is drawn correctly
            self.viewport().update() 
        else:
            self.sort_indicator_label.hide() # Hide if no column is sorted
            self.viewport().update() # Request repaint to clear hidden label

    def _set_arrow_icon(self, svg_data, color="#d1d1d1"): # Use header text color
        """Sets an SVG icon for the sort indicator label, coloring it."""
        # Replace fill color in SVG data
        colored_svg = svg_data.replace('currentColor', color)
        
        pixmap = QPixmap()
        pixmap.loadFromData(colored_svg.encode('utf-8'))
        
        # Scale pixmap to desired icon size
        scaled_pixmap = pixmap.scaled(self.sort_indicator_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.sort_indicator_label.setPixmap(scaled_pixmap)

    # --- FIX: REMOVED THESE OVERRIDES ---
    # def sortIndicatorSection(self):
    #     if self.model() and self.model().isSortingEnabled():
    #         return self.model().sortColumn()
    #     return -1 # No column sorted

    # def sortIndicatorOrder(self):
    #     if self.model() and self.model().isSortingEnabled():
    #         return self.model().sortOrder()
    #     return Qt.AscendingOrder # Default
    # --- END FIX ---

    def setModel(self, model):
        super().setModel(model)
        # Re-evaluate sort indicator position when model changes (e.g., when data is filtered)
        self._update_sort_indicator_position()


from PySide6.QtCore import QAbstractTableModel


class DownloadTableModel(QAbstractTableModel):
    def __init__(self, data, is_completed_model=False, parent=None):
        super().__init__(parent)
        self._data = data
        self.is_completed_model = is_completed_model
        # Define headers based on whether it's an active or completed model
        if self.is_completed_model:
            self.header_labels = ["Name", "Date", "Size", "Type", "Location"]
        else:
            self.header_labels = ["Name", "Date", "Size", "Progress", "Status"]

    def rowCount(self, parent=QModelIndex()):
        return len(self._data)

    def columnCount(self, parent=QModelIndex()):
        return len(self.header_labels)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        if role == Qt.DisplayRole:
            try:
                item = self._data[index.row()]
                column = index.column()

                if self.header_labels[column] == "Name":
                    return item.get('filename', item.get('url', 'N/A'))
                elif self.header_labels[column] == "Date":
                    timestamp = item.get('timestamp')
                    if timestamp:
                        return datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M')
                    return "N/A"
                elif self.header_labels[column] == "Size":
                    filesize_bytes = item.get('filesize_bytes', 0)
                    return format_bytes(filesize_bytes)
                elif self.header_labels[column] == "Progress": # For active downloads
                    return item.get('progress', '0%')
                elif self.header_labels[column] == "Type": # For completed downloads
                    file_type = item.get('filetype', 'Unknown').capitalize()
                    return file_type
                elif self.header_labels[column] == "Status":
                    return item.get('status', 'N/A')
                elif self.header_labels[column] == "Location":
                    path = item.get('path', 'N/A')
                    return os.path.dirname(path) if path != 'N/A' else 'N/A'
            except IndexError:
                # This can happen briefly during filtering; it's safe to ignore.
                return None
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.header_labels[section]
        return None

    def sort(self, column, order):
        self.layoutAboutToBeChanged.emit()
        
        column_name = self.header_labels[column]
        
        # Define a key function for sorting
        def get_sort_key(item):
            if column_name == "Name":
                return item.get('filename', item.get('url', '')).lower()
            elif column_name == "Date":
                return item.get('timestamp', 0)
            elif column_name == "Size":
                return item.get('filesize_bytes', 0)
            elif column_name == "Progress":
                progress_str = item.get('progress', '0%').strip('%')
                try:
                    return float(progress_str)
                except ValueError:
                    return 0
            elif column_name == "Status":
                return item.get('status', '').lower()
            elif column_name == "Type":
                return item.get('filetype', '').lower()
            elif column_name == "Location":
                path = item.get('path', '')
                return os.path.dirname(path).lower()
            return None

        self._data.sort(key=get_sort_key, reverse=(order == Qt.DescendingOrder))
        
        self.layoutChanged.emit()

    def addItem(self, item_data):
        """Adds a new item to the model."""
        self.beginInsertRows(QModelIndex(), len(self._data), len(self._data))
        self._data.append(item_data)
        self.endInsertRows()

    def updateItem(self, url, new_data):
        """Updates data for a specific row identified by URL and emits dataChanged signal."""
        # Iterate through the _data (which is the currently displayed/filtered data)
        for row, item in enumerate(self._data):
            if item.get('url') == url:
                # Update only the changed fields
                for key, value in new_data.items():
                    item[key] = value
                # Emit dataChanged for the entire row to ensure all columns update
                self.dataChanged.emit(self.index(row, 0), self.index(row, self.columnCount() - 1))
                return True # Item found and updated
        return False # Item not found in the currently displayed data

    def removeItem(self, url):
        for row, item in enumerate(self._data):
            if item.get('url') == url:
                self.beginRemoveRows(QModelIndex(), row, row)
                del self._data[row]
                self.endRemoveRows()
                return True
        return False

    def clearAll(self):
        self.beginResetModel()
        self._data.clear()
        self.endResetModel()

    def setFilteredData(self, filtered_data):
        """Properly sets filtered data and notifies views."""
        self.beginResetModel()
        self._data = filtered_data
        self.endResetModel()

    def getData(self):
        """Returns a copy of the current data."""
        return self._data[:] # Return a copy to prevent external modification

# Dialog for adding a new download
class AddDownloadDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add New Download")
        self.setFixedSize(500, 250) 
        self.setModal(True)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)

        self.init_ui()
        self.apply_dialog_style()

    def apply_dialog_style(self):
        self.setStyleSheet("""
            QDialog {
                background-color: #2d2d2d;
                border-radius: 10px;
                border: 1px solid #495057;
            }
            QLabel {
                color: #e0e0e0;
                font-size: 13px;
            }
            QLineEdit {
                background-color: #3a3a3a;
                border: 1px solid #495057;
                border-radius: 5px;
                padding: 5px;
                color: #e0e0e0;
            }
            QLineEdit:focus {
                border: 1px solid #4dabf7;
            }
            QRadioButton::indicator {
                width: 16px;
                height: 16px;
                border-radius: 8px;
                border: 2px solid #495057;
                background-color: #2d2d2d;
            }
            QRadioButton::indicator:checked {
                background-color: #4dabf7;
                border: 2px solid #4dabf7;
            }
            QRadioButton {
                color: #e0e0e0;
            }
            QPushButton {
                background-color: #4dabf7;
                color: white;
                border: none;
                border-radius: 5px;
                padding: 8px 15px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #3b8fcc;
            }
            QPushButton#cancelButton {
                background-color: #6c757d;
            }
            QPushButton#cancelButton:hover {
                background-color: #5a6268;
            }
            QPushButton:focus {
                outline: none;
            }
        """)

    def init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(15)

        # URL input
        url_label = QLabel("Enter URL:")
        main_layout.addWidget(url_label)
        
        self.url_entry = QLineEdit()
        self.url_entry.setPlaceholderText("Video, Audio, or Playlist URL")
        self.url_entry.setToolTip("Paste the URL of the video, audio, or playlist you want to download.")
        main_layout.addWidget(self.url_entry)

        url_help_label = QLabel("Example: https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        url_help_label.setStyleSheet("color: #adb5bd; font-size: 11px;")
        main_layout.addWidget(url_help_label)
        
        # Media type selection
        type_label = QLabel("Select Media Type:")
        main_layout.addWidget(type_label)
        
        media_type_layout = QHBoxLayout()
        self.media_type_group = QButtonGroup(self)
        
        self.radio_video = QRadioButton("Video")
        self.radio_video.setChecked(True)
        self.media_type_group.addButton(self.radio_video)
        media_type_layout.addWidget(self.radio_video)
        
        self.radio_audio = QRadioButton("Audio")
        self.media_type_group.addButton(self.radio_audio)
        media_type_layout.addWidget(self.radio_audio)
        
        media_type_layout.addStretch(1)
        main_layout.addLayout(media_type_layout)

        # --- FIX: Add stretch to improve layout ---
        main_layout.addStretch(1)

        # Action buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch(1)
        
        self.ok_button = QPushButton("Start Download")
        self.ok_button.setCursor(Qt.PointingHandCursor)
        self.ok_button.clicked.connect(self.accept)
        button_layout.addWidget(self.ok_button)
        
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("cancelButton")
        self.cancel_button.setCursor(Qt.PointingHandCursor)
        self.cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(self.cancel_button)
        
        main_layout.addLayout(button_layout)


# Custom Confirmation Dialog (replaces QMessageBox)
class ConfirmationDialog(QDialog):
    def __init__(self, message, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Confirm Action")
        self.setFixedSize(350, 150)
        self.setModal(True)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        message_label = QLabel(message)
        message_label.setAlignment(Qt.AlignCenter)
        message_label.setWordWrap(True)
        layout.addWidget(message_label)

        button_layout = QHBoxLayout()
        button_layout.addStretch(1)

        self.yes_button = QPushButton("Yes")
        self.yes_button.setFixedSize(80, 30)
        self.yes_button.clicked.connect(self.accept)
        button_layout.addWidget(self.yes_button)

        self.no_button = QPushButton("No")
        self.no_button.setObjectName("cancelButton") # Use same style as other cancel buttons
        self.no_button.setFixedSize(80, 30)
        self.no_button.clicked.connect(self.reject)
        button_layout.addWidget(self.no_button)

        layout.addLayout(button_layout)
        self.apply_dialog_style()

    def apply_dialog_style(self):
        self.setStyleSheet("""
            QDialog {
                background-color: #2d2d2d;
                border-radius: 10px;
                border: 1px solid #495057;
            }
            QLabel {
                color: #e0e0e0;
                font-size: 13px;
            }
            QPushButton {
                background-color: #4dabf7;
                color: white;
                border: none;
                border-radius: 5px;
                padding: 8px 15px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #3b8fcc;
            }
            QPushButton#cancelButton {
                background-color: #6c757d;
            }
            QPushButton#cancelButton:hover {
                background-color: #5a6268;
            }
            QPushButton:focus {
                outline: none;
            }
        """)


# Main Application Window
class MainWindow(QMainWindow):
    # Signals for updating GUI from Flask thread
    add_download_signal = Signal(dict)
    update_download_status_signal = Signal(str, dict) # url, new_data_dict
    add_completed_signal = Signal(dict)
    # add_conversion_signal = Signal(dict) # Commented out
    # update_conversion_status_signal = Signal(str, str, str, str) # input_path, status, progress, message # Commented out
    show_status_signal = Signal(str, str, int) # message, type
    set_buttons_disabled_signal = Signal(bool)

    def __init__(self):
        # ... (your existing __init__ method content) ...
        super().__init__()
        logger.debug(f"MainWindow initialized. Initial browser_monitor_enabled: {settings.get('browser_monitor_enabled')}")
        self.setWindowTitle("Universal Media Tool")
        self.setGeometry(100, 100, 1000, 700)
        self.setMinimumSize(800, 600)

        # Initialize data models
        self.active_downloads_data = [] # Raw data list for filtering
        self.active_downloads_model = DownloadTableModel(self.active_downloads_data, is_completed_model=False)

        self.completed_videos_data = []
        self.completed_videos_model = DownloadTableModel(self.completed_videos_data, is_completed_model=True)
        
        self.completed_audios_data = []
        self.completed_audios_model = DownloadTableModel(self.completed_audios_data, is_completed_model=True)
        
        self.completed_playlists_data = []
        self.completed_playlists_model = DownloadTableModel(self.completed_playlists_data, is_completed_model=True)

        # Dictionary to store active download processes and their cancellation events
        # Key: URL, Value: {'process': subprocess.Popen object, 'cancel_event': threading.Event}
        self.active_processes = {}
        
        self.current_panel_type = "active_downloads" # Initialize panel type early
        
        # NOTE: init_ui is called here, so all methods it calls must be defined BEFORE init_ui
        self.init_ui() 
        self.apply_stylesheet()
        self.connect_signals()

        self.status_lock = threading.Lock() # For thread-safe status bar updates

        # Timer for clearing status bar message
        self.status_clear_timer = QTimer(self)
        self.status_clear_timer.setSingleShot(True)
        self.status_clear_timer.timeout.connect(self._clear_status_bar)

        # Start a QTimer to periodically check the Flask message queue
        self.queue_timer = QTimer(self)
        self.queue_timer.timeout.connect(self.check_flask_message_queue)
        self.queue_timer.start(100) # Check every 100 ms

        # Show default panel and check its button AFTER all UI elements are initialized
        self.show_panel(self.active_downloads_panel)
        self.active_downloads_button.setChecked(True)


    # --- Panel Creation Functions (DEFINED WITHIN MainWindow) ---
    def create_active_downloads_panel(self):
        """Creates the panel for displaying active downloads."""
        panel = QWidget()
        panel.setObjectName("ActiveDownloadsPanel")
        panel.setLayout(QVBoxLayout())
        panel.layout().setContentsMargins(20, 20, 20, 20)
        panel.layout().setSpacing(0)
        
        self.active_downloads_table_view = QTableView()
        self.active_downloads_table_view.setModel(self.active_downloads_model)
        self.active_downloads_table_view.setSortingEnabled(True)
        
        # --- Use CustomHeaderView directly for the horizontal header ---
        custom_header = CustomHeaderView(Qt.Horizontal, self.active_downloads_table_view)
        self.active_downloads_table_view.setHorizontalHeader(custom_header)
        
        custom_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        custom_header.setStretchLastSection(True)
        
        self.active_downloads_table_view.verticalHeader().setVisible(False)
        self.active_downloads_table_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.active_downloads_table_view.setAlternatingRowColors(True)
        self.active_downloads_table_view.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.active_downloads_table_view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.active_downloads_table_view.setFocusPolicy(Qt.NoFocus)

        # No setItemDelegate needed. CustomHeaderView handles sort indicator with QLabel.
        # custom_header.setSectionsClickable(True) is handled in CustomHeaderView __init__
        
        self.active_downloads_table_view.setColumnWidth(0, 300) # Name
        self.active_downloads_table_view.setColumnWidth(1, 150) # Date
        self.active_downloads_table_view.setColumnWidth(2, 100) # Size
        self.active_downloads_table_view.setColumnWidth(3, 100) # Progress
        
        panel.layout().addWidget(self.active_downloads_table_view)
        
        return panel

    def create_completed_panel(self, title, file_type):
        """Creates a generic panel for displaying completed downloads (videos, audios, playlists)."""
        panel = QWidget()
        panel.setObjectName(f"Completed{title}Panel")
        panel.setLayout(QVBoxLayout())
        panel.layout().setContentsMargins(20, 20, 20, 20)
        panel.layout().setSpacing(0)
        
        table_view = QTableView()
        if file_type == "video":
            table_view.setModel(self.completed_videos_model)
        elif file_type == "audio":
            table_view.setModel(self.completed_audios_model)
        elif file_type == "playlist":
            table_view.setModel(self.completed_playlists_model)
            
        table_view.setSortingEnabled(True)
        
        # --- Use CustomHeaderView directly for the horizontal header ---
        custom_header = CustomHeaderView(Qt.Horizontal, table_view)
        table_view.setHorizontalHeader(custom_header)
        
        custom_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        custom_header.setStretchLastSection(True)
        
        table_view.verticalHeader().setVisible(False)
        table_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table_view.setAlternatingRowColors(True)
        table_view.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        table_view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        table_view.setFocusPolicy(Qt.NoFocus)

        # No setItemDelegate needed. CustomHeaderView handles sort indicator with QLabel.
        # custom_header.setSectionsClickable(True) is handled in CustomHeaderView __init__
        
        table_view.setColumnWidth(0, 300) # Name
        table_view.setColumnWidth(1, 150) # Date
        table_view.setColumnWidth(2, 100) # Size
        table_view.setColumnWidth(3, 100) # Type

        panel.layout().addWidget(table_view)
        
        # Store reference to update later
        if file_type == "video":
            self.completed_videos_table_view = table_view
        elif file_type == "audio":
            self.completed_audios_table_view = table_view
        elif file_type == "playlist":
            self.completed_playlists_table_view = table_view
            
        return panel

    def create_coming_soon_panel(self, tool_name):
        """Creates a generic 'Coming Soon' placeholder panel."""
        panel = QWidget()
        panel.setObjectName(f"{tool_name.replace(' ', '')}ComingSoonPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)
        layout.setAlignment(Qt.AlignCenter)

        coming_soon_label = QLabel("Coming Soon")
        font = QFont()
        font.setPointSize(24)
        font.setBold(True)
        coming_soon_label.setFont(font)
        coming_soon_label.setStyleSheet("color: #adb5bd;")
        
        tool_name_label = QLabel(f"The Media Convert feature is currently under development.")
        tool_name_label.setStyleSheet("color: #adb5bd; font-size: 14px;")

        layout.addWidget(coming_soon_label, alignment=Qt.AlignCenter)
        layout.addWidget(tool_name_label, alignment=Qt.AlignCenter)

        return panel

    """
    # This entire method is commented out but kept for future reference
    def create_conversion_panel(self):
        \"\"\"Creates the media conversion panel.\"\"\"
        panel = QWidget()
        panel.setObjectName("ConversionPanel")
        panel.setLayout(QVBoxLayout())
        panel.layout().setContentsMargins(20, 20, 20, 20)
        panel.layout().setSpacing(15)
        
        input_frame = QFrame()
        input_frame.setLayout(QHBoxLayout())
        input_frame.layout().setContentsMargins(0, 0, 0, 0)
        
        input_label = QLabel("Input File:")
        input_label.setFixedWidth(100)
        input_frame.layout().addWidget(input_label)
        
        self.conversion_input_file_entry = QLineEdit()
        self.conversion_input_file_entry.setPlaceholderText("Select file to convert")
        self.conversion_input_file_entry.setReadOnly(True)
        input_frame.layout().addWidget(self.conversion_input_file_entry)
        
        self.browse_input_file_button = QPushButton("Browse...")
        self.browse_input_file_button.setFixedSize(80, 30)
        self.browse_input_file_button.setStyleSheet("QPushButton:focus { outline: none; }")
        self.browse_input_file_button.setCursor(Qt.PointingHandCursor)
        self.browse_input_file_button.clicked.connect(self.browse_input_file)
        input_frame.layout().addWidget(self.browse_input_file_button)
        panel.layout().addWidget(input_frame)

        input_help_label = QLabel("Select a video or audio file from your computer to convert.")
        input_help_label.setStyleSheet("color: #adb5bd; font-size: 11px; margin-left: 105px;")
        input_help_label.setWordWrap(True)
        panel.layout().addWidget(input_help_label)
        
        output_format_frame = QFrame()
        output_format_frame.setLayout(QHBoxLayout())
        output_format_frame.layout().setContentsMargins(0, 0, 0, 0)
        
        output_format_label = QLabel("Output Format:")
        output_format_label.setFixedWidth(100)
        output_format_frame.layout().addWidget(output_format_label)
        
        self.conversion_output_format_options = ["mp4", "mp3", "wav", "flac", "avi", "mov", "mkv", "webm", "ogg"]
        self.output_format_dropdown = QComboBox()
        self.output_format_dropdown.addItems(self.conversion_output_format_options)
        self.output_format_dropdown.setCurrentText("mp4")
        output_format_frame.layout().addWidget(self.output_format_dropdown)
        output_format_frame.layout().addStretch(1)
        panel.layout().addWidget(output_format_frame)

        self.codec_label_convert = QLabel("Output Video Codec:")
        panel.layout().addWidget(self.codec_label_convert)

        video_codec_options_convert = ["h264 (High Compatibility)", "h265 (High Efficiency)", "copy (Original Codec - may have playback issues with HDR/AV1)"]
        self.video_codec_dropdown = QComboBox()
        self.video_codec_dropdown.addItems(video_codec_options_convert)
        self.video_codec_dropdown.setCurrentText("h264 (High Compatibility)")
        self.video_codec_dropdown.setToolTip(
            "Choose a codec for video conversions.\\n"
            "H.264: Widely compatible, good quality.\\n"
            "H.265: More efficient, smaller files, but less compatible.\\n"
            "Copy: Keeps original codec (e.g., AV1). May cause playback issues on some players, especially with HDR."
        )
        panel.layout().addWidget(self.video_codec_dropdown)

        self.output_format_dropdown.currentIndexChanged.connect(self._toggle_codec_dropdown_convert)
        self._toggle_codec_dropdown_convert()

        format_help_label = QLabel("Choose the desired output format (e.g., mp4 for video, mp3 for audio).")
        format_help_label.setStyleSheet("color: #adb5bd; font-size: 11px; margin-left: 105px;")
        format_help_label.setWordWrap(True)
        panel.layout().addWidget(format_help_label)
        
        output_dir_frame = QFrame()
        output_dir_frame.setLayout(QHBoxLayout())
        output_dir_frame.layout().setContentsMargins(0, 0, 0, 0)
        
        output_dir_label = QLabel("Output Directory:")
        output_dir_label.setFixedWidth(100)
        output_dir_frame.layout().addWidget(output_dir_label)
        
        self.conversion_output_dir_entry = QLineEdit()
        self.conversion_output_dir_entry.setReadOnly(True)
        self.conversion_output_dir_entry.setText(self.convert_output_directory)
        output_dir_frame.layout().addWidget(self.conversion_output_dir_entry)
        
        self.browse_output_dir_button = QPushButton("Browse...")
        self.browse_output_dir_button.setFixedSize(80, 30)
        self.browse_output_dir_button.setStyleSheet("QPushButton:focus { outline: none; }")
        self.browse_output_dir_button.setCursor(Qt.PointingHandCursor)
        self.browse_output_dir_button.clicked.connect(self.browse_output_directory)
        output_dir_frame.layout().addWidget(self.browse_output_dir_button)
        panel.layout().addWidget(output_dir_frame)

        output_dir_help_label = QLabel("The converted file will be saved in this folder.")
        output_dir_help_label.setStyleSheet("color: #adb5bd; font-size: 11px; margin-left: 105px;")
        output_dir_help_label.setWordWrap(True)
        panel.layout().addWidget(output_dir_help_label)
        
        button_layout = QHBoxLayout()
        button_layout.addStretch(1)
        
        self.start_conversion_button = QPushButton("Start Conversion")
        self.start_conversion_button.setFixedSize(150, 40)
        self.start_conversion_button.setStyleSheet("QPushButton:focus { outline: none; }")
        self.start_conversion_button.setCursor(Qt.PointingHandCursor)
        self.start_conversion_button.clicked.connect(self.start_conversion_process)
        button_layout.addWidget(self.start_conversion_button)
        
        self.back_button_convert = QPushButton("Back")
        self.back_button_convert.setFixedSize(100, 30)
        self.back_button_convert.setStyleSheet("QPushButton:focus { outline: none; }")
        self.back_button_convert.setCursor(Qt.PointingHandCursor)
        self.back_button_convert.clicked.connect(lambda: self.show_panel(self.settings_panel))
        button_layout.addWidget(self.back_button_convert)
        panel.layout().addLayout(button_layout)

        panel.layout().addStretch(1)
        
        return panel
    """


    def create_settings_panel(self):
        """Creates the application settings panel."""
        panel = QWidget()
        panel.setObjectName("SettingsPanel")
        panel.setLayout(QVBoxLayout())
        panel.layout().setContentsMargins(20, 20, 20, 20)
        panel.layout().setSpacing(15)

        self.browser_monitor_switch = QCustomCheckBox("Enable Browser Monitoring", self)
        # --- THIS IS THE CRITICAL FIX: Use settings.get() ---
        self.browser_monitor_switch.setChecked(settings.get('browser_monitor_enabled')) 
        # --- END CRITICAL FIX ---
        self.browser_monitor_switch.setFixedHeight(25)
        self.browser_monitor_switch.stateChanged.connect(self.toggle_browser_monitor) 
        panel.layout().addWidget(self.browser_monitor_switch)

        monitor_help_label = QLabel("Allows the browser extension to send download requests to the app.")
        monitor_help_label.setStyleSheet("color: #adb5bd; font-size: 11px; margin-left: 25px;")
        monitor_help_label.setWordWrap(True)
        panel.layout().addWidget(monitor_help_label)
        
        panel.layout().addSpacing(20)

        self.go_to_extension_button = QPushButton("Browser Extension Setup")
        self.go_to_extension_button.setFixedSize(250, 30)
        self.go_to_extension_button.setStyleSheet("QPushButton:focus { outline: none; }")
        self.go_to_extension_button.setCursor(Qt.PointingHandCursor)
        self.go_to_extension_button.clicked.connect(lambda: self.show_panel(self.extension_setup_panel))
        panel.layout().addWidget(self.go_to_extension_button)

        self.go_to_download_settings_button = QPushButton("Download Settings")
        self.go_to_download_settings_button.setFixedSize(250, 30)
        self.go_to_download_settings_button.setStyleSheet("QPushButton:focus { outline: none; }")
        self.go_to_download_settings_button.setCursor(Qt.PointingHandCursor)
        self.go_to_download_settings_button.clicked.connect(lambda: self.show_panel(self.download_settings_panel))
        panel.layout().addWidget(self.go_to_download_settings_button)

        panel.layout().addStretch(1)
        
        return panel


    def create_extension_setup_panel(self):
        """Creates the panel for setting up the browser extension."""
        panel = QWidget()
        panel.setObjectName("ExtensionSetupPanel")
        panel.setLayout(QVBoxLayout())
        panel.layout().setContentsMargins(20, 20, 20, 20)
        panel.layout().setSpacing(15)

        path_frame = QFrame()
        path_frame.setLayout(QHBoxLayout())
        path_frame.layout().setContentsMargins(0, 0, 0, 0)
        
        path_label = QLabel("Extraction Path:")
        path_label.setFixedWidth(100)
        path_frame.layout().addWidget(path_label)
        
        self.extension_path_entry = QLineEdit()
        self.extension_path_entry.setText(os.path.join(os.path.expanduser("~"), "Downloads", "universal_media_tool_extension"))
        path_frame.layout().addWidget(self.extension_path_entry)
        
        self.browse_extension_dir_button = QPushButton("Browse...")
        self.browse_extension_dir_button.setFixedSize(80, 30)
        self.browse_extension_dir_button.setStyleSheet("QPushButton:focus { outline: none; }")
        self.browse_extension_dir_button.setCursor(Qt.PointingHandCursor)
        self.browse_extension_dir_button.clicked.connect(self.browse_extension_directory)
        path_frame.layout().addWidget(self.browse_extension_dir_button)
        panel.layout().addWidget(path_frame)

        instructions = QLabel(
            "Select a directory where the browser extension files will be extracted.\n"
            "After extraction, go to your browser's extensions page (e.g., `chrome://extensions/`),\n"
            "enable 'Developer mode', and click 'Load unpacked' to select the extracted folder."
        )
        instructions.setStyleSheet("color: #adb5bd; font-size: 11px; margin-left: 105px;")
        instructions.setWordWrap(True)
        panel.layout().addWidget(instructions)
        
        button_layout = QHBoxLayout()
        button_layout.addStretch(1)
        
        self.extract_extension_button = QPushButton("Extract Extension")
        self.extract_extension_button.setFixedSize(150, 30)
        self.extract_extension_button.setStyleSheet("QPushButton:focus { outline: none; }")
        self.extract_extension_button.setCursor(Qt.PointingHandCursor)
        self.extract_extension_button.clicked.connect(self.extract_browser_extension)
        button_layout.addWidget(self.extract_extension_button)
        
        # --- FIX: Unique back button name ---
        self.back_button_ext = QPushButton("Back")
        self.back_button_ext.setFixedSize(100, 30)
        self.back_button_ext.setStyleSheet("QPushButton:focus { outline: none; }")
        self.back_button_ext.setCursor(Qt.PointingHandCursor)
        self.back_button_ext.clicked.connect(lambda: self.show_panel(self.settings_panel))
        button_layout.addWidget(self.back_button_ext)
        panel.layout().addLayout(button_layout)
        
        panel.layout().addStretch(1)
        
        return panel


    def create_download_settings_panel(self):
        """Creates the panel for configuring default download settings."""
        panel = QWidget()
        panel.setObjectName("DownloadSettingsPanel")
        panel.setLayout(QVBoxLayout())
        panel.layout().setContentsMargins(20, 20, 20, 20)
        panel.layout().setSpacing(15)
        
        dir_frame = QFrame() # <--- DEFINED HERE
        dir_frame.setLayout(QHBoxLayout())
        dir_frame.layout().setContentsMargins(0, 0, 0, 0)
        
        dir_label = QLabel("Download Directory:")
        dir_label.setFixedWidth(120)
        dir_frame.layout().addWidget(dir_label)
        
        # This QLineEdit must be a member of MainWindow to be accessible from browse_global_download_directory
        self.default_download_dir_entry = QLineEdit() 
        self.default_download_dir_entry.setReadOnly(True)
        self.default_download_dir_entry.setText(settings.get('download_save_directory')) # <--- Using settings.get()
        dir_frame.layout().addWidget(self.default_download_dir_entry)

        self.browse_default_download_dir_button = QPushButton("Browse...")
        self.browse_default_download_dir_button.setFixedSize(80, 30)
        self.browse_default_download_dir_button.setStyleSheet("QPushButton:focus { outline: none; }")
        self.browse_default_download_dir_button.setCursor(Qt.PointingHandCursor)
        self.browse_default_download_dir_button.clicked.connect(self.browse_global_download_directory)
        dir_frame.layout().addWidget(self.browse_default_download_dir_button)
        panel.layout().addWidget(dir_frame) # Add dir_frame to panel's layout

        dir_help_label = QLabel("This is the default folder where all your downloads will be saved.")
        dir_help_label.setStyleSheet("color: #adb5bd; font-size: 11px; margin-left: 125px;")
        dir_help_label.setWordWrap(True)
        panel.layout().addWidget(dir_help_label)

        self.overwrite_checkbox = QCustomCheckBox("Overwrite existing file", self)
        self.overwrite_checkbox.setChecked(settings.get('overwrite_existing_file')) # <--- Using settings.get()
        self.overwrite_checkbox.stateChanged.connect(self._toggle_overwrite_setting)
        panel.layout().addWidget(self.overwrite_checkbox)

        overwrite_help_label = QLabel("If checked, new downloads will replace existing files with the same name. Otherwise, a suffix (e.g., '(1)') will be added.")
        overwrite_help_label.setStyleSheet("color: #adb5bd; font-size: 11px; margin-left: 25px;")
        overwrite_help_label.setWordWrap(True)
        panel.layout().addWidget(overwrite_help_label)
        
        double_click_frame = QFrame() # <--- DEFINED HERE
        double_click_frame.setLayout(QHBoxLayout())
        double_click_frame.layout().setContentsMargins(0, 0, 0, 0)
        
        double_click_label = QLabel("Double click on download item:")
        double_click_label.setFixedWidth(200)
        double_click_frame.layout().addWidget(double_click_label)
        
        self.double_click_action_dropdown = QComboBox()
        self.double_click_action_dropdown.addItems(["Open folder", "Open file"])
        self.double_click_action_dropdown.setCurrentText(settings.get('double_click_action')) # <--- Using settings.get()
        self.double_click_action_dropdown.currentIndexChanged.connect(self._update_double_click_action)
        double_click_frame.layout().addWidget(self.double_click_action_dropdown)
        double_click_frame.layout().addStretch(1)
        panel.layout().addWidget(double_click_frame) # Add double_click_frame to panel's layout

        double_click_help_label = QLabel("Choose what happens when you double-click a download in the list.")
        double_click_help_label.setStyleSheet("color: #adb5bd; font-size: 11px; margin-left: 25px;")
        double_click_help_label.setWordWrap(True)
        panel.layout().addWidget(double_click_help_label)

        panel.layout().addStretch(1)

        button_layout = QHBoxLayout()
        button_layout.addStretch(1)
        
        self.back_button_dl = QPushButton("Back")
        self.back_button_dl.setFixedSize(100, 30)
        self.back_button_dl.setStyleSheet("QPushButton:focus { outline: none; }")
        self.back_button_dl.setCursor(Qt.PointingHandCursor)
        self.back_button_dl.clicked.connect(lambda: self.show_panel(self.settings_panel))
        button_layout.addWidget(self.back_button_dl)
        panel.layout().addLayout(button_layout)
        
        return panel


    def apply_stylesheet(self):
        # Base dark theme
        self.setStyleSheet("""
            QMainWindow {
                background-color: #252526; /* Dark background */
                color: #e0e0e0; /* Light text */
            }
            QFrame#sidebarFrame {
                background-color: #1e1e1e; /* Even darker sidebar */
                border-right: 1px solid #3a3a3a;
            }
            QLabel {
                color: #e0e0e0;
            }
            /* Styling for QStackedWidget pages (main content area) */
            QWidget#contentPage {
                background-color: #2d2d2d; /* Slightly lighter than main window */
            }
            QScrollArea {
                background-color: transparent;
                border: none;
            }
            QScrollArea > QWidget > QWidget { /* Content widget inside scroll area */
                background-color: transparent;
            }
            QScrollBar:vertical {
                border: none;
                background: #3a3a3a;
                width: 10px;
                margin: 0px 0px 0px 0px;
                border-radius: 5px;
            }
            QScrollBar::handle:vertical {
                background: #4dabf7; /* Accent color for handle */
                border-radius: 5px;
                min-height: 20px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                background: none;
                border: none;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: none;
            }
            QScrollBar:horizontal { /* Horizontal scrollbar styling */
                border: none;
                background: #3a3a3a;
                height: 10px;
                margin: 0px 0px 0px 0px;
                border-radius: 5px;
            }
            QScrollBar::handle:horizontal {
                background: #6c757d; /* Accent color for handle */
                border-radius: 5px;
                min-width: 20px;
            }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
                background: none;
                border: none;
            }
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
                background: none;
            }
            QComboBox {
                background-color: #3a3a3a;
                border: 1px solid #495057;
                border-radius: 5px;
                padding: 5px;
                color: #e0e0e0;
            }
            QComboBox::drop-down {
                border: none;
            }
            QComboBox::down-arrow {
                /* Inline SVG for a clean look */
                image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23ffffff' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'%3E%3C/polyline%3E%3C/svg%3E");
                width: 12px;
                height: 12px;
                margin-right: 5px;
            }
            QLineEdit {
                background-color: #3a3a3a;
                border: 1px solid #495057;
                border-radius: 5px;
                padding: 5px;
                color: #e0e0e0;
            }
            QLineEdit:focus {
                border: 1px solid #4dabf7; /* Accent border on focus */
            }
            QRadioButton::indicator {
                width: 16px;
                height: 16px;
                border-radius: 8px; /* Make it circular */
                border: 2px solid #495057;
                background-color: #2d2d2d;
            }
            QRadioButton::indicator:checked {
                background-color: #4dabf7; /* Accent color when checked */
                border: 2px solid #4dabf7;
            }
            QRadioButton {
                color: #e0e0e0;
            }
            /* QCustomCheckBox styling is handled by its own paintEvent, but general QCheckBox styles might still apply */
            QCheckBox::indicator { /* Fallback for standard QCheckBox if used elsewhere */
                width: 12px;
                height: 12px;
                border-radius: 4px;
                border: 2px solid #495057;
                background-color: #2d2d2d;
            }
            QCheckBox::indicator:checked { /* Fallback for standard QCheckBox if used elsewhere */
                background-color: #4dabf7;
                border: 2px solid #4dabf7;
            }
            QCheckBox { /* General QCheckBox text color */
                color: #e0e0e0;
            }
            QDialog {
                background-color: #2d2d2d;
                border-radius: 10px;
                border: 1px solid #495057;
            }
            QDialog QLabel {
                color: #e0e0e0;
            }
            QDialog QPushButton {
                background-color: #4dabf7;
                color: white;
                border: none;
                border-radius: 5px;
                padding: 8px 15px;
            }
            QDialog QPushButton:hover {
                background-color: #3b8fcc;
            }
            QDialog QPushButton#cancelButton {
                background-color: #6c757d;
            }
            QDialog QPushButton#cancelButton:hover {
                background-color: #5a6268;
            }
            QPushButton:focus {
                outline: none;
            }
            QPlainTextEdit {
                background-color: #3a3a3a;
                border: 1px solid #495057;
                border-radius: 5px;
                padding: 10px;
                color: #e0e0e0;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 12px;
            }
            QSplitter::handle {
                background-color: #3a3a3a; /* Color of the splitter handle */
                width: 5px; /* Width of the vertical splitter handle */
            }
            QSplitter::handle:hover {
                background-color: #4dabf7; /* Accent color on hover */
            }
            /* QTableView Styling */
            QTableView {
                background-color: #2d2d2d; /* Match content page background */
                border: none; /* Removed border to blend with parent */
                gridline-color: #3a3a3a; /* Subtle grid lines */
                selection-background-color: #495057; /* Selection color */
                selection-color: #e0e0e0;
                color: #e0e0e0;
                outline: none; /* Remove focus outline for the entire table view */
            }
            QTableView::item {
                padding: 5px; /* Padding for cells */
                background-color: #2d2d2d; /* Ensure item background matches table */
                outline: none; /* Remove dotted outline on individual cells when focused */
            }
            QTableView::item:selected {
                background-color: #495057; /* Explicitly set selected item background */
                color: #e0e0e0;
            }
            QHeaderView::section {
                background-color: #3a3a3a; /* Header background - make this distinct */
                color: #d1d1d1; /* Header text color */
                border: 1px solid #3a3a3a; /* Keep border for structure */
                border-bottom: 1px solid #3a3a3a; /* Make border-bottom subtle, matching background */
                font-weight: bold;
                outline: none; /* Remove focus outline on header sections */
                padding-top: 5px; 
                padding-bottom: 5px;
                padding-left: 5px;
                padding-right: 5px;
                text-align: bottom center;
            }
            QHeaderView::section:hover {
                background-color: #495057; /* Hover effect on headers */
            }
            /* Explicitly prevent header sections from changing on row selection */
            QHeaderView::section:selected,
            QHeaderView::section:checked {
                background-color: #3a3a3a; /* Force to default background when a row is selected */
                color: #d1d1d1; /* Force to default text color */
            }
          /* Action Buttons in Top Bar */
            QPushButton.action-button {
                background-color: #495057; /* Darker gray for action buttons */
                color: #ffffff; /* Set text color to white for visibility */
                border: none;
                border-radius: 5px;
                padding: 5px 10px;
                font-size: 13px;
            }
            QPushButton.action-button:hover {
                background-color: #5a6268;
            }
            QPushButton.action-button:pressed {
                background-color: #3a3a3a;
            }
            QPushButton.action-button:focus {
                outline: none;
            }
        """)


    def init_ui(self):
        # Central widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        # Main layout using QSplitter for resizable panels
        main_splitter = QSplitter(Qt.Horizontal)
        # Set the splitter directly as the layout for the central widget
        central_widget.setLayout(QHBoxLayout()) # Create a layout for the central widget
        central_widget.layout().setContentsMargins(0,0,0,0) # Remove margins
        central_widget.layout().setSpacing(0) # Remove spacing
        central_widget.layout().addWidget(main_splitter) # Add the splitter to the layout

        # Sidebar Frame
        self.sidebar_frame = QFrame()
        self.sidebar_frame.setObjectName("sidebarFrame") # For stylesheet targeting
        # self.sidebar_frame.setFixedWidth(200) # Removed fixed width, QSplitter will manage
        self.sidebar_layout = QVBoxLayout(self.sidebar_frame)
        self.sidebar_layout.setContentsMargins(10, 20, 10, 10)
        self.sidebar_layout.setSpacing(5)

        # App logo/title
        logo_label = QLabel("Media Tool")
        logo_label.setFont(QFont("Arial", 20, QFont.Bold))
        logo_label.setStyleSheet("color: #4dabf7;") # Accent color for logo
        logo_label.setAlignment(Qt.AlignCenter)
        self.sidebar_layout.addWidget(logo_label)
        self.sidebar_layout.addSpacing(20)

        # Downloads Section Label (can be removed or made smaller)
        downloads_label = QLabel("DOWNLOADS")
        downloads_label.setFont(QFont("Arial", 12, QFont.Bold))
        downloads_label.setStyleSheet("color: #adb5bd;")
        self.sidebar_layout.addWidget(downloads_label)
        self.sidebar_layout.addSpacing(5)

        # Navigation Buttons (Icon-based)
        self.active_downloads_button = SidebarButton("Active Downloads", ICONS["download"])
        self.completed_videos_button = SidebarButton("Completed Videos", ICONS["film"])
        self.completed_audios_button = SidebarButton("Completed Audios", ICONS["headphones"])
        self.completed_playlists_button = SidebarButton("Completed Playlists", ICONS["list"])
        self.convert_media_button = SidebarButton("Convert Media", ICONS["shuffle"])
        self.settings_button = SidebarButton("Settings", ICONS["menu"]) # Added "Settings" text

        # Group for sidebar buttons to manage checked state
        self.sidebar_button_group = QButtonGroup(self)
        self.sidebar_button_group.setExclusive(True) # Only one button can be checked at a time

        self.sidebar_layout.addWidget(self.active_downloads_button)
        self.sidebar_button_group.addButton(self.active_downloads_button)
        self.sidebar_layout.addWidget(self.completed_videos_button)
        self.sidebar_button_group.addButton(self.completed_videos_button)
        self.sidebar_layout.addWidget(self.completed_audios_button)
        self.sidebar_button_group.addButton(self.completed_audios_button)
        self.sidebar_layout.addWidget(self.completed_playlists_button)
        self.sidebar_button_group.addButton(self.completed_playlists_button)
        self.sidebar_layout.addSpacing(20)


        # Tools Section Label
        tools_label = QLabel("TOOLS")
        tools_label.setFont(QFont("Arial", 12, QFont.Bold))
        tools_label.setStyleSheet("color: #adb5bd;")
        self.sidebar_layout.addWidget(tools_label)
        self.sidebar_layout.addSpacing(5)

        self.sidebar_layout.addWidget(self.convert_media_button)
        self.sidebar_button_group.addButton(self.convert_media_button)
        self.sidebar_layout.addSpacing(20)

        # Add New Download Button (FIXED: now a standard QPushButton)
        self.add_download_button_nav = QPushButton("Add New Download")
        self.add_download_button_nav.setFixedSize(180, 40)
        self.add_download_button_nav.setCursor(Qt.PointingHandCursor)
        self.add_download_button_nav.setStyleSheet("""
            QPushButton {
                background-color: #4dabf7; /* Accent color */
                color: white;
                border: none;
                border-radius: 8px;
                font-size: 14px;
                font-weight: bold;
                text-align: center; /* Center text for this button */
                padding: 5px 10px; /* Add padding */
            }
            QPushButton:hover {
                background-color: #3b8fcc; /* Darker accent on hover */
            }
            QPushButton:pressed {
                background-color: #2b7bb5;
            }
            QPushButton:focus {
                outline: none;
            }
        """)
        self.add_download_button_nav.clicked.connect(self.open_add_download_dialog)
        self.sidebar_layout.addWidget(self.add_download_button_nav)
        self.sidebar_layout.addStretch(1) # Pushes everything above to the top

        # Settings Button (at the very bottom, now part of button group)
        self.sidebar_layout.addWidget(self.settings_button)
        self.sidebar_button_group.addButton(self.settings_button) # Added to button group

        main_splitter.addWidget(self.sidebar_frame) # Add sidebar to splitter

        # Main Content Area (Stacked Widget)
        self.content_container_widget = QWidget() # New container widget for content area
        self.main_content_vlayout = QVBoxLayout(self.content_container_widget)
        self.main_content_vlayout.setContentsMargins(0, 0, 0, 0)
        self.main_content_vlayout.setSpacing(0)

        self.content_top_bar = QFrame()
        self.content_top_bar.setFixedHeight(60)
        self.content_top_bar.setStyleSheet("background-color: #2d2d2d; border-bottom: 1px solid #3a3a3a;")
        self.top_bar_layout = QHBoxLayout(self.content_top_bar)
        self.top_bar_layout.setContentsMargins(20, 0, 20, 0)
        self.top_bar_layout.setSpacing(10)

        # Create a horizontal layout for action buttons
        action_buttons_layout = QHBoxLayout()
        action_buttons_layout.setContentsMargins(0, 0, 0, 0) # No margins for this sub-layout
        action_buttons_layout.setSpacing(10) # Spacing between buttons

        self.delete_button = self._create_action_button("Delete", ICONS["trash"])
        self.open_button = self._create_action_button("Open", ICONS["play"])
        self.open_folder_button = self._create_action_button("Open Folder", ICONS["folder-open"])
        self.cancel_button = self._create_action_button("Cancel", ICONS["x-circle"])
        self.refresh_button = self._create_action_button("Refresh", ICONS["refresh"])

        action_buttons_layout.addWidget(self.delete_button)
        action_buttons_layout.addWidget(self.open_button)
        action_buttons_layout.addWidget(self.open_folder_button)
        action_buttons_layout.addWidget(self.cancel_button)
        action_buttons_layout.addWidget(self.refresh_button)

        # Add the action buttons layout to the main top bar layout
        self.top_bar_layout.addLayout(action_buttons_layout)

        # Add a stretch to push the search bar to the right and allow it to expand
        self.top_bar_layout.addStretch(1)

        # Add the search input
        self.search_input = SearchLineEdit(self, self) 
        self.top_bar_layout.addWidget(self.search_input)
        
        self.main_content_vlayout.addWidget(self.content_top_bar)

        self.content_stacked_widget = QStackedWidget()
        self.content_stacked_widget.setObjectName("contentPage") # For stylesheet targeting
        self.main_content_vlayout.addWidget(self.content_stacked_widget)
        
        main_splitter.addWidget(self.content_container_widget) # Add content area to splitter

        # Set initial sizes for the splitter (sidebar, content)
        # These weights determine the initial distribution of space.
        # A ratio of 1:4 (200:800) for a 1000px wide window.
        main_splitter.setSizes([200, 800]) 

        # Create and add all panels
        self.active_downloads_panel = self.create_active_downloads_panel()
        self.completed_videos_panel = self.create_completed_panel("Videos", "video")
        self.completed_audios_panel = self.create_completed_panel("Audios", "audio")
        self.completed_playlists_panel = self.create_completed_panel("Playlists", "playlist")
        self.conversion_panel = self.create_coming_soon_panel("Media Converter") # MODIFIED
        self.settings_panel = self.create_settings_panel()
        self.extension_setup_panel = self.create_extension_setup_panel()
        self.download_settings_panel = self.create_download_settings_panel()
        # self.uri_scheme_setup_panel = self.create_uri_scheme_setup_panel() # REMOVED

        self.content_stacked_widget.addWidget(self.active_downloads_panel)
        self.content_stacked_widget.addWidget(self.completed_videos_panel)
        self.content_stacked_widget.addWidget(self.completed_audios_panel)
        self.content_stacked_widget.addWidget(self.completed_playlists_panel)
        self.content_stacked_widget.addWidget(self.conversion_panel)
        self.content_stacked_widget.addWidget(self.settings_panel)
        self.content_stacked_widget.addWidget(self.extension_setup_panel)
        self.content_stacked_widget.addWidget(self.download_settings_panel)
        # self.content_stacked_widget.addWidget(self.uri_scheme_setup_panel) # REMOVED

        # --- Status Bar ---
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar) 

        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet("color: #adb5bd; font-size: 12px;")
        self.status_bar.addWidget(self.status_label) 

        self.version_label = QLabel("Ver: 1.0.0")
        self.version_label.setStyleSheet("color: #7F8C8D; margin-left: 15px;")
        self.status_bar.addPermanentWidget(self.version_label) 

        self.status_bar.setStyleSheet("QStatusBar { background-color: #252526; border-top: 1px solid #3a3a3a; }")

        # The initial show_panel call is now at the end of __init__
        self.show_panel(self.active_downloads_panel)
        self.active_downloads_button.setChecked(True)



    def _create_action_button(self, text, icon_svg):
        """Helper to create a styled action button for the top bar."""
        btn = QPushButton(text)
        btn.setObjectName("action-button") # For stylesheet targeting
        btn.setFixedSize(100, 30) # Fixed size for consistency
        btn.setCursor(Qt.PointingHandCursor)
        
        icon_pixmap = QPixmap()
        # Ensure icon color is explicitly set to white here
        icon_pixmap.loadFromData(icon_svg.replace('currentColor', '#ffffff').encode('utf-8')) 
        icon = QIcon(icon_pixmap.scaled(16, 16, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        btn.setIcon(icon)
        btn.setIconSize(icon.actualSize(btn.size()))
        
        # Explicitly set the text color using QPalette
        palette = btn.palette()
        palette.setColor(QPalette.ColorRole.ButtonText, QColor("#ffffff")) # Set button text color to white
        btn.setPalette(palette)
        btn.setAutoFillBackground(True) # Needed for palette changes to take effect on some widgets

        btn.setStyleSheet("QPushButton.action-button { background-color: #495057; border: none; border-radius: 5px; padding: 5px 10px; font-size: 13px; } QPushButton.action-button:hover { background-color: #5a6268; } QPushButton.action-button:pressed { background-color: #3a3a3a; } QPushButton.action-button:focus { outline: none; }")
        btn.setVisible(False) # Initially hidden
        return btn

    def connect_signals(self):
        # Connect navigation buttons
        self.active_downloads_button.clicked.connect(lambda: self.show_panel(self.active_downloads_panel))
        self.completed_videos_button.clicked.connect(lambda: self.show_panel(self.completed_videos_panel))
        self.completed_audios_button.clicked.connect(lambda: self.show_panel(self.completed_audios_panel))
        self.completed_playlists_button.clicked.connect(lambda: self.show_panel(self.completed_playlists_panel))
        self.convert_media_button.clicked.connect(lambda: self.show_panel(self.conversion_panel))
        self.add_download_button_nav.clicked.connect(self.open_add_download_dialog)
        self.settings_button.clicked.connect(lambda: self.show_panel(self.settings_panel))

        # Connect internal signals for GUI updates from Flask threads
        self.add_download_signal.connect(self.add_download_to_list)
        self.update_download_status_signal.connect(self.update_download_status_in_list)
        self.add_completed_signal.connect(self.add_completed_download)
        # self.add_conversion_signal.connect(self.add_conversion_to_list) # Commented out
        # self.update_conversion_status_signal.connect(self.update_conversion_status_in_list) # Commented out
        self.show_status_signal.connect(self.show_status)
        self.set_buttons_disabled_signal.connect(self.set_all_buttons_disabled)

        # Connect action buttons
        self.delete_button.clicked.connect(self.delete_selected_items)
        self.open_button.clicked.connect(self.open_selected_file)
        self.open_folder_button.clicked.connect(self.open_selected_folder)
        self.cancel_button.clicked.connect(self.cancel_download)
        self.refresh_button.clicked.connect(self.refresh_current_view)

        # --- ADDED: Connect double-click signals for all table views ---
        self.active_downloads_table_view.doubleClicked.connect(self.handle_table_double_click)
        self.completed_videos_table_view.doubleClicked.connect(self.handle_table_double_click)
        self.completed_audios_table_view.doubleClicked.connect(self.handle_table_double_click)
        self.completed_playlists_table_view.doubleClicked.connect(self.handle_table_double_click)

    def _add_process_to_tracker(self, url, process_info):
        """Adds a running subprocess and its cancel event to the tracker."""
        # process_info is expected to be {'process': Popen_object, 'cancel_event': threading.Event}
        self.active_processes[url] = process_info
        print(f"DEBUG: Process info added for URL: {url}")


    def _remove_process_from_tracker(self, url):
        """Removes a process from the tracker."""
        if url in self.active_processes:
            del self.active_processes[url]
            print(f"DEBUG: Process removed for URL: {url}")


    def handle_table_double_click(self, index):
        """Handles double-click events on table items."""
        selected_item = self._get_selected_item_data() # This method already gets data from current view
        if not selected_item:
            return # No item selected, nothing to do

        # Use settings.get to retrieve the action
        if settings.get('double_click_action') == "Open folder":
            self.open_selected_folder()
        elif settings.get('double_click_action') == "Open file":
            self.open_selected_file()
        else:
            self.show_status("Unknown double-click action configured.", "error")

    def closeEvent(self, event):
        """Properly shut down Flask server and clean up resources on application close."""
        logger.info("Application closing. Initiating shutdown sequence.")
        
        # Cancel all active downloads
        for url, process_info in list(self.active_processes.items()):
            try:
                cancel_event = process_info.get('cancel_event')
                if cancel_event:
                    cancel_event.set() # Signal the thread to stop
                    
                process = process_info.get('process')
                if process and process.poll() is None: # If process is still running
                    if sys.platform == 'win32':
                        # On Windows, use taskkill /F /T to forcefully terminate process tree
                        subprocess.run(['taskkill', '/F', '/T', '/PID', str(process.pid)], 
                                       check=False,
                                       creationflags=subprocess.CREATE_NO_WINDOW,
                                       stdout=DEVNULL, stderr=DEVNULL)
                    else:
                        process.terminate() # Send SIGTERM
                        try:
                            process.wait(timeout=1) # Give it a moment to terminate
                        except subprocess.TimeoutExpired:
                            process.kill() # Force kill if it doesn't respond
                logger.info(f"Cleaned up process for {url} during close.")
            except Exception as e:
                logger.error(f"Error cleaning up process for {url} during close: {e}")
        
        # Shutdown Flask server
        try:
            logger.info(f"Sending shutdown request to Flask server on port {FLASK_PORT}...")
            # Use a short timeout for the shutdown request
            requests.post(f"http://localhost:{FLASK_PORT}/shutdown", timeout=1) 
            logger.info("Flask shutdown request sent.")
        except requests.exceptions.ConnectionError:
            logger.warning("Could not connect to Flask server for shutdown (might already be down or never started).")
        except requests.exceptions.Timeout:
            logger.warning("Flask server shutdown request timed out.")
        except Exception as e:
            logger.error(f"Error sending Flask shutdown request: {e}")
        
        event.accept() # Accept the close event



    def show_status(self, message, msg_type='info', timeout_ms=5000):
        """Thread-safe status bar update. Can be called from any thread."""
        with self.status_lock: # Acquire lock for thread safety
            # If called from a non-GUI thread, emit the signal to the GUI thread
            if threading.current_thread() != threading.main_thread():
                self.show_status_signal.emit(message, msg_type, timeout_ms)
                return
            
            # This part runs only in the GUI thread
            self.status_label.setText(message)
            if msg_type == 'success':
                self.status_label.setStyleSheet("color: #4dabf7; font-size: 12px;") # Accent color
            elif msg_type == 'error':
                self.status_label.setStyleSheet("color: #ff6b6b; font-size: 12px;") # Red for error
            else:
                self.status_label.setStyleSheet("color: #adb5bd; font-size: 12px;") # Default gray
            
            # Start or restart the timer to clear the message
            self.status_clear_timer.start(timeout_ms)
            QApplication.processEvents() # Ensure immediate update

    def _clear_status_bar(self):
        """Clears the status bar message and resets its style."""
        self.status_label.setText("Ready")
        self.status_label.setStyleSheet("color: #adb5bd; font-size: 12px;")
        QApplication.processEvents() # Ensure immediate update


    def set_all_buttons_disabled(self, disabled):
        """Disables/enables all relevant GUI buttons to prevent concurrent operations."""
        # A more robust way to disable all actionable widgets
        widgets_to_disable = [
            # Sidebar
            self.active_downloads_button, self.completed_videos_button,
            self.completed_audios_button, self.completed_playlists_button,
            self.convert_media_button, self.add_download_button_nav,
            self.settings_button, 
            # Top bar actions
            self.delete_button, self.open_button, self.open_folder_button,
            self.cancel_button, self.refresh_button,
            # Search
            self.search_input,
            # Conversion Panel (Now handled by the placeholder panel, but keeping these commented for future)
            # self.browse_input_file_button, self.output_format_dropdown,
            # self.browse_output_dir_button, self.start_conversion_button,
            # self.video_codec_dropdown, self.back_button_convert,
            # Settings Panel
            self.browser_monitor_switch, self.go_to_extension_button,
            self.go_to_download_settings_button,
            # self.go_to_uri_scheme_button, # REMOVED
            # Extension Setup Panel
            self.browse_extension_dir_button, self.extract_extension_button, self.back_button_ext,
            # Download Settings Panel
            self.browse_default_download_dir_button, self.overwrite_checkbox,
            self.double_click_action_dropdown, self.back_button_dl,
            # URI Scheme Panel
            # self.back_button_uri, # REMOVED
        ]
        
        for widget in widgets_to_disable:
            # Check if widget exists before trying to disable it
            if widget and hasattr(widget, 'setEnabled'):
                widget.setEnabled(not disabled)
        
        # Handle dialog buttons if open
        if hasattr(self, 'add_download_dialog') and self.add_download_dialog.isVisible():
            self.add_download_dialog.url_entry.setEnabled(not disabled)
            for radio_btn in self.add_download_dialog.media_type_group.buttons():
                radio_btn.setEnabled(not disabled)
            self.add_download_dialog.ok_button.setEnabled(not disabled)
            self.add_download_dialog.cancel_button.setEnabled(not disabled)


    def show_panel(self, panel_to_show):
        """Switches the main content area to display the specified panel."""
        # Uncheck all buttons in the group first to ensure exclusive selection
        for button in self.sidebar_button_group.buttons():
            button.setChecked(False)

        self.content_stacked_widget.setCurrentWidget(panel_to_show)
        
        # Hide all action buttons first
        self.delete_button.setVisible(False)
        self.open_button.setVisible(False)
        self.open_folder_button.setVisible(False)
        self.cancel_button.setVisible(False)
        self.refresh_button.setVisible(False)
        self.search_input.setVisible(False) # Hide search by default

        # Update current panel type for search filtering and set the correct button as checked
        if panel_to_show == self.active_downloads_panel:
            self.current_panel_type = "active_downloads"
            self.active_downloads_button.setChecked(True)
            self.active_downloads_table_view.setModel(self.active_downloads_model) # Ensure correct model is set
            self.active_downloads_table_view.sortByColumn(0, Qt.AscendingOrder) # Default sort by name
            
            # Show relevant action buttons for Active Downloads
            self.search_input.setVisible(True)
            self.delete_button.setVisible(True)
            self.open_button.setVisible(True) # Can open partially downloaded files
            self.open_folder_button.setVisible(True)
            self.cancel_button.setVisible(True)
            self.refresh_button.setVisible(True)

        elif panel_to_show == self.completed_videos_panel:
            self.current_panel_type = "completed_videos"
            self.completed_videos_button.setChecked(True)
            self.completed_videos_table_view.setModel(self.completed_videos_model) # Ensure correct model is set
            self.completed_videos_table_view.sortByColumn(1, Qt.DescendingOrder) # Default sort by date desc
            
            # Show relevant action buttons for Completed Videos
            self.search_input.setVisible(True)
            self.delete_button.setVisible(True)
            self.open_button.setVisible(True)
            self.open_folder_button.setVisible(True)
            self.refresh_button.setVisible(True)

        elif panel_to_show == self.completed_audios_panel:
            self.current_panel_type = "completed_audios"
            self.completed_audios_button.setChecked(True)
            self.completed_audios_table_view.setModel(self.completed_audios_model) # Ensure correct model is set
            self.completed_audios_table_view.sortByColumn(1, Qt.DescendingOrder) # Default sort by date desc
            
            # Show relevant action buttons for Completed Audios
            self.search_input.setVisible(True)
            self.delete_button.setVisible(True)
            self.open_button.setVisible(True)
            self.open_folder_button.setVisible(True)
            self.refresh_button.setVisible(True)

        elif panel_to_show == self.completed_playlists_panel:
            self.current_panel_type = "completed_playlists"
            self.completed_playlists_button.setChecked(True)
            self.completed_playlists_table_view.setModel(self.completed_playlists_model) # Ensure correct model is set
            self.completed_playlists_table_view.sortByColumn(1, Qt.DescendingOrder) # Default sort by date desc
            
            # Show relevant action buttons for Completed Playlists
            self.search_input.setVisible(True)
            self.delete_button.setVisible(True)
            self.open_button.setVisible(True)
            self.open_folder_button.setVisible(True)
            self.refresh_button.setVisible(True)

        elif panel_to_show == self.conversion_panel:
            self.current_panel_type = "conversion"
            self.convert_media_button.setChecked(True)
            # No update needed for "Coming Soon" panel

        elif panel_to_show == self.settings_panel:
            self.current_panel_type = "settings"
            self.settings_button.setChecked(True) # Now part of button group
            self.update_settings_display() # Explicitly update when shown

        elif panel_to_show == self.extension_setup_panel:
            self.current_panel_type = "extension_setup"
            self.update_extension_setup_display() # Explicitly update when shown

        elif panel_to_show == self.download_settings_panel:
            self.current_panel_type = "download_settings"
            self.update_download_settings_display() # Explicitly update when shown

        # elif panel_to_show == self.uri_scheme_setup_panel: # REMOVED
        #     self.current_panel_type = "uri_scheme_setup"
        #     self.update_uri_scheme_setup_display()


    def filter_displayed_items(self, search_query):
        """Filters items in the currently displayed list based on the search query."""
        search_query = search_query.lower().strip()

        source_data = None
        current_model = None
        current_table_view = None

        if self.current_panel_type == "active_downloads":
            source_data = self.active_downloads_data
            current_model = self.active_downloads_model
            current_table_view = self.active_downloads_table_view
        elif self.current_panel_type == "completed_videos":
            source_data = self.completed_videos_data
            current_model = self.completed_videos_model
            current_table_view = self.completed_videos_table_view
        elif self.current_panel_type == "completed_audios":
            source_data = self.completed_audios_data
            current_model = self.completed_audios_model
            current_table_view = self.completed_audios_table_view
        elif self.current_panel_type == "completed_playlists":
            source_data = self.completed_playlists_data
            current_model = self.completed_playlists_model
            current_table_view = self.completed_playlists_table_view
        else:
            return # No filtering for other panels

        filtered_data = []
        if search_query:
            for item_info in source_data:
                # Search in filename, URL, status, type (for completed)
                search_text = (
                    item_info.get('filename', '').lower() + 
                    item_info.get('url', '').lower() + 
                    item_info.get('status', '').lower() + 
                    item_info.get('filetype', '').lower()
                )
                if search_query in search_text:
                    filtered_data.append(item_info)
        else:
            filtered_data = source_data[:] # Show all, use a copy

        current_model.setFilteredData(filtered_data) # <--- Using setFilteredData
        
        # Re-apply current sort order after filtering
        sort_column = current_table_view.horizontalHeader().sortIndicatorSection()
        sort_order = current_table_view.horizontalHeader().sortIndicatorOrder()
        if sort_column != -1:
            current_model.sort(sort_column, sort_order)

        QApplication.processEvents() # Force redraw


    # --- Action Button Implementations ---
    def _get_selected_item_data(self):
        """Helper to get data for the currently selected row in the active table view."""
        current_table_view = None
        current_model = None

        if self.current_panel_type == "active_downloads":
            current_table_view = self.active_downloads_table_view
            current_model = self.active_downloads_model
        elif self.current_panel_type == "completed_videos":
            current_table_view = self.completed_videos_table_view
            current_model = self.completed_videos_model
        elif self.current_panel_type == "completed_audios":
            current_table_view = self.completed_audios_table_view
            current_model = self.completed_audios_model
        elif self.current_panel_type == "completed_playlists":
            current_table_view = self.completed_playlists_table_view
            current_model = self.completed_playlists_model
        
        if current_table_view and current_model:
            selected_indexes = current_table_view.selectionModel().selectedRows()
            if selected_indexes:
                # Assuming single selection for these actions
                row = selected_indexes[0].row()
                # Ensure the row is valid for the current data
                if row < len(current_model._data):
                    return current_model._data[row]
        return None

    def delete_selected_items(self):
        """Deletes selected download items from the current view."""
        selected_item = self._get_selected_item_data()
        if not selected_item:
            self.show_status("No item selected to delete.", "info")
            return
        
        # Implement a custom confirmation dialog instead of QMessageBox
        confirm_dialog = ConfirmationDialog(f"Are you sure you want to delete '{selected_item.get('filename', 'this item')}'?", self)
        if confirm_dialog.exec() == QDialog.Accepted:
            url_to_delete = selected_item.get('url')
            
            if self.current_panel_type == "active_downloads":
                self.active_downloads_model.removeItem(url_to_delete)
                self.show_status(f"Download '{selected_item.get('filename')}' removed.", "success")
                # In a real app, you might also need to send a signal to Flask to stop/cancel the download process
                # if it's still active.
            elif self.current_panel_type in ["completed_videos", "completed_audios", "completed_playlists"]:
                # Determine which completed model to remove from
                if selected_item.get('filetype') == 'video':
                    self.completed_videos_model.removeItem(url_to_delete)
                elif selected_item.get('filetype') == 'audio':
                    self.completed_audios_model.removeItem(url_to_delete)
                elif selected_item.get('filetype') == 'playlist':
                    self.completed_playlists_model.removeItem(url_to_delete)
                self.show_status(f"Completed item '{selected_item.get('filename')}' deleted.", "success")
            
            # Re-filter to update display
            self.filter_displayed_items(self.search_input.text())
        else:
            self.show_status("Deletion cancelled.", "info")

    def open_selected_file(self):
        """Opens the file associated with the selected download item."""
        selected_item = self._get_selected_item_data()
        if not selected_item:
            self.show_status("No item selected to open.", "info")
            return
        
        file_path = selected_item.get('path')
        if file_path and os.path.exists(file_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(file_path))
            self.show_status(f"Opening file: {os.path.basename(file_path)}", "info")
        else:
            self.show_status(f"File not found: {selected_item.get('filename', 'N/A')}. Path: {file_path}", "error")

    def open_selected_folder(self):
        """Opens the folder containing the selected download item."""
        selected_item = self._get_selected_item_data()
        if not selected_item:
            self.show_status("No item selected to open folder.", "info")
            return
        
        file_path = selected_item.get('path')
        if file_path and os.path.exists(file_path):
            folder_path = os.path.dirname(file_path)
            if os.path.exists(folder_path):
                QDesktopServices.openUrl(QUrl.fromLocalFile(folder_path))
                self.show_status(f"Opening folder: {folder_path}", "info")
            else:
                self.show_status(f"Folder not found for: {selected_item.get('filename', 'N/A')}. Path: {folder_path}", "error")
        else:
            self.show_status(f"File path not available for: {selected_item.get('filename', 'N/A')}", "error")

    def cancel_download(self):
        """Cancels the selected active download."""
        selected_item = self._get_selected_item_data()
        if not selected_item or self.current_panel_type != "active_downloads":
            self.show_status("Please select an active download to cancel.", "info")
            return
        
        url_to_cancel = selected_item.get('url')

        confirm_dialog = ConfirmationDialog(f"Are you sure you want to cancel '{selected_item.get('filename', 'this download')}'?", self)
        if confirm_dialog.exec() == QDialog.Accepted:
            # Attempt to terminate the subprocess
            if url_to_cancel in self.active_processes:
                process_info = self.active_processes[url_to_cancel]
                process_to_terminate = process_info.get('process')
                cancel_event = process_info.get('cancel_event')

                if not cancel_event:
                    self.show_status(f"Error: No cancellation event found for '{selected_item.get('filename')}'", "error")
                    return

                try:
                    # 1. Set the internal cancellation event
                    cancel_event.set()
                    print(f"DEBUG: Set cancellation event for {url_to_cancel}")

                    if process_to_terminate and process_to_terminate.poll() is None: # Only try to terminate if still running
                        if sys.platform == 'win32':
                            # On Windows, use taskkill /F /T to forcefully terminate process tree
                            print(f"DEBUG: Attempting to terminate process tree (PID: {process_to_terminate.pid}) for {url_to_cancel} using taskkill.")
                            subprocess.run(['taskkill', '/F', '/T', '/PID', str(process_to_terminate.pid)], 
                                           check=False,
                                           creationflags=subprocess.CREATE_NO_WINDOW,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                            time.sleep(0.1) # Give OS a moment
                        else:
                            process_to_terminate.terminate() 
                            print(f"DEBUG: Sent terminate signal to process for {url_to_cancel}")
                            try:
                                process_to_terminate.wait(timeout=5)
                                if process_to_terminate.poll() is None:
                                    print(f"DEBUG: Process for {url_to_cancel} did not terminate gracefully, forcing kill.")
                                    process_to_terminate.kill()
                                    process_to_terminate.wait()
                            except subprocess.TimeoutExpired:
                                print(f"DEBUG: Process for {url_to_cancel} timed out during graceful termination, forcing kill.")
                                process_to_terminate.kill()
                                process_to_terminate.wait()
                    else:
                        print(f"DEBUG: Process for {url_to_cancel} was already terminated or not found when cancel was clicked.")

                    # Update GUI status immediately to show it's being cancelled
                    self.active_downloads_model.updateItem(url_to_cancel, {'status': 'Cancelling...', 'progress': '0%', 'message': 'Cancellation requested.'})
                    self.show_status(f"Cancellation requested for '{selected_item.get('filename')}'", "info")
                    
                    # The _perform_yt_dlp_download thread will handle the final 'Cancelled' status
                    # and removal from active_downloads_model once it fully exits.
                    
                except Exception as e:
                    self.show_status(f"Error during cancellation attempt: {e}", "error")
                    print(f"Error during cancellation attempt for {url_to_cancel}: {e}")
                    # If an error occurs here, ensure it's removed from tracker and GUI
                    self._remove_process_from_tracker(url_to_cancel)
                    self.active_downloads_model.removeItem(url_to_cancel) 
            else:
                self.show_status(f"No active process found for '{selected_item.get('filename')}'", "info")
                self.active_downloads_model.removeItem(url_to_cancel) 
        else:
            self.show_status("Cancellation aborted.", "info")

    def refresh_current_view(self):
        self.filter_displayed_items(self.search_input.text())
        self.show_status("View refreshed.", "info")



    def _toggle_overwrite_setting(self, state):
        """Updates the overwrite_existing_file setting based on checkbox state."""
        settings.set('overwrite_existing_file', self.overwrite_checkbox.isChecked())
        self.show_status(f"Overwrite existing file set to: {settings.get('overwrite_existing_file')}", "info")

    def _update_double_click_action(self, index):
        """Updates the double_click_action setting based on dropdown selection."""
        settings.set('double_click_action', self.double_click_action_dropdown.currentText())
        self.show_status(f"Double-click action set to: {settings.get('double_click_action')}", "info")


    # --- Panel Update Functions (to refresh content when shown) ---
    def update_active_downloads_display(self):
        """Refreshes the display of active downloads in the GUI."""
        self.filter_displayed_items(self.search_input.text())


    def update_completed_display(self, file_type):
        """Refreshes the display of completed downloads for a specific type."""
        self.filter_displayed_items(self.search_input.text())


    def clear_completed_list(self, file_type):
        """Clears the completed downloads list for a specific type"""
        if file_type == "video":
            self.completed_videos_model.clearAll()
        elif file_type == "audio":
            self.completed_audios_model.clearAll()
        elif file_type == "playlist":
            self.completed_playlists_model.clearAll()
        self.show_status(f"Cleared completed {file_type}s", "success")
        QApplication.processEvents()

    # def update_conversion_display(self): # Commented out
    #     self.conversion_input_file_entry.setText(os.path.basename(self.convert_input_file) if self.convert_input_file else "")
    #     self.conversion_output_dir_entry.setText(self.convert_output_directory)
    #     self._toggle_codec_dropdown_convert()
    #     QApplication.processEvents()

    def update_settings_display(self):
        logger.debug(f"update_settings_display called. Reading browser_monitor_enabled: {settings.get('browser_monitor_enabled')}")
        self.browser_monitor_switch.setChecked(settings.get('browser_monitor_enabled'))
        QApplication.processEvents()

    def update_download_settings_display(self):
        # Use settings.get() for all these values
        self.default_download_dir_entry.setText(settings.get('download_save_directory'))
        self.overwrite_checkbox.setChecked(settings.get('overwrite_existing_file'))
        self.double_click_action_dropdown.setCurrentText(settings.get('double_click_action'))
        QApplication.processEvents()

    def update_extension_setup_display(self):
        self.extension_path_entry.setText(os.path.join(os.path.expanduser("~"), "Downloads", "universal_media_tool_extension"))
        QApplication.processEvents()


    # def update_uri_scheme_setup_display(self): # REMOVED
    #     QApplication.processEvents()


    def add_download_to_list(self, download_info):
        """Adds a new download entry to the active downloads list in the GUI."""
        # Use the master data list for the check
        if not any(d['url'] == download_info['url'] for d in self.active_downloads_data):
            self.active_downloads_data.append(download_info)
            # --- FIX: Show it immediately in the active-downloads table if that panel is current ---
            if self.current_panel_type == 'active_downloads':
                self.active_downloads_model.addItem(download_info)
            else:
                # If not on the active downloads panel, update its display so it's ready when switched to
                self.update_active_downloads_display()
            # --- END FIX ---
            self.show_status(f"Download added: {download_info.get('filename', download_info.get('url'))}", "info")
        QApplication.processEvents()



    def update_download_status_in_list(self, url, new_data_dict):
        """Updates an existing download entry in the GUI. Does NOT add new entries."""
        # Check if the download already exists by URL in the active_downloads_data (master list)
        found_index = -1
        for i, item in enumerate(self.active_downloads_data):
            if item.get('url') == url:
                found_index = i
                break

        if found_index != -1:
            # Update existing entry in the master data list
            current_data = self.active_downloads_data[found_index]
            current_data.update(new_data_dict) # Merge new data into existing
            
            # Now, update the model. The model might be filtered, so we need to ensure
            # the update propagates correctly to the currently displayed items.
            # The DownloadTableModel.updateItem method will handle emitting dataChanged.
            self.active_downloads_model.updateItem(url, new_data_dict)
        else:
            logger.warning(f"Received update for unknown download URL: {url}. Data: {new_data_dict}")

        current_status = new_data_dict.get('status', 'N/A')
        filename = new_data_dict.get('filename', url)
        message = new_data_dict.get('message', '')

        if current_status == 'Completed':
            self.show_status(f"Download completed: {filename}", "success")
            # Completed downloads are handled by add_completed_download, which removes from active_downloads_data
        elif current_status in ['Failed', 'Error', 'Cancelled']:
            logger.error(f"Download for {url} {current_status}. Message: {message}")
            self.show_status(f"Download {current_status}: {filename}. Error: {message}", "error", timeout_ms=10000)
            
            # Remove from active_downloads_data if terminal status
            # This ensures the master list doesn't retain failed/cancelled items.
            # The model's removeItem will also update the view.
            self.active_downloads_data[:] = [d for d in self.active_downloads_data if d.get('url') != url]
            self.active_downloads_model.removeItem(url)
            self._remove_process_from_tracker(url) # Ensure process is also removed from tracker
        
        QApplication.processEvents() # Force GUI redraw



    def add_completed_download(self, download_info):
        """Adds a completed download to the appropriate completed list and removes it from active."""
        filetype = download_info.get('filetype', 'unknown')
        
        # Add to appropriate master data list and model
        if filetype == 'video':
            self.completed_videos_data.append(download_info)
            if self.current_panel_type == 'completed_videos':
                self.completed_videos_model.addItem(download_info)
        elif filetype == 'audio':
            self.completed_audios_data.append(download_info)
            if self.current_panel_type == 'completed_audios':
                self.completed_audios_model.addItem(download_info)
        elif filetype == 'playlist':
            self.completed_playlists_data.append(download_info)
            if self.current_panel_type == 'completed_playlists':
                self.completed_playlists_model.addItem(download_info)
        else:
            print(f"WARNING: Unknown filetype '{filetype}' for completed download: {download_info.get('url')}")
            self.completed_videos_data.append(download_info) # Fallback to video
            if self.current_panel_type == 'completed_videos':
                self.completed_videos_model.addItem(download_info)

        # Remove from active downloads master list and model
        self.active_downloads_data[:] = [d for d in self.active_downloads_data if d.get('url') != download_info.get('url')]
        self.active_downloads_model.removeItem(download_info.get('url'))
        
        QApplication.processEvents()

    # def add_conversion_to_list(self, conversion_info): # Commented out
    #     """Handles the start of a conversion process in the GUI."""
    #     self.show_status(f"Conversion initiated: {conversion_info.get('filename')}", "info")
    #     QApplication.processEvents()

    # def update_conversion_status_in_list(self, input_path, status_from_message=None, progress=None, message=None): # Commented out
    #     """Updates the status and progress of an active conversion."""
    #     display_message = f"Conversion {status_from_message} for {os.path.basename(input_path)}"
    #     if progress and progress != "0%":
    #         display_message += f" ({progress})"
    #     if message:
    #         display_message += f": {message}"
        
    #     msg_type = "success" if status_from_message == "Completed" else "info" if status_from_message == "Converting" else "error"
    #     self.show_status(display_message, msg_type, timeout_ms=10000)
    #     QApplication.processEvents()


    def check_flask_message_queue(self):
        """Periodically checks the Flask message queue for updates to the GUI."""
        messages_processed = 0
        max_messages_per_cycle = 10  # Process a limited number of messages per cycle to prevent UI freeze
        
        try:
            while messages_processed < max_messages_per_cycle:
                try:
                    message = gui_message_queue.get_nowait()
                    messages_processed += 1
                    
                    self._process_queue_message(message) # Process message using helper
                    
                except queue.Empty:
                    break # No more messages in the queue
                    
        except Exception as e:
            logger.error(f"Error in queue processing: {e}")
            logger.exception("Traceback for queue processing error:")

    def _process_queue_message(self, message):
        """Processes a single message from the Flask message queue."""
        msg_type = message.get('type')
        url = message.get('url') # Common for many message types

        if msg_type == 'add_download':
            cancel_event = message.pop('cancel_event', None)
            self.add_download_signal.emit(message)
            if cancel_event:
                self._add_process_to_tracker(url, {'process': None, 'cancel_event': cancel_event})
                logger.debug(f"Added initial download info and cancel_event for URL: {url}")

        elif msg_type == 'register_process':
            process_obj = message['process']
            cancel_event_from_msg = message['cancel_event']

            if url in self.active_processes:
                self.active_processes[url]['process'] = process_obj
                if self.active_processes[url].get('cancel_event') is None:
                    self.active_processes[url]['cancel_event'] = cancel_event_from_msg
                logger.debug(f"Process object and cancel_event registered for URL: {url}")
            else:
                self._add_process_to_tracker(url, {'process': process_obj, 'cancel_event': cancel_event_from_msg})
                logger.warning(f"Process registered for URL {url} without prior add_download message. This might indicate a timing issue.")

        elif msg_type == 'update_download_status':
            self.update_download_status_signal.emit(
                url, 
                {k: v for k, v in message.items() if k not in ['type', 'url']}
            )
        elif msg_type == 'add_completed':
            self.add_completed_signal.emit(message)
        elif msg_type == 'remove_process': # Handle removal explicitly
            self._remove_process_from_tracker(url)
            logger.debug(f"Process removed by message for URL: {url}")
        else:
            logger.warning(f"Unknown message type received: {msg_type}")

    def browse_extension_directory(self):
        """Opens a file dialog to select the directory for browser extension extraction."""
        directory = QFileDialog.getExistingDirectory(self, "Select Directory to Extract Browser Extension")
        if directory:
            self.extension_path_entry.setText(os.path.join(directory, "universal_media_tool_extension"))
            self.show_status_signal.emit(f"Extension will be extracted to: {directory}", "info")
        QApplication.processEvents()

    def browse_global_download_directory(self):
        """Opens a file dialog to set the global default download directory."""
        # Do NOT use 'global global_download_save_directory' here.
        # It's now managed by the settings object.
        directory = QFileDialog.getExistingDirectory(self, "Select Default Download Directory")
        if directory:
            settings.set('download_save_directory', directory) # Use settings.set()
            self.default_download_dir_entry.setText(directory)
            # Correctly emit the signal with 3 arguments (message, type, timeout_ms)
            self.show_status_signal.emit(f"Default download directory set to: {directory}", "success", 5000)
        else:
            self.show_status_signal.emit("No directory selected", "info", 5000)
        QApplication.processEvents()

    # def browse_input_file(self): # Commented out
    #     """Opens a file dialog to select an input media file for conversion."""
    #     file_path, _ = QFileDialog.getOpenFileName(
    #         self, "Select Input Media File", "",
    #         "Media Files (*.mp4 *.mkv *.avi *.mov *.wmv *.flv *.webm *.mp3 *.wav *.aac *.flac *.ogg);;All Files (*.*)"
    #     )
    #     if file_path:
    #         self.convert_input_file = file_path
    #         self.conversion_input_file_entry.setText(os.path.basename(file_path))
    #         self.show_status_signal.emit(f"Input file selected: {os.path.basename(file_path)}", "info")
    #     else:
    #         self.show_status_signal.emit("No input file selected", "info")
    #     QApplication.processEvents()

    # def browse_output_directory(self): # Commented out
    #     """Opens a file dialog to select an output directory for converted files."""
    #     directory = QFileDialog.getExistingDirectory(self, "Select Output Directory for Converted File")
    #     if directory:
    #         self.convert_output_directory = directory
    #         self.conversion_output_dir_entry.setText(directory)
    #         self.show_status_signal.emit(f"Output directory selected: {directory}", "info")
    #     else:
    #         self.show_status_signal.emit("No output directory selected", "info")
    #     QApplication.processEvents()

    def extract_browser_extension(self):
        """Initiates the extraction of the browser extension files."""
        target_dir = self.extension_path_entry.text().strip()
        if not target_dir:
            self.show_status_signal.emit("Please select a directory to extract the extension to", "error")
            return
        
        self.show_status_signal.emit(f"Extracting extension to {target_dir}...", "info")
        self.set_buttons_disabled_signal.emit(True)
        threading.Thread(target=self._extract_extension_thread, args=(target_dir,)).start()
        QApplication.processEvents()

    def _extract_extension_thread(self, target_dir):
        """Threaded function to handle browser extension extraction."""
        try:
            if not os.path.exists(EXTENSION_SOURCE_DIR_BUNDLE):
                raise FileNotFoundError(f"Extension source folder not found: {EXTENSION_SOURCE_DIR_BUNDLE}. Is it bundled correctly?")
            
            parent_dir = os.path.dirname(target_dir)
            if not os.path.exists(parent_dir):
                os.makedirs(parent_dir)

            if os.path.exists(target_dir):
                self.show_status_signal.emit(f"Deleting existing folder: {target_dir}...", "info")
                try:
                    shutil.rmtree(target_dir)
                    self.show_status_signal.emit("Existing folder deleted. Copying new extension...", "info")
                except OSError as e:
                    self.show_status_signal.emit(f"Error deleting existing folder: {e}. Please ensure it's not open or locked.", "error")
                    return

            shutil.copytree(EXTENSION_SOURCE_DIR_BUNDLE, target_dir)
            
            self.show_status_signal.emit(f"Extension extracted successfully to: {target_dir}\nNow, go to chrome://extensions/ in your browser, enable 'Developer mode', and click 'Load unpacked' to select this folder.", "success", timeout_ms=15000)
        except PermissionError:
            self.show_status_signal.emit(f"Permission denied to write to {target_dir}. Please choose a different directory or run as administrator.", "error")
        except FileNotFoundError as e:
            self.show_status_signal.emit(f"Error: {e}. Make sure 'extension' folder is in the same directory as app.py before building the executable.", "error")
        except Exception as e:
            self.show_status_signal.emit(f"Failed to extract extension: {e}", "error")
        finally:
            self.set_buttons_disabled_signal.emit(False)
        QApplication.processEvents()

    def toggle_browser_monitor(self, state):
        """Toggles the browser monitoring feature on/off."""
        new_status = self.browser_monitor_switch.isChecked()
        
        logger.debug(f"GUI toggle_browser_monitor called. New status: {new_status}")
        settings.set('browser_monitor_enabled', new_status) # Update setting via manager
        
        self.show_status_signal.emit(f"Browser monitoring {'enabled' if new_status else 'disabled'}", "success")
        
        threading.Thread(target=self._send_monitor_status_to_flask, args=(new_status,)).start()

    def _send_monitor_status_to_flask(self, enabled):
        """Sends the browser monitor status to the Flask server."""
        try:
            logger.debug(f"Sending browser monitor status to Flask: {enabled}")
            response = requests.post(f"http://localhost:{FLASK_PORT}/set_browser_monitor_status", json={"enabled": enabled}, timeout=1) # Short timeout
            response_data = response.json()
            logger.debug(f"Flask response to status update: {response_data}")
        except requests.exceptions.ConnectionError:
            logger.warning(f"Could not connect to internal Flask server to update monitor status.")
        except requests.exceptions.Timeout:
            logger.warning(f"Flask server status update request timed out.")
        except Exception as e:
            logger.error(f"An unexpected error occurred while sending monitor status: {e}")


    def open_add_download_dialog(self):
        """Opens a dialog for adding a new download via URL."""
        self.add_download_dialog = AddDownloadDialog(self)
        if self.add_download_dialog.exec() == QDialog.Accepted:
            url = self.add_download_dialog.url_entry.text().strip()
            media_type = self.add_download_dialog.media_type_group.checkedButton().text().lower()
            
            if not url:
                self.show_status_signal.emit("No URL provided for download", "error")
                return

            self.show_status_signal.emit(f"Initiating download for: {url}...", "info")
            payload = {"url": url, "media_type": media_type, "format_id": "highest"}
            threading.Thread(target=self.initiate_flask_download, args=(payload,)).start()
        QApplication.processEvents()

    def initiate_flask_download(self, payload):
        """Sends a download request to the Flask server in a thread."""
        self.set_buttons_disabled_signal.emit(True)
        try:
            response = requests.post(f"http://localhost:{FLASK_PORT}/download", json=payload, timeout=10)
            data = response.json()
            if response.ok:
                self.show_status_signal.emit(data.get('message', 'Download started.'), 'success')
            else:
                self.show_status_signal.emit(data.get('error', 'Failed to start download.'), 'error')
        except requests.exceptions.RequestException as e:
            self.show_status_signal.emit(f"Connection to server failed: {e}", 'error')
        finally:
            self.set_buttons_disabled_signal.emit(False)


    # def start_conversion_process(self): # Commented out
    #     """Initiates the media conversion process."""
    #     input_file = self.convert_input_file
    #     output_format = self.output_format_dropdown.currentText()
    #     output_dir = self.conversion_output_dir_entry.text().strip()
        
    #     target_video_codec_full = self.video_codec_dropdown.currentText()
    #     target_video_codec = target_video_codec_full.split(' ')[0].lower()

    #     if not input_file or not os.path.exists(input_file):
    #         self.show_status_signal.emit("Please select a valid input file", "error")
    #         return
    #     if not output_dir:
    #         self.show_status_signal.emit("Please select an output directory", "error")
    #         return
        
    #     self.show_status_signal.emit(f"Starting conversion to {output_format}...", "info")
    #     self.set_buttons_disabled_signal.emit(True)
    #     threading.Thread(target=self._conversion_thread, args=(input_file, output_format, output_dir, target_video_codec)).start()
    #     QApplication.processEvents()

    # def _conversion_thread(self, input_file, output_format, output_dir, target_video_codec): # Commented out
    #     """Threaded function to send conversion request to Flask server."""
    #     try:
    #         payload = {
    #             "input_path": input_file,
    #             "output_format": output_format,
    #             "output_dir": output_dir,
    #             "target_video_codec": target_video_codec
    #         }
    #         response = requests.post(f"http://localhost:{FLASK_PORT}/convert", json=payload)
    #         data = response.json()

    #         if data.get("status") == "success":
    #             self.show_status_signal.emit("Conversion started successfully!", "success")
    #         else:
    #             self.show_status_signal.emit(f"Conversion failed: {data.get('message', 'Unknown error.')}", "error")
    #     except requests.exceptions.ConnectionError:
    #         self.show_status_signal.emit(f"Could not connect to conversion server on port {FLASK_PORT}", "error")
    #     except Exception as e:
    #         self.show_status_signal.emit(f"An unexpected error occurred: {e}", "error")
    #     finally:
    #         self.set_buttons_disabled_signal.emit(False)
    #     QApplication.processEvents()



if __name__ == "__main__":
    # Start the Flask server in a background thread
    flask_thread = threading.Thread(target=start_flask_server, daemon=True)
    flask_thread.start()

    # Wait for Flask server to be ready before starting the GUI
    if not wait_for_flask_server():
        logger.critical("Failed to start Flask server. Exiting application.")
        sys.exit(1)

    # Now, start the PySide6 application
    app_qt = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app_qt.exec())