        // F9 — YouTube search view. Self-contained module bound once to the search
        // view DOM, exposes `initSearchView()` for app.switchView('search') to call.
        // ==========================================================================
        let _searchBound = false;
        let _searchState = {
            query: '',
            kind: 'all',
            continuation: null,       // Innertube continuation token for the next page; null = no more
            loading: false,
            loadingMore: false,
            searchToken: 0,           // bumped on each new search; in-flight load-more's drop their results
            suggestionsTimer: null,
            suggestionsAbort: 0,
            highlightedSuggIdx: -1,
            currentSuggestions: [],
            selectedIds: new Set(),   // video/playlist IDs the user has clicked-to-select
            seenIds: new Set(),       // every result ID we've rendered, for cross-page dedup
        };

        function initSearchView() {
            if (_searchBound) return;
            _searchBound = true;

            const input = document.getElementById('search-input');
            const goBtn = document.getElementById('search-go-btn');
            const suggBox = document.getElementById('search-suggestions');
            const filterRow = document.querySelector('.search-filter-row');
            const wrap = document.getElementById('search-results-wrap');
            const sentinel = document.getElementById('search-loadmore-sentinel');
            const inputClearBtn = document.getElementById('search-clear-btn');

            if (!input || !goBtn || !suggBox || !filterRow || !wrap || !sentinel) return;

            const paintInputClear = () => {
                if (inputClearBtn) inputClearBtn.classList.toggle('visible', !!input.value);
            };

            // ---- Suggestions (debounced) ----
            input.addEventListener('input', () => {
                const q = input.value.trim();
                _searchState.highlightedSuggIdx = -1;
                paintInputClear();
                if (_searchState.suggestionsTimer) clearTimeout(_searchState.suggestionsTimer);
                if (!q) {
                    suggBox.setAttribute('hidden', '');
                    _searchState.currentSuggestions = [];
                    return;
                }
                _searchState.suggestionsTimer = setTimeout(() => fetchSuggestions(q), 150);
            });

            if (inputClearBtn) {
                inputClearBtn.addEventListener('click', () => {
                    input.value = '';
                    paintInputClear();
                    suggBox.setAttribute('hidden', '');
                    _searchState.currentSuggestions = [];
                    // Mirror music-search: X also resets to the For-You-style
                    // landing (recent searches + empty placeholder) so the
                    // user can pick a previous query without retyping.
                    _renderVideoForYou();
                    input.focus();
                });
            }
            paintInputClear();

            // Hide suggestions on blur (with delay so click-on-suggestion still fires)
            input.addEventListener('blur', () => {
                setTimeout(() => suggBox.setAttribute('hidden', ''), 150);
            });
            // Do NOT auto-show suggestions when the input is focused via tab-return —
            // only re-show them when the user actually types. Without this, switching
            // away from Search and back caused the old suggestion dropdown to reappear
            // over the results, blocking them.

            // Keyboard nav for suggestions + Enter to search
            input.addEventListener('keydown', (e) => {
                const visible = !suggBox.hasAttribute('hidden') && _searchState.currentSuggestions.length > 0;
                if (e.key === 'ArrowDown' && visible) {
                    e.preventDefault();
                    _searchState.highlightedSuggIdx = Math.min(_searchState.highlightedSuggIdx + 1, _searchState.currentSuggestions.length - 1);
                    repaintSuggestionHighlight();
                } else if (e.key === 'ArrowUp' && visible) {
                    e.preventDefault();
                    _searchState.highlightedSuggIdx = Math.max(_searchState.highlightedSuggIdx - 1, -1);
                    repaintSuggestionHighlight();
                } else if (e.key === 'Escape' && visible) {
                    suggBox.setAttribute('hidden', '');
                } else if (e.key === 'Enter') {
                    e.preventDefault();
                    let q = input.value.trim();
                    if (visible && _searchState.highlightedSuggIdx >= 0) {
                        q = _searchState.currentSuggestions[_searchState.highlightedSuggIdx];
                        input.value = q;
                    }
                    // Also cancel any in-flight suggestion debounce so it doesn't
                    // re-show suggestions 150ms after Enter dismissed them.
                    if (_searchState.suggestionsTimer) {
                        clearTimeout(_searchState.suggestionsTimer);
                        _searchState.suggestionsTimer = null;
                    }
                    _searchState.currentSuggestions = [];
                    suggBox.setAttribute('hidden', '');
                    input.blur();
                    if (q) runSearch(q, _searchState.kind);
                }
            });

            // Go button
            goBtn.addEventListener('click', () => {
                const q = input.value.trim();
                if (q) runSearch(q, _searchState.kind);
            });

            // Filter chips
            filterRow.querySelectorAll('.search-chip').forEach(chip => {
                chip.addEventListener('click', () => {
                    const kind = chip.getAttribute('data-search-kind');
                    if (kind === _searchState.kind) return;
                    filterRow.querySelectorAll('.search-chip').forEach(c => c.classList.toggle('active', c === chip));
                    _searchState.kind = kind;
                    if (_searchState.query) runSearch(_searchState.query, kind);
                });
            });

            // Infinite scroll — sentinel at the bottom triggers loadMore() if the background
            // prefetch hasn't already filled in the rest. With prefetch usually completing
            // before the user gets to the bottom, this rarely fires.
            const io = new IntersectionObserver((entries) => {
                // Only paginate when ACTUAL search results are showing — never on
                // the For-You landing. Its bottom sentinel was triggering an
                // unwanted "infinite scroll" of the previous query's results.
                const fy = document.getElementById('video-foryou');
                const landingVisible = fy && !fy.hasAttribute('hidden');
                if (landingVisible) return;
                for (const entry of entries) {
                    if (entry.isIntersecting && !_searchState.loading && !_searchState.loadingMore && _searchState.query) {
                        loadMore();
                    }
                }
            }, { root: wrap, rootMargin: '200px' });
            io.observe(sentinel);

            // Bottom action bar — clear deselects all, add fires fetch_url_info for each.
            const clearBtn = document.getElementById('search-action-clear');
            const addBtn = document.getElementById('search-action-add');
            if (clearBtn) clearBtn.addEventListener('click', clearSearchSelection);
            if (addBtn) addBtn.addEventListener('click', addSelectedToQueue);
        }

        async function fetchSuggestions(query) {
            const abortToken = ++_searchState.suggestionsAbort;
            try {
                const res = await pywebview.api.search_youtube_suggestions(query);
                // Stale response — a newer keystroke already kicked off another fetch
                if (abortToken !== _searchState.suggestionsAbort) return;
                const items = (res && Array.isArray(res.suggestions)) ? res.suggestions : [];
                _searchState.currentSuggestions = items;
                _searchState.highlightedSuggIdx = -1;
                renderSuggestions(items, query);
            } catch (_) {
                // Network blip — silently skip; the suggestion endpoint is best-effort.
            }
        }

        function renderSuggestions(items, query) {
            const box = document.getElementById('search-suggestions');
            if (!box) return;
            if (!items.length) { box.setAttribute('hidden', ''); return; }
            const escapeHtml = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
            const ql = (query || '').toLowerCase();
            const iconSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>';
            box.innerHTML = items.map((s, i) => {
                const safe = escapeHtml(s);
                // Bold the prefix that matches the query
                let body = safe;
                if (ql && s.toLowerCase().startsWith(ql)) {
                    body = `<strong>${escapeHtml(s.slice(0, query.length))}</strong>${escapeHtml(s.slice(query.length))}`;
                }
                return `<div class="sugg-item" data-sugg-idx="${i}">${iconSvg}<span>${body}</span></div>`;
            }).join('');
            box.removeAttribute('hidden');
            // Wire clicks
            box.querySelectorAll('.sugg-item').forEach(el => {
                el.addEventListener('mousedown', (e) => {
                    // mousedown beats the input's blur handler so the click registers
                    e.preventDefault();
                    const idx = parseInt(el.getAttribute('data-sugg-idx'), 10);
                    const text = _searchState.currentSuggestions[idx];
                    if (!text) return;
                    const si = document.getElementById('search-input');
                    si.value = text;
                    const cb = document.getElementById('search-clear-btn');
                    if (cb) cb.classList.toggle('visible', !!text);
                    box.setAttribute('hidden', '');
                    runSearch(text, _searchState.kind);
                });
            });
        }

        function repaintSuggestionHighlight() {
            const items = document.querySelectorAll('#search-suggestions .sugg-item');
            items.forEach((el, i) => el.classList.toggle('hover', i === _searchState.highlightedSuggIdx));
        }

        // Video-search landing — mirror of _renderMusicForYou but intentionally
        // limited to data driven by the user's own activity (recent searches
        // + recently-added library items). No algorithmic recommendations.
        // Called on initial search-view mount and on the X clear button.
        async function _renderVideoForYou() {
            // Renders into the dedicated #video-foryou container — NOT the
            // .search-status flex-center box (which squashed multi-section
            // content into a jumbled centered layout). The For-You IS the
            // empty state, so hide the standard "Search YouTube to find…"
            // status block while it's visible.
            const wrap = document.getElementById('video-foryou');
            const status = document.getElementById('search-status');
            const results = document.getElementById('search-results');
            if (results) results.innerHTML = '';
            if (status) status.setAttribute('hidden', '');
            // No infinite-scroll sentinel on the landing (only on real results).
            const _sent = document.getElementById('search-loadmore-sentinel');
            if (_sent) _sent.setAttribute('hidden', '');
            if (!wrap) return;
            wrap.removeAttribute('hidden');
            wrap.innerHTML = '<div class="search-status" style="padding:24px;"><div>Loading…</div></div>';
            let data;
            try { data = await pywebview.api.get_video_for_you(); }
            catch (_) { data = null; }
            const recents = (data && data.recent_searches) || [];
            const recommendations = (data && data.recommendations) || [];
            const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
            const clockSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg>';
            const playSvg = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l12-7z"/></svg>';
            // Recommendation card — library-card style (16:9 thumb on top, title,
            // channel, then view count · date). Crisp thumb via maxresdefault with
            // onerror→hqdefault. Click runs a search seeded by the video title so
            // the user lands on full results (incl. it) and downloads from there.
            const recCardHtml = (r) => {
                const id = esc(r.id || '');
                const stats = [r.view_count_string, r.published_time].filter(Boolean).map(esc).join(' · ');
                const thumb = /^[\w-]{11}$/.test(r.id || '')
                    ? `<img src="https://i.ytimg.com/vi/${id}/maxresdefault.jpg" loading="lazy" alt="" onerror="this.onerror=null;this.src='https://i.ytimg.com/vi/${id}/hqdefault.jpg'">`
                    : (r.thumbnail ? `<img src="${esc(r.thumbnail)}" loading="lazy" alt="">` : '');
                const dur = r.duration_string ? `<div class="library-card-duration">${esc(r.duration_string)}</div>` : '';
                // Reuse the SAME card component as channel/library mode (.library-card)
                // so the recommendations look identical, plus uploader + view·date.
                return `<div class="library-card vfy-rec-card" data-vfy-rec-title="${esc(r.title || '')}">
                    <div class="library-card-thumb">${thumb}${dur}</div>
                    <div class="library-card-body">
                        <div class="library-card-title">${esc(r.title || 'Untitled')}</div>
                        ${r.uploader ? `<div class="pd-channel-card-stats">${esc(r.uploader)}</div>` : ''}
                        ${stats ? `<div class="pd-channel-card-stats">${stats}</div>` : ''}
                    </div>
                </div>`;
            };
            let html = '';
            // Recent searches — chips, each with a × to remove (couldn't clear
            // individual past searches before).
            if (recents.length) {
                html += '<div class="myf-section"><div class="myf-head"><h3>Recent searches</h3></div><div class="myf-chips">';
                html += recents.map(q => `<span class="myf-chip" data-vfy-query="${esc(q)}">${clockSvg}<span class="myf-chip-label">${esc(q)}</span><button class="myf-chip-x" data-vfy-remove="${esc(q)}" title="Remove" aria-label="Remove">&times;</button></span>`).join('');
                html += '</div></div>';
            }
            // ONE "Recommended for you" grid (~2-3 rows) of library-style cards,
            // seeded by your library's top channels + recent searches. NO trending /
            // shuffled / "pick up where you left off".
            if (recommendations.length) {
                html += '<div class="myf-section"><div class="myf-head"><h3>Recommended for you</h3><span class="myf-sub">based on your library</span></div>';
                html += '<div class="vfy-rec-grid">' + recommendations.map(recCardHtml).join('') + '</div></div>';
            }
            if (!html) {
                // No history + no library + no recommendations → show the
                // bare-empty prompt in the status box (not the For-You wrap).
                wrap.innerHTML = '';
                wrap.setAttribute('hidden', '');
                if (status) {
                    status.removeAttribute('hidden');
                    status.innerHTML = '<div class="search-empty-default">Search YouTube to find videos, channels, or playlists to download.</div>';
                }
                return;
            }
            wrap.innerHTML = html;
            // Resolve pt:thumb: markers on the OWNED cards (same path the
            // music For-You uses — backend hands back base64 data URLs).
            if (typeof _resolveMusicThumbMarkers === 'function') {
                _resolveMusicThumbMarkers();
            }
            const _runFromChip = (q) => {
                const input = document.getElementById('search-input');
                if (input) {
                    input.value = q;
                    const cb = document.getElementById('search-clear-btn');
                    if (cb) cb.classList.toggle('visible', !!q);
                }
                runSearch(q, _searchState.kind || 'all');
            };
            // Recent-search chip → run that search (ignoring clicks on the × ).
            wrap.querySelectorAll('.myf-chip[data-vfy-query]').forEach(el => {
                el.addEventListener('click', (e) => {
                    if (e.target.closest('.myf-chip-x')) return;
                    const q = el.getAttribute('data-vfy-query');
                    if (q) _runFromChip(q);
                });
            });
            // Per-chip × → remove from recent searches, then re-render.
            wrap.querySelectorAll('.myf-chip-x[data-vfy-remove]').forEach(el => {
                el.addEventListener('click', async (e) => {
                    e.stopPropagation();
                    const q = el.getAttribute('data-vfy-remove');
                    try { await pywebview.api.remove_video_recent_search(q); } catch (_) {}
                    _renderVideoForYou();
                });
            });
            // Recommendation card → search the video's title so the user lands on
            // full results (incl. it) and downloads from there.
            wrap.querySelectorAll('.vfy-rec-card[data-vfy-rec-title]').forEach(el => {
                el.addEventListener('click', () => {
                    const q = (el.getAttribute('data-vfy-rec-title') || '').trim();
                    if (q) _runFromChip(q);
                });
            });
        }

        async function runSearch(query, kind) {
            _searchState.query = query;
            _searchState.kind = kind || 'all';
            _searchState.continuation = null;
            _searchState.loading = true;
            _searchState.searchToken++;        // invalidate any in-flight loadMore from prior search
            _searchState.selectedIds.clear();
            _searchState.seenIds.clear();
            updateSelectionBar();
            // Record this query for the For-You "Recent searches" chips. Mirrors
            // _runMusicSearch's record_music_search call — without this, the
            // video For-You's chips row never had any data to render.
            try { pywebview.api.record_video_search(query); } catch (_) {}
            // Foolproof suggestion dismiss — see _runMusicSearch for rationale.
            // Re-hide a few times to defeat any debounced/focus-driven re-open.
            const _sb = document.getElementById('search-suggestions');
            if (_sb) {
                _sb.setAttribute('hidden', '');
                setTimeout(() => _sb.setAttribute('hidden', ''), 100);
                setTimeout(() => _sb.setAttribute('hidden', ''), 300);
                setTimeout(() => _sb.setAttribute('hidden', ''), 600);
            }
            // Also cancel any pending suggestion debounce so it doesn't re-show
            // 150ms after we dismiss.
            if (_searchState.suggestionsTimer) {
                clearTimeout(_searchState.suggestionsTimer);
                _searchState.suggestionsTimer = null;
            }
            // Invalidate any ALREADY-DISPATCHED suggestion fetch that's still
            // awaiting the network. Without this, a fetch that resolves AFTER the
            // user pressed Enter calls renderSuggestions → removeAttribute('hidden')
            // and the dropdown pops back over the results (the "suggestions stay
            // after I search" bug). Bumping the abort token makes fetchSuggestions
            // see a stale token and bail before it renders.
            _searchState.suggestionsAbort++;
            // Wipe + hide the For-You landing so its OWNED / Recent / Because-you
            // sections don't bleed into the search-results layout while results
            // are coming in (the bug that caused the jumbled side-by-side
            // layout the user flagged).
            const _vfy = document.getElementById('video-foryou');
            if (_vfy) {
                _vfy.innerHTML = '';
                _vfy.setAttribute('hidden', '');
            }
            // Also wipe any leftover content from #search-status (legacy path).
            const _ss = document.getElementById('search-status');
            if (_ss) _ss.innerHTML = '';
            renderSearchStatus('loading');
            const results = document.getElementById('search-results');
            if (results) results.innerHTML = '';
            const myToken = _searchState.searchToken;
            try {
                const res = await pywebview.api.search_youtube(query, 20, _searchState.kind);
                if (myToken !== _searchState.searchToken) return;   // user moved on
                if (!res || res.error) {
                    renderSearchStatus('error', res?.error || 'Search failed');
                    return;
                }
                const list = res.results || [];
                list.forEach(r => r.id && _searchState.seenIds.add(r.id));
                renderSearchResults(list, /*append*/ false);
                _searchState.continuation = res.continuation || null;
                if (!list.length) {
                    renderSearchStatus('empty', `No results for "${query}".`);
                } else {
                    renderSearchStatus('hidden');
                    updateSearchMeta(list.length);
                    updateSentinelVisibility();
                }
            } catch (e) {
                renderSearchStatus('error', 'Search failed.');
            } finally {
                if (myToken === _searchState.searchToken) _searchState.loading = false;
            }
        }

        async function loadMore() {
            if (_searchState.loadingMore || _searchState.loading) return;
            if (!_searchState.continuation) return;          // no more pages
            _searchState.loadingMore = true;
            const myToken = _searchState.searchToken;
            const sentinel = document.getElementById('search-loadmore-sentinel');
            if (sentinel) {
                sentinel.classList.add('loading');
                sentinel.innerHTML = '<div class="spinner" style="width:14px;height:14px;border:2px solid rgba(255,255,255,0.1);border-top-color:#3b82f6;border-radius:50%;animation:search-spin 0.8s linear infinite;"></div><span>Loading more…</span>';
            }
            try {
                const res = await pywebview.api.search_youtube(
                    _searchState.query, 20, _searchState.kind, _searchState.continuation
                );
                if (myToken !== _searchState.searchToken) return;   // user moved on
                if (!res || res.error) return;
                const fresh = (res.results || []).filter(r => r.id && !_searchState.seenIds.has(r.id));
                fresh.forEach(r => _searchState.seenIds.add(r.id));
                if (fresh.length) {
                    renderSearchResults(fresh, /*append*/ true);
                    updateSearchMeta(_searchState.seenIds.size);
                }
                _searchState.continuation = res.continuation || null;
            } catch (_) { /* soft */ }
            finally {
                if (myToken === _searchState.searchToken) {
                    _searchState.loadingMore = false;
                    if (sentinel) {
                        sentinel.classList.remove('loading');
                        sentinel.innerHTML = '';
                    }
                    updateSentinelVisibility();
                }
            }
        }

        function updateSentinelVisibility() {
            const sentinel = document.getElementById('search-loadmore-sentinel');
            if (!sentinel) return;
            if (_searchState.continuation) sentinel.removeAttribute('hidden');
            else sentinel.setAttribute('hidden', '');
        }

        function renderSearchResults(list, append) {
            const wrap = document.getElementById('search-results');
            const sentinel = document.getElementById('search-loadmore-sentinel');
            if (!wrap) return;
            const escapeHtml = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
            const checkSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
            // SVG icons reused below. Defined once so the template literal stays readable.
            const verifiedSvg = '<svg class="sr-verified" viewBox="0 0 24 24" fill="currentColor" aria-label="Verified"><path d="M12 1l2.36 1.62 2.86-.34.92 2.73 2.49 1.45-.34 2.85L21.94 11l-1.78 2.24.51 2.83-2.55 1.36-1.05 2.68-2.86-.19L12 21.92l-2.21-1.99-2.86.19-1.05-2.68-2.55-1.36.51-2.83L2.06 11l1.71-1.69-.34-2.85L5.92 5l.92-2.73 2.86.34L12 1zm-1.4 13.9l5.5-5.5-1.41-1.42-4.09 4.09-1.59-1.6-1.42 1.42 3 3.01z"/></svg>';
            const html = list.map(r => {
                const isChannel = r.type === 'channel';
                const isPlaylist = r.type === 'playlist';
                const action = (r.in_queue || r.in_library)
                    ? `<span class="sr-already">${checkSvg}Already in your ${r.in_library ? 'library' : 'queue'}</span>`
                    : '';
                const thumbImg = r.thumbnail ? `<img src="${escapeHtml(r.thumbnail)}" loading="lazy" />` : '';

                // Channel cards: circular avatar + name + "@handle · subs" + description.
                // Subscribe button on the right — click immediately queues the channel,
                // no multi-select. Whole card is also clickable as a shortcut.
                if (isChannel) {
                    const subscribeArea = (r.in_queue || r.in_library)
                        ? `<div class="sr-action">${action}</div>`
                        : '<button class="sr-subscribe-btn">Subscribe</button>';
                    return `
                    <div class="search-result-card channel" data-result-id="${escapeHtml(r.id)}" data-result-type="channel" data-result-url="${escapeHtml(r.url)}" data-in-queue="${r.in_queue ? '1' : ''}" data-in-library="${r.in_library ? '1' : ''}">
                        <div class="sr-thumb">${thumbImg}</div>
                        <div class="sr-meta">
                            <div class="sr-title">${escapeHtml(r.title)}</div>
                            ${r.view_count_string ? `<div class="sr-channel">${escapeHtml(r.view_count_string)}</div>` : ''}
                            ${r.uploader ? `<div class="sr-description">${escapeHtml(r.uploader)}</div>` : ''}
                        </div>
                        ${subscribeArea}
                    </div>`;
                }

                // Playlist + Video cards share the bigger 16:9 thumbnail layout. The
                // structure mirrors YouTube's own search results: title → stats row
                // (views · time-ago) → channel row (small avatar + name + ✓ verified)
                // → description snippet. Playlists skip stats/avatar/description and
                // show video count instead.
                const typeBadge = isPlaylist ? '<span class="sr-type-badge">Playlist</span>' : '';
                const durBadge = r.duration_string ? `<span class="sr-duration">${escapeHtml(r.duration_string)}</span>` : '';

                if (isPlaylist) {
                    return `
                    <div class="search-result-card" data-result-id="${escapeHtml(r.id)}" data-result-type="playlist" data-result-url="${escapeHtml(r.url)}" data-in-queue="${r.in_queue ? '1' : ''}" data-in-library="${r.in_library ? '1' : ''}">
                        <div class="sr-thumb">${thumbImg}${typeBadge}${durBadge}</div>
                        <div class="sr-meta">
                            <div class="sr-title">${escapeHtml(r.title)}</div>
                            ${r.uploader ? `<div class="sr-channel">${escapeHtml(r.uploader)}</div>` : ''}
                            ${action ? `<div class="sr-action">${action}</div>` : ''}
                        </div>
                    </div>`;
                }

                // Video card
                const statsLine = [r.view_count_string, r.published_time]
                    .filter(Boolean).map(escapeHtml).join(' · ');
                const channelAvatar = r.channel_thumbnail
                    ? `<img class="sr-channel-avatar" src="${escapeHtml(r.channel_thumbnail)}" loading="lazy" />`
                    : '<div class="sr-channel-avatar sr-channel-avatar-empty"></div>';
                const verified = r.channel_verified ? verifiedSvg : '';
                return `
                <div class="search-result-card" data-result-id="${escapeHtml(r.id)}" data-result-type="video" data-result-url="${escapeHtml(r.url)}" data-in-queue="${r.in_queue ? '1' : ''}" data-in-library="${r.in_library ? '1' : ''}">
                    <div class="sr-thumb">${thumbImg}${durBadge}</div>
                    <div class="sr-meta">
                        <div class="sr-title">${escapeHtml(r.title)}</div>
                        ${statsLine ? `<div class="sr-stats">${statsLine}</div>` : ''}
                        ${r.uploader ? `<div class="sr-channel-row${r.channel_url ? ' sr-channel-clickable' : ''}"${r.channel_url ? ` data-channel-url="${escapeHtml(r.channel_url)}" title="View channel"` : ''}>${channelAvatar}<span class="sr-channel-name">${escapeHtml(r.uploader)}</span>${verified}</div>` : ''}
                        ${r.description ? `<div class="sr-description">${escapeHtml(r.description)}</div>` : ''}
                        ${action ? `<div class="sr-action">${action}</div>` : ''}
                    </div>
                </div>`;
            }).join('');
            if (append) wrap.insertAdjacentHTML('beforeend', html);
            else wrap.innerHTML = html;

            // Sentinel visibility is now driven by whether we still have a continuation token,
            // managed in updateSentinelVisibility() called from runSearch / loadMore.

            // Wire card clicks (whole card = action)
            const newCards = append
                ? wrap.querySelectorAll('.search-result-card:not([data-bound])')
                : wrap.querySelectorAll('.search-result-card');
            newCards.forEach(card => {
                card.setAttribute('data-bound', '1');
                card.addEventListener('click', () => handleSearchResultClick(card));
                // Creator row → open the read-only channel preview, without
                // triggering the card's own video action.
                const chRow = card.querySelector('.sr-channel-row.sr-channel-clickable');
                if (chRow && chRow.dataset.channelUrl) {
                    chRow.addEventListener('click', (e) => {
                        e.stopPropagation();
                        openChannelPreview(chRow.dataset.channelUrl);
                    });
                }
            });
        }

        // Read-only channel preview: fetch a channel and open its detail view
        // WITHOUT saving it to the queue. Reuses the normal channel fetch; the
        // _pendingPreview flag tells handleFullFetch to open the result as a
        // transient (isPreview) entry instead of a persisted queue item.
        function openChannelPreview(channelUrl) {
            if (!channelUrl) return;
            app._pendingPreview = true;
            showToast('Opening channel…', null, null);
            try {
                pywebview.api.fetch_url_info(channelUrl, 'browser', 'none');
            } catch (e) {
                app._pendingPreview = false;
                showToast('Could not open channel', null, null);
            }
        }

        function updateSearchMeta(count) {
            const meta = document.getElementById('search-meta');
            if (meta) meta.textContent = count ? `${count} result${count === 1 ? '' : 's'}` : '';
        }

        function renderSearchStatus(state, msg) {
            const el = document.getElementById('search-status');
            const sentinel = document.getElementById('search-loadmore-sentinel');
            if (!el) return;
            if (state === 'hidden') {
                // Also wipe innerHTML — otherwise leftover For-You content
                // (rendered into this same element by _renderVideoForYou)
                // can reappear if anything later removes the hidden attr.
                // That was the source of the OWNED cards bleeding into the
                // search-results grid during an active search.
                el.setAttribute('hidden', '');
                el.innerHTML = '';
                return;
            }
            el.removeAttribute('hidden');
            if (state === 'loading') {
                el.innerHTML = '<div class="search-loading"><div class="spinner"></div><span id="search-loading-text">Searching YouTube…</span></div>';
                // YouTube search via yt-dlp takes 1-3s. Soften the wait with a friendlier
                // message after 1.2s if it's still going — feels less like a hang.
                setTimeout(() => {
                    const t = document.getElementById('search-loading-text');
                    if (t) t.textContent = 'Almost there — fetching results…';
                }, 1200);
            } else if (state === 'empty') {
                el.innerHTML = `<div class="search-empty-default">${msg || 'No results.'}</div>`;
            } else if (state === 'error') {
                el.innerHTML = `<div class="search-empty-default" style="color:#dc2626;">${msg || 'Search failed.'}</div>`;
            } else {
                el.innerHTML = '<div class="search-empty-default">Search YouTube to find videos, channels, or playlists to download.</div>';
            }
            if (sentinel) sentinel.setAttribute('hidden', '');
            updateSearchMeta(0);
        }

        function handleSearchResultClick(card) {
            const id = card.dataset.resultId;
            const type = card.dataset.resultType;
            const url = card.dataset.resultUrl;
            const inQueue = card.dataset.inQueue === '1';
            const inLibrary = card.dataset.inLibrary === '1';

            // Already in queue/library → jump to that view and highlight the item.
            if (inQueue || inLibrary) {
                const targetView = inLibrary ? 'library' : 'queue';
                app.switchView(targetView);
                setTimeout(() => {
                    const existing = document.getElementById(`item-${id}`);
                    if (existing) {
                        existing.scrollIntoView({ behavior: 'smooth', block: 'center' });
                        existing.classList.add('flash-highlight');
                        setTimeout(() => existing.classList.remove('flash-highlight'), 1500);
                    }
                }, 200);
                return;
            }

            // CHANNELS are NOT selectable — clicking a channel card subscribes immediately
            // (queues every video on the channel). The card transitions inline:
            //   Subscribe → "Fetching channel…" (spinner) → "Channel in queue" (green)
            // No toast — the inline state IS the feedback, and toasts for an action
            // the user just clicked on a card right in front of them are noise.
            // The "Fetching" → "Channel in queue" flip is driven by handleFullFetch
            // (which fires when the backend's fetch_url_info resolves and pushes
            // the channel-as-playlist into the queue).
            if (type === 'channel') {
                if (!url) return;
                card.classList.add('adding');
                const subBtn = card.querySelector('.sr-subscribe-btn');
                try {
                    pywebview.api.fetch_url_info(url, 'browser', 'none');
                    // Stash URL on the card so handleFullFetch can match by URL.
                    card.dataset.fetchingPending = '1';
                    if (subBtn) {
                        subBtn.classList.add('fetching');
                        subBtn.disabled = true;
                        subBtn.innerHTML = '<span class="sub-spin"></span>Fetching channel…';
                    }
                    // Fail-safe: if no handleFullFetch arrives within 60s (offline,
                    // unexpected backend hang), restore the button so the user
                    // isn't stuck staring at "Fetching" forever. The success path
                    // clears data-fetching-pending so this no-ops normally.
                    setTimeout(() => {
                        if (card.dataset.fetchingPending !== '1') return;
                        delete card.dataset.fetchingPending;
                        if (subBtn && subBtn.isConnected) {
                            subBtn.classList.remove('fetching');
                            subBtn.disabled = false;
                            subBtn.textContent = 'Subscribe';
                        }
                    }, 60000);
                } catch (e) {
                    delete card.dataset.fetchingPending;
                    if (subBtn) {
                        subBtn.classList.remove('fetching');
                        subBtn.disabled = false;
                        subBtn.textContent = 'Subscribe';
                    }
                    showToast('Couldn\'t add channel: ' + (e?.message || e), null, null);
                } finally {
                    card.classList.remove('adding');
                }
                return;
            }

            // VIDEOS + PLAYLISTS → toggle selection. The user accumulates picks, then
            // hits "Add to queue" in the bottom action bar to batch-add them.
            if (_searchState.selectedIds.has(id)) {
                _searchState.selectedIds.delete(id);
                card.classList.remove('selected');
            } else {
                _searchState.selectedIds.add(id);
                card.classList.add('selected');
            }
            updateSelectionBar();
        }

        function updateSelectionBar() {
            const bar = document.getElementById('search-action-bar');
            const countEl = document.getElementById('search-action-count');
            const addBtn = document.getElementById('search-action-add');
            const n = _searchState.selectedIds.size;
            if (!bar) return;
            if (n === 0) {
                bar.classList.remove('visible');
            } else {
                bar.classList.add('visible');
                if (countEl) countEl.textContent = `${n} selected`;
                if (addBtn) addBtn.textContent = n === 1 ? 'Add to queue' : `Add ${n} to queue`;
            }
        }

        function clearSearchSelection() {
            _searchState.selectedIds.clear();
            document.querySelectorAll('.search-result-card.selected').forEach(c => c.classList.remove('selected'));
            updateSelectionBar();
        }

        function addSelectedToQueue() {
            const ids = Array.from(_searchState.selectedIds);
            if (!ids.length) return;
            // Grab URLs from the cards before clearing selection state.
            const urls = [];
            const cards = [];
            for (const id of ids) {
                const safeId = (window.CSS && CSS.escape) ? CSS.escape(id) : id.replace(/"/g, '\\"');
                const card = document.querySelector(`.search-result-card[data-result-id="${safeId}"]`);
                if (card && card.dataset.resultUrl) {
                    urls.push(card.dataset.resultUrl);
                    cards.push(card);
                }
            }
            if (!urls.length) return;
            const label = urls.length === 1 ? '1 item' : `${urls.length} items`;
            showToast(`Adding ${label} to queue…`, null, null);
            // Sequential fire-and-forget. fetch_url_info returns immediately; the actual
            // metadata fetch happens in a background thread on the Python side.
            for (const url of urls) {
                try { pywebview.api.fetch_url_info(url, 'browser', 'none'); } catch (_) {}
            }
            // Mark cards as "Adding…" — NOT "Already in queue" yet. The badge
            // flips to "Already in queue" inside handleFullFetch once the
            // backend confirms the video actually landed in the queue. This
            // was the user-reported timing bug: previously we slapped the
            // "Already in queue" tag on the card before fetch_url_info even
            // started fetching, so a flaky network/extract would silently
            // never add the video while the UI claimed it already had.
            for (const card of cards) {
                card.dataset.addingPending = '1';
                card.classList.remove('selected');
                let actionEl = card.querySelector('.sr-action');
                if (!actionEl) {
                    actionEl = document.createElement('div');
                    actionEl.className = 'sr-action';
                    card.querySelector('.sr-meta')?.appendChild(actionEl);
                }
                actionEl.innerHTML = '<span class="sr-pending">Adding…</span>';
            }
            clearSearchSelection();
        }

