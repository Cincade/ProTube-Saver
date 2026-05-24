        // F10 — Music view (Spotify-shaped surface for audio-only downloads).
        // ==========================================================================
        let _musicBound = false;
        let _musicState = {
            library: [],
            albums: [],              // music album entities (parallel to library)
            currentAlbumId: null,    // album currently shown in the detail view
            sub: 'library',          // 'library' | 'search'
            searchKind: 'songs',
            searchToken: 0,
            loading: false,
            downloading: new Set(),  // video IDs currently downloading
        };
        // Where the back chevron in the full music player should return to.
        // Captured at the moment we switch INTO music-player so we can restore
        // album detail / library / wherever the user was. Defaults to 'music'
        // (library tab) when origin isn't a valid return target (e.g. they hit
        // play from a search row on a non-music view).
        let _musicPlayerReturnTo = 'music';
        // Player state. `volume` is the LINEAR slider position (0..1); audio.volume
        // gets a perceptual log curve (Math.pow(v, 4)) applied on every assign.
        let _musicPlayer = {
            audio: null,
            currentTrack: null,
            playing: false,
            volume: 0.8,
            isSeeking: false,        // true while user drags the seek thumb → suppresses timeupdate paint
            shuffle: false,
            repeat: 'off',           // 'off' | 'all' | 'one'
            shuffleOrder: [],        // shuffled queue of library IDs (consumed front-to-back)
        };

        function _audioVolumeFromSlider(s) {
            // Perceptual log curve — slider 0.5 → ~6% actual gain, which "feels" like half
            const v = Math.max(0, Math.min(1, s));
            return Math.pow(v, 4);
        }

        // --- Music-player keyboard shortcut helpers (used by the listener registered
        //     at the end of _initMusicPlayerView). Module-level so they're reusable
        //     if we ever wire dock-side shortcuts. ---
        let _musicFsActive = false;
        const _IS_MAC = (navigator.platform || '').toLowerCase().includes('mac');
        function _musicToggleFs() {
            // Use a MUSIC-SPECIFIC body class — NOT the video's player-is-fullscreen.
            // Reusing the video class previously caused the music-player view to
            // disappear after pressing F. The music-player-fullscreen class has its
            // own rules that explicitly KEEP the music-player view visible.
            _musicFsActive = !_musicFsActive;
            document.body.classList.toggle('music-player-fullscreen', _musicFsActive);
            // On macOS, skip the pywebview OS-fullscreen toggle — Cocoa's
            // `NSWindow toggleFullScreen:` moves the window to a separate
            // Space and animates, which clashes with our in-view immersive
            // mode (user 2026-05-16). The CSS class alone gives the
            // borderless feel within the existing window; users who want
            // true OS fullscreen can still use the green window button or
            // Cmd+Ctrl+F.
            if (!_IS_MAC) {
                try { pywebview.api.set_fullscreen(_musicFsActive); } catch (_) {}
            }
        }
        function _musicSeekBy(seconds) {
            const a = _musicPlayer.audio;
            if (!a || !a.duration || !isFinite(a.duration)) return;
            a.currentTime = Math.max(0, Math.min(a.duration, a.currentTime + seconds));
            _paintDockProgress();
            _paintFullPlayerProgress();
        }
        function _musicVolStep(delta) {
            const v = Math.max(0, Math.min(1, _musicPlayer.volume + delta));
            _applyMusicVolume(v, true);
        }

        function initMusicView() {
            // Load the library on first mount; refresh on every subsequent activation
            // so newly downloaded tracks appear right away.
            _refreshMusicLibrary();
            if (_musicBound) return;
            _musicBound = true;

            // Hydrate the "show hidden" music-library preference from settings so
            // the toggle's state and the render filter agree from first paint.
            // Default false (hidden cards don't render until the user opts in).
            // Mirrors the video library's window._showHiddenLibrary pattern.
            (async () => {
                try {
                    const v = await pywebview.api.get_setting('show_hidden_music_library');
                    window._showHiddenMusicLibrary = v === true;
                } catch (_) {
                    window._showHiddenMusicLibrary = false;
                }
                const btn = document.getElementById('music-show-hidden-btn');
                if (btn) {
                    btn.classList.toggle('active', !!window._showHiddenMusicLibrary);
                    btn.setAttribute('data-tip', window._showHiddenMusicLibrary ? 'Hide hidden' : 'Show hidden');
                }
                // Re-paint if we already drew with the default (false) before settings landed.
                _renderMusicLibrary();
            })();

            // "Show hidden" toggle — flips the global, re-renders, and persists.
            const showHiddenBtn = document.getElementById('music-show-hidden-btn');
            if (showHiddenBtn) {
                showHiddenBtn.addEventListener('click', () => {
                    const next = !window._showHiddenMusicLibrary;
                    window._showHiddenMusicLibrary = next;
                    showHiddenBtn.classList.toggle('active', next);
                    showHiddenBtn.setAttribute('data-tip', next ? 'Hide hidden' : 'Show hidden');
                    try { pywebview.api.set_setting('show_hidden_music_library', next); } catch (_) {}
                    _renderMusicLibrary();
                });
            }

            // Sub-tab switching
            document.querySelectorAll('.music-subtab').forEach(btn => {
                btn.addEventListener('click', () => {
                    const tab = btn.getAttribute('data-music-tab');
                    if (!tab) return;
                    // Leaving the library pane cancels any active multi-select —
                    // the selection + action bar are library-only and shouldn't
                    // bleed into the Search / Downloads panes.
                    try { if (MusicSelection.isActive()) MusicSelection.exit(); } catch (_) {}
                    _musicState.sub = tab;
                    document.querySelectorAll('.music-subtab').forEach(b => b.classList.toggle('active', b === btn));
                    document.getElementById('music-pane-library')?.classList.toggle('active', tab === 'library');
                    document.getElementById('music-pane-search')?.classList.toggle('active', tab === 'search');
                    document.getElementById('music-pane-downloads')?.classList.toggle('active', tab === 'downloads');
                    if (tab === 'search') {
                        setTimeout(() => document.getElementById('music-search-input')?.focus(), 50);
                        // If no active query, paint the For-You landing
                        const inp = document.getElementById('music-search-input');
                        if (!inp || !inp.value.trim()) _renderMusicForYou();
                    } else if (tab === 'downloads') {
                        _refreshMusicQueue();
                    }
                });
            });

            // Search submission + autocomplete
            const input = document.getElementById('music-search-input');
            const goBtn = document.getElementById('music-search-btn');
            const clearBtn = document.getElementById('music-search-clear');
            const suggBox = document.getElementById('music-search-suggestions');
            const paintClear = () => {
                if (clearBtn) clearBtn.classList.toggle('visible', !!(input && input.value));
            };
            // Music suggestions: reuse the YouTube suggest endpoint. ds=yt returns
            // the same query strings YouTube/YT Music use behind their own boxes.
            let _msSuggTimer = null;
            let _msSuggList = [];
            let _msSuggIdx = -1;
            let _msSuggAbort = 0;   // bumped to invalidate in-flight suggestion fetches
            const hideSugg = () => {
                if (suggBox) suggBox.setAttribute('hidden', '');
                _msSuggIdx = -1;
            };
            const paintSugg = () => {
                if (!suggBox) return;
                if (!_msSuggList.length) { hideSugg(); return; }
                const searchSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>';
                const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
                suggBox.innerHTML = _msSuggList.map((s, i) =>
                    `<div class="sugg-item${i === _msSuggIdx ? ' hover' : ''}" data-ms-idx="${i}">${searchSvg}<span>${esc(s)}</span></div>`
                ).join('');
                suggBox.removeAttribute('hidden');
                suggBox.querySelectorAll('.sugg-item').forEach(el => {
                    el.addEventListener('mousedown', (e) => {
                        e.preventDefault();
                        const idx = parseInt(el.getAttribute('data-ms-idx'), 10);
                        const text = _msSuggList[idx];
                        if (!text || !input) return;
                        input.value = text;
                        paintClear();
                        hideSugg();
                        _runMusicSearch(text, _musicState.searchKind);
                    });
                });
            };
            const fetchSugg = async (q) => {
                const tok = ++_msSuggAbort;
                try {
                    const res = await pywebview.api.search_youtube_suggestions(q);
                    // Stale: a newer keystroke OR a committed search bumped the token
                    // while we awaited the network — don't re-open the dropdown.
                    if (tok !== _msSuggAbort) return;
                    _msSuggList = (res && res.suggestions) || [];
                    _msSuggIdx = -1;
                    paintSugg();
                } catch (_) { _msSuggList = []; hideSugg(); }
            };
            if (input) {
                input.addEventListener('input', () => {
                    paintClear();
                    const q = input.value.trim();
                    if (_msSuggTimer) clearTimeout(_msSuggTimer);
                    if (!q) { _msSuggList = []; hideSugg(); return; }
                    _msSuggTimer = setTimeout(() => fetchSugg(q), 150);
                });
                input.addEventListener('blur', () => setTimeout(hideSugg, 150));
                input.addEventListener('keydown', (e) => {
                    const visible = suggBox && !suggBox.hasAttribute('hidden') && _msSuggList.length;
                    if (e.key === 'ArrowDown' && visible) {
                        e.preventDefault();
                        _msSuggIdx = Math.min(_msSuggIdx + 1, _msSuggList.length - 1);
                        paintSugg();
                    } else if (e.key === 'ArrowUp' && visible) {
                        e.preventDefault();
                        _msSuggIdx = Math.max(_msSuggIdx - 1, -1);
                        paintSugg();
                    } else if (e.key === 'Enter') {
                        e.preventDefault();
                        let q = input.value.trim();
                        if (visible && _msSuggIdx >= 0) {
                            q = _msSuggList[_msSuggIdx];
                            input.value = q;
                            paintClear();
                        }
                        // Cancel pending suggestion fetch + clear list so the
                        // debounced timer can't re-show the dropdown after Enter.
                        if (_msSuggTimer) { clearTimeout(_msSuggTimer); _msSuggTimer = null; }
                        _msSuggAbort++;   // abort any in-flight fetch awaiting network
                        _msSuggList = [];
                        hideSugg();
                        input.blur();
                        if (q) _runMusicSearch(q, _musicState.searchKind);
                    } else if (e.key === 'Escape') {
                        if (visible) { hideSugg(); }
                        else if (input.value) { e.preventDefault(); input.value = ''; paintClear(); }
                    }
                });
            }
            if (goBtn) {
                goBtn.addEventListener('click', () => {
                    const q = (input?.value || '').trim();
                    if (_msSuggTimer) { clearTimeout(_msSuggTimer); _msSuggTimer = null; }
                    _msSuggAbort++;   // abort any in-flight fetch so it can't re-show
                    _msSuggList = [];
                    hideSugg();
                    if (q) _runMusicSearch(q, _musicState.searchKind);
                });
            }
            if (clearBtn) {
                clearBtn.addEventListener('click', () => {
                    if (!input) return;
                    input.value = '';
                    paintClear();
                    _msSuggList = [];
                    hideSugg();
                    // Clear results + repaint For-You landing
                    const results = document.getElementById('music-results');
                    const status = document.getElementById('music-search-status');
                    const meta = document.getElementById('music-results-meta');
                    if (results) results.innerHTML = '';
                    if (status) status.setAttribute('hidden', '');
                    if (meta) meta.textContent = '';
                    _msClearSelection();
                    _renderMusicForYou();
                    input.focus();
                });
            }
            paintClear();
            // Initial paint of the For-You landing (no search yet)
            _renderMusicForYou();
            // Chip filter switching
            document.querySelectorAll('.music-chip').forEach(chip => {
                chip.addEventListener('click', () => {
                    const kind = chip.getAttribute('data-music-kind');
                    if (kind === _musicState.searchKind) return;
                    document.querySelectorAll('.music-chip').forEach(c => c.classList.toggle('active', c === chip));
                    _musicState.searchKind = kind;
                    const q = (input?.value || '').trim();
                    if (q) _runMusicSearch(q, kind);
                });
            });

            // Sort dropdown (placeholder for v1 — just toggles between "Recently added"
            // and "Title" so the affordance feels real. Real sort UI is post-MVP.)
            const sortBtn = document.getElementById('music-lib-sort');
            if (sortBtn) {
                sortBtn.addEventListener('click', () => {
                    const lbl = document.getElementById('music-lib-sort-label');
                    if (!lbl) return;
                    if (lbl.textContent === 'Recently added') {
                        lbl.textContent = 'Title (A-Z)';
                        _musicState.library.sort((a, b) => (a.title || '').localeCompare(b.title || ''));
                    } else if (lbl.textContent === 'Title (A-Z)') {
                        lbl.textContent = 'Artist (A-Z)';
                        _musicState.library.sort((a, b) => (a.artist || '').localeCompare(b.artist || ''));
                    } else {
                        lbl.textContent = 'Recently added';
                        _musicState.library.sort((a, b) => (b.added_at || 0) - (a.added_at || 0));
                    }
                    _renderMusicLibrary();
                });
            }

            // Initialize the audio + dock controls (separate so they only bind once).
            _initMusicDock();
        }

        async function _refreshMusicLibrary() {
            // Load tracks + albums in parallel so first paint includes both.
            try {
                const [lib, albums] = await Promise.all([
                    pywebview.api.load_music_library().catch(() => []),
                    (pywebview.api.load_music_albums
                        ? pywebview.api.load_music_albums().catch(() => [])
                        : Promise.resolve([])),
                ]);
                _musicState.library = Array.isArray(lib) ? lib : [];
                _musicState.library.sort((a, b) => (b.added_at || 0) - (a.added_at || 0));
                _musicState.albums = Array.isArray(albums) ? albums : [];
                _musicState.albums.sort((a, b) => (b.added_at || 0) - (a.added_at || 0));
                _renderMusicLibrary();
            } catch (_) { /* ignore */ }
        }

        function _renderMusicLibrary() {
            const grid = document.getElementById('music-grid');
            const empty = document.getElementById('music-lib-empty');
            const meta = document.getElementById('music-lib-meta');
            if (!grid) return;
            const lib = _musicState.library || [];
            const albums = _musicState.albums || [];
            // Build a set of track IDs that belong to a known album — those are
            // hidden from the top-level grid (they live inside the album detail
            // view instead). Tracks whose album_id doesn't match any known album
            // fall back to being rendered as singles (defensive: orphans shouldn't
            // disappear silently).
            const knownAlbumIds = new Set(albums.map(a => a.id));
            const albumTrackIds = new Set();
            for (const a of albums) {
                for (const tid of (a.track_ids || [])) albumTrackIds.add(tid);
            }
            const singles = lib.filter(t => {
                if (t.album_id && knownAlbumIds.has(t.album_id)) return false;
                if (albumTrackIds.has(t.id)) return false;   // stamped-but-album_id-missing
                return true;
            });
            // Hidden filtering. An album is considered "hidden" when EVERY track
            // it owns has track.hidden === true (empty album → not hidden).
            // When window._showHiddenMusicLibrary is false, hidden cards skip
            // rendering; when true, they render dimmed with a "Hidden" badge.
            const showHidden = !!window._showHiddenMusicLibrary;
            const byId = new Map(lib.map(t => [t.id, t]));
            const albumIsHidden = (a) => {
                const ids = a.track_ids || [];
                if (!ids.length) return false;
                for (const tid of ids) {
                    const t = byId.get(tid);
                    if (!t || !t.hidden) return false;
                }
                return true;
            };
            // Album is "library-ready" when: every track downloaded, cover is
            // a local pt:thumb: marker (extracted from embedded art, not a
            // placeholder/remote URL that might 404), AND artist isn't the
            // 'Unknown Artist' placeholder. Mirrors the video-library policy
            // where items only appear once the download fully completes — no
            // skeleton/placeholder states in the library grid.
            const albumIsLibraryReady = (a) => {
                const total = a.total_tracks || (a.track_ids || []).length || 0;
                const done = a.downloaded_count || 0;
                if (total === 0 || done < total) return false;
                const cover = a.cover_url || '';
                if (!cover.startsWith('pt:thumb:')) return false;
                const artist = (a.artist || '').trim().toLowerCase();
                if (!artist || artist === 'unknown artist' || artist === 'unknown') return false;
                return true;
            };
            const readyAlbums = albums.filter(albumIsLibraryReady);
            const visibleAlbums = showHidden ? readyAlbums : readyAlbums.filter(a => !albumIsHidden(a));
            const visibleSingles = showHidden ? singles : singles.filter(t => !t.hidden);
            if (!visibleAlbums.length && !visibleSingles.length) {
                grid.innerHTML = '';
                if (empty) empty.classList.add('visible');
                if (meta) meta.textContent = '';
                return;
            }
            if (empty) empty.classList.remove('visible');
            // Meta line: count tracks (incl. inside albums) + total size best-effort.
            // Counts reflect the full library (incl. hidden) so the user can see
            // what's there — the toggle only changes which cards render.
            const trackCount = lib.length;
            let totalBytes = 0;
            for (const t of lib) { if (t.filesize) totalBytes += t.filesize; }
            const sizeStr = totalBytes ? _formatBytesShort(totalBytes) : '';
            const albumStr = albums.length ? `${albums.length} album${albums.length === 1 ? '' : 's'} · ` : '';
            if (meta) meta.textContent = `${albumStr}${trackCount} track${trackCount === 1 ? '' : 's'}${sizeStr ? ' · ' + sizeStr : ''}`;

            const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
            const currentId = _musicPlayer.currentTrack?.id || '';
            const nowSec = Date.now() / 1000;

            // Album cover URLs may be local 'pt:thumb:' markers (extracted from
            // embedded MP3 art) or remote URLs. Markers need backend resolution
            // via get_thumbnail_data; emit them as data-thumb-marker placeholders
            // and resolve below — same scheme the video library uses.
            const coverAttrs = (url) => {
                if (!url) return '';
                if (typeof url === 'string' && url.startsWith('pt:thumb:')) {
                    return `data-thumb-marker="${esc(url)}"`;
                }
                return `src="${esc(url)}"`;
            };

            // --- Album cards first ---
            const albumHtml = visibleAlbums.map(a => {
                const cv = coverAttrs(a.cover_url);
                const cover = cv ? `<img ${cv} loading="lazy" alt="" />` : '';
                const total = a.total_tracks || (a.track_ids || []).length || 0;
                const done = a.downloaded_count || 0;
                const isDownloading = a.status === 'downloading' && done < total;
                const isNew = a.added_at && !a.seen_at && (nowSec - a.added_at) < 86400;
                const newPill = isNew ? '<span class="music-card-new">NEW</span>' : '';
                const hidden = albumIsHidden(a);
                const hiddenBadge = hidden ? '<span class="music-card-hidden-badge">Hidden</span>' : '';
                const pct = total > 0 ? Math.round((done / total) * 100) : 0;
                // r=46 in a 100×100 viewBox → C = 2π·46 ≈ 289.03
                const dashOffset = 289.03 * (1 - pct / 100);
                const ring = `
                    <div class="mcd-ring-wrap">
                        <svg viewBox="0 0 100 100">
                            <circle class="ring-bg" cx="50" cy="50" r="46"/>
                            <circle class="ring-fg" cx="50" cy="50" r="46"
                                stroke-dasharray="289.03" stroke-dashoffset="${dashOffset}"/>
                        </svg>
                        <span class="mcd-ring-pct">${pct}%</span>
                    </div>`;
                const trackCountStr = `${total} track${total === 1 ? '' : 's'}`;
                const cls = (isDownloading ? ' album-downloading' : '') + (hidden ? ' is-hidden' : '');
                return `
                    <div class="music-card${cls}" data-album-id="${esc(a.id)}" data-music-card-id="${esc(a.id)}" data-card-kind="album">
                        <div class="music-card-art">
                            ${cover}
                            <div class="music-card-select-mark"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg></div>
                            ${newPill}
                            ${hiddenBadge}
                            <span class="music-card-album-pill">Album</span>
                            <div class="music-card-play" title="Play album"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l12-7z"/></svg></div>
                            ${ring}
                        </div>
                        <div class="music-card-title">${esc(a.title || 'Untitled Album')}</div>
                        <div class="music-card-artist">${esc(a.artist || '')}</div>
                    </div>`;
            }).join('');

            // --- Singles (tracks with no album_id) ---
            const singleHtml = visibleSingles.map(t => {
                // pt:thumb: markers need to go through _musicThumbAttrs, not
                // straight into src="". Hardcoding src="${t.thumbnail}" broke
                // singles + dock + player after the local-cache rollout.
                const tAttrs = _musicThumbAttrs(t.thumbnail);
                const thumb = tAttrs ? `<img ${tAttrs} loading="lazy" />` : '';
                const playing = t.id === currentId ? ' now-playing' : '';
                const hidden = !!t.hidden;
                const cls = playing + (hidden ? ' is-hidden' : '');
                const isNew = t.added_at && !t.seen_at && (nowSec - t.added_at) < 86400 * 7;
                const newPill = isNew ? '<span class="music-card-new">NEW</span>' : '';
                const hiddenBadge = hidden ? '<span class="music-card-hidden-badge">Hidden</span>' : '';
                return `
                    <div class="music-card${cls}" data-track-id="${esc(t.id)}" data-music-card-id="${esc(t.id)}" data-card-kind="track">
                        <div class="music-card-art">
                            ${thumb}
                            <div class="music-card-select-mark"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg></div>
                            ${newPill}
                            ${hiddenBadge}
                            <div class="music-card-play"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l12-7z"/></svg></div>
                        </div>
                        <div class="music-card-title">${esc(t.title || 'Untitled')}</div>
                        <div class="music-card-artist">${esc(t.artist || '')}</div>
                    </div>`;
            }).join('');

            grid.innerHTML = albumHtml + singleHtml;

            // Single track click → play; right-click → context menu.
            grid.querySelectorAll('.music-card[data-card-kind="track"]').forEach(card => {
                card.addEventListener('click', (e) => {
                    // Click on the per-card checkmark → enter/toggle selection
                    // (mirrors video library entry-via-checkbox path).
                    if (e.target.closest('.music-card-select-mark')) {
                        e.stopPropagation();
                        MusicSelection.handleCheckboxClick(card.dataset.trackId);
                        return;
                    }
                    // If selection mode is active, click toggles selection
                    // rather than playing the track.
                    if (MusicSelection.isActive()) {
                        MusicSelection.toggle(card.dataset.trackId);
                        return;
                    }
                    const id = card.dataset.trackId;
                    const track = lib.find(x => x.id === id);
                    if (track) _playMusicTrack(track);
                });
                card.addEventListener('contextmenu', (e) => {
                    e.preventDefault();
                    // Right-click = multi-select, exactly like the video library.
                    // First right-click enters selection mode with this card
                    // selected; subsequent right-clicks toggle. Bulk actions
                    // (Hide / Remove / Delete) live in the selection action bar.
                    MusicSelection.handleCheckboxClick(card.dataset.trackId);
                });
            });
            // Album cards: body click → open detail view; play-button click → play first track;
            // right-click → context menu with album-specific actions.
            grid.querySelectorAll('.music-card[data-card-kind="album"]').forEach(card => {
                const albumId = card.dataset.albumId;
                const playBtn = card.querySelector('.music-card-play');
                if (playBtn) {
                    playBtn.addEventListener('click', (e) => {
                        e.stopPropagation();
                        if (MusicSelection.isActive()) {
                            MusicSelection.toggle(albumId);
                            return;
                        }
                        _playMusicAlbum(albumId, { shuffle: false });
                    });
                }
                card.addEventListener('click', (e) => {
                    if (e.target.closest('.music-card-select-mark')) {
                        e.stopPropagation();
                        MusicSelection.handleCheckboxClick(albumId);
                        return;
                    }
                    if (MusicSelection.isActive()) {
                        MusicSelection.toggle(albumId);
                        return;
                    }
                    // Debounce single-click so a dblclick can preempt — matches
                    // the video library's openLibraryDetail/openLibraryItemDirect
                    // pattern. Single = open the detail view; double = play.
                    if (card._musicClickTimer) {
                        clearTimeout(card._musicClickTimer);
                        card._musicClickTimer = null;
                    }
                    card._musicClickTimer = setTimeout(() => {
                        card._musicClickTimer = null;
                        _openMusicAlbumDetail(albumId);
                    }, 230);
                });
                card.addEventListener('dblclick', (e) => {
                    if (e.target.closest('.music-card-select-mark')) return;
                    if (MusicSelection.isActive()) return;
                    if (card._musicClickTimer) {
                        clearTimeout(card._musicClickTimer);
                        card._musicClickTimer = null;
                    }
                    try { _playMusicAlbum(albumId, { startIndex: 0 }); } catch (_) {}
                });
                card.addEventListener('contextmenu', (e) => {
                    e.preventDefault();
                    // Right-click = multi-select, like the video library. An
                    // album id in the selection expands to its track ids when
                    // a bulk action runs (see MusicSelection.expandedTrackIds).
                    MusicSelection.handleCheckboxClick(albumId);
                });
            });

            // Resolve any pt:thumb: cover markers via the backend, same as the
            // video library's _resolvePendingThumbnails. We do this here (vs.
            // reusing that method) because the music view has no access to it.
            _resolveMusicThumbMarkers();

            // Re-apply selection visuals when the grid re-renders (e.g. after a
            // background refresh). Without this the .is-selected class is lost.
            MusicSelection.refreshAfterRender();
        }

        // Walk every img[data-thumb-marker] currently in the music view and
        // ask the backend to hand back base64 image bytes. Cached so repeated
        // re-renders don't refetch. Mirrors the library-side resolver — kept
        // separate only because the two views don't share scope today.
        const _musicThumbCache = {};
        const _musicThumbInflight = new Set();
        // Build <img> attrs for a thumbnail value: handles both remote URLs
        // and pt:thumb: markers (the latter resolved via the backend). Must
        // be used everywhere we render a track's thumbnail or singles + dock
        // + player will silently break when the thumbnail is a local marker.
        function _musicThumbAttrs(thumb) {
            const _esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
            if (!thumb) return '';
            if (typeof thumb === 'string' && thumb.startsWith('pt:thumb:')) {
                if (_musicThumbCache[thumb]) return `src="${_musicThumbCache[thumb]}"`;
                return `data-thumb-marker="${_esc(thumb)}"`;
            }
            return `src="${_esc(thumb)}"`;
        }
        // Imperative variant for code paths that already have an <img> element
        // and need to set its src. Drop-in for `imgEl.src = thumb`.
        function _setMusicArt(imgEl, thumb) {
            if (!imgEl) return;
            if (!thumb) { imgEl.removeAttribute('src'); return; }
            if (typeof thumb === 'string' && thumb.startsWith('pt:thumb:')) {
                if (_musicThumbCache[thumb]) {
                    imgEl.src = _musicThumbCache[thumb];
                    return;
                }
                imgEl.removeAttribute('src');
                if (typeof pywebview === 'undefined' || !pywebview.api) return;
                if (_musicThumbInflight.has(thumb)) return;
                _musicThumbInflight.add(thumb);
                pywebview.api.get_thumbnail_data(thumb).then(dataUrl => {
                    _musicThumbInflight.delete(thumb);
                    if (!dataUrl) return;
                    _musicThumbCache[thumb] = dataUrl;
                    imgEl.src = dataUrl;
                }).catch(() => { _musicThumbInflight.delete(thumb); });
                return;
            }
            imgEl.src = thumb;
        }
        // Same idea, for background-image (used by player backdrop).
        function _setMusicBackdrop(el, thumb) {
            if (!el) return;
            const apply = (url) => {
                if (!url) { el.style.backgroundImage = ''; el.classList.remove('has-art'); return; }
                el.style.backgroundImage = `url("${url.replace(/"/g, '\\"')}")`;
                el.classList.add('has-art');
            };
            if (!thumb) { apply(''); return; }
            if (typeof thumb === 'string' && thumb.startsWith('pt:thumb:')) {
                if (_musicThumbCache[thumb]) { apply(_musicThumbCache[thumb]); return; }
                apply('');
                if (typeof pywebview === 'undefined' || !pywebview.api) return;
                pywebview.api.get_thumbnail_data(thumb).then(dataUrl => {
                    if (!dataUrl) return;
                    _musicThumbCache[thumb] = dataUrl;
                    apply(dataUrl);
                }).catch(() => {});
                return;
            }
            apply(thumb);
        }
        async function _resolveMusicThumbMarkers() {
            if (typeof pywebview === 'undefined' || !pywebview.api) return;
            const imgs = document.querySelectorAll('img[data-thumb-marker]');
            for (const img of imgs) {
                const marker = img.getAttribute('data-thumb-marker');
                if (!marker || !marker.startsWith('pt:thumb:')) continue;
                if (_musicThumbCache[marker]) {
                    img.src = _musicThumbCache[marker];
                    img.removeAttribute('data-thumb-marker');
                    continue;
                }
                if (_musicThumbInflight.has(marker)) continue;
                _musicThumbInflight.add(marker);
                pywebview.api.get_thumbnail_data(marker).then(dataUrl => {
                    _musicThumbInflight.delete(marker);
                    if (!dataUrl) return;
                    _musicThumbCache[marker] = dataUrl;
                    document.querySelectorAll(`img[data-thumb-marker="${marker}"]`).forEach(el => {
                        el.src = dataUrl;
                        el.removeAttribute('data-thumb-marker');
                    });
                }).catch(() => { _musicThumbInflight.delete(marker); });
            }
        }

        // ================================================================
        // MUSIC LIBRARY SELECTION MODE — mirror of the video Selection module
        // ================================================================
        // Per-card checkbox (top-left, hover or active) + right-click both
        // enter selection mode. Cards with kind="album" expand to their
        // member track_ids for the bulk hide/remove/delete actions so a
        // single click selects the whole album. Esc cancels.
        // Visuals: .is-selectable + .is-selected on .music-card (parallels
        // .library-card.is-selectable / .is-selected). The action bar lives
        // in #music-selection-actionbar (separate from the video one so the
        // two never collide).
        const MusicSelection = (() => {
            let active = false;
            // Stores card ids — either track ids OR album ids. We expand
            // album ids to their constituent track ids only when invoking
            // bulk operations (so the user can deselect a single album with
            // one click even if it owns 12 tracks).
            const selected = new Set();

            function findAlbumById(id) {
                return (_musicState.albums || []).find(a => a.id === id) || null;
            }
            function findTrackById(id) {
                return (_musicState.library || []).find(t => t.id === id) || null;
            }
            function isAlbumId(id) {
                return !!findAlbumById(id);
            }
            // Expand selection → flat set of track ids covered by it. Albums
            // contribute all their owned track_ids; bare track ids pass through.
            function expandedTrackIds() {
                const out = new Set();
                selected.forEach(id => {
                    const album = findAlbumById(id);
                    if (album) {
                        for (const tid of (album.track_ids || [])) out.add(tid);
                    } else {
                        out.add(id);
                    }
                });
                return Array.from(out);
            }

            function enter(initialId) {
                active = true;
                document.body.classList.add('in-music-selection-mode');
                document.querySelectorAll('.music-card').forEach(card => card.classList.add('is-selectable'));
                if (initialId) toggle(initialId);
                else updateBar();
            }
            function exit() {
                active = false;
                selected.clear();
                document.body.classList.remove('in-music-selection-mode');
                document.querySelectorAll('.music-card').forEach(card => {
                    card.classList.remove('is-selectable', 'is-selected');
                });
                hideBar();
            }
            function toggle(id) {
                if (!id) return;
                if (selected.has(id)) selected.delete(id);
                else selected.add(id);
                const card = document.querySelector(`.music-card[data-music-card-id="${(window.CSS && CSS.escape) ? CSS.escape(id) : id}"]`);
                if (card) card.classList.toggle('is-selected', selected.has(id));
                if (selected.size === 0) exit();
                else updateBar();
            }
            function handleCheckboxClick(id) {
                if (!active) enter(id);
                else toggle(id);
            }
            function selectAll() {
                document.querySelectorAll('.music-card').forEach(card => {
                    const id = card.getAttribute('data-music-card-id');
                    if (id) { selected.add(id); card.classList.add('is-selected'); }
                });
                updateBar();
            }
            function refreshAfterRender() {
                if (!active) return;
                document.querySelectorAll('.music-card').forEach(card => {
                    card.classList.add('is-selectable');
                    const id = card.getAttribute('data-music-card-id');
                    if (id && selected.has(id)) card.classList.add('is-selected');
                });
            }
            function updateBar() {
                const bar = document.getElementById('music-selection-actionbar');
                const countEl = document.getElementById('music-selection-actionbar-count');
                if (!bar) return;
                if (active && selected.size > 0) {
                    countEl.textContent = `${selected.size} selected`;
                    bar.classList.add('visible');
                } else if (active) {
                    countEl.textContent = '0 selected';
                    bar.classList.add('visible');
                } else {
                    hideBar();
                }
                // Hide-button label: if every track covered by the selection
                // is already hidden, the button unhides. Mirrors the video
                // library's label logic.
                const hideLabel = document.getElementById('music-selection-hide-btn-label');
                if (hideLabel) {
                    const tids = expandedTrackIds();
                    if (tids.length > 0) {
                        const lib = _musicState.library || [];
                        const byId = new Map(lib.map(t => [t.id, t]));
                        let allHidden = true;
                        for (const tid of tids) {
                            const t = byId.get(tid);
                            if (!t || !t.hidden) { allHidden = false; break; }
                        }
                        hideLabel.textContent = allHidden ? 'Show' : 'Hide';
                    } else {
                        hideLabel.textContent = 'Hide';
                    }
                }
            }
            function hideBar() {
                const bar = document.getElementById('music-selection-actionbar');
                if (bar) bar.classList.remove('visible');
            }

            async function hideSelected() {
                const tids = expandedTrackIds();
                if (!tids.length) return;
                const lib = _musicState.library || [];
                const byId = new Map(lib.map(t => [t.id, t]));
                let allHidden = true;
                for (const tid of tids) {
                    const t = byId.get(tid);
                    if (!t || !t.hidden) { allHidden = false; break; }
                }
                const wantHidden = !allHidden;
                // Optimistic
                for (const tid of tids) {
                    const t = byId.get(tid);
                    if (!t) continue;
                    if (wantHidden) t.hidden = true; else delete t.hidden;
                }
                _renderMusicLibrary();
                try {
                    await pywebview.api.bulk_hide_music_tracks(tids, wantHidden);
                    const n = tids.length;
                    showToast(`${wantHidden ? 'Hidden' : 'Shown'} ${n} track${n === 1 ? '' : 's'}`, 'Undo', async () => {
                        try { await pywebview.api.bulk_hide_music_tracks(tids, !wantHidden); } catch (_) {}
                        await _refreshMusicLibrary();
                    });
                } catch (_) {
                    // Roll back
                    for (const tid of tids) {
                        const t = byId.get(tid);
                        if (!t) continue;
                        if (wantHidden) delete t.hidden; else t.hidden = true;
                    }
                    _renderMusicLibrary();
                    showToast("Couldn't update hidden state", null, null);
                    return;
                }
                exit();
            }

            async function removeSelected() {
                // Albums in the selection: remove the whole album record
                // (which also drops linked tracks from the library) via the
                // existing delete_music_album(album_id, delete_files=False)
                // path so the album card vanishes too. Tracks selected on
                // their own go through bulk_remove_music_tracks.
                const albumIds = [];
                const looseTrackIds = [];
                selected.forEach(id => {
                    if (isAlbumId(id)) albumIds.push(id);
                    else looseTrackIds.push(id);
                });
                if (!albumIds.length && !looseTrackIds.length) return;
                const totalCount = albumIds.length + looseTrackIds.length;
                let removedTracks = [];
                try {
                    if (looseTrackIds.length) {
                        const res = await pywebview.api.bulk_remove_music_tracks(looseTrackIds);
                        removedTracks = (res && res.removed) || [];
                    }
                    for (const aid of albumIds) {
                        try { await pywebview.api.delete_music_album(aid, false); } catch (_) {}
                    }
                } catch (_) {
                    showToast("Couldn't remove from library", null, null);
                    return;
                }
                exit();
                await _refreshMusicLibrary();
                showToast(
                    `Removed ${totalCount} item${totalCount === 1 ? '' : 's'} from library`,
                    null, null
                );
            }

            async function deleteSelected() {
                const totalCount = selected.size;
                const ok = await confirmDialog({
                    title: `Delete ${totalCount} item${totalCount === 1 ? '' : 's'} from disk?`,
                    body: `Audio files for the selected ${totalCount === 1 ? 'track / album' : 'tracks / albums'} will be deleted from disk. You'll need to re-download to listen again. This cannot be undone.`,
                    confirmText: `Delete ${totalCount}`,
                    cancelText: 'Cancel',
                    danger: true,
                });
                if (!ok) return;

                // Stop the dock if the now-playing track is in the kill list.
                const tids = expandedTrackIds();
                const curId = _musicPlayer.currentTrack?.id;
                if (curId && tids.includes(curId) && _musicPlayer.audio) {
                    try { _musicPlayer.audio.pause(); } catch (_) {}
                }

                const albumIds = [];
                const looseTrackIds = [];
                selected.forEach(id => {
                    if (isAlbumId(id)) albumIds.push(id);
                    else looseTrackIds.push(id);
                });

                let deleted = 0, errors = 0;
                try {
                    if (looseTrackIds.length) {
                        const res = await pywebview.api.bulk_delete_music_tracks(looseTrackIds);
                        deleted += (res?.deleted || 0);
                        errors += (res?.errors || 0);
                    }
                    for (const aid of albumIds) {
                        try {
                            const res = await pywebview.api.delete_music_album(aid, true);
                            if (res?.ok) deleted++;
                            else errors++;
                        } catch (_) {
                            errors++;
                        }
                    }
                } catch (_) {
                    showToast('Bulk delete failed', null, null);
                    return;
                }
                exit();
                await _refreshMusicLibrary();
                if (errors === 0) {
                    showToast(`Deleted ${deleted} item${deleted === 1 ? '' : 's'}`, null, null);
                } else {
                    showToast(`Deleted ${deleted}, ${errors} failed (files locked?)`, null, null);
                }
            }

            return {
                isActive: () => active,
                enter, exit, toggle, handleCheckboxClick, selectAll, refreshAfterRender,
                hideSelected, removeSelected, deleteSelected,
            };
        })();

        // Wire bar buttons + Esc once at module load.
        (function _wireMusicSelectionChrome() {
            const wire = (id, fn) => {
                const el = document.getElementById(id);
                if (el && !el._musSelWired) {
                    el._musSelWired = true;
                    el.addEventListener('click', fn);
                }
            };
            wire('music-selection-cancel-btn', () => MusicSelection.exit());
            wire('music-selection-delete-btn', () => MusicSelection.deleteSelected());
            wire('music-selection-remove-btn', () => MusicSelection.removeSelected());
            wire('music-selection-hide-btn', () => MusicSelection.hideSelected());
            wire('music-selection-select-all-btn', () => MusicSelection.selectAll());
            // Esc clears the selection — but only when no other higher-priority
            // surface (video selection mode, panels, ctx menus) is open. We let
            // the video Selection's Esc handler win when it's active.
            document.addEventListener('keydown', (e) => {
                if (e.key !== 'Escape') return;
                if (!MusicSelection.isActive()) return;
                MusicSelection.exit();
            });
        })();

        // ---- Music album detail view ----------------------------------- //
        // Opens the dedicated album page (Spotify-shaped). Switches the view
        // pane, fetches the joined album+tracks payload from the backend, and
        // calls _renderMusicAlbumDetail. Also clears the NEW pill for the album.
        async function _openMusicAlbumDetail(albumId) {
            if (!albumId) return;
            _musicState.currentAlbumId = albumId;
            // Optimistic UI: clear NEW immediately so the badge doesn't linger
            // on the return-to-library paint.
            const local = (_musicState.albums || []).find(a => a.id === albumId);
            if (local && !local.seen_at) local.seen_at = Math.floor(Date.now() / 1000);
            try { pywebview.api.mark_album_seen(albumId); } catch (_) {}
            // Switch view first so the user sees the scaffold immediately.
            app.switchView('music-album-detail');
            await _renderMusicAlbumDetail(albumId);
        }

        async function _renderMusicAlbumDetail(albumId) {
            if (!albumId) return;
            let album;
            try {
                album = await pywebview.api.get_music_album(albumId);
            } catch (_) {
                album = null;
            }
            if (!album) {
                showToast('Album not found', null, null);
                app.switchView('music');
                return;
            }
            const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
            const titleEl = document.getElementById('mad-title');
            const artImg = document.getElementById('mad-art-img');
            const backdrop = document.getElementById('mad-hero-backdrop');
            const submeta = document.getElementById('mad-submeta');
            const rowsWrap = document.getElementById('mad-rows');
            const progressNote = document.getElementById('mad-progress-note');
            const playAllBtn = document.getElementById('mad-play-all');
            const shuffleBtn = document.getElementById('mad-shuffle');

            const cover = album.cover_url || '';
            if (titleEl) titleEl.textContent = album.title || 'Untitled Album';
            // Cover may be a 'pt:thumb:' marker (local) or a remote URL. For
            // markers, ask the backend for the data URL and apply it to both
            // the hero <img> and the backdrop background. For remote URLs, use
            // as-is.
            const applyCover = (resolvedUrl) => {
                if (artImg) {
                    artImg.src = resolvedUrl;
                    artImg.alt = album.title || '';
                }
                if (backdrop) {
                    backdrop.style.backgroundImage = resolvedUrl
                        ? `url("${resolvedUrl.replace(/"/g, '\\"')}")` : '';
                }
            };
            if (cover.startsWith('pt:thumb:')) {
                if (_musicThumbCache[cover]) {
                    applyCover(_musicThumbCache[cover]);
                } else {
                    applyCover('');
                    if (typeof pywebview !== 'undefined' && pywebview.api) {
                        pywebview.api.get_thumbnail_data(cover).then(dataUrl => {
                            if (dataUrl) {
                                _musicThumbCache[cover] = dataUrl;
                                applyCover(dataUrl);
                            }
                        }).catch(() => {});
                    }
                }
            } else {
                applyCover(cover);
            }
            // Submeta line: Artist · Year · N tracks · total duration
            const tracks = album.tracks || [];
            const total = album.total_tracks || tracks.length || 0;
            const downloaded = album.downloaded_count || 0;
            const downloadedTracks = tracks.filter(t => !t.pending);
            const totalSeconds = downloadedTracks.reduce((acc, t) => acc + (t.duration_seconds || 0), 0);
            const durStr = totalSeconds > 0 ? _formatAlbumDuration(totalSeconds) : '';
            // Pick year from any downloaded track (they all share it). Fall back to ''.
            const year = (downloadedTracks.find(t => t.year)?.year) || '';
            const parts = [];
            if (album.artist) parts.push(`<span class="mad-submeta-artist">${esc(album.artist)}</span>`);
            if (year) parts.push(`<span>${esc(year)}</span>`);
            parts.push(`<span>${total} track${total === 1 ? '' : 's'}</span>`);
            if (durStr) parts.push(`<span>${durStr}</span>`);
            if (submeta) {
                submeta.innerHTML = parts.join('<span class="mad-submeta-sep">·</span>');
            }

            // Progress note + button disable while downloading.
            const isDownloading = album.status === 'downloading' && downloaded < total;
            const actionsEl = document.getElementById('mad-actions');
            const progBarWrap = document.getElementById('mad-progress-bar-wrap');
            const progBarFill = document.getElementById('mad-progress-bar-fill');
            const aggPct = total > 0 ? Math.round((downloaded / total) * 100) : 0;
            if (progressNote) {
                if (isDownloading) {
                    progressNote.hidden = false;
                    progressNote.innerHTML = `Downloading <strong>${downloaded}</strong> of ${total} tracks…`;
                } else {
                    progressNote.hidden = true;
                    progressNote.textContent = '';
                }
            }
            if (actionsEl) actionsEl.classList.toggle('is-downloading', isDownloading);
            if (progBarWrap) {
                if (isDownloading) progBarWrap.removeAttribute('hidden');
                else progBarWrap.setAttribute('hidden', '');
            }
            if (progBarFill) progBarFill.style.width = `${aggPct}%`;
            // Disable Play All / Shuffle if nothing is downloaded yet (avoid no-op clicks).
            const anyPlayable = downloadedTracks.length > 0;
            if (playAllBtn) playAllBtn.disabled = !anyPlayable;
            if (shuffleBtn) shuffleBtn.disabled = !anyPlayable;

            // Render track rows.
            const currentId = _musicPlayer.currentTrack?.id || '';
            const playingDot = '<span class="mad-playing-indicator"><span></span><span></span><span></span></span>';
            if (rowsWrap) {
                rowsWrap.innerHTML = tracks.map((t, idx) => {
                    const isPlaying = t.id === currentId;
                    const pending = t.pending;
                    const cls = 'mad-row' + (isPlaying ? ' now-playing' : '') + (pending ? ' pending' : '');
                    // One unified row-number indicator: playing → animated bars,
                    // pending → spinner, otherwise → the index number.
                    const numHtml = isPlaying
                        ? playingDot
                        : (pending ? '<span class="mad-pending-spinner"></span>' : `<span>${idx + 1}</span>`);
                    const titleText = pending ? 'Downloading…' : (t.title || 'Untitled');
                    const dur = pending ? '' : (t.duration_string || '');
                    // Per-track thumb: prefer the track's own YT video thumb
                    // (downloaded singles look distinct), fall back to album cover
                    // for pending tracks where we don't have a video thumb yet.
                    const tThumb = (t.thumbnail || '') || (album.cover_url || '');
                    let thumbHtml = '<div class="mad-row-thumb"></div>';
                    if (tThumb) {
                        if (tThumb.startsWith('pt:thumb:')) {
                            thumbHtml = `<div class="mad-row-thumb"><img data-thumb-marker="${esc(tThumb)}" alt=""></div>`;
                        } else {
                            thumbHtml = `<div class="mad-row-thumb"><img src="${esc(tThumb)}" loading="lazy" alt=""></div>`;
                        }
                    }
                    return `
                        <div class="${cls}" data-track-id="${esc(t.id)}" data-track-idx="${idx}">
                            <span class="mad-row-num">${numHtml}</span>
                            ${thumbHtml}
                            <span class="mad-row-title">${esc(titleText)}</span>
                            <span class="mad-row-duration">${esc(dur)}</span>
                        </div>`;
                }).join('');
                rowsWrap.querySelectorAll('.mad-row').forEach(row => {
                    row.addEventListener('click', () => {
                        if (row.classList.contains('pending')) return;
                        const idx = parseInt(row.dataset.trackIdx, 10);
                        if (!isNaN(idx)) _playMusicAlbum(albumId, { startIndex: idx });
                    });
                    row.addEventListener('contextmenu', (e) => {
                        e.preventDefault();
                        if (row.classList.contains('pending')) return;
                        _showMadCtx(e.clientX, e.clientY, row, albumId);
                    });
                });
                // Any per-track thumbs that landed as pt:thumb: markers (rare —
                // happens if a track's thumbnail was cached locally) need backend
                // resolution. Same scheme as the music library covers above.
                _resolveMusicThumbMarkers();
            }
        }

        // Format aggregate album duration as "12 min" / "1 hr 4 min".
        function _formatAlbumDuration(seconds) {
            if (!seconds || seconds < 60) return '';
            const m = Math.round(seconds / 60);
            if (m < 60) return `${m} min`;
            const h = Math.floor(m / 60);
            const rem = m % 60;
            return rem ? `${h} hr ${rem} min` : `${h} hr`;
        }

        // Set the shuffle bag from an arbitrary list of tracks (album, in our case)
        // then play the first track. Used by Play All (when shuffle is off, the bag
        // is the ordered list; when shuffle is on, we shuffle in-place).
        function _playMusicAlbum(albumId, opts) {
            opts = opts || {};
            const album = (_musicState.albums || []).find(a => a.id === albumId);
            if (!album) return;
            // Resolve playable tracks from the library, preserving album order.
            const lib = _musicState.library || [];
            const byId = new Map(lib.map(t => [t.id, t]));
            const ordered = (album.track_ids || []).map(tid => byId.get(tid)).filter(Boolean);
            if (!ordered.length) {
                showToast('No downloaded tracks yet', null, null);
                return;
            }
            let startIndex = typeof opts.startIndex === 'number' ? opts.startIndex : 0;
            let playlist = ordered;
            if (opts.shuffle) {
                // Shuffle in place (Fisher-Yates).
                playlist = ordered.slice();
                for (let i = playlist.length - 1; i > 0; i--) {
                    const j = Math.floor(Math.random() * (i + 1));
                    [playlist[i], playlist[j]] = [playlist[j], playlist[i]];
                }
                startIndex = 0;
                // Stamp the music player's shuffle bag so subsequent "Next" pops
                // from this album's order (matches existing shuffle UX).
                _musicPlayer.shuffle = true;
                _paintModeButtons();
                try { pywebview.api.set_setting('music_shuffle', true); } catch (_) {}
            }
            // If startIndex was a row click into an ordered (non-shuffle) play,
            // seed the shuffle bag with the remaining ordered tracks so "Next"
            // continues through the album in order regardless of shuffle setting.
            _musicPlayer.shuffleOrder = playlist
                .slice(startIndex + 1)
                .map(t => t.id);
            const startTrack = playlist[startIndex];
            if (startTrack) _playMusicTrack(startTrack);
        }

        // ---- Album row right-click context menu ----
        let _madCtxRow = null;
        // ---- Music LIBRARY-card right-click context menu --------------- //
        // Separate from _showMusicCtx (which targets the search-row pattern)
        // because library cards need different actions: View details, Hide/
        // Unhide, Remove (drop from library, keep file), Delete (drop + erase).
        // Reuses the .music-ctx visual class. Stamped on body, lazy-created.
        let _mlibCtxTarget = null;   // { kind: 'track'|'album', track?, album? }
        function _showMlibCtx(x, y, payload) {
            let ctx = document.getElementById('mlib-ctx');
            if (!ctx) {
                ctx = document.createElement('div');
                ctx.id = 'mlib-ctx';
                ctx.className = 'music-ctx';
                document.body.appendChild(ctx);
                // One delegated click handler for all items inside this ctx.
                ctx.addEventListener('click', _onMlibCtxClick);
            }
            _mlibCtxTarget = payload;
            ctx.innerHTML = _buildMlibCtxHTML(payload);
            ctx.removeAttribute('hidden');
            ctx.style.display = 'block';
            const w = 240;
            const h = ctx.offsetHeight || 280;
            const vw = window.innerWidth, vh = window.innerHeight;
            ctx.style.left = `${Math.min(x, vw - w - 8)}px`;
            ctx.style.top = `${Math.min(y, vh - h - 8)}px`;
            // Click-outside close (registered next frame so this click doesn't fire it)
            setTimeout(() => {
                const close = (e) => {
                    if (!ctx.contains(e.target)) {
                        _hideMlibCtx();
                        document.removeEventListener('mousedown', close);
                    }
                };
                document.addEventListener('mousedown', close);
            }, 0);
        }
        function _hideMlibCtx() {
            const ctx = document.getElementById('mlib-ctx');
            if (ctx) { ctx.style.display = 'none'; ctx.setAttribute('hidden', ''); }
            _mlibCtxTarget = null;
        }
        function _buildMlibCtxHTML(payload) {
            const playSvg = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l12-7z"/></svg>';
            const detailSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>';
            const hideSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.94 10.94 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A10.94 10.94 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';
            const eyeSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
            const copySvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>';
            const openSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>';
            const folderSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';
            const removeSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="5" y1="12" x2="19" y2="12"/></svg>';
            const trashSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>';
            const shuffleSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="16 3 21 3 21 8"/><line x1="4" y1="20" x2="21" y2="3"/><polyline points="21 16 21 21 16 21"/><line x1="15" y1="15" x2="21" y2="21"/><line x1="4" y1="4" x2="9" y2="9"/></svg>';

            if (payload.kind === 'track') {
                const t = payload.track;
                const hidden = !!t.hidden;
                return `
                    <button class="mctx-item primary" data-mlib-ctx="play">${playSvg} Play</button>
                    <button class="mctx-item" data-mlib-ctx="details">${detailSvg} View details</button>
                    <button class="mctx-item" data-mlib-ctx="hide">${hidden ? eyeSvg : hideSvg} ${hidden ? 'Unhide' : 'Hide'}</button>
                    <button class="mctx-item" data-mlib-ctx="copy-link">${copySvg} Copy YouTube link</button>
                    <button class="mctx-item" data-mlib-ctx="open-external">${openSvg} Open in YouTube</button>
                    <button class="mctx-item" data-mlib-ctx="reveal">${folderSvg} Reveal in folder</button>
                    <div class="mctx-sep"></div>
                    <button class="mctx-item" data-mlib-ctx="remove">${removeSvg} Remove from library</button>
                    <button class="mctx-item" data-mlib-ctx="delete">${trashSvg} Delete from disk</button>`;
            }
            // Album payload
            const a = payload.album;
            const lib = _musicState.library || [];
            const byId = new Map(lib.map(t => [t.id, t]));
            const tids = a.track_ids || [];
            const allHidden = tids.length > 0 && tids.every(tid => byId.get(tid)?.hidden);
            return `
                <button class="mctx-item primary" data-mlib-ctx="play-album">${playSvg} Play all</button>
                <button class="mctx-item" data-mlib-ctx="shuffle-album">${shuffleSvg} Shuffle</button>
                <button class="mctx-item" data-mlib-ctx="view-album">${detailSvg} View album</button>
                <button class="mctx-item" data-mlib-ctx="hide-album">${allHidden ? eyeSvg : hideSvg} ${allHidden ? 'Unhide album' : 'Hide album'}</button>
                <div class="mctx-sep"></div>
                <button class="mctx-item" data-mlib-ctx="delete-album">${trashSvg} Delete album</button>`;
        }
        async function _onMlibCtxClick(e) {
            const btn = e.target.closest('button[data-mlib-ctx]');
            if (!btn) return;
            const action = btn.getAttribute('data-mlib-ctx');
            const payload = _mlibCtxTarget;
            _hideMlibCtx();
            if (!payload) return;

            // ----- Track actions ----- //
            if (payload.kind === 'track') {
                const t = payload.track;
                const id = t.id;
                if (action === 'play') {
                    _playMusicTrack(t);
                } else if (action === 'details') {
                    _showMusicTrackDetail(id);
                } else if (action === 'hide') {
                    const next = !t.hidden;
                    // Optimistic flip + re-render.
                    if (next) t.hidden = true; else delete t.hidden;
                    _renderMusicLibrary();
                    try {
                        await pywebview.api.hide_music_track(id, next);
                        showToast(next ? 'Hidden' : 'Unhidden', null, null);
                    } catch (_) {
                        // Roll back on failure.
                        if (next) delete t.hidden; else t.hidden = true;
                        _renderMusicLibrary();
                        showToast("Couldn't update hidden state", null, null);
                    }
                } else if (action === 'copy-link') {
                    const url = t.url || `https://music.youtube.com/watch?v=${id}`;
                    try { await navigator.clipboard.writeText(url); showToast('Link copied', null, null); } catch (_) {}
                } else if (action === 'open-external') {
                    const url = t.url || `https://music.youtube.com/watch?v=${id}`;
                    try { pywebview.api.open_external_url(url); } catch (_) {}
                } else if (action === 'reveal') {
                    if (t.filepath) {
                        try { pywebview.api.reveal_in_folder(t.filepath); } catch (_) {}
                    } else {
                        showToast('No file path on record', null, null);
                    }
                } else if (action === 'remove') {
                    try {
                        await pywebview.api.remove_from_music_library(id);
                        showToast('Removed from library', null, null);
                        await _refreshMusicLibrary();
                    } catch (_) {
                        showToast("Couldn't remove track", null, null);
                    }
                } else if (action === 'delete') {
                    const ok = await confirmDialog({
                        title: `Delete "${t.title || 'this track'}"?`,
                        body: 'The audio file will be deleted from disk. You\'ll need to re-download to listen again. This cannot be undone.',
                        confirmText: 'Delete',
                        cancelText: 'Cancel',
                        danger: true,
                    });
                    if (!ok) return;
                    try {
                        await pywebview.api.delete_music_track(id);
                        showToast(`Deleted "${t.title || 'track'}"`, null, null);
                        // If the deleted track is currently playing, stop the dock.
                        if (_musicPlayer.currentTrack?.id === id && _musicPlayer.audio) {
                            try { _musicPlayer.audio.pause(); } catch (_) {}
                        }
                        await _refreshMusicLibrary();
                    } catch (err) {
                        showToast(`Couldn't delete: ${err?.message || 'unknown error'}`, null, null);
                    }
                }
                return;
            }

            // ----- Album actions ----- //
            if (payload.kind === 'album') {
                const a = payload.album;
                const albumId = a.id;
                if (action === 'play-album') {
                    _playMusicAlbum(albumId, { shuffle: false });
                } else if (action === 'shuffle-album') {
                    _playMusicAlbum(albumId, { shuffle: true });
                } else if (action === 'view-album') {
                    _openMusicAlbumDetail(albumId);
                } else if (action === 'hide-album') {
                    const tids = a.track_ids || [];
                    const lib = _musicState.library || [];
                    const byId = new Map(lib.map(t => [t.id, t]));
                    const allHidden = tids.length > 0 && tids.every(tid => byId.get(tid)?.hidden);
                    const next = !allHidden;
                    // Optimistic flip on every owned track.
                    for (const tid of tids) {
                        const t = byId.get(tid);
                        if (!t) continue;
                        if (next) t.hidden = true; else delete t.hidden;
                    }
                    _renderMusicLibrary();
                    try {
                        await pywebview.api.bulk_hide_music_tracks(tids, next);
                        showToast(next ? 'Album hidden' : 'Album unhidden', null, null);
                    } catch (_) {
                        // Roll back.
                        for (const tid of tids) {
                            const t = byId.get(tid);
                            if (!t) continue;
                            if (next) delete t.hidden; else t.hidden = true;
                        }
                        _renderMusicLibrary();
                        showToast("Couldn't update album hidden state", null, null);
                    }
                } else if (action === 'delete-album') {
                    const ok = await confirmDialog({
                        title: `Delete album "${a.title || 'this album'}"?`,
                        body: `All ${(a.track_ids || []).length} track(s) will be deleted from disk. This cannot be undone.`,
                        confirmText: 'Delete album',
                        cancelText: 'Cancel',
                        danger: true,
                    });
                    if (!ok) return;
                    try {
                        await pywebview.api.delete_music_album(albumId, true);
                        showToast(`Deleted album "${a.title || ''}"`, null, null);
                        await _refreshMusicLibrary();
                    } catch (err) {
                        showToast(`Couldn't delete album: ${err?.message || 'unknown error'}`, null, null);
                    }
                }
            }
        }

        // ---- Music track detail panel --------------------------------- //
        // Reuses the .detail-panel slide-in CSS (shared with the video library)
        // via a separate DOM node anchored inside #music-view, so the panel
        // overlays the music grid without disturbing the video library's own
        // detail panel state. Body is filled with mtd-* content.
        function _showMusicTrackDetail(trackId) {
            if (!trackId) return;
            const lib = _musicState.library || [];
            const t = lib.find(x => x.id === trackId);
            if (!t) return;
            const panel = document.getElementById('music-detail-panel');
            const backdrop = document.getElementById('music-detail-panel-backdrop');
            const body = document.getElementById('music-detail-panel-body');
            if (!panel || !body) return;
            body.innerHTML = _buildMusicTrackDetailHTML(t);
            body.scrollTop = 0;
            panel.classList.remove('closing');
            panel.classList.add('visible');
            if (backdrop) backdrop.classList.add('visible');
            panel.dataset.trackId = trackId;
            _bindMusicTrackDetailActions(t);
        }
        function _hideMusicTrackDetail() {
            const panel = document.getElementById('music-detail-panel');
            const backdrop = document.getElementById('music-detail-panel-backdrop');
            if (!panel) return;
            panel.classList.remove('visible');
            panel.classList.add('closing');
            if (backdrop) backdrop.classList.remove('visible');
            const onEnd = () => {
                panel.classList.remove('closing');
                panel.removeEventListener('transitionend', onEnd);
            };
            panel.addEventListener('transitionend', onEnd);
            setTimeout(onEnd, 300);
        }
        function _buildMusicTrackDetailHTML(t) {
            const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
            const url = t.url || `https://music.youtube.com/watch?v=${t.id}`;
            const art = t.thumbnail ? `<img src="${esc(t.thumbnail)}" alt="">` : '';
            const addedDate = t.added_at
                ? new Date(t.added_at * 1000).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
                : '';
            const size = t.filesize ? _formatBytesShort(t.filesize) : '';
            // Action row icons
            const playSvg = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l12-7z"/></svg>';
            const queueSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>';
            const folderSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';
            const moreSvg = '<svg viewBox="0 0 24 24" fill="currentColor"><circle cx="5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="19" cy="12" r="2"/></svg>';

            // Meta rows
            const rows = [];
            if (t.duration_string) rows.push(['Duration', esc(t.duration_string)]);
            if (size) rows.push(['File size', esc(size)]);
            if (addedDate) rows.push(['Added', esc(addedDate)]);
            if (t.year) rows.push(['Year', esc(String(t.year))]);
            rows.push(['Source', `<a class="mtd-source-link" data-mtd-open-url="${esc(url)}">${esc(url)}</a>`]);
            if (t.filepath) rows.push(['File path', `<span class="mtd-filepath" data-mtd-copy-path="${esc(t.filepath)}" title="Click to copy">${esc(t.filepath)}</span>`]);

            const metaHtml = rows.map(([k, v]) =>
                `<div class="mtd-meta-key">${k}</div><div class="mtd-meta-val">${v}</div>`
            ).join('');

            const aboutBlock = t.description
                ? `<div class="mtd-about"><div class="mtd-about-title">About this track</div>${esc(t.description)}</div>`
                : '';

            const hidden = !!t.hidden;
            const hideLabel = hidden ? 'Unhide' : 'Hide';

            return `
                <div class="mtd-art">${art}</div>
                <h2 class="mtd-title">${esc(t.title || 'Untitled')}</h2>
                <div class="mtd-artist">${esc(t.artist || '')}</div>
                ${t.album ? `<div class="mtd-album">${esc(t.album)}</div>` : ''}
                <div class="mtd-actions">
                    <button class="mtd-action-btn primary" data-mtd-act="play">${playSvg}<span>Play</span></button>
                    <button class="mtd-action-btn" data-mtd-act="queue">${queueSvg}<span>Add to queue</span></button>
                    <button class="mtd-action-btn" data-mtd-act="reveal">${folderSvg}<span>Reveal</span></button>
                    <div class="mtd-more-wrap">
                        <button class="mtd-action-btn" data-mtd-act="more" aria-label="More">${moreSvg}<span>More</span></button>
                        <div class="mtd-more-menu" id="mtd-more-menu" hidden>
                            <button class="mctx-item" data-mtd-act="hide">${hideLabel}</button>
                            <button class="mctx-item" data-mtd-act="remove">Remove from library</button>
                            <button class="mctx-item" data-mtd-act="delete">Delete from disk</button>
                        </div>
                    </div>
                </div>
                <div class="mtd-meta">${metaHtml}</div>
                ${aboutBlock}`;
        }
        function _bindMusicTrackDetailActions(t) {
            const body = document.getElementById('music-detail-panel-body');
            if (!body) return;
            // Source-link click → external open (don't navigate the webview).
            body.querySelectorAll('[data-mtd-open-url]').forEach(el => {
                el.addEventListener('click', (e) => {
                    e.preventDefault();
                    const url = el.getAttribute('data-mtd-open-url');
                    if (url) { try { pywebview.api.open_external_url(url); } catch (_) {} }
                });
            });
            // File-path click → copy to clipboard.
            body.querySelectorAll('[data-mtd-copy-path]').forEach(el => {
                el.addEventListener('click', async () => {
                    const fp = el.getAttribute('data-mtd-copy-path');
                    if (!fp) return;
                    try { await navigator.clipboard.writeText(fp); showToast('Path copied', null, null); } catch (_) {}
                });
            });
            // Action buttons.
            body.querySelectorAll('[data-mtd-act]').forEach(btn => {
                btn.addEventListener('click', async (e) => {
                    e.stopPropagation();
                    const act = btn.getAttribute('data-mtd-act');
                    if (act === 'play') {
                        _playMusicTrack(t);
                    } else if (act === 'queue') {
                        // Push the underlying video file into the video-style queue so it
                        // shows up under the Queue rail. _qAddTrackToQueue isn't a thing;
                        // simplest path: play the track directly. Toast for clarity.
                        // (Music has no separate "up next" queue API yet — playing the track
                        // is the equivalent immediate action.)
                        _playMusicTrack(t);
                    } else if (act === 'reveal') {
                        if (t.filepath) {
                            try { pywebview.api.reveal_in_folder(t.filepath); } catch (_) {}
                        } else {
                            showToast('No file path on record', null, null);
                        }
                    } else if (act === 'more') {
                        const menu = document.getElementById('mtd-more-menu');
                        if (menu) menu.toggleAttribute('hidden');
                    } else if (act === 'hide') {
                        const next = !t.hidden;
                        if (next) t.hidden = true; else delete t.hidden;
                        try {
                            await pywebview.api.hide_music_track(t.id, next);
                            showToast(next ? 'Hidden' : 'Unhidden', null, null);
                            // Re-render the grid + the panel so the More menu reflects the new state.
                            _renderMusicLibrary();
                            _showMusicTrackDetail(t.id);
                        } catch (_) {
                            if (next) delete t.hidden; else t.hidden = true;
                            showToast("Couldn't update hidden state", null, null);
                        }
                    } else if (act === 'remove') {
                        try {
                            await pywebview.api.remove_from_music_library(t.id);
                            showToast('Removed from library', null, null);
                            _hideMusicTrackDetail();
                            await _refreshMusicLibrary();
                        } catch (_) { showToast("Couldn't remove track", null, null); }
                    } else if (act === 'delete') {
                        const ok = await confirmDialog({
                            title: `Delete "${t.title || 'this track'}"?`,
                            body: 'The audio file will be deleted from disk. You\'ll need to re-download to listen again. This cannot be undone.',
                            confirmText: 'Delete',
                            cancelText: 'Cancel',
                            danger: true,
                        });
                        if (!ok) return;
                        try {
                            await pywebview.api.delete_music_track(t.id);
                            showToast(`Deleted "${t.title || 'track'}"`, null, null);
                            if (_musicPlayer.currentTrack?.id === t.id && _musicPlayer.audio) {
                                try { _musicPlayer.audio.pause(); } catch (_) {}
                            }
                            _hideMusicTrackDetail();
                            await _refreshMusicLibrary();
                        } catch (err) {
                            showToast(`Couldn't delete: ${err?.message || 'unknown error'}`, null, null);
                        }
                    }
                });
            });
        }
        // Wire the panel's close/backdrop/esc once at module load.
        (function _wireMusicDetailPanelChrome() {
            const setup = () => {
                const close = document.getElementById('music-detail-panel-close');
                const backdrop = document.getElementById('music-detail-panel-backdrop');
                if (close && !close._bound) {
                    close._bound = true;
                    close.addEventListener('click', _hideMusicTrackDetail);
                }
                if (backdrop && !backdrop._bound) {
                    backdrop._bound = true;
                    backdrop.addEventListener('click', _hideMusicTrackDetail);
                }
                document.addEventListener('keydown', (e) => {
                    if (e.key !== 'Escape') return;
                    const panel = document.getElementById('music-detail-panel');
                    if (panel && panel.classList.contains('visible')) _hideMusicTrackDetail();
                });
            };
            if (document.readyState === 'loading') {
                document.addEventListener('DOMContentLoaded', setup);
            } else {
                setup();
            }
        })();

        let _madCtxAlbumId = null;
        function _showMadCtx(x, y, row, albumId) {
            let ctx = document.getElementById('mad-ctx');
            if (!ctx) {
                ctx = document.createElement('div');
                ctx.id = 'mad-ctx';
                ctx.className = 'mad-ctx';
                ctx.innerHTML = `
                    <button data-mad-ctx="play">
                        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l12-7z"/></svg>
                        Play
                    </button>
                    <button data-mad-ctx="copy-link">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>
                        Copy YouTube link
                    </button>
                    <div class="mad-ctx-sep"></div>
                    <button data-mad-ctx="remove">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
                        Remove from library
                    </button>`;
                document.body.appendChild(ctx);
                ctx.addEventListener('click', _onMadCtxClick);
            }
            _madCtxRow = row;
            _madCtxAlbumId = albumId;
            ctx.style.left = `${Math.min(x, window.innerWidth - 220)}px`;
            ctx.style.top = `${Math.min(y, window.innerHeight - 160)}px`;
            ctx.style.display = 'block';
            // Click outside closes
            setTimeout(() => {
                const close = (e) => {
                    if (!ctx.contains(e.target)) {
                        ctx.style.display = 'none';
                        document.removeEventListener('mousedown', close);
                    }
                };
                document.addEventListener('mousedown', close);
            }, 0);
        }
        function _onMadCtxClick(e) {
            const btn = e.target.closest('button[data-mad-ctx]');
            if (!btn) return;
            const action = btn.getAttribute('data-mad-ctx');
            const row = _madCtxRow;
            if (!row) return;
            document.getElementById('mad-ctx').style.display = 'none';
            const trackId = row.dataset.trackId;
            const idx = parseInt(row.dataset.trackIdx, 10);
            const track = (_musicState.library || []).find(t => t.id === trackId);
            if (action === 'play') {
                if (!isNaN(idx)) _playMusicAlbum(_madCtxAlbumId, { startIndex: idx });
            } else if (action === 'copy-link') {
                const url = track?.url || `https://music.youtube.com/watch?v=${trackId}`;
                try { navigator.clipboard.writeText(url); showToast('Link copied', null, null); } catch (_) {}
            } else if (action === 'remove') {
                try {
                    pywebview.api.remove_from_music_library(trackId).then(() => {
                        showToast('Removed from library', null, null);
                        _refreshMusicLibrary().then(() => {
                            if (_musicState.currentAlbumId) _renderMusicAlbumDetail(_musicState.currentAlbumId);
                        });
                    });
                } catch (_) {}
            }
        }

        function _formatBytesShort(b) {
            if (!b) return '';
            const units = ['B', 'KB', 'MB', 'GB', 'TB'];
            let i = 0;
            while (b >= 1024 && i < units.length - 1) { b /= 1024; i++; }
            return `${b.toFixed(b < 10 ? 1 : 0)} ${units[i]}`;
        }

        // Tracks pagination for the current music search. Reset on every new
        // query so a stale continuation token doesn't leak across searches.
        let _musicSearchContinuation = '';
        let _musicSearchCurrentKind = 'songs';
        let _musicSearchLoadingMore = false;
        async function _runMusicSearch(query, kind) {
            _musicState.searchToken++;
            const token = _musicState.searchToken;
            _musicState.loading = true;
            _musicSearchContinuation = '';
            _musicSearchCurrentKind = kind || 'songs';
            _musicSearchLoadingMore = false;
            _msClearSelection(); // any pending selection from prior search is dropped
            // Foolproof suggestion dismiss — every code path that runs a
            // search (Enter, Search button click, chip click, programmatic)
            // funnels through here, so closing the dropdown at this single
            // point fixes "suggestions stay open after pressing Enter".
            // Belt-and-suspenders: re-hide a few times in case a debounced
            // suggestion fetch or focus handler tries to re-open it
            // milliseconds after we hide (the bug we kept chasing).
            const _sb = document.getElementById('music-search-suggestions');
            if (_sb) {
                _sb.setAttribute('hidden', '');
                setTimeout(() => _sb.setAttribute('hidden', ''), 100);
                setTimeout(() => _sb.setAttribute('hidden', ''), 300);
                setTimeout(() => _sb.setAttribute('hidden', ''), 600);
            }
            const status = document.getElementById('music-search-status');
            const results = document.getElementById('music-results');
            const foryou = document.getElementById('music-foryou');
            const meta = document.getElementById('music-results-meta');
            // Hide the For-You landing while a search is running/active
            if (foryou) foryou.innerHTML = '';
            if (results) results.innerHTML = '';
            if (status) {
                status.removeAttribute('hidden');
                status.innerHTML = '<div>Searching YouTube Music…</div>';
            }
            if (meta) meta.textContent = '';
            // Record the search query for the "recent searches" row
            try { pywebview.api.record_music_search(query); } catch (_) {}
            try {
                const res = await pywebview.api.search_youtube_music(query, kind || 'songs');
                if (token !== _musicState.searchToken) return;
                if (!res || res.error) {
                    if (status) status.innerHTML = `<div style="color:#dc2626;">${res?.error || 'Search failed.'}</div>`;
                    return;
                }
                const list = res.results || [];
                if (!list.length) {
                    if (status) status.innerHTML = `<div>No results for "${query}".</div>`;
                    return;
                }
                if (status) status.setAttribute('hidden', '');
                if (meta) meta.textContent = `${list.length} result${list.length === 1 ? '' : 's'}`;
                _musicSearchContinuation = res.continuation || '';
                _renderMusicResults(list);
                _ensureMusicSearchInfiniteScroll();
            } catch (e) {
                if (status) status.innerHTML = '<div style="color:#dc2626;">Search failed.</div>';
            } finally {
                if (token === _musicState.searchToken) _musicState.loading = false;
            }
        }

        // Append a "loading more" sentinel + IntersectionObserver below the
        // results list so we fetch the next continuation page when the user
        // scrolls near the bottom. Idempotent — safe to call after every
        // render that updates _musicSearchContinuation.
        let _musicSearchScrollObserver = null;
        function _ensureMusicSearchInfiniteScroll() {
            const results = document.getElementById('music-results');
            if (!results) return;
            let sentinel = results.querySelector('.music-search-sentinel');
            if (!_musicSearchContinuation) {
                // No more pages — drop the sentinel if present and disconnect.
                if (sentinel) sentinel.remove();
                if (_musicSearchScrollObserver) { _musicSearchScrollObserver.disconnect(); _musicSearchScrollObserver = null; }
                return;
            }
            if (!sentinel) {
                sentinel = document.createElement('div');
                sentinel.className = 'music-search-sentinel';
                sentinel.style.cssText = 'height: 24px; display: flex; align-items: center; justify-content: center; color: #6e6e6e; font-size: 12px;';
                results.appendChild(sentinel);
            } else {
                // Move sentinel back to the bottom in case it got displaced.
                results.appendChild(sentinel);
            }
            if (_musicSearchScrollObserver) _musicSearchScrollObserver.disconnect();
            _musicSearchScrollObserver = new IntersectionObserver(async (entries) => {
                if (!entries.some(e => e.isIntersecting)) return;
                if (_musicSearchLoadingMore || !_musicSearchContinuation) return;
                _musicSearchLoadingMore = true;
                sentinel.textContent = 'Loading more…';
                const myToken = _musicState.searchToken;
                try {
                    const res = await pywebview.api.search_youtube_music_continuation(
                        _musicSearchContinuation, _musicSearchCurrentKind);
                    if (myToken !== _musicState.searchToken) return;   // user typed a new query
                    if (!res || res.error) {
                        sentinel.textContent = res?.error ? `Error: ${res.error}` : 'Failed to load more.';
                        return;
                    }
                    const list = res.results || [];
                    _musicSearchContinuation = res.continuation || '';
                    if (list.length) {
                        _renderMusicResults(list, /* append */ true);
                    }
                    if (!_musicSearchContinuation) {
                        sentinel.remove();
                        if (_musicSearchScrollObserver) { _musicSearchScrollObserver.disconnect(); _musicSearchScrollObserver = null; }
                    } else {
                        sentinel.textContent = '';
                        // Move sentinel back to the bottom after rendering more rows.
                        results.appendChild(sentinel);
                    }
                } catch (e) {
                    sentinel.textContent = 'Failed to load more.';
                } finally {
                    _musicSearchLoadingMore = false;
                }
            }, { root: results, rootMargin: '300px' });
            _musicSearchScrollObserver.observe(sentinel);
        }

        // Show the For-You landing (called when the search pane mounts with no active query).
        async function _renderMusicForYou() {
            const wrap = document.getElementById('music-foryou');
            if (!wrap) return;
            const status = document.getElementById('music-search-status');
            if (status) status.setAttribute('hidden', '');
            wrap.innerHTML = '<div class="music-search-status" style="padding:24px;"><div>Loading…</div></div>';
            let data;
            try { data = await pywebview.api.get_music_for_you(); } catch (e) { wrap.innerHTML = ''; return; }
            if (!data) { wrap.innerHTML = ''; return; }
            const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
            const playSvg = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l12-7z"/></svg>';
            const clockSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg>';
            let html = '';

            // Recent searches
            if (data.recent_searches && data.recent_searches.length) {
                html += '<div class="myf-section"><div class="myf-head"><h3>Recent searches</h3></div><div class="myf-chips">';
                html += data.recent_searches.map(q => `<button class="myf-chip" data-myf-query="${esc(q)}">${clockSvg}${esc(q)}</button>`).join('');
                html += '</div></div>';
            }

            // Recently added (library)
            if (data.recent_library && data.recent_library.length) {
                html += '<div class="myf-section"><div class="myf-head"><h3>Pick up where you left off</h3><span class="myf-sub">from your library</span></div><div class="myf-grid">';
                html += data.recent_library.map(t => {
                    const tAttrs = _musicThumbAttrs(t.thumbnail);
                    const thumb = tAttrs ? `<img ${tAttrs} loading="lazy">` : '';
                    return `<div class="myf-card" data-myf-lib-id="${esc(t.id)}">
                        <div class="myf-card-art">${thumb}<span class="myf-badge">OWNED</span><div class="myf-card-play">${playSvg}</div></div>
                        <div class="myf-card-title">${esc(t.title || 'Untitled')}</div>
                        <div class="myf-card-sub">${esc(t.artist || '')}</div>
                    </div>`;
                }).join('');
                html += '</div></div>';
            }

            // Because you have [Top Artist]
            if (data.top_artist && data.because_you && data.because_you.length) {
                html += `<div class="myf-section"><div class="myf-head"><h3>Because you have ${esc(data.top_artist)}</h3></div><div class="myf-grid">`;
                html += data.because_you.map(t => {
                    const tAttrs = _musicThumbAttrs(t.thumbnail);
                    const thumb = tAttrs ? `<img ${tAttrs} loading="lazy">` : '';
                    return `<div class="myf-card" data-myf-search-id="${esc(t.id)}" data-myf-search-kind="${esc(t.kind || 'song')}">
                        <div class="myf-card-art">${thumb}<div class="myf-card-play">${playSvg}</div></div>
                        <div class="myf-card-title">${esc(t.title || 'Untitled')}</div>
                        <div class="myf-card-sub">${esc(t.artist || '')}</div>
                    </div>`;
                }).join('');
                html += '</div></div>';
            }

            // Trending on YT Music
            if (data.trending && data.trending.length) {
                html += '<div class="myf-section"><div class="myf-head"><h3>Trending on YouTube Music</h3><span class="myf-sub">Updated daily</span></div><div class="myf-grid">';
                html += data.trending.map(t => {
                    const tAttrs = _musicThumbAttrs(t.thumbnail);
                    const thumb = tAttrs ? `<img ${tAttrs} loading="lazy">` : '';
                    return `<div class="myf-card" data-myf-search-id="${esc(t.id)}" data-myf-search-kind="${esc(t.kind || 'song')}">
                        <div class="myf-card-art">${thumb}<div class="myf-card-play">${playSvg}</div></div>
                        <div class="myf-card-title">${esc(t.title || 'Untitled')}</div>
                        <div class="myf-card-sub">${esc(t.artist || '')}</div>
                    </div>`;
                }).join('');
                html += '</div></div>';
            }

            // Shuffled library
            if (data.shuffled_lib && data.shuffled_lib.length) {
                html += '<div class="myf-section"><div class="myf-head"><h3>From your library</h3><span class="myf-sub">shuffled</span></div><div class="myf-grid">';
                html += data.shuffled_lib.map(t => {
                    const tAttrs = _musicThumbAttrs(t.thumbnail);
                    const thumb = tAttrs ? `<img ${tAttrs} loading="lazy">` : '';
                    return `<div class="myf-card" data-myf-lib-id="${esc(t.id)}">
                        <div class="myf-card-art">${thumb}<span class="myf-badge">OWNED</span><div class="myf-card-play">${playSvg}</div></div>
                        <div class="myf-card-title">${esc(t.title || 'Untitled')}</div>
                        <div class="myf-card-sub">${esc(t.artist || '')}</div>
                    </div>`;
                }).join('');
                html += '</div></div>';
            }

            if (!html) {
                html = '<div class="music-search-status" style="padding:60px 20px;"><div>Search YouTube Music to find tracks to download.</div></div>';
            }
            wrap.innerHTML = html;
            // Resolve any pt:thumb: markers (library thumbnails are stored as
            // local-cache markers, not URLs) so the OWNED cards actually show
            // their cover instead of the gray placeholder.
            _resolveMusicThumbMarkers();

            // Wire chip clicks → run that search
            wrap.querySelectorAll('[data-myf-query]').forEach(el => {
                el.addEventListener('click', () => {
                    const q = el.getAttribute('data-myf-query');
                    const input = document.getElementById('music-search-input');
                    if (input) { input.value = q; }
                    const clearBtn = document.getElementById('music-search-clear');
                    if (clearBtn) clearBtn.classList.toggle('visible', !!q);
                    _runMusicSearch(q, _musicState.searchKind);
                });
            });
            // Wire library cards → play
            wrap.querySelectorAll('[data-myf-lib-id]').forEach(el => {
                el.addEventListener('click', () => {
                    const id = el.getAttribute('data-myf-lib-id');
                    const t = (_musicState.library || []).find(x => x.id === id);
                    if (t) _playMusicTrack(t);
                });
            });
            // Wire search-source cards (because you have / trending) → run a search for that title
            // (Could also download directly — but routing to search keeps the click contract honest:
            // download is an explicit weighty action, click previews/explores.)
            wrap.querySelectorAll('[data-myf-search-id]').forEach(el => {
                el.addEventListener('click', () => {
                    const t = el.querySelector('.myf-card-title')?.textContent || '';
                    const a = el.querySelector('.myf-card-sub')?.textContent || '';
                    const q = (t + ' ' + a).trim();
                    if (!q) return;
                    const input = document.getElementById('music-search-input');
                    if (input) input.value = q;
                    const clearBtn = document.getElementById('music-search-clear');
                    if (clearBtn) clearBtn.classList.toggle('visible', true);
                    _runMusicSearch(q, 'songs');
                });
            });
        }

        /* ===== Click contract: selection state + multi-select bar + context menu ===== */
        let _msSelection = new Set();
        let _msLastResults = [];   // cache of last rendered search results for ctx-menu lookup

        function _msSetSelected(row, on) {
            const id = row.dataset.trackId;
            if (!id) return;
            if (on) { _msSelection.add(id); row.classList.add('selected'); }
            else { _msSelection.delete(id); row.classList.remove('selected'); }
            _msRepaintActionBar();
        }
        function _msToggleSelected(row) {
            _msSetSelected(row, !row.classList.contains('selected'));
        }
        function _msClearSelection() {
            _msSelection.clear();
            document.querySelectorAll('.music-track-row.selected').forEach(r => r.classList.remove('selected'));
            _msRepaintActionBar();
        }
        function _msRepaintActionBar() {
            const bar = document.getElementById('music-actionbar');
            const n = _msSelection.size;
            if (!bar) return;
            if (n > 0) {
                bar.removeAttribute('hidden');
                document.getElementById('mab-count-num').textContent = String(n);
                document.getElementById('mab-download-num').textContent = String(n);
            } else {
                bar.setAttribute('hidden', '');
            }
        }
        function _msBindActionBar() {
            const bar = document.getElementById('music-actionbar');
            if (!bar || bar._bound) return; bar._bound = true;
            document.getElementById('mab-clear')?.addEventListener('click', _msClearSelection);
            document.getElementById('mab-download-all')?.addEventListener('click', () => {
                // Mixed-kind dispatch: songs/videos go through add_music_track,
                // album/artist/playlist rows go through add_music_collection.
                // Mirrors the video search's "select results → push batch to
                // queue" UX so a user can multi-select songs + albums and
                // queue them all in one button press.
                const ids = Array.from(_msSelection);
                let songsQueued = 0, collectionsQueued = 0;
                ids.forEach(id => {
                    const safe = (window.CSS && CSS.escape) ? CSS.escape(id) : id.replace(/"/g, '\\"');
                    const row = document.querySelector(`.music-track-row[data-track-id="${safe}"]`);
                    if (!row) return;
                    const kind = row.dataset.trackKind;
                    if (kind === 'album' || kind === 'artist' || kind === 'playlist') {
                        if (!row.classList.contains('downloading')) {
                            _bulkDownloadFromRow(row);
                            collectionsQueued++;
                        }
                    } else {
                        if (!row.classList.contains('in-library') && !row.classList.contains('downloading')) {
                            _addMusicTrackFromRow(row);
                            songsQueued++;
                        }
                    }
                });
                _msClearSelection();
                // Single aggregated toast — _addMusicTrackFromRow + _bulkDownloadFromRow
                // each fire their own toasts for finer-grained per-item feedback,
                // but a summary line helps when 10+ items were queued at once.
                if (songsQueued + collectionsQueued > 1) {
                    const parts = [];
                    if (songsQueued) parts.push(`${songsQueued} track${songsQueued === 1 ? '' : 's'}`);
                    if (collectionsQueued) parts.push(`${collectionsQueued} collection${collectionsQueued === 1 ? '' : 's'}`);
                    showToast(`Queued ${parts.join(' + ')}`, null, null);
                }
            });
        }
        function _msBindKeyboard() {
            if (document._msKeyBound) return;
            document._msKeyBound = true;
            document.addEventListener('keydown', (e) => {
                if (e.key === 'Escape' && _msSelection.size > 0) {
                    const input = document.getElementById('music-search-input');
                    if (document.activeElement !== input) {
                        _msClearSelection();
                    }
                }
            });
        }

        /* Right-click context menu */
        let _mctxTargetRow = null;
        function _showMusicCtx(x, y, row) {
            const ctx = document.getElementById('music-ctx');
            if (!ctx) return;
            _mctxTargetRow = row;
            ctx.removeAttribute('hidden');
            // Position, clamping inside viewport
            const w = 240, h = ctx.offsetHeight || 200;
            const vw = window.innerWidth, vh = window.innerHeight;
            const left = Math.min(x, vw - w - 8);
            const top = Math.min(y, vh - h - 8);
            ctx.style.left = `${left}px`;
            ctx.style.top = `${top}px`;
        }
        function _hideMusicCtx() {
            const ctx = document.getElementById('music-ctx');
            if (ctx) ctx.setAttribute('hidden', '');
            _mctxTargetRow = null;
        }
        function _initMusicCtx() {
            const ctx = document.getElementById('music-ctx');
            if (!ctx || ctx._bound) return; ctx._bound = true;
            ctx.querySelectorAll('.mctx-item').forEach(btn => {
                btn.addEventListener('click', () => {
                    const action = btn.getAttribute('data-mctx');
                    const row = _mctxTargetRow;
                    _hideMusicCtx();
                    if (!row) return;
                    const id = row.dataset.trackId;
                    const url = row.dataset.trackUrl;
                    const kind = row.dataset.trackKind;
                    if (action === 'download') {
                        if (kind === 'album' || kind === 'playlist' || kind === 'artist') {
                            _bulkDownloadFromRow(row);
                        } else if (!row.classList.contains('in-library') && !row.classList.contains('downloading')) {
                            _addMusicTrackFromRow(row);
                        }
                    } else if (action === 'add-to-selection') {
                        _msSetSelected(row, true);
                    } else if (action === 'copy-link') {
                        try { navigator.clipboard.writeText(url || ''); showToast('Link copied', null, null); } catch (_) {}
                    } else if (action === 'open-external') {
                        try { pywebview.api.open_external_url(url); } catch (_) {}
                    }
                });
            });
            // Click anywhere else → close
            document.addEventListener('mousedown', (e) => {
                if (!ctx.hasAttribute('hidden') && !ctx.contains(e.target)) _hideMusicCtx();
            });
            window.addEventListener('blur', _hideMusicCtx);
            window.addEventListener('resize', _hideMusicCtx);
        }

        // Bulk download (album / playlist / artist) — replaces the old "open externally" path.
        // Adds the same .downloading visual the per-track + button uses, so the user
        // gets immediate feedback on the row itself (not just a toast). For albums,
        // updateMusicAlbumProgress drives the ring + flips to in-library on complete.
        // Playlists/artists don't have a collection entity yet (only albums do), so
        // their rows stay in the downloading visual until the search re-renders or
        // the user navigates — see the TODO at the end.
        function _bulkDownloadFromRow(row) {
            const id = row.dataset.trackId;
            const kind = row.dataset.trackKind;
            const label = kind === 'album' ? 'album' : kind === 'artist' ? 'artist' : 'playlist';
            if (!id) {
                showToast(`Can't resolve this ${label}`, null, null);
                return;
            }
            if (row.classList.contains('downloading')) return;   // already in flight, no-op
            row.classList.add('downloading');
            _setRowProgress(row, 0);
            // No immediate toast — the backend resolves the collection async
            // and emits "Added N tracks to download queue." once it knows N.
            try { pywebview.api.add_music_collection(id, kind); } catch (e) {
                showToast(`Bulk download failed: ${e?.message || e}`, null, null);
                row.classList.remove('downloading');
            }
            // TODO(v1.4): extend music_albums to cover playlists + artists so
            // updateMusicAlbumProgress drives all three. Today only albums get the
            // aggregate-progress event; playlist/artist rows show "downloading" but
            // don't visibly progress past 0%.
        }

        function _renderMusicResults(list, append) {
            const wrap = document.getElementById('music-results');
            if (!wrap) return;
            const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
            const plusSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>';
            const checkSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
            const openSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>';
            const playSvg = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l12-7z"/></svg>';
            // Progress ring around the 56px artwork. r=26 → circumference 2π·26 ≈ 163.36
            const ringSvg = `
                <svg class="mtr-art-ring" viewBox="0 0 56 56">
                    <circle class="ring-bg" cx="28" cy="28" r="26"/>
                    <circle class="ring-fg" cx="28" cy="28" r="26" stroke-dasharray="163.36" stroke-dashoffset="163.36"/>
                </svg>
                <span class="mtr-art-pct">0%</span>`;
            const rowsHtml = list.map(r => {
                const isSong = r.kind === 'song' || r.kind === 'video';
                const inLib = r.in_library;
                const downloading = _musicState.downloading.has(r.id);
                const cls = inLib ? 'in-library' : (downloading ? 'downloading' : '');
                const thumb = r.thumbnail ? `<img src="${esc(r.thumbnail)}" loading="lazy" />` : '';
                const sub = r.album
                    ? `${esc(r.artist)} · <span style="color:#a0a0a0;">${esc(r.album)}</span>`
                    : esc(r.subtitle || r.artist || '');
                // Songs/videos: + (or check). Albums/artists/playlists: external-open icon.
                let buttonIcon, buttonTitle;
                if (!isSong) {
                    buttonIcon = openSvg;
                    buttonTitle = 'Open in YouTube Music';
                } else if (inLib) {
                    buttonIcon = checkSvg;
                    buttonTitle = 'Already in library';
                } else if (downloading) {
                    buttonIcon = plusSvg;
                    buttonTitle = 'Downloading…';
                } else {
                    buttonIcon = plusSvg;
                    buttonTitle = 'Add to music library';
                }
                const checkInner = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
                return `
                    <div class="music-track-row ${cls}" data-track-id="${esc(r.id)}" data-track-url="${esc(r.url)}" data-track-kind="${esc(r.kind || '')}">
                        <span class="mtr-check">${checkInner}</span>
                        <div class="mtr-art-wrap">
                            <div class="mtr-art">${thumb}<div class="mtr-play-overlay">${playSvg}</div></div>
                            ${ringSvg}
                        </div>
                        <div class="mtr-meta">
                            <div class="mtr-title">${esc(r.title || 'Untitled')}</div>
                            <div class="mtr-sub">${sub}</div>
                        </div>
                        <div class="mtr-plays">${esc(r.play_count || '')}</div>
                        <div class="mtr-duration">${esc(r.duration_string || '')}</div>
                        <button class="mtr-add" data-add-id="${esc(r.id)}" title="${buttonTitle}">
                            ${buttonIcon}
                        </button>
                    </div>`;
            }).join('');
            if (append) {
                // Strip the sentinel (we re-append it after) and add new rows
                // without clearing the existing results. Used by infinite scroll.
                const oldSentinel = wrap.querySelector('.music-search-sentinel');
                if (oldSentinel) oldSentinel.remove();
                wrap.insertAdjacentHTML('beforeend', rowsHtml);
            } else {
                wrap.innerHTML = rowsHtml;
            }
            // Re-apply in-flight progress to any row whose track is currently downloading.
            wrap.querySelectorAll('.music-track-row.downloading').forEach(row => {
                const pct = _musicDownloadProgress.get(row.dataset.trackId);
                if (typeof pct === 'number') _setRowProgress(row, pct);
            });
            // Cache results for ctx-menu lookups
            _msLastResults = list;
            _msBindActionBar();
            _msBindKeyboard();
            _initMusicCtx();

            wrap.querySelectorAll('.music-track-row').forEach(row => {
                // SINGLE-CLICK row body → toggle selection. ALL kinds (song /
                // album / artist / playlist) are selectable now; the action
                // bar handles the mixed-kind dispatch on "Add to queue".
                // (Previous behavior auto-downloaded albums/playlists on
                // click; user said: "I told you select and push to the
                // download queue not download instantly.")
                row.addEventListener('click', (e) => {
                    if (e.target.closest('.mtr-add')) return;        // download btn has its own handler
                    if (e.target.closest('.mtr-play-overlay')) return; // play overlay handled below
                    if (row.classList.contains('in-library')) {
                        // Already in library → play it directly (small UX win — no point selecting)
                        const t = _musicState.library.find(x => x.id === row.dataset.trackId);
                        if (t) _playMusicTrack(t);
                        return;
                    }
                    _msToggleSelected(row);
                });
                // RIGHT-CLICK row → context menu
                row.addEventListener('contextmenu', (e) => {
                    e.preventDefault();
                    _showMusicCtx(e.clientX, e.clientY, row);
                });
                // Click on the artwork's play overlay → play preview (if in library) — preview-
                // streaming for non-library tracks is a future feature; for now just play if owned.
                const overlay = row.querySelector('.mtr-play-overlay');
                if (overlay) overlay.addEventListener('click', (e) => {
                    e.stopPropagation();
                    const t = _musicState.library.find(x => x.id === row.dataset.trackId);
                    if (t) _playMusicTrack(t);
                });
            });
            // The + (download) button is the EXPLICIT per-row immediate-enqueue
            // action — works for songs OR collections (album/playlist/artist).
            // Bulk + push-to-queue across many selected rows lives on the
            // multi-select action bar's "Add to queue" button.
            wrap.querySelectorAll('.mtr-add').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    const row = btn.closest('.music-track-row');
                    if (!row) return;
                    const kind = row.dataset.trackKind;
                    if (kind === 'album' || kind === 'artist' || kind === 'playlist') {
                        _bulkDownloadFromRow(row);
                        return;
                    }
                    if (row.classList.contains('in-library') || row.classList.contains('downloading')) return;
                    _addMusicTrackFromRow(row);
                });
            });
        }

        function _openMusicExternal(row) {
            const url = row.dataset.trackUrl;
            const kind = row.dataset.trackKind;
            const label = kind === 'album' ? 'Album' : kind === 'artist' ? 'Artist' : 'Playlist';
            if (!url) {
                showToast(`${label} link unavailable`, null, null);
                return;
            }
            showToast(`${label} downloads aren't supported yet — opening in YouTube Music.`, null, null);
            try { pywebview.api.open_external_url(url); } catch (_) {}
        }

        function _addMusicTrackFromRow(row) {
            const id = row.dataset.trackId;
            if (!id) return;
            _musicState.downloading.add(id);
            row.classList.add('downloading');
            // Reset ring to 0 — the search-row ring still tracks per-track
            // progress via updateMusicDownload events. The queue ALSO tracks
            // it via updateMusicQueue; both surfaces stay in sync.
            _setRowProgress(row, 0);
            (async () => {
                try {
                    const res = await pywebview.api.add_music_track(id);
                    if (res && res.queued) {
                        showToast('Added to download queue.', null, null);
                    } else if (res && res.already_queued) {
                        showToast('Already in download queue.', null, null);
                    } else if (res && res.already_in_library) {
                        showToast('Already in your music library.', null, null);
                        _musicState.downloading.delete(id);
                        row.classList.remove('downloading');
                    } else if (res && res.error) {
                        throw new Error(res.error);
                    }
                } catch (e) {
                    showToast('Music download failed: ' + (e?.message || e), null, null);
                    _musicState.downloading.delete(id);
                    row.classList.remove('downloading');
                }
            })();
        }

        function _setRowProgress(row, pct) {
            const ring = row.querySelector('.mtr-art-ring .ring-fg');
            const text = row.querySelector('.mtr-art-pct');
            const p = Math.max(0, Math.min(100, pct));
            // Circumference of r=26: 2π·26 = 163.36
            if (ring) ring.setAttribute('stroke-dashoffset', String(163.36 * (1 - p / 100)));
            if (text) text.textContent = `${Math.round(p)}%`;
        }

        // Per-id download progress %, used to repaint rings when a search re-renders
        // mid-download (so the new row picks up the in-flight progress).
        let _musicDownloadProgress = new Map();

        window.updateMusicDownload = function(videoId, pct) {
            const pctRound = Math.round(pct);
            _musicDownloadProgress.set(videoId, pctRound);
            const safe = (window.CSS && CSS.escape) ? CSS.escape(videoId) : videoId.replace(/"/g, '\\"');
            // Search row progress (the ring around the cover)
            document.querySelectorAll(`.music-track-row[data-track-id="${safe}"]`).forEach(row => {
                _setRowProgress(row, pct);
            });
            // Queue row progress — surgical in-place update so we don't trigger
            // a full _renderMusicQueue() on every tick (was the source of the
            // flicker the user reported). Update text + bar width only.
            document.querySelectorAll(`.mq-row[data-mq-id="${safe}"]`).forEach(row => {
                const txt = row.querySelector('.mq-progress-text');
                if (txt) txt.textContent = `${pctRound}%`;
                const bar = row.querySelector('.mq-progress-bar');
                if (bar) bar.style.width = `${pctRound}%`;
                // Also keep the in-memory cache fresh so a later full render
                // doesn't snap back to 0%.
                for (const e of _musicQueueCache) {
                    if (e.id === videoId) { e.progress = pctRound; break; }
                }
            });
            // Album header aggregate (sum / average across this album's tracks)
            for (const e of _musicQueueCache) {
                if (e.id !== videoId || !e.album_id) continue;
                const albumEntries = _musicQueueCache.filter(x => x.album_id === e.album_id);
                if (albumEntries.length < 2) break;
                const total = albumEntries.length;
                const totalProgress = albumEntries.reduce(
                    (acc, x) => acc + (x.status === 'done' ? 100 : (x.progress || 0)), 0);
                const aggPct = Math.round(totalProgress / total);
                const aidSafe = (window.CSS && CSS.escape) ? CSS.escape(e.album_id) : e.album_id.replace(/"/g, '\\"');
                document.querySelectorAll(`.mq-album-pl-row[data-mq-group="${aidSafe}"]`).forEach(albRow => {
                    const albBar = albRow.querySelector('.playlist-progress-bar');
                    if (albBar) albBar.style.width = `${aggPct}%`;
                });
                break;
            }
        };

        // Dedup set so an album that "completes" multiple times in one session
        // (e.g. user re-downloads, or events fire twice) only toasts once.
        const _albumToastFired = new Set();

        // Cover resolved event — fires right after the first track of an album
        // finishes downloading and ffmpeg extracts the embedded art. Updates
        // local state + repaints the library so the placeholder is replaced
        // with the real cover ASAP, not on next load.
        window.musicAlbumCoverResolved = function(albumId, coverMarker) {
            if (!albumId || !coverMarker) return;
            if (_musicState.albums) {
                for (const a of _musicState.albums) {
                    if (a.id === albumId) {
                        a.cover_url = coverMarker;
                        break;
                    }
                }
            }
            _renderMusicLibrary();
        };

        // Aggregate album progress event from the backend. Fired on each per-track
        // completion AND once at the start (0 of N). Updates the library album
        // card's ring + the detail view's progress note if open. When complete,
        // flips the card out of the downloading state.
        window.updateMusicAlbumProgress = function(albumId, done, total) {
            if (!albumId) return;
            const pct = total > 0 ? Math.round((done / total) * 100) : 0;
            const safe = (window.CSS && CSS.escape) ? CSS.escape(albumId) : albumId.replace(/"/g, '\\"');

            // (1) Search-view rows: same album_id matches the row's data-track-id
            //     (the backend uses the browseId we passed). Paint the ring +
            //     flip to in-library on complete so the user sees feedback in
            //     the surface where they clicked +.
            document.querySelectorAll(`.music-track-row[data-track-id="${safe}"]`).forEach(row => {
                if (!row.classList.contains('downloading') && !row.classList.contains('in-library')) {
                    row.classList.add('downloading');
                }
                _setRowProgress(row, pct);
                if (total > 0 && done >= total) {
                    row.classList.remove('downloading');
                    row.classList.add('in-library');
                    const btn = row.querySelector('.mtr-add');
                    if (btn) {
                        btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
                        btn.title = 'Album in library';
                    }
                }
            });

            // (2) Library: if we don't yet know about this album in local state
            //     (first event for a freshly-started album download), pull from
            //     backend and re-render so the album card appears immediately
            //     instead of after the user manually navigates away and back.
            const album = (_musicState.albums || []).find(a => a.id === albumId);
            if (!album) {
                _refreshMusicLibrary();
                return;
            }
            // Update local state copy so subsequent re-renders pick up the new
            // counts without a full backend reload.
            album.downloaded_count = done;
            album.total_tracks = total || album.total_tracks;
            if (total && done >= total) album.status = 'complete';

            // Update the library album card ring in place (no full re-render
            // needed — re-rendering would scroll-reset the grid).
            const card = document.querySelector(`.music-card[data-album-id="${safe}"]`);
            if (card) {
                const dashOffset = 289.03 * (1 - pct / 100);
                const ringFg = card.querySelector('.mcd-ring-wrap .ring-fg');
                const ringPct = card.querySelector('.mcd-ring-pct');
                if (ringFg) ringFg.setAttribute('stroke-dashoffset', String(dashOffset));
                if (ringPct) ringPct.textContent = `${pct}%`;
                if (total > 0 && done >= total) {
                    card.classList.remove('album-downloading');
                    // One toast per completed album (replaces the per-track
                    // toasts that were spamming for 50-track albums). Use the
                    // album's title from local state so the message reads
                    // 'Album added: <title>' instead of just the album_id.
                    const finishedAlbum = (_musicState.albums || []).find(a => a.id === albumId);
                    const albName = finishedAlbum && finishedAlbum.title ? finishedAlbum.title : 'Album';
                    if (!_albumToastFired.has(albumId)) {
                        _albumToastFired.add(albumId);
                        showToast(`Album added: ${albName}`, null, null);
                    }
                }
            } else {
                // We know about the album but it's not in the DOM (e.g. user is
                // on the search sub-tab, or the grid hasn't rendered yet). The
                // next time the library renders, the updated state is already
                // there. No re-render needed.
            }
            // Update the detail view if it's currently showing this album.
            // Surgically paint the progress bar + note + button-disabled state
            // first (no DOM rebuild → no flicker, no scroll-jump, no lost
            // focus). Only re-render the whole row list when total changes
            // (a new track became downloaded, so a pending row needs to flip).
            if (_musicState.currentAlbumId === albumId && app.currentView === 'music-album-detail') {
                const isDownloading = total > 0 && done < total;
                const note = document.getElementById('mad-progress-note');
                const barWrap = document.getElementById('mad-progress-bar-wrap');
                const barFill = document.getElementById('mad-progress-bar-fill');
                const actionsEl = document.getElementById('mad-actions');
                if (note) {
                    if (isDownloading) {
                        note.hidden = false;
                        note.innerHTML = `Downloading <strong>${done}</strong> of ${total} tracks…`;
                    } else {
                        note.hidden = true;
                        note.textContent = '';
                    }
                }
                if (actionsEl) actionsEl.classList.toggle('is-downloading', isDownloading);
                if (barWrap) {
                    if (isDownloading) barWrap.removeAttribute('hidden');
                    else barWrap.setAttribute('hidden', '');
                }
                if (barFill) barFill.style.width = `${pct}%`;
                // Re-render rows so newly-completed pending stubs flip to
                // their real titles. This is the only thing that requires a
                // rebuild — most updates change just the aggregate state.
                _renderMusicAlbumDetail(albumId);
            }
        };

        window.musicDownloadDone = function(entry) {
            if (!entry || !entry.id) return;
            // Stamp the surgical-update timestamp BEFORE doing the patch so
            // any updateMusicQueue event that arrives concurrently from the
            // backend gets skipped (avoids the album-body thumbnail flicker).
            if (typeof window._markMqSurgical === 'function') window._markMqSurgical();
            _musicState.downloading.delete(entry.id);
            _musicDownloadProgress.delete(entry.id);
            const existing = _musicState.library.findIndex(t => t.id === entry.id);
            if (existing >= 0) _musicState.library[existing] = entry;
            else _musicState.library.unshift(entry);
            _renderMusicLibrary();
            const safe = (window.CSS && CSS.escape) ? CSS.escape(entry.id) : entry.id.replace(/"/g, '\\"');
            // Search-row flip (unchanged).
            document.querySelectorAll(`.music-track-row[data-track-id="${safe}"]`).forEach(row => {
                row.classList.remove('downloading');
                row.classList.add('in-library');
                const btn = row.querySelector('.mtr-add');
                if (btn) {
                    btn.title = 'Already in library';
                    btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
                }
            });
            // Only toast individual tracks. Album tracks are silent here —
            // the per-album toast fires once when the whole album finishes.
            if (!entry.album_id) {
                showToast(`Added: ${entry.title}`, null, null);
            }
            // Surgical queue update — was previously a full _refreshMusicQueue
            // call which rebuilt every <img> in the list and caused the album
            // cover to flicker on each track completion. Now we patch only
            // the affected row + recompute the album header aggregate in place.
            for (const e of _musicQueueCache) {
                if (e.id === entry.id) {
                    e.status = 'done';
                    e.progress = 100;
                    e.thumbnail = entry.thumbnail || e.thumbnail;
                    break;
                }
            }
            const mqRow = document.querySelector(`.mq-row[data-mq-id="${safe}"]`);
            if (mqRow) {
                // Strip the .is-downloading / .is-queued state markers, add done.
                mqRow.classList.remove('is-downloading', 'is-queued', 'is-failed', 'is-cancelled');
                mqRow.classList.add('is-done');
                // Remove inline progress strip.
                const prog = mqRow.querySelector('.mq-progress');
                if (prog) prog.remove();
                // Remove cancel/retry action button.
                const act = mqRow.querySelector('.mq-action');
                if (act) act.remove();
                // Replace the pill (Downloading/Queued/etc.) with the green
                // check badge, matching the done-state visual rendered by
                // rowHtml() so a later full render is consistent.
                const pill = mqRow.querySelector('.mq-status-pill');
                if (pill && !mqRow.querySelector('.mq-done-check')) {
                    const check = document.createElement('span');
                    check.className = 'mq-done-check';
                    check.title = 'Downloaded';
                    check.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
                    pill.replaceWith(check);
                } else if (!pill && !mqRow.querySelector('.mq-done-check')) {
                    // Defensive: if there was no pill, append the check at the end.
                    const check = document.createElement('span');
                    check.className = 'mq-done-check';
                    check.title = 'Downloaded';
                    check.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
                    mqRow.appendChild(check);
                }
            }
            // Update the parent album header's aggregate text + progress bar.
            if (entry.album_id) {
                const albumEntries = _musicQueueCache.filter(x => x.album_id === entry.album_id);
                const total = albumEntries.length;
                const done = albumEntries.filter(x => x.status === 'done').length;
                const aggPct = total ? Math.round((done / total) * 100) : 0;
                const aidSafe = (window.CSS && CSS.escape) ? CSS.escape(entry.album_id) : entry.album_id.replace(/"/g, '\\"');
                document.querySelectorAll(`.mq-album-pl-row[data-mq-group="${aidSafe}"]`).forEach(albRow => {
                    const albBar = albRow.querySelector('.playlist-progress-bar');
                    if (albBar) albBar.style.width = `${aggPct}%`;
                    const albText = albRow.querySelector('.playlist-progress-text');
                    if (albText) albText.textContent = done >= total ? '' : `${done} of ${total} done`;
                    // Update the "downloaded" count in the subtitle line.
                    const sub = albRow.querySelector('.video-meta-line');
                    if (sub) {
                        sub.textContent = sub.textContent.replace(/\d+\/\d+ downloaded/, `${done}/${total} downloaded`);
                    }
                });
            }
            // Refresh the badge count on the Downloads sub-tab.
            _paintMusicQueueBadge();
        };

        // ----- Music download queue (Downloads sub-tab) -----
        // Cached local copy of the queue. Re-fetched from backend on every
        // `updateMusicQueue` event. The backend keeps the persistent state;
        // this is just a render cache.
        let _musicQueueCache = [];

        function _refreshMusicQueue() {
            try {
                Promise.resolve(pywebview.api.get_music_queue())
                    .then(q => {
                        _musicQueueCache = Array.isArray(q) ? q : [];
                        _syncMusicDownloadingFromQueue();
                        _renderMusicQueue();
                        _paintMusicQueueBadge();
                    })
                    .catch(() => {});
            } catch (_) {}
        }

        // Keep `_musicState.downloading` (drives the search-row ring + spinner)
        // in sync with the backend queue. Cancellations/failures from the
        // Downloads tab need to flip search rows back to the idle '+' state.
        function _syncMusicDownloadingFromQueue() {
            try {
                const liveIds = new Set();
                for (const e of _musicQueueCache) {
                    if (e.status === 'queued' || e.status === 'downloading') liveIds.add(e.id);
                }
                if (!_musicState || !_musicState.downloading) return;
                // Remove anyone no longer live
                const toRemove = [];
                _musicState.downloading.forEach(id => {
                    if (!liveIds.has(id)) toRemove.push(id);
                });
                for (const id of toRemove) {
                    _musicState.downloading.delete(id);
                    _musicDownloadProgress.delete(id);
                    const safe = (window.CSS && CSS.escape) ? CSS.escape(id) : id.replace(/"/g, '\\"');
                    document.querySelectorAll(`.music-track-row[data-track-id="${safe}"]`).forEach(row => {
                        // Only clear if not in library (musicDownloadDone owns the in-library flip)
                        if (!row.classList.contains('in-library')) {
                            row.classList.remove('downloading');
                            _setRowProgress(row, 0);
                        }
                    });
                }
                // Add anyone newly live
                liveIds.forEach(id => _musicState.downloading.add(id));
            } catch (_) {}
        }

        // Push event from backend on every queue mutation. Skip the full
        // re-render if a musicDownloadDone surgical update fired in the last
        // 400ms — the DOM is already current, and a duplicate full re-render
        // is exactly what was causing the album-body thumbnail flash on each
        // track completion (every <img> being recreated → browser flicker).
        // User-visible cancellations / failures still trigger updates because
        // they go through this path with no recent surgical update.
        let _mqLastSurgicalAt = 0;
        window.updateMusicQueue = function() {
            if (Date.now() - _mqLastSurgicalAt < 400) return;
            _refreshMusicQueue();
        };
        // Expose a setter the musicDownloadDone handler can stamp.
        window._markMqSurgical = function() { _mqLastSurgicalAt = Date.now(); };

        function _paintMusicQueueBadge() {
            const badge = document.getElementById('music-tab-downloads-badge');
            if (!badge) return;
            const n = _musicQueueCache.filter(e => e.status === 'queued' || e.status === 'downloading').length;
            if (n > 0) {
                badge.textContent = String(n);
                badge.removeAttribute('hidden');
            } else {
                badge.setAttribute('hidden', '');
            }
        }

        function _renderMusicQueue() {
            const list = document.getElementById('mq-list');
            const clearBtn = document.getElementById('mq-clear-done');
            const meta = document.getElementById('mq-meta');
            if (!list) return;
            const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');

            const q = _musicQueueCache;
            // Group by album_id (only when 2+ tracks share one); singletons stay flat.
            const groups = new Map();   // album_id → [entries]
            const singles = [];
            for (const e of q) {
                const aid = e.album_id || '';
                if (!aid) { singles.push(e); continue; }
                if (!groups.has(aid)) groups.set(aid, []);
                groups.get(aid).push(e);
            }
            const albumGroups = [];
            for (const [aid, entries] of groups) {
                if (entries.length >= 2) {
                    albumGroups.push({ album_id: aid, entries });
                } else {
                    // Single track that happens to have an album_id — render flat.
                    singles.push(...entries);
                }
            }

            // Sort: in-progress + queued first (preserve queue order), then done/cancelled/failed
            const orderRank = (e) => {
                if (e.status === 'downloading') return 0;
                if (e.status === 'queued') return 1;
                if (e.status === 'failed') return 2;
                if (e.status === 'cancelled') return 3;
                return 4; // done
            };
            const sortFn = (a, b) => {
                const ra = orderRank(a), rb = orderRank(b);
                if (ra !== rb) return ra - rb;
                return (a.queued_at || 0) - (b.queued_at || 0);
            };
            singles.sort(sortFn);

            // SVG icon templates — kept inline so we don't pay an extra DOM
            // lookup for every row. Same chrome the video queue's status-icon-btn
            // uses, just with our .mq-action wrapper.
            const cancelSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
            const retrySvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>';
            const chevSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>';
            const doneCheckSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';

            const rowHtml = (e) => {
                const status = e.status || 'queued';
                const pct = Math.max(0, Math.min(100, Math.round(e.progress || 0)));
                // Pill text is stable-width for downloading (no live %) so the
                // row layout doesn't reflow on every progress tick. The actual
                // percentage lives in .mq-progress-text below.
                let pillText;
                if (status === 'downloading') pillText = 'Downloading';
                else if (status === 'queued') pillText = 'Queued';
                else if (status === 'done') pillText = '';   // replaced by check badge
                else if (status === 'failed') pillText = 'Failed';
                else if (status === 'cancelled') pillText = 'Cancelled';
                else pillText = status;

                // Done state uses a green check badge instead of a pill — matches
                // the .mtr-add.in-library "already in library" visual the user
                // liked in search results.
                const statusEl = status === 'done'
                    ? `<span class="mq-done-check" title="Downloaded">${doneCheckSvg}</span>`
                    : `<span class="mq-status-pill ${status}">${esc(pillText)}</span>`;

                let action = '';
                if (status === 'queued' || status === 'downloading') {
                    action = `<button class="mq-action cancel" data-mq-cancel="${esc(e.id)}" title="Cancel">${cancelSvg}</button>`;
                } else if (status === 'failed' || status === 'cancelled') {
                    action = `<button class="mq-action retry" data-mq-retry="${esc(e.id)}" title="Retry">${retrySvg}</button>`;
                }
                const progressStrip = status === 'downloading'
                    ? `<div class="mq-progress">
                        <div class="mq-progress-text">${pct}%</div>
                        <div class="mq-progress-bar-bg"><div class="mq-progress-bar" style="width:${pct}%"></div></div>
                      </div>`
                    : '';
                const artAttrs = _musicThumbAttrs(e.thumbnail);
                const art = artAttrs ? `<img ${artAttrs} loading="lazy" />` : '';
                const sub = e.album
                    ? `${esc(e.artist || 'Unknown')} · ${esc(e.album)}`
                    : esc(e.artist || 'Unknown');
                // Inline error: only rendered for failed rows. Click on the row
                // toggles .error-dismissed so the user can collapse it; retry
                // re-renders the row fresh.
                const errorRow = status === 'failed'
                    ? `<div class="mq-error" title="${esc(e.error || 'Download failed')}">${esc(e.error || 'Download failed — click retry to try again.')}</div>`
                    : '';
                return `
                    <div class="mq-row is-${status}" data-mq-id="${esc(e.id)}">
                        <div class="mq-art">${art}</div>
                        <div class="mq-details">
                            <div class="mq-title">${esc(e.title || 'Loading…')}</div>
                            <div class="mq-sub">${sub}</div>
                            ${errorRow}
                            ${progressStrip}
                        </div>
                        ${statusEl}
                        ${action}
                    </div>`;
            };

            const groupHtml = (g) => {
                const entries = g.entries.slice().sort(sortFn);
                const total = entries.length;
                const done = entries.filter(e => e.status === 'done').length;
                const dlInFlight = entries.filter(e => e.status === 'downloading').length;
                const queuedCount = entries.filter(e => e.status === 'queued').length;
                const failed = entries.filter(e => e.status === 'failed').length;
                const totalProgress = entries.reduce((acc, e) => acc + (e.status === 'done' ? 100 : (e.progress || 0)), 0);
                const aggPct = total ? Math.round(totalProgress / total) : 0;
                const albumTitle = entries[0].album || 'Album';
                const albumArtist = entries[0].artist || '';
                const cover = entries.find(e => e.thumbnail)?.thumbnail || '';
                const allDone = done === total;
                const inFlightOrQueued = dlInFlight + queuedCount;
                // Build a video-playlist-row-shaped album header so it inherits
                // the working `.playlist-row` layout (no flex-wrap bug, blue
                // hover state, right-side controls) instead of a bespoke
                // `.mq-album-header` that diverged. Cancel uses `.remove-btn`
                // and the expand toggle uses `.playlist-open-hint` styling.
                const cancelAllBtn = inFlightOrQueued > 0
                    ? `<button type="button" class="remove-btn mq-album-cancel-all" data-mq-cancel-album="${esc(g.album_id)}" title="Cancel remaining">
                         <svg fill="currentColor" viewBox="0 0 20 20"><path d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z"/></svg>
                       </button>`
                    : '';
                // Stable progress text (no live %): in-progress shows "X of N",
                // complete shows nothing (the green check badge below replaces it).
                const progressText = allDone
                    ? ''
                    : (inFlightOrQueued > 0 ? `${done} of ${total} done` : `${done}/${total} done`);
                // Green check badge for complete albums — matches the per-row
                // .mq-done-check visual so single-tracks and albums read the same.
                const albumDoneCheck = allDone
                    ? `<span class="mq-done-check" title="Downloaded">${doneCheckSvg}</span>`
                    : '';
                return `
                    <div class="playlist-row mq-album-pl-row" data-mq-group="${esc(g.album_id)}" data-mq-toggle-group="${esc(g.album_id)}">
                        <div class="mq-album-pl-thumb">${(() => { const a = _musicThumbAttrs(cover); return a ? `<img ${a} loading="lazy" alt="">` : ''; })()}</div>
                        <div class="video-details">
                            <div class="video-heading">${esc(albumTitle)}</div>
                            <div class="video-meta-line">${esc(albumArtist)} · ${done}/${total} downloaded${failed ? ' · ' + failed + ' failed' : ''}</div>
                        </div>
                        <div class="playlist-progress-wrap${allDone ? '' : ' visible'}">
                            <div class="playlist-progress-text">${esc(progressText)}</div>
                            <div class="playlist-progress-bar-bg">
                                <div class="playlist-progress-bar" style="width:${aggPct}%"></div>
                            </div>
                        </div>
                        ${albumDoneCheck}
                        <div class="playlist-open-hint mq-album-toggle" data-tip-right="Expand">
                            <svg fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2.2"><polyline points="6 9 12 15 18 9"/></svg>
                        </div>
                        ${cancelAllBtn}
                    </div>
                    <div class="mq-album-body" data-mq-body="${esc(g.album_id)}">
                        ${entries.map(rowHtml).join('')}
                    </div>`;
            };

            // Persist which album groups are expanded across re-renders.
            const expanded = new Set();
            list.querySelectorAll('.mq-album-pl-row.expanded').forEach(el => {
                expanded.add(el.getAttribute('data-mq-group'));
            });
            // Also persist which failed rows the user has dismissed the
            // inline error on, so a re-render doesn't keep popping it back.
            const dismissed = new Set();
            list.querySelectorAll('.mq-row.error-dismissed').forEach(el => {
                dismissed.add(el.getAttribute('data-mq-id'));
            });

            list.innerHTML = albumGroups.map(groupHtml).join('') + singles.map(rowHtml).join('');

            // Restore expanded state
            list.querySelectorAll('.mq-album-pl-row').forEach(el => {
                if (expanded.has(el.getAttribute('data-mq-group'))) {
                    el.classList.add('expanded');
                }
            });
            // Restore dismissed-error state
            list.querySelectorAll('.mq-row.is-failed').forEach(el => {
                if (dismissed.has(el.getAttribute('data-mq-id'))) {
                    el.classList.add('error-dismissed');
                }
            });

            // Meta line + clear-done visibility
            const totalCount = q.length;
            const activeCount = q.filter(e => e.status === 'queued' || e.status === 'downloading').length;
            const finishedCount = q.filter(e => e.status === 'done' || e.status === 'cancelled').length;
            if (meta) {
                if (totalCount === 0) {
                    meta.textContent = '';
                } else if (activeCount > 0) {
                    meta.textContent = `${activeCount} in queue · ${finishedCount} completed`;
                } else {
                    meta.textContent = `${totalCount} item${totalCount === 1 ? '' : 's'}`;
                }
            }
            if (clearBtn) {
                if (finishedCount > 0) clearBtn.removeAttribute('hidden');
                else clearBtn.setAttribute('hidden', '');
            }

            // Wire row actions
            list.querySelectorAll('[data-mq-cancel]').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    const id = btn.getAttribute('data-mq-cancel');
                    if (!id) return;
                    // Optimistic: remove from local list if queued (will re-render
                    // from backend on the event); downloading stays until backend
                    // confirms cancellation.
                    try { pywebview.api.cancel_music_queue_item(id); } catch (_) {}
                });
            });
            list.querySelectorAll('[data-mq-retry]').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    const id = btn.getAttribute('data-mq-retry');
                    if (!id) return;
                    // Clear the dismissed-error marker so a future failure shows
                    // a fresh inline error.
                    const row = btn.closest('.mq-row');
                    if (row) row.classList.remove('error-dismissed');
                    try { pywebview.api.retry_music_queue_item(id); } catch (_) {}
                });
            });
            list.querySelectorAll('[data-mq-cancel-album]').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    const aid = btn.getAttribute('data-mq-cancel-album');
                    if (!aid) return;
                    try { pywebview.api.cancel_music_album_queued(aid); } catch (_) {}
                });
            });
            list.querySelectorAll('[data-mq-toggle-group]').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    // Don't toggle if the user clicked an action button inside
                    // the row (cancel-all, etc.) — those have their own handler.
                    if (e.target.closest('.remove-btn, .mq-action')) return;
                    btn.classList.toggle('expanded');
                });
            });
            // Click anywhere on a failed row body to collapse the inline error.
            // Excludes the action button so retry still fires.
            list.querySelectorAll('.mq-row.is-failed').forEach(row => {
                row.addEventListener('click', (e) => {
                    if (e.target.closest('.mq-action')) return;
                    row.classList.toggle('error-dismissed');
                });
            });
        }

        // Wire "Clear completed" + prime the badge on app start. The script
        // block evaluates after the HTML is parsed (script at end of body),
        // so the elements exist now. Defer slightly so pywebview.api is up.
        (function _mqInit() {
            const wire = () => {
                const clearBtn = document.getElementById('mq-clear-done');
                if (clearBtn && !clearBtn._wired) {
                    clearBtn._wired = true;
                    clearBtn.addEventListener('click', () => {
                        try { pywebview.api.clear_music_queue_done(); } catch (_) {}
                    });
                }
                // Prime the badge on app start (might already have persisted queue items).
                _refreshMusicQueue();
            };
            // pywebviewready fires when the JS bridge is live; fall back to a
            // short timeout if it has already fired.
            if (window.pywebview && window.pywebview.api) {
                wire();
            } else {
                window.addEventListener('pywebviewready', wire, { once: true });
                setTimeout(wire, 1000);
            }
        })();

        // Backend → frontend bridge fired when _music_collection_worker fails
        // to resolve an album/playlist/artist URL (or finds zero tracks). Today
        // the search row's .downloading visual would stay forever because no
        // per-track event ever fires for a failed resolve. Clear it here + toast.
        window.updateMusicCollectionResolveError = function(collectionId, errorMsg) {
            try {
                if (collectionId) {
                    const safe = (window.CSS && CSS.escape) ? CSS.escape(collectionId) : String(collectionId).replace(/"/g, '\\"');
                    document.querySelectorAll(`.music-track-row[data-track-id="${safe}"]`).forEach(row => {
                        row.classList.remove('downloading');
                    });
                    if (_musicState && _musicState.downloading) {
                        _musicState.downloading.delete(collectionId);
                    }
                }
                const msg = errorMsg ? String(errorMsg) : 'Resolve failed';
                showToast('Couldn’t resolve this — ' + msg, null, null);
            } catch (_) {}
        };

        // ----- Dock + audio playback -----
        // --- Pointer-capture drag helper for any horizontal slider track ---
        // onCommit fires on every move + on release. The caller decides what to do
        // with the 0..1 pct value. We add a `dragging` class to the track so CSS
        // can paint the hover-only fill+thumb state while the pointer is held.
        function _bindHorizontalDrag(el, onCommit) {
            if (!el) return;
            let activePointer = null;
            const commit = (clientX) => {
                const rect = el.getBoundingClientRect();
                const pct = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
                onCommit(pct);
            };
            el.addEventListener('pointerdown', (e) => {
                if (e.button !== undefined && e.button !== 0) return;
                e.preventDefault();
                e.stopPropagation();
                activePointer = e.pointerId;
                el.classList.add('dragging');
                try { el.setPointerCapture(e.pointerId); } catch (_) {}
                commit(e.clientX);
            });
            el.addEventListener('pointermove', (e) => {
                if (activePointer !== e.pointerId) return;
                commit(e.clientX);
            });
            const release = (e) => {
                if (activePointer !== e.pointerId) return;
                activePointer = null;
                el.classList.remove('dragging');
                try { el.releasePointerCapture(e.pointerId); } catch (_) {}
            };
            el.addEventListener('pointerup', release);
            el.addEventListener('pointercancel', release);
            // Stop click bubble — otherwise clicking the dock track propagates to the
            // dock's "click anywhere → open full player" handler.
            el.addEventListener('click', (e) => e.stopPropagation());
        }

        // Apply a slider value (0..1 linear) to audio + paint UI + persist.
        function _applyMusicVolume(slider, persist) {
            const audio = _musicPlayer.audio;
            _musicPlayer.volume = slider;
            if (audio) audio.volume = _audioVolumeFromSlider(slider);
            _paintDockVolume(slider);
            if (persist) {
                try { pywebview.api.set_setting('music_volume', slider); } catch (_) {}
            }
        }

        // Toggle mute. Used by both the dock and player-view mute icons.
        function _musicToggleMute() {
            if (_musicPlayer.volume > 0) {
                _musicPlayer._preMute = _musicPlayer.volume;
                _applyMusicVolume(0, true);
            } else {
                _applyMusicVolume(_musicPlayer._preMute || 0.5, true);
            }
        }

        // Module-level so the `ended` listener can call it.
        function _musicGoAdjacent(delta) {
            const cur = _musicPlayer.currentTrack;
            const lib = _musicState.library;
            if (!cur || !lib.length) return;
            // Shuffle: pick a random other track. We keep a rolling "shuffleOrder"
            // so we don't repeat until the bag empties.
            if (_musicPlayer.shuffle && delta > 0) {
                if (!_musicPlayer.shuffleOrder.length) {
                    _musicPlayer.shuffleOrder = lib.map(t => t.id).filter(id => id !== cur.id);
                    for (let i = _musicPlayer.shuffleOrder.length - 1; i > 0; i--) {
                        const j = Math.floor(Math.random() * (i + 1));
                        [_musicPlayer.shuffleOrder[i], _musicPlayer.shuffleOrder[j]] = [_musicPlayer.shuffleOrder[j], _musicPlayer.shuffleOrder[i]];
                    }
                }
                const nextId = _musicPlayer.shuffleOrder.shift();
                const target = lib.find(t => t.id === nextId);
                if (target) _playMusicTrack(target, { skipViewSwitch: app.currentView !== 'music-player' });
                return;
            }
            const i = lib.findIndex(t => t.id === cur.id);
            if (i < 0) return;
            const ni = (i + delta + lib.length) % lib.length;
            const target = lib[ni];
            if (target) _playMusicTrack(target, { skipViewSwitch: app.currentView !== 'music-player' });
        }

        function _initMusicDock() {
            _musicPlayer.audio = document.getElementById('music-audio');
            const audio = _musicPlayer.audio;
            if (!audio) return;

            audio.volume = _audioVolumeFromSlider(_musicPlayer.volume);

            // Restore persisted state
            try {
                pywebview.api.get_setting('music_volume').then(v => {
                    if (typeof v === 'number' && v >= 0 && v <= 1) {
                        _musicPlayer.volume = v;
                        audio.volume = _audioVolumeFromSlider(v);
                        _paintDockVolume(v);
                    }
                }).catch(() => {});
                pywebview.api.get_setting('music_shuffle').then(v => {
                    if (v === true) { _musicPlayer.shuffle = true; _paintModeButtons(); }
                }).catch(() => {});
                pywebview.api.get_setting('music_repeat').then(v => {
                    if (v === 'all' || v === 'one') {
                        _musicPlayer.repeat = v;
                        if (_musicPlayer.audio) _musicPlayer.audio.loop = (v === 'one');
                        _paintModeButtons();
                    }
                }).catch(() => {});
            } catch (_) {}

            const playBtn = document.getElementById('music-dock-play');
            if (playBtn) playBtn.addEventListener('click', (e) => { e.stopPropagation(); _musicTogglePlay(); });

            audio.addEventListener('play', () => {
                _musicPlayer.playing = true;
                _paintAllPlayIcons(true);
                _startMusicProgressLoop();   // rAF takes over from timeupdate for smooth visuals
                // Audio focus: pause the video player when music starts so
                // both don't play at once. Symmetric with the video <video>
                // element's `play` listener which pauses the music.
                try {
                    const v = document.getElementById('player-video');
                    if (v && !v.paused) v.pause();
                } catch (_) {}
            });
            audio.addEventListener('pause', () => {
                _musicPlayer.playing = false;
                _paintAllPlayIcons(false);
                _stopMusicProgressLoop();
            });
            audio.addEventListener('ended', () => {
                _musicPlayer.playing = false;
                _paintAllPlayIcons(false);
                _stopMusicProgressLoop();
                // Repeat-one: replay current. Otherwise advance — but only loop the
                // library if repeat=all (or shuffle is on, which infinite-loops by design).
                if (_musicPlayer.repeat === 'one') {
                    try { audio.currentTime = 0; audio.play(); } catch (_) {}
                    return;
                }
                const cur = _musicPlayer.currentTrack;
                const lib = _musicState.library;
                if (!cur || !lib.length) return;
                const i = lib.findIndex(t => t.id === cur.id);
                const isLast = i === lib.length - 1;
                if (isLast && _musicPlayer.repeat !== 'all' && !_musicPlayer.shuffle) return;
                _musicGoAdjacent(1);
            });
            audio.addEventListener('timeupdate', () => { _paintAllProgress(); _paintLyricsProgress(); });
            audio.addEventListener('loadedmetadata', _paintAllProgress);
            audio.addEventListener('error', () => {
                const e = audio.error;
                const codes = { 1: 'aborted', 2: 'network', 3: 'decode', 4: 'src not supported' };
                showToast('Audio error: ' + (codes[e?.code] || 'unknown'), null, null);
            });

            // Seek bar — pointer-capture drag, no click handler needed (pointerdown commits too)
            const seek = document.getElementById('music-dock-seek');
            _bindHorizontalDrag(seek, (pct) => {
                if (!audio.duration || !isFinite(audio.duration)) return;
                _musicPlayer.isSeeking = true;
                audio.currentTime = pct * audio.duration;
                _paintDockProgress();
                _paintFullPlayerProgress();
                // Released on next pointerup — release handler below clears the flag
            });
            if (seek) {
                seek.addEventListener('pointerup', () => { _musicPlayer.isSeeking = false; });
                seek.addEventListener('pointercancel', () => { _musicPlayer.isSeeking = false; });
            }

            // Volume — drag + wheel + mute toggle on icon
            const volTrack = document.getElementById('music-dock-vol-track');
            _bindHorizontalDrag(volTrack, (pct) => _applyMusicVolume(pct, true));
            if (volTrack) {
                volTrack.addEventListener('wheel', (e) => {
                    e.preventDefault();
                    const step = 0.05 * (e.deltaY < 0 ? 1 : -1);
                    _applyMusicVolume(Math.max(0, Math.min(1, _musicPlayer.volume + step)), true);
                }, { passive: false });
            }
            const dockVolBtn = document.getElementById('music-dock-vol-btn');
            if (dockVolBtn) dockVolBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                _musicToggleMute();
            });

            // Expand opens full view; close stops + dismisses
            const expandBtn = document.getElementById('music-dock-expand');
            if (expandBtn) expandBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                if (_musicPlayer.currentTrack) app.switchView('music-player');
            });
            const closeBtn = document.getElementById('music-dock-close');
            if (closeBtn) closeBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                _musicCloseAndStop();
            });
            const dock = document.getElementById('music-dock');
            if (dock) {
                dock.addEventListener('click', () => {
                    if (_musicPlayer.currentTrack) app.switchView('music-player');
                });
                // Mouse wheel anywhere over the dock = volume (Spotify behavior)
                dock.addEventListener('wheel', (e) => {
                    // Only when not over the seek bar (would conflict w/ scrub-by-wheel later)
                    if (e.target.closest('.music-dock-seek')) return;
                    e.preventDefault();
                    const step = 0.05 * (e.deltaY < 0 ? 1 : -1);
                    _applyMusicVolume(Math.max(0, Math.min(1, _musicPlayer.volume + step)), true);
                }, { passive: false });
            }

            _paintDockVolume(_musicPlayer.volume);
            _initMusicPlayerView();
        }

        function _initMusicPlayerView() {
            const backBtn = document.getElementById('mp-back');
            // Return to whichever music-side view the user opened the player
            // from (library or album detail). Captured in _playMusicTrack.
            if (backBtn) backBtn.addEventListener('click', () => app.switchView(_musicPlayerReturnTo || 'music'));

            const audio = _musicPlayer.audio;

            const playBtn = document.getElementById('mp-play');
            if (playBtn) playBtn.addEventListener('click', _musicTogglePlay);

            const seek = document.getElementById('mp-seek');
            _bindHorizontalDrag(seek, (pct) => {
                if (!audio || !audio.duration || !isFinite(audio.duration)) return;
                _musicPlayer.isSeeking = true;
                audio.currentTime = pct * audio.duration;
                _paintFullPlayerProgress();
                _paintDockProgress();
            });
            if (seek) {
                seek.addEventListener('pointerup', () => { _musicPlayer.isSeeking = false; });
                seek.addEventListener('pointercancel', () => { _musicPlayer.isSeeking = false; });
            }

            const prev = document.getElementById('mp-prev');
            const next = document.getElementById('mp-next');
            const dockPrev = document.getElementById('music-dock-prev');
            const dockNext = document.getElementById('music-dock-next');
            if (prev) prev.addEventListener('click', () => _musicGoAdjacent(-1));
            if (next) next.addEventListener('click', () => _musicGoAdjacent(1));
            if (dockPrev) dockPrev.addEventListener('click', (e) => { e.stopPropagation(); _musicGoAdjacent(-1); });
            if (dockNext) dockNext.addEventListener('click', (e) => { e.stopPropagation(); _musicGoAdjacent(1); });

            // Fullscreen toggle — same action as F key, with an enter/exit
            // icon swap so the button reflects current state.
            const fsBtn = document.getElementById('mp-fullscreen');
            const fsIcon = document.getElementById('mp-fullscreen-icon');
            const _enterIcon = '<polyline points="4 14 4 20 10 20"/><polyline points="20 10 20 4 14 4"/><line x1="4" y1="20" x2="11" y2="13"/><line x1="20" y1="4" x2="13" y2="11"/>';
            const _exitIcon = '<polyline points="4 10 4 4 10 4"/><polyline points="20 14 20 20 14 20"/><line x1="4" y1="4" x2="11" y2="11"/><line x1="20" y1="20" x2="13" y2="13"/>';
            const paintFsIcon = () => {
                if (fsIcon) fsIcon.innerHTML = _musicFsActive ? _exitIcon : _enterIcon;
                if (fsBtn) fsBtn.title = _musicFsActive ? 'Exit fullscreen (F)' : 'Fullscreen (F)';
            };
            if (fsBtn) {
                fsBtn.addEventListener('click', () => {
                    _musicToggleFs();
                    paintFsIcon();
                });
            }
            // Keep icon in sync when F is pressed instead of clicked.
            document.addEventListener('keydown', (e) => {
                const k = e.key;
                if ((k === 'f' || k === 'F' || k === 'Escape') && app.currentView === 'music-player') {
                    setTimeout(paintFsIcon, 0);
                }
            });

            // Player-view volume — same drag pattern as the dock, plus wheel + mute toggle
            const volTrack = document.getElementById('mp-vol-track');
            _bindHorizontalDrag(volTrack, (pct) => _applyMusicVolume(pct, true));
            if (volTrack) {
                volTrack.addEventListener('wheel', (e) => {
                    e.preventDefault();
                    const step = 0.05 * (e.deltaY < 0 ? 1 : -1);
                    _applyMusicVolume(Math.max(0, Math.min(1, _musicPlayer.volume + step)), true);
                }, { passive: false });
            }
            const volIcon = document.getElementById('mp-vol-icon');
            if (volIcon) volIcon.addEventListener('click', (e) => {
                e.stopPropagation();
                _musicToggleMute();
            });

            // Shuffle + repeat — real
            const shuffle = document.getElementById('mp-shuffle');
            const repeat = document.getElementById('mp-repeat');
            if (shuffle) shuffle.addEventListener('click', () => {
                _musicPlayer.shuffle = !_musicPlayer.shuffle;
                _musicPlayer.shuffleOrder = [];  // reset bag
                _paintModeButtons();
                _paintMpPanel();   // Up Next changes with shuffle
                try { pywebview.api.set_setting('music_shuffle', _musicPlayer.shuffle); } catch (_) {}
            });
            if (repeat) repeat.addEventListener('click', () => {
                const order = ['off', 'all', 'one'];
                _musicPlayer.repeat = order[(order.indexOf(_musicPlayer.repeat) + 1) % 3];
                // Repeat-one is driven by the native HTMLMediaElement.loop flag — the
                // browser loops the current track seamlessly with no `ended` round-trip.
                // The old path ("on ended: currentTime=0; play()") swallowed the
                // play() promise rejection, so repeat silently did nothing — that was
                // the user's "repeat button does nothing" bug.
                if (_musicPlayer.audio) _musicPlayer.audio.loop = (_musicPlayer.repeat === 'one');
                _paintModeButtons();
                _paintMpPanel();
                try { pywebview.api.set_setting('music_repeat', _musicPlayer.repeat); } catch (_) {}
            });

            _paintModeButtons();
            _initMpPanel();

            // --- Keyboard shortcuts (approved binding table 2026-05-16) ---
            // Fires only when the music-player view is active. The video player has
            // its own scoped handler at the top of the file; we deliberately don't
            // share one so each can evolve independently.
            //
            // Input-field guard is mandatory — without it, typing 'n' in a URL box
            // would skip a track. Same trap docs/agents flagged.
            document.addEventListener('keydown', (e) => {
                const onFullPlayer = app.currentView === 'music-player';
                const dockVisible = document.body.classList.contains('music-dock-visible');
                if (!onFullPlayer && !dockVisible) return;
                const t = e.target;
                if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;

                const ctrl = e.ctrlKey || e.metaKey;
                const shift = e.shiftKey;
                const k = e.key;

                // Play / pause — Space (universal) and K (YouTube alias).
                // Spacebar works whenever the dock is visible (any view); the
                // other shortcuts (seek, volume, next/prev, etc.) only fire on
                // the full music-player view to avoid stealing keys from other
                // surfaces during browsing.
                if (((k === ' ' || e.code === 'Space') || ((k === 'k' || k === 'K') && !ctrl && !shift))) {
                    e.preventDefault(); _musicTogglePlay(); return;
                }
                if (!onFullPlayer) return;   // remaining bindings are player-view-only
                // Seek ±5s — arrows. ±10s — J / L (YouTube power-user aliases).
                if (k === 'ArrowLeft'  && !ctrl) { e.preventDefault(); _musicSeekBy(-5);  return; }
                if (k === 'ArrowRight' && !ctrl) { e.preventDefault(); _musicSeekBy( 5);  return; }
                if ((k === 'j' || k === 'J') && !ctrl && !shift) { e.preventDefault(); _musicSeekBy(-10); return; }
                if ((k === 'l' || k === 'L') && !ctrl && !shift) { e.preventDefault(); _musicSeekBy( 10); return; }
                // Volume ±5%.
                if (k === 'ArrowUp'   && !ctrl) { e.preventDefault(); _musicVolStep( 0.05); return; }
                if (k === 'ArrowDown' && !ctrl) { e.preventDefault(); _musicVolStep(-0.05); return; }
                // Mute.
                if ((k === 'm' || k === 'M') && !ctrl && !shift) { e.preventDefault(); _musicToggleMute(); return; }
                // Next / prev — Shift+N / Shift+P (plain N/P would clash if focus ever lands on body during a typing session).
                if (shift && (k === 'N' || k === 'n')) { e.preventDefault(); _musicGoAdjacent( 1); return; }
                if (shift && (k === 'P' || k === 'p')) { e.preventDefault(); _musicGoAdjacent(-1); return; }
                // Fullscreen.
                if ((k === 'f' || k === 'F') && !ctrl && !shift) { e.preventDefault(); _musicToggleFs(); return; }
                // Shuffle — Ctrl+S. Chromium binds Ctrl+S to "Save Page As"; preventDefault stops that.
                if (ctrl && (k === 's' || k === 'S')) {
                    e.preventDefault();
                    _musicPlayer.shuffle = !_musicPlayer.shuffle;
                    _musicPlayer.shuffleOrder = [];
                    _paintModeButtons(); _paintMpPanel();
                    try { pywebview.api.set_setting('music_shuffle', _musicPlayer.shuffle); } catch (_) {}
                    return;
                }
                // Repeat — Shift+R (Ctrl+R reloads the page in Chromium/WebView2).
                if (shift && (k === 'R' || k === 'r')) {
                    e.preventDefault();
                    const order = ['off', 'all', 'one'];
                    _musicPlayer.repeat = order[(order.indexOf(_musicPlayer.repeat) + 1) % 3];
                    _paintModeButtons(); _paintMpPanel();
                    try { pywebview.api.set_setting('music_repeat', _musicPlayer.repeat); } catch (_) {}
                    return;
                }
                // Escape — exit fullscreen if in fullscreen, otherwise let other handlers (modals) win.
                if (k === 'Escape' && _musicFsActive) { e.preventDefault(); _musicToggleFs(); return; }
            });

            // --- Mouse-wheel volume on the right half of the player stage ---
            // User asked for "scroll on the right half of the screen = volume".
            // We listen on the whole player view, then early-return if the wheel
            // event is on the left half (lets the left side stay scroll-free for
            // any future content there) or already over a slider (those have
            // their own wheel handlers).
            const playerView = document.getElementById('music-player-view');
            if (playerView) {
                playerView.addEventListener('wheel', (e) => {
                    // Don't fight elements that have their own wheel/scroll behavior:
                    // the volume track + seek bar (their own wheel listeners) and the
                    // bottom panel (scrollable Up Next list).
                    if (e.target.closest('.mp-vol-track')) return;
                    if (e.target.closest('.mp-seek'))     return;
                    if (e.target.closest('.mp-panel'))    return;
                    const rect = playerView.getBoundingClientRect();
                    const xInView = e.clientX - rect.left;
                    if (xInView < rect.width / 2) return;   // left half = no-op
                    e.preventDefault();
                    const step = 0.05 * (e.deltaY < 0 ? 1 : -1);
                    _applyMusicVolume(Math.max(0, Math.min(1, _musicPlayer.volume + step)), true);
                }, { passive: false });
            }

            _initMusicAlbumDetailView();
        }

        // ---- Music album detail view controls (back / play-all / shuffle / more) ---- //
        let _madBound = false;
        function _initMusicAlbumDetailView() {
            if (_madBound) return; _madBound = true;
            const backBtn = document.getElementById('mad-back');
            if (backBtn) backBtn.addEventListener('click', () => {
                _musicState.currentAlbumId = null;
                app.switchView('music');
            });
            const playAll = document.getElementById('mad-play-all');
            if (playAll) playAll.addEventListener('click', () => {
                if (_musicState.currentAlbumId) {
                    _playMusicAlbum(_musicState.currentAlbumId, { shuffle: false });
                }
            });
            const shuffle = document.getElementById('mad-shuffle');
            if (shuffle) shuffle.addEventListener('click', () => {
                if (_musicState.currentAlbumId) {
                    _playMusicAlbum(_musicState.currentAlbumId, { shuffle: true });
                }
            });
            const moreBtn = document.getElementById('mad-more');
            if (moreBtn) moreBtn.addEventListener('click', () => {
                // Minimal "more" menu — for now, just a confirm-style "delete album".
                if (!_musicState.currentAlbumId) return;
                const album = (_musicState.albums || []).find(a => a.id === _musicState.currentAlbumId);
                if (!album) return;
                const ok = window.confirm(`Delete "${album.title || 'this album'}" from your library and disk?\n\nThis removes all ${album.total_tracks || 0} tracks.`);
                if (!ok) return;
                try {
                    pywebview.api.delete_music_album(album.id, true).then(() => {
                        showToast('Album deleted', null, null);
                        _musicState.currentAlbumId = null;
                        app.switchView('music');
                        _refreshMusicLibrary();
                    });
                } catch (e) { showToast('Delete failed', null, null); }
            });
        }

        // Paint shuffle/repeat active state. Repeat-one shows a tiny "1" badge.
        function _paintModeButtons() {
            const shuffle = document.getElementById('mp-shuffle');
            if (shuffle) shuffle.classList.toggle('mp-mode-active', _musicPlayer.shuffle);
            const repeat = document.getElementById('mp-repeat');
            if (repeat) {
                repeat.classList.toggle('mp-mode-active', _musicPlayer.repeat !== 'off');
                let badge = repeat.querySelector('.mp-repeat-one-badge');
                if (_musicPlayer.repeat === 'one') {
                    if (!badge) {
                        badge = document.createElement('span');
                        badge.className = 'mp-repeat-one-badge';
                        badge.textContent = '1';
                        repeat.appendChild(badge);
                    }
                } else if (badge) {
                    badge.remove();
                }
            }
        }

        function _musicCloseAndStop() {
            const audio = _musicPlayer.audio;
            if (audio) {
                try { audio.pause(); } catch (_) {}
                audio.removeAttribute('src');
                audio.load();
            }
            _musicPlayer.currentTrack = null;
            _musicPlayer.playing = false;
            document.body.classList.remove('music-dock-visible');
            // If we were on the full player view, drop back to the music library.
            if (app.currentView === 'music-player') app.switchView('music');
            _renderMusicLibrary();   // clear the now-playing highlight
        }

        function _paintAllPlayIcons(playing) {
            _paintDockPlayIcon(playing);
            const mpIcon = document.getElementById('mp-play-icon');
            if (mpIcon) {
                mpIcon.innerHTML = playing
                    ? '<rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/>'
                    : '<path d="M8 5v14l12-7z"/>';
            }
        }

        function _paintAllProgress() {
            // While the user is actively dragging a seek thumb, audio.currentTime jumps
            // a few times per second as we commit new positions — re-painting from
            // timeupdate fights the drag. The drag commit handler paints directly instead.
            if (_musicPlayer.isSeeking) return;
            _paintDockProgress();
            _paintFullPlayerProgress();
        }

        // requestAnimationFrame loop driving the seek bars at ~60fps during
        // playback. Replaces the previous timeupdate-only approach, which ran at
        // ~4 fps and depended on CSS `transition: width 0.25s linear` to smooth
        // the discrete jumps — that combination caused the visible stuttering
        // (next jump arrived before transition finished → easing curve restart).
        // rAF gives buttery-smooth motion; CSS transitions on width/left removed
        // because the loop already paints continuously.
        let _musicProgressRaf = null;
        function _startMusicProgressLoop() {
            if (_musicProgressRaf) return;
            const tick = () => {
                _paintAllProgress();
                _musicProgressRaf = requestAnimationFrame(tick);
            };
            _musicProgressRaf = requestAnimationFrame(tick);
        }
        function _stopMusicProgressLoop() {
            if (_musicProgressRaf) {
                cancelAnimationFrame(_musicProgressRaf);
                _musicProgressRaf = null;
            }
            // One last paint so the bar shows the final position when paused/ended.
            _paintAllProgress();
        }

        function _paintFullPlayerProgress() {
            const audio = _musicPlayer.audio;
            if (!audio) return;
            const dur = audio.duration && isFinite(audio.duration) ? audio.duration : 0;
            const cur = audio.currentTime || 0;
            const fmt = (s) => {
                if (!isFinite(s) || s < 0) return '0:00';
                s = Math.floor(s);
                return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
            };
            const curEl = document.getElementById('mp-cur');
            const durEl = document.getElementById('mp-dur');
            const fill = document.getElementById('mp-seek-fill');
            const thumb = document.getElementById('mp-seek-thumb');
            if (curEl) curEl.textContent = fmt(cur);
            if (durEl) durEl.textContent = fmt(dur);
            if (fill && dur > 0) {
                const pct = (cur / dur) * 100;
                fill.style.width = `${pct}%`;
                if (thumb) thumb.style.left = `${pct}%`;
            } else if (fill) {
                fill.style.width = '0%';
                if (thumb) thumb.style.left = '0%';
            }
        }

        async function _playMusicTrack(track, opts) {
            if (!track || !track.id) return;
            const skipViewSwitch = opts && opts.skipViewSwitch;
            _musicPlayer.currentTrack = track;
            // Clear the NEW pill the first time the user actually plays the track.
            // Optimistic local update + persistent backend stamp so it stays cleared.
            if (track.added_at && !track.seen_at) {
                track.seen_at = Math.floor(Date.now() / 1000);
                const libEntry = _musicState.library.find(t => t.id === track.id);
                if (libEntry) libEntry.seen_at = track.seen_at;
                try { pywebview.api.mark_music_seen(track.id); } catch (_) {}
            }
            document.body.classList.add('music-dock-visible');
            // Paint dock immediately (the user might be on another tab when they click).
            _paintDockTrackInfo(track);
            _paintFullPlayerTrackInfo(track);
            // Refresh now-playing highlight on the library grid
            _renderMusicLibrary();
            // Open the full music player view by default — that's the main listening UX.
            // skipViewSwitch=true lets the dock's play/pause re-fire playback without
            // forcing the user out of whatever tab they're on.
            if (!skipViewSwitch && app.currentView !== 'music-player') {
                // Remember origin so the back chevron returns to it — fixes the
                // "play from album detail → back goes to library" bug. Only
                // honor music-side origins; for anything else fall back to the
                // library tab so we don't punt the user into the video player
                // or some unrelated view.
                if (app.currentView === 'music-album-detail' || app.currentView === 'music') {
                    _musicPlayerReturnTo = app.currentView;
                } else {
                    _musicPlayerReturnTo = 'music';
                }
                app.switchView('music-player');
            }
            try {
                const res = await pywebview.api.get_music_stream_url(track.id);
                if (!res || res.error) {
                    showToast(res?.error || 'Could not start playback', null, null);
                    return;
                }
                const audio = _musicPlayer.audio;
                audio.src = res.url;
                // Carry repeat-one across track loads via the native loop flag.
                audio.loop = (_musicPlayer.repeat === 'one');
                // Run the linear slider value through the perceptual log curve before
                // assigning to audio.volume — otherwise track-change resets perceived
                // loudness because the slider's 0.2 becomes 0.2 actual gain (vs the
                // 0.0016 the curve maps it to). That's the "volume doesn't persist"
                // bug.
                audio.volume = _audioVolumeFromSlider(_musicPlayer.volume);
                audio.play().catch(err => showToast('Playback failed: ' + (err?.message || err), null, null));
            } catch (e) {
                showToast('Playback failed: ' + (e?.message || e), null, null);
            }
        }

        function _paintDockTrackInfo(track) {
            const titleEl = document.getElementById('music-dock-track');
            const artistEl = document.getElementById('music-dock-artist');
            const artImg = document.getElementById('music-dock-art-img');
            if (titleEl) titleEl.textContent = track.title || 'Untitled';
            if (artistEl) artistEl.textContent = [track.artist, track.album].filter(Boolean).join(' · ');
            // pt:thumb: markers require backend resolution; can't be assigned
            // straight to src. _setMusicArt handles both URL + marker cases.
            _setMusicArt(artImg, track.thumbnail);
        }

        function _paintFullPlayerTrackInfo(track) {
            const title = document.getElementById('mp-title');
            const artist = document.getElementById('mp-artist');
            const album = document.getElementById('mp-album');
            const art = document.getElementById('mp-art-img');
            const backdrop = document.getElementById('mp-backdrop');
            if (title) title.textContent = track.title || 'Untitled';
            if (artist) artist.textContent = track.artist || '';
            if (album) album.textContent = track.album || '';
            _setMusicArt(art, track.thumbnail);
            _setMusicBackdrop(backdrop, track.thumbnail);
            // Reset lyrics state so the new track re-fetches
            _mpLyricsState = { trackId: null, lines: [], plain: '' };
            _lyricsActiveLine = -1;
            _paintMpPanel();
        }

        // Bottom panel state + render
        let _mpPanelTab = 'upnext';
        let _mpPanelOpen = false;

        // Lyrics state (JS-side cache so we don't re-fetch on every panel repaint)
        let _mpLyricsState = { trackId: null, lines: [], plain: '' };
        let _lyricsActiveLine = -1;

        function _parseLrc(lrc) {
            const out = [];
            for (const raw of (lrc || '').split('\n')) {
                const m = raw.match(/^\[(\d+):(\d+(?:\.\d+)?)\](.*)/);
                if (!m) continue;
                const secs = parseInt(m[1], 10) * 60 + parseFloat(m[2]);
                const text = m[3].trim();
                if (text) out.push({ time: secs, text });
            }
            return out.sort((a, b) => a.time - b.time);
        }

        // Lookahead so the active line lights up slightly before it's sung —
        // matches Apple Music / Spotify behavior. lrclib's timestamps mark when
        // a line STARTS being sung; without a lead, the highlight feels late
        // because the user is just starting to focus on the line as the vocal
        // is already mid-syllable. 0.35s is the sweet spot in testing — early
        // enough to read into the line, late enough that it's not "ahead" of
        // the song.
        const LYRICS_LEAD_SECONDS = 0.7;

        function _paintLyricsProgress() {
            if (_mpPanelTab !== 'lyrics') return;
            const lines = _mpLyricsState.lines;
            if (!lines.length) return;
            const t = (_musicPlayer.audio?.currentTime || 0) + LYRICS_LEAD_SECONDS;
            let active = 0;
            for (let i = 0; i < lines.length; i++) {
                if (lines[i].time <= t) active = i; else break;
            }
            if (active === _lyricsActiveLine) return;
            _lyricsActiveLine = active;
            const body = document.getElementById('mp-tab-body');
            const els = body ? body.querySelectorAll('.mp-lyric-line') : [];
            els.forEach((el, i) => {
                el.classList.toggle('active', i === active);
                el.classList.toggle('near', Math.abs(i - active) === 1);
            });
            // Manual scroll on the panel's container directly — was using
            // scrollIntoView, but that walks up the DOM to find the "nearest
            // scrollable ancestor" and its smooth-scroll animation could
            // propagate into the cockpit grid, shifting the hero composition
            // every time a line changed. scrollTo on the known container is
            // unambiguous: it scrolls THIS box, period.
            const target = els[active];
            if (body && target) {
                const offset = target.offsetTop - body.clientHeight / 2 + target.offsetHeight / 2;
                body.scrollTo({ top: Math.max(0, offset), behavior: 'smooth' });
            }
        }

        function _renderLyrics(body, meta) {
            const { lines, plain } = _mpLyricsState;
            if (lines.length) {
                // Synced
                body.innerHTML = '<div class="mp-lyrics-scroll">' +
                    lines.map(l => `<div class="mp-lyric-line">${l.text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}</div>`).join('') +
                    '</div>';
                if (meta) meta.textContent = 'synced';
                _lyricsActiveLine = -1;
                _paintLyricsProgress(); // immediately highlight the right line
            } else if (plain) {
                // Plain text fallback
                body.innerHTML = `<div class="mp-lyrics-plain">${plain.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}</div>`;
                if (meta) meta.textContent = '';
            } else {
                body.innerHTML = '<div class="mp-lyrics-empty">No lyrics found for this track.</div>';
                if (meta) meta.textContent = '';
            }
        }

        function _setMpPanelOpen(open, persist) {
            _mpPanelOpen = !!open;
            const view = document.getElementById('music-player-view');
            if (view) view.classList.toggle('panel-open', _mpPanelOpen);
            if (persist) {
                try { pywebview.api.set_setting('music_panel_open', _mpPanelOpen); } catch (_) {}
            }
        }

        function _initMpPanel() {
            document.querySelectorAll('.mp-tab').forEach(btn => {
                btn.addEventListener('click', () => {
                    const tab = btn.getAttribute('data-mp-tab');
                    if (!tab) return;
                    _mpPanelTab = tab;
                    document.querySelectorAll('.mp-tab').forEach(b => b.classList.toggle('on', b === btn));
                    _paintMpPanel();
                });
            });
            const handle = document.getElementById('mp-panel-handle');
            if (handle) handle.addEventListener('click', () => _setMpPanelOpen(!_mpPanelOpen, true));
            // Restore prior open/closed preference. Default: collapsed.
            try {
                pywebview.api.get_setting('music_panel_open').then(v => {
                    _setMpPanelOpen(v === true, false);
                }).catch(() => _setMpPanelOpen(false, false));
            } catch (_) { _setMpPanelOpen(false, false); }
            _paintMpPanel();
        }

        // Compute + write the "Up next: <title>" peek shown on the handle pill.
        function _paintMpHandleText() {
            const el = document.getElementById('mp-handle-next');
            if (!el) return;
            const cur = _musicPlayer.currentTrack;
            const lib = _musicState.library || [];
            if (!cur || lib.length <= 1) { el.textContent = '—'; return; }
            let nxt = null;
            if (_musicPlayer.shuffle && _musicPlayer.shuffleOrder.length) {
                nxt = lib.find(t => t.id === _musicPlayer.shuffleOrder[0]);
            } else {
                const i = lib.findIndex(t => t.id === cur.id);
                if (i >= 0) nxt = lib[(i + 1) % lib.length];
            }
            el.textContent = nxt && nxt.id !== cur.id ? (nxt.title || 'Untitled') : '—';
        }

        function _paintMpPanel() {
            _paintMpHandleText();
            const body = document.getElementById('mp-tab-body');
            const meta = document.getElementById('mp-tab-meta');
            if (!body) return;
            const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
            if (_mpPanelTab === 'upnext') {
                const cur = _musicPlayer.currentTrack;
                const lib = _musicState.library || [];
                if (!cur || lib.length <= 1) {
                    body.innerHTML = '<div class="mp-upnext-empty">Nothing queued yet — your library starts playing in order.</div>';
                    if (meta) meta.textContent = '';
                    return;
                }
                let queue = [];
                if (_musicPlayer.shuffle && _musicPlayer.shuffleOrder.length) {
                    queue = _musicPlayer.shuffleOrder.slice(0, 5).map(id => lib.find(t => t.id === id)).filter(Boolean);
                } else {
                    const i = lib.findIndex(t => t.id === cur.id);
                    for (let k = 1; k <= 5; k++) {
                        const t = lib[(i + k) % lib.length];
                        if (t && t.id !== cur.id) queue.push(t);
                    }
                }
                body.innerHTML = '<div class="mp-upnext">' + queue.map(t => {
                    const tAttrs = _musicThumbAttrs(t.thumbnail);
                    const thumb = tAttrs ? `<img ${tAttrs} loading="lazy" />` : '';
                    const dur = (() => {
                        if (t.duration_string) return t.duration_string;
                        if (t.duration) { const s = Math.floor(t.duration); return `${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`; }
                        return '';
                    })();
                    return `<div class="mp-mini-card" data-mp-next-id="${esc(t.id)}">
                        <div class="mp-mini-art">${thumb}</div>
                        <div class="mp-mini-text">
                            <div class="mp-mini-title">${esc(t.title || 'Untitled')}</div>
                            <div class="mp-mini-sub">${esc(t.artist || '')}</div>
                        </div>
                        <div class="mp-mini-dur">${esc(dur)}</div>
                    </div>`;
                }).join('') + '</div>';
                _resolveMusicThumbMarkers();
                if (meta) meta.textContent = `${queue.length} up next${_musicPlayer.shuffle ? ' · shuffle' : ''}${_musicPlayer.repeat !== 'off' ? ' · repeat ' + _musicPlayer.repeat : ''}`;
                body.querySelectorAll('.mp-mini-card').forEach(card => {
                    card.addEventListener('click', () => {
                        const id = card.getAttribute('data-mp-next-id');
                        const t = lib.find(x => x.id === id);
                        if (t) _playMusicTrack(t, { skipViewSwitch: true });
                    });
                });
            } else if (_mpPanelTab === 'lyrics') {
                const cur = _musicPlayer.currentTrack;
                if (!cur) {
                    body.innerHTML = '<div class="mp-lyrics-empty">Play a track to see lyrics.</div>';
                    if (meta) meta.textContent = '';
                } else if (_mpLyricsState.trackId === cur.id) {
                    // Already loaded — just re-render (tab switch, etc.)
                    _renderLyrics(body, meta);
                } else {
                    // Fetch from backend
                    body.innerHTML = '<div class="mp-lyrics-loading">Loading lyrics…</div>';
                    if (meta) meta.textContent = '';
                    const fetchId = cur.id;
                    pywebview.api.get_lyrics(fetchId).then(res => {
                        // Guard: user may have switched track while we were fetching
                        if (_musicPlayer.currentTrack?.id !== fetchId) return;
                        _mpLyricsState = {
                            trackId: fetchId,
                            lines: res.synced ? _parseLrc(res.synced) : [],
                            plain: res.plain || '',
                        };
                        _lyricsActiveLine = -1;
                        if (_mpPanelTab === 'lyrics') _renderLyrics(body, meta);
                    }).catch(() => {
                        if (_mpPanelTab === 'lyrics' && _musicPlayer.currentTrack?.id === fetchId) {
                            body.innerHTML = '<div class="mp-lyrics-empty">Couldn\'t load lyrics.</div>';
                        }
                    });
                }
            } else if (_mpPanelTab === 'related') {
                body.innerHTML = '<div class="mp-related-empty">Related tracks coming soon.</div>';
                if (meta) meta.textContent = '';
            }
        }

        function _musicTogglePlay() {
            const audio = _musicPlayer.audio;
            if (!audio || !audio.src) return;
            if (audio.paused) audio.play().catch(() => {});
            else audio.pause();
        }

        function _paintDockPlayIcon(playing) {
            const icon = document.getElementById('music-dock-play-icon');
            if (!icon) return;
            icon.innerHTML = playing
                ? '<rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/>'
                : '<path d="M8 5v14l12-7z"/>';
        }

        function _paintDockProgress() {
            const audio = _musicPlayer.audio;
            if (!audio) return;
            const dur = audio.duration && isFinite(audio.duration) ? audio.duration : 0;
            const cur = audio.currentTime || 0;
            const fmt = (s) => {
                if (!isFinite(s) || s < 0) return '0:00';
                s = Math.floor(s);
                const m = Math.floor(s / 60);
                return `${m}:${String(s % 60).padStart(2, '0')}`;
            };
            const curEl = document.getElementById('music-dock-cur');
            const durEl = document.getElementById('music-dock-dur');
            const fill = document.getElementById('music-dock-seek-fill');
            const thumb = document.getElementById('music-dock-seek-thumb');
            if (curEl) curEl.textContent = fmt(cur);
            if (durEl) durEl.textContent = fmt(dur);
            if (fill && dur > 0) {
                const pct = (cur / dur) * 100;
                fill.style.width = `${pct}%`;
                if (thumb) thumb.style.left = `${pct}%`;
            } else if (fill) {
                fill.style.width = '0%';
                if (thumb) thumb.style.left = '0%';
            }
        }

        function _paintDockVolume(v) {
            const pct = `${Math.round(v * 100)}%`;
            const dFill = document.getElementById('music-dock-vol-fill');
            const dThumb = document.getElementById('music-dock-vol-thumb');
            if (dFill) dFill.style.width = pct;
            if (dThumb) dThumb.style.left = pct;
            // Also paint the player-view's volume — same value, two surfaces.
            const pFill = document.getElementById('mp-vol-fill');
            const pThumb = document.getElementById('mp-vol-thumb');
            if (pFill) pFill.style.width = pct;
            if (pThumb) pThumb.style.left = pct;
            // Mute icon repaints in both dock + player view: 0 = muted (× lines), else = waves
            const muted = v === 0;
            const innerSvg = muted
                ? '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5" fill="currentColor"/><line x1="22" y1="9" x2="16" y2="15"/><line x1="16" y1="9" x2="22" y2="15"/>'
                : '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5" fill="currentColor"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/>';
            const pSvg = document.querySelector('#mp-vol-icon svg');
            if (pSvg) pSvg.innerHTML = innerSvg;
            const dSvg = document.querySelector('#music-dock-vol-btn svg');
            if (dSvg) dSvg.innerHTML = innerSvg;
        }

        // ==========================================================================
