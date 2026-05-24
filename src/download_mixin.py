"""Download domain mixin — URL fetch/info, download workers, queue management,
playlist format resolution, and progress hooks.

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
import traceback

import requests

from ydl_utils import YoutubeDL
from service_base import Service


class DownloadMixin(Service):
    """URL fetching, video/playlist download workers, and queue management."""

    def __init__(self, ctx):
        super().__init__(ctx)
        self._streaming_svc = None  # wired: _get_ydl_opts, _is_bot_check_error
        self._channel_svc = None    # wired: _classify_playlist_url, _extract_channel_metadata
        self._settings_svc = None   # wired: _user_default_quality
        self._repair_svc = None     # wired: _cache_thumbnail
        self._library_svc = None    # wired: add_to_library

    def wire(self, *, streaming_svc, channel_svc, settings_svc, repair_svc, library_svc, **_):
        self._streaming_svc = streaming_svc
        self._channel_svc = channel_svc
        self._settings_svc = settings_svc
        self._repair_svc = repair_svc
        self._library_svc = library_svc

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
            if self._streaming_svc._is_bot_check_error(str(e)):
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
        base_opts = self._streaming_svc._get_ydl_opts(cookie_mode, cookie_value, player_clients=player_clients)
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
        pref = self._settings_svc._user_default_quality()
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
        subtype = self._channel_svc._classify_playlist_url(source_url)
        # For channels, pull the full header bundle (avatar + banner + subscriber
        # count + description) so the detail-view hero can render a YouTube-style
        # channel page instead of just a tile + name.
        channel_meta = {}
        if subtype == 'channel':
            channel_meta = self._channel_svc._extract_channel_metadata(probe)
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
            "defaultQuality": self._settings_svc._user_default_quality(),
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
                    marker = self._repair_svc._cache_thumbnail(thumb, vid_id)
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
        base_opts = self._streaming_svc._get_ydl_opts(cookie_mode, cookie_value)
        total = len(video_urls)

        def _extract(vid_url, clients=None):
            opts = (self._streaming_svc._get_ydl_opts(cookie_mode, cookie_value, player_clients=clients)
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
                    if self._streaming_svc._is_bot_check_error(str(e_first)):
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
                opts = self._streaming_svc._get_ydl_opts(mode, val)
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
                    if self._streaming_svc._is_bot_check_error(str(e_dl)) and video_id not in self.cancelled_ids and video_id not in self.paused_ids:
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
                        sub_opts = self._streaming_svc._get_ydl_opts(mode, val)
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
                        cached = self._repair_svc._cache_thumbnail(remote_thumb, video_id)
                        video_data['thumbnail'] = cached
                    self._library_svc.add_to_library(video_data)

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
        if self._streaming_svc._is_bot_check_error(m):
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

