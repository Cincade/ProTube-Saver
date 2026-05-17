import os, threading, webview, re, json, sys, time, subprocess, shutil, importlib.metadata, traceback, contextlib

# Defense-in-depth UTF-8 reconfigure (main.py also does this). Belongs here too
# so logic.py is safe to import even when launched via paths that don't run our
# main.py — e.g., a stale frozen build whose entry point was compiled before the
# main.py fix landed. Without this, any print() that interpolates a video title
# containing emoji crashes the calling thread with 'charmap' codec errors and
# surfaces in the UI as "Stream prep failed: 'charmap' codec can't encode...".
for _s in (sys.stdout, sys.stderr):
    if _s is not None:
        try: _s.reconfigure(encoding='utf-8', errors='replace')
        except Exception: pass

from updater import YtDlpUpdater
import requests
from packaging.version import parse


# Lazy-import wrapper for yt_dlp.YoutubeDL.
#
# yt_dlp's __init__ eagerly imports ~1800 extractor modules at module load,
# which costs ~4.5s on cold start (measured: 86% of backend startup time).
# Most ProTube launches go several seconds before the user pastes a URL —
# sometimes the user just browses their library and never fetches at all.
# Deferring the import until first actual use turns "open the app" from a
# multi-second wait into a near-instant one.
#
# Call sites keep their plain `with YoutubeDL(opts) as ydl:` syntax — the
# wrapper resolves the real class on first call (cached for the rest of
# the process via `_YoutubeDL_class`). Subsequent calls hit Python's module
# cache so the cost is microseconds. Thread-safe enough: worst case is two
# threads racing on the very first import, both succeed, second one is a
# no-op against Python's `sys.modules` cache.
_YoutubeDL_class = None
def YoutubeDL(*args, **kwargs):
    global _YoutubeDL_class
    if _YoutubeDL_class is None:
        from yt_dlp import YoutubeDL as _Y
        _YoutubeDL_class = _Y
    return _YoutubeDL_class(*args, **kwargs)


# App version. Surfaced in the Settings drawer's About section and used by
# check_for_updates() to compare against the landing site's version.json.
# Bump when shipping a build.
__version__ = '1.3.0'

# Where check_for_updates() looks for the release manifest. Points at the
# landing site's version.json. If you change Netlify subdomain or move to a
# custom domain, set 'update_check_url' in settings to override without
# rebuilding the exe.
LANDING_VERSION_URL_DEFAULT = 'https://protubesaver.netlify.app/version.json'

# Alternative update source: GitHub Releases. When settings['update_source']
# is 'github', check_for_updates() polls this endpoint instead. The response
# shape is GitHub's own — we adapt it to the same {latest, downloadUrl,
# releaseNotes, releasedAt} contract the frontend already consumes.
GITHUB_RELEASES_URL_DEFAULT = 'https://api.github.com/repos/Cincade/ProTube-Saver/releases/latest'


def _resolve_ffmpeg_location():
    """
    Locate ffmpeg. When running as a PyInstaller bundle, ffmpeg(.exe) and
    ffprobe(.exe) are packed next to the executable in sys._MEIPASS. During
    development on Mac we also look in assets/mac/. Windows dev falls through
    to PATH (yt-dlp handles that).
    """
    _exe = 'ffmpeg.exe' if sys.platform == 'win32' else 'ffmpeg'
    if hasattr(sys, '_MEIPASS'):
        bundled = os.path.join(sys._MEIPASS, _exe)
        if os.path.exists(bundled):
            return sys._MEIPASS  # yt-dlp wants the DIRECTORY, not the exe path
    if sys.platform == 'darwin':
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _mac_dir = os.path.join(_root, 'assets', 'mac')
        if os.path.isfile(os.path.join(_mac_dir, 'ffmpeg')):
            return _mac_dir
    return None  # Dev mode — let yt-dlp use PATH


class _MusicDownloadCancelled(Exception):
    """Raised inside the music download progress hook when the user has clicked
    cancel on a queue item. Caught by `_music_download_worker` so it can mark
    the entry as 'cancelled' and clean up any partial file."""
    pass


def _richness(video):
    """Score how 'rich' a library entry is — more metadata = higher score. Used to pick
    the better of two duplicate entries when deduping."""
    score = 0
    for key in ('url', 'thumbnail', 'uploader', 'duration_string', 'filepath'):
        if video.get(key):
            score += 1
    if video.get('formats'):
        score += 1
    return score


class API:
    def __init__(self):
        # Path layout is owned by app_paths. settings.json + thumbnails always
        # live next to the exe (or the project dir in dev). The download_folder
        # for videos defaults to <app-dir>/data/downloads/ but is overridable
        # via settings — existing users who configured a custom path (e.g.
        # C:/Cincade/Youtube Downloads/) keep it through the migrate_legacy()
        # copy of their settings.json.
        from app_paths import (
            settings_path, thumbnails_dir, default_downloads_dir, ytdlp_runtime_dir,
        )
        self.settings_file = settings_path()
        self.thumbnail_cache_dir = thumbnails_dir()
        self.download_folder = default_downloads_dir()

        self.settings = self._load_settings()
        # Honor the user's saved download_folder if present and still valid.
        # If the saved path no longer exists (e.g. external drive unplugged),
        # we DON'T silently reset — instead we keep the saved value so the UI
        # can show the broken path and prompt the user to repick. mkdirs is
        # only attempted on the default path.
        saved = self.settings.get("download_folder")
        if saved:
            self.download_folder = saved
        else:
            # Fresh install or settings.json without a download_folder key —
            # use the default and persist it so the UI shows a real value.
            self.settings["download_folder"] = self.download_folder

        self.active_downloads = {}
        self.paused_ids = set()
        self.cancelled_ids = set()
        self.first_tick_seen = set()  # track per-video first progress tick for resume detection
        self.session_completed_ids = set()  # videos that finished during the current batch
        self.is_fetching = False
        # Concurrent download limit is user-configurable via the Settings drawer.
        # Read it at startup; default 2 if never set. New downloads will block on
        # this semaphore. Changing the limit at runtime calls
        # set_max_concurrent_downloads() which replaces this with a fresh
        # Semaphore — in-flight downloads keep their old reference and finish
        # naturally, NEW downloads then queue against the new limit.
        self.max_concurrent_downloads = int(self.settings.get('max_concurrent_downloads') or 2)
        self.max_concurrent_downloads = max(1, min(8, self.max_concurrent_downloads))
        self.download_semaphore = threading.Semaphore(self.max_concurrent_downloads)
        self.ffmpeg_location = _resolve_ffmpeg_location()

        # --- Music download queue -------------------------------------------------
        # `settings['music_queue']` holds the persistent list of queued/active/done
        # tracks. The queue processor (a single daemon thread) promotes 'queued'
        # entries up to `max_concurrent_music_downloads` at a time and spawns the
        # actual worker thread. Wake the processor by setting `_music_queue_event`
        # whenever the queue changes — avoids busy-polling. See
        # `_music_queue_processor` for the drain loop.
        # Default to 1 (serial) — running multiple yt-dlp instances in parallel
        # for music triggers a "dictionary changed size during iteration" race
        # inside yt-dlp's internal extractor state. Music files are small
        # (~3-5MB each) so serializing barely changes wall-clock time. The
        # setting still respects user-set higher values, but the default trades
        # 1-2s of speed per album for reliability.
        self.max_concurrent_music_downloads = int(
            self.settings.get('max_concurrent_music_downloads') or 1
        )
        self.max_concurrent_music_downloads = max(1, min(8, self.max_concurrent_music_downloads))
        self._music_queue_lock = threading.Lock()
        self._music_queue_event = threading.Event()
        self._music_queue_active = 0   # in-flight worker count
        self._music_queue_cancelled_ids = set()   # ids the user asked to cancel mid-flight
        # Last integer % we emitted per queue id — used to throttle progress
        # events to whole-percent changes only (avoids ~10/sec re-render spam).
        self._music_queue_last_pct = {}
        self._sanitize_music_queue_on_startup()
        threading.Thread(target=self._music_queue_processor, daemon=True).start()

        # Auto-update yt-dlp in background. The runtime dir is now next to the
        # exe (was ~/Downloads/ProTube Saver/), so updates persist across
        # builds without polluting the user's Downloads folder.
        self.updater = YtDlpUpdater(ytdlp_runtime_dir())
        # Opt-in nightly: stable lags YouTube extraction fixes by weeks; nightly catches
        # the breakage faster. Off by default (settings has no value yet → False).
        use_nightly = bool(self.settings.get('yt_dlp_use_nightly', False))
        self.updater.check_on_startup(silent=True, include_nightly=use_nightly)

        # Local HTTP server for in-app video playback. The webview can't load
        # arbitrary file:// URLs as <video src>, so we serve them through a
        # localhost HTTP endpoint. The server runs on a random port and only
        # accepts requests from localhost — safe.
        self._video_server = None
        self._video_server_port = None
        self._start_video_server()

    def _start_video_server(self):
        """Spin up a tiny localhost HTTP server that serves video files from the library
        with proper Range request support (so seek bar works). Runs in a background thread.
        Exposes one endpoint: GET /v?p=<base64-encoded-filepath>"""
        try:
            import http.server
            import socket
            import socketserver
            import threading as _t
            import base64 as _b64
            import urllib.parse as _up

            api_self = self  # capture for closure

            class VideoHandler(http.server.BaseHTTPRequestHandler):
                # Suppress noisy logs
                def log_message(self, format, *args): pass

                def _resolve_path(self):
                    """Decode the ?p= param to a real filepath. Validate it's actually
                    in the library to prevent arbitrary file reads."""
                    try:
                        parsed = _up.urlparse(self.path)
                        query = _up.parse_qs(parsed.query)
                        encoded = query.get('p', [''])[0]
                        if not encoded:
                            print('[ProTube/server] no p= param')
                            return None
                        decoded = _b64.urlsafe_b64decode(encoded.encode('ascii')).decode('utf-8')
                        if not api_self._is_known_library_filepath(decoded):
                            print(f'[ProTube/server] rejected unknown path: {decoded}')
                            return None
                        if not os.path.isfile(decoded):
                            print(f'[ProTube/server] file not found: {decoded}')
                            return None
                        return decoded
                    except Exception as e:
                        print(f'[ProTube/server] decode error: {e}')
                        return None

                def _content_type(self, filepath):
                    ext = os.path.splitext(filepath)[1].lower()
                    return {
                        '.mp4': 'video/mp4', '.m4v': 'video/mp4',
                        '.webm': 'video/webm', '.mkv': 'video/x-matroska',
                        '.mov': 'video/quicktime', '.avi': 'video/x-msvideo',
                        '.mp3': 'audio/mpeg', '.m4a': 'audio/mp4',
                    }.get(ext, 'application/octet-stream')

                def do_GET(self):
                    filepath = self._resolve_path()
                    if not filepath:
                        self.send_response(404)
                        self.end_headers()
                        return

                    file_size = os.path.getsize(filepath)
                    content_type = self._content_type(filepath)
                    range_header = self.headers.get('Range', '')

                    if range_header.startswith('bytes='):
                        # Parse: bytes=START-END (END optional)
                        try:
                            range_str = range_header[6:].strip()
                            start_str, _, end_str = range_str.partition('-')
                            start = int(start_str) if start_str else 0
                            end = int(end_str) if end_str else file_size - 1
                            end = min(end, file_size - 1)
                            length = end - start + 1
                            self.send_response(206)
                            self.send_header('Content-Type', content_type)
                            self.send_header('Accept-Ranges', 'bytes')
                            self.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
                            self.send_header('Content-Length', str(length))
                            # CORS: video element loads from http://127.0.0.1:<port> while
                            # the page is file:// — cross-origin. Without this header, calling
                            # AudioContext.createMediaElementSource(video) "taints" the audio
                            # source and the browser silences Web Audio output entirely. That
                            # was the root cause of "200% volume boost goes silent" — the
                            # GainNode chain was correct but routed tainted (muted) samples.
                            self.send_header('Access-Control-Allow-Origin', '*')
                            self.end_headers()
                            with open(filepath, 'rb') as f:
                                f.seek(start)
                                remaining = length
                                # 1 MB chunks (was 64 KB). At 1080p the player decodes
                                # ~1-2 MB per second, so 1 MB writes cover a real
                                # decoding window; 64 KB writes meant ~16 syscalls per
                                # second of video, which on slower disks left the
                                # decoder buffer hungry every few seconds.
                                while remaining > 0:
                                    chunk = f.read(min(1024 * 1024, remaining))
                                    if not chunk:
                                        break
                                    try:
                                        self.wfile.write(chunk)
                                    except (BrokenPipeError, ConnectionResetError):
                                        return
                                    remaining -= len(chunk)
                        except Exception:
                            self.send_response(500)
                            self.end_headers()
                    else:
                        # Full file
                        self.send_response(200)
                        self.send_header('Content-Type', content_type)
                        self.send_header('Content-Length', str(file_size))
                        self.send_header('Accept-Ranges', 'bytes')
                        # CORS: see range-response branch above for why this is required.
                        self.send_header('Access-Control-Allow-Origin', '*')
                        self.end_headers()
                        with open(filepath, 'rb') as f:
                            while True:
                                # 1 MB chunks — see range-handler comment above.
                                chunk = f.read(1024 * 1024)
                                if not chunk:
                                    break
                                try:
                                    self.wfile.write(chunk)
                                except (BrokenPipeError, ConnectionResetError):
                                    return

            # Bind to localhost on an OS-assigned port. Override the request handler
            # to set TCP_NODELAY on each accepted client socket — without this, Nagle's
            # algorithm coalesces small writes and 5-10s into 1080p+ playback we'd
            # see periodic stutters as the player buffer drained between sends. Pair
            # with the larger 1 MB chunk size below so each socket write feeds the
            # decoder for a meaningful slice of video time.
            class _NoDelayServer(socketserver.ThreadingTCPServer):
                allow_reuse_address = True
                daemon_threads = True
                def process_request(self, request, client_address):
                    try:
                        request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                        # 4 MB send buffer keeps the kernel queue deep enough that
                        # disk-read latency doesn't translate to socket starvation.
                        request.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
                    except OSError:
                        pass
                    super().process_request(request, client_address)

            httpd = _NoDelayServer(('127.0.0.1', 0), VideoHandler)
            self._video_server = httpd
            self._video_server_port = httpd.server_address[1]

            t = _t.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
        except Exception as e:
            print(f'[ProTube] video server failed to start: {e}')
            self._video_server_port = None

    def _is_known_library_filepath(self, filepath):
        """Security check — only allow video server to serve files we have in the library.
        Prevents the local server from being abused to read arbitrary disk files."""
        if not filepath:
            return False
        norm = os.path.normcase(os.path.normpath(filepath))
        # Also accept files inside the transcoded cache — those are produced by
        # _streamable_path_for() and live in <data>/transcoded/. They aren't in the
        # library list directly, but they're trusted (we wrote them ourselves).
        try:
            from app_paths import transcoded_cache_dir
            cache_root = os.path.normcase(os.path.normpath(transcoded_cache_dir())) + os.sep
            if norm.startswith(cache_root):
                return True
        except Exception:
            pass
        lib = self.settings.get('library', [])
        for v in lib:
            if v.get('type') == 'playlist':
                for c in v.get('videos', []):
                    fp = c.get('filepath')
                    if fp and os.path.normcase(os.path.normpath(fp)) == norm:
                        return True
            else:
                fp = v.get('filepath')
                if fp and os.path.normcase(os.path.normpath(fp)) == norm:
                    return True
        # Also allow files in the music library — the M4A audio downloads live under
        # data/music/<Artist>/<Album>/<Title>.m4a and the music player streams them
        # through this same server. Without this check the audio element 404s.
        for t in (self.settings.get('music_library') or []):
            fp = t.get('filepath')
            if fp and os.path.normcase(os.path.normpath(fp)) == norm:
                return True
        return False

    # --- Streamability helpers ---------------------------------------------------
    # These let the in-app player show files that Chromium's <video> tag can't
    # decode natively (.mkv containers, HEVC/H.265 video, AC3/DTS audio, etc.).
    # The pipeline:
    #   get_video_stream_url(id) → _streamable_path_for(filepath)
    #   _streamable_path_for     → _probe_video + _is_web_compatible
    #     compat?  → return original filepath  (fast path, current behavior)
    #     not?     → ffmpeg remux/transcode into transcoded_cache_dir, return cache path
    # Cache key includes mtime+size so a re-downloaded file invalidates the cache.

    def _ffprobe_path(self):
        """Locate ffprobe — bundled (PyInstaller _MEIPASS) first, then PATH.
        Delegates to _find_ffprobe() which already handles both. Returns None
        only if ffprobe is genuinely absent — caller falls back to extension-
        based compat checks in that case."""
        try:
            return self._find_ffprobe()
        except Exception:
            return None

    def _ffmpeg_path(self):
        """Sibling helper to _ffprobe_path. Delegates to _find_ffmpeg_exe()
        which handles bundled + PATH lookup correctly in both dev and frozen
        modes. Without this, dev runs (no _MEIPASS, ffmpeg only on PATH) would
        always fail to transcode legacy library entries."""
        try:
            return self._find_ffmpeg_exe()
        except Exception:
            return None

    def _probe_video(self, filepath):
        """Run ffprobe on the file and return {'video', 'audio', 'format'} codec
        names. Returns None on probe failure — caller should fall back to an
        extension-based heuristic in that case."""
        ffprobe = self._ffprobe_path()
        if not ffprobe:
            self._prep_log(f'[ProTube/probe] no ffprobe binary found (PATH + bundled both empty)')
            return None
        try:
            # encoding/errors keep stderr decode safe; some ffprobe builds emit
            # localized messages or filename echoes that aren't valid UTF-8.
            r = subprocess.run(
                [ffprobe, '-v', 'error', '-print_format', 'json',
                 '-show_format', '-show_streams', filepath],
                capture_output=True, text=True, timeout=10,
                encoding='utf-8', errors='replace',
                creationflags=(0x08000000 if sys.platform == 'win32' else 0),  # CREATE_NO_WINDOW
            )
            if r.returncode != 0:
                err = (r.stderr or '').strip()[:300]
                try:
                    safe = str(filepath).encode('ascii', 'replace').decode('ascii')
                except Exception:
                    safe = '<unprintable>'
                self._prep_log(f'[ProTube/probe] ffprobe rc={r.returncode} on {safe}: {err}')
                return None
            data = json.loads(r.stdout or '{}')
            v_codec = a_codec = None
            for s in data.get('streams', []):
                t = s.get('codec_type')
                if t == 'video' and not v_codec:
                    v_codec = (s.get('codec_name') or '').lower()
                elif t == 'audio' and not a_codec:
                    a_codec = (s.get('codec_name') or '').lower()
            fmt_name = (data.get('format', {}).get('format_name') or '').lower()
            return {'video': v_codec, 'audio': a_codec, 'format': fmt_name}
        except Exception as e:
            try:
                safe_path = str(filepath).encode('ascii', 'replace').decode('ascii')
                print(f'[ProTube] _probe_video failed for {safe_path}: {e}')
            except Exception:
                pass
            return None

    def _is_web_compatible(self, info, filepath):
        """Decide whether Chromium's <video> tag can decode this file natively.
        Container is determined by file extension (Chromium goes by Content-Type,
        which we set from extension); codec is determined by ffprobe."""
        ext = os.path.splitext(filepath)[1].lower()
        # Audio-only files are easy
        if ext in ('.mp3',):
            return True
        if ext == '.m4a':
            return (info or {}).get('audio') in ('aac', None)
        # Without ffprobe we can only trust that the extension is in the right
        # family. A .mp4 might still be HEVC, but we can't know without probing.
        # Returning True here means "let the player try"; if it fails, the user
        # gets an error overlay and the VLC fallback button is still available.
        if not info:
            return ext in ('.mp4', '.m4v', '.webm')
        v = info.get('video')
        a = info.get('audio')
        if ext in ('.mp4', '.m4v'):
            # Chromium / WebView2 plays H.264 (avc1) and AV1 (av01) in MP4
            # natively. yt-dlp increasingly hands us AV1-in-MP4 for 1080p+ since
            # YouTube prefers AV1 for higher resolutions; without av1 in the
            # allowlist we'd round-trip those through `-c copy` for nothing
            # (still AV1 on the other side, just a different MP4 file). HEVC is
            # deliberately omitted — playback requires the paid Microsoft HEVC
            # Video Extensions, which most users don't have, so we'd rather
            # transcode up front than send an unplayable stream.
            return v in ('h264', 'avc1', 'av1', 'av01') and a in ('aac', 'mp3', None)
        if ext == '.webm':
            return v in ('vp8', 'vp9', 'av1', 'av01') and a in ('opus', 'vorbis', None)
        # .mkv / .avi / .mov / anything else → not directly playable
        return False

    def _streamable_cache_path(self, filepath):
        """Derive a stable cache path for the transcoded copy of `filepath`. The
        key includes mtime+size so a redownload (different mtime) invalidates."""
        from app_paths import transcoded_cache_dir
        import hashlib
        try:
            st = os.stat(filepath)
            key = f'{filepath}|{st.st_mtime_ns}|{st.st_size}'
        except OSError:
            key = filepath
        h = hashlib.sha1(key.encode('utf-8', errors='replace')).hexdigest()[:16]
        return os.path.join(transcoded_cache_dir(), f'{h}.mp4')

    def _prep_log(self, msg):
        """Append a timestamped line to data/prep.log so we have a persistent
        record of what happened during stream prep — independent of stdout,
        which gets eaten when the app launches via the VBS or a frozen exe.
        Also mirrors to stdout for live debugging."""
        try:
            from app_paths import data_dir
            line = f'{time.strftime("%H:%M:%S")} {msg}'
            try:
                line.encode('ascii')
                ascii_line = line
            except UnicodeEncodeError:
                ascii_line = line.encode('ascii', 'replace').decode('ascii')
            with open(os.path.join(data_dir(), 'prep.log'), 'a', encoding='utf-8') as f:
                f.write(line + '\n')
            try: print(ascii_line)
            except Exception: pass
        except Exception:
            pass

    def get_prep_log(self, tail=120):
        """Frontend reads this when the prep state hangs/fails so we can
        surface diagnostics to the player overlay. Returns the last `tail`
        lines of data/prep.log. Used as a debugging shortcut so the user
        doesn't have to dig the file out themselves."""
        try:
            from app_paths import data_dir
            path = os.path.join(data_dir(), 'prep.log')
            if not os.path.exists(path):
                return ''
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
            return ''.join(lines[-int(tail):])
        except Exception as e:
            return f'log read failed: {e}'

    def _probe_duration_us(self, filepath):
        """Total media duration in microseconds, or None if probe fails. Used by
        the transcode worker to compute a percent-complete value from ffmpeg's
        `-progress pipe:1` output (which reports out_time_us)."""
        ffprobe = self._ffprobe_path()
        if not ffprobe:
            return None
        try:
            r = subprocess.run(
                [ffprobe, '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'csv=p=0', filepath],
                capture_output=True, text=True, timeout=10,
                encoding='utf-8', errors='replace',
                creationflags=(0x08000000 if sys.platform == 'win32' else 0),
            )
            if r.returncode == 0 and r.stdout.strip():
                return int(float(r.stdout.strip()) * 1_000_000)
        except Exception:
            pass
        return None

    def _immediate_streamable_path(self, filepath):
        """Return a path the in-app player can decode directly RIGHT NOW —
        either the source itself (if web-compatible) or a previously cached
        transcoded copy. Returns None when neither applies, in which case the
        caller should kick off a background transcode via _transcode_worker.

        Probes the file once and stashes the result on self._pending_probe so
        the worker can reuse it without re-running ffprobe."""
        def _safe(s):
            try: return str(s).encode('ascii', errors='replace').decode('ascii')
            except Exception: return '<unprintable>'
        self._last_prep_error = None
        self._pending_probe = None
        if not filepath or not os.path.exists(filepath):
            self._last_prep_error = f'File missing on disk: {_safe(filepath)}'
            self._prep_log(f'[ProTube/streamable] file gone: {_safe(filepath)}')
            return None
        info = self._probe_video(filepath)
        compat = self._is_web_compatible(info, filepath)
        ext = os.path.splitext(filepath)[1].lower()
        self._prep_log(f'[ProTube/streamable] {_safe(os.path.basename(filepath))} '
              f'ext={ext} probe={info} compat={compat}')
        if compat:
            return filepath
        cache_path = self._streamable_cache_path(filepath)
        if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
            self._prep_log(f'[ProTube/streamable] cache hit: {_safe(cache_path)}')
            return cache_path
        # Need to transcode. Stash probe result for the worker.
        self._pending_probe = info
        return None

    def _transcode_worker(self, src_path, cache_path, info, job_id, target):
        """Run ffmpeg in a background thread, push progress updates to JS via
        protubePrepProgress(job_id, percent), and signal completion via
        protubePrepDone(job_id, response_dict) or protubePrepError(job_id, msg).

        Strategy — two-stage, fast path first:
          1. Try `-c copy -f mp4` (container-only remux, near-instant for any
             file whose codecs already fit MP4: H.264/AAC, H.265/AAC, etc.).
             VLC plays MKVs effortlessly because it doesn't care about the
             wrapper; doing the same for the in-app player just means swapping
             MKV bytes for MP4 bytes around the same payload. Disk-bound, no
             encoding work, completes in seconds even for GB-scale files.
          2. If `-c copy` fails (codec genuinely incompatible with MP4 — VP9 in
             a mkv, opus in some configs, etc.), fall back to libx264
             ultrafast + AAC. Slow, but correctness > speed for the long tail.
        """
        def _safe(s):
            try: return str(s).encode('ascii', errors='replace').decode('ascii')
            except Exception: return '<unprintable>'
        ffmpeg = self._ffmpeg_path()
        try:
            # ----- STAGE 1: -c copy remux ---------------------------------
            # No re-encoding, just container rewrite. ffmpeg streams the
            # video and audio bitstreams unmodified into a new MP4. For an
            # MKV with H.264 + AAC this is a few seconds for a multi-GB file.
            try: self._send_to_js('protubePrepProgress', job_id, 1)
            except Exception: pass
            self._prep_log(f'[ProTube/transcode] stage 1: -c copy remux for '
                  f'{_safe(os.path.basename(src_path))} (job={job_id})')
            remux_tmp = cache_path + '.remux.mp4'
            # `-map 0:v:0? -map 0:a:0?` is the critical bit. Without explicit
            # stream selection ffmpeg copies EVERY stream by default, including
            # SRT/ASS subtitles and font/image attachments that an MKV almost
            # always carries. The MP4 muxer rejects those (it only knows tx3g
            # and a few others), so the entire remux aborts with "Subtitle codec
            # X is not supported by MP4" — and we'd fall through to the slow
            # transcode for no reason. Mapping just the first video + first
            # audio (with `?` making each optional, so audio-less or video-less
            # files don't error) gives us the same playback the user wants and
            # ignores everything MP4 can't carry. -sn / -dn / -an-not are
            # belt-and-braces in case the muxer auto-selects extra streams.
            # -avoid_negative_ts make_zero handles MKVs with negative start
            # timestamps (common for streams cut from longer sources) which
            # otherwise cause the MP4 to start with frozen video.
            remux_cmd = [
                ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
                '-i', src_path,
                '-map', '0:v:0?', '-map', '0:a:0?',
                '-sn', '-dn',
                '-c', 'copy',
                '-avoid_negative_ts', 'make_zero',
                '-movflags', '+faststart',
                '-f', 'mp4',
                remux_tmp,
            ]
            try:
                rr = subprocess.run(
                    remux_cmd, capture_output=True, timeout=60 * 10,
                    text=True, encoding='utf-8', errors='replace',
                    creationflags=(0x08000000 if sys.platform == 'win32' else 0),
                )
            except subprocess.TimeoutExpired:
                rr = None
                try: os.remove(remux_tmp)
                except OSError: pass
            if rr is not None and rr.returncode == 0 and os.path.exists(remux_tmp) \
                    and os.path.getsize(remux_tmp) > 0:
                # Success — atomic rename and ship.
                try: os.replace(remux_tmp, cache_path)
                except Exception as e:
                    try: self._send_to_js('protubePrepError', job_id,
                                          f'Cache write failed: {_safe(e)}')
                    except Exception: pass
                    return
                self._prep_log(f'[ProTube/transcode] remux ok (job={job_id})')
                self._ship_done(cache_path, src_path, target, job_id)
                return
            # Remux failed — clean up partial and fall through to transcode.
            if rr is not None:
                err_lines = [ln for ln in (rr.stderr or '').splitlines() if ln.strip()]
                err_first = err_lines[0] if err_lines else ''
                self._prep_log(f'[ProTube/transcode] remux failed (rc='
                      f'{rr.returncode}), trying full transcode. First stderr: '
                      f'{_safe(err_first)}')
            try: os.remove(remux_tmp)
            except OSError: pass

            # ----- STAGE 2: full transcode --------------------------------
            v = (info or {}).get('video') or ''
            a = (info or {}).get('audio') or ''
            duration_us = self._probe_duration_us(src_path)
            # Even in the transcode path, copy whichever stream we can.
            v_args = (['-c:v', 'copy'] if v in ('h264',)
                      else ['-c:v', 'libx264', '-preset', 'ultrafast',
                            '-crf', '23', '-pix_fmt', 'yuv420p'])
            a_args = (['-c:a', 'copy'] if a in ('aac',)
                      else ['-c:a', 'aac', '-b:a', '192k'])
            out_tmp = cache_path + '.tmp.mp4'
            cmd = [
                ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
                '-i', src_path,
                # Same stream-selection logic as stage 1 — MKV subtitle/data
                # streams will trip the MP4 muxer here too if we let ffmpeg
                # auto-select. Stick to one video + one audio.
                '-map', '0:v:0?', '-map', '0:a:0?',
                '-sn', '-dn',
                *v_args, *a_args,
                '-movflags', '+faststart',
                '-progress', 'pipe:1',
                '-f', 'mp4',
                out_tmp,
            ]
            self._prep_log(f'[ProTube/transcode] stage 2: re-encoding '
                  f'{_safe(os.path.basename(src_path))} '
                  f'(v={v}, a={a}, dur={duration_us}us, job={job_id})')

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True, encoding='utf-8', errors='replace',
                creationflags=(0x08000000 if sys.platform == 'win32' else 0),
            )

            last_send = 0.0
            last_pct = -1
            try:
                for raw in iter(proc.stdout.readline, ''):
                    line = raw.strip()
                    if not line.startswith('out_time_us='):
                        continue
                    try:
                        cur_us = int(line.split('=', 1)[1])
                    except (ValueError, IndexError):
                        continue
                    if not duration_us:
                        continue
                    pct = max(0, min(99, int(cur_us / duration_us * 100)))
                    now = time.time()
                    if pct != last_pct and (now - last_send) > 0.4:
                        try: self._send_to_js('protubePrepProgress', job_id, pct)
                        except Exception: pass
                        last_send = now
                        last_pct = pct
            except Exception as e:
                self._prep_log(f'[ProTube/transcode] progress reader crashed: {_safe(e)}')

            proc.wait()
            if proc.returncode != 0 or not os.path.exists(out_tmp):
                try: os.remove(out_tmp)
                except OSError: pass
                stderr_text = ''
                try: stderr_text = proc.stderr.read() or ''
                except Exception: pass
                lines = [ln for ln in stderr_text.splitlines() if ln.strip()]
                first = lines[0] if lines else 'no stderr'
                last = lines[-1] if lines else ''
                msg = (f'ffmpeg transcode failed (rc={proc.returncode}, v={v}, a={a}). '
                       f'Error: {_safe(first)}'
                       + (f' | {_safe(last)}' if last and last != first else ''))
                self._prep_log(f'[ProTube/transcode] failed: {_safe(stderr_text[:800])}')
                try: self._send_to_js('protubePrepError', job_id, msg)
                except Exception: pass
                return

            try: os.replace(out_tmp, cache_path)
            except Exception as e:
                try: self._send_to_js('protubePrepError', job_id,
                                      f'Cache write failed: {_safe(e)}')
                except Exception: pass
                return
            self._prep_log(f'[ProTube/transcode] re-encode done (job={job_id})')
            self._ship_done(cache_path, src_path, target, job_id)
        except Exception as e:
            self._prep_log(f'[ProTube/transcode] worker crashed: {_safe(e)}')
            try: self._send_to_js('protubePrepError', job_id,
                                  f'Transcode worker crashed: {_safe(e)}')
            except Exception: pass

    def _ship_done(self, cache_path, src_path, target, job_id):
        """Build the streaming response and signal protubePrepDone. Shared by
        both stages of _transcode_worker so they emit identical payloads."""
        import base64 as _b64
        encoded = _b64.urlsafe_b64encode(cache_path.encode('utf-8')).decode('ascii')
        response = {
            'url': f'http://127.0.0.1:{self._video_server_port}/v?p={encoded}',
            'filepath': src_path,
            'title': (target or {}).get('title', ''),
            'last_position_seconds': (target or {}).get('last_position_seconds') or 0,
            'last_duration_seconds': (target or {}).get('last_duration_seconds') or 0,
        }
        try: self._send_to_js('protubePrepProgress', job_id, 100)
        except Exception: pass
        try: self._send_to_js('protubePrepDone', job_id, response)
        except Exception: pass

    def save_playback_position(self, video_id, position_seconds, duration_seconds=None):
        """Persist where the user paused/left a video so they can resume later. Stored
        on the library entry directly. Called periodically during playback (every ~5s)
        and on player close. Skips saves for the first few seconds (avoids spurious
        'resumes from 0:02') and ignores positions past 95% of duration (treats them
        as 'finished — start fresh next time'). When a video crosses the 95% mark we
        also stamp `watched_to_end` so the NEW badge stays hidden even after position
        is cleared on natural end."""
        if not video_id or position_seconds is None:
            return {'ok': False}
        try:
            position_seconds = float(position_seconds)
        except (ValueError, TypeError):
            return {'ok': False}

        # Don't save trivially-small positions or completed views
        if position_seconds < 5:
            # Treat as "haven't really started" — clear any stored position
            return self._clear_playback_position(video_id)
        if duration_seconds and duration_seconds > 0:
            try:
                duration_seconds = float(duration_seconds)
                if position_seconds >= duration_seconds * 0.95:
                    # User basically finished — mark watched_to_end (so NEW badge
                    # stays hidden) and clear position so next open starts fresh.
                    self._mark_watched_to_end(video_id)
                    return self._clear_playback_position(video_id)
            except (ValueError, TypeError):
                pass

        # Find the entry
        target = self._find_library_entry(video_id)
        if not target:
            return {'ok': False, 'error': 'not found'}

        target['last_position_seconds'] = round(position_seconds, 1)
        target['last_watched_at'] = int(time.time())
        if duration_seconds:
            target['last_duration_seconds'] = round(float(duration_seconds), 1)
        self._save_settings()
        return {'ok': True}

    def _mark_watched_to_end(self, video_id):
        """Stamp `watched_to_end=True` on the library entry. Used to suppress the
        NEW badge once a video has been completed. Persistent across position clears."""
        target = self._find_library_entry(video_id)
        if not target:
            return
        target['watched_to_end'] = True
        # Don't save here — caller will save_settings via _clear_playback_position
        # (saves a redundant disk write). But safe to save defensively if we end up
        # called from a path that doesn't follow with another save.
        self._save_settings()

    def mark_watched_to_end(self, video_id):
        """Public API wrapper for _mark_watched_to_end. Frontend calls this when a
        video ends but duration is unknown (rare), so the 95% logic in
        save_playback_position can't trigger."""
        self._mark_watched_to_end(video_id)
        return {'ok': True}

    def _clear_playback_position(self, video_id):
        target = self._find_library_entry(video_id)
        if not target:
            return {'ok': False}
        target.pop('last_position_seconds', None)
        target.pop('last_duration_seconds', None)
        # Keep last_watched_at — useful for "recently watched" sorting later
        self._save_settings()
        return {'ok': True, 'cleared': True}

    def _find_library_entry(self, video_id):
        """Helper: locate a library entry by id (top-level or playlist child)."""
        lib = self.settings.get('library', [])
        for v in lib:
            if v.get('id') == video_id:
                return v
            if v.get('type') == 'playlist':
                for c in v.get('videos', []):
                    if c.get('id') == video_id:
                        return c
        return None

    def get_video_stream_url(self, video_id):
        """Frontend calls this with a library video id. Returns an HTTP URL the
        <video> tag can use as src. URL points at our localhost video server,
        which streams the file with Range support so seeking works.

        Wrapped in a top-level try/except so unexpected exceptions surface as
        structured error responses instead of propagating through pywebview as
        a generic Promise rejection (which the JS catches as "Backend error
        starting stream" — useless for diagnosis). Any exception is logged with
        a stack trace, and the user sees a hint that points them at the VLC
        fallback while we figure out what failed."""
        try:
            return self._get_video_stream_url_impl(video_id)
        except Exception as e:
            import traceback
            print(f'[ProTube] get_video_stream_url crashed for {video_id}: {e}')
            traceback.print_exc()
            return {'error': f'Stream prep failed: {e}. Try "Open in VLC".'}

    def _get_video_stream_url_impl(self, video_id):
        if not self._video_server_port:
            print('[ProTube] get_video_stream_url: video server not running')
            return {'error': 'Video server not running'}

        # Find the entry by id
        lib = self.settings.get('library', [])
        target = None
        for v in lib:
            if v.get('id') == video_id:
                target = v
                break
            if v.get('type') == 'playlist':
                for c in v.get('videos', []):
                    if c.get('id') == video_id:
                        target = c
                        break
                if target:
                    break

        if not target:
            print(f'[ProTube] get_video_stream_url: video {video_id} not in library')
            return {'error': 'Video not found in library'}
        filepath = target.get('filepath')
        if not filepath:
            print(f'[ProTube] get_video_stream_url: no filepath for {video_id}')
            return {'error': 'No filepath stored for this video'}
        # ASCII-safe path stringification for logs.
        def _safe(s):
            try: return str(s).encode('ascii', errors='replace').decode('ascii')
            except Exception: return '<unprintable>'
        if not os.path.exists(filepath):
            # Try self-healing first
            if self._is_file_missing(target):
                print(f'[ProTube] get_video_stream_url: file gone {_safe(filepath)}')
                return {'error': 'File missing from disk'}
            filepath = target.get('filepath')

        # Fast path: source is already web-compatible OR a cached transcode
        # exists from a previous play. _immediate_streamable_path probes and
        # returns the path we should serve, or None if a fresh transcode is
        # needed. Either way it's quick (<200ms).
        immediate = self._immediate_streamable_path(filepath)
        if immediate:
            print(f'[ProTube] streaming{" (cache)" if immediate != filepath else ""}: '
                  f'{_safe(immediate)} ({os.path.getsize(immediate)} bytes)')
            import base64 as _b64
            encoded = _b64.urlsafe_b64encode(immediate.encode('utf-8')).decode('ascii')
            return {
                'url': f'http://127.0.0.1:{self._video_server_port}/v?p={encoded}',
                'filepath': filepath,
                'title': target.get('title', ''),
                'last_position_seconds': target.get('last_position_seconds') or 0,
                'last_duration_seconds': target.get('last_duration_seconds') or 0,
            }

        # Slow path: transcode required. Return immediately with a "preparing"
        # marker so the JS player can show a progress UI, and kick off ffmpeg
        # in a background thread that pushes progress + completion via
        # _send_to_js. Without this the API call blocked the pywebview thread
        # for the entire transcode (could be many minutes), with no feedback.
        ffmpeg = self._ffmpeg_path()
        if not ffmpeg:
            reason = getattr(self, '_last_prep_error', None) or (
                'ffmpeg not found and source is not web-compatible. Install ffmpeg '
                'or use Open in VLC.'
            )
            return {'error': f'Could not prepare for in-app playback. {reason}'}
        cache_path = self._streamable_cache_path(filepath)
        info = getattr(self, '_pending_probe', None)
        # Generate a short job id used by the JS handlers as a correlation key.
        import uuid as _uuid
        job_id = _uuid.uuid4().hex[:8]
        threading.Thread(
            target=self._transcode_worker,
            args=(filepath, cache_path, info, job_id, target),
            daemon=True,
            name=f'protube-transcode-{job_id}',
        ).start()
        return {
            'preparing': True,
            'job_id': job_id,
            'title': target.get('title', ''),
        }

    def _get_ydl_opts(self, cookie_mode, cookie_value):
        opts = {'quiet': True, 'no_warnings': True, 'noprogress': True, 'ratelimit': 10*1024*1024}
        if self.ffmpeg_location:
            opts['ffmpeg_location'] = self.ffmpeg_location
        if cookie_mode == 'browser' and cookie_value != 'none': opts['cookiesfrombrowser'] = (cookie_value,)
        elif cookie_mode == 'file' and os.path.exists(cookie_value): opts['cookies'] = cookie_value
        return opts

    def fetch_url_info(self, url, cookie_mode, cookie_value):
        if self.is_fetching: return
        self.is_fetching = True
        threading.Thread(target=self._fetch_worker, args=(url, cookie_mode, cookie_value), daemon=True).start()

    def _normalize_channel_url(self, url):
        """A bare YouTube channel URL (`/@handle`, `/c/custom`, `/channel/UCxxx`,
        `/user/legacy`) is ambiguous — yt-dlp may fetch every tab (Videos +
        Shorts + Streams + Community + Playlists) and concatenate them, which
        gives a messy mixed queue.

        Force `/videos` for bare channel URLs so we always get just the
        long-form Videos tab. Users who explicitly want Shorts or Streams
        can paste `/shorts` or `/streams` and we leave it alone — they typed
        the tab name, they meant it.

        Watch, playlist, shortlink, embed, and shorts/<id> URLs are untouched.
        """
        if not url or not isinstance(url, str):
            return url
        stripped = url.strip()
        base = stripped.rstrip('/')
        base_low = base.lower()
        # Already on an explicit tab — pass through unchanged
        explicit_tabs = (
            '/videos', '/shorts', '/streams', '/live', '/playlists',
            '/community', '/about', '/featured', '/podcasts', '/courses',
            '/membership', '/store', '/releases',
        )
        if any(base_low.endswith(t) for t in explicit_tabs):
            return stripped
        # Watch / playlist / shortlink / embed / individual shorts video → not a channel
        u = base_low
        if ('/watch' in u or '/playlist' in u or 'youtu.be/' in u
                or '/embed/' in u or re.search(r'/shorts/[\w-]+', u)):
            return stripped
        # Bare channel patterns. Pattern is matched against base_low (lowercased)
        # so the channel-id branch uses lowercase `uc` — real YouTube IDs are
        # always `UC...` but they're now lowercased here.
        if re.search(r'youtube\.com/(@[\w.-]+|channel/[\w-]+|c/[\w.-]+|user/[\w.-]+)$', base_low):
            return base + '/videos'
        return stripped

    def _fetch_worker(self, url, cookie_mode, cookie_value):
        """
        Fetches URL info. Two fast paths:
        - Single video: extract fully (formats included). Fast.
        - Playlist: extract FLAT (no per-video format probe). ~2s for any size playlist.
          Format resolution happens later via resolve_playlist_formats when user drills in.
        """
        # Bare channel URLs get auto-rewritten to /videos so the fetch returns
        # only long-form videos, not Shorts/Streams/Community mashed together.
        url = self._normalize_channel_url(url)
        try:
            base_opts = self._get_ydl_opts(cookie_mode, cookie_value)

            # Step 1: probe whether this URL is a playlist using the cheapest possible call
            probe_opts = {**base_opts, 'extract_flat': True, 'skip_download': True}
            with YoutubeDL(probe_opts) as ydl:
                probe = ydl.extract_info(url, download=False)

            is_playlist = probe.get('_type') == 'playlist' or 'entries' in probe

            if is_playlist:
                self._handle_playlist_fetch(probe, base_opts)
            else:
                # Single video — do a full fetch to get formats (same as before)
                self._handle_single_video_fetch(url, base_opts)

        except Exception as e:
            self._send_to_js('finishFetch', f"Error: {str(e)}")
        finally:
            self.is_fetching = False

    def _handle_single_video_fetch(self, url, base_opts):
        """Full extract for a single video (formats included)."""
        with YoutubeDL(base_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        formats, size_map = self._parse_formats(info)
        # Pre-select the user's default quality if it's actually available in
        # this video's format list; otherwise leave selectedQuality unset so
        # the queue card falls back to formats[0] (best). This makes the
        # Settings → Default quality preference take effect for new pastes
        # without showing a label that doesn't match any available format.
        pref = self._user_default_quality()
        selected = None
        if formats and any((f.get('label') == pref) for f in formats):
            selected = pref
        video = {
            "type": "video",
            "id": info['id'],
            "url": info.get('webpage_url'),
            "title": info.get('title', 'Untitled'),
            "uploader": info.get('channel', 'N/A'),
            # uploader_url enables the "Add channel to queue" action in the detail panel
            # later. yt-dlp provides this directly; older library entries that pre-date this
            # field will fall back to a uploader-name-based reconstruction at action time.
            "uploader_url": info.get('uploader_url') or info.get('channel_url'),
            "thumbnail": info.get('thumbnail'),
            "formats": formats,
            "sizeMap": size_map,
            "duration_string": self._format_duration(info.get('duration')),
        }
        if selected:
            video['selectedQuality'] = selected
        self._send_to_js('handleFullFetch', [video], info.get('title', 'ProTube Downloads'), False)
        # Cache the thumbnail in the background so the queue item survives offline.
        self._start_thumbnail_caching_for_queue([video], playlist_id=None)

    def _handle_playlist_fetch(self, probe, base_opts):
        """
        Flat playlist handling. The `probe` already has entries from extract_flat=True,
        which contain: id, title, url, duration, thumbnails, uploader (sometimes).
        We build a single playlist queue item with child videos (formats=null for now).
        """
        entries = [e for e in (probe.get('entries') or []) if e]

        children = []
        for e in entries:
            thumb = None
            thumbs = e.get('thumbnails') or []
            if thumbs:
                thumb = thumbs[-1].get('url')  # highest-res available in flat mode
            elif e.get('thumbnail'):
                thumb = e.get('thumbnail')
            else:
                vid_id = e.get('id')
                if vid_id:
                    thumb = f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg"

            children.append({
                "type": "video",
                "id": e.get('id'),
                "url": e.get('url') or e.get('webpage_url'),
                "title": e.get('title', 'Untitled'),
                "uploader": e.get('uploader') or e.get('channel') or probe.get('uploader', 'N/A'),
                "thumbnail": thumb,
                "formats": None,   # sentinel: not yet resolved
                "sizeMap": {},
                "duration_string": self._format_duration(e.get('duration')),
                "selected": True,  # per-video selection inside the playlist; default on
            })

        source_url = probe.get('webpage_url') or probe.get('original_url')
        playlist = {
            "type": "playlist",
            "id": probe.get('id') or f"pl_{int(time.time())}",
            "url": source_url,
            # 'channel' vs 'playlist' — drives the badge in the UI and affects how
            # the "Check for updates" flow describes itself. Channel URLs (the user
            # pastes them from the channel's Videos tab) re-fetch the same way as
            # playlists; the distinction is purely semantic for display.
            "subtype": self._classify_playlist_url(source_url),
            "title": probe.get('title', 'Untitled Playlist'),
            "uploader": probe.get('uploader') or probe.get('channel', 'N/A'),
            "videoCount": len(children),
            # Honor the user's Settings → Default quality preference. Falls back
            # to 1080p when unset. _user_default_quality maps 'best'/'audio'
            # abstractions to picker-compatible resolutions.
            "defaultQuality": self._user_default_quality(),
            "thumbnails": [c.get('thumbnail') for c in children[:4] if c.get('thumbnail')],
            "videos": children,
            "formatsResolved": False,
        }

        self._send_to_js('handleFullFetch', [playlist], probe.get('title', 'ProTube Downloads'), True)
        # Kick off a background pass to cache the thumbnails locally so they
        # survive offline. We do this AFTER sending the fetch result so the
        # user sees their queue immediately; thumbnail markers stream back as
        # each one is downloaded.
        self._start_thumbnail_caching_for_queue(children, playlist_id=playlist['id'])

    def _start_thumbnail_caching_for_queue(self, items, playlist_id=None):
        """Run thumbnail caching in a background thread for a list of just-fetched
        queue items. Three jobs:
        1. Download the remote thumbnail to disk (so it works offline)
        2. Persist the new 'pt:thumb:' marker into settings['queue'] so it survives
           app restarts (otherwise queue restores from disk with stale remote URLs)
        3. Push a UI update with the marker AND data URL so the frontend can swap
           the remote URL for the cached version with zero render flash.

        Items already pointing at 'pt:thumb:' markers are skipped. On failure, the
        original remote URL is preserved (still works online)."""
        def _worker():
            queue_was_modified = False
            # Batch frontend updates instead of firing one bridge call per
            # cached thumbnail. For a 200-video channel that's the difference
            # between ~200 evaluate_js round-trips and ~25 — each round-trip
            # takes 5-50ms on WebView2, so the cumulative savings are real
            # and you can see the queue render without UI stutter during cache.
            BATCH_SIZE = 8
            BATCH_FLUSH_MS = 220
            pending = []
            last_flush_ms = [int(time.time() * 1000)]

            def _flush():
                if not pending:
                    return
                try:
                    self._send_to_js('updateItemThumbnailBatch', pending[:])
                except Exception:
                    pass
                pending.clear()
                last_flush_ms[0] = int(time.time() * 1000)

            for item in items:
                vid_id = item.get('id')
                thumb = item.get('thumbnail') or ''
                if not vid_id or not thumb:
                    continue
                if thumb.startswith('pt:thumb:'):
                    continue  # already a marker
                if not (thumb.startswith('http://') or thumb.startswith('https://')):
                    continue  # not a remote URL
                try:
                    marker = self._cache_thumbnail(thumb, vid_id)
                    if not (marker and marker.startswith('pt:thumb:')):
                        continue  # caching failed (offline?), leave as-is

                    # Persist the marker into settings['queue']. Walks both top-level
                    # entries and playlist children. Without this, queue restores from
                    # disk on next launch with the OLD remote URL — defeating offline.
                    if self._update_queue_thumbnail(vid_id, marker, playlist_id):
                        queue_was_modified = True

                    # Build the data URL inline so frontend renders without flash.
                    # If get_thumbnail_data fails, we still ship the marker — the
                    # frontend resolver will pick it up via the regular path.
                    data_url = ''
                    try:
                        data_url = self.get_thumbnail_data(marker) or ''
                    except Exception:
                        data_url = ''

                    pending.append({
                        'id': vid_id,
                        'marker': marker,
                        'playlistId': playlist_id,
                        'dataUrl': data_url,
                    })
                    # Flush when batch is full OR enough time has passed since
                    # the last flush — whichever comes first. Time-based flush
                    # matters for slow remote thumbs where each iteration takes
                    # noticeable time and the user shouldn't have to wait 8
                    # downloads before seeing any cache hits.
                    now_ms = int(time.time() * 1000)
                    if len(pending) >= BATCH_SIZE or (now_ms - last_flush_ms[0]) >= BATCH_FLUSH_MS:
                        _flush()
                except Exception:
                    pass  # caching is best-effort
            # Final flush for the trailing partial batch
            _flush()
            # Save settings once at the end so we don't write to disk N times during
            # a 50-video playlist cache. Only saves if anything was actually changed.
            if queue_was_modified:
                self._save_settings()
        threading.Thread(target=_worker, daemon=True).start()

    def _update_queue_thumbnail(self, video_id, marker, playlist_id=None):
        """Update the thumbnail field on a queue entry. Returns True if anything
        changed (caller decides whether to flush settings to disk). Walks both
        top-level queue items and playlist children to find the right entry."""
        queue = self.settings.get('queue', [])
        for entry in queue:
            if playlist_id and entry.get('id') == playlist_id and entry.get('type') == 'playlist':
                # Look inside this playlist's videos
                for child in entry.get('videos', []):
                    if child.get('id') == video_id:
                        child['thumbnail'] = marker
                        return True
            elif not playlist_id and entry.get('id') == video_id:
                entry['thumbnail'] = marker
                return True
        return False

    def resolve_playlist_formats(self, playlist_id, video_urls, cookie_mode, cookie_value):
        """
        Called when the user opens a playlist's detail view.
        Resolves formats for each video in a background thread and streams updates.
        video_urls: [{id, url}, ...]
        """
        threading.Thread(
            target=self._resolve_formats_worker,
            args=(playlist_id, video_urls, cookie_mode, cookie_value),
            daemon=True,
        ).start()

    def _resolve_formats_worker(self, playlist_id, video_urls, cookie_mode, cookie_value):
        """
        Resolves formats serially for each video. Serial (not concurrent) to avoid
        tripping YouTube rate limits on large playlists. Each resolution streams
        back to the UI as it completes.
        """
        base_opts = self._get_ydl_opts(cookie_mode, cookie_value)
        total = len(video_urls)

        for idx, item in enumerate(video_urls):
            vid_id = item.get('id')
            vid_url = item.get('url')
            if not vid_id or not vid_url:
                continue
            try:
                with YoutubeDL(base_opts) as ydl:
                    info = ydl.extract_info(vid_url, download=False)
                formats, size_map = self._parse_formats(info)
                payload = {
                    "id": vid_id,
                    "formats": formats,
                    "sizeMap": size_map,
                    # Updated fields in case flat mode missed them:
                    "uploader": info.get('channel') or info.get('uploader'),
                    "thumbnail": info.get('thumbnail'),
                    "duration_string": self._format_duration(info.get('duration')),
                }
                self._send_to_js('onVideoFormatsResolved', playlist_id, payload, idx + 1, total)
            except Exception as e:
                self._send_to_js('onVideoFormatsFailed', playlist_id, vid_id, str(e))

        self._send_to_js('onPlaylistFormatsComplete', playlist_id)

    def start_download(self, videos, mode, val):
        """
        Accepts a mix of video items and playlist items. Playlist items are expanded
        into their selected children with playlist metadata attached.
        """
        # New download session — clear any stale cancel flags from previous runs
        self.cancelled_ids.clear()
        for item in videos:
            item_type = item.get('type', 'video')

            if item_type == 'playlist':
                # Expand: download each selected child video, tagged with playlist metadata
                default_q = item.get('defaultQuality', '1080p')
                p_title = item.get('title', 'Playlist')
                p_id = item.get('id')
                for child in item.get('videos', []):
                    if not child.get('selected', True):
                        continue
                    if not child.get('formats'):
                        # Formats never resolved — skip, can't determine quality mapping
                        continue
                    if child['id'] in self.active_downloads:
                        continue
                    enriched = {
                        **child,
                        'selectedQuality': child.get('selectedQuality') or default_q,
                        'isFromPlaylist': True,
                        'playlistTitle': p_title,
                        'playlistId': p_id,
                    }
                    t = threading.Thread(target=self._download_worker, args=(enriched, mode, val), daemon=True)
                    self.active_downloads[child['id']] = t
                    t.start()
            else:
                if item['id'] in self.active_downloads:
                    continue
                t = threading.Thread(target=self._download_worker, args=(item, mode, val), daemon=True)
                self.active_downloads[item['id']] = t
                t.start()

    def _download_worker(self, video_data, mode, val):
        video_id = video_data['id']
        playlist_id = video_data.get('playlistId')  # None for non-playlist downloads

        # If download was cancelled before this thread even started, bail out silently
        if video_id in self.cancelled_ids:
            self.active_downloads.pop(video_id, None)
            if len(self.active_downloads) == 0:
                completed_count = len(self.session_completed_ids)
                self.session_completed_ids.clear()
                self._send_to_js('finishProcessing', completed_count)
            return

        with self.download_semaphore:
            # Check again after acquiring semaphore — cancel may have happened while queued
            if video_id in self.cancelled_ids:
                self.active_downloads.pop(video_id, None)
                self._send_to_js('updateItemStatus', video_id, 'Cancelled', playlist_id)
                if len(self.active_downloads) == 0:
                    completed_count = len(self.session_completed_ids)
                    self.session_completed_ids.clear()
                    self._send_to_js('finishProcessing', completed_count)
                return

            # 1. Handle Playlist Folder -> Video Folder logic
            base_path = self.download_folder
            if video_data.get('isFromPlaylist'):
                p_title = re.sub(r'[\\/*?:"<>|]', "_", video_data.get('playlistTitle', 'Playlist'))
                base_path = os.path.join(self.download_folder, p_title)
            
            v_title = re.sub(r'[\\/*?:"<>|]', "_", video_data['title'])[:150]
            v_folder = os.path.join(base_path, v_title)
            os.makedirs(v_folder, exist_ok=True)

            self._send_to_js('updateItemStatus', video_id, 'Downloading', playlist_id)
            self.first_tick_seen.discard(video_id)  # reset so the next tick can detect resume

            # Captured by the _hook when yt-dlp reports status='finished'.
            # Stored per-video-id because the hook runs in the worker's thread.
            captured_filepath = {'path': None}

            try:
                opts = self._get_ydl_opts(mode, val)
                quality = video_data.get('selectedQuality', '1080p')
                opts.update({
                    'outtmpl': os.path.join(v_folder, f'{v_title}.%(ext)s'),
                    'progress_hooks': [lambda d: self._hook(d, video_id, playlist_id, captured_filepath)],
                    'postprocessor_hooks': [lambda d: self._pp_hook(d, captured_filepath)],
                    'writethumbnail': video_data.get('downloadThumbnail', False),
                    # NOTE: subtitle download is deliberately NOT part of the main yt-dlp call.
                    # When subtitles fail (network blip, YouTube blocking the sub endpoint, etc.)
                    # yt-dlp raises DownloadError which kills the whole video download — even
                    # though the actual video succeeded. We isolate subs into a separate post-
                    # download step below, wrapped in try/except, so subtitle failures are soft.
                })
                # Quality selection logic. The format string is layered so we ALWAYS
                # land on a file the Chromium <video> tag can decode in-app:
                #   1. avc1 (H.264) video + m4a (AAC) audio in MP4  — universally web-compatible
                #   2. fallback: any MP4-in-MP4 combo                 — usually still avc1
                #   3. fallback: best progressive [ext=mp4]            — last resort
                #   4. fallback: 'best'                                — yt-dlp picks whatever it can
                # merge_output_format=mp4 forces the final container to MP4 even when
                # yt-dlp's defaults would have picked MKV, and the Remuxer postprocessor
                # rewraps anything that lands in a non-MP4 container into MP4 with -c copy.
                # Net effect: every new download is something the in-app player can play
                # without needing the "Open in VLC" fallback.
                if "Audio" in quality:
                    opts['format'] = 'bestaudio/best'
                    opts['postprocessors'] = [{'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '192'}]
                else:
                    h = quality.replace('p','')
                    opts['format'] = (
                        f'bestvideo[height<={h}][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/'
                        f'bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/'
                        f'best[height<={h}][ext=mp4]/'
                        f'best[height<={h}]'
                    )
                    opts['merge_output_format'] = 'mp4'
                    # Final-stage remux to MP4 with -c copy — instant container change
                    # for anything that slipped past the format selector (e.g. webm
                    # progressive stream as last-resort fallback). Existing
                    # postprocessors list (audio extract) only applies to the audio
                    # branch above, so this assignment is safe.
                    opts['postprocessors'] = [{'key': 'FFmpegVideoRemuxer', 'preferedformat': 'mp4'}]

                with YoutubeDL(opts) as ydl: ydl.download([video_data['url']])

                # Resolve final filepath. After post-processing the merged file may have
                # a different name than what hooks reported. Be thorough: prefer existing
                # merged file (.mp4/.mkv) in the folder over intermediate .m4a / .f140.* files.
                final_path = captured_filepath.get('path')
                if final_path and not os.path.exists(final_path):
                    # Post-processor changed the extension — find whatever's there now
                    base = os.path.splitext(os.path.basename(final_path))[0]
                    for fname in os.listdir(v_folder):
                        if fname.startswith(base) and not fname.endswith('.part'):
                            final_path = os.path.join(v_folder, fname)
                            break

                # Final sanity: if the captured path is an intermediate stream file
                # (.f###.m4a, .f###.webm, etc.), scan the folder for the merged output.
                if final_path and self._looks_intermediate(final_path):
                    merged = self._find_merged_output(v_folder, v_title)
                    if merged:
                        final_path = merged

                # Enrich video_data with final state for library persistence
                video_data['status'] = 'Done'
                if final_path:
                    video_data['filepath'] = final_path
                if v_folder:
                    video_data['folderpath'] = v_folder

                # Subtitle fetch — runs as a separate, NON-FATAL yt-dlp call after the main
                # video download succeeds. Isolating it like this means that if the YouTube
                # subtitle endpoint times out, returns 403, or is unreachable (curl: (7)
                # Failed to connect…) the user still gets the video. Without this split, a
                # single subtitle failure was killing the entire download with an ERROR badge.
                if v_folder and final_path:
                    base_name = os.path.splitext(os.path.basename(final_path))[0]
                    try:
                        sub_opts = self._get_ydl_opts(mode, val)
                        sub_opts.update({
                            'outtmpl': os.path.join(v_folder, f'{v_title}.%(ext)s'),
                            'skip_download': True,
                            'writesubtitles': True,
                            'writeautomaticsub': True,
                            'subtitleslangs': ['en.*', '-live_chat'],
                            'subtitlesformat': 'vtt',
                            'quiet': True,
                            'no_warnings': True,
                            # Don't surface "no subtitles available" or similar as errors;
                            # we already gracefully handle the absence on the frontend.
                            'ignoreerrors': True,
                        })
                        with YoutubeDL(sub_opts) as ydl:
                            ydl.download([video_data['url']])
                    except Exception as sub_err:
                        # Network blip, YouTube blocking, no subs available — all soft.
                        # Log and move on; the video is already saved.
                        print(f'[ProTube] subtitle fetch skipped for {video_id}: {sub_err}')

                    # Stamp the subtitle path on video_data so the library entry knows
                    # where the .vtt lives. Scans the folder in case sanitization differs.
                    try:
                        for fname in os.listdir(v_folder):
                            if fname.startswith(base_name) and fname.endswith('.vtt'):
                                video_data['subtitle_path'] = os.path.join(v_folder, fname)
                                break
                    except OSError:
                        pass

                # If this is a standalone video (not from a playlist), move it to library now.
                # Playlist children are handled on the frontend side — frontend calls
                # add_playlist_to_library when the last child completes, so the whole playlist
                # moves as one entry preserving its organization.
                if not playlist_id:
                    # Cache thumbnail locally so library works offline immediately.
                    remote_thumb = video_data.get('thumbnail')
                    if remote_thumb and remote_thumb.startswith('http'):
                        cached = self._cache_thumbnail(remote_thumb, video_id)
                        video_data['thumbnail'] = cached
                    self.add_to_library(video_data)

                self._send_to_js('updateItemStatus', video_id, 'Done', playlist_id, final_path, v_folder)
                self.session_completed_ids.add(video_id)
            except Exception as e:
                if video_id in self.cancelled_ids:
                    self._send_to_js('updateItemStatus', video_id, 'Cancelled', playlist_id)
                elif video_id in self.paused_ids:
                    self._send_to_js('updateItemStatus', video_id, 'Paused', playlist_id)
                else:
                    # Classify error and maybe auto-retry once for network issues
                    raw_msg = str(e)
                    category, friendly = self._classify_error(raw_msg)

                    is_retry = video_data.get('_isAutoRetry', False)
                    if category == 'network' and not is_retry:
                        # Auto-retry exactly once for transient network issues. Signal UI so
                        # the card shows a 'Retrying…' state instead of flashing to Error.
                        self._send_to_js('showRetryToast', video_data.get('title', 'Download'))
                        self._send_to_js('updateItemStatus', video_id, 'Retrying', playlist_id)
                        time.sleep(3)  # brief delay before retry
                        # Only retry if not cancelled/paused during the wait
                        if video_id not in self.cancelled_ids and video_id not in self.paused_ids:
                            video_data['_isAutoRetry'] = True
                            # Hand off to a fresh worker thread. The finally block below would
                            # normally clean up self.active_downloads[video_id], but that entry
                            # now refers to the retry thread — so set a flag to skip the cleanup.
                            auto_retry_handoff = True
                            t = threading.Thread(target=self._download_worker, args=(video_data, mode, val), daemon=True)
                            self.active_downloads[video_id] = t
                            t.start()
                            # Bail out of the except block; finally still runs but respects the flag.
                            return
                    # Send error with category and message so the UI can render a useful badge + tooltip
                    self._send_to_js('updateItemStatus', video_id, 'Error', playlist_id,
                                     None, v_folder, category, friendly)
            finally:
                # Skip cleanup if we handed off to an auto-retry thread (it now owns the slot)
                if not locals().get('auto_retry_handoff'):
                    self.active_downloads.pop(video_id, None)
                    if len(self.active_downloads) == 0:
                        # Batch finished. Send count of fresh completions so UI can toast
                        # accurately. If count is 0 (all paused/cancelled/errored), UI stays quiet.
                        completed_count = len(self.session_completed_ids)
                        self.session_completed_ids.clear()
                        self._send_to_js('finishProcessing', completed_count)

    def _classify_error(self, msg):
        """Classify a yt-dlp / download exception into a (category, friendly_message) tuple.
        Categories: network, geo, rate_limit, unavailable, age_restricted, format, disk, stale_resume, generic."""
        m = (msg or '').lower()

        # HTTP 416 — stale resume. The .part file is out of sync with what's on YouTube's side.
        # User-facing fix: clear the .part and retry from scratch.
        if '416' in m or 'range not satisfiable' in m:
            return ('stale_resume', "The paused download is out of sync with YouTube. Retry will start from scratch.")

        # Network / transport-level issues (auto-retry candidates)
        network_markers = [
            'timed out', 'timeout', 'connection reset', 'connection aborted',
            'connection refused', 'connection broken', 'network is unreachable',
            'temporary failure in name resolution', 'name or service not known',
            'nodename nor servname', 'read operation timed out', 'unable to download webpage',
            'incomplete read', 'max retries exceeded', 'remote end closed connection',
            'getaddrinfo failed', 'handshake', 'ssl', 'tls',
        ]
        if any(x in m for x in network_markers):
            return ('network', "Network hiccup — connection dropped or timed out.")

        # Rate limiting
        if '429' in m or 'too many requests' in m or 'rate limit' in m:
            return ('rate_limit', "YouTube is rate-limiting us. Wait a minute and try again.")

        # Geo-blocked
        if 'not available in your country' in m or 'geo' in m or 'geoblock' in m:
            return ('geo', "This video isn't available in your region.")

        # Age restricted / sign-in required
        if 'sign in to confirm your age' in m or 'age-restricted' in m or 'age restricted' in m:
            return ('age_restricted', "Video is age-restricted — sign-in required (not currently supported).")

        # Video gone
        unavailable_markers = [
            'video unavailable', 'this video is unavailable', 'this video has been removed',
            'private video', 'members-only', 'member-only',
            'video is not available', 'removed by the uploader', 'terminated'
        ]
        if any(x in m for x in unavailable_markers):
            return ('unavailable', "Video is unavailable — private, deleted, or members-only.")

        # Forbidden (often age-gated or restricted, sometimes real 403)
        if '403' in m or 'forbidden' in m:
            return ('unavailable', "YouTube refused the request (403). Video may be gated or taken down.")

        # Format unavailable for the requested quality
        if 'requested format is not available' in m or 'requested format' in m:
            return ('format', "Requested quality isn't available. Try a different one.")

        # Disk / filesystem
        if 'no space left' in m or 'disk full' in m or 'not enough' in m and 'space' in m:
            return ('disk', "Not enough disk space to save the file.")
        if 'permission denied' in m or 'access is denied' in m:
            return ('disk', "Couldn't write to disk — permission denied on the target folder.")

        # Fallback: return a trimmed version of the raw message so it's still actionable
        trimmed = msg.strip().split('\n')[0]
        if len(trimmed) > 160:
            trimmed = trimmed[:157] + '…'
        return ('generic', trimmed or "Download failed for an unknown reason.")

    def restart_download(self, video_data, mode='browser', val='none', force_restart=False):
        """User-triggered retry of a single failed video. Clears any stale error state and
        spawns a fresh worker thread — same codepath as an initial download.
        If force_restart=True, wipe any .part files first (needed for 416/stale-resume errors)."""
        vid = video_data.get('id')
        if not vid:
            return False
        # Clear any lingering cancelled/paused flags so the worker doesn't bail out immediately
        self.cancelled_ids.discard(vid)
        self.paused_ids.discard(vid)
        # Don't restart if already running
        if vid in self.active_downloads:
            return False
        # If force-restart requested (416 / stale resume), clear .part files in the video's folder.
        # This is scoped to the target folder only — we don't touch unrelated files.
        if force_restart:
            try:
                base_path = self.download_folder
                if video_data.get('isFromPlaylist'):
                    p_title = re.sub(r'[\\/*?:"<>|]', "_", video_data.get('playlistTitle', 'Playlist'))
                    base_path = os.path.join(self.download_folder, p_title)
                v_title = re.sub(r'[\\/*?:"<>|]', "_", video_data['title'])[:150]
                v_folder = os.path.join(base_path, v_title)
                if os.path.exists(v_folder):
                    for fname in os.listdir(v_folder):
                        if fname.endswith('.part') or fname.endswith('.ytdl'):
                            try:
                                os.remove(os.path.join(v_folder, fname))
                            except Exception:
                                pass
            except Exception:
                pass  # Best-effort cleanup; don't block the retry if this fails
        # Strip any auto-retry flag so the retry gets its own auto-retry budget
        video_data.pop('_isAutoRetry', None)
        t = threading.Thread(target=self._download_worker, args=(video_data, mode, val), daemon=True)
        self.active_downloads[vid] = t
        t.start()
        return True

    def _pp_hook(self, d, captured_filepath):
        """Postprocessor hook — fires during merge/extract. When yt-dlp finishes merging
        video+audio into the final .mp4, d['info_dict']['filepath'] or d['filename'] has
        the merged path. This is more authoritative than progress_hooks' last filename."""
        if not captured_filepath:
            return
        status = d.get('status')
        if status in ('finished', 'processing'):
            # Try several keys yt-dlp might use
            fn = (d.get('filename')
                  or (d.get('info_dict') or {}).get('filepath')
                  or (d.get('info_dict') or {}).get('_filename')
                  or '')
            if fn and fn.lower().endswith(('.mp4', '.mkv', '.webm', '.mp3', '.m4a')):
                # Prefer merged containers. If what we already have is an intermediate
                # stream (e.g. .f140.m4a), overwrite it.
                current = captured_filepath.get('path') or ''
                if not current or self._looks_intermediate(current) or fn.lower().endswith(('.mp4', '.mkv')):
                    captured_filepath['path'] = fn

    def _looks_intermediate(self, filepath):
        """True if a path looks like a yt-dlp intermediate stream file (e.g. '.f140.m4a').
        These are per-stream downloads that exist before merge — they're not playable
        videos by themselves in most players."""
        if not filepath:
            return False
        basename = os.path.basename(filepath).lower()
        # Pattern: anything.fNNN.ext where NNN is a yt-dlp format code
        import re as _re
        return bool(_re.search(r'\.f\d+\.(m4a|webm|mp4)$', basename))

    def _find_merged_output(self, folder, title):
        """Given a folder and a video title, look for the merged output file.
        Prefers mp4/mkv over intermediate stream files."""
        if not os.path.isdir(folder):
            return None
        # Sanitize title the same way yt-dlp does (rough approximation)
        # Look for any file matching title.{mp4,mkv,webm} that's NOT intermediate
        candidates = []
        try:
            for fname in os.listdir(folder):
                full = os.path.join(folder, fname)
                if not os.path.isfile(full):
                    continue
                if self._looks_intermediate(full):
                    continue
                if fname.endswith('.part'):
                    continue
                if fname.lower().endswith(('.mp4', '.mkv', '.webm')):
                    candidates.append(full)
        except OSError:
            return None
        if not candidates:
            return None
        # Prefer the largest file (merged output is usually bigger than leftover streams)
        candidates.sort(key=lambda p: os.path.getsize(p) if os.path.exists(p) else 0, reverse=True)
        return candidates[0]

    def _hook(self, d, vid, playlist_id=None, captured_filepath=None):
        if vid in self.cancelled_ids or vid in self.paused_ids:
            raise Exception("Cancelled" if vid in self.cancelled_ids else "Paused")

        # Capture final path when yt-dlp reports a file is done. yt-dlp fires 'finished'
        # for each stream (video, then audio), then the postprocessor merges them. We
        # prefer the merged .mp4 when it appears, but fall back to whatever was last
        # finished if no postprocessor runs (e.g. audio-only formats).
        if d.get('status') == 'finished' and captured_filepath is not None:
            fn = d.get('filename') or ''
            current = captured_filepath.get('path') or ''
            # Don't overwrite a merged container with an intermediate stream
            current_is_good = current and not self._looks_intermediate(current) and current.lower().endswith(('.mp4', '.mkv', '.webm'))
            if not current_is_good:
                captured_filepath['path'] = fn

        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            if total > 0:
                downloaded_bytes = d.get('downloaded_bytes', 0)
                pct = (downloaded_bytes / total) * 100
                speed_bytes = d.get('speed') or 0
                speed_str = self._format_bytes(d.get('speed')) + "/s"

                # Detect resume-from-partial on the first progress tick.
                # If we've got >3% already downloaded, yt-dlp picked up from a .part file.
                if vid not in self.first_tick_seen:
                    self.first_tick_seen.add(vid)
                    if pct > 3:
                        filename = d.get('filename', '')
                        title = os.path.basename(filename).rsplit('.', 1)[0] if filename else 'a previous download'
                        # Truncate long titles for toast
                        if len(title) > 40:
                            title = title[:37] + '...'
                        self._send_to_js('showResumeToast', title, round(pct))

                # Remember last progress so get_active_progress() can resync the UI
                # after a window refocus (when Chromium throttling lifts).
                if not hasattr(self, '_last_progress'):
                    self._last_progress = {}
                self._last_progress[vid] = {
                    'pct': pct,
                    'speed': speed_str,
                    'playlist_id': playlist_id
                }

                self._send_to_js('updateItemProgress', vid, pct, speed_str,
                                 playlist_id, downloaded_bytes, total, speed_bytes)

    def _parse_formats(self, info):
        """Build the quality-picker list from yt-dlp's per-format metadata.

        IMPORTANT: prefer `format_note` (e.g. "2160p", "1440p60", "1080p") over deriving
        a label from `height`. For 16:9 video they're the same, but for anamorphic /
        ultrawide / 2:1 (e.g. some VR / cinema-aspect uploads) the `height` field reports
        the actual pixel rows (e.g. 1920 for a 3840×1920 video) which we'd previously
        render as "1920p" — leaving the user to wonder why their 4K video doesn't show
        a 4K option. format_note carries the standard quality label YouTube assigns.

        Falls back to `f'{height}p'` only when format_note is missing.
        """
        formats = []; size_map = {}
        for f in info.get('formats', []):
            if f.get('vcodec') == 'none':
                continue
            h = f.get('height')
            if not h:
                continue
            # Strip trailing fps suffix like "1080p60" → "1080p" so the label matches the
            # picker options and "selectedQuality" string comparison still works.
            raw_note = (f.get('format_note') or '').strip()
            label = ''
            if raw_note:
                # Take the leading "<digits>p" run — "1080p60 HDR" → "1080p"
                import re
                m = re.match(r'^(\d{2,4}p)', raw_note)
                if m:
                    label = m.group(1)
            if not label:
                label = f'{h}p'
            if label not in size_map:
                sz = f.get('filesize') or f.get('filesize_approx', 0)
                size_map[label] = sz
                formats.append({'label': label, 'filesize_string': self._format_bytes(sz)})
        return sorted(formats, key=lambda x: int(x['label'][:-1]), reverse=True), size_map

    def _format_bytes(self, b):
        if not b: return "0B"
        for u in ['B','KB','MB','GB']:
            if b < 1024: return f"{b:.1f}{u}"
            b /= 1024
        return f"{b:.1f}TB"

    def _format_duration(self, d):
        if not d: return "0:00"
        m, s = divmod(int(d), 60); h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def _load_settings(self):
        """Load settings.json. Resilient to corruption / empty files — returns {} rather than
        crashing the app. If the file exists but is malformed, attempt recovery by
        trimming trailing garbage (the most common failure mode is one or two stray
        bytes from a partial write or a sync/AV write hook). Only if recovery also
        fails do we back up as settings.json.corrupt and start fresh."""
        if not os.path.exists(self.settings_file):
            return {}
        try:
            with open(self.settings_file, 'r', encoding='utf-8') as f:
                content = f.read()
            if not content.strip():
                # Empty file — treat as fresh start
                return {}
            return json.loads(content)
        except json.JSONDecodeError as e:
            # The most common corruption pattern is a trailing byte (extra '}', '\\0',
            # or whitespace) from a partial write — the JSON proper is intact, just
            # has garbage glued to the end. Try to scan for the first complete top-
            # level object and re-parse. Recovers ALL the user's library/queue data
            # instead of dropping them into a fresh install.
            try:
                recovered = self._try_recover_truncated_json(content)
                if recovered is not None:
                    print(f'[ProTube] settings.json had trailing garbage; recovered cleanly. Error was: {e}')
                    return recovered
            except Exception:
                pass
            # Recovery failed — back up the file rather than silently lose the data.
            try:
                backup = self.settings_file + '.corrupt'
                if os.path.exists(backup):
                    backup = self.settings_file + f'.corrupt.{int(time.time())}'
                os.rename(self.settings_file, backup)
                print(f'[ProTube] settings.json was corrupt; backed up to {backup}. Error: {e}')
            except OSError:
                pass
            return {}
        except OSError as e:
            print(f'[ProTube] settings.json unreadable: {e}')
            return {}

    def _try_recover_truncated_json(self, content):
        """Scan for the first complete top-level JSON object and try to parse it.
        Returns the parsed dict on success, None on failure. Used by _load_settings
        when the raw file fails json.loads — covers the common partial-write case
        where the JSON is intact but a stray byte (extra '}' / null / newline) got
        appended after the final closing brace."""
        depth = 0
        in_str = False
        escape = False
        for i, ch in enumerate(content):
            if escape:
                escape = False
                continue
            if in_str:
                if ch == '\\':
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    candidate = content[:i + 1]
                    return json.loads(candidate)
        return None

    def _save_settings(self):
        """Atomic write: dump to a temp file, then rename. On any OS, the rename is atomic
        within the same directory — so readers never see a half-written file. If the write
        fails mid-way, the original settings.json stays intact.

        When inside a `_deferred_save()` context, the actual write is skipped
        and the dirty flag is set instead — the context manager flushes once
        on exit. Used to coalesce the 4 writes/track on the album download
        hot path (library add + album bump + cover ensure + queue patch) down
        to a single write per track."""
        if getattr(self, '_save_deferred', False):
            self._save_dirty = True
            return
        tmp = self.settings_file + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.settings, f)
                f.flush()
                os.fsync(f.fileno())
            # Atomic replace on Windows + POSIX
            os.replace(tmp, self.settings_file)
        except OSError as e:
            # Clean up the temp file on failure; leave original settings.json untouched
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            print(f'[ProTube] settings save failed: {e}')

    @contextlib.contextmanager
    def _deferred_save(self):
        """Coalesce multiple _save_settings calls into a single write at
        context exit. Re-entrant — nested deferrals flush only at the outer-
        most exit. Use on hot paths that mutate settings several times in
        succession (album download finalize, batch repair, etc.)."""
        if getattr(self, '_save_deferred', False):
            # Already deferring (nested) — just yield, outer scope flushes.
            yield
            return
        self._save_deferred = True
        self._save_dirty = False
        try:
            yield
        finally:
            self._save_deferred = False
            if self._save_dirty:
                self._save_dirty = False
                self._save_settings()
    def on_dom_ready(self): pass
    def choose_folder(self):
        result = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
        if result and len(result) > 0:
            self.download_folder = result[0]
            self.settings['download_folder'] = self.download_folder
            self._save_settings()
            return self.download_folder
        return self.download_folder
    
    def get_download_folder(self): return self.download_folder

    def heartbeat(self):
        """No-op called by the JS heartbeat ticker every 30s. Belt-and-suspenders
        with the IntensiveWakeUpThrottling Chromium flags in main.py — even if
        a future WebView2 build ignores the flags, the regular API traffic from
        this method counts as 'work' and keeps the renderer's event loop active.
        Without it, Chromium foreground-idle throttling kicks in after ~5 min of
        no input and the whole UI freezes until the user clicks. Returns nothing
        the JS side cares about; the call itself is the entire point."""
        return {'ok': True}

    def reset_onboarding(self):
        """Clear the _onboarded flag so the welcome modal shows on next launch.
        Useful for re-triggering onboarding while testing."""
        self.settings.pop('_onboarded', None)
        self._save_settings()
        return {'ok': True}
    def save_queue(self, q): self.settings['queue'] = q; self._save_settings()
    def load_queue(self): return self.settings.get('queue', [])

    def get_setting(self, key):
        """Generic settings read for frontend use (feature flags, one-time migration markers)."""
        return self.settings.get(key)

    def get_subtitles_for_video(self, video_id):
        """Read the subtitle .vtt file for a library video and return its raw text.

        Looks up the library entry by id (top-level OR playlist child), falls back to
        scanning the video's folder if the stored subtitle_path is missing or stale (file
        moved / deleted). Returns {'vtt': '<text>'} on success, {'error': '...'} otherwise.
        Frontend parses the VTT itself and renders cues in a custom overlay so we get
        our fonts instead of the native <track> rendering.
        """
        try:
            # Library entries live under 'library' (not 'videos' — that key doesn't exist).
            # The bug here was returning 'video not in library' for every video, so the
            # frontend silently dimmed the CC button as if no subs existed.
            entry = None
            for v in self.settings.get('library', []):
                if v.get('type') == 'playlist':
                    for c in (v.get('videos') or []):
                        if c.get('id') == video_id:
                            entry = c
                            break
                    if entry:
                        break
                elif v.get('id') == video_id:
                    entry = v
                    break
            if not entry:
                return {'error': 'video not in library'}

            sub_path = entry.get('subtitle_path')
            # Fallback: stored path missing or file gone — scan the video's folder for a .vtt.
            # Two passes: first try a name match (Title.en.vtt next to Title.mp4) so we pick
                # the right file when a folder somehow has multiple videos. If that misses, take
                # any .vtt in the folder — yt-dlp can sanitize titles slightly differently for
                # video vs subtitle filenames in edge cases (Unicode, trailing dots, etc.) and
                # ProTube downloads each video into its own subfolder, so any .vtt here is ours.
            if (not sub_path or not os.path.exists(sub_path)) and entry.get('filepath'):
                folder = os.path.dirname(entry['filepath'])
                base = os.path.splitext(os.path.basename(entry['filepath']))[0]
                if os.path.isdir(folder):
                    matched = None
                    fallback_any = None
                    for fname in os.listdir(folder):
                        if not fname.endswith('.vtt'):
                            continue
                        if fname.startswith(base):
                            matched = fname
                            break
                        if fallback_any is None:
                            fallback_any = fname
                    pick = matched or fallback_any
                    if pick:
                        sub_path = os.path.join(folder, pick)
                        entry['subtitle_path'] = sub_path
                        self._save_settings()

            if not sub_path or not os.path.exists(sub_path):
                return {'error': 'no subtitles available'}

            with open(sub_path, 'r', encoding='utf-8', errors='replace') as f:
                return {'vtt': f.read()}
        except Exception as e:
            return {'error': f'failed to load subtitles: {e}'}

    def _find_library_entry(self, video_id):
        """Walk the library (top-level + playlist children) and return the matching entry,
        or None. Shared helper for AI features that need to read/write per-video metadata."""
        for v in self.settings.get('library', []):
            if v.get('type') == 'playlist':
                for c in (v.get('videos') or []):
                    if c.get('id') == video_id:
                        return c
            elif v.get('id') == video_id:
                return v
        return None

    def _vtt_to_plain_text(self, vtt_text):
        """Strip VTT timestamps and tags, return continuous prose for LLM input."""
        import re
        out_lines = []
        for line in vtt_text.replace('\r\n', '\n').split('\n'):
            s = line.strip()
            if not s or s.startswith('WEBVTT') or s.startswith('NOTE'):
                continue
            if '-->' in s:
                continue  # timestamp line
            if re.match(r'^\d+$', s):
                continue  # cue identifier (a number on its own line)
            # Strip inline tags + word-level timestamp markers
            s = re.sub(r'<\d{2}:\d{2}:\d{2}\.\d{3}>', '', s)
            s = re.sub(r'<\/?[^>]+>', '', s)
            s = s.strip()
            if s:
                out_lines.append(s)
        # Collapse adjacent duplicates (auto-captions repeat lines as the speaker
        # continues — we don't want each fragment counted multiple times in the prompt).
        deduped = []
        for line in out_lines:
            if not deduped or deduped[-1] != line:
                deduped.append(line)
        return ' '.join(deduped)

    def polish_subtitles_with_ai(self, video_id):
        """F8 — pass the video's .vtt through Groq for caption cleanup (punctuation,
        homophones, duplicate words) and cache the cleaned VTT next to the original.
        Frontend swaps the displayed cues to the cleaned set on success."""
        try:
            raw = self.get_subtitles_for_video(video_id)
            if 'error' in raw:
                return raw

            entry = self._find_library_entry(video_id)
            if not entry:
                return {'error': 'video not in library'}

            sub_path = entry.get('subtitle_path')
            cleaned_path = None
            if sub_path:
                cleaned_path = os.path.splitext(sub_path)[0] + '.cleaned.vtt'
                # Cache hit — skip the Groq call entirely.
                if os.path.exists(cleaned_path):
                    with open(cleaned_path, 'r', encoding='utf-8', errors='replace') as f:
                        return {'vtt': f.read(), 'cached': True}

            api_key = self.settings.get('groq_api_key', '').strip()
            if not api_key:
                return {'error': 'No Groq API key. Add one in Settings → AI.'}

            from groq_client import GroqClient, GroqError
            client = GroqClient(api_key)

            system = (
                "You fix YouTube auto-generated subtitles. The user will give you a WebVTT file. "
                "Return the SAME WebVTT file with these fixes:\n"
                "1. Add proper sentence punctuation (periods, commas, question marks).\n"
                "2. Capitalize the start of sentences and proper nouns.\n"
                "3. Fix obvious homophone errors (their/there/they're, your/you're, etc.) when context makes the right one obvious.\n"
                "4. Remove immediate duplicate words (\"the the\" -> \"the\").\n"
                "RULES:\n"
                "- Do NOT paraphrase or change meaning.\n"
                "- Do NOT change the cue count or merge cues.\n"
                "- Do NOT change the timestamps.\n"
                "- Do NOT remove the WEBVTT header.\n"
                "- Output ONLY the cleaned WebVTT file content, nothing before or after it."
            )

            try:
                cleaned = client.chat(system, raw['vtt'], max_tokens=8000, temperature=0.2)
            except GroqError as e:
                return {'error': str(e)}

            # Trim wrapping code fences if the model added them despite instructions.
            cleaned = cleaned.strip()
            if cleaned.startswith('```'):
                cleaned = cleaned.split('\n', 1)[1] if '\n' in cleaned else cleaned
                if cleaned.endswith('```'):
                    cleaned = cleaned.rsplit('```', 1)[0]
                cleaned = cleaned.strip()

            # Sanity: response must look like VTT (contains WEBVTT and at least one '-->' line).
            if 'WEBVTT' not in cleaned or '-->' not in cleaned:
                return {'error': 'AI response was not valid VTT — keeping original.'}

            if cleaned_path:
                try:
                    with open(cleaned_path, 'w', encoding='utf-8') as f:
                        f.write(cleaned)
                except OSError as e:
                    print(f'[ProTube] failed to cache cleaned VTT: {e}')

            return {'vtt': cleaned, 'cached': False}
        except Exception as e:
            return {'error': f'polish failed: {e}'}

    def generate_video_summary(self, video_id):
        """F7 — generate a 3-5 paragraph article-style summary from the video's subtitles
        using Groq. Result is cached on the library entry as `ai_summary` so subsequent
        opens are instant."""
        try:
            entry = self._find_library_entry(video_id)
            if not entry:
                return {'error': 'video not in library'}

            # Cache hit
            if entry.get('ai_summary'):
                return {'summary': entry['ai_summary'], 'cached': True}

            raw = self.get_subtitles_for_video(video_id)
            if 'error' in raw:
                return {'error': 'Need subtitles to generate a summary. Re-download to fetch them.'}

            api_key = self.settings.get('groq_api_key', '').strip()
            if not api_key:
                return {'error': 'No Groq API key. Add one in Settings → AI.'}

            transcript = self._vtt_to_plain_text(raw['vtt'])
            if not transcript or len(transcript) < 60:
                return {'error': 'Transcript too short to summarize.'}

            # Llama 3.3 70B has a 32k token context. Chars-to-tokens roughly 4:1, so
            # cap input at ~80k chars to leave room for prompt + output. Most videos
            # under ~3 hours fit. Longer ones get truncated to the first 80k chars.
            transcript = transcript[:80000]
            title = entry.get('title') or 'YouTube video'
            uploader = entry.get('uploader') or ''

            from groq_client import GroqClient, GroqError
            client = GroqClient(api_key)

            system = (
                "You are writing a high-signal summary of a YouTube video for someone deciding "
                "whether to watch it. Density matters. Specifics matter. Generic statements are "
                "a failure mode — every bullet should have something a reader couldn't have "
                "guessed from the title alone.\n\n"
                "## OUTPUT FORMAT (strict markdown)\n\n"
                "OPENING: 1-2 sentences. Name the creator (if mentioned), name the topic, name "
                "the angle/promise. Examples of GOOD openings:\n"
                "- \"Mert Yerlikaya breaks down the exact 3-year path to a $10M AI agency exit, "
                "drawing on his own $600k/year shop.\"\n"
                "- \"Andrej Karpathy walks through how he uses Cursor and Claude to ship side "
                "projects in a weekend, with concrete examples from his recent micro-app.\"\n\n"
                "Then a `## What it covers` section with 3-6 bullets. Each bullet captures a "
                "specific topic IN THE ORDER the video covers it. Lead with the topic, then a "
                "concrete detail (a number, name, example) that anchors what's actually said.\n\n"
                "Then a `## Key points` section with 3-6 bullets. These are the CLAIMS the "
                "creator makes — what they argue, recommend, or warn against. Each must be "
                "specific enough that a reader could quote it back. Lean on **bold** to pull "
                "out the most quotable phrase in each bullet.\n\n"
                "End with `## Bottom line` — ONE punchy sentence. Not two. The thesis of the "
                "video distilled to a tweetable line.\n\n"
                "## QUALITY BAR\n\n"
                "Re-read every bullet you write and ask: \"could a reader have written this "
                "without watching the video?\" If yes, the bullet is bad — replace it with "
                "something specific (a number, a name, a counterintuitive claim, a step in "
                "a process). EVERY summary should contain at least 4 specific numbers, names, "
                "or proper nouns drawn from the video. Bullets without specifics are forbidden.\n\n"
                "## STYLE\n\n"
                "- Keep bullets SHORT — one sentence, ideally under 25 words. Cut filler words.\n"
                "- Use **bold** sparingly: 1 bold phrase per Key Points bullet, max.\n"
                "- Don't introduce sections with sentences like \"Here are the key points:\" — "
                "the heading is the introduction. Just go.\n"
                "- TARGET LENGTH: 250-450 words total. Tighter is better than longer.\n\n"
                "## STRICT DON'TS\n\n"
                "- DO NOT add a title or H1 before the opening.\n"
                "- DO NOT mention transcripts or subtitles.\n"
                "- DO NOT invent details not in the transcript. If the transcript is sparse, "
                "the summary should be sparse too — never pad.\n"
                "- DO NOT close with meta-commentary.\n"
                "- DO NOT wrap output in code fences or quotes.\n"
                "- Output ONLY the markdown. Nothing before. Nothing after."
            )
            user_msg = (
                f"Title: {title}\n"
                f"Channel: {uploader}\n\n"
                f"Transcript:\n{transcript}"
            )

            try:
                summary = client.chat(system, user_msg, max_tokens=1500, temperature=0.5)
            except GroqError as e:
                return {'error': str(e)}

            summary = summary.strip()
            if not summary:
                return {'error': 'AI returned an empty summary.'}

            entry['ai_summary'] = summary
            self._save_settings()
            return {'summary': summary, 'cached': False}
        except Exception as e:
            return {'error': f'summary failed: {e}'}

    def clear_video_ai_summary(self, video_id):
        """Drop the cached summary so the next call regenerates. Useful if the user
        wants a fresh take or the first response was bad."""
        entry = self._find_library_entry(video_id)
        if not entry:
            return {'ok': False}
        if 'ai_summary' in entry:
            del entry['ai_summary']
            self._save_settings()
        return {'ok': True}

    # ---- YouTube search (F9) ---------------------------------------------------------
    # Hits YouTube's internal "Innertube" API directly — the same JSON endpoint
    # the YouTube website itself uses. Way faster than yt-dlp's ytsearch (which has
    # to fetch + parse the full HTML search page server-side). No API key needed,
    # no quota. Tradeoff: unofficial, so YouTube could change the schema — but it's
    # the API powering their own website, so they have strong incentive to keep it
    # stable. We added it 2026-05 after yt-dlp's ytsearch was taking 30-75s on the
    # user's network. Innertube on the same network: 1-3s.

    _INNERTUBE_URL = 'https://www.youtube.com/youtubei/v1/search'
    _INNERTUBE_CLIENT = {
        'clientName': 'WEB',
        'clientVersion': '2.20240101.00.00',
        'hl': 'en',
        'gl': 'US',
    }
    # Base64-encoded protobuf filter params (the strings YouTube uses in its own
    # "Filters" dropdown). These restrict results to a single type — without them
    # results are mixed.
    _INNERTUBE_FILTER_PARAMS = {
        'videos':    'EgIQAQ%3D%3D',
        'channels':  'EgIQAg%3D%3D',
        'playlists': 'EgIQAw%3D%3D',
    }
    _INNERTUBE_SESSION = None  # lazy, persistent for connection reuse

    # YouTube's unofficial suggestion endpoint. Same one the YouTube search box itself
    # uses. No API key, no quota. Stable for years.
    _YT_SUGGEST_URL = 'https://suggestqueries.google.com/complete/search'

    # YouTube MUSIC Innertube — same protocol as regular YT but the WEB_REMIX client
    # gets music-shaped responses (Songs / Albums / Artists / Playlists) with proper
    # track/artist/album metadata instead of generic videoRenderer blobs.
    _MUSIC_INNERTUBE_URL = 'https://music.youtube.com/youtubei/v1/search'
    _MUSIC_INNERTUBE_CLIENT = {
        'clientName': 'WEB_REMIX',
        'clientVersion': '1.20240101.01.00',
        'hl': 'en',
        'gl': 'US',
    }
    # Filter params (base64-encoded protobuf) for YT Music's category tabs.
    _MUSIC_INNERTUBE_FILTER_PARAMS = {
        'songs':     'EgWKAQIIAWoKEAkQBRADEAoQBA%3D%3D',
        'videos':    'EgWKAQIQAWoKEAkQAxAEEAUQCg%3D%3D',
        'albums':    'EgWKAQIYAWoKEAkQChAFEAMQBA%3D%3D',
        'artists':   'EgWKAQIgAWoKEAkQBRAKEAMQBA%3D%3D',
        'playlists': 'EgWKAQIoAWoKEAkQAxAEEAUQCQ%3D%3D',
    }
    _MUSIC_INNERTUBE_SESSION = None

    def search_youtube_suggestions(self, query):
        """Return up to 8 search-suggestion strings for a partial query. Powers the
        autocomplete dropdown under the search input."""
        try:
            q = (query or '').strip()
            if not q:
                return {'suggestions': []}
            resp = requests.get(
                self._YT_SUGGEST_URL,
                params={'client': 'firefox', 'ds': 'yt', 'q': q},
                timeout=4,
            )
            if resp.status_code != 200:
                return {'suggestions': []}
            data = resp.json()
            sugg = data[1] if isinstance(data, list) and len(data) > 1 else []
            return {'suggestions': [s for s in sugg[:8] if isinstance(s, str)]}
        except Exception:
            return {'suggestions': []}

    def _get_innertube_session(self):
        """Persistent requests.Session for keep-alive across search calls.
        Without this each call goes through TLS handshake + connection setup."""
        if self._INNERTUBE_SESSION is None:
            sess = requests.Session()
            sess.headers.update({
                'Content-Type': 'application/json',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36',
                'Accept-Language': 'en-US,en;q=0.9',
            })
            # Stash on the class so all instances share the connection pool.
            type(self)._INNERTUBE_SESSION = sess
        return self._INNERTUBE_SESSION

    def _build_dedup_sets(self):
        """Build per-type sets of IDs/URLs from the current queue and library so
        search results can be tagged in_queue / in_library without per-result loops."""
        queue_video_ids = set()
        queue_playlist_ids = set()
        queue_channel_urls = set()
        for q_item in self.settings.get('queue', []):
            if q_item.get('type') == 'playlist':
                pid = q_item.get('id')
                if pid:
                    queue_playlist_ids.add(pid)
                if q_item.get('subtype') == 'channel' and q_item.get('url'):
                    queue_channel_urls.add(self._normalize_channel_url_str(q_item['url']))
                for c in (q_item.get('videos') or []):
                    if c.get('id'):
                        queue_video_ids.add(c['id'])
            elif q_item.get('id'):
                queue_video_ids.add(q_item['id'])
        lib_video_ids = set()
        lib_playlist_ids = set()
        lib_channel_urls = set()
        for l_item in self.settings.get('library', []):
            if l_item.get('type') == 'playlist':
                pid = l_item.get('id')
                if pid:
                    lib_playlist_ids.add(pid)
                if l_item.get('subtype') == 'channel' and l_item.get('url'):
                    lib_channel_urls.add(self._normalize_channel_url_str(l_item['url']))
                for c in (l_item.get('videos') or []):
                    if c.get('id'):
                        lib_video_ids.add(c['id'])
            elif l_item.get('id'):
                lib_video_ids.add(l_item['id'])
        return {
            'queue_video_ids': queue_video_ids,
            'queue_playlist_ids': queue_playlist_ids,
            'queue_channel_urls': queue_channel_urls,
            'lib_video_ids': lib_video_ids,
            'lib_playlist_ids': lib_playlist_ids,
            'lib_channel_urls': lib_channel_urls,
        }

    def _innertube_extract_text(self, node, *keys):
        """Pull a text string out of YouTube's nested {runs:[{text:...}], simpleText:...}
        shapes. Returns the first non-empty value found across the given keys."""
        if not isinstance(node, dict):
            return ''
        for k in keys:
            v = node.get(k)
            if not v:
                continue
            if isinstance(v, str):
                return v
            if isinstance(v, dict):
                if v.get('simpleText'):
                    return v['simpleText']
                runs = v.get('runs') or []
                if runs:
                    return ''.join(r.get('text', '') for r in runs if isinstance(r, dict))
        return ''

    def _innertube_parse_video(self, vr, dedup):
        """Convert a videoRenderer dict into the unified result shape.

        Extracts the same fields YouTube's own search UI shows: title, channel name,
        view count, time-ago, channel avatar, verified-creator badge, and the
        description snippet. The frontend uses these to render YouTube-style cards.
        """
        vid = vr.get('videoId') or ''
        if not vid:
            return None
        title = self._innertube_extract_text(vr, 'title')
        uploader = self._innertube_extract_text(vr, 'ownerText', 'longBylineText', 'shortBylineText')
        duration = self._innertube_extract_text(vr, 'lengthText')
        # Prefer the short ("5.8M views") form over the long ("5,886,105 views") form —
        # matches YouTube's own search UI and reads better on the card.
        view_count = self._innertube_extract_text(vr, 'shortViewCountText', 'viewCountText')
        published_time = self._innertube_extract_text(vr, 'publishedTimeText')

        # Description: "detailedMetadataSnippets" wraps the text in {snippetText: {runs:[]}}.
        description = ''
        dms = vr.get('detailedMetadataSnippets') or []
        if dms:
            description = self._innertube_extract_text(dms[0], 'snippetText')

        # Channel avatar (small circular thumb shown next to channel name).
        channel_thumbnail = ''
        ctsr = (vr.get('channelThumbnailSupportedRenderers') or {})
        ctwlr = ctsr.get('channelThumbnailWithLinkRenderer') or {}
        ct_thumbs = (ctwlr.get('thumbnail') or {}).get('thumbnails') or []
        if ct_thumbs:
            channel_thumbnail = ct_thumbs[-1].get('url') or ''
        if channel_thumbnail.startswith('//'):
            channel_thumbnail = 'https:' + channel_thumbnail

        # Verified-creator badge — YouTube renders a small checkmark next to the channel name.
        channel_verified = False
        for badge in (vr.get('ownerBadges') or []):
            mbr = (badge or {}).get('metadataBadgeRenderer') or {}
            if 'VERIFIED' in str(mbr.get('style') or '').upper():
                channel_verified = True
                break

        thumbs = (vr.get('thumbnail') or {}).get('thumbnails') or []
        thumb = thumbs[-1].get('url') if thumbs else f'https://i.ytimg.com/vi/{vid}/hqdefault.jpg'
        return {
            'id': vid,
            'type': 'video',
            'title': title or 'Untitled',
            'uploader': uploader,
            'url': f'https://www.youtube.com/watch?v={vid}',
            'thumbnail': thumb,
            'duration_string': duration,
            'view_count_string': view_count,
            'published_time': published_time,           # "2 years ago"
            'description': description,                  # short snippet line
            'channel_thumbnail': channel_thumbnail,      # small avatar URL
            'channel_verified': channel_verified,        # bool
            'in_queue': vid in dedup['queue_video_ids'],
            'in_library': vid in dedup['lib_video_ids'],
        }

    def _innertube_parse_channel(self, cr, dedup):
        """Convert a channelRenderer dict into the unified result shape.

        Heads-up on YouTube's misleading field names:
        - `videoCountText.simpleText` actually holds the SUBSCRIBER COUNT ("2.28K subscribers")
        - `subscriberCountText.simpleText` actually holds the @HANDLE ("@PastaWord")
        Yes, they're swapped from what the names suggest. Don't ask me why.
        """
        cid = cr.get('channelId') or ''
        if not cid:
            return None
        title = self._innertube_extract_text(cr, 'title')
        # Channel URL: prefer the canonical @handle path when present (looks cleaner
        # to the user and is what the existing channel-fetch flow expects).
        nav = cr.get('navigationEndpoint') or {}
        browse = nav.get('browseEndpoint') or {}
        canonical = browse.get('canonicalBaseUrl') or ''
        if canonical and canonical.startswith('/'):
            url = f'https://www.youtube.com{canonical}'
        else:
            url = f'https://www.youtube.com/channel/{cid}'
        # Pull subscribers + handle from the (swapped) fields.
        subs = self._innertube_extract_text(cr, 'videoCountText')
        handle = self._innertube_extract_text(cr, 'subscriberCountText')
        # descriptionSnippet — usually has runs of text + bold markers; join and trim.
        desc = self._innertube_extract_text(cr, 'descriptionSnippet')
        thumbs = (cr.get('thumbnail') or {}).get('thumbnails') or []
        thumb = thumbs[-1].get('url') if thumbs else ''
        if thumb.startswith('//'):
            thumb = 'https:' + thumb
        norm = self._normalize_channel_url_str(url)
        # uploader field carries the description for the channel-card subtitle line —
        # frontend already renders this as the secondary text under the title.
        # view_count_string carries "@handle · 2.28K subscribers" so the bottom stats
        # row reads naturally without us needing a new field.
        stats_bits = []
        if handle:
            stats_bits.append(handle)
        if subs:
            stats_bits.append(subs)
        return {
            'id': cid,
            'type': 'channel',
            'title': title or 'Untitled',
            'uploader': desc,
            'url': url,
            'thumbnail': thumb,
            'duration_string': '',
            'view_count_string': ' · '.join(stats_bits),
            'in_queue': norm in dedup['queue_channel_urls'],
            'in_library': norm in dedup['lib_channel_urls'],
        }

    def _innertube_parse_playlist(self, pr, dedup):
        """Convert a playlistRenderer dict into the unified result shape."""
        pid = pr.get('playlistId') or ''
        if not pid:
            return None
        title = self._innertube_extract_text(pr, 'title')
        uploader = self._innertube_extract_text(pr, 'shortBylineText', 'longBylineText')
        vid_count = pr.get('videoCount') or ''
        # Playlist thumbnails are nested differently: thumbnails: [{ thumbnails: [...] }]
        thumb = ''
        thumbs_outer = pr.get('thumbnails') or []
        if thumbs_outer:
            inner = (thumbs_outer[0] or {}).get('thumbnails') or []
            if inner:
                thumb = inner[-1].get('url') or ''
        if not thumb:
            # Fall back to the first video in the playlist if exposed
            videos = pr.get('videos') or []
            if videos:
                vid_id = (videos[0] or {}).get('videoId')
                if vid_id:
                    thumb = f'https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg'
        return {
            'id': pid,
            'type': 'playlist',
            'title': title or 'Untitled',
            'uploader': uploader,
            'url': f'https://www.youtube.com/playlist?list={pid}',
            'thumbnail': thumb,
            'duration_string': f'{vid_count} videos' if vid_count else '',
            'view_count_string': '',
            'in_queue': pid in dedup['queue_playlist_ids'],
            'in_library': pid in dedup['lib_playlist_ids'],
        }

    def search_youtube(self, query, count=20, kind='all', continuation=None):
        """Run a YouTube search via Innertube. Supports continuation tokens for true
        infinite scroll — pass the token from a previous call's `continuation` field
        to fetch the next batch instead of restarting the query.

        kind: 'all' | 'videos' | 'channels' | 'playlists' (ignored when using continuation;
                                                          YouTube already remembered the filter).

        Returns:
          { results: [...], continuation: '<token-or-null>', kind, count }
        """
        try:
            body = {'context': {'client': dict(self._INNERTUBE_CLIENT)}}
            if continuation:
                # Continuation request — no query/params needed; the token carries them.
                body['continuation'] = continuation
            else:
                q = (query or '').strip()
                if not q:
                    return {'results': [], 'continuation': None, 'kind': kind}
                body['query'] = q
                if kind in self._INNERTUBE_FILTER_PARAMS:
                    body['params'] = self._INNERTUBE_FILTER_PARAMS[kind].replace('%3D', '=')

            sess = self._get_innertube_session()
            try:
                resp = sess.post(
                    self._INNERTUBE_URL,
                    params={'prettyPrint': 'false'},
                    json=body,
                    timeout=15,
                )
            except requests.RequestException as exc:
                return {'error': f'Network error: {exc}'}
            if resp.status_code != 200:
                return {'error': f'Innertube HTTP {resp.status_code}'}

            try:
                data = resp.json()
            except ValueError:
                return {'error': 'Innertube returned non-JSON'}

            dedup = self._build_dedup_sets()
            results = []
            next_continuation = None

            # Two response shapes — initial search vs continuation fetch.
            if continuation:
                # Continuation: onResponseReceivedCommands[].appendContinuationItemsAction.continuationItems[]
                for cmd in (data.get('onResponseReceivedCommands') or []):
                    a = (cmd or {}).get('appendContinuationItemsAction') or {}
                    for item in (a.get('continuationItems') or []):
                        if not isinstance(item, dict):
                            continue
                        # Each item can be: itemSectionRenderer (wrapping result items),
                        # continuationItemRenderer (next-page token), or a bare result item.
                        if 'itemSectionRenderer' in item:
                            for sub in (item['itemSectionRenderer'].get('contents') or []):
                                if isinstance(sub, dict):
                                    parsed = self._innertube_parse_item(sub, dedup)
                                    if parsed: results.append(parsed)
                        elif 'continuationItemRenderer' in item:
                            ce = item['continuationItemRenderer'].get('continuationEndpoint') or {}
                            cc = ce.get('continuationCommand') or {}
                            tok = cc.get('token')
                            if tok: next_continuation = tok
                        else:
                            parsed = self._innertube_parse_item(item, dedup)
                            if parsed: results.append(parsed)
            else:
                # Initial: contents → twoColumnSearchResultsRenderer → primaryContents
                # → sectionListRenderer → contents[]
                try:
                    sections = (data.get('contents', {})
                                    .get('twoColumnSearchResultsRenderer', {})
                                    .get('primaryContents', {})
                                    .get('sectionListRenderer', {})
                                    .get('contents', [])) or []
                except AttributeError:
                    sections = []
                for section in sections:
                    if not isinstance(section, dict):
                        continue
                    if 'continuationItemRenderer' in section:
                        ce = section['continuationItemRenderer'].get('continuationEndpoint') or {}
                        cc = ce.get('continuationCommand') or {}
                        tok = cc.get('token')
                        if tok: next_continuation = tok
                        continue
                    for item in ((section.get('itemSectionRenderer') or {}).get('contents') or []):
                        if isinstance(item, dict):
                            parsed = self._innertube_parse_item(item, dedup)
                            if parsed: results.append(parsed)

            return {
                'results': results,
                'continuation': next_continuation,
                'kind': kind,
                'count': len(results),
            }
        except Exception as exc:
            print(f'[ProTube] search failed: {exc}')
            return {'error': f'Search failed: {exc}'}

    def _innertube_parse_item(self, item, dedup):
        """Dispatch a raw Innertube item to the appropriate parser based on which
        renderer key it carries. Returns None for shapes we don't care about (shelves,
        ads, radio, etc.)."""
        if 'videoRenderer' in item:
            return self._innertube_parse_video(item['videoRenderer'], dedup)
        if 'channelRenderer' in item:
            return self._innertube_parse_channel(item['channelRenderer'], dedup)
        if 'playlistRenderer' in item:
            return self._innertube_parse_playlist(item['playlistRenderer'], dedup)
        if 'lockupViewModel' in item:
            return self._innertube_parse_lockup(item['lockupViewModel'], dedup)
        return None

    # ---- YouTube Music search (music mode) -------------------------------------------

    def _get_music_innertube_session(self):
        """Lazy persistent session for YT Music Innertube calls. Reuses connections."""
        if self._MUSIC_INNERTUBE_SESSION is None:
            s = requests.Session()
            s.headers.update({
                'Content-Type': 'application/json',
                'User-Agent': 'Mozilla/5.0',
                'Origin': 'https://music.youtube.com',
                'Referer': 'https://music.youtube.com/',
            })
            self._MUSIC_INNERTUBE_SESSION = s
        return self._MUSIC_INNERTUBE_SESSION

    def record_music_search(self, query):
        """Append a query to the recent-searches list (most recent first, capped at 12)."""
        try:
            q = (query or '').strip()
            if not q:
                return {'ok': True}
            lst = self.settings.get('music_recent_searches', []) or []
            lst = [x for x in lst if x.lower() != q.lower()]
            lst.insert(0, q)
            self.settings['music_recent_searches'] = lst[:12]
            self._save_settings()
            return {'ok': True}
        except Exception as e:
            return {'error': str(e)}

    def record_video_search(self, query):
        """Same as record_music_search but for the video Search tab."""
        try:
            q = (query or '').strip()
            if not q:
                return {'ok': True}
            lst = self.settings.get('video_recent_searches', []) or []
            lst = [x for x in lst if x.lower() != q.lower()]
            lst.insert(0, q)
            self.settings['video_recent_searches'] = lst[:12]
            self._save_settings()
            return {'ok': True}
        except Exception as e:
            return {'error': str(e)}

    def get_video_for_you(self):
        """Video-search landing data — direct parallel of `get_music_for_you()`.
        Returns the same 6-row shape so the Search view's empty state feels
        identical to the Music tab's:

          {
            recent_searches: [str, ...]      # cap 8
            recent_library:  [video, ...]    # last 8 added
            top_uploader:    str|None        # most-represented uploader in library
            because_you:     [search-result, ...]  # 6 videos by top_uploader (cached)
            trending:        [search-result, ...]  # 6 trending YT videos (cached)
            shuffled_lib:    [video, ...]    # 6 random library videos
          }
        All sub-arrays may be empty; frontend hides empty rows."""
        import random
        result = {
            'recent_searches': [],
            'recent_library': [],
            'top_uploader': None,
            'because_you': [],
            'trending': [],
            'shuffled_lib': [],
        }
        try:
            result['recent_searches'] = (self.settings.get('video_recent_searches', []) or [])[:8]

            lib = list(self.settings.get('library', []) or [])
            def _proj(v):
                return {
                    'id': v.get('id') or '',
                    'title': v.get('title') or 'Untitled',
                    'uploader': v.get('uploader') or '',
                    'thumbnail': v.get('thumbnail') or '',
                    'type': v.get('type') or 'video',
                }
            if lib:
                # Last 8 added — by added_at desc.
                lib_sorted = sorted(lib, key=lambda v: int(v.get('added_at') or 0), reverse=True)
                result['recent_library'] = [_proj(v) for v in lib_sorted[:8]]
                # Top uploader by count.
                counts = {}
                for v in lib:
                    u = (v.get('uploader') or '').strip()
                    if u:
                        counts[u] = counts.get(u, 0) + 1
                if counts:
                    result['top_uploader'] = max(counts.items(), key=lambda kv: kv[1])[0]
                # Shuffled — prefer entries NOT by the top uploader so the row
                # feels different from "Because you have X". Falls back to the
                # whole library if there aren't enough non-top-uploader items.
                pool = [v for v in lib if (v.get('uploader') or '') != result['top_uploader']]
                if len(pool) < 6:
                    pool = lib
                random.shuffle(pool)
                result['shuffled_lib'] = [_proj(v) for v in pool[:6]]

            # "Because you have [Top Uploader]" — search YouTube for that
            # uploader's videos. Cached 24h to avoid hitting Innertube every
            # mount. Filter out library dupes so the row offers genuinely
            # new content.
            if result['top_uploader']:
                cached_key = f'video_because_you_cache_{result["top_uploader"]}'
                cached = self.settings.get(cached_key)
                if cached and (int(time.time()) - cached.get('at', 0)) < 86400:
                    result['because_you'] = cached.get('items', [])
                else:
                    try:
                        sr = self.search_youtube(result['top_uploader'], count=12, kind='videos')
                        items = (sr or {}).get('results', []) or []
                        dedup = self._build_dedup_sets()
                        lib_ids = dedup.get('library_ids', set()) if isinstance(dedup, dict) else set()
                        items = [i for i in items if isinstance(i, dict) and i.get('type') == 'video' and i.get('id') not in lib_ids][:6]
                        result['because_you'] = items
                        self.settings[cached_key] = {'at': int(time.time()), 'items': items}
                        self._save_settings()
                    except Exception:
                        pass

            # Trending — YouTube's daily trending feed via Innertube browse.
            # 24h cache. Same pattern as music's _fetch_yt_music_trending.
            cached_trend = self.settings.get('video_trending_cache')
            if cached_trend and (int(time.time()) - cached_trend.get('at', 0)) < 86400:
                result['trending'] = cached_trend.get('items', [])
            else:
                try:
                    result['trending'] = self._fetch_yt_trending()
                    self.settings['video_trending_cache'] = {'at': int(time.time()), 'items': result['trending']}
                    self._save_settings()
                except Exception:
                    pass

            return result
        except Exception as e:
            print(f'[ProTube] video for-you build failed: {e}')
            return result

    def _fetch_yt_trending(self):
        """Hit YouTube's trending feed (browseId=FEtrending) via Innertube
        and pluck 6 trending videos. Mirrors _fetch_yt_music_trending."""
        body = {
            'context': {'client': dict(self._INNERTUBE_CLIENT)},
            'browseId': 'FEtrending',
        }
        sess = self._get_innertube_session()
        try:
            resp = sess.post('https://www.youtube.com/youtubei/v1/browse',
                             params={'prettyPrint': 'false'}, json=body, timeout=15)
        except requests.RequestException:
            return []
        if resp.status_code != 200:
            return []
        try:
            data = resp.json()
        except ValueError:
            return []
        # Walk the response for any videoRenderer items (the trending tab
        # nests them several levels deep under sectionListRenderer →
        # itemSectionRenderer → contents → shelfRenderer → content →
        # expandedShelfContentsRenderer → items; recurse to keep it simple).
        items = []
        dedup = self._build_dedup_sets()
        def walk(node):
            if not isinstance(node, (dict, list)) or len(items) >= 6:
                return
            if isinstance(node, dict):
                if 'videoRenderer' in node:
                    parsed = self._innertube_parse_item({'videoRenderer': node['videoRenderer']}, dedup)
                    if parsed and parsed.get('type') == 'video':
                        if not any(p.get('id') == parsed.get('id') for p in items):
                            items.append(parsed)
                    return
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(data)
        return items[:6]

    def clear_video_recent_searches(self):
        try:
            self.settings['video_recent_searches'] = []
            self._save_settings()
            return {'ok': True}
        except Exception as e:
            return {'error': str(e)}

    def get_music_for_you(self):
        """Build the 'For You' search-empty landing rows. Returns:
          {
            recent_searches: [str, ...]      # cap 8
            recent_library:  [track, ...]    # last 8 added
            top_artist:      str|None        # most-represented artist in library
            because_you:     [search-result, ...]  # 6 tracks by top_artist (cached)
            trending:        [search-result, ...]  # 6 trending from YT Music charts
            shuffled_lib:    [track, ...]    # 6 random library tracks
          }
        All sub-arrays may be empty. Frontend should hide empty rows.
        """
        import random
        result = {
            'recent_searches': [],
            'recent_library': [],
            'top_artist': None,
            'because_you': [],
            'trending': [],
            'shuffled_lib': [],
        }
        try:
            # Recent searches
            result['recent_searches'] = (self.settings.get('music_recent_searches', []) or [])[:8]

            # Library-derived rows
            lib = list(self.settings.get('music_library', []) or [])
            if lib:
                # Last 8 added
                lib_sorted = sorted(lib, key=lambda t: t.get('added_at', 0), reverse=True)
                result['recent_library'] = lib_sorted[:8]
                # Top artist by track count
                counts = {}
                for t in lib:
                    a = (t.get('artist') or '').strip()
                    if a: counts[a] = counts.get(a, 0) + 1
                if counts:
                    top = max(counts.items(), key=lambda kv: kv[1])
                    result['top_artist'] = top[0]
                # Shuffled sample (6, excluding the top-artist tracks to feel different)
                pool = [t for t in lib if (t.get('artist') or '') != result['top_artist']]
                if len(pool) < 6:
                    pool = lib
                random.shuffle(pool)
                result['shuffled_lib'] = pool[:6]

            # "Because you have [Top Artist]" — search YT Music for that artist's songs
            if result['top_artist']:
                cached_key = f'music_because_you_cache_{result["top_artist"]}'
                cached = self.settings.get(cached_key)
                # Cache for 24h
                if cached and (int(time.time()) - cached.get('at', 0)) < 86400:
                    result['because_you'] = cached.get('items', [])
                else:
                    try:
                        sr = self.search_youtube_music(result['top_artist'], kind='songs')
                        items = (sr or {}).get('results', [])[:6]
                        # Filter out items already in library
                        lib_ids = self._build_music_library_id_set()
                        items = [i for i in items if i.get('id') not in lib_ids]
                        result['because_you'] = items[:6]
                        self.settings[cached_key] = {'at': int(time.time()), 'items': result['because_you']}
                        self._save_settings()
                    except Exception:
                        pass

            # Trending — YT Music charts via Innertube. Cache 24h.
            cached_trend = self.settings.get('music_trending_cache')
            if cached_trend and (int(time.time()) - cached_trend.get('at', 0)) < 86400:
                result['trending'] = cached_trend.get('items', [])
            else:
                try:
                    result['trending'] = self._fetch_yt_music_trending()
                    self.settings['music_trending_cache'] = {'at': int(time.time()), 'items': result['trending']}
                    self._save_settings()
                except Exception:
                    pass

            return result
        except Exception as e:
            print(f'[ProTube] for-you build failed: {e}')
            return result

    def _fetch_yt_music_trending(self):
        """Hit YT Music's charts browse endpoint and pluck 6 trending songs."""
        body = {
            'context': {'client': dict(self._MUSIC_INNERTUBE_CLIENT)},
            'browseId': 'FEmusic_charts',
        }
        sess = self._get_music_innertube_session()
        resp = sess.post('https://music.youtube.com/youtubei/v1/browse',
                         params={'prettyPrint': 'false'}, json=body, timeout=15)
        if resp.status_code != 200:
            return []
        data = resp.json()
        # Walk the response for any musicResponsiveListItemRenderer items.
        items = []
        lib_ids = self._build_music_library_id_set()
        def walk(node):
            if not isinstance(node, (dict, list)) or len(items) >= 6:
                return
            if isinstance(node, dict):
                if 'musicResponsiveListItemRenderer' in node:
                    parsed = self._parse_music_shelf_item(node['musicResponsiveListItemRenderer'], 'songs', lib_ids)
                    if parsed and parsed.get('kind') == 'song':
                        items.append(parsed)
                        return
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(data)
        return items[:6]

    def search_youtube_music(self, query, kind='songs'):
        """Search YouTube Music and return music-shaped results.

        kind: 'songs' | 'videos' | 'albums' | 'artists' | 'playlists' (default 'songs')

        Each result for kind='songs':
          { id, type='music', kind='song', title, artist, album, duration_string,
            play_count, thumbnail, url, in_library }

        For other kinds, returned fields shift to match the type. The frontend can
        adapt the card layout per result.kind.
        """
        try:
            q = (query or '').strip()
            if not q:
                return {'results': [], 'kind': kind}

            body = {
                'context': {'client': dict(self._MUSIC_INNERTUBE_CLIENT)},
                'query': q,
            }
            if kind in self._MUSIC_INNERTUBE_FILTER_PARAMS:
                body['params'] = self._MUSIC_INNERTUBE_FILTER_PARAMS[kind].replace('%3D', '=')

            sess = self._get_music_innertube_session()
            try:
                resp = sess.post(
                    self._MUSIC_INNERTUBE_URL,
                    params={'prettyPrint': 'false'},
                    json=body,
                    timeout=15,
                )
            except requests.RequestException as exc:
                return {'error': f'Network error: {exc}'}
            if resp.status_code != 200:
                return {'error': f'YT Music HTTP {resp.status_code}'}
            try:
                data = resp.json()
            except ValueError:
                return {'error': 'YT Music returned non-JSON'}

            # Walk YT Music's response: tabbedSearchResultsRenderer → tabs[0] → tabRenderer
            # → content.sectionListRenderer.contents[] → musicShelfRenderer.contents[].
            tabs = (data.get('contents', {})
                        .get('tabbedSearchResultsRenderer', {})
                        .get('tabs', []) or [])
            if not tabs:
                return {'results': [], 'kind': kind}
            sections = (tabs[0].get('tabRenderer', {})
                              .get('content', {})
                              .get('sectionListRenderer', {})
                              .get('contents', []) or [])

            music_lib_ids = self._build_music_library_id_set()
            results = []
            continuation = ''
            for s in sections:
                shelf = s.get('musicShelfRenderer')
                if not shelf:
                    continue
                for c in (shelf.get('contents') or []):
                    item = c.get('musicResponsiveListItemRenderer')
                    if not item:
                        continue
                    parsed = self._parse_music_shelf_item(item, kind, music_lib_ids)
                    if parsed:
                        results.append(parsed)
                # YT Music puts the "next page" token on the shelf itself. Only
                # the FIRST shelf we hit gets its continuation captured (other
                # shelves would represent a different kind, which we don't mix
                # across pages).
                if not continuation:
                    continuation = self._extract_music_continuation(shelf)

            return {'results': results, 'kind': kind, 'count': len(results),
                    'continuation': continuation}
        except Exception as exc:
            print(f'[ProTube] music search failed: {exc}')
            return {'error': f'Music search failed: {exc}'}

    @staticmethod
    def _extract_music_continuation(shelf):
        """Pull the continuation token out of a musicShelfRenderer or
        musicShelfContinuation. Returns '' when there's no next page."""
        try:
            conts = shelf.get('continuations') or []
            for c in conts:
                tok = (c.get('nextContinuationData') or {}).get('continuation')
                if tok:
                    return tok
                # InnerTube sometimes wraps the same token under reloadContinuationData
                tok = (c.get('reloadContinuationData') or {}).get('continuation')
                if tok:
                    return tok
        except Exception:
            pass
        return ''

    def search_youtube_music_continuation(self, continuation, kind='songs'):
        """Fetch the next page of music search results. Frontend calls this
        when the user scrolls near the bottom of the results list. Returns
        the same shape as search_youtube_music — { results, kind, continuation }.
        Empty continuation in the response means we've hit the end."""
        try:
            tok = (continuation or '').strip()
            if not tok:
                return {'results': [], 'kind': kind, 'continuation': ''}
            body = {
                'context': {'client': dict(self._MUSIC_INNERTUBE_CLIENT)},
            }
            sess = self._get_music_innertube_session()
            try:
                resp = sess.post(
                    self._MUSIC_INNERTUBE_URL,
                    params={'prettyPrint': 'false', 'continuation': tok},
                    json=body,
                    timeout=15,
                )
            except requests.RequestException as exc:
                return {'error': f'Network error: {exc}'}
            if resp.status_code != 200:
                return {'error': f'YT Music HTTP {resp.status_code}'}
            try:
                data = resp.json()
            except ValueError:
                return {'error': 'YT Music returned non-JSON'}

            # Continuation response: continuationContents.musicShelfContinuation
            cont = (data.get('continuationContents') or {}).get('musicShelfContinuation') or {}
            music_lib_ids = self._build_music_library_id_set()
            results = []
            for c in (cont.get('contents') or []):
                item = c.get('musicResponsiveListItemRenderer')
                if not item:
                    continue
                parsed = self._parse_music_shelf_item(item, kind, music_lib_ids)
                if parsed:
                    results.append(parsed)
            next_token = self._extract_music_continuation(cont)
            return {'results': results, 'kind': kind, 'count': len(results),
                    'continuation': next_token}
        except Exception as exc:
            print(f'[ProTube] music search continuation failed: {exc}')
            return {'error': f'Music search continuation failed: {exc}'}

    def _parse_music_shelf_item(self, mrlir, kind, lib_ids):
        """Convert a musicResponsiveListItemRenderer into a clean result dict.

        Column layout varies by kind:
          - songs:    [Title, Artist • Album • Duration, Play count]
          - videos:   [Title, Channel • Views • Duration]
          - albums:   [Title, Album • Artist • Year]
          - artists:  [Name, Subscribers]
          - playlists:[Title, Playlist • Author • TrackCount]
        We pull text out of each flex column, then map to fields based on kind.
        """
        cols = mrlir.get('flexColumns') or []
        col_texts = []
        for col in cols:
            t = col.get('musicResponsiveListItemFlexColumnRenderer', {}).get('text', {})
            runs = t.get('runs') or []
            # Join all run texts (skips the " • " separators which are runs themselves)
            col_texts.append(''.join(r.get('text', '') for r in runs).strip())

        # Track ID lives in playlistItemData.videoId for songs/videos.
        playlist_item = mrlir.get('playlistItemData') or {}
        video_id = playlist_item.get('videoId') or ''

        # Thumbnail
        thumbs = (mrlir.get('thumbnail') or {}).get('musicThumbnailRenderer', {}) \
                 .get('thumbnail', {}).get('thumbnails', []) or []
        thumb = thumbs[-1].get('url') if thumbs else ''

        title = col_texts[0] if col_texts else ''
        if not title:
            return None

        # Second column is the "Artist • Album • Duration" line; we split on " • ".
        meta_bits = []
        if len(col_texts) > 1:
            meta_bits = [b.strip() for b in col_texts[1].split('•') if b.strip()]

        if kind in ('songs', 'videos'):
            if not video_id:
                return None
            artist = meta_bits[0] if meta_bits else ''
            duration = meta_bits[-1] if len(meta_bits) >= 2 else ''
            album = meta_bits[1] if len(meta_bits) >= 3 else ''  # songs only — videos don't have an album
            play_count = col_texts[2] if len(col_texts) > 2 else ''
            return {
                'id': video_id,
                'type': 'music',
                'kind': 'song' if kind == 'songs' else 'video',
                'title': title,
                'artist': artist,
                'album': album,
                'duration_string': duration,
                'play_count': play_count,
                'thumbnail': thumb,
                'url': f'https://music.youtube.com/watch?v={video_id}',
                'in_library': video_id in lib_ids,
            }

        # albums / artists / playlists — for v1 we only return the basics so the
        # frontend can render the card; clicking these opens the YT Music URL.
        nav = mrlir.get('navigationEndpoint', {}) or {}
        browse = (nav.get('browseEndpoint') or {})
        browse_id = browse.get('browseId') or ''
        return {
            'id': browse_id,
            'type': 'music',
            'kind': kind[:-1],   # 'albums' → 'album', etc.
            'title': title,
            'subtitle': col_texts[1] if len(col_texts) > 1 else '',
            'thumbnail': thumb,
            'url': f'https://music.youtube.com/browse/{browse_id}' if browse_id else '',
            'in_library': False,
        }

    def _build_music_library_id_set(self):
        """Snapshot the music_library's video IDs for cross-checking search results."""
        ids = set()
        for t in (self.settings.get('music_library', []) or []):
            if t.get('id'):
                ids.add(t['id'])
        return ids

    # ---- Music download + library ----------------------------------------------------

    @staticmethod
    def _strip_collection_prefix(title):
        """Strip the leading 'Album – ' / 'Single – ' / 'EP – ' / 'Playlist – '
        / 'Soundtrack – ' / 'Mixtape – ' classifier that YT Music's browse
        endpoint puts on collection titles. Without this, the library shows
        cards like 'Album – ICEMAN' instead of just 'ICEMAN'.
        Handles em-dash, en-dash, and hyphen variants because YT Music uses
        different separators across regions."""
        if not title:
            return title
        import re
        return re.sub(
            r'^(Album|Single|EP|Playlist|Soundtrack|Mixtape|Compilation)\s*[–—\-]\s*',
            '', title, flags=re.IGNORECASE,
        )

    @staticmethod
    def _resolve_album_artist(info):
        """Resolve an album's artist. YT Music sometimes leaves the top-level
        `artist`/`uploader` fields empty on browse pages, so we also walk the
        playlist's child entries and pick the most common artist (excluding
        'Various Artists' style placeholders). Fall back chain:
        info-level artist/creator/uploader/channel → entry-level most-common
        artist → playlist_uploader → 'Unknown Artist'.
        Without the ' - Topic' strip, Drake's album would read 'Drake - Topic'."""
        def _strip_topic(v):
            v = (v or '').strip()
            if v.endswith(' - Topic'):
                v = v[:-len(' - Topic')]
            return v
        for key in ('artist', 'creator'):
            v = info.get(key)
            if v:
                return v
        for key in ('uploader', 'channel'):
            v = _strip_topic(info.get(key))
            if v:
                return v
        # Walk the album's tracks — most YT Music album tracks carry artist.
        entries = info.get('entries') or []
        from collections import Counter
        counts = Counter()
        for e in entries:
            if not isinstance(e, dict):
                continue
            a = e.get('artist') or e.get('creator') or _strip_topic(e.get('uploader') or e.get('channel'))
            if a and a.lower() not in ('various artists', 'various', ''):
                counts[a] += 1
        if counts:
            return counts.most_common(1)[0][0]
        # Last-ditch: playlist_uploader from the YT extractor
        v = _strip_topic(info.get('playlist_uploader'))
        if v:
            return v
        return 'Unknown Artist'

    @staticmethod
    def _sanitize_path_segment(s, fallback='Unknown'):
        """Strip filesystem-unsafe characters from a string so we can use it as a
        path segment. Keeps Unicode letters/digits, replaces the Windows-forbidden
        set (`<>:"/\\|?*`) and trims trailing dots/spaces (also forbidden on Windows)."""
        import re
        s = (s or '').strip()
        if not s:
            return fallback
        # Replace forbidden chars with a space, collapse runs of whitespace.
        s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', ' ', s)
        s = re.sub(r'\s+', ' ', s).strip()
        # Trim trailing dots/spaces (Windows weirdness)
        s = s.rstrip('. ')
        # Cap length to keep paths sane (Windows MAX_PATH = 260 total).
        if len(s) > 80:
            s = s[:80].rstrip('. ')
        return s or fallback

    def add_music_track(self, video_id_or_url):
        """Enqueue a single track for background download. The queue processor
        promotes it to 'downloading' up to `max_concurrent_music_downloads` at a
        time and runs `_music_download_worker`. Progress flows through both the
        per-track `updateMusicDownload` event AND the queue-wide
        `updateMusicQueue` event."""
        try:
            url = video_id_or_url
            if not url.startswith('http'):
                url = f'https://music.youtube.com/watch?v={video_id_or_url}'
            # Cheap dedup — already in library? (only catches the bare-id case)
            lib_ids = self._build_music_library_id_set()
            if video_id_or_url in lib_ids:
                return {'already_in_library': True}
            # Resolve a stable id for the queue entry. If the caller passed an
            # 11-char video id, use it; otherwise try to extract `v=<id>` from
            # the URL. Falls back to '' if neither yields one (rare — the
            # worker will still process the URL but cancel won't work).
            if len(video_id_or_url) == 11 and not video_id_or_url.startswith('http'):
                track_id = video_id_or_url
            else:
                import re as _re
                m = _re.search(r'[?&]v=([A-Za-z0-9_-]{11})', url)
                track_id = m.group(1) if m else ''
            with self._music_queue_lock:
                q = self.settings.get('music_queue', []) or []
                for entry in q:
                    if entry.get('id') == track_id and entry.get('status') in ('queued', 'downloading'):
                        return {'already_queued': True, 'id': track_id}
                # Quick partial entry — title/artist will be filled in by the worker
                # after it does the metadata probe. We don't block to extract here
                # because the user just clicked '+' and wants instant feedback.
                entry = {
                    'id': track_id,
                    'title': '',
                    'artist': '',
                    'album': '',
                    'album_id': '',
                    'thumbnail': '',
                    'url': url,
                    'status': 'queued',
                    'progress': 0,
                    'queued_at': int(time.time()),
                    'started_at': None,
                    'completed_at': None,
                    'error': None,
                }
                q.append(entry)
                self.settings['music_queue'] = q
                self._save_settings()
                queue_len = sum(1 for e in q if e.get('status') in ('queued', 'downloading'))
            self._music_queue_wake()
            self._emit_music_queue()
            return {'queued': True, 'id': track_id, 'queue_len': queue_len}
        except Exception as e:
            return {'error': str(e)}

    def add_music_collection(self, browse_id_or_url, kind=None):
        """Bulk-download a YT Music album / playlist / artist's top tracks.

        Resolves the browse/playlist ID to a list of video IDs via yt-dlp's flat
        extraction, then queues each one through `add_music_track`. Fire-and-forget.
        kind: 'album' | 'playlist' | 'artist' — only used to label progress toasts.
        For kind='album', the resolved collection is also persisted as a first-class
        `music_albums` entry with aggregate progress (track grouping in the library).
        """
        try:
            url = browse_id_or_url
            collection_id = browse_id_or_url if not browse_id_or_url.startswith('http') else ''
            if not url.startswith('http'):
                # YT Music browseIds: albums (MPREb_), artists (UC), playlists (VLPLor RDCLAK5)
                if url.startswith('MPREb_') or url.startswith('OLAK5uy_'):
                    url = f'https://music.youtube.com/browse/{url}'
                elif url.startswith('UC'):
                    url = f'https://music.youtube.com/channel/{url}'
                elif url.startswith('VL') or url.startswith('PL') or url.startswith('RD'):
                    pid = url[2:] if url.startswith('VL') else url
                    url = f'https://music.youtube.com/playlist?list={pid}'
                else:
                    url = f'https://music.youtube.com/browse/{url}'
            threading.Thread(
                target=self._music_collection_worker,
                args=(url, kind or 'collection', collection_id),
                daemon=True,
            ).start()
            return {'ok': True, 'started': True}
        except Exception as e:
            return {'error': str(e)}

    def _music_collection_worker(self, url, kind, collection_id=''):
        """Resolve a YT Music collection URL → list of video IDs, then download each.

        For kind='album', also writes a `music_albums` entry up-front so the library
        grid can show a single album card with aggregate progress instead of N
        independent track rows.
        """
        try:
            self._send_to_js('showToast', f'Resolving {kind}…', None, None)
            opts = self._get_ydl_opts('browser', 'none')
            opts.update({
                'quiet': True, 'no_warnings': True,
                'skip_download': True,
                'extract_flat': 'in_playlist',
            })
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            entries = info.get('entries') or []
            # Some YT Music browse responses wrap tracks under a "Songs" tab; flat-extract
            # exposes them as nested entries with 'entries' key in each parent. Flatten.
            flat = []
            for e in entries:
                if not e:
                    continue
                if isinstance(e, dict) and e.get('entries'):
                    flat.extend(x for x in e['entries'] if x)
                else:
                    flat.append(e)
            video_ids = []
            for e in flat:
                vid = e.get('id') if isinstance(e, dict) else None
                if vid and len(vid) == 11:   # YT video IDs are 11 chars
                    video_ids.append(vid)
            if not video_ids:
                self._send_to_js('showToast', f'No tracks found in this {kind}.', None, None)
                # Also clear any search-row spinner waiting on this collection.
                try:
                    self._send_to_js(
                        'updateMusicCollectionResolveError',
                        collection_id or '',
                        f'No tracks found in this {kind}.',
                    )
                except Exception:
                    pass
                return
            # Cap artist downloads at 25 tracks so we don't accidentally grab a whole channel
            if kind == 'artist' and len(video_ids) > 25:
                video_ids = video_ids[:25]

            # -- Album entity: persist BEFORE spawning workers so the library grid
            # can pick up the in-progress album card immediately. --
            album_id = ''
            if kind == 'album':
                # Resolve a stable album_id. Prefer the original browse id (MPREb_/OLAK5uy_),
                # otherwise yt-dlp's own id field, otherwise hash the URL.
                album_id = collection_id or (info.get('id') or '') or ''
                if not album_id:
                    import hashlib as _hashlib
                    album_id = 'AL' + _hashlib.md5(url.encode('utf-8')).hexdigest()[:14]
                # YT Music browse pages return titles like 'Album – ICEMAN' /
                # 'Single – ...' / 'Playlist – ...' / 'EP – ...'. The classifier
                # prefix is redundant inside the app and noisy in the UI — strip it
                # so the user sees just the album name.
                raw_title = info.get('title') or info.get('album') or 'Untitled Album'
                album_title = self._strip_collection_prefix(raw_title)
                # Artist resolution: YT Music sets `artist` directly on the info
                # dict for album pages. Fall back to uploader (with the auto-channel
                # " - Topic" suffix stripped — that suffix is how YT Music names
                # its per-artist topic channels and looks ugly in the UI).
                album_artist = self._resolve_album_artist(info)
                # Best cover candidate: largest thumbnail in the info dict.
                cover_url = ''
                thumbs = info.get('thumbnails') or []
                if thumbs and isinstance(thumbs, list):
                    try:
                        best = max(
                            (t for t in thumbs if isinstance(t, dict) and t.get('url')),
                            key=lambda t: (t.get('width') or 0) * (t.get('height') or 0),
                            default=None,
                        )
                        if best:
                            cover_url = best.get('url') or ''
                    except Exception:
                        cover_url = ''
                if not cover_url:
                    cover_url = info.get('thumbnail') or ''
                # Filter to the IDs we'll actually attempt — keep the originally resolved
                # ordered list (including already-in-library ones) as the album manifest,
                # so the album detail view always shows the full track list.
                self._upsert_music_album({
                    'id': album_id,
                    'title': album_title,
                    'artist': album_artist,
                    'cover_url': cover_url,
                    'source_url': url,
                    'added_at': int(time.time()),
                    'total_tracks': len(video_ids),
                    'downloaded_count': 0,
                    'status': 'downloading',
                    'track_ids': list(video_ids),
                    'seen_at': None,
                })
                # If some tracks were already in the user's library (singles), stamp
                # them with this album_id so they get absorbed under the album card.
                self._stamp_existing_tracks_with_album(video_ids, album_id)
                # Recompute downloaded_count to reflect already-owned tracks before
                # spawning workers — the album might already be partially complete.
                self._recount_album(album_id)
                # Initial 0% event so the JS can render the ring + paint the card.
                done = self._album_downloaded_count(album_id)
                self._send_to_js(
                    'updateMusicAlbumProgress', album_id, done, len(video_ids)
                )

            lib_ids = self._build_music_library_id_set()
            # Build a quick lookup of already-queued tracks so we don't re-enqueue.
            with self._music_queue_lock:
                existing_q = self.settings.get('music_queue', []) or []
                queued_ids = {
                    e.get('id') for e in existing_q
                    if e.get('status') in ('queued', 'downloading')
                }
            # Per-track metadata stamped on each queue entry up-front: artist/title
            # come from the flat extraction (yt-dlp gives us 'title' which is
            # usually "Artist - Track" on YT Music). Album metadata comes from the
            # collection-level info dict.
            album_title = info.get('title') or info.get('album') or ''
            album_artist = (
                info.get('uploader') or info.get('artist')
                or info.get('creator') or info.get('channel') or ''
            )
            album_cover = ''
            thumbs = info.get('thumbnails') or []
            if thumbs and isinstance(thumbs, list):
                try:
                    best = max(
                        (t for t in thumbs if isinstance(t, dict) and t.get('url')),
                        key=lambda t: (t.get('width') or 0) * (t.get('height') or 0),
                        default=None,
                    )
                    if best:
                        album_cover = best.get('url') or ''
                except Exception:
                    pass
            if not album_cover:
                album_cover = info.get('thumbnail') or ''
            flat_by_id = {e.get('id'): e for e in flat if isinstance(e, dict) and e.get('id')}

            queued = 0
            skipped = 0
            now_ts = int(time.time())
            with self._music_queue_lock:
                q = self.settings.get('music_queue', []) or []
                for vid in video_ids:
                    if vid in lib_ids:
                        skipped += 1
                        continue
                    if vid in queued_ids:
                        skipped += 1
                        continue
                    src = flat_by_id.get(vid, {}) or {}
                    raw_title = src.get('title') or ''
                    # YT Music flat-extract titles often look like "Artist - Track".
                    # Split on " - " if present, else fall back to album-level artist.
                    track_artist = src.get('artist') or src.get('uploader') or ''
                    track_title = raw_title
                    if not track_artist and ' - ' in raw_title:
                        parts = raw_title.split(' - ', 1)
                        track_artist, track_title = parts[0].strip(), parts[1].strip()
                    if not track_artist:
                        track_artist = album_artist
                    # Pick best thumb from per-track thumbnails, else fall back to album cover.
                    track_thumb = ''
                    t_thumbs = src.get('thumbnails') or []
                    if t_thumbs and isinstance(t_thumbs, list):
                        try:
                            best = max(
                                (t for t in t_thumbs if isinstance(t, dict) and t.get('url')),
                                key=lambda t: (t.get('width') or 0) * (t.get('height') or 0),
                                default=None,
                            )
                            if best:
                                track_thumb = best.get('url') or ''
                        except Exception:
                            pass
                    if not track_thumb:
                        track_thumb = album_cover
                    entry = {
                        'id': vid,
                        'title': track_title or 'Untitled',
                        'artist': track_artist,
                        'album': album_title if kind == 'album' else '',
                        'album_id': album_id or '',
                        'thumbnail': track_thumb,
                        'url': f'https://music.youtube.com/watch?v={vid}',
                        'status': 'queued',
                        'progress': 0,
                        'queued_at': now_ts,
                        'started_at': None,
                        'completed_at': None,
                        'error': None,
                    }
                    q.append(entry)
                    queued += 1
                    queued_ids.add(vid)
                self.settings['music_queue'] = q
                self._save_settings()
            # Initial 0% events so search-row rings paint immediately.
            for vid in video_ids:
                if vid not in lib_ids:
                    self._send_to_js('updateMusicDownload', vid, 0)
            if queued:
                self._music_queue_wake()
            self._emit_music_queue()
            if queued:
                self._send_to_js('showToast', f'Added {queued} tracks to download queue.', None, None)
            elif skipped:
                self._send_to_js('showToast', f'{skipped} already in library or queue', None, None)
            else:
                self._send_to_js('showToast', 'Nothing to download', None, None)
            # Edge case: album with zero new downloads (everything was already in
            # library). Flip status to complete immediately so the card doesn't
            # sit forever on 'downloading'.
            if kind == 'album' and queued == 0:
                self._mark_album_complete_if_done(album_id)
        except Exception as e:
            # Log full traceback to protube.log; print() under pythonw is dropped.
            tb = traceback.format_exc()
            self._log_to_protube_log(
                f'[ProTube/music-collection] failed: {kind} {url}: {e}\n{tb}'
            )
            self._send_to_js('showToast', f'{kind.title()} download failed: {e}', None, None)
            # Tell the frontend the collection resolve failed so any search-row
            # that's been spinning on this collection_id can clear its
            # .downloading state — otherwise it sits forever (no per-track
            # event ever fires for an empty/erroring resolve).
            try:
                self._send_to_js(
                    'updateMusicCollectionResolveError',
                    collection_id or '',
                    str(e),
                )
            except Exception:
                pass

    def _music_download_worker(self, url, album_id=None, queue_id=None):
        """Background worker: extract metadata, download audio, embed tags + art,
        stamp the library. If `album_id` is provided, the resulting track entry is
        stamped with it AND the parent album's downloaded_count is incremented.

        `queue_id` is the music_queue entry id this worker is fulfilling — used to
        update status/progress on that entry. Provided when the queue processor
        spawns us; None for direct calls (legacy)."""

        def _is_cancelled():
            if not queue_id:
                return False
            with self._music_queue_lock:
                return queue_id in self._music_queue_cancelled_ids

        captured_filepath = {'path': None}
        try:
            from app_paths import music_dir
            base_dir = music_dir()

            # Mark queue entry as 'downloading'.
            if queue_id:
                self._update_music_queue_entry(
                    queue_id,
                    {'status': 'downloading', 'started_at': int(time.time()), 'progress': 0},
                )

            if _is_cancelled():
                self._finalize_cancelled(queue_id)
                return

            # Phase 1: metadata probe (no download) so we know artist/album for
            # the final filename layout and library entry.
            opts = self._get_ydl_opts('browser', 'none')
            opts.update({'quiet': True, 'no_warnings': True, 'skip_download': True})
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)

            vid = info.get('id') or ''
            if not vid:
                self._send_to_js('showToast', 'Music download failed: no video id', None, None)
                if queue_id:
                    self._update_music_queue_entry(
                        queue_id,
                        {'status': 'failed', 'error': 'no video id', 'completed_at': int(time.time())},
                    )
                return

            # Re-check dedup with the actual extracted ID
            if vid in self._build_music_library_id_set():
                self._send_to_js('showToast', 'Already in your music library', None, None)
                if queue_id:
                    self._update_music_queue_entry(
                        queue_id,
                        {'status': 'done', 'progress': 100, 'completed_at': int(time.time())},
                    )
                return

            # Metadata: YT Music gives us 'artist'/'album'/'track' directly; regular YT
            # falls back to uploader/title with no album info.
            artist = info.get('artist') or info.get('creator') or info.get('uploader') or 'Unknown Artist'
            album = info.get('album') or 'Singles'
            title = info.get('track') or info.get('title') or 'Untitled'
            release_year = info.get('release_year') or info.get('upload_date', '')[:4]
            duration_s = info.get('duration') or 0

            safe_artist = self._sanitize_path_segment(artist, 'Unknown Artist')
            safe_album = self._sanitize_path_segment(album, 'Singles')
            safe_title = self._sanitize_path_segment(title, 'Untitled')

            target_dir = os.path.join(base_dir, safe_artist, safe_album)
            os.makedirs(target_dir, exist_ok=True)

            # Phase 2: actual download.
            dl_opts = self._get_ydl_opts('browser', 'none')
            dl_opts.update({
                'format': 'bestaudio[ext=m4a]/bestaudio/best',
                'outtmpl': os.path.join(target_dir, f'{safe_title}.%(ext)s'),
                'writethumbnail': True,
                'postprocessors': [
                    {'key': 'FFmpegMetadata', 'add_metadata': True},
                    {'key': 'EmbedThumbnail'},
                ],
                'quiet': True,
                'no_warnings': True,
                # Force a fresh download every time. Without this, yt-dlp tries to
                # resume any partial .m4a sitting in target_dir from a previous
                # failed run, which trips 'HTTP Error 416: Requested range not
                # satisfiable' when the URL/server state has changed since the
                # partial was written. Music files are small (~3-5MB) so the
                # retry cost is negligible.
                'continue': False,
                'nopart': False,
            })

            def hook(d):
                # Cancellation: raise to abort the ydl.download() call below.
                if _is_cancelled():
                    raise _MusicDownloadCancelled()
                if d.get('status') == 'finished':
                    captured_filepath['path'] = d.get('filename')
                if d.get('status') == 'downloading':
                    pct = 0
                    total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                    if total:
                        pct = (d.get('downloaded_bytes') or 0) * 100 / total
                    pct_i = int(round(pct))
                    # Throttle: only emit on whole-percent change. Avoids the
                    # ~10x/sec progress-hook spam from yt-dlp causing the queue
                    # to flicker through full re-renders.
                    if queue_id and pct_i == self._music_queue_last_pct.get(queue_id):
                        return
                    if queue_id:
                        self._music_queue_last_pct[queue_id] = pct_i
                    self._send_to_js('updateMusicDownload', vid, pct_i)
                    # In-memory only — don't persist progress to disk on every
                    # tick (was triggering settings-write + full queue refresh).
                    # Frontend updates the row in place via updateMusicDownload.
                    if queue_id:
                        with self._music_queue_lock:
                            q = self.settings.get('music_queue', []) or []
                            for entry in q:
                                if entry.get('id') == queue_id:
                                    entry['progress'] = pct_i
                                    break

            def pp_hook(d):
                if d.get('status') == 'finished':
                    fp = (d.get('info_dict') or {}).get('filepath')
                    if fp:
                        captured_filepath['path'] = fp

            dl_opts['progress_hooks'] = [hook]
            dl_opts['postprocessor_hooks'] = [pp_hook]

            self._send_to_js('updateMusicDownload', vid, 0)
            with YoutubeDL(dl_opts) as ydl:
                ydl.download([url])

            final_path = captured_filepath['path'] or ''
            # If postprocessors changed the extension (likely .m4a) the captured path
            # may be the pre-conversion file. Find the actual final file.
            if final_path:
                base = os.path.splitext(final_path)[0]
                for ext in ('.m4a', '.mp3', '.opus', '.webm'):
                    cand = base + ext
                    if os.path.exists(cand):
                        final_path = cand
                        break

            # The EmbedThumbnail postprocessor put the album art inside the .m4a's
            # metadata stream; we also kept the loose image next to the file (so
            # external players see album art too). The library entry just stores
            # the remote thumbnail URL — fast to render from YouTube's CDN, and
            # works fine as long as we have internet. (Offline album art is a
            # post-MVP polish — would extend the stream server to serve images.)
            local_thumb = ''
            for ext in ('.webp', '.jpg', '.png'):
                cand = os.path.join(target_dir, f'{safe_title}{ext}')
                if os.path.exists(cand):
                    local_thumb = cand
                    break

            # Per-track thumbnail = the YouTube video thumb for this song.
            # Cache it locally via _cache_thumbnail (returns a 'pt:thumb:' marker)
            # so the album-detail view + player view render instantly instead of
            # waiting on i.ytimg.com over the network on every paint. Falls back
            # to the remote URL if the cache attempt fails.
            remote_thumb = info.get('thumbnail') or ''
            if not remote_thumb and vid:
                remote_thumb = f'https://i.ytimg.com/vi/{vid}/hqdefault.jpg'
            thumb = self._cache_thumbnail(remote_thumb, vid) if (remote_thumb and vid) else remote_thumb

            # Build library entry.
            entry = {
                'id': vid,
                'type': 'music',
                'title': title,
                'artist': artist,
                'album': album,
                'year': str(release_year) if release_year else '',
                'duration_string': self._format_duration(duration_s),
                'duration_seconds': int(duration_s) if duration_s else 0,
                'filepath': final_path,
                'thumbnail': thumb,
                'local_thumbnail': local_thumb,
                'url': url,
                'added_at': int(time.time()),
            }
            # Stamp the album link BEFORE persisting so the track is grouped on
            # first paint of the music library grid.
            if album_id:
                entry['album_id'] = album_id
            # Wrap the post-download finalize in a single deferred-save context.
            # Previously each of add_to_music_library / _bump_album_progress /
            # _ensure_album_cover_local / _update_music_queue_entry triggered
            # its own settings.json write — 4 atomic writes per track. For a
            # 50-track album that's 200 writes; visible delay before the
            # library reflects "done". Now: 1 write per track.
            new_cover_marker = ''
            with self._deferred_save():
                self.add_to_music_library(entry)
                if album_id:
                    self._bump_album_progress(album_id)
                    # Extract album cover from the just-downloaded track
                    # immediately so the library card has real art on first
                    # paint instead of a placeholder.
                    try:
                        albums = self.settings.get('music_albums', []) or []
                        alb = next((a for a in albums if a.get('id') == album_id), None)
                        if alb and not (alb.get('cover_url') or '').startswith('pt:thumb:'):
                            new_cover = self._ensure_album_cover_local(alb)
                            if new_cover and new_cover != alb.get('cover_url'):
                                alb['cover_url'] = new_cover
                                self.settings['music_albums'] = albums
                                self._save_settings()  # buffered by deferred ctx
                                new_cover_marker = new_cover
                    except Exception:
                        pass
                if queue_id:
                    self._update_music_queue_entry(
                        queue_id,
                        {
                            'status': 'done',
                            'progress': 100,
                            'completed_at': int(time.time()),
                            'title': title,
                            'artist': artist,
                            'album': album,
                            'thumbnail': thumb,
                        },
                    )
            # Emit JS events AFTER the single flush — frontend reads from
            # persistent state next refresh and we don't want the inflight
            # write to race with the load_music_library callback.
            if album_id and new_cover_marker:
                self._send_to_js('musicAlbumCoverResolved', album_id, new_cover_marker)
            self._send_to_js('musicDownloadDone', entry)
        except _MusicDownloadCancelled:
            # User cancelled while ydl.download() was running. Clean up any
            # partial file the download left behind so a retry starts fresh.
            try:
                fp = captured_filepath.get('path')
                if fp and os.path.exists(fp):
                    os.remove(fp)
                # yt-dlp's .part files also leak; clean those siblings too.
                if fp:
                    part = fp + '.part'
                    if os.path.exists(part):
                        os.remove(part)
            except Exception:
                pass
            self._finalize_cancelled(queue_id)
        except Exception as e:
            # Persist the full traceback to data/protube.log so the user can
            # see why a track failed after the fact. The toast is transient
            # and `print()` under pythonw is discarded.
            tb = traceback.format_exc()
            self._log_to_protube_log(
                f'[ProTube/music-dl] failed: {url}: {e}\n{tb}'
            )
            self._send_to_js('showToast', f'Music download failed: {e}', None, None)
            if queue_id:
                self._update_music_queue_entry(
                    queue_id,
                    {'status': 'failed', 'error': str(e), 'completed_at': int(time.time())},
                )

    # ----------------------------- music queue ---------------------------------- #

    def _music_queue_wake(self):
        """Nudge the queue processor to take another pass. Cheap, idempotent."""
        self._music_queue_event.set()

    def _sanitize_music_queue_on_startup(self):
        """Called once during __init__. Resets stuck 'downloading' entries (the
        process was killed mid-download — partial file is incomplete, retry from
        scratch) and drops 'done' / 'cancelled' entries older than 1 hour so the
        queue doesn't grow unbounded across sessions."""
        try:
            q = self.settings.get('music_queue', []) or []
            now = int(time.time())
            cleaned = []
            for e in q:
                st = e.get('status')
                if st == 'downloading':
                    e['status'] = 'queued'
                    e['progress'] = 0
                    e['started_at'] = None
                    cleaned.append(e)
                elif st in ('done', 'cancelled'):
                    age = now - int(e.get('completed_at') or e.get('queued_at') or now)
                    if age < 3600:
                        cleaned.append(e)
                    # else: drop
                else:
                    cleaned.append(e)
            if cleaned != q:
                self.settings['music_queue'] = cleaned
                self._save_settings()
        except Exception as ex:
            print(f'[ProTube/music] queue sanitize failed: {ex}')

    def _emit_music_queue(self):
        """Push the current queue to the frontend so the Downloads panel
        re-renders. Called on every state change. The queue is small (<100 items
        in practice), so re-rendering the whole list is cheap and avoids diffing."""
        try:
            self._send_to_js('updateMusicQueue')
        except Exception:
            pass

    def _update_music_queue_entry(self, queue_id, patch):
        """Patch fields on a queue entry (matched by id), persist, and emit."""
        if not queue_id or not patch:
            return
        with self._music_queue_lock:
            q = self.settings.get('music_queue', []) or []
            changed = False
            for e in q:
                if e.get('id') == queue_id:
                    e.update(patch)
                    changed = True
                    break
            if changed:
                self.settings['music_queue'] = q
                self._save_settings()
        if changed:
            self._emit_music_queue()

    def _finalize_cancelled(self, queue_id):
        """Mark a queue entry as cancelled + clear the cancellation flag."""
        if not queue_id:
            return
        with self._music_queue_lock:
            self._music_queue_cancelled_ids.discard(queue_id)
        self._update_music_queue_entry(
            queue_id,
            {'status': 'cancelled', 'completed_at': int(time.time())},
        )

    def _music_queue_processor(self):
        """Background daemon: drains 'queued' entries up to
        `max_concurrent_music_downloads` at a time. Waits on
        `_music_queue_event` between drains — no busy-poll."""
        while True:
            try:
                self._music_queue_event.wait()
                # Clear immediately so wakes during this drain still trigger
                # another pass after we finish.
                self._music_queue_event.clear()
                self._drain_music_queue()
            except Exception as e:
                print(f'[ProTube/music] queue processor error: {e}')
                # Brief backoff so a persistent bug doesn't hot-loop.
                time.sleep(0.5)

    def _drain_music_queue(self):
        """Promote 'queued' entries to 'downloading' and spawn workers until we
        hit the concurrency cap. Each worker decrements the in-flight count + wakes
        the processor when it finishes so we pick up the next item."""
        while True:
            with self._music_queue_lock:
                if self._music_queue_active >= self.max_concurrent_music_downloads:
                    return
                q = self.settings.get('music_queue', []) or []
                next_entry = next((e for e in q if e.get('status') == 'queued'), None)
                if not next_entry:
                    return
                # Reserve a slot — flip its status here (the worker also sets it,
                # but doing it inside the lock avoids racing the next iteration).
                next_entry['status'] = 'downloading'
                next_entry['started_at'] = int(time.time())
                self.settings['music_queue'] = q
                self._save_settings()
                self._music_queue_active += 1
            self._emit_music_queue()
            queue_id = next_entry.get('id')
            url = next_entry.get('url')
            album_id = next_entry.get('album_id') or None
            threading.Thread(
                target=self._run_music_queue_worker,
                args=(url, album_id, queue_id),
                daemon=True,
            ).start()

    def _run_music_queue_worker(self, url, album_id, queue_id):
        """Wrap _music_download_worker so we always decrement the in-flight
        counter and re-wake the processor for the next item, even on exceptions."""
        try:
            self._music_download_worker(url, album_id=album_id, queue_id=queue_id)
        finally:
            with self._music_queue_lock:
                self._music_queue_active = max(0, self._music_queue_active - 1)
            self._music_queue_wake()

    def get_music_queue(self):
        """Frontend reads this on the Downloads tab mount and after every
        `updateMusicQueue` event."""
        return self.settings.get('music_queue', []) or []

    def cancel_music_queue_item(self, track_id):
        """If status is 'queued', drop from queue. If 'downloading', set the
        cancellation flag so the worker bails at the next progress tick."""
        if not track_id:
            return {'ok': False, 'error': 'no track_id'}
        with self._music_queue_lock:
            q = self.settings.get('music_queue', []) or []
            target = next((e for e in q if e.get('id') == track_id), None)
            if not target:
                return {'ok': False, 'error': 'not in queue'}
            was = target.get('status')
            if was == 'queued':
                # Drop immediately — worker never started for this one.
                self.settings['music_queue'] = [e for e in q if e.get('id') != track_id]
                self._save_settings()
            elif was == 'downloading':
                # Worker is running — flag for cancellation. The hook checks
                # this on every progress tick and raises to abort.
                self._music_queue_cancelled_ids.add(track_id)
            else:
                # done / failed / cancelled — nothing to cancel
                return {'ok': False, 'was_status': was}
        self._emit_music_queue()
        return {'ok': True, 'was_status': was}

    def clear_music_queue_done(self):
        """Drop all 'done' + 'cancelled' entries."""
        with self._music_queue_lock:
            q = self.settings.get('music_queue', []) or []
            kept = [e for e in q if e.get('status') not in ('done', 'cancelled')]
            cleared = len(q) - len(kept)
            if cleared:
                self.settings['music_queue'] = kept
                self._save_settings()
        if cleared:
            self._emit_music_queue()
        return {'cleared': cleared}

    def retry_music_queue_item(self, track_id):
        """Flip a 'failed' (or 'cancelled') item back to 'queued', clear error,
        wake the processor."""
        if not track_id:
            return {'ok': False, 'error': 'no track_id'}
        with self._music_queue_lock:
            q = self.settings.get('music_queue', []) or []
            target = next((e for e in q if e.get('id') == track_id), None)
            if not target:
                return {'ok': False, 'error': 'not in queue'}
            if target.get('status') not in ('failed', 'cancelled'):
                return {'ok': False, 'was_status': target.get('status')}
            target['status'] = 'queued'
            target['progress'] = 0
            target['error'] = None
            target['started_at'] = None
            target['completed_at'] = None
            self.settings['music_queue'] = q
            self._save_settings()
        self._music_queue_wake()
        self._emit_music_queue()
        return {'ok': True}

    def cancel_music_album_queued(self, album_id):
        """Drop all 'queued' entries that belong to this album. Lets any
        currently-downloading tracks finish. Returns how many were cancelled."""
        if not album_id:
            return {'cancelled': 0}
        with self._music_queue_lock:
            q = self.settings.get('music_queue', []) or []
            kept = []
            cancelled = 0
            for e in q:
                if e.get('album_id') == album_id and e.get('status') == 'queued':
                    cancelled += 1
                    continue
                kept.append(e)
            if cancelled:
                self.settings['music_queue'] = kept
                self._save_settings()
        if cancelled:
            self._emit_music_queue()
        return {'cancelled': cancelled}

    def set_max_concurrent_music_downloads(self, n):
        """Live-update the concurrency cap. Existing workers continue; new ones
        are spawned/throttled against the new value on the next drain."""
        try:
            n = max(1, min(8, int(n)))
        except Exception:
            return {'ok': False, 'error': 'invalid value'}
        self.max_concurrent_music_downloads = n
        self.settings['max_concurrent_music_downloads'] = n
        self._save_settings()
        self._music_queue_wake()
        return {'ok': True, 'value': n}

    def load_music_library(self):
        """Frontend reads this on the Music view's mount."""
        self._repair_music_thumbnails_from_album_cover()
        self._repair_album_artists_from_tracks()
        # Background-cache remote per-track thumbnails so album-view rows render
        # instantly. Async so the Music tab opens immediately; the swaps land
        # row-by-row as caches complete.
        try:
            t = threading.Thread(target=self._backfill_track_thumb_cache, daemon=True)
            t.start()
        except Exception:
            pass
        return self.settings.get('music_library', []) or []

    def _backfill_track_thumb_cache(self):
        """For every music_library track whose thumbnail is a remote URL,
        download + cache locally so subsequent renders use pt:thumb: markers
        (resolved from disk, no network)."""
        try:
            lib = self.settings.get('music_library', []) or []
            changed = False
            for t in lib:
                tb = t.get('thumbnail') or ''
                vid = t.get('id') or ''
                if not vid or not tb or tb.startswith('pt:thumb:'):
                    continue
                marker = self._cache_thumbnail(tb, vid)
                if marker.startswith('pt:thumb:'):
                    t['thumbnail'] = marker
                    changed = True
            if changed:
                with self._music_queue_lock:
                    self.settings['music_library'] = lib
                    self._save_settings()
        except Exception:
            pass

    def _extract_embedded_art(self, audio_path, dest_path):
        """Pull the embedded album-art image out of an audio file via ffmpeg.
        Returns True on success. We try this because YouTube's i9.ytimg.com
        album-cover URLs frequently 404 even with the signed query params
        yt-dlp captures, so the remote URL can't be trusted as a thumbnail
        source. The embedded art always works (yt-dlp puts it there)."""
        try:
            ffmpeg = self._find_ffmpeg_exe()
            if not ffmpeg or not os.path.isfile(audio_path):
                return False
            r = subprocess.run(
                [ffmpeg, '-y', '-i', audio_path, '-an', '-vcodec', 'copy', dest_path],
                capture_output=True, timeout=15,
                creationflags=(0x08000000 if sys.platform == 'win32' else 0),
            )
            return r.returncode == 0 and os.path.isfile(dest_path) and os.path.getsize(dest_path) > 100
        except Exception:
            return False

    def _ensure_album_cover_local(self, album):
        """If an album's cover_url is a remote URL (or absent), try to extract
        the embedded art from any downloaded track and rewrite cover_url to a
        local 'pt:thumb:' marker. Returns the new marker, or the existing
        cover_url if extraction wasn't possible."""
        if not album:
            return ''
        cur = album.get('cover_url') or ''
        if cur.startswith('pt:thumb:'):
            return cur
        aid = album.get('id') or ''
        if not aid:
            return cur
        safe_id = re.sub(r'[^a-zA-Z0-9_\-]', '_', f'alb_{aid}')[:80]
        local_path = os.path.join(self.thumbnail_cache_dir, f'{safe_id}.jpg')
        if not os.path.isfile(local_path) or os.path.getsize(local_path) <= 100:
            lib = self.settings.get('music_library', []) or []
            src = None
            for t in lib:
                if t.get('album_id') == aid and t.get('filepath') and os.path.isfile(t['filepath']):
                    src = t['filepath']
                    break
            if not src:
                return cur
            if not self._extract_embedded_art(src, local_path):
                return cur
        return f'pt:thumb:{safe_id}.jpg'

    def _repair_album_artists_from_tracks(self):
        """Existing albums saved before _resolve_album_artist had its track-
        derived fallback may show 'Unknown Artist'. Fix by walking the album's
        downloaded tracks and picking the most-common artist. Cheap — only
        touches albums whose artist is empty or literally 'Unknown Artist'."""
        try:
            albums = self.settings.get('music_albums', []) or []
            if not albums:
                return
            lib = self.settings.get('music_library', []) or []
            from collections import Counter
            changed = False
            for a in albums:
                cur = (a.get('artist') or '').strip()
                if cur and cur.lower() not in ('unknown artist', 'unknown'):
                    continue
                aid = a.get('id')
                if not aid:
                    continue
                tracks = [t for t in lib if t.get('album_id') == aid]
                counts = Counter()
                for t in tracks:
                    artist = (t.get('artist') or '').strip()
                    if artist and artist.lower() not in ('unknown artist', 'unknown', 'various artists', 'various'):
                        counts[artist] += 1
                if counts:
                    a['artist'] = counts.most_common(1)[0][0]
                    changed = True
            if changed:
                self.settings['music_albums'] = albums
                self._save_settings()
        except Exception:
            pass

    def _repair_music_thumbnails_from_album_cover(self):
        """Cheap-to-run repair: ensure every album has a local cover (extracted
        from a downloaded track's embedded art) and undo an earlier mistake
        where I overwrote per-track thumbnails with the album cover marker.
        Tracks should keep their own YouTube video thumbnail — that's what
        renders next to each song in the album detail view and in the player
        view; the album cover is only for the album card."""
        try:
            albums = self.settings.get('music_albums', []) or []
            if not albums:
                return
            changed_albums = False
            for a in albums:
                aid = a.get('id')
                if not aid:
                    continue
                new_cover = self._ensure_album_cover_local(a)
                if new_cover and new_cover != a.get('cover_url'):
                    a['cover_url'] = new_cover
                    changed_albums = True
            lib = self.settings.get('music_library', []) or []
            changed_lib = False
            for t in lib:
                # Undo the earlier wrong rewrite: if a track's thumbnail points
                # at an album cover marker (pt:thumb:alb_*), restore it to the
                # YouTube video thumb derived from the video id. i.ytimg.com's
                # hqdefault.jpg is reliable for any public YT video id.
                tb = t.get('thumbnail') or ''
                vid = t.get('id') or ''
                if vid and tb.startswith('pt:thumb:alb_'):
                    t['thumbnail'] = f'https://i.ytimg.com/vi/{vid}/hqdefault.jpg'
                    changed_lib = True
            if changed_albums:
                self.settings['music_albums'] = albums
            if changed_lib:
                self.settings['music_library'] = lib
            if changed_albums or changed_lib:
                self._save_settings()
        except Exception:
            pass

    def add_to_music_library(self, track):
        """Append/replace a track in the music library. Dedup by id (latest wins)."""
        if not track or not track.get('id'):
            return False
        lib = self.settings.get('music_library', []) or []
        lib = [t for t in lib if t.get('id') != track['id']]
        lib.append(track)
        self.settings['music_library'] = lib
        self._save_settings()
        return True

    def mark_music_seen(self, track_id):
        """Stamp the track as 'seen' so the NEW badge on its card disappears.
        Called when the user plays the track for the first time."""
        if not track_id:
            return {'ok': False}
        lib = self.settings.get('music_library', []) or []
        changed = False
        for t in lib:
            if t.get('id') == track_id and not t.get('seen_at'):
                t['seen_at'] = int(time.time())
                changed = True
                break
        if changed:
            self._save_settings()
        return {'ok': True}

    def remove_from_music_library(self, track_id):
        """Drop a track from the library. Doesn't delete the file on disk."""
        lib = self.settings.get('music_library', []) or []
        self.settings['music_library'] = [t for t in lib if t.get('id') != track_id]
        self._save_settings()
        return {'ok': True}

    def delete_music_track(self, track_id):
        """Drop from library AND delete the file. Hard remove."""
        lib = self.settings.get('music_library', []) or []
        target = next((t for t in lib if t.get('id') == track_id), None)
        if target and target.get('filepath') and os.path.exists(target['filepath']):
            try:
                os.remove(target['filepath'])
            except OSError as e:
                print(f'[ProTube/music] failed to delete {target["filepath"]}: {e}')
        self.settings['music_library'] = [t for t in lib if t.get('id') != track_id]
        self._save_settings()
        return {'ok': True}

    def hide_music_track(self, track_id, hidden=True):
        """Toggle a music track's `hidden` flag. Hidden tracks render dimmed
        with a 'Hidden' badge when the user has the 'Show hidden' toggle on,
        and don't render at all when it's off. Mirrors the video library's
        hide-card affordance. Returns the new state so the frontend can flip
        optimistically and reconcile."""
        if not track_id:
            return {'ok': False, 'hidden': False}
        lib = self.settings.get('music_library', []) or []
        changed = False
        new_state = bool(hidden)
        for t in lib:
            if t.get('id') == track_id:
                if bool(t.get('hidden')) != new_state:
                    if new_state:
                        t['hidden'] = True
                    else:
                        t.pop('hidden', None)
                    changed = True
                break
        if changed:
            self._save_settings()
        return {'ok': True, 'hidden': new_state}

    def bulk_hide_music_tracks(self, track_ids, hidden=True):
        """Apply hide_music_track to many ids in one shot (one save). Used by
        the album-card "Hide album" right-click action and any future multi-
        select flow."""
        if not track_ids:
            return {'ok': True, 'hidden': bool(hidden), 'count': 0}
        ids = set(track_ids)
        lib = self.settings.get('music_library', []) or []
        changed = False
        new_state = bool(hidden)
        count = 0
        for t in lib:
            if t.get('id') in ids:
                if bool(t.get('hidden')) != new_state:
                    if new_state:
                        t['hidden'] = True
                    else:
                        t.pop('hidden', None)
                    changed = True
                count += 1
        if changed:
            self._save_settings()
        return {'ok': True, 'hidden': new_state, 'count': count}

    def bulk_remove_music_tracks(self, track_ids):
        """Drop many tracks from the library in one save. Files on disk are
        preserved (mirrors the video library's 'Remove from library' bulk
        action — the file stays so a re-import can restore the entry).
        Returns the list of ids actually removed so the frontend can size its
        Undo toast accurately."""
        if not track_ids:
            return {'ok': True, 'removed': [], 'count': 0}
        ids = set(track_ids)
        lib = self.settings.get('music_library', []) or []
        removed = [t.get('id') for t in lib if t.get('id') in ids]
        if not removed:
            return {'ok': True, 'removed': [], 'count': 0}
        self.settings['music_library'] = [t for t in lib if t.get('id') not in ids]
        self._save_settings()
        return {'ok': True, 'removed': removed, 'count': len(removed)}

    def bulk_delete_music_tracks(self, track_ids):
        """Drop many tracks AND delete their files on disk. One settings save.
        Mirrors delete_music_track for each id but batched so the UI doesn't
        block on per-track round-trips. Returns counts so the frontend can
        report how many succeeded vs failed (locked files, etc.)."""
        if not track_ids:
            return {'ok': True, 'deleted': 0, 'errors': 0, 'count': 0}
        ids = set(track_ids)
        lib = self.settings.get('music_library', []) or []
        deleted = 0
        errors = 0
        for t in lib:
            if t.get('id') not in ids:
                continue
            fp = t.get('filepath') or ''
            if fp and os.path.exists(fp):
                try:
                    os.remove(fp)
                    deleted += 1
                except OSError as e:
                    errors += 1
                    print(f'[ProTube/music] failed to delete {fp}: {e}')
            else:
                # No file on disk to delete, but still drop the entry — count
                # it as deleted so the user sees the row vanish.
                deleted += 1
        self.settings['music_library'] = [t for t in lib if t.get('id') not in ids]
        self._save_settings()
        return {'ok': True, 'deleted': deleted, 'errors': errors, 'count': deleted + errors}

    # ----------------------------------------------------------------- #
    # Music albums (first-class library entity grouping multiple tracks) #
    # ----------------------------------------------------------------- #

    def load_music_albums(self):
        """Frontend reads this on the Music view's mount, alongside load_music_library."""
        return self.settings.get('music_albums', []) or []

    def get_music_album(self, album_id):
        """Return one album joined with its full track objects, ordered by
        track_ids. Missing tracks (still downloading) appear as placeholder
        stubs with `pending=True` so the detail view can render a greyed row."""
        if not album_id:
            return None
        albums = self.settings.get('music_albums', []) or []
        album = next((a for a in albums if a.get('id') == album_id), None)
        if not album:
            return None
        lib = self.settings.get('music_library', []) or []
        by_id = {t.get('id'): t for t in lib if t.get('id')}
        tracks = []
        for tid in album.get('track_ids', []) or []:
            t = by_id.get(tid)
            if t:
                tracks.append(t)
            else:
                # Pending stub — track not yet downloaded (or download failed).
                tracks.append({
                    'id': tid,
                    'type': 'music',
                    'title': '',          # will be filled once download lands
                    'artist': album.get('artist', ''),
                    'album': album.get('title', ''),
                    'album_id': album_id,
                    'thumbnail': album.get('cover_url', ''),
                    'duration_string': '',
                    'duration_seconds': 0,
                    'pending': True,
                })
        joined = dict(album)
        joined['tracks'] = tracks
        return joined

    def delete_music_album(self, album_id, delete_files=True):
        """Remove an album record. If delete_files is true, also remove every
        track in track_ids from music_library AND delete the underlying M4As.
        Singles stamped with this album_id (from a re-import flow) are also
        unlinked so they don't leak back into the top-level grid as orphans
        pointing at a deleted album."""
        if not album_id:
            return {'ok': False, 'error': 'no album_id'}
        albums = self.settings.get('music_albums', []) or []
        album = next((a for a in albums if a.get('id') == album_id), None)
        if not album:
            return {'ok': False, 'error': 'album not found'}
        if delete_files:
            lib = self.settings.get('music_library', []) or []
            track_id_set = set(album.get('track_ids') or [])
            # Delete files for tracks linked to this album (by id or album_id stamp).
            for t in lib:
                if t.get('id') in track_id_set or t.get('album_id') == album_id:
                    fp = t.get('filepath') or ''
                    if fp and os.path.exists(fp):
                        try:
                            os.remove(fp)
                        except OSError as e:
                            print(f'[ProTube/music] failed to delete {fp}: {e}')
            # Drop those entries from the library.
            self.settings['music_library'] = [
                t for t in lib
                if not (t.get('id') in track_id_set or t.get('album_id') == album_id)
            ]
        else:
            # Unlink without deleting: clear the album_id stamp so tracks remain
            # as singles in the top-level grid.
            lib = self.settings.get('music_library', []) or []
            for t in lib:
                if t.get('album_id') == album_id:
                    t.pop('album_id', None)
        # Drop the album record.
        self.settings['music_albums'] = [a for a in albums if a.get('id') != album_id]
        self._save_settings()
        return {'ok': True}

    def mark_album_seen(self, album_id):
        """Stamp the album as 'seen' so the NEW pill on its card disappears.
        Called when the user opens the album detail view."""
        if not album_id:
            return {'ok': False}
        albums = self.settings.get('music_albums', []) or []
        changed = False
        for a in albums:
            if a.get('id') == album_id and not a.get('seen_at'):
                a['seen_at'] = int(time.time())
                changed = True
                break
        if changed:
            self._save_settings()
        return {'ok': True}

    # ---- Internal album helpers -------------------------------------- #

    def _upsert_music_album(self, album_entry):
        """Insert or replace an album record by id. Latest write wins on the
        mutable fields (downloaded_count, status, etc.) but preserves the
        seen_at stamp from any existing record so reopening the album later
        doesn't re-light the NEW pill."""
        if not album_entry or not album_entry.get('id'):
            return False
        albums = self.settings.get('music_albums', []) or []
        existing = next((a for a in albums if a.get('id') == album_entry['id']), None)
        if existing:
            # Preserve seen_at + original added_at across re-downloads (e.g.
            # user re-adds an album after deleting it — the NEW pill will
            # re-light because added_at gets refreshed, which is the desired
            # behavior; but a partial re-resolve shouldn't reset seen_at if
            # the user has already opened it). We keep both fields from the
            # existing record when present.
            album_entry['seen_at'] = existing.get('seen_at') or album_entry.get('seen_at')
            album_entry['added_at'] = existing.get('added_at') or album_entry.get('added_at')
            albums = [a for a in albums if a.get('id') != album_entry['id']]
        albums.append(album_entry)
        self.settings['music_albums'] = albums
        self._save_settings()
        return True

    def _stamp_existing_tracks_with_album(self, track_ids, album_id):
        """When an album re-download starts and some tracks are already in the
        user's library as singles, stamp them with the album_id so they get
        absorbed into the album card instead of showing as duplicates."""
        if not album_id or not track_ids:
            return
        lib = self.settings.get('music_library', []) or []
        target = set(track_ids)
        changed = False
        for t in lib:
            if t.get('id') in target and t.get('album_id') != album_id:
                t['album_id'] = album_id
                changed = True
        if changed:
            self.settings['music_library'] = lib
            self._save_settings()

    def _album_downloaded_count(self, album_id):
        """Count how many of the album's track_ids are currently in the library."""
        albums = self.settings.get('music_albums', []) or []
        album = next((a for a in albums if a.get('id') == album_id), None)
        if not album:
            return 0
        track_id_set = set(album.get('track_ids') or [])
        lib = self.settings.get('music_library', []) or []
        return sum(1 for t in lib if t.get('id') in track_id_set)

    def _recount_album(self, album_id):
        """Recompute downloaded_count from the library and persist."""
        albums = self.settings.get('music_albums', []) or []
        album = next((a for a in albums if a.get('id') == album_id), None)
        if not album:
            return
        album['downloaded_count'] = self._album_downloaded_count(album_id)
        if album['downloaded_count'] >= album.get('total_tracks', 0) and album.get('total_tracks', 0) > 0:
            album['status'] = 'complete'
        self._save_settings()

    def _bump_album_progress(self, album_id):
        """A track from this album just landed. Increment the counter, emit a
        progress event, and flip to 'complete' status if we're done."""
        if not album_id:
            return
        albums = self.settings.get('music_albums', []) or []
        album = next((a for a in albums if a.get('id') == album_id), None)
        if not album:
            return
        # Always recount from library (cheap, ~hundreds of entries max) — avoids
        # off-by-one drift if a worker double-fires or a track is re-downloaded.
        album['downloaded_count'] = self._album_downloaded_count(album_id)
        total = album.get('total_tracks', 0) or 0
        if total and album['downloaded_count'] >= total:
            album['status'] = 'complete'
        self._save_settings()
        self._send_to_js(
            'updateMusicAlbumProgress',
            album_id,
            album['downloaded_count'],
            total,
        )

    def _mark_album_complete_if_done(self, album_id):
        """Flip status to 'complete' if all tracks are already in library
        (used for the case where every track was already owned before download)."""
        if not album_id:
            return
        albums = self.settings.get('music_albums', []) or []
        album = next((a for a in albums if a.get('id') == album_id), None)
        if not album:
            return
        done = self._album_downloaded_count(album_id)
        total = album.get('total_tracks', 0) or 0
        album['downloaded_count'] = done
        if total and done >= total:
            album['status'] = 'complete'
        self._save_settings()
        self._send_to_js('updateMusicAlbumProgress', album_id, done, total)

    def get_music_stream_url(self, track_id):
        """Return a localhost-served URL for an M4A in the music library so the
        <audio> element can play it. Reuses the existing video server (same port,
        same `/v?p=<base64-path>` URL scheme — works for audio files too because
        the server just streams bytes with Range + CORS headers)."""
        try:
            if not self._video_server_port:
                return {'error': 'Stream server not running'}
            lib = self.settings.get('music_library', []) or []
            target = next((t for t in lib if t.get('id') == track_id), None)
            if not target:
                return {'error': 'Track not in music library'}
            fp = target.get('filepath') or ''
            if not fp or not os.path.exists(fp):
                return {'error': 'Audio file missing from disk'}
            import base64 as _b64
            encoded = _b64.urlsafe_b64encode(fp.encode('utf-8')).decode('ascii')
            return {
                'url': f'http://127.0.0.1:{self._video_server_port}/v?p={encoded}',
                'filepath': fp,
                'title': target.get('title', ''),
                'artist': target.get('artist', ''),
                'album': target.get('album', ''),
                'thumbnail': target.get('thumbnail', ''),
                'duration_seconds': target.get('duration_seconds', 0),
            }
        except Exception as e:
            return {'error': f'Stream prep failed: {e}'}

    def _innertube_parse_lockup(self, lv, dedup):
        """Newer 'lockupViewModel' shape YouTube returns for playlists when the
        Playlists filter is active. Best-effort extraction; falls back to skipping
        if we can't pin down a playlist ID.

        Real-world shape (sampled 2026-05):
          contentId: 'PLmNvVoj...' (the playlist ID directly — no VL prefix)
          contentType: 'LOCKUP_CONTENT_TYPE_PLAYLIST'
          metadata.lockupMetadataViewModel.title.content: '<title>'
          contentImage.collectionThumbnailViewModel.primaryThumbnail.thumbnailViewModel.image.sources[]
        """
        try:
            content_type = lv.get('contentType') or ''
            if 'PLAYLIST' not in content_type.upper():
                return None
            pid = lv.get('contentId') or ''
            if not pid:
                return None
            meta = lv.get('metadata', {}).get('lockupMetadataViewModel', {})
            title = (meta.get('title') or {}).get('content') or ''
            # Thumbnail
            thumb = ''
            image = (lv.get('contentImage') or {}).get('collectionThumbnailViewModel', {})
            primary = (image.get('primaryThumbnail') or {}).get('thumbnailViewModel', {})
            thumbs = (primary.get('image') or {}).get('sources') or []
            if thumbs:
                thumb = thumbs[-1].get('url') or ''
            # Video count — look for the badge overlay text "15 videos"
            vcount_text = ''
            overlays = (primary.get('overlays') or [])
            for ov in overlays:
                badge_vm = (ov.get('thumbnailOverlayBadgeViewModel') or {})
                badges = badge_vm.get('thumbnailBadges') or []
                for b in badges:
                    bvm = (b.get('thumbnailBadgeViewModel') or {})
                    if bvm.get('text'):
                        vcount_text = bvm['text']
                        break
                if vcount_text:
                    break
            return {
                'id': pid,
                'type': 'playlist',
                'title': title or 'Untitled',
                'uploader': '',
                'url': f'https://www.youtube.com/playlist?list={pid}',
                'thumbnail': thumb,
                'duration_string': vcount_text,
                'view_count_string': '',
                'in_queue': pid in dedup['queue_playlist_ids'],
                'in_library': pid in dedup['lib_playlist_ids'],
            }
        except Exception:
            return None

    def _normalize_channel_url_str(self, url):
        """Collapse channel URL variants to one key for dedup."""
        try:
            return (url or '').rstrip('/').replace('/videos', '').lower()
        except Exception:
            return ''

    def _format_view_count(self, n):
        """1234567 → '1.2M views'."""
        try:
            n = int(n)
            if n >= 1_000_000_000:
                return f'{n/1_000_000_000:.1f}B views'.replace('.0B', 'B')
            if n >= 1_000_000:
                return f'{n/1_000_000:.1f}M views'.replace('.0M', 'M')
            if n >= 1_000:
                return f'{n/1_000:.1f}K views'.replace('.0K', 'K')
            return f'{n} views'
        except Exception:
            return ''

    def find_channel_for_video(self, video_id):
        """Look up the canonical channel URL for a library video AND check whether that
        channel is already queued or in the library. Returns:
            {url: <channel-url>, already: <{id, title} or null>, source: 'stamped'|'probed'}
            or {error: ...}

        Resolution priority:
        1. `uploader_url` / `channel_url` stamped on the entry at fetch time.
        2. Fresh yt-dlp probe of the video's watch URL. yt-dlp returns the REAL channel
           URL (e.g. https://www.youtube.com/@MertYerlikaya) regardless of how messy the
           display name is — this is the only reliable path for older entries that pre-date
           the uploader_url stamping. Result is cached on the entry for next time.

        We deliberately removed the "derive @handle from display name" fallback — it was
        wrong often enough (display names rarely match handles for channels with spaces or
        rebranded handles) that the user lost trust in the feature.
        """
        try:
            entry = self._find_library_entry(video_id)
            if not entry:
                return {'error': 'video not in library'}

            channel_url = entry.get('uploader_url') or entry.get('channel_url')
            source = 'stamped' if channel_url else None

            # Fallback: probe yt-dlp on the video's watch URL to extract the real channel URL.
            # We use extract_flat='in_playlist' + skip downloads, so this is just metadata.
            if not channel_url and entry.get('url'):
                try:
                    probe_opts = self._get_ydl_opts('browser', 'none')
                    probe_opts.update({
                        'quiet': True,
                        'no_warnings': True,
                        'skip_download': True,
                        'extract_flat': False,  # we want the full channel_url field
                    })
                    with YoutubeDL(probe_opts) as ydl:
                        info = ydl.extract_info(entry['url'], download=False)
                    channel_url = (info.get('uploader_url')
                                   or info.get('channel_url'))
                    if channel_url:
                        source = 'probed'
                        # Stamp on entry so we don't probe again next time.
                        entry['uploader_url'] = channel_url
                        self._save_settings()
                except Exception as e:
                    print(f'[ProTube] channel probe failed for {video_id}: {e}')

            if not channel_url:
                return {'error': 'Could not find this channel URL. Try opening the video on YouTube and pasting the channel URL manually.'}

            # Smart-already check against both queue AND library (channels can live in either).
            normalized = channel_url.rstrip('/').replace('/videos', '').lower()
            this_uploader = (entry.get('uploader') or '').strip().lower()
            already = None
            sources = [
                ('queue', self.settings.get('queue', [])),
                ('library', self.settings.get('library', [])),
            ]
            for source_name, items in sources:
                for item in items:
                    if item.get('type') != 'playlist':
                        continue
                    item_url = (item.get('url') or '').rstrip('/').replace('/videos', '').lower()
                    if item_url and item_url == normalized:
                        already = {'id': item.get('id'), 'title': item.get('title') or 'Channel', 'where': source_name}
                        break
                    if (item.get('subtype') == 'channel'
                            and item.get('uploader')
                            and this_uploader
                            and item['uploader'].strip().lower() == this_uploader):
                        already = {'id': item.get('id'), 'title': item.get('title') or 'Channel', 'where': source_name}
                        break
                if already:
                    break

            return {'url': channel_url, 'already': already, 'source': source}
        except Exception as e:
            return {'error': f'channel lookup failed: {e}'}

    def chat_about_video(self, video_id, question, history=None):
        """Answer a question about a video using its transcript + the conversation history.
        history is a list of {role: 'user'|'assistant', content: str} (capped on the frontend
        so we don't blow the context window). Returns {'reply': str} or {'error': str}."""
        try:
            entry = self._find_library_entry(video_id)
            if not entry:
                return {'error': 'video not in library'}

            api_key = self.settings.get('groq_api_key', '').strip()
            if not api_key:
                return {'error': 'No Groq API key. Add one in Settings → AI.'}

            raw = self.get_subtitles_for_video(video_id)
            if 'error' in raw:
                return {'error': 'Need subtitles to chat about this video. Re-download it to fetch them.'}

            transcript = self._vtt_to_plain_text(raw['vtt'])
            if not transcript:
                return {'error': 'Transcript is empty.'}

            # Cap transcript to leave room for prompt + history + answer.
            transcript = transcript[:60000]
            title = entry.get('title') or 'YouTube video'
            uploader = entry.get('uploader') or ''

            from groq_client import GroqClient, GroqError
            client = GroqClient(api_key)

            system = (
                "You answer questions about a specific YouTube video using its transcript as "
                "the source of truth. Ground every answer in what the video actually says.\n\n"
                "RULES:\n"
                "- Answer concisely (1-3 short paragraphs unless the question demands more).\n"
                "- Quote or paraphrase specific points from the video — name people, numbers, "
                "examples that appear in it.\n"
                "- If the video doesn't address the question, say so plainly: \"The video doesn't "
                "cover that.\" Don't make things up.\n"
                "- Use markdown: short paragraphs, bullet points if listing, **bold** for key "
                "terms. No headers (the answer is short).\n"
                "- Don't preface with \"Great question!\" or similar fluff. Just answer."
            )

            # Build the messages array: system, transcript context, then chat history, then new question.
            # The transcript is provided as a one-off context message so it doesn't get repeated in
            # every turn — but each call still includes it because Groq is stateless.
            user_context = (
                f"VIDEO TITLE: {title}\n"
                f"CHANNEL: {uploader}\n\n"
                f"TRANSCRIPT:\n{transcript}\n\n"
                f"---\nThe user will now ask questions about this video. Use the transcript above "
                f"as your source of truth."
            )

            messages = [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': user_context},
                {'role': 'assistant', 'content': 'Got it. I have the transcript and I\'m ready for your questions.'},
            ]

            # Append prior turns (cap to last 8 to keep context tight)
            if isinstance(history, list):
                for turn in history[-8:]:
                    role = turn.get('role')
                    content = (turn.get('content') or '').strip()
                    if role in ('user', 'assistant') and content:
                        messages.append({'role': role, 'content': content})

            # Append the current question
            messages.append({'role': 'user', 'content': str(question or '').strip()})

            # Direct call (bypass GroqClient.chat which only takes system+user).
            try:
                resp = requests.post(
                    GroqClient.URL,
                    json={
                        'model': client.model,
                        'messages': messages,
                        'max_tokens': 1200,
                        'temperature': 0.4,
                    },
                    headers={
                        'Authorization': f'Bearer {api_key}',
                        'Content-Type': 'application/json',
                    },
                    timeout=60,
                )
            except requests.RequestException as e:
                return {'error': f'Network error: {e}'}

            if resp.status_code == 401:
                return {'error': 'Groq rejected the API key.'}
            if resp.status_code == 429:
                return {'error': 'Rate limit hit. Try again in a minute.'}
            if resp.status_code != 200:
                return {'error': f'Groq HTTP {resp.status_code}'}

            try:
                data = resp.json()
                reply = data['choices'][0]['message']['content'].strip()
            except (ValueError, KeyError, IndexError) as e:
                return {'error': f'Bad response from Groq: {e}'}

            return {'reply': reply}
        except Exception as e:
            return {'error': f'chat failed: {e}'}

    def set_setting(self, key, value):
        """Generic settings write. Frontend uses this for things like migration markers."""
        self.settings[key] = value
        self._save_settings()
        return True

    def set_video_hidden(self, video_id, hidden=True):
        """Mark a library entry as hidden so it's filtered out of the default
        library view. Hidden items are still in the library AND on disk — this
        is purely a display preference. Toggle off via hidden=False.

        Works on top-level entries (video or playlist) and on playlist children.
        Returns {ok: bool, error?: str}.
        """
        lib = self.settings.get('library', [])
        target = None
        for v in lib:
            if v.get('id') == video_id:
                target = v
                break
            if v.get('type') == 'playlist':
                for c in v.get('videos', []):
                    if c.get('id') == video_id:
                        target = c
                        break
                if target:
                    break
        if not target:
            return {'ok': False, 'error': 'Library entry not found'}
        target['hidden'] = bool(hidden)
        self._save_settings()
        return {'ok': True}

    def set_video_pinned(self, video_id, pinned=True):
        """Toggle the `pinned` flag on a top-level library entry. Pinned items
        sort to the top of the library grid. Frontend handles the visual
        ordering — backend just stores the flag and timestamp.

        We stamp `pinned_at` so multiple pinned items sort by pin-time
        (most-recently-pinned on top). Unpinning clears both fields.

        Pinning is for top-level library entries only (videos + playlists).
        Playlist children aren't pinnable individually — they live inside
        their parent and pin the whole entry to elevate them.
        """
        lib = self.settings.get('library', [])
        for v in lib:
            if v.get('id') == video_id:
                if pinned:
                    v['pinned'] = True
                    v['pinned_at'] = int(time.time())
                else:
                    v.pop('pinned', None)
                    v.pop('pinned_at', None)
                self._save_settings()
                return {'ok': True}
        return {'ok': False, 'error': 'Library entry not found'}

    def set_videos_pinned_batch(self, video_ids, pinned=True):
        """Bulk pin/unpin top-level library entries. One pass through the
        library, ONE settings.json write at the end. Same shape as
        set_videos_hidden_batch — returns the ids actually flipped (skips
        items already in the target state) so the Undo only reverses what
        changed.

        Pinning playlist children isn't supported here (set_video_pinned
        rejects them too) — pin the whole entry to elevate its position.
        """
        if not video_ids:
            return {'ok': True, 'flipped': []}
        wanted = {vid for vid in video_ids if vid}
        if not wanted:
            return {'ok': True, 'flipped': []}
        flipped = []
        now = int(time.time())
        for v in self.settings.get('library', []):
            if v.get('id') in wanted:
                if pinned and not v.get('pinned'):
                    v['pinned'] = True
                    v['pinned_at'] = now
                    flipped.append(v.get('id'))
                elif not pinned and v.get('pinned'):
                    v.pop('pinned', None)
                    v.pop('pinned_at', None)
                    flipped.append(v.get('id'))
        if flipped:
            self._save_settings()
        return {'ok': True, 'flipped': flipped}

    def set_videos_hidden_batch(self, video_ids, hidden=True):
        """Bulk hide/unhide. One pass through the library, ONE settings.json
        write at the end. Avoids the "49 sequential awaits, each saving the
        whole 1MB+ settings.json" pattern that was failing silently for big
        selections — each save took a moment and the JSON bridge was getting
        backed up.

        video_ids: list of ids to flip. Missing ids are skipped silently
                   (callers shouldn't have to filter ahead of time).
        hidden: True to hide, False to unhide.

        Returns {ok: True, flipped: [ids actually changed]}. Items already in
        the target state are NOT counted as flipped, so the frontend's Undo
        toast only reverses the ones it actually changed.
        """
        if not video_ids:
            return {'ok': True, 'flipped': []}
        wanted_set = {vid for vid in video_ids if vid}
        if not wanted_set:
            return {'ok': True, 'flipped': []}
        target_state = bool(hidden)
        flipped = []
        lib = self.settings.get('library', [])
        for v in lib:
            if v.get('id') in wanted_set:
                if bool(v.get('hidden')) != target_state:
                    v['hidden'] = target_state
                    flipped.append(v.get('id'))
            if v.get('type') == 'playlist':
                for c in v.get('videos', []):
                    if c.get('id') in wanted_set:
                        if bool(c.get('hidden')) != target_state:
                            c['hidden'] = target_state
                            flipped.append(c.get('id'))
        if flipped:
            self._save_settings()
        return {'ok': True, 'flipped': flipped}

    def set_max_concurrent_downloads(self, n):
        """Settings drawer setter. Clamps to [1, 8] and replaces the semaphore so
        new downloads queue against the new limit. In-flight downloads keep the
        old semaphore reference and finish naturally — they don't get killed.
        Persisted to settings.json so the limit survives restarts."""
        try:
            n = max(1, min(8, int(n)))
        except (TypeError, ValueError):
            n = 2
        self.max_concurrent_downloads = n
        self.download_semaphore = threading.Semaphore(n)
        self.settings['max_concurrent_downloads'] = n
        self._save_settings()
        return n

    def _user_default_quality(self):
        """Translate the user's `default_quality` setting (Settings drawer) into
        a string the queue picker understands. The picker only knows resolutions
        ('2160p', '1440p', '1080p', '720p', '480p'); 'best' and 'audio' are
        Settings-only abstractions that we resolve here.
        - 'best' → '2160p' (yt-dlp auto-picks best available <= this)
        - 'audio' → '1080p' (audio-only is a per-video Extra toggle, not a quality)
        - resolution string → use as-is
        - unset / unknown → '1080p'
        """
        pref = self.settings.get('default_quality') or '1080p'
        if pref == 'best':
            return '2160p'
        if pref == 'audio':
            return '1080p'
        if pref in ('2160p', '1440p', '1080p', '720p', '480p'):
            return pref
        return '1080p'

    def get_about_info(self):
        """Snapshot for the Settings drawer's About section.
        - version: app version (bumped per release in __version__ at top of file)
        - ytdlp_version: whichever yt-dlp the runtime ended up using (auto-updated
          copy in data/yt-dlp-runtime/ takes precedence over the bundled one)
        - library_count: number of top-level library entries (videos + playlists)
        - library_video_count: flattened — playlists' children count individually
        - library_size_bytes: sum of selected-quality file sizes from sizeMap
        - queue_count: number of items currently in queue
        """
        try:
            from yt_dlp.version import __version__ as ytdlp_ver
        except Exception:
            ytdlp_ver = 'unknown'

        lib = self.settings.get('library', [])
        queue = self.settings.get('queue', [])

        library_video_count = 0
        library_size_bytes = 0
        for item in lib:
            if item.get('type') == 'playlist':
                children = item.get('videos', [])
                library_video_count += len(children)
                for c in children:
                    q = c.get('selectedQuality')
                    sm = c.get('sizeMap') or {}
                    if q and isinstance(sm.get(q), (int, float)):
                        library_size_bytes += sm[q]
            else:
                library_video_count += 1
                q = item.get('selectedQuality')
                sm = item.get('sizeMap') or {}
                if q and isinstance(sm.get(q), (int, float)):
                    library_size_bytes += sm[q]

        # Data dir — surfaced so the Settings drawer's "Open data folder" button
        # has a path to hand to open_folder(). Computed via app_paths so it
        # works in both dev and frozen-PyInstaller modes.
        try:
            from app_paths import data_dir as _dd
            data_dir_path = _dd()
        except Exception:
            data_dir_path = ''

        return {
            'version': __version__,
            'ytdlp_version': ytdlp_ver,
            'library_count': len(lib),
            'library_video_count': library_video_count,
            'library_size_bytes': library_size_bytes,
            'queue_count': len(queue),
            'max_concurrent_downloads': self.max_concurrent_downloads,
            'data_dir': data_dir_path,
        }

    def _fetch_update_manifest_landing(self):
        """Hit the landing site's version.json. Returns (error_str, data_dict) —
        exactly one of the two is set. data_dict already matches the contract
        check_for_updates() consumes (latest, downloadUrl, downloadSizeMB,
        releaseNotes, releasedAt)."""
        url = self.settings.get('update_check_url') or LANDING_VERSION_URL_DEFAULT
        try:
            resp = requests.get(url, timeout=6, headers={'Cache-Control': 'no-cache'})
            if resp.status_code != 200:
                return f'HTTP {resp.status_code}', None
            return None, resp.json()
        except Exception as e:
            return str(e), None

    def _fetch_update_manifest_github(self):
        """Hit GitHub's Releases API and adapt the response to the same shape
        the landing-site fetcher returns. Picks the first asset whose name ends
        in .exe as the download. Public repo → no auth needed; GitHub's
        anonymous quota (60 req/IP/hour) is plenty since we throttle to once
        per 24h via the on-disk cache anyway.

        Returns (error_str, data_dict) — exactly one of the two is set."""
        url = self.settings.get('github_releases_url') or GITHUB_RELEASES_URL_DEFAULT
        try:
            resp = requests.get(url, timeout=6, headers={
                'Accept': 'application/vnd.github+json',
                'Cache-Control': 'no-cache',
            })
            if resp.status_code != 200:
                return f'GitHub HTTP {resp.status_code}', None
            gh = resp.json()
        except Exception as e:
            return f'GitHub: {e}', None

        # GitHub tag convention is 'v1.2.0' — strip the v so the comparator
        # sees the same shape it gets from version.json's bare '1.2.0'.
        tag = str(gh.get('tag_name') or '').strip()
        latest = tag[1:] if tag.lower().startswith('v') else tag

        download_url = ''
        download_size_mb = None
        for asset in (gh.get('assets') or []):
            name = (asset.get('name') or '').lower()
            if name.endswith('.exe'):
                download_url = asset.get('browser_download_url') or ''
                size_bytes = asset.get('size') or 0
                if size_bytes:
                    download_size_mb = round(size_bytes / (1024 * 1024), 1)
                break

        return None, {
            'latest': latest or '0.0.0',
            'downloadUrl': download_url,
            'downloadSizeMB': download_size_mb,
            'releaseNotes': gh.get('body') or '',
            'releasedAt': gh.get('published_at') or '',
        }

    def check_for_updates(self, force=False):
        """Compare local __version__ against the landing site's version.json
        and report whether a newer release is available.

        Throttled to once per 24h via data/update_check.json so we don't hit
        the site on every launch. Pass force=True (e.g., from the settings
        drawer's "Check now" button) to bypass the cache.

        Returns:
            {has_update: True, current, latest, downloadUrl, downloadSizeMB,
             releaseNotes, releasedAt}     — when newer version is published
            {has_update: False, current, latest}                  — up to date
            {has_update: False, current, error: str}              — fetch failed
        """
        try:
            from app_paths import data_dir
            state_path = os.path.join(data_dir(), 'update_check.json')
        except Exception:
            state_path = None

        # Cache hit?
        if not force and state_path and os.path.exists(state_path):
            try:
                with open(state_path, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                age = time.time() - state.get('checked_at', 0)
                if age < 86400 and state.get('result'):
                    cached = state['result']
                    # The cache file is keyed by time only, but the user might
                    # have upgraded between writes — e.g., v1.0.0 cached
                    # `has_update: true, latest: 1.0.1`, then the user installed
                    # v1.0.1, and the new exe reads the OLD cache. Trusting
                    # `has_update` verbatim would spam an upgrade pill on a
                    # version that's already current. Re-evaluate the comparison
                    # against the CURRENT __version__ before returning.
                    latest = str(cached.get('latest', '0.0.0')).strip()
                    try:
                        cached['has_update'] = parse(latest) > parse(__version__)
                    except Exception:
                        cached['has_update'] = latest > __version__
                    cached['current'] = __version__
                    cached['cached'] = True
                    return cached
            except Exception:
                pass  # corrupt cache file, just re-fetch

        source = (self.settings.get('update_source') or 'github').lower()
        if source == 'github':
            fetch_err, data = self._fetch_update_manifest_github()
        else:
            fetch_err, data = self._fetch_update_manifest_landing()
        if fetch_err:
            return {'has_update': False, 'current': __version__, 'error': fetch_err}

        latest = str(data.get('latest', '0.0.0')).strip()
        try:
            has_update = parse(latest) > parse(__version__)
        except Exception:
            # Fallback to lex-compare if version strings don't parse cleanly
            has_update = latest > __version__

        result = {
            'has_update': bool(has_update),
            'current': __version__,
            'latest': latest,
            'downloadUrl': data.get('downloadUrl', ''),
            'downloadSizeMB': data.get('downloadSizeMB'),
            'releaseNotes': data.get('releaseNotes', ''),
            'releasedAt': data.get('releasedAt', ''),
            'source': source,
        }

        # Persist the result so the next 24 launches don't re-fetch
        if state_path:
            try:
                with open(state_path, 'w', encoding='utf-8') as f:
                    json.dump({'checked_at': time.time(), 'result': result}, f)
            except Exception:
                pass

        return result

    def get_active_progress(self):
        """Return current progress snapshot for any in-flight downloads. Used by the frontend
        after the window regains focus to re-sync any progress UI that may have missed
        ticks during background throttling."""
        out = []
        for vid in list(self.active_downloads.keys()):
            info = self._last_progress.get(vid) if hasattr(self, '_last_progress') else None
            if info:
                out.append({
                    'id': vid,
                    'pct': info.get('pct'),
                    'speed': info.get('speed'),
                    'playlist_id': info.get('playlist_id')
                })
        return out

    # ============================================================
    # Library — permanent collection of completed downloads.
    # Data shape: same as queue video entries, but with status always 'Done'
    # plus optional 'missing': True if the file no longer exists on disk.
    # ============================================================
    def load_library(self):
        """Return the full library. Runs migration on first call if needed.
        Also kicks off a background pass that extracts frame thumbnails for
        any imported videos that lack one — covers pre-existing imports from
        before the auto-frame feature existed."""
        if not self.settings.get('_library_migrated'):
            self._migrate_queue_done_to_library()
        # Idempotent — no-ops if there's nothing pending or worker already running
        try:
            self._start_frame_extraction_worker()
        except Exception:
            pass
        return self.settings.get('library', [])

    def save_library(self, lib):
        """Overwrite the library. Frontend calls this after reorder/remove operations."""
        self.settings['library'] = lib
        self._save_settings()

    def add_to_library(self, video):
        """Append a single video (or playlist with all children Done) to the library."""
        if not video or not video.get('id'):
            return False
        lib = self.settings.get('library', [])
        # Stamp added_at so we can show "NEW" badge for recently-added videos.
        # Only stamp if not already present (preserves the original add date on undo).
        if not video.get('added_at'):
            video['added_at'] = int(time.time())
        # Dedupe by id — if somehow already in the library, replace it (latest wins)
        lib = [v for v in lib if v.get('id') != video['id']]
        lib.append(video)
        self.settings['library'] = lib
        # If this was previously removed (archived by filepath), pop the archive
        # entry — the live library is now canonical again.
        archive = self.settings.get('library_archive')
        if archive:
            arc_key = self._archive_key(video.get('filepath'))
            if arc_key and arc_key in archive:
                try:
                    del archive[arc_key]
                except KeyError:
                    pass
        self._save_settings()
        return True

    def _archive_key(self, filepath):
        """Normalize a filepath into the canonical key used by library_archive."""
        if not filepath:
            return None
        try:
            return os.path.normcase(os.path.normpath(filepath))
        except (TypeError, ValueError):
            return None

    def _archive_entry(self, video):
        """Snapshot a library entry into settings['library_archive'] keyed by filepath
        so a later import-from-folder can restore the original metadata (url, thumbnail,
        uploader, formats, etc.) instead of rebuilding a sparse ffprobe-only entry."""
        if not video:
            return
        archive = self.settings.setdefault('library_archive', {})
        if video.get('type') == 'playlist':
            for child in video.get('videos', []):
                key = self._archive_key(child.get('filepath'))
                if key:
                    archive[key] = child
        else:
            key = self._archive_key(video.get('filepath'))
            if key:
                archive[key] = video

    def remove_from_library(self, video_id):
        """Remove a single entry from the library by id. Does not delete the file on disk.
        Archives the entry's metadata by filepath so re-importing the file later restores
        the original title/thumbnail/url instead of rebuilding a sparse ffprobe entry."""
        lib = self.settings.get('library', [])
        for v in lib:
            if v.get('id') == video_id:
                self._archive_entry(v)
                break
        self.settings['library'] = [v for v in lib if v.get('id') != video_id]
        self._save_settings()
        return True

    def delete_video_from_library_and_disk(self, video_id):
        """Hard delete: remove the entry AND delete the file from disk. For STANDALONE
        videos (not part of a playlist), also delete the containing folder since each
        standalone video lives in its own folder. For playlist CHILDREN, leave the
        folder alone — siblings may still live in the playlist's folder structure.

        Errors are non-fatal; the entry is removed from the library either way so the
        user isn't stuck with phantom entries."""
        lib = self.settings.get('library', [])
        target = None
        is_playlist_child = False
        for v in lib:
            if v.get('id') == video_id:
                target = v
                break
            if v.get('type') == 'playlist':
                for c in v.get('videos', []):
                    if c.get('id') == video_id:
                        target = c
                        is_playlist_child = True
                        break
                if target:
                    break
        if not target:
            return {'ok': False, 'error': 'Video not found in library'}

        deleted_files = []
        skipped_files = []
        deleted_folder = None

        # Delete the video file itself
        filepath = target.get('filepath')
        if filepath and os.path.exists(filepath):
            try:
                os.remove(filepath)
                deleted_files.append(filepath)
            except OSError as e:
                skipped_files.append({'path': filepath, 'reason': str(e)})

        # For standalone videos: each lives in its own folder (download_folder/title/file.mp4).
        # After removing the file, the folder usually still has the .info.json, .description,
        # subtitles, thumbnail variants etc. We delete the whole folder for standalones.
        # For playlist children: leave the folder alone — peer videos may share it via the
        # playlist's folder structure, and we don't want surprises.
        if not is_playlist_child and filepath:
            v_folder = os.path.dirname(filepath)
            # Safety: never delete the user's main download folder (only sub-folders of it)
            if v_folder and os.path.isdir(v_folder):
                try:
                    download_root = os.path.abspath(self.download_folder)
                    target_folder = os.path.abspath(v_folder)
                    if target_folder != download_root and target_folder.startswith(download_root + os.sep):
                        shutil.rmtree(v_folder, ignore_errors=False)
                        deleted_folder = v_folder
                except OSError as e:
                    skipped_files.append({'path': v_folder, 'reason': str(e)})

        # Delete the cached thumbnail (a 'pt:thumb:<id>.<ext>' marker resolves to a
        # local path under thumbnail_cache_dir).
        thumb_marker = target.get('thumbnail') or ''
        if thumb_marker.startswith('pt:thumb:'):
            thumb_filename = thumb_marker[len('pt:thumb:'):]
            thumb_path = os.path.join(self.thumbnail_cache_dir, thumb_filename)
            if os.path.exists(thumb_path):
                try:
                    os.remove(thumb_path)
                    deleted_files.append(thumb_path)
                except OSError as e:
                    skipped_files.append({'path': thumb_path, 'reason': str(e)})

        # Remove from library — top-level OR playlist child.
        if is_playlist_child:
            for v in lib:
                if v.get('type') == 'playlist':
                    v['videos'] = [c for c in v.get('videos', []) if c.get('id') != video_id]
        else:
            self.settings['library'] = [v for v in lib if v.get('id') != video_id]
        self._save_settings()

        return {
            'ok': True,
            'deleted': deleted_files,
            'deleted_folder': deleted_folder,
            'skipped': skipped_files,
            'is_playlist_child': is_playlist_child,
        }

    def _classify_playlist_url(self, url):
        """Return 'channel' for YouTube channel-style URLs, 'playlist' otherwise.

        Channel patterns (videos tab is the canonical re-fetch target):
          youtube.com/@handle[/videos]
          youtube.com/c/customname[/videos]
          youtube.com/channel/UCxxx[/videos]
          youtube.com/user/legacyname[/videos]

        Playlist patterns:
          youtube.com/playlist?list=PLxxx
          youtube.com/watch?v=xxx&list=PLxxx
        """
        if not url:
            return 'playlist'
        u = url.lower()
        # Channel handle, custom URL, channel ID, or legacy user URL
        if '/@' in u or '/channel/' in u or '/c/' in u or '/user/' in u:
            return 'channel'
        return 'playlist'

    def add_playlist_to_library(self, playlist):
        """Move a fully-completed playlist to the library as a single entry with all its children.
        Called by the frontend when it detects a playlist has all children marked Done.
        Dedupes by playlist id and strips missing-file children."""
        if not playlist or not playlist.get('id'):
            return False
        # Backfill subtype from URL for entries fetched before subtype existed.
        if not playlist.get('subtype'):
            playlist['subtype'] = self._classify_playlist_url(playlist.get('url', ''))
        # Filter children: only Done and selected ones, and only those whose files exist
        children = playlist.get('videos', [])
        surviving = []
        for c in children:
            if c.get('selected') is False:
                continue
            if c.get('status') != 'Done':
                continue
            if self._is_file_missing(c):
                continue
            # Cache child thumbnail locally for offline use
            remote = c.get('thumbnail')
            if remote and remote.startswith('http'):
                c['thumbnail'] = self._cache_thumbnail(remote, c.get('id'))
            surviving.append(c)
        if not surviving:
            return False
        playlist_copy = {**playlist, 'videos': surviving, 'status': 'Done'}
        # Stamp added_at if not present so the "NEW" badge works for playlists too.
        if not playlist_copy.get('added_at'):
            playlist_copy['added_at'] = int(time.time())
        # Also stamp each child if not stamped
        for c in playlist_copy['videos']:
            if not c.get('added_at'):
                c['added_at'] = playlist_copy['added_at']
        lib = self.settings.get('library', [])
        lib = [v for v in lib if v.get('id') != playlist['id']]
        lib.append(playlist_copy)
        self.settings['library'] = lib
        self._save_settings()
        return True

    def check_playlist_updates(self, playlist_id):
        """Re-fetch a playlist/channel via yt-dlp flat-playlist and diff against
        the entry's current children. Used by the "Check for updates" UI on the
        playlist detail panels — works for entries in EITHER the library (fully
        downloaded playlists) OR the queue (partially-downloaded playlists and
        channels that were just pasted in). Most playlists live in the queue
        because users rarely download every video, and channels keep growing
        forever — so checking queue is the common case.

        Returns:
          {ok: True, source: 'library'|'queue',
           new: [{id, url, title, uploader, thumbnail, duration_string}, ...],
           removed_ids: [...], total_now: int, last_checked_at: int, subtype: str}
          {ok: False, error: str}

        Side effect for library entries: stamps last_checked_at on the entry. Queue
        entries don't get stamped because the queue is owned by the frontend and
        re-saved on its own cadence — stamping here would race with frontend writes.
        """
        target = None
        source = None
        for v in self.settings.get('library', []):
            if v.get('id') == playlist_id and v.get('type') == 'playlist':
                target, source = v, 'library'
                break
        if not target:
            for v in self.settings.get('queue', []):
                if v.get('id') == playlist_id and v.get('type') == 'playlist':
                    target, source = v, 'queue'
                    break
        if not target:
            return {'ok': False, 'error': 'Playlist not found'}

        url = target.get('url')
        if not url:
            return {'ok': False, 'error': 'Playlist has no source URL — cannot re-fetch'}

        local_ids = {c.get('id') for c in target.get('videos', []) if c.get('id')}

        try:
            # extract_flat is the cheap call (IDs + titles only, no per-video
            # format probe). lazy_playlist=True turns probe['entries'] into a
            # generator so we can iterate page-by-page and report progress as
            # entries arrive, instead of blocking until yt-dlp finishes the walk.
            opts = self._get_ydl_opts('browser', 'none')
            opts.update({
                'extract_flat': True,
                'skip_download': True,
                'lazy_playlist': True,
            })
            with YoutubeDL(opts) as ydl:
                probe = ydl.extract_info(url, download=False)

                # Stream INSIDE the YoutubeDL context so any per-page fetches
                # complete before the session closes. The count we send to the
                # UI is "new videos found so far" (not in local library) — that
                # matches what the user actually cares about. Counting all
                # scanned entries instead would balloon to channel-total-size
                # for users who keep their library curated, and it's misleading.
                #
                # Defensive: dedupe by video id and skip null/no-id stubs.
                # yt-dlp's lazy iterator can yield empty or duplicate items
                # for some YouTube paginators, which inflates raw counts wildly
                # (we saw 100x on a small channel). Dedupe makes the count
                # match reality regardless of what yt-dlp emits.
                entries = []
                seen_ids = set()
                new_count = 0
                for e in (probe.get('entries') or []):
                    if not e:
                        continue
                    vid_id = e.get('id')
                    if not vid_id or vid_id in seen_ids:
                        continue
                    seen_ids.add(vid_id)
                    entries.append(e)
                    if vid_id not in local_ids:
                        new_count += 1
                        if new_count == 1 or new_count % 5 == 0:
                            try:
                                self._send_to_js('onUpdateCheckProgress', playlist_id, new_count)
                            except Exception:
                                pass
                # Final tick so the count lands on the real number even if it
                # didn't fall on a %5 boundary.
                try:
                    self._send_to_js('onUpdateCheckProgress', playlist_id, new_count)
                except Exception:
                    pass
        except Exception as e:
            return {'ok': False, 'error': f'Fetch failed: {e}'}

        remote_ids = seen_ids

        new_entries = []
        for e in entries:
            vid_id = e.get('id')
            if not vid_id or vid_id in local_ids:
                continue
            thumb = None
            thumbs = e.get('thumbnails') or []
            if thumbs:
                thumb = thumbs[-1].get('url')
            elif e.get('thumbnail'):
                thumb = e.get('thumbnail')
            else:
                thumb = f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg"
            new_entries.append({
                'id': vid_id,
                'url': e.get('url') or e.get('webpage_url') or f'https://www.youtube.com/watch?v={vid_id}',
                'title': e.get('title', 'Untitled'),
                'uploader': e.get('uploader') or e.get('channel') or target.get('uploader', ''),
                'thumbnail': thumb,
                'duration_string': self._format_duration(e.get('duration')),
            })

        removed_ids = sorted(local_ids - remote_ids)
        last_checked_at = int(time.time())

        # Stamp + persist only for library entries. The queue is owned by the
        # frontend (see save_queue / load_queue) and writing to it here would
        # race with the frontend's saveQueueState calls.
        if source == 'library':
            target['last_checked_at'] = last_checked_at
            if not target.get('subtype'):
                target['subtype'] = self._classify_playlist_url(url)
            self._save_settings()

        return {
            'ok': True,
            'source': source,
            'new': new_entries,
            'removed_ids': removed_ids,
            'total_now': len(remote_ids),
            'last_checked_at': last_checked_at,
            'subtype': target.get('subtype') or self._classify_playlist_url(url),
        }

    def add_videos_to_library_playlist(self, target_playlist_id, videos):
        """Append downloaded videos to an existing library playlist/channel entry.
        Called by the frontend after a "Check for updates" download batch finishes,
        to merge the temp-playlist children into their original library home.

        Dedupes by video id (already-present videos are skipped). Stamps added_at
        on each newly-appended child so the NEW badge fires.

        Returns {ok: bool, added: int, error?: str}.
        """
        if not target_playlist_id or not videos:
            return {'ok': False, 'error': 'Missing target id or videos'}
        lib = self.settings.get('library', [])
        target = None
        for v in lib:
            if v.get('id') == target_playlist_id and v.get('type') == 'playlist':
                target = v
                break
        if not target:
            return {'ok': False, 'error': 'Target playlist not found in library'}

        existing_ids = {c.get('id') for c in target.get('videos', []) if c.get('id')}
        now = int(time.time())
        new_children = []
        for v in videos:
            vid_id = v.get('id')
            if not vid_id or vid_id in existing_ids:
                continue
            child = dict(v)
            # Match the shape of children placed by add_playlist_to_library —
            # cache thumbnail locally, stamp added_at, mark as Done playlist child.
            remote = child.get('thumbnail')
            if remote and isinstance(remote, str) and remote.startswith('http'):
                child['thumbnail'] = self._cache_thumbnail(remote, vid_id)
            child['added_at'] = now
            child['isFromPlaylist'] = True
            child['playlistTitle'] = target.get('title', '')
            child['status'] = 'Done'
            new_children.append(child)
            existing_ids.add(vid_id)

        # Prepend — these are the newest videos from the source. YouTube's flat
        # playlist response returns reverse-chronological for channels' Videos tabs
        # and playlist order otherwise, both of which mean "new stuff goes on top."
        target['videos'] = new_children + target.get('videos', [])
        target['videoCount'] = len(target['videos'])
        target['last_updated_at'] = now
        self._save_settings()
        return {'ok': True, 'added': len(new_children)}

    def check_library_files(self):
        """
        Scan every library entry and mark 'missing: True' on any whose file is gone.
        Returns a list of ids that are newly missing so the UI can refresh them.
        Also silently self-heals broken filepaths where the real file exists in the folder.
        Called periodically (or on library view mount) to catch user-deleted files.
        """
        lib = self.settings.get('library', [])
        newly_missing = []
        changed = False
        for v in lib:
            if v.get('type') == 'playlist':
                for child in v.get('videos', []):
                    was_missing = child.get('missing', False)
                    prev_fp = child.get('filepath')
                    is_missing = self._is_file_missing(child)  # may mutate filepath
                    if child.get('filepath') != prev_fp:
                        changed = True
                    child['missing'] = is_missing
                    if is_missing and not was_missing:
                        newly_missing.append(child['id'])
                        changed = True
                    elif not is_missing and was_missing:
                        changed = True
            else:
                was_missing = v.get('missing', False)
                prev_fp = v.get('filepath')
                is_missing = self._is_file_missing(v)
                if v.get('filepath') != prev_fp:
                    changed = True
                v['missing'] = is_missing
                if is_missing and not was_missing:
                    newly_missing.append(v['id'])
                    changed = True
                elif not is_missing and was_missing:
                    changed = True
        if changed:
            self._save_settings()
        return newly_missing

    def _is_file_missing(self, video):
        """True if this video claims to have a filepath but the file is gone from disk.
        Self-healing: if the recorded filepath is an intermediate stream (e.g. .f140.m4a)
        or a .part file that no longer exists, look in the same folder for the real
        merged video file. If we find one, silently UPDATE the video's filepath and
        return False. This fixes entries created when the download path was wrong."""
        fp = video.get('filepath')
        if not fp:
            # No filepath means we never tracked one — can't verify, assume OK
            return False

        if os.path.exists(fp):
            # But even if it exists — if it's an intermediate stream, try to upgrade to merged
            if self._looks_intermediate(fp) or fp.endswith('.part'):
                folder = os.path.dirname(fp)
                merged = self._find_merged_output(folder, video.get('title', ''))
                if merged and merged != fp:
                    video['filepath'] = merged
                    # Folder is fine as-is
            return False

        # File doesn't exist at recorded path. Try to find the real file in the folder.
        folder = video.get('folderpath') or os.path.dirname(fp)
        if folder and os.path.isdir(folder):
            merged = self._find_merged_output(folder, video.get('title', ''))
            if merged:
                video['filepath'] = merged
                return False

        return True

    def debug_queue_status(self):
        """Dump what's in queue and library for debugging. Called by frontend on demand."""
        queue = self.settings.get('queue', [])
        library = self.settings.get('library', [])
        migrated = self.settings.get('_library_migrated', False)

        summary = {
            'migrated': migrated,
            'queue_count': len(queue),
            'library_count': len(library),
            'queue_items': [],
            'library_items': []
        }
        for item in queue:
            if item.get('type') == 'playlist':
                children_status = [c.get('status') for c in item.get('videos', [])]
                summary['queue_items'].append({
                    'id': item.get('id'),
                    'title': item.get('title'),
                    'type': 'playlist',
                    'children_status': children_status,
                    'done_children': sum(1 for s in children_status if s == 'Done')
                })
            else:
                summary['queue_items'].append({
                    'id': item.get('id'),
                    'title': item.get('title'),
                    'type': 'video',
                    'status': item.get('status'),
                    'has_filepath': bool(item.get('filepath'))
                })
        for item in library:
            summary['library_items'].append({
                'id': item.get('id'),
                'title': item.get('title'),
                'type': item.get('type', 'video'),
                'has_filepath': bool(item.get('filepath')),
                'missing': item.get('missing', False)
            })
        return summary

    def force_remigrate(self):
        """Clear migration flag and re-run. For testing / recovery when migration failed."""
        self.settings['_library_migrated'] = False
        self._save_settings()
        result = self._migrate_queue_done_to_library()
        return result

    def repair_library(self):
        """One-shot cleanup pass over the library. Fixes:
          - Entries duplicated across imported + downloaded flows (same filepath/title)
          - Entries with stale intermediate filepaths (.f140.m4a etc.) — heals to merged .mp4
          - Entries whose filepath doesn't exist and has no recoverable folder (removed)
          - Playlist entries with 0 or 1 children that aren't real playlists (flattened or dropped)
          - Items in library that are ALSO in queue as non-Done (they belong only in queue)

        Returns counts so the UI can summarize what happened."""
        lib = self.settings.get('library', [])
        queue = self.settings.get('queue', [])

        # Build queue id set for cross-check (items paused/queued in queue shouldn't be in library)
        queue_non_done_ids = set()
        for q in queue:
            if q.get('type') == 'playlist':
                for c in q.get('videos', []):
                    if c.get('status') != 'Done':
                        queue_non_done_ids.add(c.get('id'))
            else:
                if q.get('status') != 'Done':
                    queue_non_done_ids.add(q.get('id'))

        fixed_paths = 0
        dropped_broken = 0
        dropped_queue_conflict = 0
        dedupe_merged = 0
        flattened_playlists = 0

        new_lib = []
        seen_filepaths = {}  # normalized filepath -> index in new_lib

        def normalize_fp(fp):
            if not fp:
                return None
            try:
                return os.path.normcase(os.path.normpath(fp))
            except Exception:
                return fp

        for v in lib:
            # Rule: if this id exists in queue as non-Done, it doesn't belong in library
            if v.get('id') in queue_non_done_ids:
                dropped_queue_conflict += 1
                continue

            if v.get('type') == 'playlist':
                children = v.get('videos', [])
                # Heal each child's filepath + drop children we can't find
                good_children = []
                for c in children:
                    is_missing = self._is_file_missing(c)  # may heal filepath in-place
                    if c.get('filepath') and is_missing:
                        # Path is genuinely broken — drop the child
                        continue
                    good_children.append(c)

                if len(good_children) == 0:
                    # Empty playlist is garbage — drop it
                    dropped_broken += 1
                    continue
                if len(good_children) == 1:
                    # "Playlist" with 1 child is just a standalone — flatten it
                    only = good_children[0]
                    flat = {**only, 'isFromPlaylist': False}
                    flat.pop('playlistTitle', None)
                    # Dedupe by filepath with existing entries
                    fp_key = normalize_fp(flat.get('filepath'))
                    if fp_key and fp_key in seen_filepaths:
                        dedupe_merged += 1
                    else:
                        if fp_key:
                            seen_filepaths[fp_key] = len(new_lib)
                        new_lib.append(flat)
                        flattened_playlists += 1
                    continue

                # Real playlist — keep with healed children
                v['videos'] = good_children
                new_lib.append(v)
                continue

            # Single video
            prev_fp = v.get('filepath')
            is_missing = self._is_file_missing(v)  # self-heals filepath
            if v.get('filepath') != prev_fp:
                fixed_paths += 1

            # If it has a filepath AND the filepath is still bad, drop it
            if v.get('filepath') and is_missing:
                dropped_broken += 1
                continue

            # Dedupe by filepath
            fp_key = normalize_fp(v.get('filepath'))
            if fp_key and fp_key in seen_filepaths:
                # Merge: keep the richer entry (one with a url / thumbnail / uploader)
                idx = seen_filepaths[fp_key]
                existing = new_lib[idx]
                # Prefer entry with more metadata
                better = v if _richness(v) > _richness(existing) else existing
                new_lib[idx] = better
                dedupe_merged += 1
                continue

            if fp_key:
                seen_filepaths[fp_key] = len(new_lib)
            new_lib.append(v)

        self.settings['library'] = new_lib
        self._save_settings()
        return {
            'fixed_paths': fixed_paths,
            'dropped_broken': dropped_broken,
            'dropped_queue_conflict': dropped_queue_conflict,
            'deduped': dedupe_merged,
            'flattened_playlists': flattened_playlists,
            'final_count': len(new_lib)
        }

    # ============================================================
    # YouTube refetch — enriches imported library entries by searching YouTube
    # for their titles and pulling thumbnail, channel, and real URL.
    # ============================================================
    def refetch_count_needs(self):
        """Count library entries that would benefit from a refetch (imported + missing URL
        or missing thumbnail). Called by the UI to show 'Refetch 37 videos' or similar."""
        lib = self.settings.get('library', [])
        count = 0
        for v in lib:
            if v.get('type') == 'playlist':
                for c in v.get('videos', []):
                    if self._needs_refetch(c):
                        count += 1
            else:
                if self._needs_refetch(v):
                    count += 1
        return count

    def _needs_refetch(self, video):
        """True if a video entry would benefit from a refetch.
        Either missing URL/thumbnail entirely, OR was previously refetched with the
        old (less accurate) v1 matcher and hasn't been upgraded yet."""
        if not video.get('imported') and not video.get('_refetched'):
            # Native ProTube downloads have everything they need from the start
            return False
        if not video.get('url'):
            return True
        if not video.get('thumbnail'):
            return True
        # Items refetched with v1 matcher get re-run by v2 (better matching)
        if video.get('_refetched') and not video.get('_refetched_v2'):
            return True
        return False

    def _extract_youtube_id(self, url):
        """Pull the 11-character video ID out of a YouTube URL. Returns None if the URL
        isn't a recognizable YouTube URL. Handles youtube.com/watch?v=..., youtu.be/...,
        youtube.com/shorts/..., and youtube.com/embed/... formats."""
        if not url:
            return None
        try:
            import re as _re
            patterns = [
                r'(?:youtube\.com/watch\?(?:[^&]*&)*v=)([A-Za-z0-9_\-]{11})',
                r'(?:youtu\.be/)([A-Za-z0-9_\-]{11})',
                r'(?:youtube\.com/shorts/)([A-Za-z0-9_\-]{11})',
                r'(?:youtube\.com/embed/)([A-Za-z0-9_\-]{11})',
                r'(?:youtube\.com/v/)([A-Za-z0-9_\-]{11})',
            ]
            for p in patterns:
                m = _re.search(p, url)
                if m:
                    return m.group(1)
        except Exception:
            pass
        return None

    def _fetch_by_id(self, video_id):
        """Fetch metadata for a specific YouTube video by ID. This is the most reliable
        path — we know exactly which video to grab, no search-and-guess. Returns dict
        with url/thumbnail/uploader/duration_string/matched_title or {'error': ...}."""
        if not video_id or len(video_id) != 11:
            return {'error': 'Invalid video ID'}
        opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'noprogress': True,
        }
        if self.ffmpeg_location:
            opts['ffmpeg_location'] = self.ffmpeg_location
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f'https://www.youtube.com/watch?v={video_id}', download=False)
        except Exception as e:
            return {'error': f'Direct fetch failed: {str(e)[:120]}'}
        if not info:
            return {'error': 'No info returned'}

        thumbnail = info.get('thumbnail') or ''
        if not thumbnail:
            thumbs = info.get('thumbnails') or []
            if thumbs:
                thumbnail = thumbs[-1].get('url', '')

        return {
            'url': info.get('webpage_url') or f'https://www.youtube.com/watch?v={video_id}',
            'thumbnail': thumbnail,
            'uploader': info.get('uploader') or info.get('channel') or '',
            'duration_string': info.get('duration_string') or self._format_duration(info.get('duration')) or '',
            'matched_title': info.get('title') or '',
            'duration_seconds': info.get('duration') or 0,
            'method': 'id',
        }

    def _title_similarity(self, a, b):
        """Returns 0.0-1.0 similarity between two title strings. Uses character-level
        ratio after normalization (lowercased, special chars stripped). Robust to typos,
        emoji removal during yt-dlp filename sanitization, etc."""
        if not a or not b:
            return 0.0
        import re as _re
        from difflib import SequenceMatcher
        def norm(s):
            s = s.lower()
            # Strip non-alphanumeric except spaces (mirrors yt-dlp's restriction roughly)
            s = _re.sub(r'[^a-z0-9 ]+', ' ', s)
            s = _re.sub(r'\s+', ' ', s).strip()
            return s
        return SequenceMatcher(None, norm(a), norm(b)).ratio()

    def _search_and_extract(self, title, target_duration_sec=None):
        """Search YouTube for a title and pick the BEST match using a scoring function.
        Considers: title similarity, duration match (if we know the local file's duration),
        channel name presence. Refuses to pick a result that scores too low — better to
        flag 'no good match' than to confidently pick the wrong video.

        Returns metadata dict on success, or {'error': '...'} on failure."""
        # ytsearch5: returns top 5 candidates so we can score and pick the best
        search_url = f'ytsearch5:{title}'
        opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'extract_flat': False,
            'noprogress': True,
        }
        if self.ffmpeg_location:
            opts['ffmpeg_location'] = self.ffmpeg_location

        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(search_url, download=False)
        except Exception as e:
            return {'error': f'Search failed: {str(e)[:120]}'}

        entries = info.get('entries') if info else None
        if not entries:
            return {'error': 'No search results'}

        # Score each candidate
        best = None
        best_score = 0.0
        for entry in entries:
            if not entry:
                continue
            entry_title = entry.get('title') or ''
            entry_duration = entry.get('duration') or 0

            # Title similarity (0.0 - 1.0). Weight: 0.7
            title_sim = self._title_similarity(title, entry_title)

            # Duration similarity (0.0 - 1.0). Weight: 0.3
            # If we have a target duration and the candidate's duration matches within
            # 5% (or 5 seconds for short videos), give full points. Otherwise scale linearly.
            duration_sim = 0.5  # neutral if no target
            if target_duration_sec and target_duration_sec > 0 and entry_duration > 0:
                diff = abs(target_duration_sec - entry_duration)
                tolerance = max(5, target_duration_sec * 0.05)
                if diff <= tolerance:
                    duration_sim = 1.0
                elif diff <= target_duration_sec * 0.20:
                    duration_sim = 0.6
                else:
                    duration_sim = max(0.0, 1.0 - (diff / target_duration_sec))

            score = (title_sim * 0.7) + (duration_sim * 0.3)
            if score > best_score:
                best_score = score
                best = entry

        # Threshold: refuse to commit to a match that's clearly wrong.
        # title_sim alone of 0.5 means "rough word overlap" — below that is suspect.
        # Combined with the weights, a total score of 0.45 is the floor.
        if not best or best_score < 0.45:
            return {'error': f'No confident match (best score {best_score:.2f})'}

        thumbnail = best.get('thumbnail') or ''
        if not thumbnail:
            thumbs = best.get('thumbnails') or []
            if thumbs:
                thumbnail = thumbs[-1].get('url', '')

        return {
            'url': best.get('webpage_url') or best.get('url') or '',
            'thumbnail': thumbnail,
            'uploader': best.get('uploader') or best.get('channel') or '',
            'duration_string': best.get('duration_string') or self._format_duration(best.get('duration')) or '',
            'matched_title': best.get('title') or '',
            'duration_seconds': best.get('duration') or 0,
            'method': 'search',
            'score': round(best_score, 3),
        }

    def _resolve_video_metadata(self, video_entry):
        """Single entry point that decides which strategy to use and returns metadata.
        Tries Layer 1 (direct ID lookup) first if we have a URL with extractable ID.
        Falls back to Layer 2 (scored title search). Probes local file duration if
        we don't have one stored, to improve match accuracy."""
        # Layer 1 — direct ID lookup
        existing_url = video_entry.get('url') or ''
        yt_id = self._extract_youtube_id(existing_url)
        if yt_id:
            result = self._fetch_by_id(yt_id)
            if not result.get('error'):
                return result
            # If ID lookup failed (private video, deleted, network) fall through to search

        # Layer 2 — scored search. Use file duration as additional signal if available.
        target_duration = None
        # Already have duration_string? parse it back to seconds
        dur_str = video_entry.get('duration_string') or ''
        if dur_str:
            target_duration = self._parse_duration_string(dur_str)
        # No stored duration but we have a filepath? Probe the file via ffprobe.
        if not target_duration and video_entry.get('filepath'):
            target_duration = self._probe_duration(video_entry['filepath'])

        title = video_entry.get('title', '').strip()
        if not title:
            return {'error': 'No title to search with'}

        return self._search_and_extract(title, target_duration_sec=target_duration)

    def _parse_duration_string(self, dur_str):
        """Parse '1:23:45' or '12:34' or '34' into seconds. Returns None if unparseable."""
        if not dur_str:
            return None
        try:
            parts = str(dur_str).strip().split(':')
            parts = [int(p) for p in parts]
            if len(parts) == 3:
                return parts[0] * 3600 + parts[1] * 60 + parts[2]
            elif len(parts) == 2:
                return parts[0] * 60 + parts[1]
            elif len(parts) == 1:
                return parts[0]
        except (ValueError, AttributeError):
            pass
        return None

    def refetch_single(self, video_id):
        """Refetch metadata for a single library entry by id. Searches YouTube by title,
        grabs the top result, updates the library entry. Returns the updated entry or
        an error dict. Used for individual retry if bulk refetch mismatches."""
        lib = self.settings.get('library', [])
        target = None
        target_container = None  # reference for saving the mutation
        for v in lib:
            if v.get('id') == video_id:
                target = v
                target_container = v
                break
            if v.get('type') == 'playlist':
                for c in v.get('videos', []):
                    if c.get('id') == video_id:
                        target = c
                        break
                if target:
                    break
        if not target:
            return {'error': 'Video not found in library', 'id': video_id}

        result = self._resolve_video_metadata(target)
        if result.get('error'):
            return result

        # Merge result into target; preserve filepath, folderpath, id, title (don't overwrite
        # local title with whatever YouTube has — user might have renamed the file)
        if result.get('url'):
            target['url'] = result['url']
        if result.get('thumbnail'):
            # Helper respects frame_thumbnail_forced (user-pinned frames stay)
            # and clears frame_thumbnail_auto (real thumb beats the fallback).
            self._apply_refetched_thumbnail(target, result['thumbnail'])
        if result.get('uploader'):
            target['uploader'] = result['uploader']
        if result.get('duration_string') and not target.get('duration_string'):
            target['duration_string'] = result['duration_string']
        target['_refetched'] = True
        target['_refetched_v2'] = True
        target['_refetch_method'] = result.get('method', 'unknown')
        if 'score' in result:
            target['_refetch_score'] = result['score']

        self._save_settings()
        return {
            'id': video_id,
            'updated': True,
            'matched_title': result.get('matched_title', ''),
            'method': result.get('method'),
            'score': result.get('score'),
        }

    def fix_metadata_from_url(self, video_id, youtube_url):
        """User-driven manual fix: given a library entry id and the correct YouTube URL,
        fetch metadata for THAT exact video and update the library entry. This is the
        bulletproof fix path — no search, no scoring, just direct lookup of the user's
        pasted URL. Returns a result dict with updated metadata or {'error': ...}."""
        if not youtube_url or not isinstance(youtube_url, str):
            return {'error': 'No URL provided'}
        youtube_url = youtube_url.strip()

        # Find the library entry
        lib = self.settings.get('library', [])
        target = None
        for v in lib:
            if v.get('id') == video_id:
                target = v
                break
            if v.get('type') == 'playlist':
                for c in v.get('videos', []):
                    if c.get('id') == video_id:
                        target = c
                        break
                if target:
                    break
        if not target:
            return {'error': 'Video not found in library'}

        # Extract the YouTube ID from the user's URL
        yt_id = self._extract_youtube_id(youtube_url)
        if not yt_id:
            return {'error': 'That URL doesn\'t look like a YouTube video link'}

        # Fetch metadata for that specific video
        result = self._fetch_by_id(yt_id)
        if result.get('error'):
            return result

        # Apply the metadata to the library entry. Title is updated too here
        # because the user is explicitly opting in to "this is the right video"
        # and wants the official title.
        if result.get('url'):
            target['url'] = result['url']
        if result.get('thumbnail'):
            # force_refresh — user explicitly asked for the new thumbnail; bypass
            # the "already cached" shortcut so we actually re-download.
            self._apply_refetched_thumbnail(target, result['thumbnail'], force_refresh=True)
        if result.get('uploader'):
            target['uploader'] = result['uploader']
        if result.get('duration_string'):
            target['duration_string'] = result['duration_string']
        if result.get('matched_title'):
            target['title'] = result['matched_title']
        target['_refetched'] = True
        target['_refetched_v2'] = True
        target['_refetch_method'] = 'manual'

        self._save_settings()
        return {
            'id': video_id,
            'updated': True,
            'title': result.get('matched_title', ''),
            'uploader': result.get('uploader', ''),
            'url': result.get('url', ''),
        }

    def force_refetch_now(self):
        """Force-clear the v2 flag and immediately re-run refetch with the new matcher.
        Also clears _refetched_v2 from every library item so they all re-process. Useful
        when user wants to manually retrigger after auto-refetch didn't run or didn't help."""
        self.settings.pop('_library_refetched_v2', None)
        lib = self.settings.get('library', [])
        cleared = 0
        for v in lib:
            if v.get('type') == 'playlist':
                for c in v.get('videos', []):
                    if c.pop('_refetched_v2', None):
                        cleared += 1
            else:
                if v.pop('_refetched_v2', None):
                    cleared += 1
        self._save_settings()
        # Now run the refetch
        return self.refetch_all()

    def refetch_all(self):
        """Bulk refetch for all library entries that need it. This is a blocking call —
        the frontend should show a spinner/progress toast while it runs. Returns a summary
        with counts of successes, failures, and the updated library."""
        lib = self.settings.get('library', [])
        updated_count = 0
        failed = []

        # Flatten the targets so we hit playlist children too
        targets = []
        for v in lib:
            if v.get('type') == 'playlist':
                for c in v.get('videos', []):
                    if self._needs_refetch(c):
                        targets.append(c)
            else:
                if self._needs_refetch(v):
                    targets.append(v)

        total = len(targets)
        method_counts = {'id': 0, 'search': 0}
        for i, target in enumerate(targets):
            self._send_to_js('refetchProgress', i + 1, total, target.get('title', ''))

            title = target.get('title', '').strip()
            if not title:
                failed.append({'id': target.get('id'), 'reason': 'no title'})
                continue

            try:
                result = self._resolve_video_metadata(target)
                if result.get('error'):
                    failed.append({
                        'id': target.get('id'),
                        'title': title[:60],
                        'reason': result['error']
                    })
                    continue
                if result.get('url'):
                    target['url'] = result['url']
                if result.get('thumbnail'):
                    self._apply_refetched_thumbnail(target, result['thumbnail'])
                if result.get('uploader'):
                    target['uploader'] = result['uploader']
                if result.get('duration_string') and not target.get('duration_string'):
                    target['duration_string'] = result['duration_string']
                target['_refetched'] = True
                target['_refetched_v2'] = True
                method = result.get('method', 'unknown')
                target['_refetch_method'] = method
                if 'score' in result:
                    target['_refetch_score'] = result['score']
                method_counts[method] = method_counts.get(method, 0) + 1
                updated_count += 1
            except Exception as e:
                failed.append({'id': target.get('id'), 'reason': str(e)[:100]})

        self._save_settings()
        self._send_to_js('refetchComplete', updated_count, len(failed))
        return {
            'total': total,
            'updated': updated_count,
            'failed_count': len(failed),
            'failed': failed[:10],
            'methods': method_counts,
        }

    def _cache_thumbnail(self, remote_url, video_id, force_refresh=False):
        """Download a thumbnail image and save it locally. Returns an opaque marker that
        the frontend can send back to get_thumbnail_data() to retrieve the actual image
        bytes. We DON'T return a file:// URL because pywebview's webview blocks loading
        local files as <img src>. Instead we use a scheme 'pt:thumb:<video_id>' that the
        frontend resolves on render by calling back to the Python API for base64 data.

        force_refresh=True bypasses the "already cached" shortcut — used when we KNOW
        the remote thumbnail has changed (e.g. user manually fixed metadata to point at
        a different YouTube video). Without this, a stale cached image stays forever.

        On failure, returns the remote URL (still works online) or empty string."""
        if not remote_url or not video_id:
            return remote_url or ''
        safe_id = re.sub(r'[^a-zA-Z0-9_\-]', '_', str(video_id))[:80]
        if not safe_id:
            return remote_url

        # Guess extension from URL
        ext = '.jpg'
        low = remote_url.lower().split('?')[0]
        for candidate in ('.webp', '.png', '.jpg', '.jpeg'):
            if low.endswith(candidate):
                ext = candidate
                break

        local_path = os.path.join(self.thumbnail_cache_dir, f'{safe_id}{ext}')

        # Reuse cached file unless caller explicitly asked for a fresh download.
        if not force_refresh and os.path.exists(local_path) and os.path.getsize(local_path) > 100:
            return f'pt:thumb:{safe_id}{ext}'

        # When force_refresh, also clear any older-extension cached files for this id
        # so a stale .webp doesn't sit alongside a new .jpg
        if force_refresh:
            for old_ext in ('.webp', '.png', '.jpg', '.jpeg'):
                stale = os.path.join(self.thumbnail_cache_dir, f'{safe_id}{old_ext}')
                if stale != local_path and os.path.exists(stale):
                    try:
                        os.remove(stale)
                    except OSError:
                        pass

        try:
            resp = requests.get(remote_url, timeout=15, stream=True)
            if resp.status_code != 200:
                return remote_url
            with open(local_path, 'wb') as f:
                for chunk in resp.iter_content(8192):
                    if chunk:
                        f.write(chunk)
            if os.path.getsize(local_path) < 100:
                try:
                    os.remove(local_path)
                except OSError:
                    pass
                return remote_url
            return f'pt:thumb:{safe_id}{ext}'
        except Exception:
            return remote_url

    def migrate_file_thumbnails_to_markers(self):
        """One-time migration: existing library entries may have thumbnails stored as
        'file:///C:/Users/.../thumbnails/abc.jpg' URLs that the webview can't render.
        Convert those to 'pt:thumb:abc.jpg' markers which the frontend resolves through
        get_thumbnail_data(). Returns the count that was migrated."""
        lib = self.settings.get('library', [])
        changed = 0

        def convert_one(entry):
            nonlocal changed
            thumb = entry.get('thumbnail') or ''
            if thumb.startswith('file:///') and 'thumbnails/' in thumb.replace('\\', '/'):
                # Extract filename after 'thumbnails/'
                normalized = thumb.replace('\\', '/')
                idx = normalized.rfind('thumbnails/')
                if idx >= 0:
                    filename = normalized[idx + len('thumbnails/'):]
                    filename = filename.split('?')[0].split('#')[0]
                    entry['thumbnail'] = f'pt:thumb:{filename}'
                    changed += 1

        for item in lib:
            if item.get('type') == 'playlist':
                for child in item.get('videos', []):
                    convert_one(child)
            else:
                convert_one(item)

        if changed:
            self._save_settings()
        return {'migrated': changed}

    def get_all_thumbnails(self):
        """Return a {marker: data_url} dict for every library thumbnail. Used at app
        launch to pre-warm the frontend's _thumbCache so cards render with thumbnails
        already in place — no flash, no per-card backend round-trip on first paint."""
        result = {}
        lib = self.settings.get('library', [])

        def collect(entry):
            thumb = entry.get('thumbnail') or ''
            if thumb.startswith('pt:thumb:'):
                data = self.get_thumbnail_data(thumb)
                if data:
                    result[thumb] = data

        for v in lib:
            if v.get('type') == 'playlist':
                # Playlists may have their own thumbs (the first child's), and children
                # have their own. Collect both since either may be rendered.
                collect(v)
                for c in v.get('videos', []):
                    collect(c)
            else:
                collect(v)
        return result

    def cache_remote_thumb_on_demand(self, video_id, remote_url):
        """Frontend calls this when it encounters a library item with a remote-URL
        thumbnail. Downloads + caches, then persists the new marker back to settings
        so future loads skip this round-trip. Returns the marker (or empty string
        on failure / no internet)."""
        if not video_id or not remote_url:
            return ''
        if remote_url.startswith('pt:thumb:'):
            return remote_url  # already cached
        if not (remote_url.startswith('http://') or remote_url.startswith('https://')):
            return ''
        marker = self._cache_thumbnail(remote_url, video_id)
        if not marker or not marker.startswith('pt:thumb:'):
            return ''  # caching failed (offline most likely)

        # Persist the new marker into the library so we don't refetch
        lib = self.settings.get('library', [])
        changed = False
        for v in lib:
            if v.get('id') == video_id:
                v['thumbnail'] = marker
                changed = True
                break
            if v.get('type') == 'playlist':
                for c in v.get('videos', []):
                    if c.get('id') == video_id:
                        c['thumbnail'] = marker
                        changed = True
                        break
        if changed:
            self._save_settings()
        return marker

    def get_thumbnail_data(self, marker):
        """Resolve a 'pt:thumb:<filename>' marker to a base64 data URL the frontend
        can use as <img src>. Returns empty string if not found.
        Also accepts bare file basenames for resilience."""
        if not marker:
            return ''
        # Strip the prefix if present
        if marker.startswith('pt:thumb:'):
            filename = marker[len('pt:thumb:'):]
        else:
            filename = marker
        # Security: make sure we only read from inside the cache dir
        filename = os.path.basename(filename)  # strips any path components
        full_path = os.path.join(self.thumbnail_cache_dir, filename)
        if not os.path.exists(full_path):
            return ''
        try:
            with open(full_path, 'rb') as f:
                data = f.read()
            if not data:
                return ''
            # Pick MIME type from extension
            ext = os.path.splitext(filename)[1].lower()
            mime = {
                '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                '.png': 'image/png', '.webp': 'image/webp'
            }.get(ext, 'image/jpeg')
            import base64
            b64 = base64.b64encode(data).decode('ascii')
            return f'data:{mime};base64,{b64}'
        except OSError:
            return ''

    def _search_and_extract(self, title):
        """Search YouTube for a title string and return metadata for the top result.
        Uses yt-dlp's built-in search. Returns a dict with url/thumbnail/uploader/etc,
        or {'error': '...'} if the search fails."""
        # ytsearch1: = "give me the top 1 result for this query"
        search_url = f'ytsearch1:{title}'
        opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'extract_flat': False,  # we need full metadata, not flat
            'noprogress': True,
        }
        if self.ffmpeg_location:
            opts['ffmpeg_location'] = self.ffmpeg_location

        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(search_url, download=False)
        except Exception as e:
            return {'error': f'Search failed: {str(e)[:120]}'}

        # Search returns a playlist-like structure with entries
        entries = info.get('entries') if info else None
        if not entries:
            return {'error': 'No results'}
        first = entries[0]
        if not first:
            return {'error': 'Empty search result'}

        # Build a clean result object
        thumbnail = first.get('thumbnail') or ''
        if not thumbnail:
            # yt-dlp sometimes puts thumbs in a list. Pick the highest-res one.
            thumbs = first.get('thumbnails') or []
            if thumbs:
                thumbnail = thumbs[-1].get('url', '')

        return {
            'url': first.get('webpage_url') or first.get('url') or '',
            'thumbnail': thumbnail,
            'uploader': first.get('uploader') or first.get('channel') or '',
            'duration_string': first.get('duration_string') or '',
            'matched_title': first.get('title') or ''
        }

    # ============================================================
    # Import from folder — scans a disk folder and builds library entries
    # from video files found there. Used to rebuild library from files that
    # exist on disk but aren't in the app's memory (e.g. after a data loss).
    # ============================================================
    VIDEO_EXTS = ('.mp4', '.mkv', '.webm', '.m4a', '.mov', '.avi', '.flv')

    def scan_folder_preview(self, folder=None):
        """Walk the folder (defaults to current download folder), count how many
        videos would be imported. Returns a preview summary the UI can show before
        the user commits to the import.

        A subfolder is treated as a 'playlist' ONLY if it contains more than one video.
        Single-video subfolders are how ProTube organizes individual downloads (one folder
        per video) — those should become standalone entries, not 1-video playlists."""
        target = folder or self.download_folder
        if not target or not os.path.isdir(target):
            return {'error': 'Folder does not exist', 'folder': target}

        standalone = 0
        playlists = {}  # subfolder_name -> list of video filenames (only when >1)
        total_bytes = 0

        try:
            # Scan top-level files first
            for entry in os.scandir(target):
                if entry.is_file() and entry.name.lower().endswith(self.VIDEO_EXTS):
                    standalone += 1
                    try:
                        total_bytes += entry.stat().st_size
                    except OSError:
                        pass
                elif entry.is_dir():
                    children = []
                    try:
                        for sub in os.scandir(entry.path):
                            if sub.is_file() and sub.name.lower().endswith(self.VIDEO_EXTS):
                                children.append(sub.name)
                                try:
                                    total_bytes += sub.stat().st_size
                                except OSError:
                                    pass
                    except OSError:
                        continue
                    if len(children) > 1:
                        playlists[entry.name] = children
                    elif len(children) == 1:
                        # Single-video folder — treat as standalone
                        standalone += 1
        except OSError as e:
            return {'error': str(e), 'folder': target}

        total = standalone + sum(len(v) for v in playlists.values())
        return {
            'folder': target,
            'standalone_count': standalone,
            'playlist_count': len(playlists),
            'total_videos': total,
            'total_bytes': total_bytes,
            'playlist_names': list(playlists.keys())[:10]
        }

    def scan_folder_full(self, folder=None):
        """Walk the folder and return every video file grouped into standalone + playlists,
        with the per-file detail the import-picker UI needs (name, path, size, and whether
        the file is already in library or has archived metadata waiting to be restored).

        Cheap scan — no ffprobe. Single-video subfolders are treated as standalone, matching
        scan_folder_preview's behavior. Items already in library are still listed but flagged
        in_library=True so the UI can render them as disabled."""
        target = folder or self.download_folder
        if not target or not os.path.isdir(target):
            return {'error': 'Folder does not exist', 'folder': target}

        # Build the lookups once: known filepaths in library, archived filepaths
        library = self.settings.get('library', [])
        in_lib = set()
        for v in library:
            if v.get('type') == 'playlist':
                for c in v.get('videos', []):
                    key = self._archive_key(c.get('filepath'))
                    if key:
                        in_lib.add(key)
            else:
                key = self._archive_key(v.get('filepath'))
                if key:
                    in_lib.add(key)
        archive = self.settings.get('library_archive', {}) or {}

        def _file_entry(path, name):
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            key = self._archive_key(path)
            return {
                'path': path,
                'name': name,
                'size_bytes': size,
                'in_library': key in in_lib if key else False,
                'in_archive': key in archive if key else False,
            }

        standalone = []
        playlists = []
        total_bytes = 0

        try:
            entries = sorted(os.scandir(target), key=lambda e: e.name.lower())
        except OSError as e:
            return {'error': str(e), 'folder': target}

        for entry in entries:
            try:
                if entry.is_file() and entry.name.lower().endswith(self.VIDEO_EXTS):
                    item = _file_entry(entry.path, entry.name)
                    total_bytes += item['size_bytes']
                    standalone.append(item)
                elif entry.is_dir():
                    try:
                        sub_entries = sorted(os.scandir(entry.path), key=lambda e: e.name.lower())
                    except OSError:
                        continue
                    video_subs = [s for s in sub_entries
                                  if s.is_file() and s.name.lower().endswith(self.VIDEO_EXTS)]
                    if len(video_subs) == 1:
                        # Single-video folder — treat as standalone
                        sub = video_subs[0]
                        item = _file_entry(sub.path, sub.name)
                        total_bytes += item['size_bytes']
                        standalone.append(item)
                    elif len(video_subs) > 1:
                        children = []
                        for sub in video_subs:
                            item = _file_entry(sub.path, sub.name)
                            total_bytes += item['size_bytes']
                            children.append(item)
                        playlists.append({
                            'name': entry.name,
                            'folder_path': entry.path,
                            'videos': children,
                        })
            except OSError:
                continue

        total_videos = len(standalone) + sum(len(p['videos']) for p in playlists)
        return {
            'folder': target,
            'standalone': standalone,
            'playlists': playlists,
            'total_videos': total_videos,
            'total_bytes': total_bytes,
        }

    def import_from_folder(self, folder=None, merge=True, selected_paths=None):
        """Walk the folder and build library entries for every video file found.
        - Top-level files become standalone library entries
        - Subfolders become playlists (folder name = playlist title, contents = children)
        - If merge=True, skip items whose filepath is already in the library
        - If selected_paths is a list, only import files whose normalized path is in
          that set (lets the UI offer a checklist instead of all-or-nothing). None
          imports everything.
        - Returns counts so the UI can toast a summary.

        Progress is tracked in self._import_progress (current/total/phase) so the
        frontend can poll get_import_progress() while this runs and show a bar.
        Per-file ffprobe is the slow part; pre-counting lets us give a denominator."""
        self._import_progress = {'current': 0, 'total': 0, 'phase': 'counting'}
        target = folder or self.download_folder
        if not target or not os.path.isdir(target):
            self._import_progress = {'current': 0, 'total': 0, 'phase': 'error'}
            return {'error': 'Folder does not exist', 'folder': target}

        library = self.settings.get('library', [])

        # When selected_paths is provided, we only walk files whose filepath is in the
        # set. None means "import everything" (legacy behavior).
        selected_set = None
        if selected_paths is not None:
            selected_set = set()
            for p in selected_paths:
                key = self._archive_key(p)
                if key:
                    selected_set.add(key)

        def _should_import(path):
            if selected_set is None:
                return True
            key = self._archive_key(path)
            return bool(key) and key in selected_set

        # Build set of known filepaths so we don't duplicate entries on re-import
        known_paths = set()
        if merge:
            for v in library:
                if v.get('type') == 'playlist':
                    for c in v.get('videos', []):
                        if c.get('filepath'):
                            known_paths.add(os.path.normcase(os.path.normpath(c['filepath'])))
                elif v.get('filepath'):
                    known_paths.add(os.path.normcase(os.path.normpath(v['filepath'])))

        # Pre-count total videos so the UI has a denominator. Cheap (no ffprobe).
        # Honors selected_paths so the bar reflects only what we'll actually process.
        total_count = 0
        try:
            for entry in os.scandir(target):
                if entry.is_file() and entry.name.lower().endswith(self.VIDEO_EXTS):
                    if _should_import(entry.path):
                        total_count += 1
                elif entry.is_dir():
                    try:
                        for sub in os.scandir(entry.path):
                            if sub.is_file() and sub.name.lower().endswith(self.VIDEO_EXTS):
                                if _should_import(sub.path):
                                    total_count += 1
                    except OSError:
                        pass
        except OSError:
            pass
        self._import_progress = {'current': 0, 'total': total_count, 'phase': 'importing'}

        imported_videos = 0
        imported_playlists = 0
        skipped = 0

        try:
            for entry in os.scandir(target):
                if entry.is_file() and entry.name.lower().endswith(self.VIDEO_EXTS):
                    # Standalone video
                    if not _should_import(entry.path):
                        continue
                    fp_key = os.path.normcase(os.path.normpath(entry.path))
                    if fp_key in known_paths:
                        skipped += 1
                        self._import_progress['current'] += 1
                        continue
                    video = self._build_video_entry_from_file(entry.path, parent_folder=target)
                    if video:
                        library.append(video)
                        known_paths.add(fp_key)
                        imported_videos += 1
                    self._import_progress['current'] += 1

                elif entry.is_dir():
                    # Collect videos in this subfolder
                    try:
                        sub_entries = sorted(os.scandir(entry.path), key=lambda e: e.name.lower())
                    except OSError:
                        continue
                    video_subs = [s for s in sub_entries
                                  if s.is_file() and s.name.lower().endswith(self.VIDEO_EXTS)]

                    if len(video_subs) == 1:
                        # Single-video folder — treat as standalone, not a 1-item playlist.
                        # ProTube saves each downloaded video into its own folder by default.
                        sub = video_subs[0]
                        if not _should_import(sub.path):
                            continue
                        fp_key = os.path.normcase(os.path.normpath(sub.path))
                        if fp_key in known_paths:
                            skipped += 1
                            self._import_progress['current'] += 1
                            continue
                        video = self._build_video_entry_from_file(sub.path, parent_folder=entry.path)
                        if video:
                            library.append(video)
                            known_paths.add(fp_key)
                            imported_videos += 1
                        self._import_progress['current'] += 1
                    elif len(video_subs) > 1:
                        # Multi-video folder → real playlist (built only from selected children)
                        children = []
                        for sub in video_subs:
                            if not _should_import(sub.path):
                                continue
                            fp_key = os.path.normcase(os.path.normpath(sub.path))
                            if fp_key in known_paths:
                                skipped += 1
                                self._import_progress['current'] += 1
                                continue
                            child = self._build_video_entry_from_file(sub.path, parent_folder=entry.path)
                            if child:
                                child['isFromPlaylist'] = True
                                child['playlistTitle'] = entry.name
                                children.append(child)
                                known_paths.add(fp_key)
                            self._import_progress['current'] += 1

                        if children:
                            # Sanitize folder name before baking it into the ID.
                            # Imported playlist IDs have historically been the only
                            # source of user-controlled characters in entry IDs —
                            # YouTube IDs are alphanumeric, but a folder name can
                            # contain anything (`'`, `"`, `<`, even spaces). Without
                            # this, the ID would later flow into inline `onclick="
                            # foo('${id}')"` templates and a folder named `Bob's`
                            # would inject JS via the `'`. Strip everything outside
                            # `[a-zA-Z0-9_-]` and cap length so even pathological
                            # folder names produce a clean, safe identifier.
                            safe_name = re.sub(r'[^A-Za-z0-9_-]', '_', entry.name)[:50]
                            playlist_id = f'imported_{safe_name}_{abs(hash(entry.path)) % (10**9)}'
                            playlist = {
                                'type': 'playlist',
                                'id': playlist_id,
                                'title': entry.name,
                                'uploader': '',
                                'status': 'Done',
                                'imported': True,
                                'videos': children,
                                'thumbnails': [],
                                # Stamp so the playlist also fires the NEW badge.
                                # Children already get their own added_at via
                                # _build_video_entry_from_file.
                                'added_at': int(time.time()),
                            }
                            library.append(playlist)
                            imported_playlists += 1
                            imported_videos += len(children)
        except OSError as e:
            self._import_progress = {'current': 0, 'total': 0, 'phase': 'error'}
            return {'error': str(e), 'folder': target}

        self.settings['library'] = library
        self._save_settings()
        self._import_progress = {'current': total_count, 'total': total_count, 'phase': 'done'}

        # Kick off a background pass to extract frame thumbnails for the
        # imported videos. Runs in a daemon thread so import_from_folder can
        # return immediately. Auto-extracted frames get replaced if/when the
        # user later refetches metadata and gets a real YouTube thumb.
        self._start_frame_extraction_worker()

        return {
            'folder': target,
            'imported_videos': imported_videos,
            'imported_playlists': imported_playlists,
            'skipped': skipped
        }

    def get_import_progress(self):
        """Snapshot of the current import_from_folder progress for UI polling.
        Returns {current, total, phase} where phase is one of:
        'idle' | 'counting' | 'importing' | 'done' | 'error'."""
        return getattr(self, '_import_progress',
                       {'current': 0, 'total': 0, 'phase': 'idle'})

    def _build_video_entry_from_file(self, filepath, parent_folder):
        """Build a library entry for a single video file on disk. Uses ffprobe for
        duration when available, otherwise leaves it blank. Never raises — returns
        None if the file can't be read.

        If a previous library entry for this filepath was archived (via remove_from_library),
        restore it instead of building a sparse new entry. This preserves the original
        title/url/thumbnail/uploader/formats so the user doesn't have to refetch metadata
        after a remove → re-import cycle."""
        try:
            if not os.path.isfile(filepath):
                return None

            archive = self.settings.get('library_archive', {})
            arc_key = self._archive_key(filepath)
            archived = archive.get(arc_key) if arc_key else None
            if archived:
                restored = dict(archived)
                # Refresh disk-derived fields in case the file changed since archival
                try:
                    new_size = os.path.getsize(filepath)
                    if isinstance(restored.get('sizeMap'), dict):
                        sel = restored.get('selectedQuality') or 'imported'
                        restored['sizeMap'] = dict(restored['sizeMap'])
                        restored['sizeMap'][sel] = new_size
                    else:
                        restored['sizeMap'] = {restored.get('selectedQuality') or 'imported': new_size}
                except OSError:
                    pass
                restored['filepath'] = filepath
                restored['folderpath'] = parent_folder
                restored['missing'] = False
                # Re-stamp added_at to "now" so the NEW badge fires again — the user
                # is importing it fresh into their library, even if it was previously
                # archived. From their POV this is a new addition.
                restored['added_at'] = int(time.time())
                # Drop the archive entry now that it's back in the active library
                try:
                    del archive[arc_key]
                except KeyError:
                    pass
                return restored

            filename = os.path.basename(filepath)
            name_no_ext = os.path.splitext(filename)[0]
            size = 0
            try:
                size = os.path.getsize(filepath)
            except OSError:
                pass

            duration_str = self._probe_duration(filepath)

            # Use a stable id based on filepath so re-imports dedupe correctly
            stable_id = 'imported_' + str(abs(hash(os.path.normcase(os.path.normpath(filepath)))) % (10**12))

            return {
                'type': 'video',
                'id': stable_id,
                'title': name_no_ext,
                'uploader': '',
                'thumbnail': '',
                'url': '',
                'status': 'Done',
                'imported': True,
                'filepath': filepath,
                'folderpath': parent_folder,
                'duration_string': duration_str or '',
                'sizeMap': {'imported': size},
                'selectedQuality': 'imported',
                'formats': [],
                'missing': False,
                # Stamp so the "NEW" badge (48h window) fires for freshly imported
                # videos — matches the behavior of add_to_library / add_playlist_to_library.
                'added_at': int(time.time()),
            }
        except Exception:
            return None

    def _probe_duration(self, filepath):
        """Return a duration string like '26:16' for a video file using ffprobe.
        Returns empty string if ffprobe isn't available or the probe fails."""
        ffprobe = self._find_ffprobe()
        if not ffprobe:
            return ''
        try:
            result = subprocess.run(
                [ffprobe, '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', filepath],
                capture_output=True, text=True, timeout=10,
                creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
            )
            if result.returncode != 0:
                return ''
            seconds = float(result.stdout.strip() or 0)
            if seconds <= 0:
                return ''
            h = int(seconds // 3600)
            m = int((seconds % 3600) // 60)
            s = int(seconds % 60)
            if h > 0:
                return f'{h}:{m:02d}:{s:02d}'
            return f'{m}:{s:02d}'
        except (subprocess.TimeoutExpired, ValueError, OSError):
            return ''

    def _find_ffprobe(self):
        """Locate ffprobe similar to how _resolve_ffmpeg_location handles ffmpeg."""
        exe = 'ffprobe.exe' if sys.platform == 'win32' else 'ffprobe'
        if hasattr(sys, '_MEIPASS'):
            bundled = os.path.join(sys._MEIPASS, exe)
            if os.path.exists(bundled):
                return bundled
        if sys.platform == 'darwin':
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            local = os.path.join(root, 'assets', 'mac', 'ffprobe')
            if os.path.isfile(local):
                return local
        for path_dir in os.environ.get('PATH', '').split(os.pathsep):
            candidate = os.path.join(path_dir, exe)
            if os.path.isfile(candidate):
                return candidate
        return None

    def _find_ffmpeg_exe(self):
        """Locate the ffmpeg executable. self.ffmpeg_location stores a directory
        (yt-dlp wants a directory), but for direct invocation we need the actual
        ffmpeg(.exe) path."""
        exe = 'ffmpeg.exe' if sys.platform == 'win32' else 'ffmpeg'
        if hasattr(sys, '_MEIPASS'):
            bundled = os.path.join(sys._MEIPASS, exe)
            if os.path.exists(bundled):
                return bundled
        if sys.platform == 'darwin':
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            local = os.path.join(root, 'assets', 'mac', 'ffmpeg')
            if os.path.isfile(local):
                return local
        for path_dir in os.environ.get('PATH', '').split(os.pathsep):
            candidate = os.path.join(path_dir, exe)
            if os.path.isfile(candidate):
                return candidate
        return None

    # ============================================================
    # Auto frame thumbnails — extract a representative frame from a
    # video file and use it as the thumbnail for imported entries
    # that lack a YouTube thumb. Two flags track origin:
    #   frame_thumbnail_auto   = True  -> extracted as fallback,
    #                                     metadata refetch may replace it.
    #   frame_thumbnail_forced = True  -> user explicitly pinned this
    #                                     frame, refetch leaves it alone.
    # ============================================================
    def _duration_string_to_seconds(self, s):
        """'15:26' -> 926.0, '1:23:45' -> 5025.0. Returns None on parse error."""
        if not s:
            return None
        try:
            parts = [int(p) for p in str(s).split(':')]
        except ValueError:
            return None
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        if len(parts) == 1:
            return parts[0]
        return None

    def _extract_video_frame(self, filepath, video_id, timestamp_sec=5.0, force_refresh=False):
        """Run ffmpeg to grab a single frame at `timestamp_sec`, save it to the
        thumbnail cache as <safe_id>.jpg, return a 'pt:thumb:...' marker the
        frontend can resolve. Returns None on failure.

        force_refresh=True will re-encode even if a cached file already exists,
        used by the manual 'Use video frame' action so the user can change the
        timestamp and see the result."""
        if not filepath or not os.path.isfile(filepath) or not video_id:
            return None
        ffmpeg = self._find_ffmpeg_exe()
        if not ffmpeg:
            return None
        safe_id = re.sub(r'[^a-zA-Z0-9_\-]', '_', str(video_id))[:80]
        if not safe_id:
            return None
        local_path = os.path.join(self.thumbnail_cache_dir, f'{safe_id}.jpg')
        if not force_refresh and os.path.exists(local_path) and os.path.getsize(local_path) > 100:
            return f'pt:thumb:{safe_id}.jpg'
        # Clear any older-extension cached file for this id so the .jpg we're
        # about to write becomes canonical.
        if force_refresh:
            for old_ext in ('.webp', '.png', '.jpeg'):
                stale = os.path.join(self.thumbnail_cache_dir, f'{safe_id}{old_ext}')
                if os.path.exists(stale):
                    try:
                        os.remove(stale)
                    except OSError:
                        pass
        try:
            ts = max(0.0, float(timestamp_sec))
        except (TypeError, ValueError):
            ts = 5.0
        try:
            # -ss BEFORE -i = fast input seek. -vframes 1 = one frame.
            # -q:v 4 = good JPEG quality. -y = overwrite output.
            # -an = drop audio. scale=480:-2 keeps 16:9-ish at modest size.
            result = subprocess.run(
                [ffmpeg, '-hide_banner', '-loglevel', 'error',
                 '-ss', f'{ts:.3f}', '-i', filepath,
                 '-frames:v', '1', '-q:v', '4', '-an',
                 '-vf', 'scale=480:-2',
                 '-y', local_path],
                capture_output=True, timeout=30,
                creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
            )
            if result.returncode != 0 or not os.path.exists(local_path):
                return None
            if os.path.getsize(local_path) < 100:
                try:
                    os.remove(local_path)
                except OSError:
                    pass
                return None
            return f'pt:thumb:{safe_id}.jpg'
        except (subprocess.TimeoutExpired, OSError):
            return None

    def _needs_auto_frame_thumb(self, entry):
        """True if entry has a real file on disk and no usable thumbnail.

        Critical: we do NOT skip on the frame_thumbnail_auto flag. That flag is
        informational only — it records that auto-extraction has been attempted,
        not that it succeeded. The thing that actually matters is whether a
        valid thumbnail exists. If extraction failed last time and the entry
        still has no thumbnail, retry. (This is what was breaking 'auto' before:
        the flag got set on every entry on first run, and a single transient
        failure permanently locked the entry out of future retries.)

        Only frame_thumbnail_forced blocks extraction — that's a user-pinned
        choice, we never overwrite it.
        """
        if not entry or entry.get('type') == 'playlist':
            return False
        if entry.get('frame_thumbnail_forced'):
            return False
        fp = entry.get('filepath')
        if not fp or not os.path.isfile(fp):
            return False
        thumb = entry.get('thumbnail') or ''
        if not thumb:
            return True
        # If the marker points at a missing/tiny cache file, treat as no thumb.
        if thumb.startswith('pt:thumb:'):
            cached = os.path.join(self.thumbnail_cache_dir, thumb[len('pt:thumb:'):])
            if not os.path.exists(cached) or os.path.getsize(cached) < 100:
                return True
            return False
        # Any other thumbnail value (remote URL, etc.) — leave it alone
        return False

    def _next_frame_extraction_target(self):
        """Walk the library and return the next entry needing a frame thumb,
        or None when there's nothing left to process."""
        for v in self.settings.get('library', []):
            if v.get('type') == 'playlist':
                for c in v.get('videos', []):
                    if self._needs_auto_frame_thumb(c):
                        return c
            elif self._needs_auto_frame_thumb(v):
                return v
        return None

    def _run_frame_extraction_worker(self):
        """Background worker that processes pending frame extractions one by one.
        Daemon thread, swallows errors per entry so one bad file doesn't stop
        the queue. Saves settings after each successful extraction so progress
        survives a crash. Self-terminates when there's nothing left.

        On failure: we no longer mark the entry. The next worker pass (next
        launch, next import, etc.) will retry. This means a corrupted file
        will burn ~30s of timeout per launch — acceptable. The previous
        approach of marking on failure caused 'auto extraction silently does
        nothing' because one transient failure permanently locked an entry
        out of retries. To prevent runaway retries within a single session,
        we cap consecutive failures at 5 — beyond that, we stop the worker
        and let the next launch try again.
        """
        consecutive_failures = 0
        processed = 0
        succeeded = 0
        self._frame_worker_log('worker started')
        try:
            while True:
                if not self.settings.get('auto_extract_frame_thumbnails', True):
                    self._frame_worker_log('disabled via setting; stopping')
                    break
                target = self._next_frame_extraction_target()
                if not target:
                    break
                processed += 1
                ts = 5.0
                duration_secs = self._duration_string_to_seconds(target.get('duration_string'))
                if duration_secs is not None and duration_secs > 0:
                    if duration_secs < 5:
                        ts = 0.5
                    elif duration_secs < 50:
                        ts = max(1.0, duration_secs * 0.1)
                marker = self._extract_video_frame(target.get('filepath'), target.get('id'), ts)
                if marker:
                    target['thumbnail'] = marker
                    target['frame_thumbnail_auto'] = True
                    try:
                        self._save_settings()
                    except Exception as e:
                        self._frame_worker_log(f'settings save failed: {e}')
                    succeeded += 1
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    self._frame_worker_log(
                        f'extraction failed for "{target.get("title", "?")[:50]}" '
                        f'(filepath={target.get("filepath", "?")[:80]}, ts={ts:.1f}); '
                        f'consecutive_failures={consecutive_failures}'
                    )
                    if consecutive_failures >= 5:
                        self._frame_worker_log('5 consecutive failures, stopping for this session')
                        break
        except Exception as e:
            self._frame_worker_log(f'unexpected error: {e}')
        finally:
            self._frame_worker_running = False
            self._frame_worker_log(f'worker finished: {succeeded}/{processed} succeeded')

    def _frame_worker_log(self, msg):
        """Append a worker-progress line to data/protube.log. Never throws.
        Use this for diagnostics so the user can show what the worker did."""
        try:
            # data/ folder is the parent of thumbnail_cache_dir
            data_folder = os.path.dirname(self.thumbnail_cache_dir)
            log_path = os.path.join(data_folder, 'protube.log')
            with open(log_path, 'a', encoding='utf-8') as f:
                import time as _time
                ts = _time.strftime('%Y-%m-%d %H:%M:%S')
                f.write(f'[{ts}] [frame-worker] {msg}\n')
        except Exception:
            pass

    def _start_frame_extraction_worker(self):
        """Spawn the background worker if it's not already running. Idempotent."""
        if getattr(self, '_frame_worker_running', False):
            self._frame_worker_log('start requested but worker already running')
            return
        if not self.settings.get('auto_extract_frame_thumbnails', True):
            self._frame_worker_log('start requested but auto_extract_frame_thumbnails is disabled')
            return
        ffmpeg = self._find_ffmpeg_exe()
        if not ffmpeg:
            self._frame_worker_log('start requested but ffmpeg not found — skipping')
            return
        # Quick scan for pending entries so we know whether spawning is worth it
        pending = 0
        try:
            for v in self.settings.get('library', []):
                if v.get('type') == 'playlist':
                    for c in v.get('videos', []):
                        if self._needs_auto_frame_thumb(c):
                            pending += 1
                elif self._needs_auto_frame_thumb(v):
                    pending += 1
        except Exception:
            pass
        self._frame_worker_log(f'starting worker (ffmpeg={ffmpeg}, pending={pending})')
        self._frame_worker_running = True
        threading.Thread(target=self._run_frame_extraction_worker, daemon=True).start()

    def get_frame_extraction_status(self):
        """Status snapshot for the UI to poll while the background worker is running.
        Returns {'running': bool, 'pending': int}. The frontend uses this to know
        when to refresh the library and pick up newly-extracted thumbnails — the
        worker mutates settings in place, but the rendered library is a snapshot."""
        pending = 0
        for v in self.settings.get('library', []):
            if v.get('type') == 'playlist':
                for c in v.get('videos', []):
                    if self._needs_auto_frame_thumb(c):
                        pending += 1
            elif self._needs_auto_frame_thumb(v):
                pending += 1
        return {
            'running': bool(getattr(self, '_frame_worker_running', False)),
            'pending': pending,
        }

    def force_video_frame_thumbnail(self, video_id, timestamp_sec=None):
        """Manual override: replace this entry's thumbnail with an extracted
        video frame, regardless of any existing YouTube thumb. Sets the
        forced flag so future metadata refetches can't replace it.

        timestamp_sec: optional float seconds. Defaults to 5.0 (or 10% of
        duration for short videos)."""
        target = None
        for v in self.settings.get('library', []):
            if v.get('id') == video_id:
                target = v
                break
            if v.get('type') == 'playlist':
                for c in v.get('videos', []):
                    if c.get('id') == video_id:
                        target = c
                        break
                if target:
                    break
        if not target:
            return {'ok': False, 'error': 'Video not found in library'}
        fp = target.get('filepath')
        if not fp or not os.path.isfile(fp):
            return {'ok': False, 'error': 'Video file is missing on disk'}
        if not self._find_ffmpeg_exe():
            return {'ok': False, 'error': 'ffmpeg not available'}
        if timestamp_sec is None:
            ts = 5.0
            duration_secs = self._duration_string_to_seconds(target.get('duration_string'))
            if duration_secs is not None and duration_secs > 0 and duration_secs < 50:
                ts = max(1.0, duration_secs * 0.1)
        else:
            try:
                ts = max(0.0, float(timestamp_sec))
            except (TypeError, ValueError):
                ts = 5.0
        marker = self._extract_video_frame(fp, target.get('id'), ts, force_refresh=True)
        if not marker:
            return {'ok': False, 'error': 'Frame extraction failed'}
        target['thumbnail'] = marker
        target['frame_thumbnail_forced'] = True
        target.pop('frame_thumbnail_auto', None)
        self._save_settings()
        return {'ok': True, 'thumbnail': marker}

    def _apply_refetched_thumbnail(self, target, remote_url, force_refresh=False):
        """Helper: cache and apply a refetched YouTube thumbnail to a library
        entry. Skips if the user explicitly pinned a frame thumbnail (forced).
        Clears the auto-frame flag if it was set, since the entry now has a
        real curated thumb."""
        if not remote_url or not target:
            return
        if target.get('frame_thumbnail_forced'):
            return
        cached = self._cache_thumbnail(remote_url, target.get('id'), force_refresh=force_refresh)
        target['thumbnail'] = cached
        target.pop('frame_thumbnail_auto', None)

    def _migrate_queue_done_to_library(self):
        """
        One-time migration: move queue entries with status='Done' into the library.
        Silent purge for entries whose files don't exist on disk.
        Sets _library_migrated=True so this never runs twice.
        """
        queue = self.settings.get('queue', [])
        library = self.settings.get('library', [])
        library_ids = {v.get('id') for v in library}

        moved_count = 0
        purged_count = 0
        new_queue = []

        for item in queue:
            if item.get('type') == 'playlist':
                # Playlist: count how many children are Done. If all done, move playlist.
                # Otherwise leave the playlist in queue (Done children stay inside, will move
                # with the playlist when the rest finish).
                children = item.get('videos', [])
                selected_children = [c for c in children if c.get('selected') is not False]
                all_done = len(selected_children) > 0 and all(
                    c.get('status') == 'Done' for c in selected_children
                )
                if all_done:
                    # Move whole playlist. Flag any with missing files rather than purging,
                    # so the user can see what's gone and decide what to do.
                    for c in selected_children:
                        if self._is_file_missing(c):
                            c['missing'] = True
                    if item.get('id') not in library_ids:
                        item['videos'] = selected_children
                        library.append(item)
                        library_ids.add(item.get('id'))
                        moved_count += len(selected_children)
                    # Don't put back in queue — we moved it
                else:
                    # Some children not done yet; leave playlist in queue
                    new_queue.append(item)
            else:
                # Single video
                if item.get('status') == 'Done':
                    # Move to library. Flag-as-missing rather than purging — if the file
                    # is actually gone, the user sees it marked missing and can decide.
                    if self._is_file_missing(item):
                        item['missing'] = True
                    if item.get('id') not in library_ids:
                        library.append(item)
                        library_ids.add(item.get('id'))
                        moved_count += 1
                    # Don't put back in queue
                else:
                    # Not done: keep in queue
                    new_queue.append(item)

        # Commit changes
        self.settings['library'] = library
        self.settings['queue'] = new_queue
        self.settings['_library_migrated'] = True
        self._save_settings()

        # Announce to UI if anything moved. Frontend listens for this toast on first load.
        if moved_count > 0 or purged_count > 0:
            # Schedule the toast on the next tick — webview may not be ready yet at startup
            def announce():
                time.sleep(1.2)  # give UI time to finish initial render
                parts = []
                if moved_count > 0:
                    parts.append(f"{moved_count} video{'s' if moved_count != 1 else ''} moved to Library")
                if purged_count > 0:
                    parts.append(f"{purged_count} missing file{'s' if purged_count != 1 else ''} cleaned up")
                msg = " · ".join(parts)
                self._send_to_js('showToast', msg, None, None)
            threading.Thread(target=announce, daemon=True).start()

        return {'moved': moved_count, 'purged': purged_count}

    def open_folder(self, path=None):
        """Open a folder in the OS file manager. Defaults to the main download folder."""
        target = path or self.download_folder
        if not target or not os.path.exists(target):
            return False
        try:
            if sys.platform == 'win32':
                os.startfile(target)
            elif sys.platform == 'darwin':
                subprocess.run(['open', target], check=False)
            else:
                subprocess.run(['xdg-open', target], check=False)
            return True
        except OSError:
            return False

    def open_external_url(self, url):
        """Open a URL in the user's default browser. Used by 'Open on YouTube' in detail panel."""
        if not url or not isinstance(url, str):
            return False
        # Only allow http(s) URLs — guard against file:// or anything weirder
        if not (url.startswith('http://') or url.startswith('https://')):
            return False
        try:
            if sys.platform == 'win32':
                os.startfile(url)
            elif sys.platform == 'darwin':
                subprocess.run(['open', url], check=False)
            else:
                subprocess.run(['xdg-open', url], check=False)
            return True
        except OSError:
            return False
        except Exception:
            return False

    def toggle_fullscreen(self):
        """Toggle the OS window's fullscreen state. Used by the player to enter true
        borderless fullscreen — JS requestFullscreen alone leaves the pywebview title
        bar visible because WebView2 doesn't propagate fullscreen to the host window."""
        try:
            if webview.windows:
                webview.windows[0].toggle_fullscreen()
                return {'ok': True}
        except Exception as e:
            print(f'[ProTube] toggle_fullscreen failed: {e}')
        return {'ok': False}

    def set_fullscreen(self, want_fullscreen):
        """Idempotent fullscreen — set the OS window to a specific state instead of
        toggling. The player calls this on enter (True) and exit (False); we only fire
        the underlying toggle_fullscreen() when state actually needs to change. Without
        this, JS↔Python state would drift over multiple toggles and the window would
        end up stuck in a half-borderless state. pywebview doesn't expose a way to
        query the current fullscreen state portably, so we track it ourselves."""
        try:
            want = bool(want_fullscreen)
            if not hasattr(self, '_window_is_fullscreen'):
                self._window_is_fullscreen = False
            if self._window_is_fullscreen == want:
                return {'ok': True, 'changed': False}
            if webview.windows:
                webview.windows[0].toggle_fullscreen()
                self._window_is_fullscreen = want
                # pywebview's toggle_fullscreen marshals its WinForms work onto the
                # UI thread, so it runs partly *after* this call returns. If we only
                # apply polish immediately, pywebview's own DWM re-round-corners and
                # FormBorderStyle restore can land *after* our polish, undoing it.
                # Schedule polish at multiple offsets so at least one fires after
                # pywebview's UI-thread work has fully settled.
                for _delay in (0.0, 0.05, 0.2, 0.5):
                    threading.Timer(_delay, self.apply_window_polish).start()
                return {'ok': True, 'changed': True}
        except Exception as e:
            print(f'[ProTube] set_fullscreen failed: {e}')
        return {'ok': False}

    def apply_window_polish(self):
        """Force square Win11 corners on our window AND invalidate the cached frame.
        pywebview's toggle_fullscreen flips DWMWA_WINDOW_CORNER_PREFERENCE back to
        DEFAULT (rounded) on every exit, and never calls SetWindowPos with
        SWP_FRAMECHANGED — both contribute to the white-sliver/rounded-corner artifact
        after exiting fullscreen. This method is called at startup once and from
        set_fullscreen() after every toggle, undoing pywebview's behavior."""
        if sys.platform != 'win32':
            return False
        try:
            import ctypes
            user32 = ctypes.windll.user32
            dwmapi = ctypes.windll.dwmapi
            user32.FindWindowW.restype = ctypes.c_void_p
            user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
            dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
            dwmapi.DwmSetWindowAttribute.argtypes = [
                ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint
            ]
            user32.SetWindowPos.restype = ctypes.c_int
            user32.SetWindowPos.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint
            ]
            user32.RedrawWindow.restype = ctypes.c_int
            user32.RedrawWindow.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint
            ]
            hwnd = user32.FindWindowW(None, 'ProTube Saver')
            if not hwnd:
                return False
            pref = ctypes.c_int(1)  # DWMWCP_DONOTROUND
            dwmapi.DwmSetWindowAttribute(
                hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref)
            )
            # DWMWA_BORDER_COLOR (34): pywebview restores this to DEFAULT on
            # exit, which is a light gray on Win11 that reads as a near-white
            # 1px edge. Force it to opaque black so any leaked border pixel
            # blends into our dark UI. COLORREF is 0x00BBGGRR.
            border = ctypes.c_uint(0x00000000)
            dwmapi.DwmSetWindowAttribute(
                hwnd, 34, ctypes.byref(border), ctypes.sizeof(border)
            )
            SWP_NOMOVE       = 0x0002
            SWP_NOSIZE       = 0x0001
            SWP_NOZORDER     = 0x0004
            SWP_NOACTIVATE   = 0x0010
            SWP_FRAMECHANGED = 0x0020
            flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED
            user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, flags)
            # SetWindowPos(SWP_FRAMECHANGED) only invalidates the non-client area.
            # The WebView2 child's cached paint at the corners survives unless we
            # redraw all children synchronously too. RDW_ALLCHILDREN propagates the
            # invalidate down into the WebView2 control; RDW_UPDATENOW makes it
            # synchronous (no flicker window where old pixels are visible).
            RDW_INVALIDATE  = 0x0001
            RDW_UPDATENOW   = 0x0100
            RDW_ALLCHILDREN = 0x0080
            RDW_FRAME       = 0x0400
            user32.RedrawWindow(
                hwnd, None, None,
                RDW_INVALIDATE | RDW_UPDATENOW | RDW_ALLCHILDREN | RDW_FRAME,
            )
            # Force the WinForms host Form's BackColor to opaque black. pywebview's
            # background_color='#000000' parameter sets this conditionally and may
            # be skipped on the WebView2 backend, so we set it ourselves via
            # window.native (the BrowserForm) or the BrowserView.instances fallback.
            # Without this, the form paints its default color during the gap before
            # WebView2's first frame (visible white flash on launch) and during the
            # un-maximize→resize→re-maximize sequence inside toggle_fullscreen.
            backcolor_path = 'skipped'
            try:
                from System.Drawing import Color
                black = Color.FromArgb(255, 0, 0, 0)
                form = None
                if webview.windows:
                    win = webview.windows[0]
                    native = getattr(win, 'native', None)
                    if native is not None and hasattr(native, 'BackColor'):
                        form = native
                        backcolor_path = 'native'
                    else:
                        try:
                            from webview.platforms import winforms as _wf
                            cls = getattr(_wf, 'BrowserView', None) or getattr(_wf, 'BrowserForm', None)
                            if cls is not None:
                                instances = getattr(cls, 'instances', {})
                                if isinstance(instances, dict):
                                    cand = instances.get(win.uid) or (next(iter(instances.values()), None))
                                    if cand is not None and hasattr(cand, 'BackColor'):
                                        form = cand
                                        backcolor_path = 'BrowserView.instances'
                        except Exception as _e:
                            backcolor_path = f'instances-err:{_e}'
                if form is not None:
                    form.BackColor = black
            except Exception:
                # Silent — the failure path of the outer try/except still logs
                # genuinely broken cases. We don't log per-call BackColor results.
                pass
            return True
        except Exception as e:
            try:
                log_path = os.path.join(
                    os.path.expanduser('~'), 'Downloads', 'ProTube Saver', 'protube.log'
                )
                with open(log_path, 'a', encoding='utf-8') as f:
                    f.write(f'[ProTube] apply_window_polish failed: {e}\n')
            except Exception:
                pass
            return False

    def open_file(self, path):
        """Open a specific file with the OS default application."""
        if not path or not os.path.exists(path):
            return False
        try:
            if sys.platform == 'win32':
                os.startfile(path)
            elif sys.platform == 'darwin':
                subprocess.run(['open', path], check=False)
            else:
                subprocess.run(['xdg-open', path], check=False)
            return True
        except Exception:
            return False

    def reveal_in_folder(self, path):
        """Open the containing folder of `path` with the file selected (if OS supports it)."""
        if not path:
            return False
        if not os.path.exists(path):
            # File got deleted — open the folder anyway if we can
            parent = os.path.dirname(path)
            if os.path.exists(parent):
                return self.open_folder(parent)
            return False
        try:
            if sys.platform == 'win32':
                # /select, arg opens Explorer with the file highlighted
                subprocess.run(['explorer', '/select,', path], check=False)
            elif sys.platform == 'darwin':
                subprocess.run(['open', '-R', path], check=False)
            else:
                # Most Linux file managers don't have a standard "reveal" verb; just open parent
                subprocess.run(['xdg-open', os.path.dirname(path)], check=False)
            return True
        except Exception:
            return False

    def pause_download(self, vid):
        """Mark a video as paused. The _hook will raise on the next progress tick, stopping yt-dlp.
        The .part file on disk stays, so resuming picks up where it left off."""
        self.paused_ids.add(vid)

    def resume_download(self, vid):
        """Remove pause flag (so the next start_download pass can proceed)."""
        self.paused_ids.discard(vid)
        self.cancelled_ids.discard(vid)

    def cancel_all_downloads(self):
        """
        Mark every active download as cancelled. The _hook will raise on the next
        progress tick, which tears down the yt-dlp call. Unstarted threads waiting
        on the semaphore will see their id in cancelled_ids and bail out.
        """
        # Snapshot active ids so we don't mutate the dict we're iterating
        active_ids = list(self.active_downloads.keys())
        for vid in active_ids:
            self.cancelled_ids.add(vid)

    def cancel_download(self, vid):
        """Cancel a single download by id."""
        self.cancelled_ids.add(vid)
    def get_engine_version(self): return importlib.metadata.version('yt-dlp')
    def force_update_ytdlp(self):
        """Manual update trigger from UI. Honors the same nightly setting as startup."""
        def on_complete(msg):
            self._send_to_js('showToast', msg, None, None)
        use_nightly = bool(self.settings.get('yt_dlp_use_nightly', False))
        self.updater.update_in_background(callback=on_complete, include_nightly=use_nightly)
    
    def _send_to_js(self, func, *args):
        if webview.windows: webview.windows[0].evaluate_js(f"{func}({', '.join(json.dumps(a) for a in args)})")

    def _log_to_protube_log(self, msg):
        """Append a line to data/protube.log so failures are visible after the
        fact. pythonw discards stdout, so plain print() vanishes; this is the
        same file main.py's _log_diag writes to. Best-effort, never raises."""
        try:
            from app_paths import data_dir
            log_path = os.path.join(data_dir(), 'protube.log')
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(msg.rstrip('\n') + '\n')
        except Exception:
            pass
        try:
            print(msg)
        except Exception:
            pass