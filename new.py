import sys
import os 
import threading
import subprocess
import shutil
import queue
import re
import time
import json
import requests # For Flask communication
from datetime import datetime

# Import PySide6 modules
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QStackedWidget, QScrollArea, QFrame,
    QFileDialog, QComboBox, QLineEdit, QRadioButton, QButtonGroup,
    QProgressBar, QSizePolicy, QSpacerItem, QDialog, QGraphicsDropShadowEffect,
    QCheckBox, QStatusBar, QPlainTextEdit, QSplitter,
    QTableView, QHeaderView, QAbstractItemView, QStyledItemDelegate, QStyleOptionHeader
)
from PySide6.QtCore import (
    Qt, QTimer, QPropertyAnimation, QUrl, Signal, QEasingCurve, Property, QModelIndex, QPoint, QSize,
    QAbstractTableModel, QRect, QPointF, QRectF
)
from PySide6.QtGui import QColor, QFont, QDesktopServices, QIcon, QPixmap, QPalette, QPaintEvent, QPainter, QFontMetrics, QPen, QPolygonF, QCursor
from PySide6.QtWidgets import QStyleOption, QStyle # Needed for QCustomCheckBox paintEvent
from PySide6.QtSvg import QSvgRenderer

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

startupinfo = None
if sys.platform == 'win32':
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE


SUBPROCESS_CREATION_FLAGS = subprocess.DETACHED_PROCESS if sys.platform == 'win32' else 0

YT_DLP_BIN = resource_path('yt-dlp.exe') if sys.platform == 'win32' else resource_path('yt-dlp')
FFMPEG_BIN = resource_path(os.path.join('ffmpeg', 'bin', 'ffmpeg.exe')) if sys.platform == 'win32' else resource_path(os.path.join('ffmpeg', 'bin', 'ffmpeg'))
EXTENSION_SOURCE_DIR_BUNDLE = resource_path('extension')

# Global state variables
# This variable is shared between the main GUI thread and the Flask server thread.
browser_monitor_enabled = True # Initial state
global_download_save_directory = os.path.join(os.path.expanduser("~"), 'Downloads')
if not os.path.exists(global_download_save_directory):
    os.makedirs(global_download_save_directory, exist_ok=True)

# --- NEW GLOBAL SETTINGS VARIABLES ---
global_overwrite_existing_file = False # Default: do NOT overwrite, add suffix
global_double_click_action = "Open folder" # Default: open folder on double click
# --- END NEW GLOBAL SETTINGS VARIABLES ---

# Message queue for inter-thread communication (Flask to GUI)
gui_message_queue = queue.Queue()

# Ensure binaries exist (for debugging during development)
if not os.path.exists(YT_DLP_BIN):
    print(f"WARNING: yt-dlp binary not found at {YT_DLP_BIN}. Please ensure it's in the correct location or bundled.")
if not os.path.exists(FFMPEG_BIN):
    print(f"WARNING: ffmpeg binary not found at {FFMPEG_BIN}. Please ensure it's in the correct location or bundled.")


# --- Flask Server Setup (Integrated into the app) ---
from flask import Flask, request, jsonify
from flask_cors import CORS

flask_app = Flask(__name__)
CORS(flask_app)

@flask_app.after_request
def after_request(response):
    header = response.headers
    header['Access-Control-Allow-Origin'] = '*'
    header['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    header['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response

def run_command_in_bundle(command_parts, *args, check_return_code=True, **kwargs):
    """Helper to run yt-dlp or ffmpeg with correct paths and startup info."""
    # Ensure the binary path is correct
    if command_parts[0] == 'yt-dlp':
        command_parts[0] = YT_DLP_BIN
    elif command_parts[0] == 'ffmpeg':
        command_parts[0] = FFMPEG_BIN
    
    if not os.path.exists(command_parts[0]):
        raise FileNotFoundError(f"Required binary not found: {command_parts[0]}. Please ensure it's bundled correctly or in your system's PATH.")
    
    if sys.platform == 'win32':
        kwargs['startupinfo'] = startupinfo
        if 'creationflags' in kwargs:
            del kwargs['creationflags']
        
    # Pass the check_return_code argument to subprocess.run
    return subprocess.run(command_parts, *args, check=check_return_code, **kwargs)

@flask_app.route('/formats', methods=['POST'])
def get_formats_flask():
    """Fetches available formats for a given URL using yt-dlp."""
    global browser_monitor_enabled
    print(f"DEBUG: Flask /formats route accessed. Current browser_monitor_enabled: {browser_monitor_enabled}")
    if not browser_monitor_enabled:
        return jsonify({'status': 'error', 'message': 'Browser monitoring is currently disabled in the desktop app.'}), 403

    data = request.json
    url = data.get('url')
    media_type_requested = data.get('media_type', 'video')

    if not url:
        return jsonify({'status': 'error', 'message': 'No URL provided'}), 400

    try:
        # Always use --no-playlist when fetching formats to avoid fetching huge playlist info
        result = run_command_in_bundle(
            ['yt-dlp', '-F', '--no-playlist', url],
            capture_output=True,
            text=True,
            check=True,
            timeout=30
        )
        
        output_lines = result.stdout.splitlines()
        formats = []
        
        format_regex = re.compile(
            r'^(?P<id>\S+)\s+'           # ID
            r'(?P<ext>\S+)\s+'           # EXT (reported by yt-dlp)
            r'(?P<res_fps_hdr_ch>.*?)\s*\|\s*' # RESOLUTION/FPS/HDR/CH
            r'(?P<filesize_tbr_proto>.*?)\s*\|\s*' # FILESIZE/TBR/PROTO
            r'(?P<codec_info>.*)$'       # CODEC INFO
        )

        in_formats_section = False
        for line in output_lines:
            if 'ID  EXT' in line and 'FILESIZE' in line and 'RESOLUTION' in line:
                in_formats_section = True
                continue
            if not in_formats_section:
                continue
            if not line.strip():
                continue
            if line.strip().startswith('-') and len(set(line.strip())) == 1:
                continue
            
            stripped_line = line.strip()
            print(f"DEBUG: Processing line: '{stripped_line}'")
            match = format_regex.match(stripped_line)
            if match:
                group = match.groupdict()
                print(f"DEBUG: Regex matched. Captured groups: {group}")
                
                format_id = group['id'].strip()
                ext_raw = group['ext'].strip() # The 'ext' as reported by yt-dlp
                res_fps_hdr_ch = group['res_fps_hdr_ch'].strip()
                filesize_tbr_proto = group['filesize_tbr_proto'].strip()
                codec_info = group['codec_info'].strip()

                if 'images' in codec_info.lower() or 'storyboard' in codec_info.lower():
                    continue

                filesize_match = re.search(r'~?([\d.]+)([KMGTPE]?iB)', filesize_tbr_proto)
                filesize = filesize_match.group(0) if filesize_match else 'N/A'
                
                resolution = None
                resolution_match = re.search(r'(\d+x\d+)', res_fps_hdr_ch)
                if resolution_match:
                    resolution = resolution_match.group(1)
                
                bitrate_match = re.search(r'(\d+)k', filesize_tbr_proto)
                bitrate = int(bitrate_match.group(1)) if bitrate_match else 0

                has_video = resolution is not None and resolution != '0x0'
                
                has_audio = ('audio only' in res_fps_hdr_ch.lower() or 
                             'audio only' in codec_info.lower() or 
                             bool(re.search(r'mp4a|aac|opus|vorbis|flac|wav|mp3', codec_info.lower()))
                            ) and 'video only' not in codec_info.lower()

                is_audio_only = False
                is_video_only = False
                is_combined = False

                if has_video and has_audio:
                    is_combined = True
                elif has_video and not has_audio:
                    is_video_only = True
                elif has_audio and not has_video:
                    is_audio_only = True

                is_lossless_audio = ext_raw.lower() in ['flac', 'wav']

                # --- NEW LOGIC: Determine actual 'ext' for display based on protocol and codec ---
                final_ext = ext_raw # Start with the raw extension
                protocol_match = re.search(r'\b(https|m3u8|dash|hls)\b', filesize_tbr_proto)
                protocol = protocol_match.group(1) if protocol_match else 'unknown'

                if protocol == 'm3u8':
                    # For m3u8, the 'ext' column might be a suggestion.
                    # We need to infer the actual target extension from codec info.
                    if 'mp4a' in codec_info.lower() or 'aac' in codec_info.lower():
                        final_ext = 'mp4' # For AAC audio in HLS, it's typically MP4
                    elif 'opus' in codec_info.lower() or 'vorbis' in codec_info.lower() or 'vp9' in codec_info.lower():
                        final_ext = 'webm' # For Opus/Vorbis audio or VP9 video in HLS, it's typically WebM
                    elif 'avc1' in codec_info.lower():
                        final_ext = 'mp4' # For AVC video in HLS, it's typically MP4
                    else:
                        final_ext = ext_raw # Fallback to raw if no specific codec hint
                # --- END NEW LOGIC ---

                # --- REFINED LABEL GENERATION ---
                label_parts = []
                label_parts.append(f"ID: {format_id}")
                label_parts.append(f"Ext: {final_ext}") # Use the determined final_ext for the label

                if is_audio_only:
                    label_parts.append("Audio Only")
                    audio_codec_match = re.search(r'(mp4a|aac|opus|vorbis|flac|wav|mp3)', codec_info.lower())
                    if audio_codec_match:
                        label_parts.append(f"Codec: {audio_codec_match.group(1).upper()}")
                    if bitrate > 0:
                        label_parts.append(f"Bitrate: {bitrate}k")
                elif is_video_only:
                    label_parts.append("Video Only - No Audio")
                    if resolution:
                        label_parts.append(f"Res: {res_fps_hdr_ch}") # Use the full resolution/fps/ch string
                elif is_combined:
                    label_parts.append("Video + Audio")
                    if resolution:
                        label_parts.append(f"Res: {res_fps_hdr_ch}") # Use the full resolution/fps/ch string
                    
                if filesize != 'N/A':
                    label_parts.append(f"Size: {filesize}")
                
                label = " | ".join(label_parts)
                # --- END REFINED LABEL GENERATION ---

                format_entry = {
                    'id': format_id, 
                    'ext': final_ext, # Store the determined final_ext
                    'label': label,
                    'is_audio_only': is_audio_only, 
                    'is_video_only': is_video_only, 
                    'is_combined': is_combined,
                    'is_best_quality': False, 
                    'is_lossless_audio': is_lossless_audio,
                    'resolution_pixels': int(resolution.split('x')[1]) if resolution and 'x' in resolution else 0,
                    'bitrate_kbps': bitrate
                }
                formats.append(format_entry)
                print(f"DEBUG: Final format entry: {format_entry}")

        max_audio_bitrate = 0
        for f in formats:
            if f['is_audio_only'] and f['bitrate_kbps'] > max_audio_bitrate:
                max_audio_bitrate = f['bitrate_kbps']
        
        for f in formats:
            if f['is_audio_only'] and f['bitrate_kbps'] == max_audio_bitrate and max_audio_bitrate > 0 and not f['is_lossless_audio']:
                f['is_best_quality'] = True

        formats_to_return = []
        if media_type_requested == 'video':
            formats_to_return = [f for f in formats if f['is_combined'] or f['is_video_only']]
        elif media_type_requested == 'audio':
            formats_to_return = [f for f in formats if f['is_audio_only']]
        else:
            formats_to_return = formats # Return all if type is not specified or unknown

        print(f"DEBUG: Returning {len(formats_to_return)} formats for media_type_requested={media_type_requested}")
        return jsonify({'status': 'success', 'formats': formats_to_return})

    except FileNotFoundError:
        print(f"ERROR: yt-dlp binary not found at {YT_DLP_BIN}")
        return jsonify({'status': 'error', 'message': f"yt-dlp binary not found at {YT_DLP_BIN}. Please ensure it's bundled correctly."}), 500
    except subprocess.TimeoutExpired:
        print(f"ERROR: yt-dlp command timed out after 30 seconds for URL: {url}")
        return jsonify({'status': 'error', 'message': f"Failed to fetch formats: yt-dlp timed out. Please try again or check the URL."}), 500
    except subprocess.CalledProcessError as e:
        print(f"ERROR: yt-dlp failed with exit code {e.returncode}")
        print(f"yt-dlp STDOUT on error: {e.stdout}")
        print(f"yt-dlp STDERR on error: {e.stderr}")
        return jsonify({'status': 'error', 'message': f"Failed to fetch formats: {e.stderr}"}), 500
    except Exception as e:
        print(f"GENERAL ERROR in /formats: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500

@flask_app.route('/download', methods=['POST'])
def download_flask():
    """Initiates a download process in a separate thread."""
    global browser_monitor_enabled # Access global variable directly
    print(f"DEBUG: Flask /download route accessed. Current browser_monitor_enabled: {browser_monitor_enabled}") # Debug print
    if not browser_monitor_enabled:
        return jsonify({'status': 'error', 'message': 'Browser monitoring is currently disabled in the desktop app.'}), 403

    data = request.json
    url = data.get('url')
    format_id = data.get('format_id') 
    media_type = data.get('media_type', 'video')
    download_type = data.get('download_type', 'specific_format')

    output_path = global_download_save_directory

    if not url:
        return jsonify({'status': 'error', 'message': 'No URL provided'}), 400
    
    if download_type == 'specific_format' and not format_id:
        return jsonify({'status': 'error', 'message': 'Format ID is required for specific format download'}), 400

    try:
        full_output_path = os.path.abspath(output_path)
        if not os.path.exists(full_output_path):
            os.makedirs(full_output_path)
            
        yt_dlp_command_template = []
        is_merged_download = False
        
        if download_type == 'highest_quality':
            if media_type == 'video':
                yt_dlp_command_template = [
                    YT_DLP_BIN,
                    '-f', 'bv*+ba/b',
                    url,
                    '--ffmpeg-location', FFMPEG_BIN,
                    '--verbose',
                    '-o', os.path.join(full_output_path, '%(title)s.%(ext)s')
                ]
                is_merged_download = True

            elif media_type == 'audio':
                yt_dlp_command_template = [
                    YT_DLP_BIN,
                    '-f', 'bestaudio',
                    url,
                    '--ffmpeg-location', FFMPEG_BIN,
                    '--verbose',
                    '-o', os.path.join(full_output_path, '%(title)s.%(ext)s')
                ]
            else:
                return jsonify({'status': 'error', 'message': 'Invalid media type for highest quality download.'}), 400
        
        elif download_type == 'specific_format':
            yt_dlp_command_template = [
                YT_DLP_BIN,
                '-f', format_id,
                url,
                '--ffmpeg-location', FFMPEG_BIN,
                '--verbose',
                '-o', os.path.join(full_output_path, '%(title)s.%(ext)s')
            ]
            if format_id == "best_video_best_audio": 
                is_merged_download = True
        else:
            return jsonify({'status': 'error', 'message': 'Invalid download type.'}), 400

        is_playlist = 'playlist?list=' in url or '/playlist/' in url or '/watch?v=' in url and '&list=' in url

        # --- Create a threading.Event for this specific download ---
        cancel_event = threading.Event()
        # --- End threading.Event creation ---

        gui_message_queue.put({
            'type': 'add_download',
            'url': url,
            'format_id': format_id if download_type == 'specific_format' else download_type,
            'output_path': full_output_path,
            'status': 'Initializing',
            'progress': '0%',
            'filename': os.path.basename(url),
            'is_playlist': is_playlist,
            'media_type': media_type,
            'is_merged_download': is_merged_download,
            'timestamp': time.time(),
            'filesize_bytes': 0,
            'cancel_event': cancel_event # <--- Pass the event through the queue
        })

        # Start download in a new thread, passing the cancel_event
        threading.Thread(target=_perform_yt_dlp_download, args=(yt_dlp_command_template, url, is_playlist, media_type, is_merged_download, full_output_path, cancel_event)).start()

        return jsonify({'status': 'success', 'message': 'Download initiated successfully!'})

    except FileNotFoundError:
        return jsonify({'status': 'error', 'message': f"yt-dlp binary not found at {YT_DLP_BIN}. Please ensure it's bundled correctly."}), 500
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500



# Helper function to parse filesize string (e.g., "1.23MiB") to bytes
def parse_filesize_to_bytes(filesize_str):
    if not isinstance(filesize_str, str):
        return 0
    filesize_str = filesize_str.strip().upper()
    if filesize_str == 'N/A':
        return 0

    match = re.match(r'~?([\d.]+)([KMGTPE]?IB)', filesize_str)
    if not match:
        return 0

    value = float(match.group(1))
    unit = match.group(2)

    multipliers = {
        'B': 1, 'KIB': 1024, 'MIB': 1024**2, 'GIB': 1024**3,
        'TIB': 1024**4, 'PIB': 1024**5, 'EIB': 1024**6
    }
    return int(value * multipliers.get(unit, 1))

# Helper function to format bytes to human-readable string
def format_bytes(bytes_val):
    if bytes_val == 0:
        return "0 B"
    units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB', 'EiB']
    i = 0
    while bytes_val >= 1024 and i < len(units) - 1:
        bytes_val /= 1024
        i += 1
    return f"{bytes_val:.2f} {units[i]}"

# Modified _perform_yt_dlp_download signature to accept cancel_event
def _perform_yt_dlp_download(command_template, url, is_playlist, media_type, is_merged_download, download_dir, cancel_event):
    """
    Performs the yt-dlp download operation in a separate thread.
    Handles filename generation and communicates progress to the GUI.
    """
    # Initial filename, will be updated as download progresses
    filename_base = "Playlist Download" if is_playlist else os.path.basename(url) 
    final_downloaded_file_path = None # This will hold the path to the final downloaded file
    
    # Initialize these to None, will be determined based on actual downloaded content
    detected_filetype = 'unknown' 
    filename = filename_base # Initialize filename for GUI display

    # Initialize stdout/stderr lines and outputs here so they are always defined
    stdout_lines = []
    stderr_lines = []
    stdout_output = "" 
    stderr_output = "" 
    current_process = None # Initialize process to None

    try:
        # --- Determine the final output path and filename with overwrite/suffix logic ---
        info_command = [
            YT_DLP_BIN,
            '--get-filename',
            '-o', '%(title)s.%(ext)s',
            url
        ]
        if not is_playlist:
            info_command.insert(1, '--no-playlist')

        info_result = run_command_in_bundle(
            info_command,
            capture_output=True,
            text=True,
            check_return_code=False,
            timeout=15
        )
        predicted_filename = info_result.stdout.strip()
        
        if not predicted_filename and info_result.returncode != 0:
            error_detail = f"STDOUT: {info_result.stdout.strip()}\nSTDERR: {info_result.stderr.strip()}"
            raise Exception(f"Failed to predict filename (exit code {info_result.returncode}):\n{error_detail}")
        
        if not is_playlist:
            base_name, ext = os.path.splitext(predicted_filename)
            ext = ext.lstrip('.')
            
            current_filename = predicted_filename
            counter = 0
            
            while not global_overwrite_existing_file and os.path.exists(os.path.join(download_dir, current_filename)):
                counter += 1
                current_filename = f"{base_name} ({counter}).{ext}"
            
            final_output_template = os.path.join(download_dir, current_filename)
            filename = current_filename
        else:
            final_output_template = os.path.join(download_dir, '%(playlist_title)s/%(title)s.%(ext)s')
            if '--yes-playlist' not in command_template:
                try:
                    url_index = command_template.index(url)
                    command_template.insert(url_index, '--yes-playlist')
                except ValueError:
                    command_template.insert(1, '--yes-playlist') 

        output_arg_index = -1
        for i, arg in enumerate(command_template):
            if arg == '-o' and i + 1 < len(command_template):
                output_arg_index = i + 1
                break
        
        if output_arg_index != -1:
            command_template[output_arg_index] = final_output_template
        else:
            command_template.extend(['-o', final_output_template])

        gui_message_queue.put({
            'type': 'update_download_status', 
            'url': url, 
            'filename': filename, 
            'status': 'Initializing'
        })

        final_command = list(command_template)
        
        if '--no-playlist' in final_command:
            final_command.remove('--no-playlist')
        if '--yes-playlist' in final_command:
            final_command.remove('--yes-playlist')

        if not is_playlist:
            try:
                url_index = final_command.index(url)
                final_command.insert(url_index, '--no-playlist')
            except ValueError:
                final_command.insert(1, '--no-playlist') 
        elif is_playlist:
            try:
                url_index = final_command.index(url)
                final_command.insert(url_index, '--yes-playlist')
            except ValueError:
                final_command.insert(1, '--yes-playlist')
        
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
        # IMPORTANT: Send the process object AND the cancel_event to the main GUI thread for tracking
        gui_message_queue.put({'type': 'add_process', 'url': url, 'process': current_process, 'cancel_event': cancel_event}) 
        
        while True:
            # --- Check for cancellation event first in the loop ---
            if cancel_event.is_set():
                print(f"DEBUG: Cancellation event set for {url}. Breaking download loop.")
                break
            # --- End cancellation check ---

            stdout_line = current_process.stdout.readline()
            stderr_line = current_process.stderr.readline()

            # If process has terminated and no more output, break
            if not stdout_line and not stderr_line and current_process.poll() is not None:
                break
            
            # If process has terminated but there's still output to read, continue reading
            if current_process.poll() is not None and (stdout_line or stderr_line):
                pass # Continue processing lines
            
            # Check for external termination (e.g., by taskkill)
            if current_process.poll() is not None and current_process.returncode != 0:
                stdout_output = "".join(stdout_lines) 
                stderr_output = "".join(stderr_lines) 
                if current_process.returncode == -15 or current_process.returncode == 1: # SIGTERM or common Windows termination code
                    print(f"DEBUG: Process for {url} terminated by external signal (exit code: {current_process.returncode}).")
                    break # Exit loop, will handle cancellation status below
                else:
                    raise Exception(f"Download process terminated unexpectedly with exit code {current_process.returncode}")

            if stdout_line:
                stdout_lines.append(stdout_line)
                if '[download]' in stdout_line and '%' in stdout_line:
                    match = re.search(r'(\d+\.\d+)%', stdout_line)
                    if match:
                        progress = match.group(1) + '%'
                        gui_message_queue.put({'type': 'update_download_status', 'url': url, 'status': 'Downloading', 'progress': progress})
                elif '[ExtractAudio]' in stdout_line or '[ffmpeg]' in stdout_line:
                    gui_message_queue.put({'type': 'update_download_status', 'url': url, 'status': 'Processing'})
                elif '[download] playlist:' in stdout_line:
                    gui_message_queue.put({'type': 'update_download_status', 'url': url, 'status': 'Downloading Playlist'})

            if stderr_line:
                stderr_lines.append(stderr_line)
            
            # --- Small sleep to yield control and allow event check to be more frequent ---
            # This is a compromise for responsiveness if there's no output for a while.
            if not stdout_line and not stderr_line:
                time.sleep(0.05) # Sleep for 50ms if no output
            # --- End small sleep ---

        stdout_output = "".join(stdout_lines) 
        stderr_output = "".join(stderr_lines) 

        return_code = current_process.poll() # Get final return code after loop

        # --- Handle cancellation based on event or return code ---
        if cancel_event.is_set() or (return_code != 0 and (return_code == -15 or return_code == 1)):
            gui_message_queue.put({'type': 'update_download_status', 'url': url, 'status': 'Cancelled', 'progress': '0%', 'message': 'Download cancelled by user.'})
            # Attempt to remove partially downloaded files if cancelled
            if final_output_template and os.path.exists(final_output_template):
                try:
                    os.remove(final_output_template)
                    print(f"Cleaned up partially downloaded file: {final_output_template}")
                except Exception as e:
                    print(f"Error cleaning up partially downloaded file {final_output_template}: {e}")
            part_file = final_output_template + ".part"
            if os.path.exists(part_file):
                try:
                    os.remove(part_file)
                    print(f"Cleaned up temporary part file: {part_file}")
                except Exception as e:
                    print(f"Error cleaning up temporary part file {part_file}: {e}")
            return # Exit the thread as it was cancelled
        # --- End cancellation handling ---

        # If return_code is not 0 (and not a cancellation code), then it's a genuine failure
        if return_code != 0: 
            error_detail = f"STDOUT: {stdout_output.strip()}\nSTDERR: {stderr_output.strip()}"
            raise Exception(f"yt-dlp download failed (exit code {return_code}):\n{error_detail}")
        
        # --- SMARTER APPROACH: Use the pre-calculated final_output_template as the expected path ---
        final_downloaded_file_path = final_output_template
        
        # Crucial check: Verify that the file actually exists at the predicted path
        # Only check if the process completed successfully (return_code == 0)
        if not os.path.exists(final_downloaded_file_path):
            error_message = (
                f"Final downloaded file not found at expected path: {final_downloaded_file_path}. "
                f"This might indicate a problem with yt-dlp's actual output path or file system operations. "
                f"STDOUT: {stdout_output.strip()}\nSTDERR: {stderr_output.strip()}"
            )
            raise Exception(error_message)


        # --- Post-download/merge checks and final update (only if successful) ---
        actual_filesize_bytes = os.path.getsize(final_downloaded_file_path)
        actual_filename = os.path.basename(final_downloaded_file_path)

        if is_playlist:
            detected_filetype = 'playlist'
        elif media_type == 'video':
            detected_filetype = 'video'
        elif media_type == 'audio':
            detected_filetype = 'audio'
        else:
            detected_filetype = 'unknown'

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
            'path': final_downloaded_file_path,
            'timestamp': time.time(),
            'filesize_bytes': actual_filesize_bytes
        })

    except Exception as e:
        error_message = str(e)
        if 'stdout_output' not in locals():
            stdout_output = ""
        if 'stderr_output' not in locals():
            stderr_output = ""

        gui_message_queue.put({'type': 'update_download_status', 'url': url, 'status': 'Failed', 'message': error_message, 'filename': filename})
        print(f"Error during download for {url}: {error_message}")
        print(f"STDOUT (on error): {stdout_output.strip()}")
        print(f"STDERR (on error): {stderr_output.strip()}")
        import traceback
        traceback.print_exc()
    finally:
        # Ensure current_process is not None before trying to poll/kill it
        if current_process and current_process.poll() is None:
            try:
                current_process.kill() # Force kill if still running
                current_process.wait()
                print(f"DEBUG: Process for {url} force-killed in finally block.")
            except Exception as e:
                print(f"Error ensuring process {url} is killed in finally block: {e}")
        gui_message_queue.put({'type': 'remove_process', 'url': url})




@flask_app.route('/set_browser_monitor_status', methods=['POST'])
def set_browser_monitor_status():
    """Endpoint to enable/disable browser monitoring."""
    global browser_monitor_enabled # Modify global variable in Flask's thread context
    data = request.json
    status = data.get('enabled')
    print(f"DEBUG: Flask received browser monitor status update: {status}") # Debug print
    if isinstance(status, bool):
        browser_monitor_enabled = status
        print(f"DEBUG: Flask browser_monitor_enabled set to: {browser_monitor_enabled}") # Debug print
        return jsonify({'status': 'success', 'message': f'Browser monitoring set to {status}'})
    print(f"DEBUG: Flask received invalid status: {status}") # Debug print
    return jsonify({'status': 'error', 'message': 'Invalid status provided'}), 400


# --- PySide6 GUI Setup ---

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

        if self.isChecked():
            # Checked state: active color for track, circle on the right
            painter.setBrush(self._activeColor)
            painter.drawRoundedRect(0, 0, track_width, track_height, track_radius, track_radius)
            painter.setBrush(self._circleColor)
            painter.drawEllipse(self.pos, thumb_y, thumb_size, thumb_size)
        else:
            # Unchecked state: background color for track, circle on the left
            painter.setBrush(self.bgColor)
            painter.drawRoundedRect(0, 0, track_width, track_height, track_radius, track_radius)
            painter.setBrush(self._circleColor)
            painter.drawEllipse(self.pos, thumb_y, thumb_size, thumb_size)

        painter.end()

# Re-add QSvgRenderer import if it was removed, as SidebarButton uses it.
from PySide6.QtSvg import QSvgRenderer 

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
            # Let's say we want 2px padding from the top of the header.
            label_y = section_rect.top() + 2 # 2px from the top of the section
            
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


    def setModel(self, model):
        super().setModel(model)
        # Re-evaluate sort indicator position when model changes (e.g., when data is filtered)
        self._update_sort_indicator_position()




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


# Main Application Window
class MainWindow(QMainWindow):
    # Signals for updating GUI from Flask thread
    add_download_signal = Signal(dict)
    update_download_status_signal = Signal(str, dict) # url, new_data_dict
    add_completed_signal = Signal(dict)
    # add_conversion_signal = Signal(dict) # Commented out
    # update_conversion_status_signal = Signal(str, str, str, str) # input_path, status, progress, message # Commented out
    show_status_signal = Signal(str, str) # message, type
    set_buttons_disabled_signal = Signal(bool)

    def __init__(self):
        super().__init__()
        print(f"DEBUG: MainWindow initialized. Initial global browser_monitor_enabled: {browser_monitor_enabled}") # Debug print
        self.setWindowTitle("Universal Media Tool")
        # Set a reasonable initial size and minimum size
        self.setGeometry(100, 100, 1000, 700) # Adjusted initial size
        self.setMinimumSize(800, 600) # Set a minimum size to prevent it from becoming too small

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
        self.active_processes = {} # <--- MODIFIED: Now stores dict with process and event
        
        self.current_panel_type = "active_downloads" # Initialize panel type early
        
        self.init_ui()
        self.apply_stylesheet()
        self.connect_signals()

        # Timer for clearing status bar message
        self.status_clear_timer = QTimer(self)
        self.status_clear_timer.setSingleShot(True) # Only run once per start
        self.status_clear_timer.timeout.connect(self._clear_status_bar)

        # Start a QTimer to periodically check the Flask message queue
        self.queue_timer = QTimer(self)
        self.queue_timer.timeout.connect(self.check_flask_message_queue)
        self.queue_timer.start(100) # Check every 100 ms

        # Show default panel and check its button AFTER all UI elements are initialized
        self.show_panel(self.active_downloads_panel)
        self.active_downloads_button.setChecked(True)



    def apply_stylesheet(self):
        # Base dark theme
        self.setStyleSheet("""
            stylesheet goes here""")

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

        global global_double_click_action # Access the global setting

        if global_double_click_action == "Open folder":
            self.open_selected_folder()
        elif global_double_click_action == "Open file":
            self.open_selected_file()
        else:
            self.show_status("Unknown double-click action configured.", "error")



    def show_status(self, message, msg_type='info', timeout_ms=5000): # Added timeout_ms parameter
        """Updates the status bar with a given message and color, clears after timeout."""
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

        # Temporarily update the model's internal data and reset
        current_model.layoutAboutToBeChanged.emit()
        current_model._data = filtered_data # Directly modify the underlying list
        current_model.layoutChanged.emit()
        
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
        self.filter_current_view(self.search_input.text())
        self.show_status("View refreshed.", "info")


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


    def create_settings_panel(self):
        """Creates the application settings panel."""
        panel = QWidget()
        panel.setObjectName("SettingsPanel")
        panel.setLayout(QVBoxLayout())
        panel.layout().setContentsMargins(20, 20, 20, 20)
        panel.layout().setSpacing(15)

        self.browser_monitor_switch = QCustomCheckBox("Enable Browser Monitoring", self)
        self.browser_monitor_switch.setChecked(browser_monitor_enabled) 
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
        
        dir_frame = QFrame()
        dir_frame.setLayout(QHBoxLayout())
        dir_frame.layout().setContentsMargins(0, 0, 0, 0)
        
        dir_label = QLabel("Download Directory:")
        dir_label.setFixedWidth(120)
        dir_frame.layout().addWidget(dir_label)
        
        self.default_download_dir_entry = QLineEdit()
        self.default_download_dir_entry.setReadOnly(True)
        self.default_download_dir_entry.setText(global_download_save_directory)
        dir_frame.layout().addWidget(self.default_download_dir_entry)
        
        self.browse_default_download_dir_button = QPushButton("Browse...")
        self.browse_default_download_dir_button.setFixedSize(80, 30)
        self.browse_default_download_dir_button.setStyleSheet("QPushButton:focus { outline: none; }")
        self.browse_default_download_dir_button.setCursor(Qt.PointingHandCursor)
        self.browse_default_download_dir_button.clicked.connect(self.browse_global_download_directory)
        dir_frame.layout().addWidget(self.browse_default_download_dir_button)
        panel.layout().addWidget(dir_frame)

        dir_help_label = QLabel("This is the default folder where all your downloads will be saved.")
        dir_help_label.setStyleSheet("color: #adb5bd; font-size: 11px; margin-left: 125px;")
        dir_help_label.setWordWrap(True)
        panel.layout().addWidget(dir_help_label)

        self.overwrite_checkbox = QCustomCheckBox("Overwrite existing file", self)
        self.overwrite_checkbox.setChecked(global_overwrite_existing_file)
        self.overwrite_checkbox.stateChanged.connect(self._toggle_overwrite_setting)
        panel.layout().addWidget(self.overwrite_checkbox)

        overwrite_help_label = QLabel("If checked, new downloads will replace existing files with the same name. Otherwise, a suffix (e.g., '(1)') will be added.")
        overwrite_help_label.setStyleSheet("color: #adb5bd; font-size: 11px; margin-left: 25px;")
        overwrite_help_label.setWordWrap(True)
        panel.layout().addWidget(overwrite_help_label)
        
        double_click_frame = QFrame()
        double_click_frame.setLayout(QHBoxLayout())
        double_click_frame.layout().setContentsMargins(0, 0, 0, 0)
        
        double_click_label = QLabel("Double click on download item:")
        double_click_label.setFixedWidth(200)
        double_click_frame.layout().addWidget(double_click_label)
        
        self.double_click_action_dropdown = QComboBox()
        self.double_click_action_dropdown.addItems(["Open folder", "Open file"])
        self.double_click_action_dropdown.setCurrentText(global_double_click_action) 
        self.double_click_action_dropdown.currentIndexChanged.connect(self._update_double_click_action)
        double_click_frame.layout().addWidget(self.double_click_action_dropdown)
        double_click_frame.layout().addStretch(1)
        panel.layout().addWidget(double_click_frame)

        double_click_help_label = QLabel("Choose what happens when you double-click a download in the list.")
        double_click_help_label.setStyleSheet("color: #adb5bd; font-size: 11px; margin-left: 25px;")
        double_click_help_label.setWordWrap(True)
        panel.layout().addWidget(double_click_help_label)


        panel.layout().addStretch(1)

        button_layout = QHBoxLayout()
        button_layout.addStretch(1)
        
        # --- FIX: Unique back button name ---
        self.back_button_dl = QPushButton("Back")
        self.back_button_dl.setFixedSize(100, 30)
        self.back_button_dl.setStyleSheet("QPushButton:focus { outline: none; }")
        self.back_button_dl.setCursor(Qt.PointingHandCursor)
        self.back_button_dl.clicked.connect(lambda: self.show_panel(self.settings_panel))
        button_layout.addWidget(self.back_button_dl)
        panel.layout().addLayout(button_layout)
        
        return panel

    def _toggle_overwrite_setting(self, state):
        """Updates the global_overwrite_existing_file based on checkbox state."""
        global global_overwrite_existing_file
        global_overwrite_existing_file = self.overwrite_checkbox.isChecked()
        self.show_status(f"Overwrite existing file set to: {global_overwrite_existing_file}", "info")

    def _update_double_click_action(self, index):
        """Updates the global_double_click_action based on dropdown selection."""
        global global_double_click_action
        global_double_click_action = self.double_click_action_dropdown.currentText()
        self.show_status(f"Double-click action set to: {global_double_click_action}", "info")


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

    def update_settings_display(self):
        print(f"DEBUG: update_settings_display called. Reading global browser_monitor_enabled: {browser_monitor_enabled}")
        self.browser_monitor_switch.setChecked(browser_monitor_enabled)
        QApplication.processEvents()

    def update_extension_setup_display(self):
        self.extension_path_entry.setText(os.path.join(os.path.expanduser("~"), "Downloads", "universal_media_tool_extension"))
        QApplication.processEvents()

    def update_download_settings_display(self):
        self.default_download_dir_entry.setText(global_download_save_directory)
        self.overwrite_checkbox.setChecked(global_overwrite_existing_file)
        self.double_click_action_dropdown.setCurrentText(global_double_click_action)
        QApplication.processEvents()

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
            # No 'else' block here. If found_index is -1, it means an update message
            # was received for an item not yet in the list, which indicates a logic
            # flow issue where 'add_download' might have been skipped or not processed yet.
            # For now, we assume 'add_download' always precedes 'update_download_status'.
        else:
            # This case should ideally not happen if 'add_download' is always sent first.
            # If it does, it means an update came for an item not yet added.
            # For robustness, we can log this as a warning, but we won't add it here
            # to prevent duplicates if 'add_download' comes later.
            print(f"WARNING: Received update for unknown download URL: {url}. Data: {new_data_dict}")


        current_status = new_data_dict.get('status', 'N/A')
        filename = new_data_dict.get('filename', url)
        message = new_data_dict.get('message', '')

        if current_status == 'Completed':
            self.show_status(f"Download completed: {filename}", "success")
        elif current_status == 'Failed' or current_status == 'Error':
            print(f"ERROR: Download for {url} {current_status}. Message: {message}")
            self.show_status(f"Download {current_status}: {filename}. Error: {message}", "error", timeout_ms=10000)
        
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



    def check_flask_message_queue(self):
        """Periodically checks the Flask message queue for updates to the GUI."""
        try:
            while True:
                message = gui_message_queue.get_nowait()
                if message['type'] == 'add_download':
                    # Extract cancel_event before emitting to add_download_to_list
                    cancel_event = message.pop('cancel_event', None) # Remove it from message dict
                    self.add_download_signal.emit(message) # Emit the rest of the message data
                    if cancel_event:
                        # Add the process and its associated cancel_event to the tracker
                        # This is where the process and event are stored in self.active_processes
                        self._add_process_to_tracker(message['url'], {'process': None, 'cancel_event': cancel_event})
                        # Note: The 'process' itself is added later in _perform_yt_dlp_download
                        # via another 'add_process' message type. This is a slight redundancy
                        # but ensures the cancel_event is available early.
                elif message['type'] == 'add_process': # This message is sent from _perform_yt_dlp_download
                    # Update the stored process object for the URL
                    url = message['url']
                    process_obj = message['process']
                    if url in self.active_processes:
                        self.active_processes[url]['process'] = process_obj
                        print(f"DEBUG: Process object updated for URL: {url}")
                    else:
                        # Fallback: if add_download didn't register it, add it now (less ideal but robust)
                        self.active_processes[url] = {'process': process_obj, 'cancel_event': threading.Event()} # Create new event if missing
                        print(f"WARNING: Process added for URL {url} without prior add_download message.")

                elif message['type'] == 'update_download_status':
                    self.update_download_status_signal.emit(
                        message['url'], 
                        {k: v for k, v in message.items() if k != 'type' and k != 'url'}
                    )
                elif message['type'] == 'add_completed':
                    self.add_completed_signal.emit(message)
        except queue.Empty:
            pass
        except Exception as e:
            print(f"Error checking Flask message queue: {e}")
            import traceback
            traceback.print_exc()

    def browse_extension_directory(self):
        """Opens a file dialog to select the directory for browser extension extraction."""
        directory = QFileDialog.getExistingDirectory(self, "Select Directory to Extract Browser Extension")
        if directory:
            self.extension_path_entry.setText(os.path.join(directory, "universal_media_tool_extension"))
            self.show_status_signal.emit(f"Extension will be extracted to: {directory}", "info")
        QApplication.processEvents()

    def browse_global_download_directory(self):
        """Opens a file dialog to set the global default download directory."""
        global global_download_save_directory
        directory = QFileDialog.getExistingDirectory(self, "Select Default Download Directory")
        if directory:
            global_download_save_directory = directory
            self.default_download_dir_entry.setText(directory)
            self.show_status_signal.emit(f"Default download directory set to: {directory}", "success")
        else:
            self.show_status_signal.emit("No directory selected", "info")
        QApplication.processEvents()

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
        global browser_monitor_enabled
        new_status = self.browser_monitor_switch.isChecked()
        
        print(f"DEBUG: GUI toggle_browser_monitor called. New status: {new_status}")
        browser_monitor_enabled = new_status
        
        self.show_status_signal.emit(f"Browser monitoring {'enabled' if new_status else 'disabled'}", "success")
        
        threading.Thread(target=self._send_monitor_status_to_flask, args=(new_status,)).start()

    def _send_monitor_status_to_flask(self, enabled):
        """Sends the browser monitor status to the Flask server."""
        try:
            print(f"DEBUG: Sending browser monitor status to Flask: {enabled}")
            response = requests.post(f"http://localhost:{FLASK_PORT}/set_browser_monitor_status", json={"enabled": enabled})
            response_data = response.json()
            print(f"DEBUG: Flask response to status update: {response_data}")
        except requests.exceptions.ConnectionError:
            print(f"Warning: Could not connect to internal Flask server to update monitor status.")
        except Exception as e:
            print(f"An unexpected error occurred: {e}")


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
            self.set_buttons_disabled_signal.emit(True)
            threading.Thread(target=self._direct_download_thread, args=(url, media_type, 'highest_quality', None)).start()
        QApplication.processEvents()

    def _direct_download_thread(self, url, media_type, download_type, format_id):
        """Threaded function to send download request to Flask server."""
        try:
            # Infer if it's a playlist from the URL for direct downloads
            is_playlist = 'playlist?list=' in url or '/playlist/' in url or '/watch?v=' in url and '&list=' in url

            payload = {
                "url": url,
                "media_type": media_type,
                "download_type": download_type,
                "is_playlist": is_playlist # Pass the inferred playlist status
            }
            if format_id:
                payload["format_id"] = format_id

            download_response = requests.post(f"http://localhost:{FLASK_PORT}/download", json=payload)
            download_data = download_response.json()

            if download_data.get("status") == "success":
                self.show_status_signal.emit(f"Download started for {url}!", "success")
            else:
                self.show_status_signal.emit(f"Download failed: {download_data.get('message', 'Unknown error.')}", "error")
        except requests.exceptions.ConnectionError:
            self.show_status_signal.emit(f"Could not connect to internal Flask server on port {FLASK_PORT}", "error")
        except Exception as e:
            self.show_status_signal.emit(f"An unexpected error occurred: {e}", "error")
        finally:
            self.set_buttons_disabled_signal.emit(False)
        QApplication.processEvents()



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


# --- Main Application Logic ---
def start_flask_server():
    """Function to start the Flask server in a separate thread."""
    try:
        import logging
        log = logging.getLogger('werkzeug')
        log.setLevel(logging.ERROR)

        flask_app.run(port=FLASK_PORT, debug=False, use_reloader=False)
    except Exception as e:
        print(f"Flask server failed to start: {e}")

if __name__ == "__main__":
    # Removed URI scheme handling from startup
    # if len(sys.argv) > 1 and sys.argv[1].startswith("universalmediatool://"):
    #     print(f"Application launched via URI scheme: {sys.argv[1]}")

    flask_thread = threading.Thread(target=start_flask_server, daemon=True)
    flask_thread.start()
    
    time.sleep(1) 

    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())