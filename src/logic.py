import sys, threading

# Defense-in-depth UTF-8 reconfigure — see comment in the old monolith for
# the full rationale.  Kept here so this module is safe to import stand-alone.
for _s in (sys.stdout, sys.stderr):
    if _s is not None:
        try: _s.reconfigure(encoding='utf-8', errors='replace')
        except Exception: pass

from updater import YtDlpUpdater
from settings_store import SettingsStore
from app_context import AppContext
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


class API:
    """Thin facade over the 10 domain services.

    Constructs an AppContext (shared mutable state), instantiates each service
    with that context, wires cross-service dependencies, then delegates every
    method call to the right service via __getattr__.  The pywebview bridge
    sees a single api object and calls api.method() — the delegation is
    invisible to it.
    """

    def __init__(self):
        from app_paths import (
            settings_path, thumbnails_dir, default_downloads_dir, ytdlp_runtime_dir,
        )

        # ── shared infrastructure ────────────────────────────────────────────
        store = SettingsStore(settings_path())
        store.load()
        settings = store.data

        download_folder = default_downloads_dir()
        saved = settings.get('download_folder')
        if saved:
            download_folder = saved
        else:
            store.set('download_folder', download_folder)

        ctx = AppContext(store, download_folder, thumbnails_dir())

        # yt-dlp concurrency controls
        _gate_n = int(settings.get('ytdlp_max_concurrent') or 3)
        ctx._ytdlp_gate = threading.BoundedSemaphore(max(1, _gate_n))
        ctx.max_concurrent_downloads = max(1, min(8, int(settings.get('max_concurrent_downloads') or 2)))
        ctx.download_semaphore = threading.Semaphore(ctx.max_concurrent_downloads)
        ctx.ffmpeg_location = _resolve_ffmpeg_location()

        # ── Phase 1: construct services (no cross-refs yet) ──────────────────
        updater = YtDlpUpdater(ytdlp_runtime_dir())

        self._ui       = UiMixin(ctx)                   # leaf — registers send_to_js on ctx
        self._settings = SettingsMixin(ctx, updater)
        self._library  = VideoLibraryMixin(ctx)
        self._channel  = ChannelMixin(ctx)
        self._repair   = RepairMixin(ctx)
        self._import   = ImportMixin(ctx)
        self._search   = SearchMixin(ctx)
        self._streaming = StreamingMixin(ctx)
        self._downloads = DownloadMixin(ctx)
        self._music    = MusicMixin(
            ctx,
            max_concurrent_music_downloads=max(1, min(8, int(
                settings.get('max_concurrent_music_downloads') or 1
            ))),
        )

        # ── Phase 2: wire cross-service dependencies ─────────────────────────
        self._ui.wire(settings_svc=self._settings)
        self._search.wire(settings_svc=self._settings)
        self._library.wire(import_svc=self._import)
        self._channel.wire(streaming_svc=self._streaming, download_svc=self._downloads)
        self._repair.wire(
            channel_svc=self._channel,
            import_svc=self._import,
            download_svc=self._downloads,
        )
        self._import.wire(library_svc=self._library, repair_svc=self._repair)
        self._streaming.wire(settings_svc=self._settings, import_svc=self._import)
        self._downloads.wire(
            streaming_svc=self._streaming,
            channel_svc=self._channel,
            settings_svc=self._settings,
            repair_svc=self._repair,
            library_svc=self._library,
        )
        self._music.wire(
            search_svc=self._search,
            repair_svc=self._repair,
            download_svc=self._downloads,
            import_svc=self._import,
            streaming_svc=self._streaming,
        )

        # ── Phase 3: post-wire startup ───────────────────────────────────────
        self._music._sanitize_music_queue_on_startup()
        threading.Thread(target=self._music._music_queue_processor, daemon=True).start()

        use_nightly = bool(settings.get('yt_dlp_use_nightly', False))
        updater.check_on_startup(silent=True, include_nightly=use_nightly)

        # Local HTTP server for in-app video playback
        self._streaming._start_video_server()

        # ordered list used by __getattr__ delegation
        self._all_services = [
            self._ui, self._settings, self._library, self._channel,
            self._repair, self._import, self._search, self._streaming,
            self._downloads, self._music,
        ]

        # pywebview enumerates the api object via inspect.getmembers() to expose
        # methods to JS.  __getattr__ alone is invisible to that introspection
        # (it's only invoked on miss, not on enumeration), so every JS call
        # silently returns undefined.  Bind each service's public methods onto
        # self so dir(api) + inspect see them as real attributes.  First-owner
        # wins, matching __getattr__'s precedence.
        for svc in self._all_services:
            for name in dir(svc):
                if name.startswith('_') or name in self.__dict__:
                    continue
                attr = getattr(svc, name)
                if callable(attr):
                    setattr(self, name, attr)

    def __getattr__(self, name):
        """Fallback for any attribute not bound during __init__ (e.g. methods
        added to a service after construction).  Walks _all_services in order."""
        for svc in self.__dict__.get('_all_services', []):
            try:
                return getattr(svc, name)
            except AttributeError:
                continue
        raise AttributeError(f"'API' has no attribute '{name}'")
