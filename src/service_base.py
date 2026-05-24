"""Base class for all ProTube domain services.

Exposes AppContext fields as `self.*` properties so every service method
body reads exactly as before — no mass find-replace needed.  A service
that reads `self.settings` is reading `ctx.settings`; one that calls
`self._save_settings()` is flushing the store through the same lock as
always.
"""
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app_context import AppContext


class Service:
    def __init__(self, ctx: 'AppContext'):
        self._ctx = ctx

    # ── shared state shortcuts ────────────────────────────────────────────────
    @property
    def settings(self): return self._ctx.settings

    @property
    def _store(self): return self._ctx.store

    @property
    def download_folder(self): return self._ctx.download_folder

    @download_folder.setter
    def download_folder(self, v): self._ctx.download_folder = v

    @property
    def thumbnail_cache_dir(self): return self._ctx.thumbnail_cache_dir

    @property
    def active_downloads(self): return self._ctx.active_downloads

    @property
    def paused_ids(self): return self._ctx.paused_ids

    @property
    def cancelled_ids(self): return self._ctx.cancelled_ids

    @property
    def first_tick_seen(self): return self._ctx.first_tick_seen

    @property
    def session_completed_ids(self): return self._ctx.session_completed_ids

    @property
    def is_fetching(self): return self._ctx.is_fetching

    @is_fetching.setter
    def is_fetching(self, v): self._ctx.is_fetching = v

    @property
    def _fetching_urls(self): return self._ctx._fetching_urls

    @property
    def _fetching_urls_lock(self): return self._ctx._fetching_urls_lock

    @property
    def _ytdlp_gate(self): return self._ctx._ytdlp_gate

    @property
    def max_concurrent_downloads(self): return self._ctx.max_concurrent_downloads

    @max_concurrent_downloads.setter
    def max_concurrent_downloads(self, v): self._ctx.max_concurrent_downloads = v

    @property
    def download_semaphore(self): return self._ctx.download_semaphore

    @download_semaphore.setter
    def download_semaphore(self, v): self._ctx.download_semaphore = v

    @property
    def ffmpeg_location(self): return self._ctx.ffmpeg_location

    # ── cross-cutting operations ──────────────────────────────────────────────
    def _send_to_js(self, func, *args):
        fn = self._ctx.send_to_js
        if fn is not None:
            fn(func, *args)

    def _log_to_protube_log(self, msg):
        fn = self._ctx.log_to_protube_log
        if fn is not None:
            fn(msg)

    def _save_settings(self):
        self._ctx.store.save()

    def _deferred_save(self):
        # Back-compat shim — callers that used the context-manager form
        # should switch to `with self._store.defer():` directly.
        return self._ctx.store.defer()
