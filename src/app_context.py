"""Shared mutable state passed to every service.

All inter-service shared variables live here.  A service that needs
`settings` or `download_folder` reads it from its `_ctx`; it never
reaches across to another service's private state.  Domain-specific
state (music queue, video server port) lives in the owning service's
own __init__ rather than here.
"""
import threading


class AppContext:
    def __init__(self, store, download_folder, thumbnail_cache_dir):
        self.store = store
        self.settings = store.data          # live reference — same dict object
        self.download_folder = download_folder
        self.thumbnail_cache_dir = thumbnail_cache_dir

        # Download runtime state (owned here so DownloadService and UiService
        # share the same set/dict rather than each having their own copy)
        self.active_downloads: dict = {}
        self.paused_ids: set = set()
        self.cancelled_ids: set = set()
        self.first_tick_seen: set = set()
        self.session_completed_ids: set = set()
        self.is_fetching: bool = False
        self._fetching_urls: set = set()
        self._fetching_urls_lock = threading.Lock()

        # yt-dlp concurrency controls — wired by API after limits are read
        self._ytdlp_gate = None             # threading.BoundedSemaphore
        self.max_concurrent_downloads: int = 2
        self.download_semaphore = None      # threading.Semaphore

        # ffmpeg location — set by API.__init__ via _resolve_ffmpeg_location()
        self.ffmpeg_location = None

        # Callback wired from UiService after construction
        self.send_to_js = None              # callable(func_name, *args)
        self.log_to_protube_log = None      # callable(msg)
