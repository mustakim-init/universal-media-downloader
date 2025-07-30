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
import logging
from datetime import datetime

# Import PySide6 modules
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QStackedWidget, QScrollArea, QFrame,
    QFileDialog, QComboBox, QLineEdit, QRadioButton, QButtonGroup,
    QProgressBar, QSizePolicy, QSpacerItem, QDialog, QGraphicsDropShadowEffect,
    QCheckBox, QStatusBar, QPlainTextEdit, QSplitter,
    QTableView, QHeaderView, QAbstractItemView, QStyledItemDelegate, QStyleOptionHeader,
    QProxyStyle, QStyle
)
from PySide6.QtCore import (
    Qt, QTimer, QPropertyAnimation, QUrl, Signal, QEasingCurve, Property, QModelIndex, QPoint, QSize,
    QAbstractTableModel, QRect, QPointF, QRectF, QSortFilterProxyModel, QRegularExpression
)
from PySide6.QtGui import QColor, QFont, QDesktopServices, QIcon, QPixmap, QPalette, QPaintEvent, QPainter, QFontMetrics, QPen, QPolygonF, QCursor
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

# --- Global State Variables ---
browser_monitor_enabled = True
global_download_save_directory = os.path.join(os.path.expanduser("~"), 'Downloads')
if not os.path.exists(global_download_save_directory):
    os.makedirs(global_download_save_directory, exist_ok=True)

global_overwrite_existing_file = False
global_double_click_action = "Open folder"
gui_message_queue = queue.Queue()



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

def run_command_in_bundle(command_parts, *args, **kwargs):
    """Helper to run yt-dlp or ffmpeg with correct paths and startup info."""
    if command_parts[0] == 'yt-dlp': command_parts[0] = YT_DLP_BIN
    elif command_parts[0] == 'ffmpeg': command_parts[0] = FFMPEG_BIN
    if not os.path.exists(command_parts[0]):
        raise FileNotFoundError(f"Required binary not found: {command_parts[0]}.")
    if sys.platform == 'win32': kwargs['startupinfo'] = startupinfo
    if 'creationflags' in kwargs: del kwargs['creationflags']
    return subprocess.run(command_parts, *args, **kwargs)

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
            formats_to_return = formats

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
    # format_id is now optional, depends on download_type
    format_id = data.get('format_id') 
    media_type = data.get('media_type', 'video')
    download_type = data.get('download_type', 'specific_format') # New parameter

    output_path = global_download_save_directory # Access global variable directly

    if not url:
        return jsonify({'status': 'error', 'message': 'No URL provided'}), 400
    
    # Validate format_id only if specific_format download
    if download_type == 'specific_format' and not format_id:
        return jsonify({'status': 'error', 'message': 'Format ID is required for specific format download'}), 400

    try:
        full_output_path = os.path.abspath(output_path)
        if not os.path.exists(full_output_path):
            os.makedirs(full_output_path)
            
        # Determine yt-dlp command based on download_type
        yt_dlp_command_template = []
        is_merged_download = False # Flag for merging logic
        
        if download_type == 'highest_quality':
            if media_type == 'video':
                # This will download best video (mp4) and best audio (m4a) separately for merging
                yt_dlp_command_template = [
                    YT_DLP_BIN,
                    '-f', 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best', # Prioritize mp4 for video, m4a for audio, then best
                    '--no-playlist', # Ensure only single video is processed
                    url,
                    '--ffmpeg-location', FFMPEG_BIN,
                    '--verbose',
                    '-o', os.path.join(full_output_path, '%(title)s.%(ext)s') # yt-dlp will handle naming
                ]
                is_merged_download = True # Indicate that merging will happen

            elif media_type == 'audio':
                yt_dlp_command_template = [
                    YT_DLP_BIN,
                    '-f', 'bestaudio[ext=m4a]/bestaudio', # Prioritize m4a, then best audio
                    '--no-playlist', # Ensure only single video is processed
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
                '--no-playlist', # Ensure only single video is processed
                url,
                '--ffmpeg-location', FFMPEG_BIN,
                '--verbose',
                '-o', os.path.join(full_output_path, '%(title)s.%(ext)s')
            ]
            # The 'best_video_best_audio' format_id is a special case handled by the backend logic
            if format_id == "best_video_best_audio": 
                is_merged_download = True
        else:
            return jsonify({'status': 'error', 'message': 'Invalid download type.'}), 400

        is_playlist = 'playlist?list=' in url or '/playlist/' in url or '/watch?v=' in url and '&list=' in url

        gui_message_queue.put({
            'type': 'add_download',
            'url': url,
            'format_id': format_id if download_type == 'specific_format' else download_type, # Use download_type as format_id for highest_quality
            'output_path': full_output_path,
            'status': 'Initializing',
            'progress': '0%',
            'filename': os.path.basename(url), # Initial filename, will be updated
            'is_playlist': is_playlist,
            'media_type': media_type,
            'is_merged_download': is_merged_download, # Pass this flag
            'timestamp': time.time(), # Add timestamp for sorting
            'filesize_bytes': 0 # Initial filesize
        })

        threading.Thread(target=_perform_yt_dlp_download, args=(yt_dlp_command_template, url, is_playlist, media_type, is_merged_download, full_output_path)).start()

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

# Modified _perform_yt_dlp_download signature to accept is_merged_download flag
def _perform_yt_dlp_download(command_template, url, is_playlist, media_type, is_merged_download, download_dir):
    """
    Performs the yt-dlp download operation in a separate thread.
    Handles both single-file downloads and separate video/audio downloads followed by FFmpeg merge.
    Incorporates overwrite/suffix logic based on global_overwrite_existing_file.
    """
    # Initial filename, will be updated as download progresses
    filename_base = "Playlist Download" if is_playlist else os.path.basename(url) 
    temp_video_file = None
    temp_audio_file = None
    final_downloaded_file_path = None # This will hold the path to the final downloaded/merged file
    
    # Initialize these to None, will be determined based on actual downloaded content
    detected_filetype = 'unknown' 
    has_video_track = False
    has_audio_track = False
    filename = filename_base # Initialize filename for GUI display

    # Create a temporary file to store the final output path from yt-dlp
    # This is more reliable than parsing stdout for the 'Destination' line
    temp_output_path_file = os.path.join(download_dir, f"yt-dlp_output_{os.urandom(8).hex()}.txt")

    try:
        # --- Determine the final output path and filename with overwrite/suffix logic ---
        # yt-dlp uses %(title)s.%(ext)s for naming, so we need to let it determine the base title/ext first
        # Then, if overwrite is off, we'll check for conflicts and adjust.
        
        # First, get the info to predict the filename without actually downloading
        info_command = [
            YT_DLP_BIN,
            '--get-filename',
            '-o', '%(title)s.%(ext)s',
            url
        ]
        # Add --no-playlist for single videos to the info command to prevent it from trying to fetch playlist info
        if not is_playlist:
            info_command.insert(1, '--no-playlist') # Insert right after yt-dlp.exe

        # Capture output to get the predicted filename
        info_result = run_command_in_bundle(
            info_command,
            capture_output=True,
            text=True,
            check=True,
            timeout=150 # Short timeout for info fetch
        )
        predicted_filename = info_result.stdout.strip()
        
        # If it's a playlist, yt-dlp's --get-filename might return multiple lines or a generic name.
        # For simplicity, if it's a playlist, we'll let yt-dlp handle the sub-file naming
        # and only apply suffix logic to the *overall* playlist folder if yt-dlp creates one.
        # For single files, we apply the suffix logic here.
        
        if not is_playlist:
            base_name, ext = os.path.splitext(predicted_filename)
            ext = ext.lstrip('.') # Remove leading dot from extension
            
            current_filename = predicted_filename
            counter = 0
            
            # Loop to find a unique filename if overwrite is disabled
            while not global_overwrite_existing_file and os.path.exists(os.path.join(download_dir, current_filename)):
                counter += 1
                current_filename = f"{base_name} ({counter}).{ext}"
            
            final_output_template = os.path.join(download_dir, current_filename)
            filename = current_filename # Update filename for GUI display
        else:
            # For playlists, yt-dlp creates a subfolder. We don't modify individual filenames here.
            # The --output template will be used as-is, and yt-dlp handles sub-file naming.
            # If the playlist folder itself needs suffixing, yt-dlp usually handles it with --output %(playlist_title)s/%(title)s.%(ext)s
            final_output_template = os.path.join(download_dir, '%(playlist_title)s/%(title)s.%(ext)s') # Use playlist template for playlists
            # We also need to add --yes-playlist for actual playlist downloads
            if '--yes-playlist' not in command_template:
                # Find the position of the URL to insert --yes-playlist before it
                try:
                    url_index = command_template.index(url)
                    command_template.insert(url_index, '--yes-playlist')
                except ValueError:
                    # Fallback if URL not found (shouldn't happen if command_template is well-formed)
                    command_template.insert(1, '--yes-playlist') 


        # Update the command_template with the determined output path
        # Find and replace the output argument in the command_template
        output_arg_index = -1
        for i, arg in enumerate(command_template):
            if arg == '-o' and i + 1 < len(command_template):
                output_arg_index = i + 1
                break
        
        if output_arg_index != -1:
            command_template[output_arg_index] = final_output_template
        else:
            # If '-o' is not found, add it (should always be there for download)
            command_template.extend(['-o', final_output_template])

        # --- End of filename determination logic ---

        # Update initial download info in GUI with the determined filename
        gui_message_queue.put({
            'type': 'update_download_status', 
            'url': url, 
            'filename': filename, # Update the filename in the active downloads list
            'status': 'Initializing' # Reset status to initializing
        })


        if is_merged_download: 
            # --- Step 1: Download Best Video Only ---
            gui_message_queue.put({'type': 'update_download_status', 'url': url, 'status': 'Downloading Video', 'progress': '0%'})
            video_command = [
                YT_DLP_BIN,
                url,
                "-f", "bestvideo[ext=mp4]", # Ensure we get best MP4 video
                "--ffmpeg-location", FFMPEG_BIN,
                "--output", os.path.join(download_dir, '%(title)s.f%(format_id)s.%(ext)s'), # Temp filename
                "--verbose",
                *(['--no-playlist'] if not is_playlist else []),
                '--print-to-file', 'filepath', temp_output_path_file # Print final path to temp file
            ]
            
            video_process = subprocess.Popen(
                video_command,
                stdout=subprocess.PIPE, # Capture stdout for debugging and filename parsing
                stderr=subprocess.PIPE, # Capture stderr for parsing
                text=True,
                bufsize=1, universal_newlines=True,
                creationflags=SUBPROCESS_CREATION_FLAGS, startupinfo=startupinfo
            )
            
            video_stdout_lines = []
            video_stderr_lines = []
            
            while True:
                stdout_line = video_process.stdout.readline()
                stderr_line = video_process.stderr.readline()

                if not stdout_line and not stderr_line and video_process.poll() is not None:
                    break

                if stdout_line:
                    video_stdout_lines.append(stdout_line)
                    # No need to parse Destination here, relying on --print-to-file
                    if '[download]' in stdout_line and '%' in stdout_line:
                        match = re.search(r'(\d+\.\d+)%', stdout_line)
                        if match:
                            progress = match.group(1) + '%'
                            gui_message_queue.put({'type': 'update_download_status', 'url': url, 'status': 'Downloading Video', 'progress': progress})
                    elif '[ExtractAudio]' in stdout_line or '[ffmpeg]' in stdout_line:
                        gui_message_queue.put({'type': 'update_download_status', 'url': url, 'status': 'Processing Video'})

                if stderr_line:
                    video_stderr_lines.append(stderr_line)

            video_return_code = video_process.wait()
            
            stdout_output_video = "".join(video_stdout_lines)
            stderr_output_video = "".join(video_stderr_lines)

            if video_return_code != 0:
                error_detail = f"STDOUT: {stdout_output_video.strip()}\nSTDERR: {stderr_output_video.strip()}"
                raise Exception(f"Video download failed (exit code {video_return_code}):\n{error_detail}")
            
            # Read the actual downloaded video file path from the temp file
            if os.path.exists(temp_output_path_file):
                with open(temp_output_path_file, 'r') as f:
                    temp_video_file = f.read().strip()
                # Clear the temp file after reading for the next part
                with open(temp_output_path_file, 'w') as f:
                    f.write('')
            
            if not temp_video_file or not os.path.exists(temp_video_file):
                error_detail = f"Expected video file not found. Last yt-dlp output:\nSTDOUT: {stdout_output_video.strip()}\nSTDERR: {stderr_output_video.strip()}"
                raise Exception(f"Video download failed or file not found:\n{error_detail}")
            
            has_video_track = True


            # --- Step 2: Download Best Audio Only ---
            gui_message_queue.put({'type': 'update_download_status', 'url': url, 'status': 'Downloading Audio', 'progress': '0%'})
            audio_command = [
                YT_DLP_BIN,
                url,
                "-f", "bestaudio[ext=m4a]/bestaudio", # Ensure we get best M4A audio
                "--ffmpeg-location", FFMPEG_BIN,
                "--output", os.path.join(download_dir, '%(title)s.f%(format_id)s.%(ext)s'), # Temp filename
                "--verbose",
                *(['--no-playlist'] if not is_playlist else []),
                '--print-to-file', 'filepath', temp_output_path_file # Print final path to temp file
            ]
            audio_process = subprocess.Popen(
                audio_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1, universal_newlines=True,
                creationflags=SUBPROCESS_CREATION_FLAGS, startupinfo=startupinfo
            )
            
            audio_stdout_lines = []
            audio_stderr_lines = []
            while True:
                stdout_line = audio_process.stdout.readline()
                stderr_line = audio_process.stderr.readline()

                if not stdout_line and not stderr_line and audio_process.poll() is not None:
                    break

                if stdout_line:
                    audio_stdout_lines.append(stdout_line)
                    # No need to parse Destination here, relying on --print-to-file
                    if '[download]' in stdout_line and '%' in stdout_line:
                        match = re.search(r'(\d+\.\d+)%', stdout_line)
                        if match:
                            progress = match.group(1) + '%'
                            gui_message_queue.put({'type': 'update_download_status', 'url': url, 'status': 'Downloading Audio', 'progress': progress})
                    elif '[ExtractAudio]' in stdout_line or '[ffmpeg]' in stdout_line:
                        gui_message_queue.put({'type': 'update_download_status', 'url': url, 'status': 'Processing Audio'})
            
                if stderr_line:
                    audio_stderr_lines.append(stderr_line)

            audio_return_code = audio_process.wait()

            stdout_output_audio = "".join(audio_stdout_lines)
            stderr_output_audio = "".join(audio_stderr_lines)

            if audio_return_code != 0:
                error_detail = f"STDOUT: {stdout_output_audio.strip()}\nSTDERR: {stderr_output_audio.strip()}"
                raise Exception(f"Audio download failed (exit code {audio_return_code}):\n{error_detail}")
            
            # Read the actual downloaded audio file path from the temp file
            if os.path.exists(temp_output_path_file):
                with open(temp_output_path_file, 'r') as f:
                    temp_audio_file = f.read().strip()
                # Clear the temp file after reading for the next part
                with open(temp_output_path_file, 'w') as f:
                    f.write('')

            if not temp_audio_file or not os.path.exists(temp_audio_file):
                error_detail = f"Expected audio file not found. Last yt-dlp output:\nSTDOUT: {stdout_output_audio.strip()}\nSTDERR: {stderr_output_audio.strip()}"
                raise Exception(f"Audio download failed or file not found:\n{error_detail}")
            
            has_audio_track = True

            # Determine final output filename for merged file based on the predicted filename
            # This is where we use the already determined final_output_template
            final_output_filename = os.path.basename(final_output_template)
            final_downloaded_file_path = os.path.join(download_dir, final_output_filename)


            # --- Step 3: Merge Video and Audio using FFmpeg ---
            gui_message_queue.put({'type': 'update_download_status', 'url': url, 'status': 'Merging', 'progress': '0%', 'filename': final_output_filename})
            
            ffmpeg_merge_command = [
                FFMPEG_BIN,
                "-i", temp_video_file,
                "-i", temp_audio_file,
                "-c:v", "copy",
                "-c:a", "aac",
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-y", # Overwrite output files without asking (FFmpeg will overwrite, not yt-dlp here)
                final_downloaded_file_path
            ]
            
            ffmpeg_process = subprocess.Popen(
                ffmpeg_merge_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1, universal_newlines=True,
                creationflags=SUBPROCESS_CREATION_FLAGS, startupinfo=startupinfo
            )
            
            ffmpeg_stdout_lines = []
            ffmpeg_stderr_lines = []
            while True:
                stdout_line = ffmpeg_process.stdout.readline()
                stderr_line = ffmpeg_process.stderr.readline()

                if not stdout_line and not stderr_line and ffmpeg_process.poll() is not None:
                    break

                if stdout_line:
                    ffmpeg_stdout_lines.append(stdout_line)
                
                if stderr_line:
                    ffmpeg_stderr_lines.append(stderr_line)
                    if 'time=' in stderr_line and 'speed=' in stderr_line:
                        match = re.search(r'time=(\d{2}:\d{2}:\d{2}\.\d{2})', stderr_line)
                        if match:
                            progress_time = match.group(1)
                            gui_message_queue.put({'type': 'update_download_status', 'url': url, 'status': f'Merging ({progress_time})', 'progress': '0%', 'filename': final_output_filename})
            
            ffmpeg_return_code = ffmpeg_process.wait()

            stdout_output_ffmpeg = "".join(ffmpeg_stdout_lines)
            stderr_output_ffmpeg = "".join(ffmpeg_stderr_lines)

            if ffmpeg_return_code != 0:
                error_detail = f"STDOUT: {stdout_output_ffmpeg.strip()}\nSTDERR: {stderr_output_ffmpeg.strip()}"
                raise Exception(f"FFmpeg merge failed (exit code {ffmpeg_return_code}):\n{error_detail}")
            
            # The final_downloaded_file_path is already set above for merged files

        else: # Non-merged download (specific format or highest quality audio)
            # Ensure correct playlist flags for the main download command
            final_command = list(command_template) # Create a mutable copy
            
            # Remove existing playlist flags to avoid duplicates
            if '--no-playlist' in final_command:
                final_command.remove('--no-playlist')
            if '--yes-playlist' in final_command:
                final_command.remove('--yes-playlist')

            # Add the correct playlist flag based on is_playlist
            if not is_playlist:
                # Find the position of the URL to insert --no-playlist before it
                try:
                    url_index = final_command.index(url)
                    final_command.insert(url_index, '--no-playlist')
                except ValueError:
                    # Fallback if URL not found (shouldn't happen if command_template is well-formed)
                    final_command.insert(1, '--no-playlist') 
            elif is_playlist:
                # Find the position of the URL to insert --yes-playlist before it
                try:
                    url_index = final_command.index(url)
                    final_command.insert(url_index, '--yes-playlist')
                except ValueError:
                    # Fallback if URL not found
                    final_command.insert(1, '--yes-playlist')
            
            # Add --print-to-file to get the final output path reliably
            final_command.extend(['--print-to-file', 'filepath', temp_output_path_file])

            process = subprocess.Popen(
                final_command, # Use the modified command list
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True,
                creationflags=SUBPROCESS_CREATION_FLAGS,
                startupinfo=startupinfo
            )

            stdout_lines = []
            stderr_lines = []
            # We will now rely on temp_output_path_file for the final path

            while True:
                stdout_line = process.stdout.readline()
                stderr_line = process.stderr.readline()

                if not stdout_line and not stderr_line and process.poll() is not None:
                    break

                if stdout_line:
                    stdout_lines.append(stdout_line)
                    # Look for progress updates
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
                    # No need to parse Destination from stderr, relying on --print-to-file

            return_code = process.wait()
            stdout_output = "".join(stdout_lines)
            stderr_output = "".join(stderr_lines)

            if return_code != 0:
                error_detail = f"STDOUT: {stdout_output.strip()}\nSTDERR: {stderr_output.strip()}"
                raise Exception(f"yt-dlp download failed (exit code {return_code}):\n{error_detail}")
            
            # --- FIX: Read the actual downloaded file path from the temp file after process.wait() ---
            if os.path.exists(temp_output_path_file):
                try:
                    with open(temp_output_path_file, 'r', encoding='utf-8') as f: # Specify encoding
                        path_read = f.read().strip()
                        if path_read: # Ensure path is not empty
                            final_downloaded_file_path = path_read
                        else:
                            print(f"WARNING: temp_output_path_file was empty for {url}.")
                except Exception as e:
                    print(f"ERROR: Could not read temp_output_path_file {temp_output_path_file}: {e}")
            # --- END FIX ---

            # If the temp file was not created or is empty, it means yt-dlp did not report a path.
            if not final_downloaded_file_path:
                raise Exception(f"yt-dlp completed, but did not report a final file path via --print-to-file. STDOUT: {stdout_output.strip()}\nSTDERR: {stderr_output.strip()}")


        # --- Post-download/merge checks and final update ---
        if not final_downloaded_file_path or not os.path.exists(final_downloaded_file_path):
            error_message = f"Final downloaded file not found at expected path: {final_downloaded_file_path}. This might indicate a problem with yt-dlp output or file system operations. STDOUT: {stdout_output.strip()}\nSTDERR: {stderr_output.strip()}"
            raise Exception(error_message)

        actual_filesize_bytes = os.path.getsize(final_downloaded_file_path)
        actual_filename = os.path.basename(final_downloaded_file_path)

        if is_playlist:
            detected_filetype = 'playlist'
        elif has_video_track:
            detected_filetype = 'video'
        elif has_audio_track:
            detected_filetype = 'audio'
        else: # Fallback based on media_type requested if tracks aren't detected
            detected_filetype = media_type if media_type in ['video', 'audio'] else 'unknown'


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
        gui_message_queue.put({'type': 'update_download_status', 'url': url, 'status': 'Failed', 'message': error_message, 'filename': filename})
        print(f"Error during download for {url}: {error_message}")
        import traceback
        traceback.print_exc()
    finally:
        # Cleanup temporary files
        if temp_video_file and os.path.exists(temp_video_file) and temp_video_file != final_downloaded_file_path:
            try:
                os.remove(temp_video_file)
                print(f"Cleaned up temporary video file: {temp_video_file}")
            except Exception as e:
                print(f"Error cleaning up temp video file {temp_video_file}: {e}")
        if temp_audio_file and os.path.exists(temp_audio_file) and temp_audio_file != final_downloaded_file_path:
            try:
                os.remove(temp_audio_file)
                print(f"Cleaned up temporary audio file: {temp_audio_file}")
            except Exception as e:
                print(f"Error cleaning up temp audio file {temp_audio_file}: {e}")
        
        # Always try to clean up the temp_output_path_file
        if os.path.exists(temp_output_path_file):
            try:
                os.remove(temp_output_path_file)
                print(f"Cleaned up temporary output path file: {temp_output_path_file}")
            except Exception as e:
                print(f"Error cleaning up temp output path file {temp_output_path_file}: {e}")
    pass

@flask_app.route('/set_browser_monitor_status', methods=['POST'])
def set_browser_monitor_status():
    global browser_monitor_enabled
    data = request.json
    status = data.get('enabled')
    if isinstance(status, bool):
        browser_monitor_enabled = status
        return jsonify({'status': 'success', 'message': f'Browser monitoring set to {status}'})
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
    def __init__(self, main_window_instance, parent=None):
        super().__init__(parent)
        self.main_window_instance = main_window_instance
        self.setPlaceholderText("Search...")
        self.setFixedHeight(30)

        self.search_icon_button = QPushButton(self)
        self.set_search_icon()
        self.search_icon_button.setFixedSize(24, 24)
        self.search_icon_button.setCursor(Qt.PointingHandCursor)
        self.search_icon_button.setStyleSheet("""
            QPushButton { background-color: transparent; border: none; padding: 0px; }
            QPushButton:hover { background-color: #495057; border-radius: 12px; }
            QPushButton:pressed { background-color: #3a3a3a; }
        """)

        self.setTextMargins(10, 1, self.search_icon_button.width() + 5, 1)
        self.setStyleSheet("""
            QLineEdit {
                background-color: #3a3a3a; border: 1px solid #495057;
                border-radius: 7px; padding: 5px 15px; color: #e0e0e0;
            }
            QLineEdit:focus { border: 1px solid #4dabf7; }
        """)
        
        # This signal is now connected to the new proxy-based filtering method
        self.textChanged.connect(self.main_window_instance.filter_current_view)
        self.search_icon_button.clicked.connect(lambda: self.main_window_instance.filter_current_view(self.text()))

    def set_search_icon(self, color="#adb5bd"):
        search_icon_svg = ICONS["search"]
        colored_svg = search_icon_svg.replace('currentColor', color)
        pixmap = QPixmap()
        pixmap.loadFromData(colored_svg.encode('utf-8'))
        scaled_pixmap = pixmap.scaled(16, 16, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.search_icon_button.setIcon(QIcon(scaled_pixmap))
        self.search_icon_button.setIconSize(scaled_pixmap.size())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        icon_x = self.width() - self.search_icon_button.width() - 5
        icon_y = (self.height() - self.search_icon_button.height()) // 2
        self.search_icon_button.move(icon_x, icon_y)


# --- Custom Checkbox (from QCustomCheckBox.py) ---
class QCustomCheckBox(QCheckBox):
    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)
        self.bgColor = QColor("#3a3a3a")
        self._circleColor = QColor("#e0e0e0")
        self._activeColor = QColor("#4dabf7")
        self.animationEasingCurve = QEasingCurve.Type.InOutQuad
        self.animationDuration = 200
        self.pos = 0.0
        self.animation = QPropertyAnimation(self, b"position")
        self.animation.setEasingCurve(self.animationEasingCurve)
        self.animation.setDuration(self.animationDuration)
        self.stateChanged.connect(self.setup_animation)
        self.label = QLabel(self)
        self.label.setWordWrap(True)
        self.label.setStyleSheet("color: #e0e0e0;")
        self.setText(text)
        self.icon = QIcon()
        self._iconSize = QSize(0, 0)
        self.setFixedHeight(25)

    @Property(float)
    def position(self):
        return self.pos

    @position.setter
    def position(self, pos):
        self.pos = pos
        self.update()

    def setup_animation(self, value):
        margin = 3
        track_width = self.height() * 2.0
        thumb_size = self.height() - 6
        start_pos = margin
        end_pos = track_width - thumb_size - margin
        self.animation.stop()
        self.animation.setStartValue(float(self.pos))
        self.animation.setEndValue(float(end_pos if value else start_pos))
        self.animation.start()

    def paintEvent(self, e: QPaintEvent):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 0))
        painter.drawRect(self.rect())
        track_width = self.height() * 2.0
        track_height = self.height()
        track_radius = track_height / 2
        thumb_size = self.height() - 6
        thumb_y = (track_height - thumb_size) / 2
        if self.isChecked():
            painter.setBrush(self._activeColor)
            painter.drawRoundedRect(0, 0, track_width, track_height, track_radius, track_radius)
            painter.setBrush(self._circleColor)
            painter.drawEllipse(self.pos, thumb_y, thumb_size, thumb_size)
        else:
            painter.setBrush(self.bgColor)
            painter.drawRoundedRect(0, 0, track_width, track_height, track_radius, track_radius)
            painter.setBrush(self._circleColor)
            painter.drawEllipse(self.pos, thumb_y, thumb_size, thumb_size)
        painter.end()

    def hitButton(self, pos: QPoint):
        return self.contentsRect().contains(pos)
    
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.adjustWidgetSize()

    def adjustWidgetSize(self):
        track_width = self.height() * 2.0
        label_margin_left = 10
        label_x = int(track_width + label_margin_left)
        label_width = max(0, self.width() - label_x)
        self.label.setGeometry(label_x, 0, label_width, self.height())
        self.update()

    def setText(self, text):
        self.label.setText(text)
        self.adjustWidgetSize()

    def text(self):
        return self.label.text()


# ARCHITECTURAL FIX: Use QProxyStyle for a customizable sort indicator
class CustomSortIndicatorStyle(QProxyStyle):
    def __init__(self, style=None):
        super().__init__(style)
        self.arrow_color = QColor("#4dabf7")

    def drawControl(self, element, option, painter, widget=None):
        if element == QStyle.ControlElement.CE_HeaderSection:
            # Create a mutable copy of the option to avoid modifying the original
            header_option = QStyleOptionHeader()
            header_option.initFrom(option)
            
            # Store original indicator and then disable it for the base drawing
            original_indicator = header_option.sortIndicator
            header_option.sortIndicator = 0
            
            # Let the base style draw the header (text, background, etc.)
            super().drawControl(element, header_option, painter, widget)
            
            # If there's a sort indicator, draw our custom one
            if original_indicator != 0:
                painter.save()
                pen = painter.pen()
                pen.setWidth(2)
                pen.setColor(self.arrow_color)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                
                arrow_size = 8.0
                
                # Define up and down arrows
                if original_indicator == QStyleOptionHeader.SortIndicator.SortUp:
                    arrow = QPolygonF([QPointF(0, arrow_size), QPointF(arrow_size / 2, 0), QPointF(arrow_size, arrow_size)])
                else: # SortDown
                    arrow = QPolygonF([QPointF(0, 0), QPointF(arrow_size / 2, arrow_size), QPointF(arrow_size, 0)])

                # Center the arrow in the header section rect
                header_rect = header_option.rect
                arrow.translate(
                    header_rect.center().x() - (arrow_size / 2),
                    header_rect.center().y() - (arrow_size / 2)
                )
                painter.drawPolygon(arrow)
                painter.restore()
            return # We've handled the drawing
        
        # For all other controls, use the default implementation
        super().drawControl(element, option, painter, widget)


class DownloadTableModel(QAbstractTableModel):
    def __init__(self, data, is_completed_model=False, parent=None):
        super().__init__(parent)
        # ARCHITECTURAL FIX: This model's data is a *reference* to the main window's master list
        self._data = data
        self.is_completed_model = is_completed_model
        if self.is_completed_model:
            self.header_labels = ["Name", "Date", "Size", "Type", "Location"]
        else:
            self.header_labels = ["Name", "Date", "Size", "Progress", "Status"]

    def rowCount(self, parent=QModelIndex()):
        return len(self._data)

    def columnCount(self, parent=QModelIndex()):
        return len(self.header_labels)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or role != Qt.DisplayRole:
            return None
        try:
            item = self._data[index.row()]
            column = index.column()
            column_name = self.header_labels[column]

            if column_name == "Name": return item.get('filename', item.get('url', 'N/A'))
            elif column_name == "Date":
                ts = item.get('timestamp')
                return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M') if ts else "N/A"
            elif column_name == "Size": return format_bytes(item.get('filesize_bytes', 0))
            elif column_name == "Progress": return item.get('progress', '0%')
            elif column_name == "Type": return item.get('filetype', 'Unknown').capitalize()
            elif column_name == "Status": return item.get('status', 'N/A')
            elif column_name == "Location":
                path = item.get('path', 'N/A')
                return os.path.dirname(path) if path != 'N/A' else 'N/A'
        except IndexError:
            return None
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.header_labels[section]
        return None


# Main Application Window
class MainWindow(QMainWindow):
    add_download_signal = Signal(dict)
    update_download_status_signal = Signal(str, dict)
    add_completed_signal = Signal(dict)
    show_status_signal = Signal(str, str, int)
    set_buttons_disabled_signal = Signal(bool)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Universal Media Tool")
        self.setGeometry(100, 100, 1200, 800)
        self.setMinimumSize(900, 600)
        
        self.current_panel_type = "active_downloads"
        
        self.init_data_models()
        self.init_ui()
        self.apply_stylesheet()
        self.connect_signals()

        self.status_clear_timer = QTimer(self); self.status_clear_timer.setSingleShot(True)
        self.status_clear_timer.timeout.connect(self._clear_status_bar)
        
        self.queue_timer = QTimer(self); self.queue_timer.timeout.connect(self.check_flask_message_queue)
        self.queue_timer.start(100)
        
        self.show_panel(self.active_downloads_panel)
        self.active_downloads_button.setChecked(True)

    def init_data_models(self):
        """ ARCHITECTURAL FIX: Setup all master data lists and models (source and proxy) in one place. """
        self.active_downloads_data = []
        self.completed_videos_data = []
        self.completed_audios_data = []
        self.completed_playlists_data = []

        self.active_downloads_model = DownloadTableModel(self.active_downloads_data, is_completed_model=False)
        self.completed_videos_model = DownloadTableModel(self.completed_videos_data, is_completed_model=True)
        self.completed_audios_model = DownloadTableModel(self.completed_audios_data, is_completed_model=True)
        self.completed_playlists_model = DownloadTableModel(self.completed_playlists_data, is_completed_model=True)

        self.proxy_active = QSortFilterProxyModel(); self.proxy_active.setSourceModel(self.active_downloads_model)
        self.proxy_active.setFilterCaseSensitivity(Qt.CaseInsensitive); self.proxy_active.setFilterKeyColumn(-1) # Filter all columns
        
        self.proxy_videos = QSortFilterProxyModel(); self.proxy_videos.setSourceModel(self.completed_videos_model)
        self.proxy_videos.setFilterCaseSensitivity(Qt.CaseInsensitive); self.proxy_videos.setFilterKeyColumn(-1)
        
        self.proxy_audios = QSortFilterProxyModel(); self.proxy_audios.setSourceModel(self.completed_audios_model)
        self.proxy_audios.setFilterCaseSensitivity(Qt.CaseInsensitive); self.proxy_audios.setFilterKeyColumn(-1)

        self.proxy_playlists = QSortFilterProxyModel(); self.proxy_playlists.setSourceModel(self.completed_playlists_model)
        self.proxy_playlists.setFilterCaseSensitivity(Qt.CaseInsensitive); self.proxy_playlists.setFilterKeyColumn(-1)

    def apply_stylesheet(self):
        self.setStyleSheet("""
            QMainWindow { background-color: #252526; color: #e0e0e0; }
            QFrame#sidebarFrame { background-color: #1e1e1e; border-right: 1px solid #3a3a3a; }
            QLabel { color: #e0e0e0; }
            QWidget#contentPage { background-color: #2d2d2d; }
            QScrollArea { background-color: transparent; border: none; }
            QScrollArea > QWidget > QWidget { background-color: transparent; }
            QScrollBar:vertical { border: none; background: #3a3a3a; width: 10px; margin: 0; border-radius: 5px; }
            QScrollBar::handle:vertical { background: #4dabf7; border-radius: 5px; min-height: 20px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { background: none; border: none; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
            QScrollBar:horizontal { border: none; background: #3a3a3a; height: 10px; margin: 0; border-radius: 5px; }
            QScrollBar::handle:horizontal { background: #6c757d; border-radius: 5px; min-width: 20px; }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { background: none; border: none; }
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: none; }
            QComboBox { background-color: #3a3a3a; border: 1px solid #495057; border-radius: 5px; padding: 5px; color: #e0e0e0; }
            QComboBox::drop-down { border: none; }
            QComboBox::down-arrow { image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23ffffff' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'%3E%3C/polyline%3E%3C/svg%3E"); width: 12px; height: 12px; margin-right: 5px; }
            QLineEdit { background-color: #3a3a3a; border: 1px solid #495057; border-radius: 5px; padding: 5px; color: #e0e0e0; }
            QLineEdit:focus { border: 1px solid #4dabf7; }
            QRadioButton::indicator { width: 16px; height: 16px; border-radius: 8px; border: 2px solid #495057; background-color: #2d2d2d; }
            QRadioButton::indicator:checked { background-color: #4dabf7; border: 2px solid #4dabf7; }
            QRadioButton { color: #e0e0e0; }
            QCheckBox::indicator { width: 12px; height: 12px; border-radius: 4px; border: 2px solid #495057; background-color: #2d2d2d; }
            QCheckBox::indicator:checked { background-color: #4dabf7; border: 2px solid #4dabf7; }
            QCheckBox { color: #e0e0e0; }
            QDialog { background-color: #2d2d2d; border-radius: 10px; border: 1px solid #495057; }
            QDialog QLabel { color: #e0e0e0; }
            QDialog QPushButton { background-color: #4dabf7; color: white; border: none; border-radius: 5px; padding: 8px 15px; }
            QDialog QPushButton:hover { background-color: #3b8fcc; }
            QDialog QPushButton#cancelButton { background-color: #6c757d; }
            QDialog QPushButton#cancelButton:hover { background-color: #5a6268; }
            QPushButton:focus { outline: none; }
            QPlainTextEdit { background-color: #3a3a3a; border: 1px solid #495057; border-radius: 5px; padding: 10px; color: #e0e0e0; font-family: 'Consolas', 'Courier New', monospace; font-size: 12px; }
            QSplitter::handle { background-color: #3a3a3a; width: 5px; }
            QSplitter::handle:hover { background-color: #4dabf7; }
            QTableView { background-color: #2d2d2d; border: none; gridline-color: #3a3a3a; selection-background-color: #495057; selection-color: #e0e0e0; color: #e0e0e0; outline: none; }
            QTableView::item { padding: 5px; background-color: #2d2d2d; outline: none; }
            QTableView::item:selected { background-color: #495057; color: #e0e0e0; }
            QHeaderView::section { background-color: #3a3a3a; color: #d1d1d1; border: none; border-bottom: 1px solid #495057; font-weight: bold; outline: none; padding: 5px; }
            QHeaderView::section:hover { background-color: #495057; }
            QPushButton.action-button { background-color: #495057; color: #ffffff; border: none; border-radius: 5px; padding: 5px 10px; font-size: 13px; }
            QPushButton.action-button:hover { background-color: #5a6268; }
            QPushButton.action-button:pressed { background-color: #3a3a3a; }
            QPushButton.action-button:focus { outline: none; }
        """)

    def init_ui(self):
        central_widget = QWidget(); self.setCentralWidget(central_widget)
        main_splitter = QSplitter(Qt.Horizontal); 
        central_layout = QHBoxLayout(central_widget); central_layout.setContentsMargins(0,0,0,0); central_layout.addWidget(main_splitter)

        self.sidebar_frame = QFrame(); self.sidebar_frame.setObjectName("sidebarFrame")
        self.sidebar_layout = QVBoxLayout(self.sidebar_frame); self.sidebar_layout.setContentsMargins(10, 20, 10, 10); self.sidebar_layout.setSpacing(5)
        logo_label = QLabel("Media Tool"); logo_label.setFont(QFont("Arial", 20, QFont.Bold)); logo_label.setStyleSheet("color: #4dabf7;"); logo_label.setAlignment(Qt.AlignCenter)
        self.sidebar_layout.addWidget(logo_label); self.sidebar_layout.addSpacing(20)
        
        downloads_label = QLabel("DOWNLOADS"); downloads_label.setFont(QFont("Arial", 12, QFont.Bold)); downloads_label.setStyleSheet("color: #adb5bd;")
        self.sidebar_layout.addWidget(downloads_label); self.sidebar_layout.addSpacing(5)

        self.active_downloads_button = SidebarButton("Active Downloads", ICONS["download"])
        self.completed_videos_button = SidebarButton("Completed Videos", ICONS["film"])
        self.completed_audios_button = SidebarButton("Completed Audios", ICONS["headphones"])
        self.completed_playlists_button = SidebarButton("Completed Playlists", ICONS["list"])
        self.convert_media_button = SidebarButton("Convert Media", ICONS["shuffle"])
        self.settings_button = SidebarButton("Settings", ICONS["menu"])
        self.sidebar_button_group = QButtonGroup(self); self.sidebar_button_group.setExclusive(True)
        for btn in [self.active_downloads_button, self.completed_videos_button, self.completed_audios_button, self.completed_playlists_button]: self.sidebar_button_group.addButton(btn); self.sidebar_layout.addWidget(btn)
        self.sidebar_layout.addSpacing(20)
        tools_label = QLabel("TOOLS"); tools_label.setFont(QFont("Arial", 12, QFont.Bold)); tools_label.setStyleSheet("color: #adb5bd;")
        self.sidebar_layout.addWidget(tools_label); self.sidebar_layout.addSpacing(5)
        self.sidebar_button_group.addButton(self.convert_media_button); self.sidebar_layout.addWidget(self.convert_media_button)
        self.sidebar_layout.addSpacing(20)
        self.add_download_button_nav = QPushButton("Add New Download"); self.add_download_button_nav.setFixedSize(180, 40); self.add_download_button_nav.setCursor(Qt.PointingHandCursor)
        self.add_download_button_nav.setStyleSheet("QPushButton { background-color: #4dabf7; color: white; border: none; border-radius: 8px; font-size: 14px; font-weight: bold; } QPushButton:hover { background-color: #3b8fcc; }");
        self.sidebar_layout.addWidget(self.add_download_button_nav)
        self.sidebar_layout.addStretch(1)
        self.sidebar_button_group.addButton(self.settings_button); self.sidebar_layout.addWidget(self.settings_button)
        main_splitter.addWidget(self.sidebar_frame)

        self.content_container_widget = QWidget(); self.main_content_vlayout = QVBoxLayout(self.content_container_widget); self.main_content_vlayout.setContentsMargins(0, 0, 0, 0); self.main_content_vlayout.setSpacing(0)
        self.content_top_bar = QFrame(); self.content_top_bar.setFixedHeight(60); self.content_top_bar.setStyleSheet("background-color: #2d2d2d; border-bottom: 1px solid #3a3a3a;")
        self.top_bar_layout = QHBoxLayout(self.content_top_bar); self.top_bar_layout.setContentsMargins(20, 0, 20, 0); self.top_bar_layout.setSpacing(10)
        action_buttons_layout = QHBoxLayout(); action_buttons_layout.setContentsMargins(0,0,0,0); action_buttons_layout.setSpacing(10)
        self.delete_button = self._create_action_button("Delete", ICONS["trash"]); self.open_button = self._create_action_button("Open", ICONS["play"]); self.open_folder_button = self._create_action_button("Open Folder", ICONS["folder-open"]); self.cancel_button = self._create_action_button("Cancel", ICONS["x-circle"]); self.refresh_button = self._create_action_button("Refresh", ICONS["refresh"])
        for btn in [self.delete_button, self.open_button, self.open_folder_button, self.cancel_button, self.refresh_button]: action_buttons_layout.addWidget(btn)
        self.top_bar_layout.addLayout(action_buttons_layout); self.top_bar_layout.addStretch(1)
        self.search_input = SearchLineEdit(self); self.top_bar_layout.addWidget(self.search_input)
        self.main_content_vlayout.addWidget(self.content_top_bar)

        self.content_stacked_widget = QStackedWidget(); self.content_stacked_widget.setObjectName("contentPage")
        self.main_content_vlayout.addWidget(self.content_stacked_widget)
        main_splitter.addWidget(self.content_container_widget)
        main_splitter.setSizes([220, 980])

        self.active_downloads_panel, self.active_downloads_table_view = self.create_table_panel(self.proxy_active)
        self.completed_videos_panel, self.completed_videos_table_view = self.create_table_panel(self.proxy_videos)
        self.completed_audios_panel, self.completed_audios_table_view = self.create_table_panel(self.proxy_audios)
        self.completed_playlists_panel, self.completed_playlists_table_view = self.create_table_panel(self.proxy_playlists)
        self.conversion_panel = self.create_coming_soon_panel("Media Converter")
        self.settings_panel = self.create_settings_panel()
        self.extension_setup_panel = self.create_extension_setup_panel()
        self.download_settings_panel = self.create_download_settings_panel()
        for panel in [self.active_downloads_panel, self.completed_videos_panel, self.completed_audios_panel, self.completed_playlists_panel, self.conversion_panel, self.settings_panel, self.extension_setup_panel, self.download_settings_panel]: self.content_stacked_widget.addWidget(panel)

        self.status_bar = QStatusBar(); self.setStatusBar(self.status_bar)
        self.status_label = QLabel("Ready"); self.status_label.setStyleSheet("color: #adb5bd; font-size: 12px;"); self.status_bar.addWidget(self.status_label)
        self.version_label = QLabel("Ver: 1.0.0"); self.version_label.setStyleSheet("color: #7F8C8D; margin-left: 15px;"); self.status_bar.addPermanentWidget(self.version_label)
        self.status_bar.setStyleSheet("QStatusBar { background-color: #252526; border-top: 1px solid #3a3a3a; }")

    def create_table_panel(self, proxy_model):
        """ A generic panel creator using proxy models. """
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(20, 10, 20, 20)
        layout.setSpacing(0)
        
        table_view = QTableView()
        table_view.setModel(proxy_model)
        
        header = table_view.horizontalHeader()
        header.setSortIndicatorShown(True)
        header.setSectionsClickable(True)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        
        table_view.setSortingEnabled(True)
        table_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table_view.verticalHeader().setVisible(False)
        table_view.setAlternatingRowColors(True)
        table_view.setFocusPolicy(Qt.NoFocus)
        table_view.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        
        layout.addWidget(table_view)
        return panel, table_view

    def _create_action_button(self, text, icon_svg):
        btn = QPushButton(text); btn.setObjectName("action-button"); btn.setFixedSize(100, 30); btn.setCursor(Qt.PointingHandCursor)
        icon_pixmap = QPixmap(); icon_pixmap.loadFromData(icon_svg.replace('currentColor', '#ffffff').encode('utf-8'))
        icon = QIcon(icon_pixmap.scaled(16, 16, Qt.KeepAspectRatio, Qt.SmoothTransformation)); btn.setIcon(icon); btn.setIconSize(icon.actualSize(btn.size()))
        palette = btn.palette(); palette.setColor(QPalette.ColorRole.ButtonText, QColor("#ffffff")); btn.setPalette(palette); btn.setAutoFillBackground(True)
        btn.setStyleSheet("QPushButton.action-button { background-color: #495057; border: none; border-radius: 5px; padding: 5px 10px; font-size: 13px; } QPushButton.action-button:hover { background-color: #5a6268; } QPushButton.action-button:pressed { background-color: #3a3a3a; } QPushButton.action-button:focus { outline: none; }")
        btn.setVisible(False)
        return btn

    def connect_signals(self):
        self.active_downloads_button.clicked.connect(lambda: self.show_panel(self.active_downloads_panel))
        self.completed_videos_button.clicked.connect(lambda: self.show_panel(self.completed_videos_panel))
        self.completed_audios_button.clicked.connect(lambda: self.show_panel(self.completed_audios_panel))
        self.completed_playlists_button.clicked.connect(lambda: self.show_panel(self.completed_playlists_panel))
        self.convert_media_button.clicked.connect(lambda: self.show_panel(self.conversion_panel))
        self.settings_button.clicked.connect(lambda: self.show_panel(self.settings_panel))
        self.add_download_button_nav.clicked.connect(self.open_add_download_dialog)

        self.search_input.textChanged.connect(self.filter_current_view)
        
        self.add_download_signal.connect(self.add_download_to_list)
        self.update_download_status_signal.connect(self.update_download_status_in_list)
        self.add_completed_signal.connect(self.add_completed_download)
        self.show_status_signal.connect(self.show_status)
        self.set_buttons_disabled_signal.connect(self.set_all_buttons_disabled)

        self.delete_button.clicked.connect(self.delete_selected_items)
        self.open_button.clicked.connect(self.open_selected_file)
        self.open_folder_button.clicked.connect(self.open_selected_folder)
        self.cancel_button.clicked.connect(self.cancel_download)
        self.refresh_button.clicked.connect(self.refresh_current_view)

        for tv in [self.active_downloads_table_view, self.completed_videos_table_view, self.completed_audios_table_view, self.completed_playlists_table_view]:
            tv.doubleClicked.connect(self.handle_table_double_click)

    def handle_table_double_click(self, index):
        if not self._get_selected_item_data(): return
        if global_double_click_action == "Open folder": self.open_selected_folder()
        elif global_double_click_action == "Open file": self.open_selected_file()

    def show_status(self, message, msg_type='info', timeout_ms=5000):
        self.status_label.setText(message)
        if msg_type == 'success': self.status_label.setStyleSheet("color: #4dabf7; font-size: 12px;")
        elif msg_type == 'error': self.status_label.setStyleSheet("color: #ff6b6b; font-size: 12px;")
        else: self.status_label.setStyleSheet("color: #adb5bd; font-size: 12px;")
        self.status_clear_timer.start(timeout_ms)

    def _clear_status_bar(self):
        self.status_label.setText("Ready"); self.status_label.setStyleSheet("color: #adb5bd; font-size: 12px;")

    def set_all_buttons_disabled(self, disabled):
        for widget in self.findChildren(QPushButton) + self.findChildren(QComboBox) + self.findChildren(QCheckBox):
            widget.setEnabled(not disabled)

    def show_panel(self, panel_to_show):
        for button in self.sidebar_button_group.buttons(): button.setChecked(False)
        self.content_stacked_widget.setCurrentWidget(panel_to_show)
        
        is_list_view = panel_to_show in [self.active_downloads_panel, self.completed_videos_panel, self.completed_audios_panel, self.completed_playlists_panel]
        self.search_input.setVisible(is_list_view)
        self.delete_button.setVisible(is_list_view)
        self.open_button.setVisible(is_list_view)
        self.open_folder_button.setVisible(is_list_view)
        self.refresh_button.setVisible(is_list_view)
        self.cancel_button.setVisible(panel_to_show == self.active_downloads_panel)

        if panel_to_show == self.active_downloads_panel: self.current_panel_type = "active_downloads"; self.active_downloads_button.setChecked(True)
        elif panel_to_show == self.completed_videos_panel: self.current_panel_type = "completed_videos"; self.completed_videos_button.setChecked(True)
        elif panel_to_show == self.completed_audios_panel: self.current_panel_type = "completed_audios"; self.completed_audios_button.setChecked(True)
        elif panel_to_show == self.completed_playlists_panel: self.current_panel_type = "completed_playlists"; self.completed_playlists_button.setChecked(True)
        elif panel_to_show == self.conversion_panel: self.current_panel_type = "conversion"; self.convert_media_button.setChecked(True)
        elif panel_to_show == self.settings_panel: self.current_panel_type = "settings"; self.settings_button.setChecked(True); self.update_settings_display()
        elif panel_to_show == self.extension_setup_panel: self.current_panel_type = "extension_setup"; self.update_extension_setup_display()
        elif panel_to_show == self.download_settings_panel: self.current_panel_type = "download_settings"; self.update_download_settings_display()
        
        self.filter_current_view(self.search_input.text())

    def filter_current_view(self, text):
        """ ARCHITECTURAL FIX: Filters the view by setting a filter on the correct PROXY model. """
        regex = QRegularExpression(text, QRegularExpression.PatternOption.CaseInsensitiveOption)
        if self.current_panel_type == "active_downloads": self.proxy_active.setFilterRegularExpression(regex)
        elif self.current_panel_type == "completed_videos": self.proxy_videos.setFilterRegularExpression(regex)
        elif self.current_panel_type == "completed_audios": self.proxy_audios.setFilterRegularExpression(regex)
        elif self.current_panel_type == "completed_playlists": self.proxy_playlists.setFilterRegularExpression(regex)

    def _get_selected_item_data(self):
        """ Helper to get data for the currently selected row by mapping proxy index to source index. """
        current_table_view, proxy_model = None, None
        if self.current_panel_type == "active_downloads": current_table_view, proxy_model = self.active_downloads_table_view, self.proxy_active
        elif self.current_panel_type == "completed_videos": current_table_view, proxy_model = self.completed_videos_table_view, self.proxy_videos
        elif self.current_panel_type == "completed_audios": current_table_view, proxy_model = self.completed_audios_table_view, self.proxy_audios
        elif self.current_panel_type == "completed_playlists": current_table_view, proxy_model = self.completed_playlists_table_view, self.proxy_playlists
        
        if current_table_view:
            selected_proxy_indexes = current_table_view.selectionModel().selectedRows()
            if selected_proxy_indexes:
                proxy_index = selected_proxy_indexes[0]
                source_index = proxy_model.mapToSource(proxy_index)
                source_model = proxy_model.sourceModel()
                if source_index.isValid() and source_index.row() < len(source_model._data):
                    return source_model._data[source_index.row()]
        return None

    def delete_selected_items(self):
        selected_item = self._get_selected_item_data()
        if not selected_item: self.show_status("No item selected to delete.", "info"); return
        
        confirm_dialog = ConfirmationDialog(f"Are you sure you want to delete '{selected_item.get('filename', 'this item')}'?", self)
        if confirm_dialog.exec() == QDialog.Accepted:
            url_to_delete = selected_item.get('url')
            # ARCHITECTURAL FIX: Find in and remove from the correct master list, notifying the source model
            all_lists = [self.active_downloads_data, self.completed_videos_data, self.completed_audios_data, self.completed_playlists_data]
            all_models = [self.active_downloads_model, self.completed_videos_model, self.completed_audios_model, self.completed_playlists_model]

            for data_list, model in zip(all_lists, all_models):
                for i, item in enumerate(data_list):
                    if item.get('url') == url_to_delete:
                        model.beginRemoveRows(QModelIndex(), i, i)
                        data_list.pop(i)
                        model.endRemoveRows()
                        self.show_status(f"Item '{selected_item.get('filename')}' deleted.", "success")
                        return

    def open_selected_file(self):
        selected_item = self._get_selected_item_data()
        if not selected_item: self.show_status("No item selected to open.", "info"); return
        file_path = selected_item.get('path')
        if file_path and os.path.exists(file_path): QDesktopServices.openUrl(QUrl.fromLocalFile(file_path))
        else: self.show_status(f"File not found: {file_path}", "error")

    def open_selected_folder(self):
        selected_item = self._get_selected_item_data()
        if not selected_item: self.show_status("No item selected to open folder.", "info"); return
        file_path = selected_item.get('path')
        if file_path and os.path.exists(file_path): QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(file_path)))
        else: self.show_status(f"File path not available.", "error")

    def cancel_download(self):
        # This logic remains primarily conceptual as it requires process management
        selected_item = self._get_selected_item_data()
        if not selected_item or self.current_panel_type != "active_downloads": self.show_status("Please select an active download to cancel.", "info"); return
        # In a real app, signal backend to kill the process for selected_item['url']
        self.delete_selected_items() # For now, just remove it from the list
        self.show_status(f"Download '{selected_item.get('filename')}' cancelled.", "info")

    def refresh_current_view(self):
        self.filter_current_view(self.search_input.text())
        self.show_status("View refreshed.", "info")

    def create_coming_soon_panel(self, tool_name):
        panel = QWidget(); layout = QVBoxLayout(panel); layout.setAlignment(Qt.AlignCenter)
        coming_soon_label = QLabel("Coming Soon"); font = QFont(); font.setPointSize(24); font.setBold(True); coming_soon_label.setFont(font)
        tool_name_label = QLabel(f"The {tool_name} feature is currently under development.")
        layout.addWidget(coming_soon_label, alignment=Qt.AlignCenter); layout.addWidget(tool_name_label, alignment=Qt.AlignCenter)
        return panel

    def create_settings_panel(self):
        panel = QWidget(); panel.setLayout(QVBoxLayout()); panel.layout().setContentsMargins(20, 20, 20, 20); panel.layout().setSpacing(15)
        self.browser_monitor_switch = QCustomCheckBox("Enable Browser Monitoring", self); self.browser_monitor_switch.setChecked(browser_monitor_enabled)
        self.browser_monitor_switch.stateChanged.connect(self.toggle_browser_monitor); panel.layout().addWidget(self.browser_monitor_switch)
        monitor_help_label = QLabel("Allows the browser extension to send download requests to the app."); monitor_help_label.setStyleSheet("color: #adb5bd; font-size: 11px; margin-left: 25px;"); panel.layout().addWidget(monitor_help_label)
        panel.layout().addSpacing(20)
        self.go_to_extension_button = QPushButton("Browser Extension Setup"); self.go_to_extension_button.setFixedSize(250, 30)
        self.go_to_extension_button.clicked.connect(lambda: self.show_panel(self.extension_setup_panel)); panel.layout().addWidget(self.go_to_extension_button)
        self.go_to_download_settings_button = QPushButton("Download Settings"); self.go_to_download_settings_button.setFixedSize(250, 30)
        self.go_to_download_settings_button.clicked.connect(lambda: self.show_panel(self.download_settings_panel)); panel.layout().addWidget(self.go_to_download_settings_button)
        panel.layout().addStretch(1)
        return panel

    def create_extension_setup_panel(self):
        panel = QWidget(); panel.setLayout(QVBoxLayout()); panel.layout().setContentsMargins(20, 20, 20, 20); panel.layout().setSpacing(15)
        path_frame = QFrame(); path_frame.setLayout(QHBoxLayout()); path_frame.layout().setContentsMargins(0,0,0,0)
        path_label = QLabel("Extraction Path:"); path_label.setFixedWidth(100); path_frame.layout().addWidget(path_label)
        self.extension_path_entry = QLineEdit(); self.extension_path_entry.setText(os.path.join(os.path.expanduser("~"), "Downloads", "universal_media_tool_extension")); path_frame.layout().addWidget(self.extension_path_entry)
        self.browse_extension_dir_button = QPushButton("Browse..."); self.browse_extension_dir_button.setFixedSize(80, 30); self.browse_extension_dir_button.clicked.connect(self.browse_extension_directory); path_frame.layout().addWidget(self.browse_extension_dir_button)
        panel.layout().addWidget(path_frame)
        instructions = QLabel("Select a directory to extract the browser extension files to.\nThen, go to your browser's extensions page, enable 'Developer mode', and 'Load unpacked'."); instructions.setWordWrap(True); panel.layout().addWidget(instructions)
        button_layout = QHBoxLayout(); button_layout.addStretch(1)
        self.extract_extension_button = QPushButton("Extract Extension"); self.extract_extension_button.setFixedSize(150, 30); self.extract_extension_button.clicked.connect(self.extract_browser_extension); button_layout.addWidget(self.extract_extension_button)
        self.back_button_ext = QPushButton("Back"); self.back_button_ext.setFixedSize(100, 30); self.back_button_ext.clicked.connect(lambda: self.show_panel(self.settings_panel)); button_layout.addWidget(self.back_button_ext)
        panel.layout().addLayout(button_layout); panel.layout().addStretch(1)
        return panel

    def create_download_settings_panel(self):
        panel = QWidget(); panel.setLayout(QVBoxLayout()); panel.layout().setContentsMargins(20, 20, 20, 20); panel.layout().setSpacing(15)
        dir_frame = QFrame(); dir_frame.setLayout(QHBoxLayout()); dir_frame.layout().setContentsMargins(0,0,0,0)
        dir_label = QLabel("Download Directory:"); dir_label.setFixedWidth(120); dir_frame.layout().addWidget(dir_label)
        self.default_download_dir_entry = QLineEdit(); self.default_download_dir_entry.setReadOnly(True); self.default_download_dir_entry.setText(global_download_save_directory); dir_frame.layout().addWidget(self.default_download_dir_entry)
        self.browse_default_download_dir_button = QPushButton("Browse..."); self.browse_default_download_dir_button.setFixedSize(80, 30); self.browse_default_download_dir_button.clicked.connect(self.browse_global_download_directory); dir_frame.layout().addWidget(self.browse_default_download_dir_button)
        panel.layout().addWidget(dir_frame)
        self.overwrite_checkbox = QCustomCheckBox("Overwrite existing file", self); self.overwrite_checkbox.setChecked(global_overwrite_existing_file); self.overwrite_checkbox.stateChanged.connect(self._toggle_overwrite_setting); panel.layout().addWidget(self.overwrite_checkbox)
        double_click_frame = QFrame(); double_click_frame.setLayout(QHBoxLayout()); double_click_frame.layout().setContentsMargins(0,0,0,0)
        double_click_label = QLabel("Double click action:"); double_click_label.setFixedWidth(120); double_click_frame.layout().addWidget(double_click_label)
        self.double_click_action_dropdown = QComboBox(); self.double_click_action_dropdown.addItems(["Open folder", "Open file"]); self.double_click_action_dropdown.setCurrentText(global_double_click_action); self.double_click_action_dropdown.currentIndexChanged.connect(self._update_double_click_action); double_click_frame.layout().addWidget(self.double_click_action_dropdown); double_click_frame.layout().addStretch(1)
        panel.layout().addWidget(double_click_frame)
        panel.layout().addStretch(1)
        button_layout = QHBoxLayout(); button_layout.addStretch(1)
        self.back_button_dl = QPushButton("Back"); self.back_button_dl.setFixedSize(100, 30); self.back_button_dl.clicked.connect(lambda: self.show_panel(self.settings_panel)); button_layout.addWidget(self.back_button_dl)
        panel.layout().addLayout(button_layout)
        return panel

    def _toggle_overwrite_setting(self, state):
        global global_overwrite_existing_file; global_overwrite_existing_file = self.overwrite_checkbox.isChecked()
        self.show_status(f"Overwrite existing file set to: {global_overwrite_existing_file}", "info")

    def _update_double_click_action(self, index):
        global global_double_click_action; global_double_click_action = self.double_click_action_dropdown.currentText()
        self.show_status(f"Double-click action set to: {global_double_click_action}", "info")

    def update_settings_display(self): self.browser_monitor_switch.setChecked(browser_monitor_enabled)
    def update_extension_setup_display(self): self.extension_path_entry.setText(os.path.join(os.path.expanduser("~"), "Downloads", "universal_media_tool_extension"))
    def update_download_settings_display(self):
        self.default_download_dir_entry.setText(global_download_save_directory)
        self.overwrite_checkbox.setChecked(global_overwrite_existing_file)
        self.double_click_action_dropdown.setCurrentText(global_double_click_action)

    def add_download_to_list(self, download_info):
        """ ARCHITECTURAL FIX: Modifies the master list and notifies the SOURCE model. """
        if not any(d['url'] == download_info['url'] for d in self.active_downloads_data):
            self.active_downloads_model.beginInsertRows(QModelIndex(), len(self.active_downloads_data), len(self.active_downloads_data))
            self.active_downloads_data.append(download_info)
            self.active_downloads_model.endInsertRows()
            self.show_status(f"Download added: {download_info.get('filename', download_info.get('url'))}", "info")

    def update_download_status_in_list(self, url, new_data_dict):
        """ ARCHITECTURAL FIX: Finds item in master list, updates it, and notifies SOURCE model. """
        for i, item in enumerate(self.active_downloads_data):
            if item.get('url') == url:
                item.update(new_data_dict)
                start_idx = self.active_downloads_model.index(i, 0)
                end_idx = self.active_downloads_model.index(i, self.active_downloads_model.columnCount() - 1)
                self.active_downloads_model.dataChanged.emit(start_idx, end_idx)
                
                status = new_data_dict.get('status', '')
                if status == 'Failed' or status == 'Error':
                    self.show_status(f"Download Failed: {item.get('filename')}", "error", 10000)
                return

    def add_completed_download(self, download_info):
        """ ARCHITECTURAL FIX: Moves item from one master list to another, notifying both SOURCE models. """
        url_to_move, row_to_remove, item_to_move = download_info.get('url'), -1, None
        for i, item in enumerate(self.active_downloads_data):
            if item.get('url') == url_to_move:
                row_to_remove, item_to_move = i, item
                break
        
        if row_to_remove != -1:
            self.active_downloads_model.beginRemoveRows(QModelIndex(), row_to_remove, row_to_remove)
            self.active_downloads_data.pop(row_to_remove)
            self.active_downloads_model.endRemoveRows()

        if item_to_move:
            filetype = item_to_move.get('filetype', 'unknown')
            target_data, target_model = None, None
            if filetype == 'video': target_data, target_model = self.completed_videos_data, self.completed_videos_model
            elif filetype == 'audio': target_data, target_model = self.completed_audios_data, self.completed_audios_model
            elif filetype == 'playlist': target_data, target_model = self.completed_playlists_data, self.completed_playlists_model
            else: target_data, target_model = self.completed_videos_data, self.completed_videos_model # Fallback

            target_model.beginInsertRows(QModelIndex(), len(target_data), len(target_data))
            target_data.append(item_to_move)
            target_model.endInsertRows()
            self.show_status(f"Download completed: {item_to_move.get('filename')}", "success")

    def check_flask_message_queue(self):
        try:
            while True:
                message = gui_message_queue.get_nowait()
                msg_type = message.get('type')
                if msg_type == 'add_download': self.add_download_signal.emit(message)
                elif msg_type == 'update_download_status': self.update_download_status_signal.emit(message['url'], {k: v for k, v in message.items() if k not in ['type', 'url']})
                elif msg_type == 'add_completed': self.add_completed_signal.emit(message)
        except queue.Empty: pass

    def browse_extension_directory(self):
        directory = QFileDialog.getExistingDirectory(self, "Select Directory to Extract Browser Extension")
        if directory: self.extension_path_entry.setText(os.path.join(directory, "universal_media_tool_extension"))

    def browse_global_download_directory(self):
        global global_download_save_directory
        directory = QFileDialog.getExistingDirectory(self, "Select Default Download Directory")
        if directory: global_download_save_directory = directory; self.update_download_settings_display()

    def extract_browser_extension(self):
        target_dir = self.extension_path_entry.text().strip()
        if not target_dir: self.show_status_signal.emit("Please select a directory", "error"); return
        self.set_buttons_disabled_signal.emit(True)
        threading.Thread(target=self._extract_extension_thread, args=(target_dir,)).start()

    def _extract_extension_thread(self, target_dir):
        try:
            if os.path.exists(target_dir): shutil.rmtree(target_dir)
            shutil.copytree(EXTENSION_SOURCE_DIR_BUNDLE, target_dir)
            self.show_status_signal.emit(f"Extension extracted successfully to: {target_dir}", "success", 15000)
        except Exception as e: self.show_status_signal.emit(f"Failed to extract extension: {e}", "error", 15000)
        finally: self.set_buttons_disabled_signal.emit(False)

    def toggle_browser_monitor(self, state):
        global browser_monitor_enabled; new_status = self.browser_monitor_switch.isChecked()
        browser_monitor_enabled = new_status
        self.show_status_signal.emit(f"Browser monitoring {'enabled' if new_status else 'disabled'}", "success", 5000)
        threading.Thread(target=self._send_monitor_status_to_flask, args=(new_status,)).start()

    def _send_monitor_status_to_flask(self, enabled):
        try: requests.post(f"http://localhost:{FLASK_PORT}/set_browser_monitor_status", json={"enabled": enabled})
        except Exception as e: print(f"Could not update Flask monitor status: {e}")

    def open_add_download_dialog(self):
        dialog = AddDownloadDialog(self)
        if dialog.exec() == QDialog.Accepted:
            url, media_type = dialog.url_entry.text().strip(), dialog.media_type_group.checkedButton().text().lower()
            if not url: self.show_status("No URL provided", "error"); return
            self.set_buttons_disabled_signal.emit(True)
            threading.Thread(target=self._direct_download_thread, args=(url, media_type, 'highest_quality', None)).start()

    def _direct_download_thread(self, url, media_type, download_type, format_id):
        try:
            payload = {"url": url, "media_type": media_type, "download_type": download_type, "format_id": format_id}
            response = requests.post(f"http://localhost:{FLASK_PORT}/download", json=payload)
            if response.json().get("status") != "success": self.show_status_signal.emit(f"Download failed: {response.json().get('message')}", "error", 10000)
        except Exception as e: self.show_status_signal.emit(f"Error initiating download: {e}", "error", 10000)
        finally: self.set_buttons_disabled_signal.emit(False)


# Dialog for adding a new download
class AddDownloadDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent); self.setWindowTitle("Add New Download"); self.setFixedSize(500, 220); self.setModal(True)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        main_layout = QVBoxLayout(self); main_layout.setContentsMargins(20, 20, 20, 20); main_layout.setSpacing(15)
        url_label = QLabel("Enter URL:"); main_layout.addWidget(url_label)
        self.url_entry = QLineEdit(); self.url_entry.setPlaceholderText("Video, Audio, or Playlist URL"); main_layout.addWidget(self.url_entry)
        type_label = QLabel("Select Media Type:"); main_layout.addWidget(type_label)
        media_type_layout = QHBoxLayout(); self.media_type_group = QButtonGroup(self)
        self.radio_video = QRadioButton("Video"); self.radio_video.setChecked(True); self.media_type_group.addButton(self.radio_video)
        self.radio_audio = QRadioButton("Audio"); self.media_type_group.addButton(self.radio_audio)
        media_type_layout.addWidget(self.radio_video); media_type_layout.addWidget(self.radio_audio); media_type_layout.addStretch(1)
        main_layout.addLayout(media_type_layout); main_layout.addStretch(1)
        button_layout = QHBoxLayout(); button_layout.addStretch(1)
        self.ok_button = QPushButton("Start Download"); self.ok_button.clicked.connect(self.accept); button_layout.addWidget(self.ok_button)
        self.cancel_button = QPushButton("Cancel"); self.cancel_button.setObjectName("cancelButton"); self.cancel_button.clicked.connect(self.reject); button_layout.addWidget(self.cancel_button)
        main_layout.addLayout(button_layout)

# Custom Confirmation Dialog
class ConfirmationDialog(QDialog):
    def __init__(self, message, parent=None):
        super().__init__(parent); self.setWindowTitle("Confirm Action"); self.setFixedSize(350, 150); self.setModal(True)
        layout = QVBoxLayout(self); layout.setContentsMargins(20, 20, 20, 20); layout.setSpacing(15)
        message_label = QLabel(message); message_label.setAlignment(Qt.AlignCenter); message_label.setWordWrap(True); layout.addWidget(message_label)
        button_layout = QHBoxLayout(); button_layout.addStretch(1)
        self.yes_button = QPushButton("Yes"); self.yes_button.setFixedSize(80, 30); self.yes_button.clicked.connect(self.accept); button_layout.addWidget(self.yes_button)
        self.no_button = QPushButton("No"); self.no_button.setObjectName("cancelButton"); self.no_button.setFixedSize(80, 30); self.no_button.clicked.connect(self.reject); button_layout.addWidget(self.no_button)
        layout.addLayout(button_layout)


# --- Main Application Logic ---
def start_flask_server():
    try:
        logging.getLogger('werkzeug').setLevel(logging.ERROR)
        flask_app.run(port=FLASK_PORT, debug=False, use_reloader=False)
    except Exception as e:
        print(f"Flask server failed to start: {e}", file=sys.stderr)

if __name__ == "__main__":
    flask_thread = threading.Thread(target=start_flask_server, daemon=True)
    flask_thread.start()
    time.sleep(1) 

    app = QApplication(sys.argv)
    
    # ARCHITECTURAL FIX: Apply the custom style globally to the application
    app.setStyle(CustomSortIndicatorStyle())
    
    window = MainWindow()
    window.show()
    sys.exit(app.exec())