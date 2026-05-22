        var app = {
            videosInQueue: [],
            videosInLibrary: [],
            librarySearchQuery: '',
            currentView: 'library',
            selectedVideoIds: new Set(),
            elements: {},
            currentFilter: 'all',

            async init() {
                this.mapElements();

                // Hydrate the "show hidden" library preference from settings so
                // the toggle's state and the render filter agree from first paint.
                // Default false (hidden items don't render until user opts in).
                try {
                    const showHidden = await pywebview.api.get_setting('show_hidden_library');
                    window._showHiddenLibrary = showHidden === true;
                } catch (_) {
                    window._showHiddenLibrary = false;
                }

                // Fire-and-forget update check. The backend caches results for
                // 24h via data/update_check.json, so this is a network hit only
                // once a day at most. If a newer version is published on the
                // landing site's version.json, we surface the corner pill.
                // Don't await — startup shouldn't block on this.
                this._checkForAppUpdates();

                // Honor "Open to" preference (Settings → Defaults). HTML defaults
                // to library-view active; if the user picked queue, switch now
                // before any data loads so they don't see a flash of library
                // before the swap.
                try {
                    const startupView = await pywebview.api.get_setting('default_startup_view');
                    if (startupView === 'queue') {
                        // switchView is defined later on the same `app` object —
                        // safe to call here because mapElements ran above.
                        this.switchView('queue');
                    }
                } catch (_) { /* default to library */ }

                // Load default playback speed so the player applies it on first open.
                try {
                    const spd = await pywebview.api.get_setting('default_speed');
                    this._defaultSpeed = parseFloat(spd) || 1;
                } catch (_) { this._defaultSpeed = 1; }

                // Cache AI feature settings so the player can branch without an extra
                // backend round-trip every time a video opens.
                try {
                    window._hasGroqKey = !!(await pywebview.api.get_setting('groq_api_key'));
                    window._autoPolishSubtitles = !!(await pywebview.api.get_setting('auto_polish_subtitles'));
                } catch (_) {
                    window._hasGroqKey = false;
                    window._autoPolishSubtitles = false;
                }

                // Load queue (as before)
                const q = await pywebview.api.load_queue();
                if (q?.length > 0) {
                    this.videosInQueue = q;
                    // Clean up stale "Downloading" status from a previous app session
                    const cleanStale = (v) => {
                        if (v.status === 'Downloading') {
                            v.status = 'Cancelled';
                            v.progressPct = null;
                            v.progressSpeed = null;
                        }
                    };
                    this.videosInQueue.forEach(item => {
                        if (item.type === 'playlist') {
                            (item.videos || []).forEach(cleanStale);
                        } else {
                            cleanStale(item);
                        }
                    });
                }

                // Load library — this triggers migration on first launch of this version
                this.videosInLibrary = (await pywebview.api.load_library()) || [];

                // Kick off a poller that watches the background frame-extraction
                // worker and refreshes the library each time a thumbnail lands.
                // Without this, the worker mutates settings.json silently and you'd
                // only see new thumbs after a manual refresh / app restart.
                this._startFrameExtractionPoll();

                // Onboarding check — show welcome modal if user hasn't been onboarded.
                // Runs after library load so the modal sits over an actually-rendered app.
                this._checkOnboarding();

                // Pre-warm the thumbnail cache. Backend returns {marker: data_url} for every
                // library thumbnail in one round-trip; we populate _thumbCache so when the
                // grid renders, the resolver finds everything in cache instantly. No flash.
                try {
                    const allThumbs = await pywebview.api.get_all_thumbnails();
                    if (allThumbs && typeof allThumbs === 'object') {
                        // Initialize cache if it doesn't exist yet (it lives on `this` in renderLibrary's closure)
                        if (!this._thumbCache) this._thumbCache = {};
                        if (!this._thumbInflight) this._thumbInflight = new Set();
                        Object.assign(this._thumbCache, allThumbs);
                    }
                } catch(_) {
                    // Pre-warm is best-effort; if it fails, resolver still works per-card
                }

                // Load saved rail preference. If user collapsed the rail before, restore that.
                try {
                    const railCollapsed = await pywebview.api.get_setting('rail_collapsed');
                    this._railUserPreference = !!railCollapsed;
                    if (railCollapsed) {
                        const cockpit = document.querySelector('.cockpit');
                        if (cockpit) cockpit.classList.add('rail-collapsed');
                    }
                } catch(_) {}

                // One-time recovery: if the user ran the earlier buggy migration and has Done
                // items still stuck in the queue, force a re-migration with the forgiving logic.
                // The backend marks _library_remigrated_v2 so this only runs once.
                try {
                    const hasStuckDones = this.videosInQueue.some(item => {
                        if (item.type === 'playlist') {
                            const sel = (item.videos || []).filter(c => c.selected !== false);
                            return sel.length > 0 && sel.every(c => c.status === 'Done');
                        }
                        return item.status === 'Done';
                    });
                    const migratedV2 = await pywebview.api.get_setting('_library_remigrated_v2');
                    if (hasStuckDones && !migratedV2) {
                        await pywebview.api.force_remigrate();
                        await pywebview.api.set_setting('_library_remigrated_v2', true);
                        this.videosInLibrary = (await pywebview.api.load_library()) || [];
                    }
                } catch(_) {
                    // If the helper methods aren't available for some reason, fail silently
                }

                // One-time library repair: fix broken filepaths, dedupe, remove broken playlist
                // entries, etc. Runs once, gated by _library_repaired_v1 flag.
                try {
                    const repairedV1 = await pywebview.api.get_setting('_library_repaired_v1');
                    if (!repairedV1) {
                        const result = await pywebview.api.repair_library();
                        await pywebview.api.set_setting('_library_repaired_v1', true);
                        this.videosInLibrary = (await pywebview.api.load_library()) || [];
                        // Summarize what changed so the user knows something happened.
                        if (result && (result.fixed_paths || result.dropped_broken ||
                                       result.dropped_queue_conflict || result.deduped)) {
                            const parts = [];
                            if (result.fixed_paths) parts.push(`${result.fixed_paths} fixed`);
                            if (result.dropped_broken) parts.push(`${result.dropped_broken} broken removed`);
                            if (result.dropped_queue_conflict) parts.push(`${result.dropped_queue_conflict} returned to queue`);
                            if (result.deduped) parts.push(`${result.deduped} duplicates merged`);
                            // Schedule the toast slightly after render so it's visible
                            setTimeout(() => {
                                if (typeof showToast === 'function') {
                                    showToast(`Library cleaned up: ${parts.join(' · ')}`, null, null);
                                }
                            }, 800);
                        }
                    }
                } catch(_) {}

                // Migrate existing library thumbnails from file:/// URLs (which the webview
                // can't render) to pt:thumb: markers (which the frontend resolves through
                // the backend as base64 data URLs). Runs silently every launch — no-op
                // after the first time since no entries will have file:/// URLs anymore.
                try {
                    const r = await pywebview.api.migrate_file_thumbnails_to_markers();
                    if (r && r.migrated > 0) {
                        this.videosInLibrary = (await pywebview.api.load_library()) || [];
                    }
                } catch(_) {}

                // After migration, load_queue's stale data may no longer match backend truth.
                // Re-pull queue to pick up any items the migration moved out of it.
                const qAfterMigration = await pywebview.api.load_queue();
                this.videosInQueue = qAfterMigration || [];

                this.elements.folderPath.textContent = await pywebview.api.get_download_folder();
                this.addEventListeners();
                this.renderQueue();
                this.renderLibrary();

                // Splash dismissal — fade out smoothly now that the UI is rendered.
                // Slight delay (180ms) so the fade-out feels gentle rather than abrupt.
                // The CSS transition handles the actual fade; we just toggle the class.
                setTimeout(() => {
                    const splash = document.getElementById('splash');
                    if (splash) {
                        splash.classList.add('fade-out');
                        // Remove from DOM after transition so it doesn't intercept any events
                        setTimeout(() => { try { splash.remove(); } catch(_) {} }, 400);
                    }
                }, 180);

                // Auto-refetch metadata for imported videos in the background. Runs ONCE per
                // Auto-refetch was killed — too unreliable, fired wrong matches without user
                // visibility. Replaced with per-card "Fix metadata" button in detail panel.
                // User pastes the correct YouTube URL, gets exact match. Cleaner UX.

                // Scan for files that have gone missing since last launch (user-deleted, etc.)
                pywebview.api.check_library_files().then(newlyMissing => {
                    if (newlyMissing && newlyMissing.length > 0) {
                        // Mark them locally and re-render
                        const markMissing = (v) => { if (newlyMissing.includes(v.id)) v.missing = true; };
                        this.videosInLibrary.forEach(item => {
                            if (item.type === 'playlist') {
                                (item.videos || []).forEach(markMissing);
                            } else {
                                markMissing(item);
                            }
                        });
                        this.renderLibrary();
                    }
                });
            },

            mapElements() {
                const ids = [
                    'main-url-input', 'main-fetch-button', 'main-fetch-status',
                    'select-all-pill', 'sa-icon', 'sa-label', 'clear-selection-btn',
                    'video-list', 'download-button', 'cancel-button',
                    'folder-path', 'change-folder-button', 'open-folder-button',
                    'queue-view', 'playlist-detail-view', 'pd-layout',
                    'queue-empty', 'queue-empty-title', 'queue-empty-hint',
                    'rail-queue-badge', 'rail-library-badge', 'rail-queue-active-dot',
                    'rail-library', 'rail-queue', 'settings-btn', 'rail-collapse-btn',
                    'count-all', 'count-videos', 'count-playlists', 'count-channels', 'count-failed',
                    'filter-stats',
                    'library-view', 'library-grid', 'library-empty', 'library-subtitle', 'library-search',
                    'player-view'
                ];
                ids.forEach(id => {
                    this.elements[id.replace(/-./g, x => x[1].toUpperCase())] = document.getElementById(id);
                });
            },

            // Onboarding flow. Checks the _onboarded flag in settings; if not set,
            // shows the welcome modal. Modal asks for download folder + dismisses on
            // "Get started" which sets the flag so it never re-appears for this user.
            async _checkOnboarding() {
                let onboarded = false;
                try {
                    onboarded = await pywebview.api.get_setting('_onboarded');
                } catch (_) {}
                if (onboarded) return;
                this._showOnboarding();
            },

            async _showOnboarding() {
                const backdrop = document.getElementById('onb-backdrop');
                const folderDisplay = document.getElementById('onb-folder-display');
                const browseBtn = document.getElementById('onb-browse-btn');
                const startBtn = document.getElementById('onb-get-started');
                if (!backdrop || !folderDisplay || !browseBtn || !startBtn) return;

                // Pre-fill with the current folder (defaults to ~/Downloads/ProTube Saver)
                let chosenFolder = '';
                try {
                    chosenFolder = await pywebview.api.get_setting('download_folder');
                } catch (_) {}
                if (!chosenFolder) {
                    // Fall back to whatever the backend reports
                    try {
                        chosenFolder = await pywebview.api.get_download_folder();
                    } catch (_) {}
                }
                folderDisplay.textContent = chosenFolder || 'Default downloads folder';

                backdrop.removeAttribute('hidden');

                // Browse — open native folder picker
                const onBrowse = async () => {
                    try {
                        const picked = await pywebview.api.choose_folder();
                        if (picked) {
                            chosenFolder = picked;
                            folderDisplay.textContent = picked;
                        }
                    } catch (e) {
                        console.warn('[onboarding] folder picker failed', e);
                    }
                };

                // Get started — mark as onboarded, dismiss. The folder was already
                // persisted when the user picked it via choose_folder (which writes to
                // settings.json directly). If they didn't browse, the default stays.
                const onStart = async () => {
                    startBtn.disabled = true;
                    try {
                        await pywebview.api.set_setting('_onboarded', true);
                    } catch (e) {
                        console.warn('[onboarding] save failed', e);
                    }
                    backdrop.setAttribute('hidden', '');
                    // Update the path display in the queue view
                    if (this.elements.folderPath && chosenFolder) {
                        this.elements.folderPath.textContent = chosenFolder;
                    }
                    // Drop handlers so they don't stack if onboarding ever re-shows in this session
                    browseBtn.removeEventListener('click', onBrowse);
                    startBtn.removeEventListener('click', onStart);
                };

                browseBtn.addEventListener('click', onBrowse);
                startBtn.addEventListener('click', onStart);
            },

            // Background frame-extraction poller. The Python-side worker walks the
            // library and runs ffmpeg on entries that have a real file on disk but
            // no usable thumbnail. As each frame is extracted, settings.json is
            // mutated — but the rendered library is a JS-side snapshot, so without
            // refreshing nothing visibly changes until the next manual reload.
            //
            // This poller watches get_frame_extraction_status() every 2.5s, and
            // each time the pending count drops it pulls the latest library and
            // re-renders. Self-terminates when the worker is idle and pending=0
            // for two consecutive ticks (so a brief pause between entries doesn't
            // cause us to give up early).
            _startFrameExtractionPoll() {
                if (this._frameExtractionPolling) return;
                this._frameExtractionPolling = true;
                let zeroHits = 0;
                let everSawWork = false;  // ensure we refresh at least once after work was seen
                const refreshLibrary = async () => {
                    // Intentionally NOT busting the thumb cache here. Auto-frame extraction
                    // adds thumbnails to entries that didn't have one before, so their
                    // markers aren't in the cache yet — no bust needed. Busting the whole
                    // cache made every thumbnail flicker because each tile had to re-fetch
                    // from backend even though its data hadn't changed. The manual "Use
                    // video frame" handler still busts the affected entry's cache key
                    // because that flow REPLACES an existing thumbnail with the same marker.
                    try {
                        this.videosInLibrary = (await pywebview.api.load_library()) || [];
                        if (this.currentView === 'library') this.renderLibrary();
                        if (this._resolvePendingThumbnails) this._resolvePendingThumbnails();
                    } catch (_) { /* best-effort */ }
                };
                const tick = async () => {
                    try {
                        if (!pywebview?.api?.get_frame_extraction_status) {
                            this._frameExtractionPolling = false;
                            return;
                        }
                        const status = await pywebview.api.get_frame_extraction_status();
                        if (!status) { this._frameExtractionPolling = false; return; }

                        const workActive = status.running || status.pending > 0;
                        if (workActive) {
                            everSawWork = true;
                            // Refresh on EVERY tick while the worker is active. The old
                            // logic only refreshed on count CHANGES, which missed the case
                            // where the worker finished faster than our 1500ms first-tick
                            // delay — pending dropped to 0 between ticks 0 and 1, so the
                            // poller saw pending=0 throughout and never refreshed. Result:
                            // thumbnails extracted to disk but UI didn't show them until
                            // the next app launch. Refresh-every-tick fixes this; cost is
                            // a couple extra load_library calls per import, which is cheap.
                            await refreshLibrary();
                        } else if (everSawWork && zeroHits === 0) {
                            // Final refresh once the worker reports idle, in case the last
                            // extraction landed in the gap between status calls.
                            await refreshLibrary();
                        }

                        if (!status.running && status.pending === 0) {
                            zeroHits++;
                            if (zeroHits >= 2) {
                                this._frameExtractionPolling = false;
                                return;
                            }
                        } else {
                            zeroHits = 0;
                        }
                    } catch (_) {
                        zeroHits++;
                        if (zeroHits >= 4) {
                            this._frameExtractionPolling = false;
                            return;
                        }
                    }
                    setTimeout(tick, 2000);
                };
                // First tick fires fast — catches workers that finish almost immediately
                // (e.g. cached frames, single-video import). Subsequent ticks at 2s.
                setTimeout(tick, 400);
            },

            // Drag-and-drop wiring. Drops are accepted on document.body — that's the
            // whole window. We show a full-screen overlay during drag for visual feedback.
            _setupDragAndDrop() {
                const overlay = document.getElementById('dnd-overlay');
                const isYoutubeUrl = (url) => {
                    if (!url || typeof url !== 'string') return false;
                    return /^https?:\/\/(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)\//i.test(url.trim());
                };
                const extractUrlFromEvent = (e) => {
                    const dt = e.dataTransfer;
                    if (!dt) return null;
                    // Canonical URL drop type — set by browsers when dragging a hyperlink
                    let url = dt.getData('text/uri-list');
                    if (url) {
                        // text/uri-list can be multi-line with comments; first non-comment line is the URL
                        url = url.split('\n').filter(l => l && !l.startsWith('#'))[0]?.trim();
                    }
                    // Fallback: most browsers also stuff the URL into text/plain
                    if (!url) url = (dt.getData('text/plain') || '').trim();
                    return url;
                };

                let dragDepth = 0;  // counts nested dragenter/leave events to handle child elements

                document.addEventListener('dragenter', (e) => {
                    // Only show overlay if the drag carries a URL/text payload (not files etc)
                    const types = e.dataTransfer?.types || [];
                    const hasText = Array.from(types).some(t => t === 'text/uri-list' || t === 'text/plain' || t === 'text/x-moz-url');
                    if (!hasText) return;
                    dragDepth++;
                    if (overlay) overlay.classList.add('visible');
                });

                document.addEventListener('dragleave', (e) => {
                    dragDepth--;
                    if (dragDepth <= 0) {
                        dragDepth = 0;
                        if (overlay) overlay.classList.remove('visible');
                    }
                });

                document.addEventListener('dragover', (e) => {
                    // CRITICAL: must preventDefault here or drop event never fires
                    e.preventDefault();
                    if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
                });

                document.addEventListener('drop', (e) => {
                    dragDepth = 0;
                    if (overlay) overlay.classList.remove('visible');

                    // Don't hijack drops on the URL input itself or any text input —
                    // let normal paste-on-drop work for those
                    const tag = (e.target?.tagName || '').toLowerCase();
                    if (tag === 'input' || tag === 'textarea') return;

                    e.preventDefault();
                    const url = extractUrlFromEvent(e);
                    if (!url) return;

                    if (!isYoutubeUrl(url)) {
                        // Not a YouTube URL — silently ignore. They were probably dragging
                        // something else (text from a doc, an image, etc.)
                        return;
                    }

                    // Ensure we're on the queue view so user can see what's happening
                    if (this.currentView !== 'queue') {
                        this.switchView('queue');
                    }

                    // Populate URL input + trigger fetch
                    const input = this.elements.mainUrlInput;
                    if (input) {
                        input.value = url;
                        // Chrome quirk: after dropping, text stays selected invisibly.
                        // Blur + refocus + place caret at end fixes it.
                        input.blur();
                        setTimeout(() => {
                            input.focus();
                            input.setSelectionRange(url.length, url.length);
                        }, 10);
                    }
                    this.fetch();
                });
            },

            switchView(view) {
                const wasInPlayer = this.currentView === 'player';
                const goingToPlayer = view === 'player';

                // Always exit selection mode when changing views — the action bar
                // shouldn't persist into the queue, player, or anywhere else.
                if (typeof Selection !== 'undefined' && Selection.isActive()) {
                    Selection.exit();
                }
                if (typeof MusicSelection !== 'undefined' && MusicSelection.isActive()) {
                    MusicSelection.exit();
                }

                // If we're leaving player view, stop playback so the file is released
                // and we don't keep streaming bytes from the server in the background.
                if (wasInPlayer && !goingToPlayer) {
                    if (window.player && typeof window.player.stop === 'function') {
                        window.player.stop();
                    }
                    // Restore the user's normal rail preference + clear the in-player override
                    this._railManualOverrideInPlayer = null;
                    const cockpit = document.querySelector('.cockpit');
                    if (cockpit) {
                        cockpit.classList.toggle('rail-collapsed', !!this._railUserPreference);
                    }
                    // Reload library + re-render so watch-progress bars on cards reflect
                    // the latest position we just saved when stop() fired.
                    pywebview.api.load_library().then(lib => {
                        this.videosInLibrary = lib || [];
                        this.renderLibrary();
                    }).catch(() => {});
                }

                // Leaving the music player view? Exit immersive fullscreen so
                // the body class doesn't persist into Library/Queue/etc — that
                // was forcing `#music-player-view { display: block !important }`
                // on top of whatever view the user navigated to, producing
                // overlapping content (user 2026-05-17 screenshot).
                if (this.currentView === 'music-player' && view !== 'music-player') {
                    if (typeof _musicFsActive !== 'undefined' && _musicFsActive) {
                        try { _musicToggleFs(); } catch (_) {
                            // Defensive: at minimum, scrub the class so the
                            // overlap can't happen even if the toggle errors.
                            document.body.classList.remove('music-player-fullscreen');
                        }
                    }
                }

                this.currentView = view;

                // If we're entering player, auto-collapse the rail so video gets the canvas.
                // Honor an in-player manual override if the user already expanded mid-playback.
                if (goingToPlayer) {
                    const cockpit = document.querySelector('.cockpit');
                    if (cockpit) {
                        const wantCollapsed = this._railManualOverrideInPlayer === true ? false : true;
                        cockpit.classList.toggle('rail-collapsed', wantCollapsed);
                    }
                }

                // Clear any inline display styles that might linger from the playlist-detail
                // open/close flow.
                this.elements.queueView.style.display = '';
                this.elements.libraryView.style.display = '';
                if (this.elements.playerView) this.elements.playerView.style.display = '';
                // Also clear inline hides on the other panes (search/music) — a
                // channel preview opened from search inline-hid search-view; without
                // this it would stay hidden when navigating back to it via the rail.
                document.querySelectorAll('.cockpit-main > .view-pane').forEach(p => { p.style.display = ''; });
                // ALWAYS dismiss the playlist-detail overlay on a rail switch —
                // even when the target IS 'queue'. The detail view is only ever
                // opened via openPlaylistDetail; clicking the Queue icon while
                // inside a channel must return to the queue LIST, not leave the
                // channel grid bleeding on top of it. (Previously this was gated
                // `view !== 'queue'`, so Queue→Queue left the overlay up.)
                if (this.elements.playlistDetailView) {
                    this.elements.playlistDetailView.classList.remove('visible');
                    if (this.currentPlaylistId) {
                        this.currentPlaylistId = null;
                        if (this._teardownChannelStickyObserver) this._teardownChannelStickyObserver();
                        const _sticky = document.getElementById('pd-sticky-header');
                        if (_sticky) { _sticky.classList.remove('is-visible'); _sticky.setAttribute('hidden', ''); }
                    }
                }
                // Toggle view panes
                this.elements.libraryView.classList.toggle('active', view === 'library');
                this.elements.queueView.classList.toggle('active', view === 'queue');
                if (this.elements.playerView) {
                    this.elements.playerView.classList.toggle('active', view === 'player');
                }
                const searchView = document.getElementById('search-view');
                if (searchView) searchView.classList.toggle('active', view === 'search');
                const musicView = document.getElementById('music-view');
                if (musicView) musicView.classList.toggle('active', view === 'music');
                const musicPlayerView = document.getElementById('music-player-view');
                if (musicPlayerView) musicPlayerView.classList.toggle('active', view === 'music-player');
                const musicAlbumDetailView = document.getElementById('music-album-detail-view');
                if (musicAlbumDetailView) musicAlbumDetailView.classList.toggle('active', view === 'music-album-detail');
                document.body.classList.toggle('music-player-open', view === 'music-player');
                // Toggle rail items
                this.elements.railLibrary.classList.toggle('active', view === 'library');
                this.elements.railQueue.classList.toggle('active', view === 'queue');
                const railSearchBtn = document.getElementById('rail-search');
                if (railSearchBtn) railSearchBtn.classList.toggle('active', view === 'search');
                const railMusicBtn = document.getElementById('rail-music');
                if (railMusicBtn) railMusicBtn.classList.toggle('active', view === 'music');
                // Focus the URL bar when landing on queue (paste-ready). We deliberately
                // do NOT auto-focus on library — stealing focus into the search bar every
                // time the user lands here is annoying and triggers the keyboard for users
                // who just want to browse their grid.
                if (view === 'queue') {
                    setTimeout(() => this.elements.mainUrlInput?.focus(), 50);
                }
                if (view === 'search') {
                    setTimeout(() => document.getElementById('search-input')?.focus(), 50);
                    if (typeof initSearchView === 'function') initSearchView();
                    // Refresh the For-You landing every time the user lands
                    // on Search (so recent_searches + recent_library reflect
                    // any new activity). Only when there's no active query.
                    const _siv = document.getElementById('search-input');
                    if (typeof _renderVideoForYou === 'function' && (!_siv || !_siv.value)) {
                        _renderVideoForYou();
                    }
                } else {
                    // Leaving search → make sure the autocomplete dropdown isn't left
                    // dangling, otherwise it'd be the first thing the user sees on return.
                    const sb = document.getElementById('search-suggestions');
                    if (sb) sb.setAttribute('hidden', '');
                }
                if (view === 'music') {
                    if (typeof initMusicView === 'function') initMusicView();
                }
            },

            addEventListeners() {
                this.elements.mainFetchButton.onclick = () => this.fetch();
                this.elements.downloadButton.onclick = () => this.startDownload();
                this.elements.cancelButton.onclick = () => this.cancelAllDownloads();
                this.elements.changeFolderButton.onclick = async () => {
                    const p = await pywebview.api.choose_folder();
                    if (p) this.elements.folderPath.textContent = p;
                };

                this.elements.openFolderButton.onclick = () => {
                    pywebview.api.open_folder();
                };

                // URL input: Enter key submits
                this.elements.mainUrlInput.addEventListener('keypress', (e) => {
                    if (e.key === 'Enter') this.fetch();
                });

                // Drag-and-drop URL anywhere in the app. User drags a YouTube link from
                // their browser's address bar, drops it ANYWHERE in the ProTube window,
                // and we auto-populate the URL input + trigger fetch.
                //
                // Critical implementation notes from research:
                // - Must preventDefault on dragover, otherwise drop event never fires
                // - getData('text/uri-list') is the canonical URL transport, but most
                //   browsers also populate text/plain with the URL string — fall back to it
                // - Don't hijack drops on existing form inputs (let them paste normally)
                // - YouTube URL detection: youtube.com/watch?v=, youtu.be/, /shorts/, etc
                this._setupDragAndDrop();

                // Filter chips
                document.querySelectorAll('.filter-chip').forEach(chip => {
                    chip.addEventListener('click', () => {
                        const filter = chip.dataset.filter;
                        this.setFilter(filter);
                    });
                });

                // Rail navigation — Library / Queue / Search
                this.elements.railLibrary.addEventListener('click', () => this.switchView('library'));
                this.elements.railQueue.addEventListener('click', () => this.switchView('queue'));
                const railSearchBtn = document.getElementById('rail-search');
                if (railSearchBtn) railSearchBtn.addEventListener('click', () => this.switchView('search'));
                const railMusicBtn = document.getElementById('rail-music');
                if (railMusicBtn) railMusicBtn.addEventListener('click', () => this.switchView('music'));

                // Rail collapse toggle. The behavior we want:
                //   - User can manually collapse/expand any time (persisted to settings)
                //   - On entering player view, auto-collapse — UNLESS user explicitly expanded
                //     during this same player session (we honor their override)
                //   - On leaving player view, restore the user's manual preference
                // Rail collapse — both the toggle button (when expanded) AND clicking
                // the logo (when collapsed) flip the rail state. This matches Notion/Linear:
                // when collapsed, the logo is the affordance to expand.
                const toggleRail = () => {
                    const cockpit = document.querySelector('.cockpit');
                    const isCollapsed = cockpit.classList.toggle('rail-collapsed');
                    if (this.currentView === 'player') {
                        this._railManualOverrideInPlayer = !isCollapsed;
                    } else {
                        this._railUserPreference = isCollapsed;
                        try { pywebview.api.set_setting('rail_collapsed', isCollapsed); } catch(_) {}
                    }
                };
                if (this.elements.railCollapseBtn) {
                    this.elements.railCollapseBtn.addEventListener('click', toggleRail);
                }
                const logoMark = document.getElementById('rail-logo-mark');
                if (logoMark) {
                    logoMark.addEventListener('click', () => {
                        // Only toggle when in collapsed state — when expanded the logo
                        // is decorative and clicking it shouldn't do anything weird.
                        const cockpit = document.querySelector('.cockpit');
                        if (cockpit?.classList.contains('rail-collapsed')) {
                            toggleRail();
                        }
                    });
                }

                // Library search — live filter + suggestions dropdown + clear button
                const searchInput = this.elements.librarySearch;
                const searchClear = document.getElementById('library-search-clear');
                const suggestions = document.getElementById('library-search-suggestions');

                const updateSearchUi = () => {
                    const q = this.librarySearchQuery;
                    // Toggle clear button visibility
                    if (searchClear) searchClear.classList.toggle('hidden', !q);
                    // Build suggestion list — top matching titles, distinct, max 6
                    if (!suggestions) return;
                    if (!q) {
                        suggestions.classList.add('hidden');
                        suggestions.innerHTML = '';
                        return;
                    }
                    const matches = [];
                    const seen = new Set();
                    const pushIfNew = (v) => {
                        const key = (v.title || '').toLowerCase();
                        if (!key || seen.has(key)) return;
                        if (key.includes(q) || (v.uploader || '').toLowerCase().includes(q)) {
                            seen.add(key);
                            matches.push(v);
                        }
                    };
                    for (const item of this.videosInLibrary) {
                        if (matches.length >= 6) break;
                        if (item.type === 'playlist') {
                            pushIfNew(item);
                            for (const c of (item.videos || [])) {
                                if (matches.length >= 6) break;
                                pushIfNew(c);
                            }
                        } else {
                            pushIfNew(item);
                        }
                    }
                    if (matches.length === 0) {
                        suggestions.classList.add('hidden');
                        suggestions.innerHTML = '';
                        return;
                    }
                    suggestions.innerHTML = matches.map(m => {
                        const t = this.escapeHtml(m.title || '');
                        const u = this.escapeHtml(m.uploader || '');
                        const icon = m.type === 'playlist'
                            ? '<svg class="library-search-suggestion-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg>'
                            : '<svg class="library-search-suggestion-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2"/></svg>';
                        return `<div class="library-search-suggestion" data-title="${t}">${icon}<span class="library-search-suggestion-title">${t}</span></div>`;
                    }).join('');
                    suggestions.classList.remove('hidden');
                };

                searchInput.addEventListener('input', (e) => {
                    this.librarySearchQuery = e.target.value.trim().toLowerCase();
                    this.renderLibrary();
                    updateSearchUi();
                });

                // Clicking a suggestion commits that title as the search term
                if (suggestions) {
                    suggestions.addEventListener('click', (e) => {
                        const row = e.target.closest('.library-search-suggestion');
                        if (!row) return;
                        const title = row.getAttribute('data-title') || '';
                        searchInput.value = title;
                        this.librarySearchQuery = title.toLowerCase();
                        this.renderLibrary();
                        suggestions.classList.add('hidden');
                    });
                }

                // Escape clears the input
                searchInput.addEventListener('keydown', (e) => {
                    if (e.key === 'Escape') {
                        searchInput.value = '';
                        this.librarySearchQuery = '';
                        this.renderLibrary();
                        updateSearchUi();
                        searchInput.blur();
                    }
                });

                // Clicking outside the search area hides the suggestions
                document.addEventListener('click', (e) => {
                    const container = searchInput?.closest('.library-search');
                    if (container && !container.contains(e.target) && suggestions) {
                        suggestions.classList.add('hidden');
                    }
                });

                // Clear-X button
                if (searchClear) {
                    searchClear.addEventListener('click', () => {
                        searchInput.value = '';
                        this.librarySearchQuery = '';
                        this.renderLibrary();
                        updateSearchUi();
                        searchInput.focus();
                    });
                }

                // Import-from-folder handlers. Both the empty-state button and the header
                // button do the same thing — scan the folder, preview, then commit.
                const importEmptyBtn = document.getElementById('library-import-btn');
                const importHeaderBtn = document.getElementById('library-import-header-btn');
                const doImport = () => this.importFromFolder();
                if (importEmptyBtn) importEmptyBtn.addEventListener('click', doImport);
                if (importHeaderBtn) importHeaderBtn.addEventListener('click', doImport);

                // "Show hidden" toggle — flips window._showHiddenLibrary, re-renders,
                // and persists the choice via set_setting so it survives restarts.
                // Hydrated from settings on app boot (see init()).
                const showHiddenBtn = document.getElementById('library-show-hidden-btn');
                if (showHiddenBtn) {
                    if (window._showHiddenLibrary) showHiddenBtn.classList.add('active');
                    showHiddenBtn.addEventListener('click', () => {
                        const next = !window._showHiddenLibrary;
                        window._showHiddenLibrary = next;
                        showHiddenBtn.classList.toggle('active', next);
                        showHiddenBtn.setAttribute('data-tip', next ? 'Hide hidden' : 'Show hidden');
                        try { pywebview.api.set_setting('show_hidden_library', next); } catch (_) {}
                        this.renderLibrary();
                    });
                }

                // Detail panel close interactions
                const detailClose = document.getElementById('detail-panel-close');
                const detailBackdrop = document.getElementById('detail-panel-backdrop');
                if (detailClose) detailClose.addEventListener('click', hideDetailPanel);
                if (detailBackdrop) detailBackdrop.addEventListener('click', hideDetailPanel);
                document.addEventListener('keydown', (e) => {
                    if (e.key === 'Escape') {
                        const panel = document.getElementById('detail-panel');
                        if (panel && panel.classList.contains('visible')) hideDetailPanel();
                    }
                });
            },

            // Two-step import flow: preview the folder first to show counts, then user
            // confirms via toast action. We never touch the library without consent.
            async importFromFolder() {
                try {
                    showToast('Scanning download folder…', null, null);
                    const scan = await pywebview.api.scan_folder_full();
                    if (scan.error) {
                        showToast(`Scan failed: ${scan.error}`, null, null);
                        return;
                    }
                    if ((scan.total_videos || 0) === 0) {
                        showToast('No video files found in the download folder', null, null);
                        return;
                    }
                    this.openImportPicker(scan);
                } catch (err) {
                    console.error('Import error:', err);
                    showToast('Import failed — see console', null, null);
                }
            },

            // Build the picker modal from a scan_folder_full payload, wire interactions,
            // and run the selected import on confirm. Reuses the existing #import-progress
            // card while import_from_folder runs in the background.
            openImportPicker(scan) {
                const backdrop = document.getElementById('imp-backdrop');
                const folderEl = document.getElementById('imp-folder');
                const listEl = document.getElementById('imp-list');
                const searchInput = document.getElementById('imp-search-input');
                const masterBtn = document.getElementById('imp-master');
                const masterLabel = document.getElementById('imp-master-label');
                const selectedCountEl = document.getElementById('imp-selected-count');
                const skipInfoEl = document.getElementById('imp-skip-info');
                const confirmBtn = document.getElementById('imp-confirm');
                const cancelBtn = document.getElementById('imp-cancel');
                const closeBtn = document.getElementById('imp-close');

                folderEl.textContent = scan.folder || '';
                searchInput.value = '';
                listEl.innerHTML = '';

                // Build sections from the scan payload
                const renderRow = (item) => {
                    const safeName = this.escapeHtml(item.name || '');
                    const sizeStr = formatBytes(item.size_bytes || 0);
                    const badges = [];
                    if (item.in_library) {
                        badges.push('<span class="imp-badge in-lib">in library</span>');
                    } else if (item.in_archive) {
                        badges.push('<span class="imp-badge archived">metadata saved</span>');
                    }
                    const disabled = item.in_library ? ' disabled data-disabled' : '';
                    return `<div class="imp-row"${disabled} data-path="${this.escapeHtml(item.path || '')}" data-name="${safeName.toLowerCase()}" title="${this.escapeHtml(item.path || '')}">
                        <div class="imp-check"></div>
                        <div class="imp-name">${safeName}</div>
                        ${badges.join('')}
                        <div class="imp-size">${sizeStr}</div>
                    </div>`;
                };

                const renderSection = (label, isPlaylist, rows) => {
                    if (!rows || rows.length === 0) return '';
                    const nameHtml = isPlaylist
                        ? `<div class="imp-secname"><span class="imp-secname-name">${this.escapeHtml(label)}</span></div>`
                        : `<div class="imp-secname">${this.escapeHtml(label)}</div>`;
                    return `<div class="imp-section" data-group="${isPlaylist ? 'playlist' : 'standalone'}">
                        <div class="imp-sechead" data-toggle-section>
                            <svg class="imp-seccaret" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round">
                                <path d="M6 9l6 6 6-6"/>
                            </svg>
                            <div class="imp-check" data-section-check></div>
                            ${nameHtml}
                            <div class="imp-seccount" data-seccount>0 / ${rows.length}</div>
                        </div>
                        <div class="imp-secrows">${rows.map(renderRow).join('')}</div>
                    </div>`;
                };

                const parts = [];
                parts.push(renderSection('Standalone videos', false, scan.standalone || []));
                for (const pl of (scan.playlists || [])) {
                    parts.push(renderSection(pl.name, true, pl.videos || []));
                }
                parts.push('<div class="imp-empty" id="imp-empty" hidden>No matches<div class="imp-empty-hint">Try a different search term</div></div>');
                listEl.innerHTML = parts.join('');

                const refreshState = () => {
                    const sections = listEl.querySelectorAll('.imp-section[data-group]');
                    let totalSelected = 0;
                    let totalDisabled = 0;
                    let visibleSelectable = 0;
                    let visibleSelected = 0;
                    sections.forEach(sec => {
                        const rows = sec.querySelectorAll('.imp-row');
                        let secSelectable = 0;
                        let secSelected = 0;
                        let anyVisible = false;
                        rows.forEach(r => {
                            const isDisabled = r.hasAttribute('data-disabled');
                            const isVisible = r.style.display !== 'none';
                            if (isDisabled) totalDisabled++;
                            if (!isDisabled) {
                                secSelectable++;
                                if (r.classList.contains('selected')) {
                                    secSelected++;
                                    totalSelected++;
                                }
                            }
                            if (isVisible) {
                                anyVisible = true;
                                if (!isDisabled) {
                                    visibleSelectable++;
                                    if (r.classList.contains('selected')) visibleSelected++;
                                }
                            }
                        });
                        const secCount = sec.querySelector('[data-seccount]');
                        if (secCount) secCount.textContent = `${secSelected} / ${secSelectable}`;
                        const secCheck = sec.querySelector('[data-section-check]');
                        if (secCheck) {
                            secCheck.classList.remove('all', 'indeterminate');
                            if (secSelectable > 0 && secSelected === secSelectable) secCheck.classList.add('all');
                            else if (secSelected > 0) secCheck.classList.add('indeterminate');
                        }
                        sec.toggleAttribute('hidden', !anyVisible);
                    });
                    const emptyEl = document.getElementById('imp-empty');
                    const anyVisibleSection = Array.from(sections).some(s => !s.hasAttribute('hidden'));
                    if (emptyEl) emptyEl.toggleAttribute('hidden', anyVisibleSection || sections.length === 0);

                    if (visibleSelectable > 0 && visibleSelected === visibleSelectable) {
                        masterLabel.textContent = 'Deselect all';
                        masterBtn.classList.add('active');
                    } else {
                        masterLabel.textContent = 'Select all';
                        masterBtn.classList.remove('active');
                    }
                    selectedCountEl.textContent = totalSelected;
                    skipInfoEl.textContent = totalDisabled > 0 ? ` · ${totalDisabled} already in library` : '';
                    confirmBtn.disabled = totalSelected === 0;
                    confirmBtn.textContent = totalSelected > 0 ? `Import ${totalSelected}` : 'Import';
                };

                // Click handler — single delegate on the list. Distinguishes section
                // header (caret/name = collapse, checkbox = bulk toggle) from a row click.
                const onListClick = (e) => {
                    const sechead = e.target.closest('[data-toggle-section]');
                    if (sechead) {
                        const section = sechead.parentElement;
                        if (e.target.closest('[data-section-check]')) {
                            const rows = section.querySelectorAll('.imp-row');
                            const visibleSelectable = Array.from(rows).filter(r =>
                                !r.hasAttribute('data-disabled') && r.style.display !== 'none'
                            );
                            const allSelected = visibleSelectable.length > 0 && visibleSelectable.every(r => r.classList.contains('selected'));
                            visibleSelectable.forEach(r => r.classList.toggle('selected', !allSelected));
                        } else {
                            section.classList.toggle('collapsed');
                        }
                        refreshState();
                        return;
                    }
                    const row = e.target.closest('.imp-row');
                    if (row && !row.hasAttribute('data-disabled')) {
                        row.classList.toggle('selected');
                        refreshState();
                    }
                };
                const onMasterClick = () => {
                    const visibleRows = listEl.querySelectorAll('.imp-row:not([data-disabled])');
                    const eligible = Array.from(visibleRows).filter(r => r.style.display !== 'none');
                    const allSelected = eligible.length > 0 && eligible.every(r => r.classList.contains('selected'));
                    eligible.forEach(r => r.classList.toggle('selected', !allSelected));
                    refreshState();
                };
                const onSearchInput = () => {
                    const q = searchInput.value.trim().toLowerCase();
                    listEl.querySelectorAll('.imp-row').forEach(r => {
                        const name = r.getAttribute('data-name') || '';
                        const match = !q || name.includes(q);
                        r.style.display = match ? '' : 'none';
                    });
                    refreshState();
                };
                const onKeydown = (e) => {
                    if (e.key === 'Escape') {
                        e.preventDefault();
                        close();
                    }
                };

                let onConfirm;
                const close = () => {
                    backdrop.setAttribute('hidden', '');
                    listEl.removeEventListener('click', onListClick);
                    masterBtn.removeEventListener('click', onMasterClick);
                    searchInput.removeEventListener('input', onSearchInput);
                    document.removeEventListener('keydown', onKeydown);
                    cancelBtn.removeEventListener('click', close);
                    closeBtn.removeEventListener('click', close);
                    confirmBtn.removeEventListener('click', onConfirm);
                };

                onConfirm = async () => {
                    const selectedPaths = Array.from(listEl.querySelectorAll('.imp-row.selected'))
                        .map(r => r.getAttribute('data-path'))
                        .filter(Boolean);
                    if (selectedPaths.length === 0) return;
                    close();
                    await this._runImport(scan.folder, selectedPaths);
                };

                listEl.addEventListener('click', onListClick);
                masterBtn.addEventListener('click', onMasterClick);
                searchInput.addEventListener('input', onSearchInput);
                document.addEventListener('keydown', onKeydown);
                cancelBtn.addEventListener('click', close);
                closeBtn.addEventListener('click', close);
                confirmBtn.addEventListener('click', onConfirm);

                backdrop.removeAttribute('hidden');
                refreshState();
                setTimeout(() => searchInput.focus(), 50);
            },

            // Run the actual import for a list of selected filepaths. Reuses the
            // existing #import-progress card and polling pattern.
            // Update check — fired from init(). Backend handles 24h caching;
            // we just show the pill if has_update is true. Per-session
            // dismissal lives on window._updatePillDismissed so a click on
            // the X persists for the rest of this run.
            async _checkForAppUpdates(force) {
                try {
                    const info = await pywebview.api.check_for_updates(!!force);
                    if (info && info.has_update) {
                        // Stash so the Settings drawer can read this without
                        // re-fetching when it opens.
                        window._updateInfo = info;
                        if (!window._updatePillDismissed) showUpdatePill(info);
                    } else {
                        window._updateInfo = info || null;
                    }
                    return info;
                } catch (_) {
                    return null;
                }
            },

            async _runImport(folder, selectedPaths) {
                const total = selectedPaths.length;
                const progEl = document.getElementById('import-progress');
                const textEl = progEl?.querySelector('.ipro-text');
                const countEl = progEl?.querySelector('.ipro-count');
                const fillEl = progEl?.querySelector('.ipro-bar-fill');
                if (progEl) {
                    if (textEl) textEl.textContent = 'Importing…';
                    if (countEl) countEl.textContent = `0 / ${total}`;
                    if (fillEl) fillEl.style.width = '0%';
                    progEl.removeAttribute('hidden');
                }
                const pollId = setInterval(async () => {
                    try {
                        const p = await pywebview.api.get_import_progress();
                        if (!p || !progEl) return;
                        const cur = p.current || 0;
                        const tot = p.total || total || 0;
                        if (countEl) countEl.textContent = `${cur} / ${tot}`;
                        if (fillEl && tot > 0) fillEl.style.width = `${Math.min(100, (cur / tot) * 100)}%`;
                    } catch (_) { /* poll best-effort */ }
                }, 200);

                try {
                    const result = await pywebview.api.import_from_folder(folder, true, selectedPaths);
                    clearInterval(pollId);
                    if (progEl) progEl.setAttribute('hidden', '');
                    if (result.error) {
                        showToast(`Import failed: ${result.error}`, null, null);
                        return;
                    }
                    this.videosInLibrary = (await pywebview.api.load_library()) || [];
                    this.renderLibrary();
                    // Kick the frame-extraction poller so users see the just-imported
                    // videos' thumbnails populate as ffmpeg generates them.
                    if (this._startFrameExtractionPoll) this._startFrameExtractionPoll();
                    const done = result.imported_videos || 0;
                    const skip = result.skipped || 0;
                    const msgParts = [`${done} imported`];
                    if (skip > 0) msgParts.push(`${skip} already in library`);
                    showToast(msgParts.join(' · '), 'OK', () => {});
                } catch (err) {
                    clearInterval(pollId);
                    if (progEl) progEl.setAttribute('hidden', '');
                    console.error('Import error:', err);
                    showToast('Import failed — see console', null, null);
                }
            },

            // Flatten library for search/count purposes: playlist children contribute individually
            getLibraryFlatCount() {
                let total = 0;
                this.videosInLibrary.forEach(item => {
                    if (item.type === 'playlist') {
                        total += (item.videos || []).length;
                    } else {
                        total += 1;
                    }
                });
                return total;
            },

            // Aggregate bytes of a library item (single video or sum of playlist children)
            libraryItemBytes(item) {
                if (item.type === 'playlist') {
                    return (item.videos || []).reduce((sum, c) => {
                        const q = c.selectedQuality;
                        return sum + (c.sizeMap?.[q] || 0);
                    }, 0);
                }
                const q = item.selectedQuality;
                return item.sizeMap?.[q] || 0;
            },

            libraryMatchesSearch(item, query) {
                if (!query) return true;
                const q = query.toLowerCase();
                if (item.type === 'playlist') {
                    if ((item.title || '').toLowerCase().includes(q)) return true;
                    if ((item.uploader || '').toLowerCase().includes(q)) return true;
                    // If any child matches, we include the whole playlist
                    return (item.videos || []).some(c =>
                        (c.title || '').toLowerCase().includes(q) ||
                        (c.uploader || '').toLowerCase().includes(q)
                    );
                }
                return (item.title || '').toLowerCase().includes(q) ||
                       (item.uploader || '').toLowerCase().includes(q);
            },

            renderLibrary() {
                const grid = this.elements.libraryGrid;
                const empty = this.elements.libraryEmpty;
                if (!grid) return;

                const query = this.librarySearchQuery;
                // Hide-aware filter: by default, items with hidden=true are not
                // rendered. The "Show hidden" toggle in the library header flips
                // window._showHiddenLibrary; when true, hidden cards render with
                // dim + "Hidden" badge so the user can tell which ones to unhide.
                const showHidden = !!window._showHiddenLibrary;
                const filtered = this.videosInLibrary
                    .filter(item => showHidden || !item.hidden)
                    .filter(item => this.libraryMatchesSearch(item, query));

                // Pinned items sort to the top, most-recently-pinned first.
                // Array.prototype.sort is stable in modern engines (V8 since
                // ~2018), so returning 0 for equal items preserves the user's
                // original library order between non-pinned entries.
                filtered.sort((a, b) => {
                    const aP = a.pinned ? 1 : 0;
                    const bP = b.pinned ? 1 : 0;
                    if (aP !== bP) return bP - aP;
                    if (aP === 1) return (b.pinned_at || 0) - (a.pinned_at || 0);
                    return 0;
                });

                // Pinned items sort to the top, most-recently-pinned first
                // (`pinned_at` is a unix timestamp set on pin). Items not pinned
                // keep their existing relative order — this is a stable sort
                // because we only re-rank on the boolean and pin-time, leaving
                // unpinned ties where they were.
                filtered.sort((a, b) => {
                    if (!!a.pinned === !!b.pinned) {
                        if (a.pinned) return (b.pinned_at || 0) - (a.pinned_at || 0);
                        return 0;
                    }
                    return a.pinned ? -1 : 1;
                });

                // Compute subtitle text (total count + size)
                const totalCount = this.getLibraryFlatCount();
                const totalBytes = this.videosInLibrary.reduce((s, item) => s + this.libraryItemBytes(item), 0);
                const parts = [`${totalCount} video${totalCount === 1 ? '' : 's'}`];
                if (totalBytes > 0) parts.push(formatBytes(totalBytes));
                if (this.elements.librarySubtitle) this.elements.librarySubtitle.textContent = parts.join(' · ');
                if (this.elements.railLibraryBadge) {
                    this.elements.railLibraryBadge.textContent = totalCount;
                }

                // Empty state handling. We collapse the grid (display:none)
                // when the empty state shows so .library-empty's `flex: 1`
                // takes the full available space inside the .view-pane and
                // centers properly. Without this the grid still claimed flex
                // space and the empty floated awkwardly relative to it.
                if (this.videosInLibrary.length === 0) {
                    // Truly empty
                    empty.classList.remove('hidden');
                    grid.innerHTML = '';
                    grid.style.display = 'none';
                    // Swap empty state copy
                    empty.querySelector('.library-empty-title').textContent = 'Your library is empty';
                    empty.querySelector('.library-empty-hint').textContent =
                        'Downloads will appear here once they complete. Head to Queue to add a URL.';
                    return;
                }
                if (filtered.length === 0) {
                    // Search with no matches
                    empty.classList.remove('hidden');
                    grid.innerHTML = '';
                    grid.style.display = 'none';
                    empty.querySelector('.library-empty-title').textContent = 'No matches';
                    empty.querySelector('.library-empty-hint').textContent = `Nothing in your library matches "${query}".`;
                    return;
                }

                empty.classList.add('hidden');
                grid.style.display = '';
                grid.innerHTML = filtered.map(item => this.createLibraryCardHTML(item)).join('');
                // Replace 'pt:thumb:' markers with real data URLs by fetching each via backend
                this._resolvePendingThumbnails();
                // Re-apply selection state if user is in selection mode (cards just got
                // rebuilt and lost their .is-selectable / .is-selected classes).
                if (typeof Selection !== 'undefined') {
                    Selection.refreshAfterRender();
                }
            },

            // In-memory cache: marker -> data URL. Avoids redundant backend calls
            // as the grid re-renders during search/filter.
            _thumbCache: {},
            _thumbInflight: new Set(),

            async _resolvePendingThumbnails() {
                if (typeof pywebview === 'undefined' || !pywebview.api) return;
                const imgs = document.querySelectorAll('img[data-thumb-marker]');
                for (const img of imgs) {
                    const marker = img.getAttribute('data-thumb-marker');
                    if (!marker || !marker.startsWith('pt:thumb:')) continue;

                    // Already have it cached? Use it.
                    if (this._thumbCache[marker]) {
                        img.src = this._thumbCache[marker];
                        img.removeAttribute('data-thumb-marker');
                        continue;
                    }

                    // Already in-flight? Don't fire another request.
                    if (this._thumbInflight.has(marker)) continue;

                    this._thumbInflight.add(marker);
                    // Fire and forget — each resolves independently
                    pywebview.api.get_thumbnail_data(marker).then(dataUrl => {
                        this._thumbInflight.delete(marker);
                        if (dataUrl) {
                            this._thumbCache[marker] = dataUrl;
                            // Update every img currently pointing at this marker
                            // (there may be duplicates across playlist children etc.)
                            document.querySelectorAll(`img[data-thumb-marker="${marker}"]`).forEach(el => {
                                el.src = dataUrl;
                                el.removeAttribute('data-thumb-marker');
                            });
                        }
                    }).catch(() => {
                        this._thumbInflight.delete(marker);
                    });
                }

                // Also process any imgs whose thumbnail is a REMOTE URL — kick off
                // background caching so the next offline launch shows them. We do
                // this lazily (only when they appear on screen) to avoid blasting
                // the backend with hundreds of requests on first library load.
                const remoteImgs = document.querySelectorAll('img[data-remote-thumb][data-thumb-item-id]');
                for (const img of remoteImgs) {
                    const url = img.getAttribute('data-remote-thumb');
                    const itemId = img.getAttribute('data-thumb-item-id');
                    const inflightKey = `remote:${itemId}`;
                    if (!url || !itemId) continue;
                    if (this._thumbInflight.has(inflightKey)) continue;
                    // Strip the markers immediately so we don't reprocess on next render
                    img.removeAttribute('data-remote-thumb');
                    img.removeAttribute('data-thumb-item-id');
                    this._thumbInflight.add(inflightKey);
                    pywebview.api.cache_remote_thumb_on_demand(itemId, url).then(marker => {
                        this._thumbInflight.delete(inflightKey);
                        // Backend already persisted the marker; on next render the
                        // resolver will use it. We don't update img.src here because
                        // the current src is the remote URL which works fine while
                        // online — the benefit kicks in on next launch.
                    }).catch(() => {
                        this._thumbInflight.delete(inflightKey);
                    });
                }
            },

            createLibraryCardHTML(item) {
                // Thumbnail src handling: 'pt:thumb:' markers are placeholders that get
                // resolved to data URLs. If we've already pre-warmed the cache, use the
                // cached data URL directly so the img has src from the very first paint.
                // Otherwise fall back to data-thumb-marker so the resolver fetches lazily.
                // Remote URLs (http/https) get a data-remote-thumb attribute too so a
                // background pass can cache them locally — that's what makes future
                // offline launches work even for items that originally had remote thumbs.
                const cache = this._thumbCache || {};
                const resolveThumbAttrs = (url, itemId) => {
                    if (!url) return '';
                    if (url.startsWith('pt:thumb:')) {
                        if (cache[url]) {
                            return `src="${cache[url]}"`;
                        }
                        return `data-thumb-marker="${url}"`;
                    }
                    // Remote URL — emit src as-is for online users, plus a hint so we
                    // can cache it in the background.
                    if (itemId) {
                        return `src="${url}" data-remote-thumb="${url}" data-thumb-item-id="${itemId}"`;
                    }
                    return `src="${url}"`;
                };

                if (item.type === 'playlist') {
                    const childCount = (item.videos || []).length;
                    // Primary thumbnail: channel/playlist cover if present, else first child.
                    // If the primary URL goes stale (YouTube A/B-tested it away, CDN purged it),
                    // the error handler will fall back to the first-child thumbnail via
                    // data-fallback-thumb before giving up and showing the grey placeholder.
                    const primaryThumb = (item.thumbnails && item.thumbnails[0]) || '';
                    const childThumb = (item.videos && item.videos[0] && item.videos[0].thumbnail) || '';
                    const thumb = primaryThumb || childThumb;
                    const fallbackThumb = primaryThumb && childThumb && primaryThumb !== childThumb ? childThumb : '';
                    const title = this.escapeHtml(item.title || 'Untitled playlist');
                    const uploader = this.escapeHtml(item.uploader || '');
                    const hiddenClass = item.hidden ? ' is-hidden' : '';
                    const pinnedClass = item.pinned ? ' is-pinned' : '';
                    const hiddenBadge = item.hidden ? '<div class="library-card-hidden-badge">Hidden</div>' : '';
                    const pinBadge = item.pinned
                        ? '<div class="library-card-pin-icon" title="Pinned"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M16 3l5 5-3.5 1.5-2 2-1 5-2-2-4.5 4.5-1-1L11.5 13l-2-2L8 9.5l3.5-1.5 2-2L16 3z"/></svg></div>'
                        : '';
                    return `
                        <div class="library-card${hiddenClass}${pinnedClass}" data-item-id="${item.id}" onclick="openLibraryDetail('${item.id}')" ondblclick="openLibraryItemDirect('${item.id}')">
                            <div class="library-card-select-mark">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                            </div>
                            <div class="library-card-thumb">
                                ${thumb ? `<img ${resolveThumbAttrs(thumb, item.id)} ${fallbackThumb ? `data-fallback-thumb="${this.escapeHtml(fallbackThumb)}"` : ''} alt="">` : '<div class="library-card-thumb-placeholder"><svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg></div>'}
                                <div class="library-card-playlist-badge${classifyPlaylistEntry(item) === 'channel' ? ' is-channel' : ''}">${classifyPlaylistEntry(item) === 'channel' ? 'Channel' : 'Playlist'} · ${childCount}</div>
                                ${pinBadge}
                                ${hiddenBadge}
                            </div>
                            <div class="library-card-body">
                                <div class="library-card-title">${title}</div>
                                <div class="library-card-meta"><span>${uploader}</span></div>
                            </div>
                        </div>
                    `;
                }

                const title = this.escapeHtml(item.title || 'Untitled');
                const uploader = this.escapeHtml(item.uploader || '');
                const duration = this.escapeHtml(item.duration_string || '');
                const isMissing = !!item.missing;

                // Compute watch progress percentage if there's a saved position.
                // Prefer last_duration_seconds (captured at save time) but fall back to
                // parsing the duration string if needed.
                let watchPct = 0;
                if (item.last_position_seconds && item.last_position_seconds > 0) {
                    let durSec = item.last_duration_seconds || 0;
                    if (!durSec && item.duration_string) {
                        const parts = item.duration_string.split(':').map(p => parseInt(p, 10));
                        if (parts.length === 3) durSec = parts[0]*3600 + parts[1]*60 + parts[2];
                        else if (parts.length === 2) durSec = parts[0]*60 + parts[1];
                    }
                    if (durSec > 0) {
                        watchPct = Math.min(100, (item.last_position_seconds / durSec) * 100);
                    }
                }
                const progressBar = watchPct > 0
                    ? `<div class="library-card-watch-progress" style="width: ${watchPct}%"></div>`
                    : '';

                // "NEW" badge — shown for entries added in the last 48 hours.
                // We use seconds because backend stamps added_at as int(time.time()).
                // Suppress when:
                //   (a) user has actively engaged (>5s into the video), OR
                //   (b) user has watched all the way to the end (watched_to_end flag)
                // The watched_to_end flag is durable across position clears, so even
                // after a natural end (which zeros position), the badge stays hidden.
                const NEW_WINDOW_SEC = 48 * 60 * 60;
                let showNewBadge = false;
                if (item.added_at && (Date.now() / 1000 - item.added_at) < NEW_WINDOW_SEC) {
                    const hasEngaged = item.last_position_seconds && item.last_position_seconds >= 5;
                    const hasFinished = item.watched_to_end === true;
                    if (!hasEngaged && !hasFinished) {
                        showNewBadge = true;
                    }
                }
                const newBadge = showNewBadge
                    ? '<div class="library-card-new-badge">New</div>'
                    : '';

                const hiddenClass = item.hidden ? ' is-hidden' : '';
                const pinnedClass = item.pinned ? ' is-pinned' : '';
                const hiddenBadge = item.hidden ? '<div class="library-card-hidden-badge">Hidden</div>' : '';
                const pinBadge = item.pinned
                    ? '<div class="library-card-pin-icon" title="Pinned"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M16 3l5 5-3.5 1.5-2 2-1 5-2-2-4.5 4.5-1-1L11.5 13l-2-2L8 9.5l3.5-1.5 2-2L16 3z"/></svg></div>'
                    : '';

                return `
                    <div class="library-card ${isMissing ? 'is-missing' : ''}${hiddenClass}${pinnedClass}" data-item-id="${item.id}" onclick="openLibraryDetail('${item.id}')" ondblclick="openLibraryItemDirect('${item.id}')">
                        <div class="library-card-select-mark">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                        </div>
                        <div class="library-card-thumb">
                            ${item.thumbnail ? `<img ${resolveThumbAttrs(item.thumbnail, item.id)} alt="">` : '<div class="library-card-thumb-placeholder"><svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2"/></svg></div>'}
                            ${newBadge}
                            ${pinBadge}
                            ${hiddenBadge}
                            ${duration ? `<div class="library-card-duration">${duration}</div>` : ''}
                            ${isMissing ? `<div class="library-card-missing-badge">Missing</div>` : ''}
                            ${progressBar}
                        </div>
                        <div class="library-card-body">
                            <div class="library-card-title">${title}</div>
                            <div class="library-card-meta"><span>${uploader}</span></div>
                        </div>
                    </div>
                `;
            },

            escapeHtml(s) {
                return String(s)
                    .replace(/&/g, '&amp;')
                    .replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;')
                    .replace(/"/g, '&quot;')
                    .replace(/'/g, '&#39;');
            },

            setFilter(filter) {
                this.currentFilter = filter;
                document.querySelectorAll('.filter-chip').forEach(chip => {
                    chip.classList.toggle('active', chip.dataset.filter === filter);
                });
                this.renderQueue();
            },

            // Extract a stable identifier from a YouTube URL so we can dedup
            // pastes against what's already in queue or library. Returns
            // {type: 'video'|'playlist'|'channel', id: string} or null when the
            // URL doesn't match a known shape (in which case we let the backend
            // try to fetch it — yt-dlp accepts plenty of URL shapes we don't
            // recognize here).
            extractYouTubeId(url) {
                if (!url) return null;
                // Watch / shortlink / embed video IDs are 11 chars [\w-]
                const vid = url.match(/(?:[?&]v=|youtu\.be\/|youtube\.com\/embed\/|youtube\.com\/shorts\/)([\w-]{11})/);
                if (vid) return { type: 'video', id: vid[1] };
                // Playlist URL with no video — list= param standalone
                const pl = url.match(/[?&]list=([\w-]+)/);
                if (pl) return { type: 'playlist', id: pl[1] };
                // Channel-shape URLs — store a comparable token
                const handle = url.match(/youtube\.com\/@([\w.-]+)/i);
                if (handle) return { type: 'channel', id: '@' + handle[1].toLowerCase() };
                const chById = url.match(/youtube\.com\/channel\/(UC[\w-]+)/i);
                if (chById) return { type: 'channel', id: chById[1] };
                const chCustom = url.match(/youtube\.com\/c\/([\w.-]+)/i);
                if (chCustom) return { type: 'channel', id: '/c/' + chCustom[1].toLowerCase() };
                const chLegacy = url.match(/youtube\.com\/user\/([\w.-]+)/i);
                if (chLegacy) return { type: 'channel', id: '/user/' + chLegacy[1].toLowerCase() };
                return null;
            },

            // Find an existing entry that matches a parsed id. Looks at top-level
            // queue/library entries AND playlist children (so pasting a single
            // video URL catches the case where it's already inside a downloaded
            // playlist). Returns the matching entry or null.
            findExistingByParsedId(parsed, itemList) {
                if (!parsed || !itemList) return null;
                for (const item of itemList) {
                    if (parsed.type === 'video') {
                        // Top-level video — id is the YouTube video id
                        if (item.type === 'video' && item.id === parsed.id) return item;
                        // Or a playlist child
                        if (item.type === 'playlist' && Array.isArray(item.videos)) {
                            const child = item.videos.find(c => c.id === parsed.id);
                            if (child) return child;
                        }
                    } else {
                        // Playlist or channel — match against the stored source URL
                        if (item.type === 'playlist' && item.url) {
                            const u = item.url.toLowerCase();
                            const needle = parsed.id.toLowerCase();
                            if (u.includes(needle)) return item;
                        }
                    }
                }
                return null;
            },

            // Pull every URL-looking substring out of pasted text. Handles the
            // common cases: one-per-line, space-separated, comma-separated.
            // Returns deduped, trimmed URLs in order. If only one URL is found
            // (or the text isn't URL-like), returns that single value.
            extractMultipleUrls(raw) {
                if (!raw) return [];
                const matches = raw.match(/(?:https?:\/\/|youtu\.be\/|www\.youtube\.com\/|youtube\.com\/)\S+/gi);
                if (!matches) return [];
                const cleaned = [];
                const seen = new Set();
                for (let m of matches) {
                    // Trim trailing punctuation users often paste with URLs
                    m = m.trim().replace(/[,;.)\]'"]+$/g, '');
                    if (!m || seen.has(m)) continue;
                    seen.add(m);
                    cleaned.push(m);
                }
                return cleaned;
            },

            fetch() {
                const input = this.elements.mainUrlInput;
                const raw = input.value.trim();
                if (!raw) return;

                // Pre-empt the round-trip when we already know we're offline.
                // yt_dlp would otherwise hang for ~15s before failing with a
                // generic network error — this gives an immediate, honest reason.
                if (!navigator.onLine) {
                    showToast("You're offline — paste will work once you reconnect.", null, null);
                    return;
                }

                // Multi-URL detect — if the input contains 2+ URL-shaped tokens
                // (newline-, space-, or comma-separated paste), batch-process.
                // Single-URL paste falls through to the existing path so the
                // dedup-toast UX is unchanged for the normal case.
                const urls = this.extractMultipleUrls(raw);
                if (urls.length >= 2) {
                    this._startFetchBatch(urls);
                    return;
                }

                const url = urls[0] || raw;

                // Duplicate guard — silently bail if this URL resolves to
                // something already in queue or library, with a toast that
                // tells the user where it is. Skips the backend round-trip
                // entirely so there's no spinner flash on "obvious" repeats.
                const parsed = this.extractYouTubeId(url);
                if (parsed) {
                    const inQueue = this.findExistingByParsedId(parsed, this.videosInQueue);
                    if (inQueue) {
                        const title = inQueue.title || (parsed.type === 'video' ? 'video' : parsed.type);
                        const short = title.length > 50 ? title.slice(0, 47) + '…' : title;
                        showToast(`Already in queue: ${short}`, null, null);
                        input.value = '';
                        return;
                    }
                    const inLibrary = this.findExistingByParsedId(parsed, this.videosInLibrary);
                    if (inLibrary) {
                        const title = inLibrary.title || (parsed.type === 'video' ? 'video' : parsed.type);
                        const short = title.length > 50 ? title.slice(0, 47) + '…' : title;
                        showToast(`Already in library: ${short}`, 'View', () => {
                            app.switchView('library');
                        });
                        input.value = '';
                        return;
                    }
                }

                setFetchLoading(true, 'main', 'Fetching...');
                pywebview.api.fetch_url_info(url, 'browser', 'none');
            },

            // Multi-URL batch path. Dedups the batch against queue, library,
            // AND itself (paste with the same URL twice → only one fetch).
            // Then enqueues survivors and kicks the pump.
            // NAMED `_startFetchBatch` (not `_fetchBatch`) on purpose — the
            // batch STATE lives at app._fetchBatch. A method with the same
            // name overwrites itself when state is assigned, and on app
            // startup `app._fetchBatch` would be this function (truthy) —
            // causing finishFetch's `inBatch = !!app._fetchBatch` to be true
            // for Subscribe / single-URL flows, then crash on
            // `batch.pending.length`. Don't rename back without untangling.
            _startFetchBatch(urls) {
                const seenInBatch = new Set();
                const survivors = [];
                let dupQueue = 0, dupLibrary = 0, dupSelf = 0;
                for (const u of urls) {
                    const parsed = this.extractYouTubeId(u);
                    if (parsed) {
                        const key = parsed.type + ':' + parsed.id.toLowerCase();
                        if (seenInBatch.has(key)) { dupSelf++; continue; }
                        seenInBatch.add(key);
                        if (this.findExistingByParsedId(parsed, this.videosInQueue)) { dupQueue++; continue; }
                        if (this.findExistingByParsedId(parsed, this.videosInLibrary)) { dupLibrary++; continue; }
                    }
                    survivors.push(u);
                }
                this.elements.mainUrlInput.value = '';
                if (survivors.length === 0) {
                    const total = dupQueue + dupLibrary + dupSelf;
                    showToast(`All ${total} URL${total === 1 ? '' : 's'} are already in queue or library`, null, null);
                    return;
                }
                // Stash batch state on app so finishFetch knows to advance,
                // and to show a summary toast when done.
                app._fetchBatch = {
                    pending: survivors.slice(),
                    totalSurvivors: survivors.length,
                    dupSkipped: dupQueue + dupLibrary + dupSelf,
                    errors: 0,
                    completed: 0,
                    inFlight: false,
                };
                this._pumpFetchBatch();
            },

            // Pumps the next URL in app._fetchBatch.pending. Re-invoked from
            // finishFetch each time a fetch resolves. Stops when pending is
            // empty and shows a single summary toast for the whole batch.
            _pumpFetchBatch() {
                const batch = app._fetchBatch;
                if (!batch || batch.inFlight) return;
                if (batch.pending.length === 0) {
                    // Done — summary toast.
                    const parts = [];
                    parts.push(`Fetched ${batch.completed} URL${batch.completed === 1 ? '' : 's'}`);
                    if (batch.dupSkipped) parts.push(`${batch.dupSkipped} duplicate`);
                    if (batch.errors) parts.push(`${batch.errors} failed`);
                    showToast(parts.join(' · '), null, null);
                    app._fetchBatch = null;
                    return;
                }
                const url = batch.pending.shift();
                batch.inFlight = true;
                const idx = batch.totalSurvivors - batch.pending.length;
                setFetchLoading(true, 'main', `Fetching ${idx} of ${batch.totalSurvivors}…`);
                try {
                    pywebview.api.fetch_url_info(url, 'browser', 'none');
                } catch (e) {
                    batch.errors++;
                    batch.inFlight = false;
                    setTimeout(() => app.fetch && app._pumpFetchBatch(), 0);
                }
            },

            startDownload() {
                const toDown = this.videosInQueue.filter(v => this.selectedVideoIds.has(v.id));
                this.elements.downloadButton.classList.add('hidden');
                this.elements.cancelButton.classList.remove('hidden');
                pywebview.api.start_download(toDown, 'browser', 'none');
            },

            cancelAllDownloads() {
                pywebview.api.cancel_all_downloads();
                // Optimistic UI update — mark all currently-downloading items as Cancelled
                // (backend will also fire updateItemStatus events, but this is snappier)
                this.getFlatVideoList().forEach(v => {
                    if (v.status === 'Downloading') {
                        v.status = 'Cancelled';
                        v.progressPct = null;
                    }
                });
                // Refresh the DOM based on current view
                this.renderQueue();
                if (this.currentPlaylistId) {
                    const pl = this.videosInQueue.find(i => i.type === 'playlist' && i.id === this.currentPlaylistId);
                    if (pl) this.renderPlaylistDetail(pl);
                }
                this.elements.downloadButton.classList.remove('hidden');
                this.elements.cancelButton.classList.add('hidden');
            },

            // Classify an item by STATUS — used internally for "is this failed?"
            // checks (which still drive the Failed chip + the filter-stats line).
            // The chip filters now operate on TYPE (video / playlist / channel)
            // via matchesFilter() below; status classification is a separate axis.
            classifyItem(item) {
                // For playlists, use their aggregate state
                if (item.type === 'playlist') {
                    const vids = (item.videos || []).filter(v => v.selected !== false);
                    if (vids.length === 0) return 'queued';
                    const anyActive = vids.some(v => v.status === 'Downloading' || v.status === 'Retrying');
                    const anyFailed = vids.some(v => v.status === 'Error');
                    const allDone = vids.every(v => v.status === 'Done');
                    if (anyActive) return 'active';
                    if (allDone) return 'done';
                    if (anyFailed) return 'failed';
                    return 'queued';
                }
                // Single video
                const s = item.status;
                if (s === 'Downloading' || s === 'Retrying') return 'active';
                if (s === 'Done') return 'done';
                if (s === 'Error') return 'failed';
                return 'queued';
            },

            // Whether a queue item matches the currently selected chip filter.
            // 'all' shows everything; 'failed' is status-based; the rest are
            // type-based and use classifyPlaylistEntry to split playlists into
            // playlist vs channel.
            matchesFilter(item, filter) {
                if (!filter || filter === 'all') return true;
                if (filter === 'failed') return this.classifyItem(item) === 'failed';
                if (filter === 'videos') return item.type === 'video';
                if (filter === 'playlists') {
                    return item.type === 'playlist' && classifyPlaylistEntry(item) === 'playlist';
                }
                if (filter === 'channels') {
                    return item.type === 'playlist' && classifyPlaylistEntry(item) === 'channel';
                }
                return true;
            },

            renderQueue() {
                // Apply current filter (type-based for videos/playlists/channels,
                // status-based for failed, no-op for all)
                const filtered = this.videosInQueue.filter(item => !item.isPreview && this.matchesFilter(item, this.currentFilter));

                // Empty state handling. We collapse the list container's
                // display when the empty state shows so the empty's `flex: 1`
                // takes the full vertical space and centers properly. Without
                // this, the (empty) list still claimed half the space and the
                // empty state was rendered in the lower half.
                const emptyEl = this.elements.queueEmpty;
                const listEl = this.elements.videoList;
                if (this.videosInQueue.length === 0) {
                    // Totally empty queue
                    this.elements.queueEmptyTitle.textContent = 'Queue is empty';
                    this.elements.queueEmptyHint.textContent = 'Paste a YouTube URL above to get started';
                    emptyEl.classList.remove('hidden');
                    listEl.innerHTML = '';
                    listEl.style.display = 'none';
                } else if (filtered.length === 0) {
                    // Queue has items but nothing matches the active filter
                    this.elements.queueEmptyTitle.textContent = 'Nothing here';
                    this.elements.queueEmptyHint.textContent = `No items match "${this.currentFilter}"`;
                    emptyEl.classList.remove('hidden');
                    listEl.innerHTML = '';
                    listEl.style.display = 'none';
                } else {
                    emptyEl.classList.add('hidden');
                    listEl.style.display = '';
                    listEl.innerHTML = filtered
                        .map(item => item.type === 'playlist' ? this.createPlaylistItemHTML(item) : this.createVideoItemHTML(item))
                        .join('');
                }

                this.updateDashboard();
                this.updateSelection();
                // Resolve any 'pt:thumb:' markers on queue cards by fetching the cached
                // bytes from backend. Same path the library uses — keeps queue thumbs
                // visible offline once they've been cached.
                this._resolvePendingThumbnails();

                // For items with any known status, replay it so cards reflect current state.
                // This covers: Done (shows reveal button), Error (shows badge), Downloading
                // (rebuilds the progress bar with the last known pct so in-flight downloads don't
                // visually reset when the queue is re-rendered).
                const replayStatus = (v, playlistId) => {
                    if (!v.status) return;
                    updateItemStatus(v.id, v.status, playlistId, v.filepath, v.folderpath);
                    if (v.status === 'Downloading' && typeof v.progressPct === 'number') {
                        updateItemProgress(
                            v.id, v.progressPct, v.progressSpeed || '',
                            playlistId, v.downloadedBytes, v.totalBytes, v.progressSpeedBytes
                        );
                    }
                };
                this.videosInQueue.forEach(item => {
                    if (item.type === 'playlist') {
                        (item.videos || []).forEach(v => replayStatus(v, item.id));
                    } else {
                        replayStatus(item, null);
                    }
                });

                // Enable/disable drag based on video count. Always rebuild Sortable because the
                // DOM was just rewritten — stale instances lose their refs and drag stops saving.
                if (this.videosInQueue.length >= 2) {
                    this.elements.videoList.classList.add('draggable');
                    this.initSortable();
                } else {
                    this.elements.videoList.classList.remove('draggable');
                    if (this.sortable) {
                        try { this.sortable.destroy(); } catch(_) {}
                        this.sortable = null;
                    }
                }
            },

            createPlaylistItemHTML(p) {
                const isSelected = this.selectedVideoIds.has(p.id);
                const thumbs = p.thumbnails && p.thumbnails.length > 0 ? p.thumbnails : [];
                const front = thumbs[0] || '';
                const mid = thumbs[1] || front;
                const back = thumbs[2] || mid;

                // Resolver path identical to video rows. We use the playlist's own id
                // as the data-thumb-item-id so updateItemThumbnail can find the right
                // <img> in the stack to update when caching completes.
                const cache = this._thumbCache || {};
                const resolveThumb = (url) => {
                    if (!url) return '';
                    if (url.startsWith('pt:thumb:')) {
                        if (cache[url]) return `src="${cache[url]}"`;
                        return `data-thumb-marker="${url}"`;
                    }
                    return `src="${url}"`;
                };

                // Pre-escape every string field that comes from yt-dlp before
                // it goes into the template. Without this, a YouTube playlist
                // titled `<img src=x onerror=...>` would execute in our app
                // context with full pywebview.api access (XSS).
                const eTitle = this.escapeHtml(p.title || '');
                const eUploader = this.escapeHtml(p.uploader || '');
                const eId = this.escapeHtml(p.id || '');
                const isChan = classifyPlaylistEntry(p) === 'channel';
                return `
                    <div id="item-${eId}" data-item-id="${eId}" class="playlist-row ${isSelected ? 'selected' : ''}" onclick="handlePlaylistCardClick(event, '${eId}')">
                        <div class="drag-handle">
                            <svg fill="currentColor" viewBox="0 0 20 20">
                                <path d="M10 6a2 2 0 110-4 2 2 0 010 4zM10 12a2 2 0 110-4 2 2 0 010 4zM10 18a2 2 0 110-4 2 2 0 010 4z"/>
                            </svg>
                        </div>
                        <input type="checkbox" class="video-checkbox" onchange="app.updateSelection()" data-id="${eId}" ${isSelected ? 'checked' : ''}>
                        <div class="playlist-thumb-stack">
                            <div class="pt pt-back">${back ? `<img ${resolveThumb(back)} alt="">` : ''}</div>
                            <div class="pt pt-mid">${mid ? `<img ${resolveThumb(mid)} alt="">` : ''}</div>
                            <div class="pt pt-front">${front ? `<img ${resolveThumb(front)} alt="">` : ''}</div>
                            <div class="playlist-video-count">
                                <svg fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2.5"><path d="M4 6h16M4 12h16M4 18h10"/></svg>
                                ${p.videoCount}
                            </div>
                        </div>
                        <div class="video-details">
                            <div class="video-heading" data-tip-wrap="${eTitle}"><span class="playlist-badge${isChan ? ' is-channel' : ''}">${isChan ? 'Channel' : 'Playlist'}</span>${eTitle}</div>
                            <div class="video-meta-line">${eUploader} · ${p.videoCount} videos</div>
                        </div>
                        <div class="playlist-progress-wrap" id="pl-prog-${eId}">
                            <div class="playlist-progress-text"><span class="pl-prog-label">0 of ${p.videoCount} done</span></div>
                            <div class="playlist-progress-bar-bg">
                                <div class="playlist-progress-bar"></div>
                            </div>
                        </div>
                        <div class="playlist-open-hint" data-tip-right="Open playlist">
                            Open
                            <svg fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2.2"><path d="M9 5l7 7-7 7"/></svg>
                        </div>
                        <button class="remove-btn" onclick="event.stopPropagation(); app.removeItem('${eId}')" data-tip-right="Remove">
                            <svg fill="currentColor" viewBox="0 0 20 20">
                                <path d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z"/>
                            </svg>
                        </button>
                    </div>
                `;
            },

            // ========== Playlist detail view ==========
            currentPlaylistId: null,

            openPlaylistDetail(playlistId) {
                const pl = this.videosInQueue.find(i => i.type === 'playlist' && i.id === playlistId);
                if (!pl) return;
                this.currentPlaylistId = playlistId;

                // Render the detail view
                this.renderPlaylistDetail(pl);

                // Switch views. Hide EVERY content pane (not just the queue) so the
                // detail view fully replaces whatever was showing — channel previews
                // open from the search pane too, and hiding only the queue left the
                // search results stacked above the channel. Cleared again on close.
                document.querySelectorAll('.cockpit-main > .view-pane').forEach(p => { p.style.display = 'none'; });
                this.elements.queueView.style.display = 'none';
                this.elements.playlistDetailView.classList.add('visible');

                // Auto-refetch channel branding for channels that pre-date the
                // channelAvatar/banner/subscriber fields. Silent — no toast, no
                // progress bar; the hero just morphs into a complete YouTube-
                // style header once the data lands. This is what saves the user
                // from having to click "Check for new videos" on every legacy
                // channel.
                // Auto-refetch channel branding when missing — but ONLY once
                // per channel per hour. Without the cooldown we'd re-fire the
                // fetch on every open for channels where yt-dlp returned no
                // avatar/banner (rare but real), making the user wait every
                // single time. Once branding is set, the !channelAvatar guard
                // skips the fetch outright.
                if (classifyPlaylistEntry(pl) === 'channel' && !pl.channelAvatar) {
                    const HOUR_MS = 60 * 60 * 1000;
                    const triedRecently = pl._brandingFetchedAt
                        && (Date.now() - pl._brandingFetchedAt) < HOUR_MS;
                    if (!triedRecently) {
                        pl._brandingFetchedAt = Date.now();
                        this.saveQueueState();
                        this._silentRefetchChannelBranding(pl);
                    }
                }

                // Kick off format resolution if not done
                if (!pl.formatsResolved) {
                    const unresolvedUrls = pl.videos
                        .filter(v => !v.formats)
                        .map(v => ({ id: v.id, url: v.url }));
                    if (unresolvedUrls.length > 0) {
                        pywebview.api.resolve_playlist_formats(playlistId, unresolvedUrls, 'browser', 'none');
                    }
                }
            },

            // Promote a read-only preview channel into a saved (subscribed) one:
            // drop the isPreview flag so it persists + shows in the queue rail.
            subscribeChannelPreview(playlistId) {
                const pl = this.videosInQueue.find(i => i.id === playlistId);
                if (!pl || !pl.isPreview) return;
                delete pl.isPreview;
                this.saveQueueState();   // now persists (filter no longer excludes it)
                this.renderQueue();      // appears in the queue rail
                this.renderPlaylistDetail(pl);   // re-render to drop the Subscribe pill
                showToast('Subscribed — added to your queue', null, null);
            },

            async _silentRefetchChannelBranding(pl) {
                if (!pl || !pl.url) return;
                try {
                    // Uses get_channel_metadata (no entry walk) so this is
                    // sub-second even for channels with thousands of videos.
                    const result = await pywebview.api.get_channel_metadata(pl.id);
                    if (!result || !result.ok) return;
                    let changed = false;
                    if (result.avatar && pl.channelAvatar !== result.avatar) {
                        pl.channelAvatar = result.avatar;
                        changed = true;
                    }
                    if (result.banner && pl.channelBanner !== result.banner) {
                        pl.channelBanner = result.banner;
                        changed = true;
                    }
                    if (result.subscriberCountString && pl.subscriberCountString !== result.subscriberCountString) {
                        pl.subscriberCountString = result.subscriberCountString;
                        changed = true;
                    }
                    if (result.subscriberCount != null && pl.subscriberCount !== result.subscriberCount) {
                        pl.subscriberCount = result.subscriberCount;
                        changed = true;
                    }
                    if (result.description && pl.channelDescription !== result.description) {
                        pl.channelDescription = result.description;
                        changed = true;
                    }
                    if (!changed) return;
                    this.saveQueueState();
                    if (this.currentPlaylistId === pl.id) this.renderPlaylistDetail(pl);
                } catch (_) {
                    // Silent — auto-refetch failures shouldn't surface; user can
                    // still hit "Check for new videos" manually.
                }
            },

            closePlaylistDetail() {
                this._teardownChannelStickyObserver();
                const sticky = document.getElementById('pd-sticky-header');
                if (sticky) {
                    sticky.classList.remove('is-visible');
                    sticky.setAttribute('hidden', '');
                }
                const returningFromId = this.currentPlaylistId;
                this.currentPlaylistId = null;
                this.elements.playlistDetailView.classList.remove('visible');
                // Reveal the pane that's still active (clear the inline hides set in
                // openPlaylistDetail; the .active CSS shows the right one — search if
                // you came from a channel preview, otherwise the queue).
                document.querySelectorAll('.cockpit-main > .view-pane').forEach(p => { p.style.display = ''; });
                // A transient preview channel (read-only browse, never saved) is
                // dropped when you leave it — UNLESS it has downloads in flight, in
                // which case keep it in memory (still hidden from the rail +
                // persistence) so progress keeps routing to its rows.
                const _prev = returningFromId && this.videosInQueue.find(i => i.id === returningFromId);
                if (_prev && _prev.isPreview) {
                    const busy = (_prev.videos || []).some(v => ['Downloading', 'Queued', 'Paused', 'Retrying'].includes(v.status));
                    if (!busy) {
                        this.videosInQueue = this.videosInQueue.filter(i => i.id !== returningFromId);
                    }
                }
                // DON'T call renderQueue() here — it wipes active progress bars and re-inits Sortable,
                // which makes in-flight downloads look like they restarted. The queue DOM is intact;
                // only the playlist card's rollup and global dashboard may need updating.
                if (returningFromId && !(_prev && _prev.isPreview)) {
                    this.updatePlaylistCardRollup(returningFromId);
                }
                this.updateDashboard();
            },

            renderPlaylistDetail(pl) {
                const isChan = classifyPlaylistEntry(pl) === 'channel';

                // Channels open clean EVERY time — "let it be like as if I'm
                // looking at the channel." A finished or library-owned video
                // should not stay ringed forever from a prior selection.
                // We preserve `selected` only for videos that are mid-flight
                // (Downloading / Retrying / Paused / Cancelled / Error) so an
                // in-progress batch isn't visually disrupted by re-opening the
                // detail view.
                if (isChan) {
                    const inFlight = new Set(['Downloading', 'Retrying', 'Paused', 'Cancelled', 'Error']);
                    let mutated = false;
                    for (const child of pl.videos) {
                        if (child.selected === true && !inFlight.has(child.status)) {
                            child.selected = false;
                            mutated = true;
                        }
                    }
                    if (mutated) this.saveQueueState();
                }

                const pickedCount = pl.videos.filter(v => v.selected === true).length;
                // For channels prefer the real avatar pulled from yt-dlp; fall
                // back to legacy entries' first thumbnail. For playlists keep
                // the existing first-child fallback (playlists have no canonical
                // avatar — the user accepted this).
                const heroThumb = (isChan && pl.channelAvatar)
                    || (pl.thumbnails && pl.thumbnails[0])
                    || (pl.videos[0] && pl.videos[0].thumbnail)
                    || '';

                const videosHTML = pl.videos.map((v, idx) => isChan
                    ? this.createPdChannelCardHTML(v, idx)
                    : this.createPdVideoRowHTML(v, idx)
                ).join('');

                // XSS guard — escape every yt-dlp-derived string before it lands
                // in the template. Hero is the loudest surface; an unescaped title
                // here means every visit to a playlist's detail view triggers any
                // payload baked into the title.
                const eTitle = this.escapeHtml(pl.title || '');
                const eUploader = this.escapeHtml(pl.uploader || '');
                const eId = this.escapeHtml(pl.id || '');
                const eThumb = this.escapeHtml(heroThumb || '');
                const eBanner = this.escapeHtml(pl.channelBanner || '');
                const eQuality = this.escapeHtml(pl.defaultQuality || '');
                // Pull @handle from uploader_url when available so the channel
                // header can render `@handle · N videos` like YouTube's page.
                let channelHandle = '';
                if (isChan && pl.uploader_url) {
                    const m = String(pl.uploader_url).match(/youtube\.com\/(@[\w.-]+)/i);
                    if (m) channelHandle = m[1];
                }
                const eHandle = this.escapeHtml(channelHandle);

                // Bottom action bar (channel only) — invisible by default,
                // slides up on selection. Holds the quality picker, clear,
                // and Download N button.
                const channelActionBar = isChan ? `
                    <div class="pd-channel-action-bar" id="pd-channel-action-bar">
                        <span class="pd-channel-action-count" id="pd-channel-action-count">0 selected</span>
                        <div class="pd-channel-action-spacer"></div>
                        <button class="pd-channel-action-quality" id="pd-channel-action-quality" onclick="event.stopPropagation(); app.togglePdChannelQualityMenu()">
                            <span id="pd-channel-action-quality-label">${eQuality}</span>
                            <svg class="caret" viewBox="0 0 12 8" fill="none"><path d="M1 1.5L6 6.5L11 1.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
                            <div class="pd-channel-quality-menu" id="pd-channel-quality-menu" onclick="event.stopPropagation()">${this.buildDefaultQualityMenuHTML(pl)}</div>
                        </button>
                        <button class="pd-channel-action-clear" onclick="app.clearChannelSelection()">Cancel</button>
                        <button class="pd-channel-action-go" id="pd-channel-action-go" onclick="app.downloadPlaylist()">Download 0</button>
                    </div>
                ` : '';

                // Channel-mode hero — mirrors YouTube's channel page:
                //   [banner band, rounded]
                //   [avatar] [name big & bold]
                //            [@handle · X subscribers · X videos]
                //            [short bio, 2-line clamp, with …more affordance]
                //            [Check for new videos pill]
                // Playlist-mode hero is unchanged (320px thumb on the left).
                const eSubs = this.escapeHtml(pl.subscriberCountString || '');
                const eBio = this.escapeHtml(pl.channelDescription || '');
                const statsParts = [];
                if (eHandle) statsParts.push(`<span class="pd-channel-handle">${eHandle}</span>`);
                if (eSubs) statsParts.push(`<span>${eSubs}</span>`);
                statsParts.push(`<span>${pl.videoCount} videos</span>`);
                const statsHTML = statsParts.join('<span class="pd-channel-stats-sep">·</span>');

                // "Check for new videos" lives as a small refresh icon overlaid
                // on the banner's top-right corner — frees up the row under the
                // bio that was burning whole-line vertical space for a single
                // button.
                const checkBtn = pl.url
                    ? `<button class="pd-channel-refresh-btn" onclick="checkPlaylistUpdates('${eId}')" data-tip-left="Check for new videos">
                           <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                               <polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/>
                               <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>
                           </svg>
                       </button>`
                    : '';

                // Back arrow rendered INSIDE the banner-wrap for channels so
                // it sits inside the banner and scrolls with it (the user's
                // direction: "make it part of the banner like the refresh
                // icon, don't have it floating in the corner").
                const channelBackBtn = `<button class="pd-channel-back-btn" onclick="app.closePlaylistDetail()" data-tip="Back to queue" aria-label="Back to queue">
                    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2.2"><path d="M15 19l-7-7 7-7"/></svg>
                </button>`;

                const channelHeader = isChan ? `
                    <div class="pd-channel-header">
                        <div class="pd-channel-banner-wrap">
                            ${eBanner
                                ? `<div class="pd-channel-banner" style="background-image: url('${eBanner}');"></div>`
                                : `<div class="pd-channel-banner pd-channel-banner-empty"></div>`}
                            ${channelBackBtn}
                            ${checkBtn}
                        </div>
                        <div class="pd-channel-meta">
                            <div class="pd-channel-avatar">
                                ${heroThumb ? `<img src="${eThumb}" alt="${eTitle}">` : ''}
                            </div>
                            <div class="pd-channel-info">
                                <div class="pd-channel-name">${eTitle}</div>
                                <div class="pd-channel-stats">${statsHTML}</div>
                                ${pl.isPreview ? `<button class="pd-channel-subscribe-btn" onclick="app.subscribeChannelPreview('${eId}')">
                                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M5 12h14"/></svg>
                                    Subscribe to save
                                </button>` : ''}
                                ${eBio ? `<div class="pd-channel-bio is-collapsed">
                                    <span class="pd-channel-bio-text">${eBio}</span>
                                    <button class="pd-channel-bio-toggle" onclick="togglePdChannelBio(this.closest('.pd-channel-bio'))">…more</button>
                                </div>` : ''}
                            </div>
                        </div>
                    </div>
                ` : '';

                const playlistHero = !isChan ? `
                    <div class="pd-hero">
                        <div class="pd-hero-thumb">
                            ${heroThumb ? `<img src="${eThumb}" alt="${eTitle}">` : ''}
                            <div class="pd-hero-overlay">
                                <span class="pd-hero-count">
                                    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2.5"><path d="M4 6h16M4 12h16M4 18h10"/></svg>
                                    ${pl.videoCount} videos
                                </span>
                            </div>
                        </div>
                        <div class="pd-hero-title">${eTitle}</div>
                        <div class="pd-hero-uploader">${eUploader}</div>
                        <div class="pd-hero-stats">
                            <span class="stat-pill pill-selected" id="pd-picked-pill">${pickedCount} of ${pl.videoCount} picked</span>
                        </div>
                        <div class="pd-quality-row">
                            <span class="pd-quality-row-label">Default quality</span>
                            <div class="quality-picker" id="qpicker-pl-${eId}" onclick="event.stopPropagation()">
                                <button class="quality-trigger" onclick="toggleQualityMenu(event, 'pl-${eId}')">
                                    <span class="qtrig-label">${eQuality}</span>
                                    <svg class="caret" viewBox="0 0 12 8" fill="none"><path d="M1 1.5L6 6.5L11 1.5" stroke="#737373" stroke-width="2" stroke-linecap="round"/></svg>
                                </button>
                                <div class="quality-menu">${this.buildDefaultQualityMenuHTML(pl)}</div>
                            </div>
                        </div>
                        <div class="pd-hero-action-row">
                            <button class="pd-download-btn" id="pd-download-btn" onclick="app.downloadPlaylist()">Download ${pickedCount}</button>
                            ${pl.url ? `<button class="pd-update-btn" onclick="checkPlaylistUpdates('${eId}')" data-tip="Re-fetch from YouTube and add any new videos">Check for updates</button>` : ''}
                        </div>
                    </div>
                ` : '';

                this.elements.pdLayout.className = 'pd-layout' + (isChan ? ' channel-mode' : '');
                this.elements.pdLayout.innerHTML = `
                    ${channelHeader}
                    ${playlistHero}
                    <div class="pd-videos">
                        <div class="pd-videos-top">
                            <button class="select-all-pill" onclick="app.togglePlaylistSelectAll()">
                                <span class="sa-icon" id="pd-sa-icon">
                                    ${this.getSelectAllIconHTML(pickedCount, pl.videoCount)}
                                </span>
                                <span class="sa-label" id="pd-sa-label">${this.getSelectAllLabel(pickedCount, pl.videoCount)}</span>
                            </button>
                        </div>
                        <div class="${isChan ? 'library-grid is-channel-grid' : 'pd-videos-list'}" id="pd-videos-list">
                            ${videosHTML}
                        </div>
                    </div>
                    ${channelActionBar}
                `;

                // Show/hide the floating back button per mode. Channel mode
                // renders its own back button inside the banner; playlist mode
                // keeps the absolute-positioned one for its hero.
                const floatBack = document.getElementById('pd-back-btn');
                if (floatBack) floatBack.style.display = isChan ? 'none' : '';

                // Sticky compact header — populate + wire observer for channels;
                // hide entirely for playlists.
                const sticky = document.getElementById('pd-sticky-header');
                if (sticky) {
                    if (isChan) {
                        const nameEl = document.getElementById('pd-sticky-name');
                        const avatarEl = document.getElementById('pd-sticky-avatar');
                        const refreshEl = document.getElementById('pd-sticky-refresh');
                        if (nameEl) nameEl.textContent = pl.title || '';
                        if (avatarEl) {
                            avatarEl.style.backgroundImage = heroThumb
                                ? `url('${eThumb}')`
                                : '';
                        }
                        if (refreshEl) {
                            refreshEl.onclick = () => checkPlaylistUpdates(pl.id);
                        }
                        sticky.removeAttribute('hidden');
                        sticky.classList.remove('is-visible');
                        this._wireChannelStickyObserver();
                    } else {
                        sticky.classList.remove('is-visible');
                        sticky.setAttribute('hidden', '');
                        this._teardownChannelStickyObserver();
                    }
                }

                if (isChan) this.refreshPdChannelActionBar();
            },

            // IntersectionObserver: when the channel banner is fully out of the
            // scroll container's visible area, fade in the sticky compact header.
            // When banner is back in view, fade it out. Mirrors YouTube's
            // channel-page behavior.
            _wireChannelStickyObserver() {
                this._teardownChannelStickyObserver();
                const sticky = document.getElementById('pd-sticky-header');
                const banner = document.querySelector('.pd-layout.channel-mode .pd-channel-banner-wrap');
                const scroller = document.querySelector('.pd-layout.channel-mode');
                if (!sticky || !banner || !scroller) return;
                this._channelBannerObserver = new IntersectionObserver((entries) => {
                    for (const entry of entries) {
                        if (entry.target === banner) {
                            sticky.classList.toggle('is-visible', !entry.isIntersecting);
                        }
                    }
                }, {
                    root: scroller,
                    // -1px rootMargin keeps the observer from firing on the
                    // exact edge boundary, which on some scroll deltas was
                    // causing rapid toggle ("flashing") between visible and
                    // hidden states.
                    rootMargin: '-1px 0px 0px 0px',
                    threshold: 0,
                });
                this._channelBannerObserver.observe(banner);
            },
            _teardownChannelStickyObserver() {
                if (this._channelBannerObserver) {
                    try { this._channelBannerObserver.disconnect(); } catch (_) {}
                    this._channelBannerObserver = null;
                }
            },

            createPdChannelCardHTML(v, idx) {
                // Channel-grid card. Reuses .library-card / .library-card-thumb /
                // .library-card-duration / .library-card-body / .library-card-title
                // so the channel grid looks identical to the library grid.
                // Channel-specific extras layered on top:
                //   - .is-selected for the picked-blue-ring (no checkmark)
                //   - .is-resolving for the half-faded pre-formats state
                //   - .library-card-state-badge top-right for active/terminal status
                //   - .library-card-watch-progress repurposed as the download bar
                const picked = v.selected === true;
                const resolving = !v.formats;

                const inLibrary = (() => {
                    if (!this.videosInLibrary) return false;
                    for (const entry of this.videosInLibrary) {
                        if (entry.type === 'playlist') {
                            if ((entry.videos || []).some(c => c.id === v.id && !c.missing)) return true;
                        } else {
                            if (entry.id === v.id && !entry.missing) return true;
                        }
                    }
                    return false;
                })();

                const eTitle = this.escapeHtml(v.title || '');
                const eId = this.escapeHtml(v.id || '');
                const eDuration = this.escapeHtml(v.duration_string || '');
                const safeThumb = (v.thumbnail && /^(?:https?:|pt:thumb:|data:image\/)/i.test(v.thumbnail))
                    ? this.escapeHtml(v.thumbnail) : '';

                const checkSvg = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`;

                // Tiny, low-noise badges — text-only for the % progress chip,
                // colored dots (with hover tooltips) for active/error states,
                // and a small green check-circle for Downloaded. No labels.
                let stateBadge = '';
                if (v.status === 'Downloading') {
                    const pct = typeof v.progressPct === 'number' ? Math.floor(v.progressPct) : 0;
                    stateBadge = `<span class="library-card-state-badge s-downloading">${pct}%</span>`;
                } else if (v.status === 'Retrying') {
                    stateBadge = `<span class="library-card-state-badge s-retrying" data-tip="Retrying download"></span>`;
                } else if (v.status === 'Paused') {
                    stateBadge = `<span class="library-card-state-badge s-paused" data-tip="Paused"></span>`;
                } else if (v.status === 'Cancelled') {
                    stateBadge = `<span class="library-card-state-badge s-cancelled" data-tip="Cancelled"></span>`;
                } else if (v.status === 'Error') {
                    const msg = (v.errorMessage || 'Download failed.').replace(/"/g, '&quot;');
                    stateBadge = `<span class="library-card-state-badge s-error" data-tip="${msg}"></span>`;
                } else if (inLibrary || v.status === 'Done') {
                    stateBadge = `<span class="library-card-state-badge s-owned" data-tip="Downloaded">${checkSvg}</span>`;
                }

                let progressBar = '';
                if (v.status === 'Downloading') {
                    const pct = typeof v.progressPct === 'number' ? v.progressPct : 0;
                    progressBar = `<div class="library-card-watch-progress" style="width: ${pct}%;"></div>`;
                }

                // Crisp 16:9 thumbnail. Derive maxresdefault from the video id at
                // RENDER time so we override any cached low-res mqdefault URL stored
                // on channels fetched before this change (no re-fetch needed). maxres
                // 404s for non-HD uploads → onerror falls back to hqdefault, which
                // always exists. `.library-card-thumb img` is object-fit:cover on a
                // 16:9 box, so hqdefault's 4:3 letterbox bars get cropped — both
                // render clean. lazy-load since maxres is heavier than the old mqdefault
                // (the #1a1a1a thumb bg covers any brief blank, so no white flash).
                const ytId = (v.id && /^[\w-]{11}$/.test(v.id)) ? v.id : '';
                const thumbInner = ytId
                    ? `<img src="https://i.ytimg.com/vi/${ytId}/maxresdefault.jpg" loading="lazy" alt="" onerror="this.onerror=null;this.src='https://i.ytimg.com/vi/${ytId}/hqdefault.jpg'">`
                    : (safeThumb
                        ? `<img src="${safeThumb}" alt="">`
                        : `<div class="library-card-thumb-placeholder"><svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2"/></svg></div>`);

                const isDownloaded = inLibrary || v.status === 'Done';
                const classes = ['library-card'];
                if (picked) classes.push('is-selected');
                if (resolving) classes.push('is-resolving');
                if (isDownloaded) classes.push('is-downloaded');

                // Downloaded videos: double-click plays them via the same
                // library player path the library grid uses. Single-click
                // is intentionally still bound to togglePlaylistVideo, which
                // no-ops for downloaded items so selection can't be toggled
                // back on.
                const dblClickAttr = isDownloaded
                    ? ` ondblclick="event.stopPropagation(); playLibraryItem('${eId}')"`
                    : '';

                return `
                    <div class="${classes.join(' ')}" id="pd-v-${eId}" data-item-id="${eId}" onclick="app.togglePlaylistVideo('${eId}')"${dblClickAttr} oncontextmenu="event.preventDefault(); event.stopPropagation();">
                        <div class="library-card-thumb">
                            ${thumbInner}
                            ${eDuration ? `<div class="library-card-duration">${eDuration}</div>` : ''}
                            ${stateBadge}
                            ${progressBar}
                        </div>
                        <div class="library-card-body">
                            <div class="library-card-title">${eTitle}</div>
                            ${(() => {
                                // View count + date — only on channel PREVIEW cards
                                // (search-opened), where they help decide what to grab.
                                const _pl = this.videosInQueue.find(i => i.id === this.currentPlaylistId);
                                if (!(_pl && _pl.isPreview)) return '';
                                const _bits = [v.view_count_string, v.published_time].filter(Boolean).map(s => this.escapeHtml(s)).join(' · ');
                                return _bits ? `<div class="pd-channel-card-stats">${_bits}</div>` : '';
                            })()}
                        </div>
                    </div>
                `;
            },

            createPdVideoRowHTML(v, idx) {
                const picked = v.selected !== false;
                const resolving = !v.formats;
                const checkSvg = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M5 13l4 4L19 7"/></svg>`;

                // Cross-reference against the library so we can show a badge on
                // videos already downloaded. Checks top-level entries AND playlist
                // children. Updates automatically because createPdVideoRowHTML is
                // re-called by updatePdVideoStatusInDom on any status change.
                const inLibrary = (() => {
                    if (!this.videosInLibrary) return false;
                    for (const entry of this.videosInLibrary) {
                        if (entry.type === 'playlist') {
                            if ((entry.videos || []).some(c => c.id === v.id && !c.missing)) return true;
                        } else {
                            if (entry.id === v.id && !entry.missing) return true;
                        }
                    }
                    return false;
                })();

                const subRight = resolving
                    ? `<span class="pd-video-resolving"><span class="pd-resolve-spinner"></span>Resolving…</span>`
                    : (v.sizeMap && v.selectedQuality && v.sizeMap[v.selectedQuality])
                        ? `${formatBytes(v.sizeMap[v.selectedQuality])}`
                        : (v.formats && v.formats[0]) ? `${v.formats[0].filesize_string}` : '';

                // Right-side indicator: status badge (Done / Error / Cancelled / Paused) or active progress bar
                let rightIndicator = '';
                const pauseSvg = `<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>`;
                const playSvg = `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l12-7z"/></svg>`;

                const revealSvg = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>`;

                const retrySvg = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg>`;

                if (v.status === 'Done') {
                    const hasFile = !!v.filepath;
                    rightIndicator = `
                        <span class="pd-video-status s-done">Done</span>
                        ${hasFile ? `<button class="status-icon-btn" onclick="event.stopPropagation(); revealVideoFile('${v.id}', app.currentPlaylistId)" data-tip-right="Show in folder">${revealSvg}</button>` : ''}
                    `;
                } else if (!v.status && inLibrary) {
                    // Video was downloaded in a previous session and is in the library.
                    // No active status in this session — show a library badge so the
                    // user knows it's already been saved without having to leave this view.
                    rightIndicator = `<span class="pd-video-status s-done" data-tip="Already in your library">In library</span>`;
                } else if (v.status === 'Error') {
                    const cat = v.errorCategory || 'generic';
                    const msg = (v.errorMessage || 'Download failed.').replace(/"/g, '&quot;');
                    const label = errorCategoryLabel(cat);
                    rightIndicator = `
                        <span class="pd-video-status s-error" data-tip="${msg}" style="cursor: help;">${label}</span>
                        <button class="status-icon-btn retry-btn" onclick="event.stopPropagation(); retryVideoDownload('${v.id}', app.currentPlaylistId)" data-tip-right="Retry download">${retrySvg}</button>
                    `;
                } else if (v.status === 'Retrying') {
                    rightIndicator = `
                        <span class="pd-video-status s-retrying">
                            <span class="retry-dot"></span>Retrying
                        </span>
                    `;
                } else if (v.status === 'Cancelled') {
                    rightIndicator = `
                        <span class="pd-video-status s-cancelled">Cancelled</span>
                        <button class="pause-btn is-paused" data-tip-right="Resume download" onclick="event.stopPropagation(); app.resumePlaylistVideo('${v.id}')">${playSvg}</button>
                    `;
                } else if (v.status === 'Paused') {
                    rightIndicator = `
                        <span class="pd-video-status s-cancelled">Paused</span>
                        <button class="pause-btn is-paused" data-tip-right="Resume download" onclick="event.stopPropagation(); app.resumePlaylistVideo('${v.id}')">${playSvg}</button>
                    `;
                } else if (v.status === 'Downloading') {
                    const pct = typeof v.progressPct === 'number' ? v.progressPct : 0;
                    const speed = v.progressSpeed || '';
                    rightIndicator = `
                        <div class="pd-video-progress">
                            <div class="pd-video-progress-text">${Math.floor(pct)}% · ${speed}</div>
                            <div class="pd-video-progress-bar-bg">
                                <div class="pd-video-progress-bar" style="width: ${pct}%;"></div>
                            </div>
                        </div>
                        <button class="pause-btn" data-tip-right="Pause download" onclick="event.stopPropagation(); app.pausePlaylistVideo('${v.id}')">${pauseSvg}</button>
                    `;
                }

                // XSS guard. Title/uploader come straight from yt-dlp and need
                // escaping; thumbnail src needs escaping AND a scheme check
                // (otherwise a malicious entry could embed `javascript:` and the
                // browser would happily fire it on click handlers / focus).
                const eTitle = this.escapeHtml(v.title || '');
                const eUploader = this.escapeHtml(v.uploader || '');
                const eId = this.escapeHtml(v.id || '');
                const eDuration = this.escapeHtml(v.duration_string || '');
                const safeThumb = (v.thumbnail && /^(?:https?:|pt:thumb:|data:image\/)/i.test(v.thumbnail))
                    ? this.escapeHtml(v.thumbnail) : '';

                return `
                    <div class="pd-video-row ${picked ? 'picked' : 'unpicked'}" id="pd-v-${eId}" onclick="app.togglePlaylistVideo('${eId}')">
                        <span class="pd-video-num">${idx + 1}</span>
                        <div class="pd-video-check">${checkSvg}</div>
                        <div class="pd-video-thumb">
                            ${safeThumb ? `<img src="${safeThumb}" alt="">` : ''}
                            <div class="pd-video-thumb-time">${eDuration}</div>
                        </div>
                        <div class="pd-video-info">
                            <div class="pd-video-title" data-tip-wrap="${eTitle}">${eTitle}</div>
                            <div class="pd-video-sub">${eUploader} ${subRight ? '· ' + subRight : ''}</div>
                        </div>
                        ${rightIndicator}
                    </div>
                `;
            },

            buildDefaultQualityMenuHTML(pl) {
                const options = ['2160p', '1440p', '1080p', '720p', '480p', '360p'];
                const hintFor = (l) => l === '2160p' ? '4K' : l === '1440p' ? '2K' : '';
                const checkIcon = `<svg class="check-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M5 13l4 4L19 7"/></svg>`;

                const items = options.map(q => {
                    const sel = q === pl.defaultQuality;
                    return `
                        <div class="quality-option ${sel ? 'selected' : ''}" onclick="event.stopPropagation(); pickPlaylistDefaultQuality('${pl.id}', '${q}')">
                            <span class="opt-label">${q}</span>
                            <span class="opt-hint">${hintFor(q)}</span>
                            ${sel ? checkIcon : ''}
                        </div>
                    `;
                }).join('');

                const audioSel = pl.defaultQuality === 'Audio';
                return items + `
                    <div class="quality-menu-divider"></div>
                    <div class="quality-option ${audioSel ? 'selected' : ''}" onclick="event.stopPropagation(); pickPlaylistDefaultQuality('${pl.id}', 'Audio')">
                        <span class="opt-label">
                            <svg width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M9 19V6l12-3v13M9 19a3 3 0 11-6 0 3 3 0 016 0zm12-3a3 3 0 11-6 0 3 3 0 016 0z"/></svg>
                            Audio only
                        </span>
                        ${audioSel ? checkIcon : ''}
                    </div>
                `;
            },

            getSelectAllIconHTML(picked, total) {
                const hollow = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="9"/></svg>`;
                const partial = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4.5" fill="currentColor" stroke="none"/></svg>`;
                const check = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="9"/><path d="M8 12l3 3 5-6"/></svg>`;
                if (picked === 0) return hollow;
                if (picked === total) return check;
                return partial;
            },

            getSelectAllLabel(picked, total) {
                if (picked === 0) return 'Select all';
                if (picked === total) return 'All selected';
                return `${picked} of ${total} selected`;
            },

            togglePlaylistVideo(videoId) {
                if (!this.currentPlaylistId) return;
                const pl = this.videosInQueue.find(i => i.type === 'playlist' && i.id === this.currentPlaylistId);
                if (!pl) return;
                const v = pl.videos.find(x => x.id === videoId);
                if (!v) return;
                // Don't allow toggling while formats are still resolving — the
                // card's .resolving class disables pointer-events anyway, but
                // belt-and-braces here in case the click slipped through.
                if (!v.formats) return;
                // Downloaded videos are locked from re-selection in channel
                // mode. Single-click is a no-op; double-click plays the file
                // (wired via ondblclick on the card).
                const isChannel = classifyPlaylistEntry(pl) === 'channel';
                if (isChannel) {
                    if (v.status === 'Done') return;
                    if (this.videosInLibrary) {
                        for (const entry of this.videosInLibrary) {
                            if (entry.type === 'playlist') {
                                if ((entry.videos || []).some(c => c.id === v.id && !c.missing)) return;
                            } else if (entry.id === v.id && !entry.missing) {
                                return;
                            }
                        }
                    }
                }
                // Channels start with everything explicitly unpicked, so "picked"
                // means v.selected === true. Playlists default to picked
                // (undefined ≈ picked), so "picked" means v.selected !== false.
                // We branch so toggling always reads the same way as the render.
                const isChan = classifyPlaylistEntry(pl) === 'channel';
                const wasPicked = isChan ? (v.selected === true) : (v.selected !== false);
                v.selected = !wasPicked;

                const row = document.getElementById(`pd-v-${videoId}`);
                if (row) {
                    if (isChan) {
                        // Channel cards are .library-card and use the library's
                        // selection class — toggling .picked/.unpicked here was
                        // a no-op (those styles only exist for .pd-video-row).
                        row.classList.toggle('is-selected', v.selected === true);
                    } else {
                        row.classList.toggle('picked', v.selected);
                        row.classList.toggle('unpicked', !v.selected);
                    }
                }
                this.refreshPdHeaderCounts();
                if (isChan) this.refreshPdChannelActionBar();
                this.saveQueueState();
            },

            // Channel-mode bottom action bar — visibility + count + quality
            // label. Called after every selection change and after a quality
            // change. Cheap; no full re-render.
            refreshPdChannelActionBar() {
                const pl = this.videosInQueue.find(i => i.type === 'playlist' && i.id === this.currentPlaylistId);
                if (!pl) return;
                const bar = document.getElementById('pd-channel-action-bar');
                if (!bar) return;
                const picked = pl.videos.filter(v => v.selected === true).length;
                const countEl = document.getElementById('pd-channel-action-count');
                const goBtn = document.getElementById('pd-channel-action-go');
                const qLabel = document.getElementById('pd-channel-action-quality-label');
                if (countEl) countEl.textContent = `${picked} selected`;
                if (goBtn) {
                    goBtn.textContent = picked === 1 ? 'Download 1' : `Download ${picked}`;
                    goBtn.disabled = picked === 0;
                }
                if (qLabel) qLabel.textContent = pl.defaultQuality || '';
                bar.classList.toggle('visible', picked > 0);
            },

            clearChannelSelection() {
                const pl = this.videosInQueue.find(i => i.type === 'playlist' && i.id === this.currentPlaylistId);
                if (!pl) return;
                pl.videos.forEach(v => {
                    if (v.selected !== true) return;
                    v.selected = false;
                    const row = document.getElementById(`pd-v-${v.id}`);
                    if (row) {
                        // Channel cards use .is-selected (library atom); old
                        // playlist rows use .picked. Strip both so the call
                        // is safe to run against either renderer.
                        row.classList.remove('is-selected');
                        row.classList.remove('picked');
                    }
                });
                this.refreshPdHeaderCounts();
                this.refreshPdChannelActionBar();
                this.saveQueueState();
            },

            togglePdChannelQualityMenu() {
                const trigger = document.getElementById('pd-channel-action-quality');
                if (!trigger) return;
                const wasOpen = trigger.classList.contains('open');
                trigger.classList.toggle('open');
                // Outside-click dismiss — bound only while the menu is open.
                if (!wasOpen) {
                    const dismiss = (e) => {
                        if (trigger.contains(e.target)) return;
                        trigger.classList.remove('open');
                        document.removeEventListener('click', dismiss, true);
                    };
                    setTimeout(() => document.addEventListener('click', dismiss, true), 0);
                }
            },

            togglePlaylistSelectAll() {
                if (!this.currentPlaylistId) return;
                const pl = this.videosInQueue.find(i => i.type === 'playlist' && i.id === this.currentPlaylistId);
                if (!pl) return;
                const picked = pl.videos.filter(v => v.selected !== false).length;
                const newState = picked < pl.videoCount;  // if not all picked, pick all; else unpick all
                pl.videos.forEach(v => { v.selected = newState; });
                // Update DOM without full re-render
                pl.videos.forEach(v => {
                    const row = document.getElementById(`pd-v-${v.id}`);
                    if (row) {
                        row.classList.toggle('picked', newState);
                        row.classList.toggle('unpicked', !newState);
                    }
                });
                this.refreshPdHeaderCounts();
                this.saveQueueState();
            },

            refreshPdHeaderCounts() {
                const pl = this.videosInQueue.find(i => i.type === 'playlist' && i.id === this.currentPlaylistId);
                if (!pl) return;
                const picked = pl.videos.filter(v => v.selected !== false).length;
                const pill = document.getElementById('pd-picked-pill');
                if (pill) pill.textContent = `${picked} of ${pl.videoCount} picked`;
                const saIcon = document.getElementById('pd-sa-icon');
                if (saIcon) saIcon.innerHTML = this.getSelectAllIconHTML(picked, pl.videoCount);
                const saLabel = document.getElementById('pd-sa-label');
                if (saLabel) saLabel.textContent = this.getSelectAllLabel(picked, pl.videoCount);
                const dlBtn = document.getElementById('pd-download-btn');
                if (dlBtn) {
                    dlBtn.textContent = `Download ${picked}`;
                    dlBtn.disabled = picked === 0;
                }
            },

            downloadPlaylist() {
                if (!this.currentPlaylistId) return;
                const pl = this.videosInQueue.find(i => i.type === 'playlist' && i.id === this.currentPlaylistId);
                if (!pl) return;

                // Kick off download — stay in the detail view so the user can
                // watch per-video progress rows update in real time. The cancel
                // button becomes accessible from the queue view when they close.
                pywebview.api.start_download([pl], 'browser', 'none');
                this.elements.downloadButton.classList.add('hidden');
                this.elements.cancelButton.classList.remove('hidden');

                // Channel mode: clear `selected` on the videos we just kicked
                // off. The cards now express their state via the downloading
                // pill + progress bar, not the blue selection outline — and
                // the bottom action bar should hide since "0 selected".
                if (classifyPlaylistEntry(pl) === 'channel') {
                    pl.videos.forEach(v => {
                        if (v.selected === true) {
                            v.selected = false;
                            const row = document.getElementById(`pd-v-${v.id}`);
                            if (row) row.classList.remove('is-selected');
                        }
                    });
                    this.refreshPdChannelActionBar();
                    this.saveQueueState();
                }
            },

            pausePlaylistVideo(videoId) {
                pywebview.api.pause_download(videoId);
                // Status will come back via updateItemStatus → 'Paused'. No optimistic UI needed.
                // Find title for the toast
                if (!this.currentPlaylistId) return;
                const pl = this.videosInQueue.find(i => i.type === 'playlist' && i.id === this.currentPlaylistId);
                const v = pl?.videos.find(x => x.id === videoId);
                const title = v ? v.title : 'Download';
                const short = title.length > 48 ? title.slice(0, 45) + '…' : title;
                showToast(`Paused "${short}"`, null, null);
            },

            resumePlaylistVideo(videoId) {
                if (!this.currentPlaylistId) return;
                const pl = this.videosInQueue.find(i => i.type === 'playlist' && i.id === this.currentPlaylistId);
                if (!pl) return;
                const v = pl.videos.find(x => x.id === videoId);
                if (!v) return;
                // Build the same shape start_download expects (enriched with playlist context)
                const enriched = {
                    ...v,
                    selectedQuality: v.selectedQuality || pl.defaultQuality,
                    isFromPlaylist: true,
                    playlistTitle: pl.title,
                    playlistId: pl.id,
                };
                pywebview.api.restart_download(enriched, 'browser', 'none');
                // Make sure the cancel button is visible since something is now active
                this.elements.downloadButton.classList.add('hidden');
                this.elements.cancelButton.classList.remove('hidden');
            },

            // Called by backend when a video's formats have been resolved
            onVideoFormatsResolved(playlistId, payload) {
                const pl = this.videosInQueue.find(i => i.type === 'playlist' && i.id === playlistId);
                if (!pl) return;
                const v = pl.videos.find(x => x.id === payload.id);
                if (!v) return;
                v.formats = payload.formats;
                v.sizeMap = payload.sizeMap;
                v.selectedQuality = v.selectedQuality || pl.defaultQuality;
                if (payload.uploader) v.uploader = payload.uploader;
                if (payload.thumbnail) v.thumbnail = payload.thumbnail;
                if (payload.duration_string) v.duration_string = payload.duration_string;
                if (payload.view_count_string) v.view_count_string = payload.view_count_string;
                if (payload.published_time) v.published_time = payload.published_time;

                // Re-render just this row if we're on the detail view
                if (this.currentPlaylistId === playlistId) {
                    const row = document.getElementById(`pd-v-${v.id}`);
                    if (row) {
                        const idx = pl.videos.indexOf(v);
                        const isChan = classifyPlaylistEntry(pl) === 'channel';
                        row.outerHTML = isChan
                            ? this.createPdChannelCardHTML(v, idx)
                            : this.createPdVideoRowHTML(v, idx);
                    }
                }
                this.saveQueueState();
            },

            onVideoFormatsFailed(playlistId, videoId, errMsg) {
                const pl = this.videosInQueue.find(i => i.type === 'playlist' && i.id === playlistId);
                if (!pl) return;
                const v = pl.videos.find(x => x.id === videoId);
                if (!v) return;
                v.formats = [];  // empty array = resolution attempted, failed
                v.selected = false;  // auto-unpick failed videos

                if (this.currentPlaylistId === playlistId) {
                    const row = document.getElementById(`pd-v-${v.id}`);
                    if (row) {
                        const idx = pl.videos.indexOf(v);
                        const isChan = classifyPlaylistEntry(pl) === 'channel';
                        row.outerHTML = isChan
                            ? this.createPdChannelCardHTML(v, idx)
                            : this.createPdVideoRowHTML(v, idx);
                    }
                    this.refreshPdHeaderCounts();
                }
            },

            onPlaylistFormatsComplete(playlistId) {
                const pl = this.videosInQueue.find(i => i.type === 'playlist' && i.id === playlistId);
                if (!pl) return;
                pl.formatsResolved = true;
                this.saveQueueState();

                // "Check for updates" temp playlists carry _update_target_id and need
                // to start downloading automatically once formats resolve — the user
                // already picked which videos in the updates modal, no further input
                // expected. This is the auto-kick that ties the modal to the merge.
                if (pl._update_target_id) {
                    pywebview.api.start_download([pl], 'browser', 'none');
                }
            },

            // Returns a flat list of all videos (playlist children expanded, only picked ones)
            getFlatVideoList() {
                const flat = [];
                this.videosInQueue.forEach(item => {
                    if (item.type === 'playlist') {
                        (item.videos || []).forEach(child => {
                            if (child.selected !== false) flat.push(child);
                        });
                    } else {
                        flat.push(item);
                    }
                });
                return flat;
            },

            // Recompute aggregate progress for a playlist card and update its DOM
            updatePlaylistCardRollup(playlistId) {
                const pl = this.videosInQueue.find(i => i.type === 'playlist' && i.id === playlistId);
                if (!pl) return;

                const picked = pl.videos.filter(v => v.selected !== false);
                const totalCount = picked.length;
                if (totalCount === 0) return;

                const done = picked.filter(v => v.status === 'Done').length;
                const downloading = picked.filter(v => v.status === 'Downloading').length;
                const queued = picked.filter(v => v.status === 'Queued').length;
                // Show progress wrap as soon as anything has started — 'Queued'
                // means the backend has picked up the batch even if semaphore
                // hasn't released the video yet. Without this, the count label
                // stays hidden while videos sit in the semaphore queue.
                const anyActive = downloading > 0 || done > 0 || queued > 0;

                // Compute aggregate percentage: sum of (per-video pct) / totalCount
                let totalPct = 0;
                picked.forEach(v => {
                    if (v.status === 'Done') totalPct += 100;
                    else if (v.status === 'Downloading' && typeof v.progressPct === 'number') totalPct += v.progressPct;
                });
                const aggPct = totalPct / totalCount;

                // Find the playlist card and update (or hide) its progress area
                const card = document.getElementById(`item-${playlistId}`);
                if (!card) return;
                const wrap = card.querySelector('.playlist-progress-wrap');
                if (!wrap) return;

                if (anyActive) {
                    wrap.classList.add('visible');
                    const label = wrap.querySelector('.pl-prog-label');
                    const bar = wrap.querySelector('.playlist-progress-bar');
                    if (label) {
                        if (done === totalCount) {
                            label.textContent = `${done} of ${totalCount} done`;
                        } else if (downloading > 0) {
                            label.textContent = `${downloading} of ${totalCount} downloading…`;
                        } else if (queued > 0 && done === 0) {
                            label.textContent = `Starting ${totalCount} video${totalCount === 1 ? '' : 's'}…`;
                        } else {
                            label.textContent = `${done} of ${totalCount} done`;
                        }
                    }
                    if (bar) bar.style.width = aggPct + '%';
                } else {
                    wrap.classList.remove('visible');
                }
            },

            // Rebuild the DOM for a single detail-view video row to reflect new status/progress
            updatePdVideoStatusInDom(videoId, status) {
                if (!this.currentPlaylistId) return;
                const pl = this.videosInQueue.find(i => i.type === 'playlist' && i.id === this.currentPlaylistId);
                if (!pl) return;
                const v = pl.videos.find(x => x.id === videoId);
                if (!v) return;
                const row = document.getElementById(`pd-v-${videoId}`);
                if (!row) return;
                const idx = pl.videos.indexOf(v);
                const isChan = classifyPlaylistEntry(pl) === 'channel';
                row.outerHTML = isChan
                    ? this.createPdChannelCardHTML(v, idx)
                    : this.createPdVideoRowHTML(v, idx);
            },

            createVideoItemHTML(v) {
                const isSelected = this.selectedVideoIds.has(v.id);
                const currentQuality = v.selectedQuality || v.formats?.[0]?.label || 'N/A';
                const size = v.formats?.find(f => f.label === currentQuality)?.filesize_string || "N/A";
                const hasActiveExtra = v.downloadThumbnail || v.downloadSubtitles;

                const qualityMenuHTML = this.buildQualityMenuHTML(v, currentQuality);

                // Resolve thumbnail through the same cache path as library cards.
                // 'pt:thumb:' markers go through the backend resolver; remote URLs
                // load directly. The data-item-id on the row enables surgical
                // updates from updateItemThumbnail() when caching completes.
                const cache = this._thumbCache || {};
                const resolveThumb = (url) => {
                    if (!url) return '';
                    if (url.startsWith('pt:thumb:')) {
                        if (cache[url]) return `src="${cache[url]}"`;
                        return `data-thumb-marker="${url}"`;
                    }
                    return `src="${url}" data-remote-thumb="${url}" data-thumb-item-id="${v.id}"`;
                };

                // XSS guard: every yt-dlp string field gets HTML-escaped before
                // it touches the template. See createPlaylistRowHTML for rationale.
                const eTitle = this.escapeHtml(v.title || '');
                const eUploader = this.escapeHtml(v.uploader || '');
                const eId = this.escapeHtml(v.id || '');
                const eDuration = this.escapeHtml(v.duration_string || '');
                return `
                    <div id="item-${eId}" data-item-id="${eId}" class="video-row ${isSelected ? 'selected' : ''}" onclick="toggleCardSelection(this, event)" ondblclick="openVideoFromCard(this)">
                        <div class="drag-handle">
                            <svg fill="currentColor" viewBox="0 0 20 20">
                                <path d="M10 6a2 2 0 110-4 2 2 0 010 4zM10 12a2 2 0 110-4 2 2 0 010 4zM10 18a2 2 0 110-4 2 2 0 010 4z"/>
                            </svg>
                        </div>
                        <input type="checkbox" class="video-checkbox" onchange="app.updateSelection()" data-id="${eId}" ${isSelected ? 'checked' : ''}>
                        <div class="video-thumb">
                            ${v.thumbnail ? `<img ${resolveThumb(v.thumbnail)} alt="${eTitle}">` : ''}
                            <div class="video-time">${eDuration}</div>
                        </div>
                        <div class="video-details">
                            <div class="video-heading" data-tip-wrap="${eTitle}">${eTitle}</div>
                            <div class="video-meta-line">${eUploader} · <span class="item-size">${size}</span></div>
                        </div>
                        <div class="quality-picker" id="qpicker-${v.id}" onclick="event.stopPropagation()">
                            <button class="quality-trigger" onclick="toggleQualityMenu(event, '${v.id}')">
                                <span class="qtrig-label">${currentQuality}</span>
                                <svg class="caret" viewBox="0 0 12 8" fill="none"><path d="M1 1.5L6 6.5L11 1.5" stroke="#737373" stroke-width="2" stroke-linecap="round"/></svg>
                            </button>
                            <div class="quality-menu">${qualityMenuHTML}</div>
                        </div>
                        <div class="extras-wrapper" onclick="event.stopPropagation()">
                            <button class="extras-trigger ${hasActiveExtra ? 'has-active' : ''}" onclick="toggleExtrasMenu(event, '${v.id}')" data-tip="More options">⋯</button>
                            <div class="extras-menu" id="extras-${v.id}">
                                <button class="extra-toggle ${v.downloadThumbnail ? 'active' : ''}" onclick="toggleExtra(this, '${v.id}', 'thumb')">
                                    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                        <path d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" stroke-width="2"/>
                                    </svg>
                                    <span>Thumbnail</span>
                                </button>
                                <!-- Subtitle toggle removed — subs are auto-downloaded for every
                                     video now (logic.py adds writesubtitles + writeautomaticsub
                                     unconditionally). Player has its own CC button to toggle
                                     display on/off per-session. -->
                            </div>
                        </div>
                        <div class="video-status item-status-container"></div>
                        <button class="remove-btn" onclick="event.stopPropagation(); app.removeItem('${v.id}')" data-tip-right="Remove">
                            <svg fill="currentColor" viewBox="0 0 20 20">
                                <path d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z"/>
                                </svg>
                        </button>
                    </div>
                `;
            },

            buildQualityMenuHTML(v, currentQuality) {
                if (!v.formats || v.formats.length === 0) {
                    return `<div class="quality-option" style="color: #525252; cursor: default;">No qualities</div>`;
                }
                const checkIcon = `<svg class="check-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M5 13l4 4L19 7"/></svg>`;
                const hintFor = (label) => {
                    if (label === '2160p') return '4K';
                    if (label === '1440p') return '2K';
                    return '';
                };

                const videoOpts = v.formats.map(f => {
                    const isSel = f.label === currentQuality;
                    const hint = hintFor(f.label);
                    return `
                        <div class="quality-option ${isSel ? 'selected' : ''}" onclick="pickQuality('${v.id}', '${f.label}')">
                            <span class="opt-label">${f.label}</span>
                            <span class="opt-hint">${hint || ''}</span>
                            ${isSel ? checkIcon : ''}
                        </div>
                    `;
                }).join('');

                const audioIsSel = currentQuality === 'Audio';
                const audioOpt = `
                    <div class="quality-menu-divider"></div>
                    <div class="quality-option ${audioIsSel ? 'selected' : ''}" onclick="pickQuality('${v.id}', 'Audio')">
                        <span class="opt-label">
                            <svg width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M9 19V6l12-3v13M9 19a3 3 0 11-6 0 3 3 0 016 0zm12-3a3 3 0 11-6 0 3 3 0 016 0z"/></svg>
                            Audio only
                        </span>
                        ${audioIsSel ? checkIcon : ''}
                    </div>
                `;

                return videoOpts + audioOpt;
            },

            initSortable() {
                // Destroy any previous instance so we don't end up with multiple Sortables on one element
                if (this.sortable) {
                    try { this.sortable.destroy(); } catch(_) {}
                    this.sortable = null;
                }
                // Sortable is bundled inline in our HTML. Defensive guard: if for any
                // reason the global isn't defined (e.g. build stripped it), skip drag
                // reordering instead of throwing and breaking the rest of init.
                if (typeof Sortable === 'undefined') {
                    console.warn('[ProTube] Sortable not loaded — drag-reorder disabled');
                    return;
                }
                // Build a smooth auto-scroller for the queue list. SortableJS has its
                // own auto-scroll built in but it's setInterval-based with binary on/off
                // velocity (-1, 0, +1) — that's why dragging near the edge feels jerky
                // and stalls. We disable it (`scroll: false`) and run a rAF loop with
                // an edge-distance velocity ramp + dt-corrected scroll. Same pattern
                // dnd-kit and react-beautiful-dnd use, just hand-rolled.
                const queueAutoScroll = (() => {
                    const HOT_ZONE = 80;   // px from edge that activates scroll
                    const MAX_SPEED = 16;  // px per 16ms frame at full-throttle
                    const el = this.elements.videoList;
                    let raf = 0;
                    let vy = 0;
                    let lastT = 0;

                    function onMove(e) {
                        const r = el.getBoundingClientRect();
                        const y = e.clientY;
                        const distTop = y - r.top;
                        const distBot = r.bottom - y;
                        if (distTop < HOT_ZONE) {
                            // ease-in: closer to edge = faster (linear is fine, squared felt sluggish)
                            vy = -MAX_SPEED * (1 - Math.max(distTop, 0) / HOT_ZONE);
                        } else if (distBot < HOT_ZONE) {
                            vy = MAX_SPEED * (1 - Math.max(distBot, 0) / HOT_ZONE);
                        } else {
                            vy = 0;
                        }
                    }
                    function tick(t) {
                        const dt = lastT ? (t - lastT) : 16;
                        lastT = t;
                        // dt/16 keeps speed constant regardless of frame rate / dropped frames.
                        // This is the buttery trick — without it, dropped frames cause stalls.
                        if (vy !== 0) el.scrollTop += vy * (dt / 16);
                        raf = requestAnimationFrame(tick);
                    }
                    return {
                        start() {
                            if (raf) return;
                            lastT = 0;
                            vy = 0;
                            // Listen to BOTH event types: dragover fires when Sortable uses
                            // native HTML5 DnD; pointermove fires when forceFallback is on.
                            // Wiring both works regardless of mode.
                            document.addEventListener('pointermove', onMove);
                            document.addEventListener('dragover', onMove);
                            raf = requestAnimationFrame(tick);
                        },
                        stop() {
                            if (raf) cancelAnimationFrame(raf);
                            raf = 0;
                            vy = 0;
                            lastT = 0;
                            document.removeEventListener('pointermove', onMove);
                            document.removeEventListener('dragover', onMove);
                        },
                    };
                })();

                this.sortable = new Sortable(this.elements.videoList, {
                    animation: 150,
                    handle: '.drag-handle',
                    ghostClass: 'sortable-ghost',
                    scroll: false,  // we own auto-scroll via the rAF loop above
                    onStart: () => queueAutoScroll.start(),
                    onUnchoose: () => queueAutoScroll.stop(),
                    onEnd: (evt) => {
                        queueAutoScroll.stop();
                        // oldIndex / newIndex are indices into the currently rendered (possibly filtered) list.
                        // Map them back to absolute indices in videosInQueue by looking up the moved node's id.
                        const rows = Array.from(this.elements.videoList.children);
                        // After the move, the rows array already reflects the new DOM order.
                        // Rebuild videosInQueue to match that order, preserving unfiltered items in their original relative positions.
                        const idToItem = new Map(this.videosInQueue.map(it => [it.id, it]));
                        const visibleIdsInNewOrder = rows.map(r => r.id.replace(/^item-/, ''));

                        // Start with items that are NOT currently visible (they keep their relative order).
                        const hiddenItems = this.videosInQueue.filter(it => !visibleIdsInNewOrder.includes(it.id));

                        // Then interleave: for each slot in videosInQueue, if it's a visible item, use the NEXT
                        // visible id in the new order; if it's hidden, keep it where it is.
                        const newQueue = [];
                        let visibleCursor = 0;
                        const hiddenMap = new Map();
                        hiddenItems.forEach(it => hiddenMap.set(it.id, it));
                        this.videosInQueue.forEach(it => {
                            if (hiddenMap.has(it.id)) {
                                newQueue.push(it);
                            } else {
                                // consume next visible-in-new-order
                                const nextId = visibleIdsInNewOrder[visibleCursor++];
                                newQueue.push(idToItem.get(nextId));
                            }
                        });

                        this.videosInQueue = newQueue;
                        this.saveQueueState();
                    }
                });
            },

            updateDashboard() {
                // Count items at top-level (what filter chips display). Playlists count as 1.
                const totalItems = this.videosInQueue.length;

                // Counts feed the chip badges. Type counts (videos/playlists/channels)
                // drive the new chips; failed is the only status count we still surface.
                let videos = 0, playlists = 0, channels = 0, failed = 0;
                this.videosInQueue.forEach(item => {
                    if (item.type === 'video') {
                        videos++;
                    } else if (item.type === 'playlist') {
                        if (classifyPlaylistEntry(item) === 'channel') channels++;
                        else playlists++;
                    }
                    if (this.classifyItem(item) === 'failed') failed++;
                });

                if (this.elements.countAll) this.elements.countAll.textContent = totalItems;
                if (this.elements.countVideos) this.elements.countVideos.textContent = videos;
                if (this.elements.countPlaylists) this.elements.countPlaylists.textContent = playlists;
                if (this.elements.countChannels) this.elements.countChannels.textContent = channels;
                if (this.elements.countFailed) this.elements.countFailed.textContent = failed;
                if (this.elements.railQueueBadge) this.elements.railQueueBadge.textContent = totalItems;

                // Aggregate size + live speed. Flatten playlist children for size/speed calc.
                const flat = [];
                this.videosInQueue.forEach(item => {
                    if (item.type === 'playlist') {
                        (item.videos || []).forEach(child => {
                            if (child.selected !== false) flat.push(child);
                        });
                    } else {
                        flat.push(item);
                    }
                });

                let totalBytes = 0;
                let liveSpeedBytes = 0;
                flat.forEach(v => {
                    if (v.sizeMap) {
                        const q = v.selectedQuality || (v.formats ? v.formats[0]?.label : null);
                        if (q && v.sizeMap[q]) totalBytes += v.sizeMap[q];
                    }
                    if (v.status === 'Downloading' && typeof v.progressSpeedBytes === 'number') {
                        liveSpeedBytes += v.progressSpeedBytes;
                    }
                });

                if (this.elements.filterStats) {
                    const sizeStr = totalBytes > 0 ? formatBytes(totalBytes) : '';
                    const speedStr = liveSpeedBytes > 0 ? `${formatBytes(liveSpeedBytes)}/s` : '';
                    this.elements.filterStats.textContent = [sizeStr, speedStr].filter(Boolean).join(' · ') || '—';
                }

                // Download button count & state
                this.elements.downloadButton.textContent = `Download ${this.selectedVideoIds.size}`;
                this.elements.downloadButton.disabled = this.selectedVideoIds.size === 0;
            },

            updateSelection() {
                this.selectedVideoIds.clear();
                document.querySelectorAll('.video-checkbox:checked').forEach(cb => {
                    this.selectedVideoIds.add(cb.dataset.id);
                });
                // Sync .selected class on all queue rows (videos and playlists) based on checkbox state
                document.querySelectorAll('.video-row, .playlist-row').forEach(row => {
                    const cb = row.querySelector('.video-checkbox');
                    if (!cb) return;
                    row.classList.toggle('selected', cb.checked);
                });
                this.updateSelectAllPillState();
                this.updateDashboard();
            },

            updateSelectAllPillState() {
                const pill = this.elements.selectAllPill;
                const label = this.elements.saLabel;
                const iconWrap = this.elements.saIcon;
                const clearBtn = this.elements.clearSelectionBtn;
                if (!pill) return;

                const total = this.videosInQueue.length;
                const sel = this.selectedVideoIds.size;

                const hollow = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="9"/></svg>`;
                const partial = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4.5" fill="currentColor" stroke="none"/></svg>`;
                const check = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="9"/><path d="M8 12l3 3 5-6"/></svg>`;

                pill.classList.remove('some-selected', 'all-selected');

                if (sel === 0) {
                    iconWrap.innerHTML = hollow;
                    label.textContent = 'Select all';
                    clearBtn.classList.add('hidden');
                } else if (sel === total && total > 0) {
                    iconWrap.innerHTML = check;
                    label.textContent = 'All selected';
                    pill.classList.add('all-selected');
                    clearBtn.classList.remove('hidden');
                } else {
                    iconWrap.innerHTML = partial;
                    label.textContent = `${sel} of ${total} selected`;
                    pill.classList.add('some-selected');
                    clearBtn.classList.remove('hidden');
                }
            },

            // Smart toggle: if anything is selected → clear; if nothing selected → select all
            toggleSelectAllSmart() {
                if (this.selectedVideoIds.size > 0) {
                    this.clearSelection();
                } else {
                    document.querySelectorAll('.video-checkbox').forEach(cb => cb.checked = true);
                    document.querySelectorAll('.video-row').forEach(row => row.classList.add('selected'));
                    this.updateSelection();
                }
            },

            clearSelection() {
                document.querySelectorAll('.video-checkbox').forEach(cb => cb.checked = false);
                document.querySelectorAll('.video-row.selected').forEach(row => row.classList.remove('selected'));
                this.updateSelection();
            },

            removeItem(id) {
                const video = this.videosInQueue.find(v => v.id === id);
                if (!video) return;

                // Cancel any in-flight downloads belonging to this entry BEFORE
                // we drop it from local state. Without this, a Downloading or
                // Retrying item keeps churning network and disk in the
                // background even after the card disappears, and leaves an
                // orphan .part file behind. cancel_download is a no-op for
                // items that aren't actually active, so it's safe to fire on
                // every child; we don't have to filter.
                const idsToCancel = [];
                if (video.type === 'playlist') {
                    (video.videos || []).forEach(c => {
                        if (c.id) idsToCancel.push(c.id);
                    });
                } else {
                    idsToCancel.push(id);
                }
                for (const cid of idsToCancel) {
                    try { pywebview.api.cancel_download(cid); } catch (_) {}
                }

                // Store removed video for undo
                removedVideos.push({
                    id: id,
                    video: video,
                    index: this.videosInQueue.indexOf(video)
                });

                // Remove from queue
                this.videosInQueue = this.videosInQueue.filter(v => v.id !== id);
                this.renderQueue();
                this.saveQueueState();
                
                // Show/update toast
                const count = removedVideos.length;
                const message = count === 1 
                    ? 'Video removed from queue' 
                    : `${count} videos removed from queue`;
                
                showToast(message, 'Undo', () => {
                    // Restore all removed videos
                    removedVideos.forEach(item => {
                        app.videosInQueue.splice(item.index, 0, item.video);
                    });
                    app.videosInQueue.sort((a, b) => a.order - b.order);
                    app.renderQueue();
                    app.saveQueueState();
                });
            },

            saveQueueState() {
                // Never persist transient preview channels (read-only browse).
                pywebview.api.save_queue(this.videosInQueue.filter(v => !v.isPreview));
            },

            clearAll() {
                this.videosInQueue = [];
                this.selectedVideoIds.clear();
                pywebview.api.save_queue([]);
                this.elements.mainUrlInput.value = '';
                this.elements.downloadButton.classList.remove('hidden');
                this.elements.cancelButton.classList.add('hidden');
                this.renderQueue();
                this.updateSelectAllPillState();
            }
        };

