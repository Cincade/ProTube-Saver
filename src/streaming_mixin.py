"""Streaming domain mixin — localhost video server, transcode pipeline,
stream URL resolution, and yt-dlp extraction helpers.

Consumed by logic.API via multiple inheritance.
"""

import os
import re
import sys
import json
import time
import shutil
import threading
import subprocess
import contextlib
import traceback

from ydl_utils import YoutubeDL


class StreamingMixin:
    """Localhost video server, transcode pipeline, and stream-URL resolution."""

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

