import sys, threading

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
from search_mixin import SearchMixin
from streaming_mixin import StreamingMixin
from download_mixin import DownloadMixin
from settings_mixin import SettingsMixin
from video_library_mixin import VideoLibraryMixin
from channel_mixin import ChannelMixin
from repair_mixin import RepairMixin
from import_mixin import ImportMixin
from ui_mixin import UiMixin
from ydl_utils import _resolve_ffmpeg_location


class API(SettingsMixin, VideoLibraryMixin, ChannelMixin, RepairMixin, ImportMixin, UiMixin, MusicMixin, SearchMixin, StreamingMixin, DownloadMixin):
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

