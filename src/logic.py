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
from settings_store import SettingsStore
from music_mixin import MusicMixin
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
__version__ = '1.4.5'

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


class API(MusicMixin):
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

        # All persistent settings go through ONE locked door — SettingsStore.
        # `self.settings` stays bound to the store's live dict so the many
        # existing read sites (`self.settings.get(...)`) keep working unchanged.
        # New/changed code should WRITE via self._store.set/update/mutate/defer
        # so each change is mutated and persisted atomically under one lock.
        self._store = SettingsStore(self.settings_file)
        self._store.load()
        self.settings = self._store.data
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
            self._store.set("download_folder", self.download_folder)

        self.active_downloads = {}
        self.paused_ids = set()
        self.cancelled_ids = set()
        self.first_tick_seen = set()  # track per-video first progress tick for resume detection
        self.session_completed_ids = set()  # videos that finished during the current batch
        self.is_fetching = False
        # Per-URL fetch dedup. The legacy is_fetching boolean blocked ALL
        # subsequent fetches while one was running, which caused rapid
        # "Add to queue" clicks across different search cards to silently
        # drop everyone after the first. Track URLs individually so distinct
        # fetches run in parallel; only the exact-same URL is deduped.
        self._fetching_urls = set()
        self._fetching_urls_lock = threading.Lock()
        # Throttle gate for yt-dlp metadata ops (fetch + format probes).
        # Unbounded concurrent fetches blast YouTube and trip the no-cookies
        # bot wall ("Sign in to confirm you're not a bot"). Cap simultaneous
        # ops so a burst of channel adds / format resolutions stays under the
        # radar. Settings-overridable; default 3.
        _gate_n = int(self.settings.get('ytdlp_max_concurrent') or 3)
        self._ytdlp_gate = threading.BoundedSemaphore(max(1, _gate_n))
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

        # Background WebM → MP4 auto-converter REMOVED 2026-05-19. See
        # archive/auto_mp4_convert.md for the full code and rationale. Short
        # version: opt-in feature, never used in practice, and a daemon-thread
        # subprocess lifecycle bug leaked orphan ffmpegs that pegged the
        # machine. Removed rather than kept-and-fixed until there's a real
        # user need for .mp4-on-disk on Mac.

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
        # AV1 (av1/av01) plays natively in Chromium / WebView2 (Windows) but
        # WKWebView (Mac Safari) only decodes AV1 on M3+ hardware with macOS
        # 14.4+, and even then unreliably. On Mac we drop AV1 from the
        # playable list so the transcode-on-play path kicks in for any 2K/4K
        # YouTube download (which yt-dlp serves as AV1 since YouTube doesn't
        # publish AVC1 above 1080p). HEVC is omitted on Windows because it
        # needs the paid Microsoft HEVC Video Extensions most users don't have.
        is_mac = sys.platform == 'darwin'
        if ext in ('.mp4', '.m4v'):
            mp4_codecs = {'h264', 'avc1'} if is_mac else {'h264', 'avc1', 'av1', 'av01'}
            return v in mp4_codecs and a in ('aac', 'mp3', None)
        if ext == '.webm':
            # Safari has solid VP9-in-WebM support since macOS 11 (Big Sur),
            # so VP9 stays playable on Mac. AV1-in-WebM is uncommon but if
            # it lands, force transcode just like AV1-in-MP4.
            webm_codecs = {'vp8', 'vp9'} if is_mac else {'vp8', 'vp9', 'av1', 'av01'}
            return v in webm_codecs and a in ('opus', 'vorbis', None)
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
            try: self._send_to_js('protubePrepProgress', job_id, 1)
            except Exception: pass

            # Stage 1 is a pure container rewrite — codecs stream through
            # untouched. That's only useful when the source CODECS would
            # be playable in MP4. For codec-driven incompat (AV1 on Mac,
            # HEVC anywhere) the remux just produces an identical-codec
            # MP4 that the player still can't decode, wasting time before
            # falling through. Predict the remuxed file's playability and
            # skip stage 1 if it wouldn't help.
            src_v = (info or {}).get('video') or ''
            src_a = (info or {}).get('audio') or ''
            _mp4_v = ({'h264', 'avc1'} if sys.platform == 'darwin'
                      else {'h264', 'avc1', 'av1', 'av01'})
            _mp4_a = {'aac', 'mp3', ''}
            can_remux = src_v in _mp4_v and src_a in _mp4_a
            if not can_remux:
                self._prep_log(f'[ProTube/transcode] skipping stage 1 remux — '
                      f'codecs {src_v}/{src_a} not playable in MP4 on this '
                      f'platform; going straight to libx264 transcode '
                      f'(job={job_id})')
                rr = None  # signal "remux didn't run / didn't succeed"
                # Jump to stage 2 below by leaving remux_tmp absent.
                remux_tmp = None
            else:
                # ----- STAGE 1: -c copy remux ---------------------------
                # No re-encoding, just container rewrite. ffmpeg streams the
                # video and audio bitstreams unmodified into a new MP4. For an
                # MKV with H.264 + AAC this is a few seconds for a multi-GB file.
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

    # YouTube alternate player clients used to dodge the "Sign in to confirm
    # you're not a bot" wall WITHOUT cookies. YouTube enforces the bot-check
    # most aggressively on the default `web` client; these clients hit
    # different API surfaces that are challenged far less. yt-dlp tries them
    # in order and uses the first that succeeds. This is settings-overridable
    # (`youtube_player_clients`) so it can be retuned without a rebuild when
    # YouTube patches a client. We DON'T force these on the happy path — yt-dlp's
    # built-in default is best for the common case — we only swap them in on a
    # bot-check retry, so a stale list can never make the normal path worse.
    _BOT_FALLBACK_CLIENTS = ['tv', 'web_safari', 'mweb', 'android_vr']

    def _get_ydl_opts(self, cookie_mode, cookie_value, player_clients=None):
        opts = {'quiet': True, 'no_warnings': True, 'noprogress': True, 'ratelimit': 10*1024*1024}
        if self.ffmpeg_location:
            opts['ffmpeg_location'] = self.ffmpeg_location
        if cookie_mode == 'browser' and cookie_value != 'none': opts['cookiesfrombrowser'] = (cookie_value,)
        elif cookie_mode == 'file' and os.path.exists(cookie_value): opts['cookies'] = cookie_value
        # Optional explicit player-client override (used by the bot-check retry,
        # or pinned permanently via settings for users who always get walled).
        clients = player_clients or self.settings.get('youtube_player_clients')
        if clients:
            opts['extractor_args'] = {'youtube': {'player_client': list(clients)}}
        return opts

    def _is_bot_check_error(self, msg):
        """True if a yt-dlp error is YouTube's no-cookies bot wall. Drives the
        automatic alternate-client retry."""
        m = (msg or '').lower()
        return ("confirm you" in m and "not a bot" in m) or "sign in to confirm" in m

    def _ydl_extract(self, url, opts, download=False):
        """Extract via yt-dlp through the shared throttle gate, with one automatic
        alternate-client retry on YouTube's no-cookies bot wall. This is the same
        resilience the video fetch/download paths get inline (_fetch_worker,
        _resolve_formats_worker, _download_worker); the music paths used to call
        `with YoutubeDL(opts) as ydl: ydl.extract_info(...)` raw, so they had NO
        throttle and NO bot-wall fallback — which is exactly why music actions
        tripped the "Sign in to confirm you're not a bot" wall. Routing them
        through here fixes that."""
        with self._ytdlp_gate:
            try:
                with YoutubeDL(opts) as ydl:
                    return ydl.extract_info(url, download=download)
            except Exception as e:
                if self._is_bot_check_error(str(e)):
                    retry = {**opts, 'extractor_args': {'youtube': {'player_client': list(self._BOT_FALLBACK_CLIENTS)}}}
                    with YoutubeDL(retry) as ydl:
                        return ydl.extract_info(url, download=download)
                raise

    def fetch_url_info(self, url, cookie_mode, cookie_value):
        # Dedupe ONLY the same URL — distinct URLs run concurrently, so
        # rapid clicks across different search cards all succeed instead of
        # silently dropping after the first.
        with self._fetching_urls_lock:
            if url in self._fetching_urls:
                return
            self._fetching_urls.add(url)
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
        original_url = url   # preserved so we release the per-URL lock on the
                             # same key fetch_url_info stored.
        # Bare channel URLs get auto-rewritten to /videos so the fetch returns
        # only long-form videos, not Shorts/Streams/Community mashed together.
        url = self._normalize_channel_url(url)
        try:
            # Throttle: cap how many yt-dlp network ops run at once so a burst
            # of fetches (many channels / format probes) doesn't trip YouTube's
            # bot wall. See _ytdlp_gate.
            with self._ytdlp_gate:
                self._run_fetch(url, cookie_mode, cookie_value)
        except Exception as e:
            # Bot wall? Retry once through alternate player clients (no cookies).
            if self._is_bot_check_error(str(e)):
                try:
                    with self._ytdlp_gate:
                        self._run_fetch(url, cookie_mode, cookie_value,
                                        player_clients=self._BOT_FALLBACK_CLIENTS)
                    return
                except Exception as e2:
                    e = e2
            self._send_to_js('finishFetch', f"Error: {str(e)}")
        finally:
            with self._fetching_urls_lock:
                self._fetching_urls.discard(original_url)

    def _run_fetch(self, url, cookie_mode, cookie_value, player_clients=None):
        """One fetch attempt — probe playlist-vs-video then dispatch. Split out
        of _fetch_worker so the bot-check path can re-run it with an alternate
        player-client set."""
        base_opts = self._get_ydl_opts(cookie_mode, cookie_value, player_clients=player_clients)
        probe_opts = {**base_opts, 'extract_flat': True, 'skip_download': True}
        with YoutubeDL(probe_opts) as ydl:
            probe = ydl.extract_info(url, download=False)
        is_playlist = probe.get('_type') == 'playlist' or 'entries' in probe
        if is_playlist:
            self._handle_playlist_fetch(probe, base_opts)
        else:
            self._handle_single_video_fetch(url, base_opts)

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
            # Use hqdefault.jpg (480×360, ~15-25KB) — mqdefault (320×180) was
            # visibly blurry on Retina/4K displays where channel cards still
            # render ~260px wide (the user flagged "low-res thumbnails"). hqdefault
            # always exists for every video, is still light enough that thumbs
            # don't flash blank while scrolling, and matches the fallback used
            # everywhere else in the app.
            vid_id = e.get('id')
            if vid_id:
                thumb = f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg"
            else:
                thumbs = e.get('thumbnails') or []
                thumb = (thumbs[-1].get('url') if thumbs else e.get('thumbnail')) or ''

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
        subtype = self._classify_playlist_url(source_url)
        # For channels, pull the full header bundle (avatar + banner + subscriber
        # count + description) so the detail-view hero can render a YouTube-style
        # channel page instead of just a tile + name.
        channel_meta = {}
        if subtype == 'channel':
            channel_meta = self._extract_channel_metadata(probe)
        playlist = {
            "type": "playlist",
            "id": probe.get('id') or f"pl_{int(time.time())}",
            "url": source_url,
            # 'channel' vs 'playlist' — drives the badge in the UI and affects how
            # the "Check for updates" flow describes itself. Channel URLs (the user
            # pastes them from the channel's Videos tab) re-fetch the same way as
            # playlists; the distinction is purely semantic for display.
            "subtype": subtype,
            "title": probe.get('title', 'Untitled Playlist'),
            "uploader": probe.get('uploader') or probe.get('channel', 'N/A'),
            "uploader_url": probe.get('uploader_url') or probe.get('channel_url'),
            "videoCount": len(children),
            # Honor the user's Settings → Default quality preference. Falls back
            # to 1080p when unset. _user_default_quality maps 'best'/'audio'
            # abstractions to picker-compatible resolutions.
            "defaultQuality": self._user_default_quality(),
            "thumbnails": [c.get('thumbnail') for c in children[:4] if c.get('thumbnail')],
            "channelAvatar": channel_meta.get('avatar', ''),
            "channelBanner": channel_meta.get('banner', ''),
            "subscriberCount": channel_meta.get('subscriberCount'),
            "subscriberCountString": channel_meta.get('subscriberCountString', ''),
            "channelDescription": channel_meta.get('description', ''),
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

        def _extract(vid_url, clients=None):
            opts = (self._get_ydl_opts(cookie_mode, cookie_value, player_clients=clients)
                    if clients else base_opts)
            with self._ytdlp_gate:
                with YoutubeDL(opts) as ydl:
                    return ydl.extract_info(vid_url, download=False)

        for idx, item in enumerate(video_urls):
            vid_id = item.get('id')
            vid_url = item.get('url')
            if not vid_id or not vid_url:
                continue
            try:
                try:
                    info = _extract(vid_url)
                except Exception as e_first:
                    # No-cookies bot wall → retry this video via alternate clients.
                    if self._is_bot_check_error(str(e_first)):
                        info = _extract(vid_url, clients=self._BOT_FALLBACK_CLIENTS)
                    else:
                        raise
                formats, size_map = self._parse_formats(info)
                payload = {
                    "id": vid_id,
                    "formats": formats,
                    "sizeMap": size_map,
                    # Updated fields in case flat mode missed them:
                    "uploader": info.get('channel') or info.get('uploader'),
                    "thumbnail": info.get('thumbnail'),
                    "duration_string": self._format_duration(info.get('duration')),
                    # For channel-PREVIEW cards (shown only there): the flat channel
                    # fetch has no view count / date, but this full extract does.
                    "view_count_string": self._format_view_count(info.get('view_count')),
                    "published_time": self._relative_published(info),
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
                    # YouTube only serves H.264 (avc1) up to 1080p. At 1440p / 2160p
                    # the streams are AV1-in-MP4 or VP9-in-WebM. The avc1-first
                    # selector below silently matched the 1080p avc1 stream when h
                    # was 1440/2160 (it satisfies [height<=2160] AND [vcodec^=avc1]),
                    # so 2K/4K downloads always came down as 1080p. Above 1080p we
                    # skip the avc1 filter; the in-app player handles av01/vp9 natively
                    # per _is_player_compatible. At ≤1080p we keep the avc1-preferred
                    # cascade for maximum device compatibility (older Smart TVs etc.).
                    if int(h) > 1080:
                        # >1080p has no AVC1 stream on YouTube — pick AV1-MP4 or VP9-WebM.
                        # On Mac, prefer VP9-in-WebM: Safari/WKWebView decodes it natively,
                        # so playback is instant. AV1-in-MP4 would need a slow ffmpeg
                        # libx264 transcode on every first play (the in-app player can't
                        # decode AV1 on Macs without M3+/macOS 14.4+ hardware), so we
                        # actively avoid it when a VP9 stream is published. On Windows
                        # Chromium handles AV1-in-MP4 natively, so we keep MP4-first.
                        #
                        # 2K/4K-specific fix: for some videos YouTube silently
                        # PO-token-gates the default `web` client down to <=1080p
                        # with NO error raised, so the cascade below bottoms out at
                        # `best[height<=h]` = a muxed 720p — the "I picked 4K and got
                        # 720p" bug. The `android_vr` client is confirmed (probed
                        # 2026-05-21) to expose the full VP9-WebM + AV1 4K ladder AND
                        # WebM/opus audio cookie-free, so we request it alongside the
                        # defaults for >1080p. Scoped to >1080p so the <=1080p happy
                        # path is untouched. ('default' keeps yt-dlp's normal client
                        # set; we just add android_vr on top.)
                        ea = opts.setdefault('extractor_args', {})
                        yt_ea = ea.setdefault('youtube', {})
                        clients = list(yt_ea.get('player_client') or [])
                        if not clients:
                            clients = ['default']
                        if 'android_vr' not in clients:
                            clients.append('android_vr')
                        yt_ea['player_client'] = clients
                        if sys.platform == 'darwin':
                            opts['format'] = (
                                f'bestvideo[height<={h}][ext=webm][vcodec^=vp9]+bestaudio[ext=webm]/'
                                f'bestvideo[height<={h}][ext=webm]+bestaudio[ext=webm]/'
                                f'bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/'
                                f'bestvideo[height<={h}]+bestaudio/'
                                f'best[height<={h}]'
                            )
                            # Don't force MP4 here — let yt-dlp keep webm if the first
                            # cascade rung matched (vp9+opus stays in webm, playable as-is).
                            # The MP4 remuxer below is also dropped on this branch so we
                            # don't reencode VP9 → unsupported VP9-in-MP4.
                        else:
                            opts['format'] = (
                                f'bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/'
                                f'bestvideo[height<={h}]+bestaudio/'
                                f'best[height<={h}]'
                            )
                            opts['merge_output_format'] = 'mp4'
                            opts['postprocessors'] = [{'key': 'FFmpegVideoRemuxer', 'preferedformat': 'mp4'}]
                    else:
                        # ≤1080p — AVC1 exists on YouTube, prefer it for max device
                        # compatibility. Cross-platform identical behavior here.
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

                try:
                    with YoutubeDL(opts) as ydl: ydl.download([video_data['url']])
                except Exception as e_dl:
                    # No-cookies bot wall mid-download → retry once through the
                    # alternate player clients. Same opts, just a different
                    # client surface for the format/stream URLs.
                    if self._is_bot_check_error(str(e_dl)) and video_id not in self.cancelled_ids and video_id not in self.paused_ids:
                        retry_opts = {**opts, 'extractor_args': {'youtube': {'player_client': list(self._BOT_FALLBACK_CLIENTS)}}}
                        with YoutubeDL(retry_opts) as ydl: ydl.download([video_data['url']])
                    else:
                        raise

                # Pipeline done (download + any post-process/merge). Emit the
                # final 100% tick the unified-progress hook held back; this
                # is the visual jump from 99 → 100 the user sees only after
                # the merge has actually completed.
                try:
                    _st = self._dl_state.get(video_id, {})
                    final_total = _st.get('known_total') or _st.get('done_so_far', 0)
                    self._send_to_js('updateItemProgress', video_id, 100, '',
                                     playlist_id, final_total, final_total, 0)
                    if hasattr(self, '_last_progress') and video_id in self._last_progress:
                        self._last_progress[video_id] = {'pct': 100, 'speed': '', 'playlist_id': playlist_id}
                except Exception:
                    pass
                finally:
                    if hasattr(self, '_dl_state'):
                        self._dl_state.pop(video_id, None)

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
                    # Drop the unified-progress accumulator so a future download
                    # of the same id starts from scratch (no stale prior bytes).
                    if hasattr(self, '_dl_state'):
                        self._dl_state.pop(video_id, None)
                    if len(self.active_downloads) == 0:
                        # Batch finished. Send count of fresh completions so UI can toast
                        # accurately. If count is 0 (all paused/cancelled/errored), UI stays quiet.
                        completed_count = len(self.session_completed_ids)
                        self.session_completed_ids.clear()
                        self._send_to_js('finishProcessing', completed_count)

    def _classify_error(self, msg):
        """Classify a yt-dlp / download exception into a (category, friendly_message) tuple.
        Categories: network, geo, rate_limit, unavailable, age_restricted, format, disk, stale_resume, bot_check, generic."""
        m = (msg or '').lower()

        # YouTube no-cookies bot wall — we already auto-retried through alternate
        # player clients before this point, so reaching here means even those
        # were challenged. Tell the user to slow down rather than dumping the
        # raw yt-dlp "use --cookies" hint.
        if self._is_bot_check_error(m):
            return ('rate_limit', "YouTube is bot-checking this IP. Wait a few minutes and retry — fewer at once helps.")

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
        status = d.get('status')
        if status == 'finished' and captured_filepath is not None:
            fn = d.get('filename') or ''
            current = captured_filepath.get('path') or ''
            # Don't overwrite a merged container with an intermediate stream
            current_is_good = current and not self._looks_intermediate(current) and current.lower().endswith(('.mp4', '.mkv', '.webm'))
            if not current_is_good:
                captured_filepath['path'] = fn

        # Unified-progress state machine. yt-dlp downloads streams in series
        # (e.g. video → audio → merge) and resets 'downloaded_bytes' to 0
        # between them. A naive emit produces TWO climbs to 100% AND a visible
        # regression when stream 2 starts: stream 1 finishes at "99%" (capped
        # against its own total), then stream 2's first tick re-computes
        # against a bigger total and the bar drops to ~5%.
        #
        # Fix: take the TOTAL upfront from `info_dict.requested_formats` so the
        # denominator is fixed for the entire download — every stream's bytes
        # contribute to the same pie. The reported pct then climbs monotonically
        # to ≤99. After ydl.download() returns (merge done), the worker emits
        # 100 explicitly. A monotone clamp catches yt-dlp edge cases where
        # filesize fields are missing or wrong.
        if not hasattr(self, '_dl_state'):
            self._dl_state = {}
        state = self._dl_state.setdefault(vid, {
            'known_total': 0,
            'done_so_far': 0,
            'finished_streams': set(),
            'last_pct': 0.0,
        })

        # Pre-compute total bytes ONCE from yt-dlp's planned formats. Falls back
        # to per-tick totals further down if requested_formats is unavailable
        # (e.g. audio-only single-stream downloads).
        if state['known_total'] <= 0:
            info = d.get('info_dict') or {}
            rf = info.get('requested_formats') or []
            if rf:
                total_sz = 0
                for f in rf:
                    sz = f.get('filesize') or f.get('filesize_approx') or 0
                    if sz:
                        total_sz += sz
                state['known_total'] = total_sz

        def _emit(pct_value, combined_done, combined_total, speed_str='', speed_bytes=0):
            # Monotone clamp — once we've shown N%, never go below N. Belt-and-
            # braces protection if requested_formats lied about sizes.
            if pct_value < state['last_pct']:
                pct_value = state['last_pct']
            state['last_pct'] = pct_value
            if not hasattr(self, '_last_progress'):
                self._last_progress = {}
            self._last_progress[vid] = {
                'pct': pct_value, 'speed': speed_str, 'playlist_id': playlist_id
            }
            self._send_to_js('updateItemProgress', vid, pct_value, speed_str,
                             playlist_id, combined_done, combined_total, speed_bytes)

        if status == 'finished':
            # Add this stream's total to the cumulative-finished bytes. Track
            # by filename so a stream is never double-counted if yt-dlp fires
            # 'finished' more than once per stream.
            fname = d.get('filename') or d.get('tmpfilename') or ''
            if fname and fname not in state['finished_streams']:
                state['finished_streams'].add(fname)
                s_total = (d.get('total_bytes') or d.get('total_bytes_estimate')
                           or d.get('downloaded_bytes', 0) or 0)
                state['done_so_far'] += s_total
            # Use known_total when we have it; fall back to done_so_far so a
            # single-stream finish still reports a sensible (~99) value.
            denom = state['known_total'] if state['known_total'] > 0 else state['done_so_far']
            if denom > 0:
                pct = min(99.0, (state['done_so_far'] / denom) * 100)
                _emit(pct, state['done_so_far'], denom)
            return

        if status == 'downloading':
            s_done = d.get('downloaded_bytes', 0)
            combined_done = state['done_so_far'] + s_done
            # Prefer the pre-computed known_total; fall back to (cumulative
            # finished + current stream's reported total) if it isn't known.
            if state['known_total'] > 0:
                combined_total = state['known_total']
            else:
                s_total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                combined_total = state['done_so_far'] + s_total

            if combined_total > 0:
                pct = min(99.0, (combined_done / combined_total) * 100)
                speed_bytes = d.get('speed') or 0
                speed_str = self._format_bytes(d.get('speed')) + "/s"

                # Detect resume-from-partial on the first progress tick.
                # If we've got >3% already downloaded, yt-dlp picked up from a .part file.
                if vid not in self.first_tick_seen:
                    self.first_tick_seen.add(vid)
                    if pct > 3:
                        filename = d.get('filename', '')
                        title = os.path.basename(filename).rsplit('.', 1)[0] if filename else 'a previous download'
                        if len(title) > 40:
                            title = title[:37] + '...'
                        self._send_to_js('showResumeToast', title, round(pct))

                _emit(pct, combined_done, combined_total, speed_str, speed_bytes)

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
        """Back-compat shim. Settings loading + corruption recovery now live in
        SettingsStore (the single locked door). Kept so any caller still works."""
        return self._store.load()

    def _save_settings(self):
        """Back-compat shim. Every persist now goes through the one locked door
        in SettingsStore, so two threads can no longer interleave a write and
        corrupt the file. Kept under the old name so the ~30 existing callers in
        this file don't all need editing at once — they migrate to
        self._store.set/update/mutate incrementally."""
        self._store.save()

    @contextlib.contextmanager
    def _deferred_save(self):
        """Back-compat shim — write-coalescing now lives in SettingsStore.defer()."""
        with self._store.defer():
            yield
    def on_dom_ready(self): pass
    def choose_folder(self):
        result = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
        if result and len(result) > 0:
            self.download_folder = result[0]
            self._store.set('download_folder', self.download_folder)
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
        # Migrated to the single door: delete-and-persist is one atomic step.
        self._store.delete('_onboarded')
        return {'ok': True}
    def save_queue(self, q): self._store.set('queue', q)
    def load_queue(self): return self._store.get('queue', [])

    def get_setting(self, key):
        """Generic settings read for frontend use (feature flags, one-time migration markers)."""
        return self._store.get(key)

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

        # Channel URL for the "open the creator's channel" preview. The owner link
        # lives in the byline runs' navigationEndpoint; prefer the @handle path,
        # fall back to /channel/<id>.
        channel_url = ''
        def _url_from_be(_be):
            _canon = (_be or {}).get('canonicalBaseUrl') or ''
            _bid = (_be or {}).get('browseId') or ''
            if _canon.startswith('/'):
                return f'https://www.youtube.com{_canon}'
            if _bid.startswith('UC'):
                return f'https://www.youtube.com/channel/{_bid}'
            return ''
        for _key in ('ownerText', 'longBylineText', 'shortBylineText'):
            for _run in ((vr.get(_key) or {}).get('runs') or []):
                channel_url = _url_from_be((_run.get('navigationEndpoint') or {}).get('browseEndpoint'))
                if channel_url:
                    break
            if channel_url:
                break
        # Fallback: the channel-avatar link carries the same browseEndpoint and is
        # present on videos whose byline runs omit it (was leaving channel_url empty
        # for some results, so their creator row wasn't clickable).
        if not channel_url:
            channel_url = _url_from_be((ctwlr.get('navigationEndpoint') or {}).get('browseEndpoint'))

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
            'channel_url': channel_url,                   # creator channel → preview
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

    def _relative_published(self, info):
        """A 'N years/months/... ago' string from a yt-dlp info dict's timestamp or
        upload_date (YYYYMMDD). Empty when neither is present. Used for channel-
        preview cards so they read like the search results."""
        ts = info.get('timestamp') or info.get('release_timestamp')
        if not ts:
            ud = str(info.get('upload_date') or '')
            if len(ud) == 8:
                try:
                    import datetime as _dt
                    ts = _dt.datetime(int(ud[:4]), int(ud[4:6]), int(ud[6:8])).timestamp()
                except Exception:
                    ts = None
        if not ts:
            return ''
        try:
            secs = max(0, int(time.time()) - int(ts))
        except Exception:
            return ''
        for name, span in (('year', 31536000), ('month', 2592000), ('week', 604800),
                           ('day', 86400), ('hour', 3600), ('minute', 60)):
            if secs >= span:
                n = secs // span
                return f"{n} {name}{'s' if n != 1 else ''} ago"
        return 'just now'

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
        """Generic settings write. Frontend uses this for things like migration markers.
        Migrated to the single door: mutation + persist happen atomically under one lock."""
        self._store.set(key, value)
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
        self._store.set('max_concurrent_downloads', n)
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

        # Pick the right asset for the platform we're running on. The original
        # filter was `.exe`-only, which left downloadUrl empty for Mac users
        # (their release asset is a .zip) so the in-app Download button did
        # nothing. Match by extension AND a platform keyword so Mac doesn't
        # accidentally grab a Windows zip when both are attached to one release.
        # Two-pass: keyword+ext required first, ext-only as fallback for legacy
        # releases (v1.0.0–v1.2.0 named the .exe without a 'windows' keyword).
        if sys.platform == 'darwin':
            kw, exts = ('mac', 'osx', 'darwin'), ('.dmg', '.zip')
        elif sys.platform == 'win32':
            kw, exts = ('win',), ('.exe', '.msi', '.zip')
        else:
            kw, exts = ('linux',), ('.appimage', '.deb', '.tar.gz', '.zip')

        assets = gh.get('assets') or []
        picked = None
        for ext in exts:
            for asset in assets:
                name = (asset.get('name') or '').lower()
                if name.endswith(ext) and any(k in name for k in kw):
                    picked = asset
                    break
            if picked:
                break
        if not picked:
            for ext in exts:
                for asset in assets:
                    name = (asset.get('name') or '').lower()
                    if name.endswith(ext):
                        picked = asset
                        break
                if picked:
                    break

        download_url = ''
        download_size_mb = None
        if picked:
            download_url = picked.get('browser_download_url') or ''
            size_bytes = picked.get('size') or 0
            if size_bytes:
                download_size_mb = round(size_bytes / (1024 * 1024), 1)

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

    # ----- In-app auto-update (Mac) -------------------------------------------
    # The flow: frontend calls start_update_download(url) → backend streams the
    # .dmg (or .zip) into the data dir, mounts/extracts the .app, stages it,
    # then emits 'protubeUpdateReady'. Frontend then calls
    # install_staged_update() which spawns a detached bash helper script and
    # quits the app. The helper waits for the parent to exit, swaps the .app
    # bundle on disk (sending the old one to Trash, not rm -rf), and relaunches.
    # Mac-only — Windows still uses the open-URL fallback because a running
    # .exe can't be replaced in place without a separate updater binary.

    def _current_app_bundle_path(self):
        """Return absolute path to the running .app bundle on Mac, or None if
        we're in dev mode (`python main.py`) or running on Windows."""
        if sys.platform != 'darwin' or not getattr(sys, 'frozen', False):
            return None
        exe = sys.executable
        marker = '/Contents/MacOS/'
        if marker in exe:
            return exe.split(marker)[0]
        return None

    def _update_staging_dir(self):
        """Where in-progress update downloads + extracted bundles live."""
        from app_paths import data_dir
        return os.path.join(data_dir(), 'update_staging')

    def _resolve_update_helper_path(self):
        """Locate the bundled update helper bash script."""
        candidates = []
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            candidates.append(os.path.join(sys._MEIPASS, 'protube_update_helper.sh'))
        here = os.path.dirname(os.path.abspath(__file__))
        candidates.append(os.path.join(here, '..', 'assets', 'mac', 'protube_update_helper.sh'))
        candidates.append(os.path.join(here, 'protube_update_helper.sh'))
        for p in candidates:
            if os.path.isfile(p):
                return os.path.abspath(p)
        return None

    def start_update_download(self, url):
        """Kick off an in-app update: download the .dmg/.zip at `url`, extract
        the .app inside, stage it for install. Returns immediately; progress
        is pushed to the frontend via _send_to_js events:
            protubeUpdateProgress {percent, state, msg}
            protubeUpdateReady    {staged_app_path, install_to}
            protubeUpdateError    {msg}
        """
        if sys.platform != 'darwin':
            self._send_to_js('protubeUpdateError', {
                'msg': 'In-app install is Mac-only. Use the Download button to grab the new version from GitHub.'
            })
            return False
        if not self._current_app_bundle_path():
            self._send_to_js('protubeUpdateError', {
                'msg': "Couldn't detect the install location (dev mode?). Open the release page to install manually."
            })
            return False
        if getattr(self, '_update_in_progress', False):
            return False
        self._update_in_progress = True

        def worker():
            try:
                self._update_download_and_stage(url)
            except Exception as e:
                self._send_to_js('protubeUpdateError', {'msg': f'Update failed: {e}'})
            finally:
                self._update_in_progress = False
        threading.Thread(target=worker, daemon=True).start()
        return True

    def _update_download_and_stage(self, url):
        """Worker: download archive, extract .app, emit ready event."""
        staging = self._update_staging_dir()
        shutil.rmtree(staging, ignore_errors=True)  # wipe prior attempt
        os.makedirs(staging, exist_ok=True)

        parsed_name = url.rsplit('/', 1)[-1].split('?', 1)[0] or 'protube_update.bin'
        download_path = os.path.join(staging, parsed_name)

        self._send_to_js('protubeUpdateProgress', {
            'percent': 0, 'state': 'downloading', 'msg': 'Downloading update…'
        })

        with requests.get(url, stream=True, timeout=30) as resp:
            if resp.status_code != 200:
                self._send_to_js('protubeUpdateError', {
                    'msg': f'Download failed (HTTP {resp.status_code})'
                })
                return
            total = int(resp.headers.get('Content-Length') or 0)
            downloaded = 0
            last_pct = -1
            with open(download_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = int(downloaded * 100 / total)
                        if pct != last_pct:
                            last_pct = pct
                            self._send_to_js('protubeUpdateProgress', {
                                'percent': pct, 'state': 'downloading',
                                'msg': f'Downloading… {pct}%'
                            })

        self._send_to_js('protubeUpdateProgress', {
            'percent': 100, 'state': 'extracting', 'msg': 'Extracting…'
        })
        staged_app = self._update_extract_app(download_path, staging)
        if not staged_app:
            return  # error already sent

        if not os.path.isdir(os.path.join(staged_app, 'Contents', 'MacOS')):
            self._send_to_js('protubeUpdateError', {
                'msg': "Downloaded archive didn't contain a valid .app bundle."
            })
            return

        self._staged_update_app = staged_app
        self._send_to_js('protubeUpdateReady', {
            'staged_app_path': staged_app,
            'install_to': self._current_app_bundle_path(),
        })

    def _update_extract_app(self, archive_path, staging):
        """Pull the .app out of a downloaded .dmg or .zip into staging/extracted.
        Returns absolute path to the staged .app, or None on failure (after
        having sent a protubeUpdateError event)."""
        name_lower = archive_path.lower()
        extract_dir = os.path.join(staging, 'extracted')
        os.makedirs(extract_dir, exist_ok=True)

        if name_lower.endswith('.dmg'):
            try:
                mount = subprocess.run(
                    ['hdiutil', 'attach', '-nobrowse', '-noverify',
                     '-mountrandom', '/tmp', archive_path],
                    capture_output=True, text=True, timeout=60,
                )
                if mount.returncode != 0:
                    self._send_to_js('protubeUpdateError', {
                        'msg': f'DMG mount failed: {mount.stderr[:200]}'
                    })
                    return None
                # hdiutil prints lines like: "/dev/diskNsM\t<fs>\t<mountpoint>"
                mount_point = None
                for line in mount.stdout.splitlines():
                    parts = line.split('\t')
                    if len(parts) >= 3 and parts[-1].strip().startswith('/'):
                        mount_point = parts[-1].strip()
                        break
                if not mount_point:
                    self._send_to_js('protubeUpdateError', {
                        'msg': 'Could not determine DMG mount point.'
                    })
                    return None
                try:
                    app_in_dmg = None
                    for name in os.listdir(mount_point):
                        if name.endswith('.app'):
                            app_in_dmg = os.path.join(mount_point, name)
                            break
                    if not app_in_dmg:
                        self._send_to_js('protubeUpdateError', {
                            'msg': '.dmg did not contain a .app bundle.'
                        })
                        return None
                    dest = os.path.join(extract_dir, os.path.basename(app_in_dmg))
                    shutil.copytree(app_in_dmg, dest, symlinks=True)
                    return dest
                finally:
                    subprocess.run(['hdiutil', 'detach', mount_point, '-force'],
                                   capture_output=True, timeout=30)
            except Exception as e:
                self._send_to_js('protubeUpdateError', {'msg': f'DMG extract failed: {e}'})
                return None

        if name_lower.endswith('.zip'):
            try:
                # ditto handles resource forks + symlinks correctly, unlike unzip
                r = subprocess.run(['ditto', '-x', '-k', archive_path, extract_dir],
                                   capture_output=True, text=True, timeout=120)
                if r.returncode != 0:
                    self._send_to_js('protubeUpdateError', {
                        'msg': f'Unzip failed: {r.stderr[:200]}'
                    })
                    return None
                for name in os.listdir(extract_dir):
                    if name.endswith('.app'):
                        return os.path.join(extract_dir, name)
                self._send_to_js('protubeUpdateError', {
                    'msg': '.zip did not contain a .app bundle.'
                })
                return None
            except Exception as e:
                self._send_to_js('protubeUpdateError', {'msg': f'Zip extract failed: {e}'})
                return None

        self._send_to_js('protubeUpdateError', {
            'msg': f'Unsupported archive type: {os.path.basename(archive_path)}'
        })
        return None

    def install_staged_update(self):
        """Spawn the detached helper script to swap the .app on disk, then
        quit this process. Returns True if helper was spawned successfully."""
        staged = getattr(self, '_staged_update_app', None)
        install_to = self._current_app_bundle_path()
        if not staged or not install_to or not os.path.isdir(staged):
            self._send_to_js('protubeUpdateError', {'msg': 'No staged update to install.'})
            return False

        helper = self._resolve_update_helper_path()
        if not helper:
            self._send_to_js('protubeUpdateError', {
                'msg': 'Update helper script not found in app bundle.'
            })
            return False

        try:
            subprocess.Popen(
                ['/bin/bash', helper, staged, install_to],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,  # detach so it survives our exit
            )
        except Exception as e:
            self._send_to_js('protubeUpdateError', {'msg': f"Couldn't start updater: {e}"})
            return False

        # Quit on a short delay so the JS side can show a "Restarting…" toast
        # before the window vanishes. Helper sleeps 2s before swapping, giving
        # the OS plenty of room to fully release the .app bundle's file locks.
        def _quit():
            try:
                for w in webview.windows:
                    try:
                        w.destroy()
                    except Exception:
                        pass
            except Exception:
                pass
            os._exit(0)
        threading.Timer(0.6, _quit).start()
        return True

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
        # Guarded: only kick the worker when there's actually something to do.
        # Without this guard, the worker was being spawned on every load_library
        # call — and load_library is called from ~30 frontend code paths,
        # including post-event refreshes after every progress tick. That led
        # to hundreds of "starting worker (pending=0)" entries per second in
        # protube.log and saturated the bridge enough to make clicks/playback
        # feel laggy. Now we scan pending once here and skip the kick entirely
        # when there's no work.
        try:
            if self._has_pending_frame_extraction():
                self._start_frame_extraction_worker()
        except Exception:
            pass
        return self.settings.get('library', [])

    def _has_pending_frame_extraction(self):
        """Cheap pending-check used by load_library to avoid spawning the
        frame-extraction worker when there's nothing for it to do."""
        try:
            if not self.settings.get('auto_extract_frame_thumbnails', True):
                return False
            for v in self.settings.get('library', []):
                if v.get('type') == 'playlist':
                    for c in v.get('videos', []):
                        if self._needs_auto_frame_thumb(c):
                            return True
                elif self._needs_auto_frame_thumb(v):
                    return True
        except Exception:
            return False
        return False

    def save_library(self, lib):
        """Overwrite the library. Frontend calls this after reorder/remove operations."""
        self._store.set('library', lib)

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
        self.settings['library'] = lib  # noqa: direct-to-live-dict
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
        self._store.save()
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
        self._store.set('library', [v for v in lib if v.get('id') != video_id])
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
            self.settings['library'] = [v for v in lib if v.get('id') != video_id]  # noqa: direct-to-live-dict

        # Also fix the download QUEUE. A completed download lives in BOTH the
        # library and the queue (status 'Done'); the frontend re-adds Done queue
        # items to the library on load, so a delete that only touched the library
        # resurrected the entry on the next launch (the "deleted videos come back
        # from the dead" bug).
        #   - Standalone queued copy: drop it.
        #   - Channel/playlist child: do NOT remove it — the playlist/channel queue
        #     entry IS that catalog, so removing the child would make the video
        #     vanish from the channel grid on restart. Instead clear its 'Done'
        #     state so (a) it isn't re-added to the library on next load and (b) the
        #     card unlocks and is selectable/re-downloadable again.
        queue = self.settings.get('queue', []) or []
        new_queue = []
        for q in queue:
            if q.get('id') == video_id and q.get('type') != 'playlist':
                continue
            if q.get('type') == 'playlist':
                for c in q.get('videos', []) or []:
                    if c.get('id') == video_id:
                        c.pop('status', None)
                        c.pop('progressPct', None)
                        c.pop('missing', None)
                        c['selected'] = False
            new_queue.append(q)
        self.settings['queue'] = new_queue  # noqa: direct-to-live-dict

        # Drop any archived metadata snapshot too, so a later import-from-folder
        # can't silently restore the entry the user just deleted "forever".
        archive = self.settings.get('library_archive')
        if archive and target.get('filepath'):
            arc_key = self._archive_key(target.get('filepath'))
            if arc_key and arc_key in archive:
                try:
                    del archive[arc_key]
                except KeyError:
                    pass

        self._store.save()

        return {
            'ok': True,
            'deleted': deleted_files,
            'deleted_folder': deleted_folder,
            'skipped': skipped_files,
            'is_playlist_child': is_playlist_child,
        }

    def _extract_channel_branding(self, probe):
        """Pull (avatar_url, banner_url) from a yt-dlp channel probe.

        yt-dlp's `thumbnails` list mixes the channel avatar (square thumbs at
        48/88/176/720/900px) and the channel banner (wide thumbs at aspect > 2).
        Sometimes entries have an `id` like "avatar_uncropped" / "banner_uncropped"
        which we prefer when present. Otherwise we use aspect ratio:
          - avatar: w ≈ h  → pick the highest-res square
          - banner: w / h > 2.5 → pick the highest-res wide
        Returns ('', '') when neither can be found.
        """
        thumbs = probe.get('thumbnails') or []
        avatar_url, avatar_area = '', 0
        banner_url, banner_area = '', 0
        for t in thumbs:
            url = (t or {}).get('url') or ''
            if not url:
                continue
            w = t.get('width') or 0
            h = t.get('height') or 0
            tid = (t.get('id') or '').lower()
            area = w * h
            is_avatar = 'avatar' in tid or (w and h and abs(w - h) <= 4)
            is_banner = 'banner' in tid or (w and h and h > 0 and (w / h) >= 2.5)
            if is_avatar and area >= avatar_area:
                avatar_url, avatar_area = url, area or avatar_area or 1
            elif is_banner and area >= banner_area:
                banner_url, banner_area = url, area or banner_area or 1
        return avatar_url, banner_url

    def _channel_about_url(self, url):
        """Turn `youtube.com/@handle/videos` (or any tabbed channel URL) into
        `youtube.com/@handle/about` so we can fetch the full About-page
        description. Returns '' if the input doesn't look like a channel URL.
        """
        if not url or not isinstance(url, str):
            return ''
        u = url.rstrip('/')
        # Strip any explicit tab suffix
        for tab in ('/videos', '/shorts', '/streams', '/live', '/playlists',
                    '/community', '/about', '/featured', '/podcasts',
                    '/courses', '/membership', '/store', '/releases'):
            if u.lower().endswith(tab):
                u = u[: -len(tab)]
                break
        # Only append /about for channel-style URLs
        ul = u.lower()
        if '/@' in ul or '/channel/' in ul or '/c/' in ul or '/user/' in ul:
            return u + '/about'
        return ''

    def _extract_channel_metadata(self, probe):
        """Pull the full channel-header bundle from a yt-dlp channel probe so
        the detail-view hero can render a YouTube-style header instead of just
        a tile + name. Returns a dict with avatar, banner, subscriberCount
        (int), subscriberCountString ("1.59M subscribers"), description.

        Only meaningful for channel probes — playlist probes won't populate
        most of these and the caller should skip the call.
        """
        avatar, banner = self._extract_channel_branding(probe)
        followers = probe.get('channel_follower_count')
        subs_string = ''
        if isinstance(followers, int) and followers > 0:
            subs_string = self._format_compact_count(followers) + ' subscribers'
        description = (probe.get('description') or '').strip()
        return {
            'avatar': avatar,
            'banner': banner,
            'subscriberCount': followers if isinstance(followers, int) else None,
            'subscriberCountString': subs_string,
            'description': description,
        }

    def _format_compact_count(self, n):
        """1_590_000 → '1.59M'. Matches YouTube's compact display style.
        Used for subscriber/video counts so the channel header reads naturally.
        """
        try:
            n = int(n)
        except (TypeError, ValueError):
            return ''
        if n < 1000:
            return str(n)
        if n < 1_000_000:
            return ('%.2f' % (n / 1000)).rstrip('0').rstrip('.') + 'K'
        if n < 1_000_000_000:
            return ('%.2f' % (n / 1_000_000)).rstrip('0').rstrip('.') + 'M'
        return ('%.2f' % (n / 1_000_000_000)).rstrip('0').rstrip('.') + 'B'

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
        self._store.set('library', lib)
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

        # Refresh the full channel header bundle (avatar, banner, subscribers,
        # description) on every update check so existing channels that pre-date
        # these fields pick them up the next time the user hits "Check for new
        # videos" — or when the frontend auto-triggers it on first open. We
        # always overwrite: yt-dlp gives us fresh URLs each fetch.
        effective_subtype = target.get('subtype') or self._classify_playlist_url(url)
        channel_meta = {}
        if effective_subtype == 'channel':
            channel_meta = self._extract_channel_metadata(probe)
            # Mirror get_channel_metadata: pull the full About-page description
            # so "...more" reveals real long-form text, not the 1-2 line header
            # blurb the /videos tab returns. Best-effort.
            about_url = self._channel_about_url(url)
            if about_url:
                try:
                    about_opts = self._get_ydl_opts('browser', 'none')
                    about_opts.update({
                        'extract_flat': True,
                        'skip_download': True,
                        'lazy_playlist': True,
                    })
                    with YoutubeDL(about_opts) as ydl_about:
                        about_probe = ydl_about.extract_info(about_url, download=False)
                    about_desc = (about_probe.get('description') or '').strip()
                    if about_desc and len(about_desc) > len(channel_meta.get('description', '')):
                        channel_meta['description'] = about_desc
                except Exception:
                    pass

        # Stamp + persist only for library entries. The queue is owned by the
        # frontend (see save_queue / load_queue) and writing to it here would
        # race with the frontend's saveQueueState calls.
        if source == 'library':
            target['last_checked_at'] = last_checked_at
            if not target.get('subtype'):
                target['subtype'] = effective_subtype
            if channel_meta.get('avatar'):
                target['channelAvatar'] = channel_meta['avatar']
            if channel_meta.get('banner'):
                target['channelBanner'] = channel_meta['banner']
            if channel_meta.get('subscriberCount') is not None:
                target['subscriberCount'] = channel_meta['subscriberCount']
            if channel_meta.get('subscriberCountString'):
                target['subscriberCountString'] = channel_meta['subscriberCountString']
            if channel_meta.get('description'):
                target['channelDescription'] = channel_meta['description']
            self._save_settings()

        return {
            'ok': True,
            'source': source,
            'new': new_entries,
            'removed_ids': removed_ids,
            'total_now': len(remote_ids),
            'last_checked_at': last_checked_at,
            'subtype': effective_subtype,
            'channelAvatar': channel_meta.get('avatar', ''),
            'channelBanner': channel_meta.get('banner', ''),
            'subscriberCount': channel_meta.get('subscriberCount'),
            'subscriberCountString': channel_meta.get('subscriberCountString', ''),
            'channelDescription': channel_meta.get('description', ''),
        }

    def get_channel_metadata(self, playlist_id):
        """Cheap, entries-free fetch of a channel's header bundle (avatar,
        banner, subscribers, description). Used by the frontend to backfill
        legacy channel entries that pre-date these fields when the user
        opens the channel detail view — without the seconds-long page walk
        that `check_playlist_updates` does. Persists onto library entries.
        Returns {ok: bool, avatar, banner, subscriberCount,
        subscriberCountString, description, error?}.
        """
        target, source = None, None
        for v in self.settings.get('library', []):
            if v.get('id') == playlist_id and v.get('type') == 'playlist':
                target, source = v, 'library'
                break
        if not target:
            for v in self.settings.get('queue', []):
                if v.get('id') == playlist_id and v.get('type') == 'playlist':
                    target, source = v, 'queue'
                    break
        if not target or not target.get('url'):
            return {'ok': False, 'error': 'Not found'}

        try:
            opts = self._get_ydl_opts('browser', 'none')
            opts.update({
                'extract_flat': True,
                'skip_download': True,
                'lazy_playlist': True,
            })
            with YoutubeDL(opts) as ydl:
                # `extract_flat=True` + `lazy_playlist=True` already keeps entry
                # walks out of the path; we just never iterate probe['entries']
                # so no page fetches happen. We DO want full processing of the
                # channel-level metadata so the description / follower count come
                # through (process=False would strip those).
                probe = ydl.extract_info(target['url'], download=False)
            meta = self._extract_channel_metadata(probe or {})

            # The /videos tab returns the channel HEADER description (a 1-2
            # line blurb). The full About-page text only comes back when we
            # hit the /about endpoint specifically. Fetch it separately and
            # swap in whichever is longer — "Show more" should reveal real
            # content, not the same blurb.
            about_url = self._channel_about_url(target['url'])
            if about_url:
                try:
                    with YoutubeDL(opts) as ydl2:
                        about_probe = ydl2.extract_info(about_url, download=False)
                    about_desc = (about_probe.get('description') or '').strip()
                    if about_desc and len(about_desc) > len(meta.get('description', '')):
                        meta['description'] = about_desc
                except Exception:
                    pass   # /about fetch is best-effort
        except Exception as e:
            return {'ok': False, 'error': f'Fetch failed: {e}'}

        if source == 'library':
            if meta.get('avatar'):
                target['channelAvatar'] = meta['avatar']
            if meta.get('banner'):
                target['channelBanner'] = meta['banner']
            if meta.get('subscriberCount') is not None:
                target['subscriberCount'] = meta['subscriberCount']
            if meta.get('subscriberCountString'):
                target['subscriberCountString'] = meta['subscriberCountString']
            if meta.get('description'):
                target['channelDescription'] = meta['description']
            self._save_settings()

        return {
            'ok': True,
            'avatar': meta.get('avatar', ''),
            'banner': meta.get('banner', ''),
            'subscriberCount': meta.get('subscriberCount'),
            'subscriberCountString': meta.get('subscriberCountString', ''),
            'description': meta.get('description', ''),
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
        self._store.set('_library_migrated', False)
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

        self._store.set('library', new_lib)
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

        self._store.set('library', library)
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

        # Commit changes atomically.
        self._store.update({'library': library, 'queue': new_queue, '_library_migrated': True})

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