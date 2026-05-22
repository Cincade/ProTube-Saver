        // ============================================================
        // Floating tooltip system — rendered in body, escapes overflow:hidden
        // ============================================================
        (function initFloatingTooltips() {
            const TIP_ATTRS = ['data-tip', 'data-tip-right', 'data-tip-down', 'data-tip-wrap'];
            const PLACEMENT_FOR_ATTR = {
                'data-tip': 'top',
                'data-tip-right': 'top-right',
                'data-tip-down': 'bottom-right',
                'data-tip-wrap': 'top-left'
            };
            const SHOW_DELAY_MS = 400;

            let tipEl = null;
            let currentTarget = null;
            let showTimer = null;

            function ensureTipEl() {
                if (tipEl) return tipEl;
                tipEl = document.getElementById('global-tooltip') || (() => {
                    const d = document.createElement('div');
                    d.id = 'global-tooltip';
                    document.body.appendChild(d);
                    return d;
                })();
                return tipEl;
            }

            function getTipData(el) {
                for (const attr of TIP_ATTRS) {
                    const v = el.getAttribute(attr);
                    if (v) return { text: v, attr, placement: PLACEMENT_FOR_ATTR[attr] };
                }
                return null;
            }

            function findTipTarget(node) {
                let el = node;
                while (el && el !== document.body) {
                    if (el.nodeType === 1) {
                        for (const attr of TIP_ATTRS) {
                            if (el.hasAttribute(attr)) return el;
                        }
                    }
                    el = el.parentNode;
                }
                return null;
            }

            function positionTip(target, data) {
                const el = ensureTipEl();
                const vw = window.innerWidth;
                const vh = window.innerHeight;
                const sideMargin = 6;
                const gap = 6;

                // Decide wrap mode: explicit via data-tip-wrap, OR the message is too long
                // to fit on one line even if we gave it the whole viewport.
                const EXPLICIT_WRAP = data.attr === 'data-tip-wrap';
                const LONG_TEXT_THRESHOLD = 80; // chars; long messages auto-wrap
                const shouldWrap = EXPLICIT_WRAP || data.text.length > LONG_TEXT_THRESHOLD;

                el.classList.toggle('wrap', shouldWrap);
                el.textContent = data.text;

                // Cap the tooltip's max-width to what the viewport can hold.
                // Without this, a 340px max-width tooltip on a button near the right edge
                // still overflows even after clamping position.
                const hardMax = Math.min(340, vw - (sideMargin * 2));
                el.style.maxWidth = hardMax + 'px';

                // Measure invisibly
                el.style.left = '0px';
                el.style.top = '0px';
                el.style.visibility = 'hidden';
                el.classList.add('visible');
                const tipRect = el.getBoundingClientRect();
                el.classList.remove('visible');
                el.style.visibility = '';

                const tRect = target.getBoundingClientRect();
                const placement = data.placement;

                const tryPlace = (p) => {
                    let t, l;
                    if (p.startsWith('top')) t = tRect.top - tipRect.height - gap;
                    else t = tRect.bottom + gap;
                    if (p === 'top' || p === 'bottom') l = tRect.left + (tRect.width / 2) - (tipRect.width / 2);
                    else if (p.endsWith('-left')) l = tRect.left;
                    else if (p.endsWith('-right')) l = tRect.right - tipRect.width;
                    return { t, l };
                };

                let pos = tryPlace(placement);
                // Flip vertically if off-screen
                if (pos.t < 4 && placement.startsWith('top')) {
                    pos = tryPlace(placement.replace('top', 'bottom'));
                } else if (pos.t + tipRect.height > vh - 4 && placement.startsWith('bottom')) {
                    pos = tryPlace(placement.replace('bottom', 'top'));
                }
                // Clamp horizontally — the measured width already respects our maxWidth cap,
                // so this just picks the best horizontal slot within the viewport.
                pos.l = Math.max(sideMargin, Math.min(pos.l, vw - tipRect.width - sideMargin));

                el.style.left = pos.l + 'px';
                el.style.top = pos.t + 'px';
            }

            function showTip(target) {
                const data = getTipData(target);
                if (!data || !data.text) return;
                const el = ensureTipEl();
                positionTip(target, data);
                el.classList.add('visible');
            }

            function hideTip() {
                if (tipEl) tipEl.classList.remove('visible');
                if (showTimer) { clearTimeout(showTimer); showTimer = null; }
                currentTarget = null;
            }

            document.addEventListener('mouseover', (e) => {
                const target = findTipTarget(e.target);
                if (!target || target === currentTarget) return;
                currentTarget = target;
                if (showTimer) clearTimeout(showTimer);
                showTimer = setTimeout(() => {
                    showTimer = null;
                    if (currentTarget === target) showTip(target);
                }, SHOW_DELAY_MS);
            }, true);

            document.addEventListener('mouseout', (e) => {
                const target = findTipTarget(e.target);
                if (!target) return;
                if (e.relatedTarget && target.contains(e.relatedTarget)) return;
                hideTip();
            }, true);

            document.addEventListener('scroll', hideTip, true);
            document.addEventListener('click', hideTip, true);
        })();

        // ============================================================
        // Broken-image fallback — when thumbnails fail to load (offline,
        // deleted video, etc.), replace the broken <img> with a placeholder.
        // Original src is stashed in data-src so we can retry when we're back online.
        // ============================================================
        document.addEventListener('error', (e) => {
            const el = e.target;
            if (!el || el.tagName !== 'IMG') return;
            if (el.dataset.fallbackApplied) return;
            // Stash original src so we can retry later (offline → online transitions)
            if (el.src && !el.dataset.originalSrc) el.dataset.originalSrc = el.src;
            // If a fallback src was set (e.g. playlist card has primary URL +
            // first-child thumbnail as fallback), try that before giving up.
            // Clear the attribute so a second failure falls through to blank.
            if (el.dataset.fallbackThumb) {
                const fallback = el.dataset.fallbackThumb;
                delete el.dataset.fallbackThumb;
                el.src = fallback;
                return;
            }
            el.dataset.fallbackApplied = '1';
            // Blank the src to stop the browser's broken-image icon & further hits
            el.removeAttribute('src');
            el.style.display = 'none';
            const parent = el.parentElement;
            if (parent) parent.classList.add('thumb-broken');
        }, true);

        // Retry all previously-broken images — called on 'online' events.
        function retryBrokenImages() {
            const imgs = document.querySelectorAll('img[data-fallback-applied]');
            imgs.forEach(img => {
                const originalSrc = img.dataset.originalSrc;
                if (!originalSrc) return;
                // Reset state so the error handler can mark it broken again if the retry fails
                delete img.dataset.fallbackApplied;
                img.style.display = '';
                const parent = img.parentElement;
                if (parent) parent.classList.remove('thumb-broken');
                img.src = originalSrc;
            });
        }

        // ============================================================
        // Offline / online detection — small floating pill (top-right) + toasts.
        // The pill is position:fixed so it never displaces layout.
        // ============================================================
        let _wasOffline = false;
        let _offlinePillFadeTimer = null;
        function updateOfflineBanner() {
            let banner = document.getElementById('offline-banner');
            const isOffline = !navigator.onLine;
            // Toggle a body-level class so any element can react to offline
            // state via pure CSS (disabled buttons, dimmed pills, tooltips).
            // Single source of truth — JS handlers also gate on navigator.onLine.
            document.body.classList.toggle('is-offline', isOffline);
            if (isOffline) {
                // Already in this offline session — don't re-show the pill on
                // every poll tick. The pill is a notification ("you just went
                // offline"), not a permanent badge — disabled buttons and
                // greyed inputs are the persistent visual cue.
                if (_wasOffline) return;
                _wasOffline = true;
                if (!banner) {
                    banner = document.createElement('div');
                    banner.id = 'offline-banner';
                    banner.innerHTML = `
                        <span class="offline-dot"></span>
                        <span>Offline</span>
                    `;
                    document.body.appendChild(banner);
                }
                // Auto-fade after ~3.5s so the pill stops cluttering the UI
                // even though we're still offline. If connection returns
                // before the timer fires, the online branch cancels it and
                // morphs the same element into the "Back online" pill.
                if (_offlinePillFadeTimer) clearTimeout(_offlinePillFadeTimer);
                _offlinePillFadeTimer = setTimeout(() => {
                    _offlinePillFadeTimer = null;
                    const b = document.getElementById('offline-banner');
                    if (!b || b.classList.contains('online')) return;
                    b.classList.add('fade-out');
                    setTimeout(() => { if (b.parentNode) b.remove(); }, 340);
                }, 3500);
            } else {
                if (_wasOffline) {
                    _wasOffline = false;
                    // Cancel any pending offline-pill fade so the "Back online"
                    // morph isn't pre-empted halfway through.
                    if (_offlinePillFadeTimer) { clearTimeout(_offlinePillFadeTimer); _offlinePillFadeTimer = null; }
                    // Retry any thumbnails that failed while we were offline
                    retryBrokenImages();
                    // The original pill may have auto-faded already — recreate
                    // it if so, then morph into the green "Back online" variant
                    // and auto-dismiss after ~1.8s. No toast — pill IS the cue.
                    if (!banner) {
                        banner = document.createElement('div');
                        banner.id = 'offline-banner';
                        document.body.appendChild(banner);
                    }
                    banner.classList.remove('fade-out');
                    banner.classList.add('online');
                    banner.innerHTML = `
                        <span class="offline-dot"></span>
                        <span>Back online</span>
                    `;
                    setTimeout(() => {
                        if (!banner || !banner.parentNode) return;
                        banner.classList.add('fade-out');
                        setTimeout(() => {
                            if (banner && banner.parentNode) banner.remove();
                        }, 340);
                    }, 1800);
                } else if (banner) {
                    // First-load case (online from the start): just remove silently
                    banner.remove();
                }
            }
        }
        window.addEventListener('online', updateOfflineBanner);
        window.addEventListener('offline', updateOfflineBanner);
        document.addEventListener('DOMContentLoaded', updateOfflineBanner);
        setTimeout(updateOfflineBanner, 500);
        // Polling fallback — browsers (WebView2 in particular) regularly miss
        // 'online' / 'offline' events when the OS network state changes, which
        // left the offline pill stuck even after connectivity returned. Cheap
        // re-check every 4s catches any transitions the events miss.
        setInterval(updateOfflineBanner, 4000);

        function setFetchLoading(load, ctx, msg) {
            const btn = app.elements.mainFetchButton;
            const status = app.elements.mainFetchStatus;

            if (load) {
                btn.disabled = true;
                btn.innerHTML = '<div class="loading-spinner"></div>';
                if (status) status.textContent = msg || '';
            } else {
                btn.disabled = false;
                btn.textContent = 'Add';
                if (status) status.textContent = '';
            }
        }

        function handleFullFetch(videos, title, isPlaylist) {
            // Read-only channel preview: when a preview fetch resolves, open the
            // channel as a transient (isPreview) entry instead of adding it to the
            // queue. Guard on type==='playlist' so a concurrent single-video fetch
            // can't be mistaken for the preview.
            if (app._pendingPreview) {
                app._pendingPreview = false;   // consume the flag either way (no staleness)
                if (videos && videos[0] && videos[0].type === 'playlist') {
                    const ch = videos[0];
                    const existing = app.videosInQueue.find(v => v.id === ch.id);
                    if (existing) {
                        // Already saved (or already previewing) — just open it.
                        app.openPlaylistDetail(existing.id);
                    } else {
                        ch.isPreview = true;          // in-memory only — excluded from
                        app.videosInQueue.push(ch);   // the queue rail + persistence
                        app.openPlaylistDetail(ch.id);
                    }
                    finishFetch('');
                    return;
                }
                // Not a channel/playlist — fall through to the normal add flow.
            }
            // Detect whether the user is currently at the bottom of the queue
            // BEFORE we mutate state. "Near bottom" is within 80px of the
            // container's full scrollHeight. We use this to decide between two
            // behaviors when new items land:
            //   - at bottom → smooth-scroll the new item into view (chat-app
            //     "follow" pattern; doesn't yank you when you're already there)
            //   - scrolled up → don't disturb the user; surface a "↓ N new"
            //     pill so they can jump down on demand
            const list = app.elements.videoList;
            const wasAtBottom = list
                ? (list.scrollHeight - list.scrollTop - list.clientHeight < 80)
                : true;

            // Filter out duplicates, capture which ones are actually new for
            // post-render scroll/pill handling.
            const newItems = videos.filter(v => !app.videosInQueue.some(ev => ev.id === v.id));
            app.videosInQueue.push(...newItems);

            // Flip any search cards that were marked "Adding…" to the final
            // "Already in your queue" state, now that we have backend
            // confirmation that the video actually landed. Matches by id so
            // a stuck/failed fetch doesn't silently leave the card claiming
            // it succeeded.
            for (const v of videos) {
                if (!v || !v.id) continue;
                const safeId = (window.CSS && CSS.escape) ? CSS.escape(v.id) : String(v.id).replace(/"/g, '\\"');
                document.querySelectorAll(`.search-result-card[data-result-id="${safeId}"][data-adding-pending="1"]`).forEach(card => {
                    card.dataset.inQueue = '1';
                    delete card.dataset.addingPending;
                    const actionEl = card.querySelector('.sr-action');
                    if (actionEl) {
                        actionEl.innerHTML = '<span class="sr-already"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>Already in your queue</span>';
                    }
                });
            }

            // Flip any channel search cards stuck in "Fetching channel…" to
            // the "Channel in queue" pill. handleFullFetch is the moment the
            // channel-as-playlist actually lands in the queue, so this is
            // the right time to confirm to the user that their Subscribe
            // worked. We match by URL (channel cards stash data-result-url
            // and the playlist arriving carries the same url that was sent
            // to fetch_url_info). If URL matching misses (different
            // normalization), we fall back to flipping ALL pending channel
            // cards — typically there's only one Subscribe in flight at a
            // time, so that's safe.
            if (isPlaylist && videos.length === 1 && videos[0] && videos[0].url) {
                const fetchedUrl = String(videos[0].url || '');
                const norm = (u) => String(u || '').toLowerCase().replace(/\/videos\/?$/, '').replace(/\/+$/, '');
                const target = norm(fetchedUrl);
                const pendingCards = document.querySelectorAll('.search-result-card.channel[data-fetching-pending="1"]');
                let matched = false;
                pendingCards.forEach(card => {
                    if (norm(card.dataset.resultUrl || '') === target) {
                        matched = true;
                        _flipChannelCardToQueued(card);
                    }
                });
                if (!matched && pendingCards.length > 0) {
                    pendingCards.forEach(_flipChannelCardToQueued);
                }
            }

            // Clear the URL input so the user can paste the next one immediately
            app.elements.mainUrlInput.value = '';

            // Reset download/cancel buttons in case they were in a weird state
            app.elements.downloadButton.classList.remove('hidden');
            app.elements.cancelButton.classList.add('hidden');

            app.renderQueue();
            finishFetch("");

            // Post-render: scroll-or-pill. Only act if at least one of the new
            // items is actually rendered (i.e., matches the active filter).
            if (newItems.length > 0 && app.currentView === 'queue') {
                // The "newest" rendered item — last one in newItems that
                // matches the active filter and has a DOM element.
                let lastVisibleNewEl = null;
                for (let i = newItems.length - 1; i >= 0; i--) {
                    const it = newItems[i];
                    if (!app.matchesFilter(it, app.currentFilter)) continue;
                    const el = document.getElementById(`item-${app.escapeHtml(it.id)}`);
                    if (el) { lastVisibleNewEl = el; break; }
                }
                if (lastVisibleNewEl) {
                    if (wasAtBottom) {
                        lastVisibleNewEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
                    } else {
                        const visibleNewCount = newItems.filter(
                            it => app.matchesFilter(it, app.currentFilter)
                        ).length;
                        app._bumpNewQueuePill(visibleNewCount);
                    }
                }
            }
        }

        // ============================================================
        // "↓ N new" queue pill — pop-up that appears when new items land in
        // the queue while the user is scrolled up. Click jumps to bottom.
        // Auto-hides when the user manually scrolls to the bottom. State
        // (count) lives on app so it can be reset cleanly across renders.
        // ============================================================
        app._newQueueCount = 0;
        app._bumpNewQueuePill = function(by) {
            this._newQueueCount += (by || 1);
            const pill = document.getElementById('queue-new-pill');
            const label = document.getElementById('queue-new-pill-label');
            if (!pill || !label) return;
            label.textContent = this._newQueueCount === 1
                ? '1 new'
                : `${this._newQueueCount} new`;
            pill.removeAttribute('hidden');
            // 800ms sticky window — auto-hide listener bails out during this
            // period so render-triggered scroll events don't dismiss the pill
            // before the user has a chance to see it.
            window._pillStickyUntil = Date.now() + 800;
            // requestAnimationFrame so the [hidden] removal flushes a frame
            // before .visible transitions in — otherwise the fade-in is skipped.
            requestAnimationFrame(() => pill.classList.add('visible'));
        };
        app._hideNewQueuePill = function() {
            this._newQueueCount = 0;
            const pill = document.getElementById('queue-new-pill');
            if (!pill) return;
            pill.classList.remove('visible');
            // Wait for transition before flipping `hidden` so the fade-out plays
            setTimeout(() => {
                if (!pill.classList.contains('visible')) pill.setAttribute('hidden', '');
            }, 220);
        };
        // Scroll listener on the queue list — auto-hide the pill the moment
        // the user reaches the bottom on their own (whether by clicking the
        // pill or scrolling manually). Throttle via rAF since wheel events
        // fire fast. The sticky window check (_pillStickyUntil) protects
        // against render-triggered scroll events firing right after the
        // pill appears — without it, queues that barely overflow would hide
        // the pill instantly because scrollHeight - clientHeight < 30 already.
        (function wireQueueScroll() {
            const list = document.getElementById('video-list');
            const pill = document.getElementById('queue-new-pill');
            if (!list || !pill) return;
            let queued = false;
            list.addEventListener('scroll', () => {
                if (queued) return;
                queued = true;
                requestAnimationFrame(() => {
                    queued = false;
                    // Don't auto-hide while the sticky window is active —
                    // gives the pill a fair chance to land in front of the user.
                    if (Date.now() < (window._pillStickyUntil || 0)) return;
                    if (list.scrollHeight - list.scrollTop - list.clientHeight < 30) {
                        if (app._newQueueCount > 0) app._hideNewQueuePill();
                    }
                });
            });
            pill.addEventListener('click', () => {
                list.scrollTo({ top: list.scrollHeight, behavior: 'smooth' });
                app._hideNewQueuePill();
            });
        })();
        // Hide pill when leaving the queue view; if the user comes back later
        // the count is stale anyway. Wired by hooking into setView via a
        // patched switchView shim — applied only once.
        (function wireViewChangeForPill() {
            if (!app || !app.switchView || app._pillSwitchPatched) return;
            app._pillSwitchPatched = true;
            const orig = app.switchView.bind(app);
            app.switchView = function(view) {
                if (view !== 'queue' && app._newQueueCount > 0) app._hideNewQueuePill();
                return orig(view);
            };
        })();

        // Swap the Subscribe button (or its lingering "Fetching channel…"
        // state) for a green "Channel in queue" pill. Used by handleFullFetch
        // when a Subscribe finishes successfully.
        function _flipChannelCardToQueued(card) {
            if (!card) return;
            delete card.dataset.fetchingPending;
            card.dataset.inQueue = '1';
            const subBtn = card.querySelector('.sr-subscribe-btn');
            if (subBtn) subBtn.remove();
            const actionEl = card.querySelector('.sr-action');
            const pillHtml = '<span class="sr-already"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>Channel in queue</span>';
            if (actionEl) {
                actionEl.innerHTML = pillHtml;
            } else {
                // No existing .sr-action wrapper (channel cards put the
                // Subscribe button as a direct child) — inject one.
                card.insertAdjacentHTML('beforeend', `<div class="sr-action">${pillHtml}</div>`);
            }
        }

        function finishFetch(msg) {
            setFetchLoading(false, 'main');
            const isError = typeof msg === 'string' && msg.startsWith('Error:');
            // A failed fetch never reaches handleFullFetch's preview branch, so
            // clear any pending-preview flag here to avoid mis-tagging the next fetch.
            if (isError) app._pendingPreview = false;
            const inBatch = !!app._fetchBatch;

            // If a Subscribe-driven fetch errored, restore the Subscribe
            // button on any channel card still showing "Fetching channel…".
            // We can't tell which URL errored at this layer, but in practice
            // only one Subscribe is in flight at a time, so restoring all
            // pending cards is safe. (Batch mode rolls errors into the
            // summary toast instead.)
            if (isError && !inBatch) {
                document.querySelectorAll('.search-result-card.channel[data-fetching-pending="1"]').forEach(card => {
                    delete card.dataset.fetchingPending;
                    const subBtn = card.querySelector('.sr-subscribe-btn');
                    if (subBtn) {
                        subBtn.classList.remove('fetching');
                        subBtn.disabled = false;
                        subBtn.textContent = 'Subscribe';
                    }
                });
            }

            // Backend signals success with an empty string and failure with a
            // string starting "Error: ...". On success we stay silent (a toast
            // for every successful paste would be noise — the new card appearing
            // in the queue is already visual confirmation). On failure we surface
            // a toast so the user knows the paste didn't take, since the fetch
            // happens off-screen if they're scrolled in a long queue.
            //
            // During a multi-URL batch we suppress the per-error toast and roll
            // it into the summary so the user gets one toast at the end instead
            // of N error toasts mid-batch.
            if (isError && !inBatch) {
                const reason = msg.slice('Error:'.length).trim();
                showToast(`Couldn't fetch — ${reason || 'unknown error'}`, null, null);
            }

            // Advance the multi-URL batch. inFlight clears regardless of
            // outcome — both success and error count as "this slot finished".
            if (inBatch) {
                if (isError) app._fetchBatch.errors++;
                else app._fetchBatch.completed++;
                app._fetchBatch.inFlight = false;
                if (app._pumpFetchBatch) app._pumpFetchBatch();
                else if (app.fetch) app._pumpFetchBatch();  // fallback (shouldn't fire)
            }
        }

        // Batched form of updateItemThumbnail — backend coalesces ~8 cached
        // thumbnails into one bridge call so a big channel fetch doesn't pile
        // up hundreds of evaluate_js round-trips behind the queue render.
        // Each entry is {id, marker, playlistId, dataUrl}.
        function updateItemThumbnailBatch(updates) {
            if (!Array.isArray(updates)) return;
            for (const u of updates) {
                if (!u || !u.id || !u.marker) continue;
                updateItemThumbnail(u.id, u.marker, u.playlistId || null, u.dataUrl || '');
            }
        }

        // Backend pushes a thumbnail-marker update once it has cached a queue item's
        // thumbnail locally. Swap remote URL → 'pt:thumb:' marker in state and re-render
        // just the affected card so the user sees the local thumb (which works offline).
        function updateItemThumbnail(id, thumbnailMarker, playlistId, dataUrl) {
            if (!id || !thumbnailMarker) return;
            let video = null;
            if (playlistId) {
                const playlist = app.videosInQueue.find(i => i.type === 'playlist' && i.id === playlistId);
                if (playlist) video = (playlist.videos || []).find(v => v.id === id);
                // Also check library — playlist items can have their thumbnails
                // cached after they've been moved over from queue.
                if (!video) {
                    const libPlaylist = app.videosInLibrary.find(i => i.type === 'playlist' && i.id === playlistId);
                    if (libPlaylist) video = (libPlaylist.videos || []).find(v => v.id === id);
                }
            } else {
                video = app.videosInQueue.find(v => v.id === id)
                     || app.videosInLibrary.find(v => v.id === id);
            }
            if (!video) return;
            video.thumbnail = thumbnailMarker;

            // Pre-stash the data URL in the in-memory cache BEFORE re-rendering. This
            // is the key to a flash-free swap: the resolver in the next render will
            // see the cached entry and emit src= directly, instead of data-thumb-marker
            // (which leaves an empty img while the resolver fires async backend calls).
            if (dataUrl && app._thumbCache) {
                app._thumbCache[thumbnailMarker] = dataUrl;
            }

            // Surgical DOM update: instead of re-rendering the whole grid (which
            // breaks scroll position + replaces other DOM listeners), find the
            // specific <img> for this item and update its src in place. We only
            // fall back to a full re-render if we can't find the image element.
            // Selector matches anything tagged data-item-id={id} containing an img.
            const cardImgs = document.querySelectorAll(
                `[data-item-id="${id}"] img`
            );
            if (cardImgs.length > 0 && dataUrl) {
                cardImgs.forEach(img => {
                    img.src = dataUrl;
                    img.removeAttribute('data-thumb-marker');
                    img.removeAttribute('data-remote-thumb');
                });
                return;
            }
            // Fallback path — selector didn't match, do the heavier re-render
            if (app.currentView === 'library') {
                app.renderLibrary && app.renderLibrary();
            } else {
                app.renderQueue && app.renderQueue();
            }
        }

        function updateItemStatus(id, status, playlistId, filepath, folderpath, errorCategory, errorMessage) {
            // Locate the item in state: either a top-level video, or a child inside a playlist
            let video = null;
            let parentPlaylist = null;
            if (playlistId) {
                parentPlaylist = app.videosInQueue.find(i => i.type === 'playlist' && i.id === playlistId);
                if (parentPlaylist) {
                    video = parentPlaylist.videos.find(v => v.id === id);
                }
            } else {
                video = app.videosInQueue.find(v => v.id === id);
            }
            if (video) {
                video.status = status;
                if (status === 'Done') {
                    if (filepath) video.filepath = filepath;
                    if (folderpath) video.folderpath = folderpath;
                    // Clear any stale error state
                    delete video.errorCategory;
                    delete video.errorMessage;
                } else if (status === 'Error') {
                    if (errorCategory) video.errorCategory = errorCategory;
                    if (errorMessage) video.errorMessage = errorMessage;
                    if (folderpath) video.folderpath = folderpath;
                } else if (status === 'Downloading' || status === 'Retrying') {
                    // User or system is retrying — wipe old error state
                    delete video.errorCategory;
                    delete video.errorMessage;
                }
            }
            if (status !== 'Downloading' && video) video.progressPct = null;

            // Update the main-queue card if it's visible (loose video in main queue)
            const card = document.getElementById(`item-${id}`);
            if (card) {
                // If completed, toggle a done class on the card so clicks open the file
                card.classList.toggle('is-done', status === 'Done');
                card.classList.toggle('is-error', status === 'Error');

                const container = card.querySelector('.item-status-container');
                if (container && container.dataset.renderedStatus !== status) {
                    // Only rebuild when the rendered status ACTUALLY changes.
                    // Without this guard, every status replay (renderQueue ⇒
                    // replayStatus loop) or duplicate Downloading event tears
                    // the progress bar down to 0% and rebuilds it, producing
                    // visible flicker mid-download. The width is updated
                    // surgically by updateItemProgress; let it keep doing
                    // that and leave the container alone when same-state.
                    container.dataset.renderedStatus = status;
                    if (status === 'Downloading') {
                        const pauseSvg = `<svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>`;
                        container.innerHTML = `
                            <div class="progress-container">
                                <div class="progress-text">0% (0 KB/s)</div>
                                <div class="progress-bar-bg"><div class="progress-bar"></div></div>
                            </div>
                            <button class="status-icon-btn pause-mq-btn" onclick="event.stopPropagation(); pauseMainVideo('${id}')" data-tip-right="Pause download">${pauseSvg}</button>
                        `;
                    } else if (status === 'Retrying') {
                        container.innerHTML = `
                            <span class="status-badge status-retrying">
                                <span class="retry-dot"></span>Retrying…
                            </span>
                        `;
                    } else if (status === 'Paused' || status === 'Cancelled') {
                        const playSvg = `<svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l12-7z"/></svg>`;
                        const label = status === 'Paused' ? 'Paused' : 'Cancelled';
                        container.innerHTML = `
                            <div class="status-done-actions">
                                <span class="status-badge status-paused">${label}</span>
                                <button class="status-icon-btn" onclick="event.stopPropagation(); resumeMainVideo('${id}')" data-tip-right="Resume download">${playSvg}</button>
                            </div>
                        `;
                    } else if (status === 'Done') {
                        const hasFile = video && video.filepath;
                        container.innerHTML = `
                            <div class="status-done-actions">
                                <span class="status-badge status-done">Done</span>
                                ${hasFile ? `
                                <button class="status-icon-btn" onclick="event.stopPropagation(); revealVideoFile('${id}', ${playlistId ? `'${playlistId}'` : 'null'})" data-tip-right="Show in folder">
                                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
                                </button>` : ''}
                            </div>
                        `;
                    } else if (status === 'Error') {
                        const cat = (video && video.errorCategory) || errorCategory || 'generic';
                        const msg = (video && video.errorMessage) || errorMessage || 'Download failed.';
                        const label = errorCategoryLabel(cat);
                        const hasFolder = video && video.folderpath;
                        const escapedMsg = msg.replace(/"/g, '&quot;');
                        const playlistArg = playlistId ? `'${playlistId}'` : 'null';
                        container.innerHTML = `
                            <div class="status-error-actions">
                                <span class="status-badge status-error" data-tip="${escapedMsg}">${label}</span>
                                <button class="status-icon-btn retry-btn" onclick="event.stopPropagation(); retryVideoDownload('${id}', ${playlistArg})" data-tip-right="Retry download">
                                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg>
                                </button>
                                ${hasFolder ? `
                                <button class="status-icon-btn" onclick="event.stopPropagation(); pywebview.api.open_folder('${video.folderpath.replace(/\\/g, '\\\\').replace(/'/g, "\\'")}')" data-tip-right="Open folder">
                                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
                                </button>` : ''}
                            </div>
                        `;
                    } else {
                        container.innerHTML = `<span style="font-size: 11px; color: #737373; font-weight: 600; text-transform: uppercase;">${status}</span>`;
                    }
                }
            }

            // Update the detail-view row if that playlist is currently open
            if (parentPlaylist && app.currentPlaylistId === parentPlaylist.id) {
                app.updatePdVideoStatusInDom(id, status);
            }

            // Update the playlist card rollup if this was a child
            if (parentPlaylist) {
                app.updatePlaylistCardRollup(parentPlaylist.id);
            }

            app.updateDashboard();

            // Library handoff: if this status is Done, the backend has already moved single
            // videos to the library. The frontend needs to sync its own state to match.
            // We re-pull the library from backend rather than pushing local state up, so the
            // backend stays source of truth for what's been persisted.
            if (status === 'Done') {
                if (parentPlaylist) {
                    // Two rules for playlist children hitting Done:
                    //
                    // (A) If EVERY video in the playlist (not just the ones the user selected,
                    //     but ALL of them) is Done, the whole playlist moves to library as one
                    //     entry with full organization preserved.
                    //
                    // (B) Otherwise, this was a partial download — user picked some videos from
                    //     a bigger playlist. The finished child moves to library as a STANDALONE
                    //     video. The playlist stays in queue so the user can still download the
                    //     rest. This replaces the old "all selected done == move whole playlist"
                    //     which mangled partial playlists badly.
                    const allChildren = parentPlaylist.videos || [];
                    const everyChildDone = allChildren.length > 0 && allChildren.every(c => c.status === 'Done');

                    if (everyChildDone) {
                        // Special case: this is a "Check for updates" temp playlist.
                        // Instead of creating a new library entry (which would duplicate
                        // the original playlist), merge the freshly-downloaded children
                        // INTO the original library entry tagged by _update_target_id.
                        if (parentPlaylist._update_target_id) {
                            const targetId = parentPlaylist._update_target_id;
                            const doneChildren = (parentPlaylist.videos || []).filter(c => c.status === 'Done');
                            pywebview.api.add_videos_to_library_playlist(targetId, doneChildren).then(async (res) => {
                                app.videosInQueue = app.videosInQueue.filter(it => it.id !== parentPlaylist.id);
                                app.videosInLibrary = (await pywebview.api.load_library()) || [];
                                app.saveQueueState();
                                app.renderQueue();
                                app.renderLibrary();
                                const added = (res && res.added) || doneChildren.length;
                                showToast(
                                    `${added} new ${added === 1 ? 'video' : 'videos'} added to "${app.videosInLibrary.find(x => x.id === targetId)?.title || 'library'}"`,
                                    'View',
                                    () => app.switchView('library')
                                );
                            });
                            return;
                        }
                        // Respect the user's "Auto-add finished downloads to
                        // library" preference (Settings drawer). When it's
                        // explicitly OFF, the playlist sits in the queue with
                        // all children Done — user can leave it there or
                        // remove via the queue card. Default behavior (cache
                        // unset or true) is the existing auto-move.
                        if (window._autoAddToLibrary === false) {
                            return;
                        }
                        // Rule A — whole playlist moves to library
                        pywebview.api.add_playlist_to_library(parentPlaylist).then(async () => {
                            app.videosInQueue = app.videosInQueue.filter(it => it.id !== parentPlaylist.id);
                            app.videosInLibrary = (await pywebview.api.load_library()) || [];
                            app.saveQueueState();
                            app.renderQueue();
                            app.renderLibrary();
                            showToast(`Playlist "${parentPlaylist.title}" added to Library`, 'View', () => {
                                app.switchView('library');
                            });
                        });
                    } else if (video) {
                        // IDEMPOTENCY GUARD: renderQueue() replays status events on
                        // every render (replayStatus loop at ~line 12152) so the
                        // cards reflect persisted state. If THIS Rule B path keeps
                        // re-firing add_to_library / saveQueueState / renderQueue
                        // for a video that's already been moved into the library,
                        // renderQueue at the end of the chain triggers another
                        // replayStatus → Rule B → renderQueue → … infinite loop.
                        // We saw it: hundreds of save-queue + render bursts per
                        // second, document-level events starved, UI feels frozen.
                        // For channel children this matters because the channel
                        // branch deliberately keeps the Done child in pl.videos
                        // (so the gallery still shows it). The exit condition is
                        // "library already has this id" — once true, every later
                        // replay is a no-op.
                        const alreadyInLibrary = (app.videosInLibrary || []).some(e => {
                            if (!e) return false;
                            if (e.id === id) return true;
                            if (e.type === 'playlist' && Array.isArray(e.videos)) {
                                return e.videos.some(c => c && c.id === id);
                            }
                            return false;
                        });
                        if (alreadyInLibrary) {
                            return;   // breaks the replay → render → replay cycle
                        }
                        // Rule B — individual child moves to library as standalone.
                        // The child keeps its playlist context (playlistTitle, isFromPlaylist) so
                        // we know where it came from, but it lives as a top-level library entry.
                        //
                        // Channel exception: a channel is a browseable backlog, not a
                        // disposable batch. Splicing a downloaded child out of pl.videos
                        // makes the user lose their place in the channel gallery. So
                        // for channels we keep the child in place — it stays visible
                        // with a "Downloaded" badge — and only the standalone library
                        // copy is added. (See createPdChannelCardHTML.)
                        const parentIsChannel = classifyPlaylistEntry(parentPlaylist) === 'channel';
                        const standaloneCopy = {
                            ...video,
                            // Drop the isFromPlaylist flag so it renders as a standalone card
                            isFromPlaylist: false,
                            playlistTitle: parentPlaylist.title,
                            status: 'Done'
                        };
                        pywebview.api.add_to_library(standaloneCopy).then(async () => {
                            if (!parentIsChannel) {
                                // Regular playlist — original behavior: remove the child from queue.
                                parentPlaylist.videos = allChildren.filter(c => c.id !== id);
                                if (parentPlaylist.videos.length === 0) {
                                    app.videosInQueue = app.videosInQueue.filter(it => it.id !== parentPlaylist.id);
                                }
                            }
                            // Channel: keep video.status === 'Done' on the child so the
                            // gallery card shows the green Downloaded badge instead of
                            // disappearing. nothing else to mutate locally.
                            app.videosInLibrary = (await pywebview.api.load_library()) || [];
                            app.saveQueueState();
                            app.renderQueue();
                            app.renderLibrary();
                            // If the channel detail view is open and showing this
                            // playlist, refresh the affected card so the "Downloaded"
                            // overlay paints immediately (load_library round-trip
                            // means the inLibrary check now resolves true).
                            if (parentIsChannel && app.currentPlaylistId === parentPlaylist.id) {
                                app.updatePdVideoStatusInDom(id, 'Done');
                            }
                        });
                    }
                } else if (video) {
                    // Standalone single video — backend already added it via _download_worker.
                    // Remove from local queue and refetch library.
                    app.videosInQueue = app.videosInQueue.filter(it => it.id !== id);
                    pywebview.api.load_library().then(lib => {
                        app.videosInLibrary = lib || [];
                        app.saveQueueState();
                        app.renderQueue();
                        app.renderLibrary();
                    });
                }
            } else {
                // Non-Done status changes — just persist queue
                app.saveQueueState();
            }
        }

        // Map category slug → short display label (uppercase, used on the error badge)
        function errorCategoryLabel(cat) {
            const map = {
                network: 'NETWORK',
                rate_limit: 'RATE LIMIT',
                geo: 'GEO-BLOCKED',
                age_restricted: 'AGE-LOCKED',
                unavailable: 'UNAVAILABLE',
                format: 'NO FORMAT',
                disk: 'DISK',
                stale_resume: 'RESUME FAILED',
                generic: 'ERROR'
            };
            return map[cat] || 'ERROR';
        }

        // User-triggered retry — fetch fresh video data and restart the download
        function retryVideoDownload(videoId, playlistId) {
            let video = null;
            if (playlistId) {
                const pl = app.videosInQueue.find(i => i.type === 'playlist' && i.id === playlistId);
                video = pl?.videos.find(v => v.id === videoId);
            } else {
                video = app.videosInQueue.find(v => v.id === videoId);
            }
            if (!video) return;
            // If the previous failure was a stale-resume (HTTP 416), tell backend to wipe .part
            // files before retrying — otherwise we hit the same 416 again on next attempt.
            const forceRestart = video.errorCategory === 'stale_resume';
            // Wipe old error state optimistically; backend will send fresh status updates
            delete video.errorCategory;
            delete video.errorMessage;
            video.status = 'Queued';
            pywebview.api.restart_download(video, 'browser', 'none', forceRestart);
            updateItemStatus(videoId, 'Queued', playlistId || null);
            app.elements.downloadButton.classList.add('hidden');
            app.elements.cancelButton.classList.remove('hidden');
        }

        // Called by backend when auto-retry kicks in, so user sees something happening
        function showRetryToast(title) {
            showToast(`Network hiccup — retrying "${title}"...`, null, null);
        }

        // Pause a single-video download from the main queue
        function pauseMainVideo(videoId) {
            pywebview.api.pause_download(videoId);
            // Optimistically flip the UI to Paused so the button responds immediately
            updateItemStatus(videoId, 'Paused', null);
            const video = app.videosInQueue.find(v => v.id === videoId);
            const title = video ? video.title : 'Download';
            const short = title.length > 48 ? title.slice(0, 45) + '…' : title;
            showToast(`Paused "${short}"`, null, null);
        }

        // Resume a paused/cancelled single-video download from the main queue
        function resumeMainVideo(videoId) {
            const video = app.videosInQueue.find(v => v.id === videoId);
            if (!video) return;
            pywebview.api.restart_download(video, 'browser', 'none');
            updateItemStatus(videoId, 'Downloading', null);
            // Cancel button needs to be visible if there's an active download
            app.elements.downloadButton.classList.add('hidden');
            app.elements.cancelButton.classList.remove('hidden');
        }

        // Click a completed card body → open the file
        function openVideoFile(videoId, playlistId) {
            let video;
            if (playlistId) {
                const pl = app.videosInQueue.find(i => i.type === 'playlist' && i.id === playlistId);
                video = pl?.videos.find(v => v.id === videoId);
            } else {
                video = app.videosInQueue.find(v => v.id === videoId);
            }
            if (video?.filepath) pywebview.api.open_file(video.filepath);
        }

        // Click the reveal icon → open containing folder with file selected
        function revealVideoFile(videoId, playlistId) {
            let video;
            if (playlistId) {
                const pl = app.videosInQueue.find(i => i.type === 'playlist' && i.id === playlistId);
                video = pl?.videos.find(v => v.id === videoId);
            } else {
                video = app.videosInQueue.find(v => v.id === videoId);
            }
            if (video?.filepath) pywebview.api.reveal_in_folder(video.filepath);
            else if (video?.folderpath) pywebview.api.open_folder(video.folderpath);
        }

        // Single-click and double-click on library cards
        // Browser will fire onclick BEFORE ondblclick, so we debounce single-click by waiting
        // ~250ms to see if a double-click is coming. If it is, cancel the single-click action.
        let _libraryClickTimer = null;

        function openLibraryDetail(itemId) {
            // Debounce: wait to see if this is the first half of a double-click
            if (_libraryClickTimer) {
                clearTimeout(_libraryClickTimer);
                _libraryClickTimer = null;
            }
            _libraryClickTimer = setTimeout(() => {
                _libraryClickTimer = null;
                showDetailPanel(itemId);
            }, 250);
        }

        function openLibraryItemDirect(itemId) {
            // Double-click fired — cancel any pending single-click (detail panel open)
            if (_libraryClickTimer) {
                clearTimeout(_libraryClickTimer);
                _libraryClickTimer = null;
            }
            playLibraryItem(itemId);
        }

        // Actually play a library item (open in default player, or open folder for playlists)
        // Primary play action — uses the in-app player. Called by double-click on cards
        // and by the Play button in the detail panel.
        function playLibraryItem(itemId) {
            // Walk top-level AND playlist children — channel videos land as
            // children of the channel playlist entry, not top-level items, so
            // a flat find() missed them and double-click did nothing.
            let item = app.videosInLibrary.find(i => i.id === itemId);
            if (!item) {
                for (const entry of app.videosInLibrary) {
                    if (entry.type === 'playlist' && Array.isArray(entry.videos)) {
                        const child = entry.videos.find(c => c.id === itemId);
                        if (child) { item = child; break; }
                    }
                }
            }
            if (!item) return;
            if (item.type === 'playlist') {
                // Playlists don't have a single playable file. Open folder for now.
                const firstChild = (item.videos || []).find(c => c.folderpath || c.filepath);
                if (firstChild?.folderpath) {
                    pywebview.api.open_folder(firstChild.folderpath.replace(/[\\/][^\\/]+$/, ''));
                } else {
                    pywebview.api.open_folder();
                }
                return;
            }
            if (item.missing) {
                showToast(`"${item.title}" is missing from disk`, 'Open folder', () => {
                    if (item.folderpath) pywebview.api.open_folder(item.folderpath);
                    else pywebview.api.open_folder();
                });
                return;
            }
            if (!item.filepath) {
                showToast("No file path on record — older download", 'Open folder', () => {
                    if (item.folderpath) pywebview.api.open_folder(item.folderpath);
                    else pywebview.api.open_folder();
                });
                return;
            }
            // Open in the in-app player
            if (window.player && typeof window.player.open === 'function') {
                window.player.open(item);
            } else {
                // Fallback if player module didn't load — should never happen
                pywebview.api.open_file(item.filepath);
            }
        }

        // Open in OS default player (VLC etc.). Called from detail panel "Open externally"
        // and from the player view's "Open in VLC" button.
        function openExternalLibraryItem(itemId) {
            const item = app.videosInLibrary.find(i => i.id === itemId);
            if (!item || !item.filepath) return;
            pywebview.api.open_file(item.filepath);
        }

        // Show the detail panel for a library item
        function showDetailPanel(itemId) {
            const item = app.videosInLibrary.find(i => i.id === itemId);
            if (!item) return;

            const panel = document.getElementById('detail-panel');
            const backdrop = document.getElementById('detail-panel-backdrop');
            const body = document.getElementById('detail-panel-body');
            if (!panel || !body) return;

            body.innerHTML = buildDetailPanelHTML(item);
            // Reset scroll position to top so the panel always opens showing the thumbnail
            body.scrollTop = 0;
            panel.classList.remove('closing');
            panel.classList.add('visible');
            backdrop.classList.add('visible');
            panel.dataset.itemId = itemId;
            // If thumbnail is a 'pt:thumb:' marker, resolve it to a data URL
            if (app._resolvePendingThumbnails) app._resolvePendingThumbnails();
        }

        function hideDetailPanel() {
            const panel = document.getElementById('detail-panel');
            const backdrop = document.getElementById('detail-panel-backdrop');
            if (!panel) return;
            // Mark as closing so CSS keeps it visible during the slide-out transition,
            // then flip to fully hidden once transition ends.
            panel.classList.remove('visible');
            panel.classList.add('closing');
            if (backdrop) backdrop.classList.remove('visible');

            const onEnd = () => {
                panel.classList.remove('closing');
                panel.removeEventListener('transitionend', onEnd);
            };
            panel.addEventListener('transitionend', onEnd);
            // Safety timeout in case transitionend doesn't fire (interrupted etc.)
            setTimeout(onEnd, 300);
        }

        function buildDetailPanelHTML(item) {
            const escape = s => String(s || '')
                .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

            const isPlaylist = item.type === 'playlist';
            const title = escape(item.title || (isPlaylist ? 'Untitled playlist' : 'Untitled'));
            const uploader = item.uploader || '';
            const isImported = !!item.imported;
            const isMissing = !!item.missing;

            // Thumbnail
            const renderThumbSrc = (url) => {
                if (!url) return '';
                if (url.startsWith('pt:thumb:')) {
                    return `<img data-thumb-marker="${escape(url)}" alt="">`;
                }
                return `<img src="${escape(url)}" alt="">`;
            };

            let thumbHtml;
            if (isPlaylist) {
                const firstThumb = (item.thumbnails && item.thumbnails[0])
                    || (item.videos && item.videos[0] && item.videos[0].thumbnail)
                    || '';
                thumbHtml = firstThumb
                    ? renderThumbSrc(firstThumb)
                    : '<div class="detail-thumb-placeholder"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg></div>';
            } else {
                thumbHtml = item.thumbnail
                    ? renderThumbSrc(item.thumbnail)
                    : '<div class="detail-thumb-placeholder"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2"/></svg></div>';
            }
            const duration = escape(item.duration_string || '');

            // Channel row — shows "Unknown channel" for imported videos without uploader
            const channelBlock = uploader
                ? `<div class="detail-channel">${escape(uploader)}</div>`
                : isImported
                    ? `<div class="detail-channel muted">Channel unknown — will populate when you refetch</div>`
                    : `<div class="detail-channel muted">Channel unknown</div>`;

            // File size from sizeMap
            let sizeStr = '';
            if (item.sizeMap && item.selectedQuality) {
                const bytes = item.sizeMap[item.selectedQuality];
                if (bytes) sizeStr = formatBytes(bytes);
            } else if (isPlaylist) {
                const totalBytes = (item.videos || []).reduce((s, c) => {
                    const q = c.selectedQuality;
                    return s + (c.sizeMap?.[q] || 0);
                }, 0);
                if (totalBytes) sizeStr = formatBytes(totalBytes);
            }

            // Build meta grid
            const metaCells = [];
            if (isPlaylist) {
                metaCells.push(['Videos', `${(item.videos || []).length}`]);
                if (sizeStr) metaCells.push(['Total size', sizeStr]);
            } else {
                if (duration) metaCells.push(['Duration', duration]);
                if (sizeStr) metaCells.push(['File size', sizeStr]);
            }
            const metaHtml = metaCells.length > 0
                ? `<div class="detail-meta-row">${metaCells.map(([label, value]) =>
                    `<div class="detail-meta-cell"><div class="detail-meta-label">${label}</div><div class="detail-meta-value">${escape(value)}</div></div>`
                ).join('')}</div>`
                : '';

            // Banners
            let banner = '';
            if (isMissing) {
                banner = '<div class="detail-missing-banner">File is missing from disk. You can open the folder to find it, or re-download.</div>';
            } else if (isImported) {
                banner = '<div class="detail-imported-banner">Imported from disk. Some details like channel name and thumbnail will fill in once refetched from YouTube.</div>';
            }

            // Action buttons
            const hasUrl = item.url && item.url.trim();
            const urlBtnDisabled = !hasUrl;
            const urlBtnTip = hasUrl ? '' : 'data-tip="Original URL not stored"';

            const playIcon = '<svg viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M8 5v14l12-7z"/></svg>';
            const copyIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
            const folderIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';
            const extIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>';
            const trashIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/></svg>';

            const playBtn = isPlaylist
                ? `<button class="detail-action-btn primary" onclick="playLibraryItem('${item.id}')">${folderIcon}<span>Open folder</span></button>`
                : `<button class="detail-action-btn primary" onclick="playLibraryItem('${item.id}')">${playIcon}<span>Play in default player</span></button>`;

            const copyUrlBtn = isPlaylist
                ? ''
                : `<button class="detail-action-btn" onclick="copyItemUrl('${item.id}')" ${urlBtnDisabled ? 'disabled' : ''} ${urlBtnTip}>${copyIcon}<span>Copy URL</span></button>`;

            const openYoutubeBtn = (hasUrl && !isPlaylist)
                ? `<button class="detail-action-btn" onclick="openItemOnYoutube('${item.id}')">${extIcon}<span>Open on YouTube</span></button>`
                : '';

            // "Add channel to queue" — only for non-playlist videos. Calls the backend to find
            // the channel URL (stamped at fetch time, or derived from uploader name as fallback)
            // and either: (a) toast "already queued" when the channel exists, or (b) feed the URL
            // through the normal URL-bar fetch flow so the channel's videos populate the queue.
            const channelIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 7l-7 5 7 5V7z"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>';
            const addChannelBtn = (!isPlaylist && !isMissing)
                ? `<button class="detail-action-btn" onclick="addChannelFromVideo('${item.id}')">${channelIcon}<span>Add channel to queue</span></button>`
                : '';

            const revealBtn = `<button class="detail-action-btn" onclick="revealItemInFolder('${item.id}')">${folderIcon}<span>Reveal in folder</span></button>`;

            // Fix metadata — only for videos (playlists are aggregates).
            // Always available — even on a "good" entry — because the user might just
            // disagree with the auto-match and want to correct it.
            const fixIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>';
            const fixMetadataBtn = isPlaylist
                ? ''
                : `<button class="detail-action-btn" onclick="fixMetadata('${item.id}')">${fixIcon}<span>Fix metadata</span></button>`;

            // Check for updates — playlist/channel only, requires a stored URL.
            // Imported-from-disk playlists have no URL so this button is hidden for them.
            const refreshIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>';
            const subtype = isPlaylist ? classifyPlaylistEntry(item) : null;
            const updatesBtnLabel = subtype === 'channel' ? 'Check for new videos' : 'Check for updates';
            const checkUpdatesBtn = (isPlaylist && hasUrl)
                ? `<button class="detail-action-btn" onclick="checkPlaylistUpdates('${item.id}')">${refreshIcon}<span>${updatesBtnLabel}</span></button>`
                : '';

            // Pin / Unpin button — toggles the `pinned` flag. Pinned items
            // sort to the top of the library grid (most-recently-pinned first).
            const pinIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 3l5 5-3.5 1.5-2 2-1 5-2-2-4.5 4.5-1-1L11.5 13l-2-2L8 9.5l3.5-1.5 2-2L16 3z"/></svg>';
            const isPinned = !!item.pinned;
            const pinBtn = `<button class="detail-action-btn" onclick="togglePinLibraryItem('${item.id}')">${pinIcon}<span>${isPinned ? 'Unpin' : 'Pin to top'}</span></button>`;

            // Hide / Unhide button — toggles the `hidden` flag without deleting
            // or removing. Hidden items are filtered out of the default library
            // view; the "Show hidden" toggle in the library header reveals them.
            const hideEyeIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';
            const showEyeIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
            const isHidden = !!item.hidden;
            const hideBtn = `<button class="detail-action-btn" onclick="toggleHideLibraryItem('${item.id}')">${isHidden ? showEyeIcon : hideEyeIcon}<span>${isHidden ? 'Show in library' : 'Hide from library'}</span></button>`;

            const removeBtn = `<button class="detail-action-btn" onclick="removeFromLibrary('${item.id}')">${trashIcon}<span>Remove from library</span></button>`;

            // Delete video — hard delete: removes the file from disk AND clears the
            // library entry. Only for videos (playlists would mean deleting many files;
            // we'd want a confirm-each-file flow for that, save for later).
            const deleteIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6M10 11v6M14 11v6"/></svg>';
            const deleteBtn = isPlaylist
                ? ''
                : `<button class="detail-action-btn danger" onclick="deleteVideoFromDisk('${item.id}')">${deleteIcon}<span>Delete video</span></button>`;

            // AI summary now lives as a floating side panel in the player view (toggle via
            // the "AI" button in player controls). Detail panel kept clean per user request:
            // they wanted the summary alongside the video, not buried in the metadata pane.

            return `
                <div class="detail-thumb">
                    ${thumbHtml}
                    ${duration && !isPlaylist ? `<div class="detail-thumb-duration">${duration}</div>` : ''}
                </div>
                <div class="detail-title">${title}</div>
                ${channelBlock}
                ${banner}
                ${metaHtml}
                <div class="detail-actions">
                    ${playBtn}
                    ${copyUrlBtn}
                    ${openYoutubeBtn}
                    ${addChannelBtn}
                    ${revealBtn}
                    ${checkUpdatesBtn}
                    ${pinBtn}
                    ${fixMetadataBtn}
                    ${hideBtn}
                    ${removeBtn}
                    ${deleteBtn}
                </div>
            `;
        }

        // F7 — legacy detail-panel handlers kept for backward compatibility in case any
        // external entry point still calls them. Primary UI moved to the player's floating
        // summary panel (toggle via "AI" button next to CC).
        async function generateAiSummary(itemId) {
            const button = document.querySelector('.detail-ai-summary-generate');
            if (button) {
                button.textContent = 'Reading subtitles + thinking…';
                button.disabled = true;
            }
            try {
                const res = await pywebview.api.generate_video_summary(itemId);
                if (!res || res.error) {
                    showToast(res?.error || 'Summary generation failed', null, null);
                    if (button) {
                        button.textContent = 'Generate article from subtitles';
                        button.disabled = false;
                    }
                    return;
                }
                // Refresh library so the detail panel re-render reads the cached summary.
                app.videosInLibrary = (await pywebview.api.load_library()) || [];
                const fresh = app.videosInLibrary.find(i => i.id === itemId)
                    || app.videosInLibrary.flatMap(v => v.type === 'playlist' ? (v.videos || []) : []).find(c => c.id === itemId);
                if (fresh) {
                    document.getElementById('detail-panel-body').innerHTML = buildDetailPanelHTML(fresh);
                    if (app._resolvePendingThumbnails) app._resolvePendingThumbnails();
                }
            } catch (e) {
                showToast('Summary generation failed', null, null);
                if (button) {
                    button.textContent = 'Generate article from subtitles';
                    button.disabled = false;
                }
            }
        }

        // Add the creator's channel to the queue from a video's detail panel. Backend
        // first checks the stamped uploader_url, then probes yt-dlp on the video's URL
        // for the canonical channel URL — that probe can take 2-4s, so we show progress.
        async function addChannelFromVideo(itemId) {
            // Disable the button to prevent double-clicks during the probe.
            const btn = document.querySelector(`button[onclick="addChannelFromVideo('${itemId}')"]`);
            if (btn) {
                btn.disabled = true;
                btn.style.opacity = '0.6';
            }
            const restoreBtn = () => {
                if (btn) { btn.disabled = false; btn.style.opacity = ''; }
            };
            showToast('Looking up channel…', null, null);
            try {
                const res = await pywebview.api.find_channel_for_video(itemId);
                if (!res || res.error) {
                    showToast(res?.error || 'Could not find channel URL', null, null);
                    restoreBtn();
                    return;
                }
                if (res.already) {
                    const title = res.already.title || 'this channel';
                    const where = res.already.where === 'library' ? 'library' : 'queue';
                    showToast(`Already in your ${where}: ${title}`, null, null);
                    // Switch to the right view + scroll to it for instant orientation.
                    app.switchView(where);
                    setTimeout(() => {
                        const card = document.getElementById(`item-${res.already.id}`);
                        if (card) {
                            card.scrollIntoView({ behavior: 'smooth', block: 'center' });
                            card.classList.add('flash-highlight');
                            setTimeout(() => card.classList.remove('flash-highlight'), 1500);
                        }
                    }, 200);
                    restoreBtn();
                    return;
                }
                // Not already queued — fire the standard fetch flow with the resolved URL.
                showToast('Adding channel to queue…', null, null);
                pywebview.api.fetch_url_info(res.url, 'browser', 'none');
                app.switchView('queue');
                restoreBtn();
            } catch (e) {
                showToast('Could not add channel: ' + (e.message || e), null, null);
                restoreBtn();
            }
        }

        // F7 — drop the cached summary and regenerate. Same UX as initial generation.
        async function regenerateAiSummary(itemId) {
            try { await pywebview.api.clear_video_ai_summary(itemId); } catch (_) {}
            // Mirror in local state so re-render shows the "Generate" button
            const find = (arr) => {
                for (const v of arr) {
                    if (v.id === itemId) return v;
                    if (v.type === 'playlist') {
                        const c = (v.videos || []).find(x => x.id === itemId);
                        if (c) return c;
                    }
                }
                return null;
            };
            const local = find(app.videosInLibrary);
            if (local && local.ai_summary) delete local.ai_summary;
            const fresh = local;
            if (fresh) document.getElementById('detail-panel-body').innerHTML = buildDetailPanelHTML(fresh);
            generateAiSummary(itemId);
        }

        // Toggle the pin state on a library item. Pinned items sort to the top
        // of the library grid (most-recently-pinned first). Top-level entries
        // only — the `set_video_pinned` backend ignores playlist children.
        async function togglePinLibraryItem(itemId) {
            const item = app.videosInLibrary.find(i => i.id === itemId);
            if (!item) return;
            const wantPinned = !item.pinned;
            try {
                const res = await pywebview.api.set_video_pinned(itemId, wantPinned);
                if (!res?.ok) {
                    showToast(res?.error || 'Pin failed', null, null);
                    return;
                }
            } catch (e) {
                showToast('Pin failed', null, null);
                return;
            }
            app.videosInLibrary = (await pywebview.api.load_library()) || [];
            app.renderLibrary();
            // If detail panel is open on this item, re-render so the button label flips.
            const panel = document.getElementById('detail-panel');
            if (panel && panel.dataset.itemId === itemId && panel.classList.contains('visible')) {
                const fresh = app.videosInLibrary.find(i => i.id === itemId);
                if (fresh) {
                    document.getElementById('detail-panel-body').innerHTML = buildDetailPanelHTML(fresh);
                    if (app._resolvePendingThumbnails) app._resolvePendingThumbnails();
                }
            }
            showToast(wantPinned ? 'Pinned to top' : 'Unpinned', null, null);
        }

        // Hide/unhide a library item by flipping the backend's `hidden` flag,
        // refreshing local state, re-rendering the library + detail panel.
        async function toggleHideLibraryItem(itemId) {
            // Find item across top-level library and inside playlists, since
            // playlists also have child videos that can be hidden individually.
            let item = app.videosInLibrary.find(i => i.id === itemId);
            if (!item) {
                for (const v of app.videosInLibrary) {
                    if (v.type === 'playlist') {
                        const c = (v.videos || []).find(c => c.id === itemId);
                        if (c) { item = c; break; }
                    }
                }
            }
            if (!item) return;
            const wantHidden = !item.hidden;
            try {
                const res = await pywebview.api.set_video_hidden(itemId, wantHidden);
                if (!res?.ok) {
                    showToast(res?.error || 'Hide failed', null, null);
                    return;
                }
            } catch (e) {
                showToast('Hide failed', null, null);
                return;
            }
            // Refresh from backend so the local copy stays consistent.
            app.videosInLibrary = (await pywebview.api.load_library()) || [];
            app.renderLibrary();
            // If the detail panel is open on this same item, re-render it so
            // the button label flips. If hiding and "show hidden" is off, the
            // panel stays open but the card behind it is now filtered out —
            // close the panel for a cleaner exit.
            const showHidden = !!window._showHiddenLibrary;
            if (wantHidden && !showHidden) {
                hideDetailPanel();
            } else {
                const panel = document.getElementById('detail-panel');
                if (panel && panel.dataset.itemId === itemId && panel.classList.contains('visible')) {
                    const fresh = app.videosInLibrary.find(i => i.id === itemId)
                        || (() => {
                            for (const v of app.videosInLibrary) {
                                if (v.type === 'playlist') {
                                    const c = (v.videos || []).find(c => c.id === itemId);
                                    if (c) return c;
                                }
                            }
                            return null;
                        })();
                    if (fresh) {
                        document.getElementById('detail-panel-body').innerHTML = buildDetailPanelHTML(fresh);
                        if (app._resolvePendingThumbnails) app._resolvePendingThumbnails();
                    }
                }
            }
            showToast(
                wantHidden ? 'Hidden from library' : 'Shown in library',
                wantHidden ? 'Undo' : null,
                wantHidden ? () => toggleHideLibraryItem(itemId) : null
            );
        }

        // ============================================================
        // Playlist / channel "Check for updates" flow.
        //
        // 1. Backend re-fetches the playlist via yt-dlp flat-playlist and diffs
        //    against the entry's stored videos[].
        // 2. If new videos are found, modal lets user pick which to download +
        //    quality. Confirming pushes a temp playlist queue item flagged with
        //    _update_target_id pointing at the original library entry.
        // 3. The all-children-done detector (in updateVideoStatus) sees that flag
        //    and routes the finished children into the original library entry via
        //    add_videos_to_library_playlist instead of creating a new entry.
        // ============================================================
        // Toggle the channel-header bio between 2-line clamp and full text.
        // The inline "…more" / "Show less" button is a real element so the
        // user can click on a clear hotspot at the end of the bio text.
        function togglePdChannelBio(el) {
            if (!el) return;
            const expanded = el.classList.toggle('is-expanded');
            const toggle = el.querySelector('.pd-channel-bio-toggle');
            if (toggle) toggle.textContent = expanded ? 'Show less' : '…more';
        }

        async function checkPlaylistUpdates(itemId) {
            // Pre-empt offline — yt_dlp would otherwise sit on a DNS lookup
            // for ~15s before failing. Keep parity with fetch()'s offline guard.
            if (!navigator.onLine) {
                showToast("You're offline — can't check for new videos right now.", null, null);
                return;
            }
            // Look in BOTH library and queue — channels and partially-downloaded
            // playlists usually live in the queue (not the library), so this is the
            // common case. Library entries are the "all done" subset.
            const item = app.videosInLibrary.find(i => i.id === itemId)
                || app.videosInQueue.find(i => i.id === itemId && i.type === 'playlist');
            if (!item) return;

            const subtype = classifyPlaylistEntry(item);
            const noun = subtype === 'channel' ? 'channel' : 'playlist';

            // Visible progress card. The flat-playlist call has no per-entry
            // progress events, so we show the bar in indeterminate mode (sweeping
            // strip) — the user gets clear "something is happening" feedback for
            // the 2–15s the call can take, instead of a one-line toast that's easy
            // to miss.
            const progEl = document.getElementById('import-progress');
            const textEl = progEl?.querySelector('.ipro-text');
            const countEl = progEl?.querySelector('.ipro-count');
            const barEl = progEl?.querySelector('.ipro-bar');
            const fillEl = progEl?.querySelector('.ipro-bar-fill');
            const restoreProgress = () => {
                if (!progEl) return;
                progEl.setAttribute('hidden', '');
                if (barEl) barEl.classList.remove('indeterminate');
                if (fillEl) fillEl.style.width = '0%';
                // Reset label so subsequent imports start with the right text.
                if (textEl) textEl.textContent = 'Importing…';
                if (countEl) countEl.textContent = '0 / 0';
                // Stop accepting backend progress ticks for this check.
                window._activeUpdateCheckId = null;
            };
            if (progEl) {
                if (textEl) textEl.textContent = `Checking ${noun} for new videos…`;
                // Live count is filled in by onUpdateCheckProgress as yt-dlp
                // streams entries. The number reflects NEW videos (not already
                // in this entry's library) — so a fully-up-to-date channel
                // shows "0 new" the whole time, and a channel with 5 fresh
                // uploads ticks up to "5 new" no matter how many videos it
                // has on YouTube.
                if (countEl) countEl.textContent = '0 new';
                // Clear any inline width left from a previous import (inline
                // styles win over our .indeterminate CSS rule's `width: 30%`,
                // so without this the sweeping strip stays at 0px wide and
                // only the spinner shows movement).
                if (fillEl) fillEl.style.width = '';
                if (barEl) barEl.classList.add('indeterminate');
                progEl.removeAttribute('hidden');
                // Mark which playlist is being checked so the global progress
                // callback only updates the bar when the tick belongs to this
                // call (otherwise a stale tick from a cancelled prior check
                // could clobber the current counter).
                window._activeUpdateCheckId = itemId;
            }

            let result;
            try {
                result = await pywebview.api.check_playlist_updates(itemId);
            } catch (err) {
                restoreProgress();
                showToast(`Couldn't check for updates: ${err}`, null, null);
                return;
            }
            restoreProgress();

            if (!result || !result.ok) {
                showToast(result?.error || 'Update check failed', null, null);
                return;
            }

            // Backfill the channel's avatar/banner if the backend returned fresh
            // values. Older queue/library entries pre-date these fields, so this
            // is how an existing channel finally gets its proper profile photo.
            let brandingChanged = false;
            if (result.channelAvatar && item.channelAvatar !== result.channelAvatar) {
                item.channelAvatar = result.channelAvatar;
                brandingChanged = true;
            }
            if (result.channelBanner && item.channelBanner !== result.channelBanner) {
                item.channelBanner = result.channelBanner;
                brandingChanged = true;
            }
            if (brandingChanged) {
                if (item === app.videosInQueue.find(i => i.id === item.id)) app.saveQueueState();
                if (app.currentPlaylistId === item.id) app.renderPlaylistDetail(item);
            }

            const newVideos = result.new || [];
            if (newVideos.length === 0) {
                const removedNote = (result.removed_ids || []).length > 0
                    ? ` (${result.removed_ids.length} no longer available)`
                    : '';
                showToast(`Up to date${removedNote}`, null, null);
                return;
            }

            openUpdatesModal(item, newVideos, result.source || 'library');
        }

        function openUpdatesModal(libraryItem, newVideos, source) {
            const backdrop = document.getElementById('upd-backdrop');
            const titleEl = document.getElementById('upd-title');
            const subtitleEl = document.getElementById('upd-subtitle');
            const listEl = document.getElementById('upd-list');
            const masterBtn = document.getElementById('upd-master');
            const masterLabel = document.getElementById('upd-master-label');
            const countEl = document.getElementById('upd-selected-count');
            const confirmBtn = document.getElementById('upd-confirm');
            const cancelBtn = document.getElementById('upd-cancel');
            const closeBtn = document.getElementById('upd-close');

            const subtype = classifyPlaylistEntry(libraryItem);
            const noun = subtype === 'channel' ? 'channel' : 'playlist';
            titleEl.textContent = `${newVideos.length} new ${newVideos.length === 1 ? 'video' : 'videos'}`;
            subtitleEl.textContent = `From ${noun}: ${libraryItem.title || 'Untitled'}`;

            // Build the list. Reusing .imp-row markup to inherit the picker's styling.
            const escape = s => String(s || '')
                .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

            const rows = newVideos.map((v, i) => {
                const thumb = v.thumbnail
                    ? `<img src="${escape(v.thumbnail)}" alt="">`
                    : '<div class="upd-row-thumb-ph"></div>';
                const dur = v.duration_string ? `<span class="upd-row-dur">${escape(v.duration_string)}</span>` : '';
                return `
                    <label class="upd-row" data-vid="${escape(v.id)}">
                        <input type="checkbox" class="upd-row-check" data-idx="${i}" checked>
                        <div class="upd-row-thumb">${thumb}</div>
                        <div class="upd-row-meta">
                            <div class="upd-row-title">${escape(v.title)}</div>
                            <div class="upd-row-sub">${escape(v.uploader || '')} ${dur}</div>
                        </div>
                    </label>
                `;
            }).join('');
            listEl.innerHTML = rows;

            // Selected = all by default
            const selected = new Set(newVideos.map((_, i) => i));
            const refreshState = () => {
                countEl.textContent = String(selected.size);
                confirmBtn.disabled = selected.size === 0;
                confirmBtn.textContent = `Add ${selected.size} to queue`;
                if (selected.size === newVideos.length) {
                    masterLabel.textContent = 'Deselect all';
                } else {
                    masterLabel.textContent = 'Select all';
                }
            };

            const onListClick = (e) => {
                const cb = e.target.closest('.upd-row-check');
                if (!cb) return;
                const idx = parseInt(cb.getAttribute('data-idx'), 10);
                if (cb.checked) selected.add(idx); else selected.delete(idx);
                refreshState();
            };

            const onMasterClick = () => {
                if (selected.size === newVideos.length) {
                    selected.clear();
                    listEl.querySelectorAll('.upd-row-check').forEach(c => c.checked = false);
                } else {
                    newVideos.forEach((_, i) => selected.add(i));
                    listEl.querySelectorAll('.upd-row-check').forEach(c => c.checked = true);
                }
                refreshState();
            };

            const close = () => {
                backdrop.setAttribute('hidden', '');
                listEl.removeEventListener('click', onListClick);
                masterBtn.removeEventListener('click', onMasterClick);
                cancelBtn.removeEventListener('click', close);
                closeBtn.removeEventListener('click', close);
                confirmBtn.removeEventListener('click', onConfirm);
                document.removeEventListener('keydown', onKeydown);
            };

            const onKeydown = (e) => {
                if (e.key === 'Escape') close();
            };

            const onConfirm = () => {
                if (selected.size === 0) return;
                const picks = Array.from(selected).sort((a, b) => a - b).map(i => newVideos[i]);
                close();
                queuePlaylistUpdates(libraryItem, picks, source);
            };

            listEl.addEventListener('click', onListClick);
            masterBtn.addEventListener('click', onMasterClick);
            cancelBtn.addEventListener('click', close);
            closeBtn.addEventListener('click', close);
            confirmBtn.addEventListener('click', onConfirm);
            document.addEventListener('keydown', onKeydown);

            backdrop.removeAttribute('hidden');
            refreshState();
        }

        // Two paths depending on where the source playlist lives:
        //
        //  source === 'queue': just append the new videos to the existing queue
        //    playlist's videos[]. They appear with "Resolving…" state, then become
        //    pickable. The user clicks Download on the playlist as usual; new ones
        //    download alongside any existing ones. No temp playlist, no merge step.
        //
        //  source === 'library': create a temp playlist tagged with _update_target_id
        //    and let it download via the standard pipeline. Auto-start kicks in
        //    when formats resolve (see onPlaylistFormatsComplete). On all-done,
        //    add_videos_to_library_playlist merges the children into the original
        //    library entry.
        function queuePlaylistUpdates(sourceItem, newVideos, source) {
            const noun = classifyPlaylistEntry(sourceItem) === 'channel' ? 'channel' : 'playlist';

            if (source === 'queue') {
                const queuePl = app.videosInQueue.find(p => p.id === sourceItem.id && p.type === 'playlist');
                if (!queuePl) {
                    showToast(`Couldn't find ${noun} in queue anymore`, null, null);
                    return;
                }
                const existingIds = new Set((queuePl.videos || []).map(v => v.id));
                const additions = newVideos
                    .filter(v => !existingIds.has(v.id))
                    .map(v => ({
                        type: 'video',
                        id: v.id,
                        url: v.url,
                        title: v.title,
                        uploader: v.uploader || queuePl.uploader || '',
                        thumbnail: v.thumbnail,
                        formats: null,
                        sizeMap: {},
                        duration_string: v.duration_string || '',
                        selected: true,
                    }));
                if (additions.length === 0) {
                    showToast('Already in queue', null, null);
                    return;
                }
                // Prepend, not append — these are the newest videos from the source
                // (channels' Videos tabs and YouTube playlists return reverse-
                // chronological by default), so they should sit at the top of the
                // detail list, above the existing children.
                queuePl.videos.unshift(...additions);
                queuePl.videoCount = queuePl.videos.length;
                queuePl.formatsResolved = false;
                app.saveQueueState();
                // Re-render the queue and, if the detail view is open on this same
                // playlist, refresh it so the user sees the new rows immediately.
                app.renderQueue();
                if (app.currentPlaylistId === queuePl.id) {
                    app.renderPlaylistDetail(queuePl);
                }
                // Kick off format resolution for the new ones only.
                const unresolved = additions.map(c => ({ id: c.id, url: c.url }));
                pywebview.api.resolve_playlist_formats(queuePl.id, unresolved, 'browser', 'none');
                showToast(
                    `Added ${additions.length} new ${additions.length === 1 ? 'video' : 'videos'} to ${noun}`,
                    null, null
                );
                return;
            }

            // source === 'library' — temp playlist + auto-download + merge flow
            const targetId = sourceItem.id;
            const tempId = `pl-update-${targetId}-${Date.now()}`;
            const defaultQuality = sourceItem.selectedQuality
                || (sourceItem.videos && sourceItem.videos[0]?.selectedQuality)
                || '1080p';

            const children = newVideos.map(v => ({
                type: 'video',
                id: v.id,
                url: v.url,
                title: v.title,
                uploader: v.uploader || sourceItem.uploader || '',
                thumbnail: v.thumbnail,
                formats: null,
                sizeMap: {},
                duration_string: v.duration_string || '',
                selected: true,
                isFromPlaylist: true,
                playlistTitle: sourceItem.title || '',
            }));

            const tempPlaylist = {
                type: 'playlist',
                id: tempId,
                _update_target_id: targetId,
                url: sourceItem.url,
                subtype: classifyPlaylistEntry(sourceItem),
                title: `Updates: ${sourceItem.title || 'Untitled'}`,
                uploader: sourceItem.uploader || '',
                videoCount: children.length,
                defaultQuality: defaultQuality,
                thumbnails: children.slice(0, 4).map(c => c.thumbnail).filter(Boolean),
                videos: children,
                formatsResolved: false,
            };

            app.videosInQueue.push(tempPlaylist);
            app.saveQueueState();
            app.renderQueue();

            const unresolved = children.map(c => ({ id: c.id, url: c.url }));
            pywebview.api.resolve_playlist_formats(tempId, unresolved, 'browser', 'none');

            showToast(
                `Queued ${children.length} ${noun} update${children.length === 1 ? '' : 's'}`,
                'View queue',
                () => app.switchView('queue')
            );
        }

        // Detail panel action handlers
        // Fix metadata flow — opens a modal, user pastes the correct YouTube URL,
        // backend updates the library entry by ID lookup (no search, no guessing).
        function fixMetadata(itemId) {
            const item = app.videosInLibrary.find(i => i.id === itemId);
            if (!item) return;

            const backdrop = document.getElementById('fix-modal-backdrop');
            const input = document.getElementById('fix-modal-input');
            const error = document.getElementById('fix-modal-error');
            const submit = document.getElementById('fix-modal-submit');
            const cancel = document.getElementById('fix-modal-cancel');
            const closeBtn = document.getElementById('fix-modal-close');

            // Pre-fill with existing URL if there is one
            input.value = item.url || '';
            error.setAttribute('hidden', '');
            error.textContent = '';
            backdrop.removeAttribute('hidden');
            submit.disabled = false;
            submit.textContent = 'Update';

            // Focus + select after the modal renders
            setTimeout(() => {
                input.focus();
                input.select();
            }, 50);

            const close = () => {
                backdrop.setAttribute('hidden', '');
                input.value = '';
                error.setAttribute('hidden', '');
                // Detach handlers so they don't stack on next open
                submit.removeEventListener('click', onSubmit);
                cancel.removeEventListener('click', close);
                closeBtn.removeEventListener('click', close);
                backdrop.removeEventListener('click', onBackdropClick);
                input.removeEventListener('keydown', onKey);
            };

            const onBackdropClick = (e) => {
                // Clicking the backdrop (but not the modal itself) closes
                if (e.target === backdrop) close();
            };

            const onKey = (e) => {
                if (e.key === 'Escape') close();
                else if (e.key === 'Enter') onSubmit();
            };

            const onSubmit = async () => {
                const url = input.value.trim();
                if (!url) {
                    error.textContent = 'Please paste a YouTube URL';
                    error.removeAttribute('hidden');
                    return;
                }
                submit.disabled = true;
                submit.textContent = 'Updating…';
                error.setAttribute('hidden', '');

                try {
                    const result = await pywebview.api.fix_metadata_from_url(itemId, url);
                    if (result?.error) {
                        error.textContent = result.error;
                        error.removeAttribute('hidden');
                        submit.disabled = false;
                        submit.textContent = 'Update';
                        return;
                    }
                    // Success — invalidate the in-memory thumb cache for this video.
                    // Marker is keyed on the entry id (which doesn't change), so without
                    // this, the resolver returns the OLD thumbnail data we cached when
                    // the metadata was wrong. Clearing forces a fresh backend round-trip.
                    if (app._thumbCache) {
                        for (const key of Object.keys(app._thumbCache)) {
                            if (key.startsWith('pt:thumb:') && key.includes(itemId)) {
                                delete app._thumbCache[key];
                            }
                        }
                    }

                    // Reload library, refresh detail panel, close modal
                    app.videosInLibrary = (await pywebview.api.load_library()) || [];
                    app.renderLibrary();
                    // If the detail panel is still open for this item, rebuild its content
                    const panel = document.getElementById('detail-panel');
                    if (panel && panel.dataset.itemId === itemId && panel.classList.contains('visible')) {
                        const updated = app.videosInLibrary.find(i => i.id === itemId);
                        if (updated) {
                            const body = document.getElementById('detail-panel-body');
                            if (body) {
                                body.innerHTML = buildDetailPanelHTML(updated);
                                if (app._resolvePendingThumbnails) app._resolvePendingThumbnails();
                            }
                        }
                    }
                    close();
                    showToast(`Updated: "${result.title || 'video'}"`, null, null);
                } catch (e) {
                    console.error('[fixMetadata] failed:', e);
                    error.textContent = 'Update failed: ' + (e?.message || 'unknown error');
                    error.removeAttribute('hidden');
                    submit.disabled = false;
                    submit.textContent = 'Update';
                }
            };

            submit.addEventListener('click', onSubmit);
            cancel.addEventListener('click', close);
            closeBtn.addEventListener('click', close);
            backdrop.addEventListener('click', onBackdropClick);
            input.addEventListener('keydown', onKey);
        }

        async function copyItemUrl(itemId) {
            const item = app.videosInLibrary.find(i => i.id === itemId);
            if (!item?.url) return;
            try {
                await navigator.clipboard.writeText(item.url);
                showToast('URL copied to clipboard', null, null);
            } catch (e) {
                showToast('Could not copy — try again', null, null);
            }
        }

        function openItemOnYoutube(itemId) {
            const item = app.videosInLibrary.find(i => i.id === itemId);
            if (!item?.url) return;
            // pywebview doesn't open external URLs directly from JS — use the OS
            pywebview.api.open_external_url(item.url);
        }

        function revealItemInFolder(itemId) {
            const item = app.videosInLibrary.find(i => i.id === itemId);
            if (!item) return;
            if (item.type === 'playlist') {
                const firstChild = (item.videos || []).find(c => c.folderpath || c.filepath);
                if (firstChild?.folderpath) {
                    pywebview.api.open_folder(firstChild.folderpath.replace(/[\\/][^\\/]+$/, ''));
                } else {
                    pywebview.api.open_folder();
                }
                return;
            }
            if (item.filepath) pywebview.api.reveal_in_folder(item.filepath);
            else if (item.folderpath) pywebview.api.open_folder(item.folderpath);
            else pywebview.api.open_folder();
        }

        async function removeFromLibrary(itemId) {
            const item = app.videosInLibrary.find(i => i.id === itemId);
            if (!item) return;
            // Remove from backend first so reload is clean
            await pywebview.api.remove_from_library(itemId);
            app.videosInLibrary = (await pywebview.api.load_library()) || [];
            app.renderLibrary();
            hideDetailPanel();
            showToast(`Removed "${item.title}" from library`, 'Undo', async () => {
                // Undo just adds it back
                if (item.type === 'playlist') {
                    await pywebview.api.add_playlist_to_library(item);
                } else {
                    await pywebview.api.add_to_library(item);
                }
                app.videosInLibrary = (await pywebview.api.load_library()) || [];
                app.renderLibrary();
            });
        }

        // Custom confirmation dialog. Replaces window.confirm() with a styled modal.
        // Returns a Promise that resolves to true (confirmed) or false (cancelled).
        // Auto-dismisses on Esc / backdrop click. Enter key triggers confirm.
        function confirmDialog({ title, body, confirmText = 'Delete', cancelText = 'Cancel', danger = true }) {
            return new Promise((resolve) => {
                const backdrop = document.getElementById('confirm-modal-backdrop');
                const titleEl = document.getElementById('confirm-modal-title');
                const bodyEl = document.getElementById('confirm-modal-body');
                const confirmBtn = document.getElementById('confirm-modal-confirm');
                const cancelBtn = document.getElementById('confirm-modal-cancel');
                if (!backdrop) { resolve(window.confirm(`${title}\n\n${body}`)); return; }

                titleEl.textContent = title;
                bodyEl.textContent = body;
                confirmBtn.textContent = confirmText;
                cancelBtn.textContent = cancelText;
                confirmBtn.classList.toggle('danger', !!danger);

                backdrop.removeAttribute('hidden');
                setTimeout(() => confirmBtn.focus(), 60);

                const cleanup = () => {
                    backdrop.setAttribute('hidden', '');
                    confirmBtn.removeEventListener('click', onConfirm);
                    cancelBtn.removeEventListener('click', onCancel);
                    backdrop.removeEventListener('click', onBackdrop);
                    document.removeEventListener('keydown', onKey);
                };
                const onConfirm = () => { cleanup(); resolve(true); };
                const onCancel = () => { cleanup(); resolve(false); };
                const onBackdrop = (e) => { if (e.target === backdrop) onCancel(); };
                const onKey = (e) => {
                    if (e.key === 'Escape') onCancel();
                    else if (e.key === 'Enter') onConfirm();
                };

                confirmBtn.addEventListener('click', onConfirm);
                cancelBtn.addEventListener('click', onCancel);
                backdrop.addEventListener('click', onBackdrop);
                document.addEventListener('keydown', onKey);
            });
        }

        // Hard delete — removes the file from disk AND clears the library entry.
        // No undo for this one because the file is gone (we'd have to redownload to
        // restore). We do confirm with a custom dialog before proceeding.
        async function deleteVideoFromDisk(itemId) {
            // Find item in library OR in any playlist's children
            let item = app.videosInLibrary.find(i => i.id === itemId);
            let isPlaylistChild = false;
            if (!item) {
                for (const v of app.videosInLibrary) {
                    if (v.type === 'playlist') {
                        const c = (v.videos || []).find(c => c.id === itemId);
                        if (c) { item = c; isPlaylistChild = true; break; }
                    }
                }
            }
            if (!item) return;

            const folderClause = isPlaylistChild
                ? "The video file will be deleted from disk."
                : "The video file and its folder will be deleted from disk.";
            const ok = await confirmDialog({
                title: `Delete "${item.title}"?`,
                body: `${folderClause} You'll need to re-download to watch again. This cannot be undone.`,
                confirmText: 'Delete',
                cancelText: 'Cancel',
                danger: true,
            });
            if (!ok) return;
            try {
                const result = await pywebview.api.delete_video_from_library_and_disk(itemId);
                app.videosInLibrary = (await pywebview.api.load_library()) || [];
                // Mirror the backend's queue cleanup in the in-memory queue so a
                // channel/playlist grid updates WITHOUT a restart: drop a standalone
                // queued copy, and clear the downloaded state on any channel/playlist
                // child so its card unlocks and becomes re-downloadable again (the
                // "can't re-download a deleted channel video" bug).
                if (Array.isArray(app.videosInQueue)) {
                    app.videosInQueue = app.videosInQueue.filter(q => !(q.id === itemId && q.type !== 'playlist'));
                    app.videosInQueue.forEach(q => {
                        if (q.type === 'playlist' && Array.isArray(q.videos)) {
                            q.videos.forEach(c => {
                                if (c.id === itemId) {
                                    delete c.status; delete c.progressPct; delete c.missing; c.selected = false;
                                }
                            });
                        }
                    });
                    // Re-render the channel/playlist detail if it's the open view so
                    // the unlocked card shows immediately.
                    if (app.currentPlaylistId && typeof app.renderPlaylistDetail === 'function') {
                        const pl = app.videosInQueue.find(i => i.type === 'playlist' && i.id === app.currentPlaylistId);
                        if (pl) app.renderPlaylistDetail(pl);
                    }
                }
                app.renderLibrary();
                hideDetailPanel();
                if (result?.skipped && result.skipped.length > 0) {
                    showToast(`Removed from library, but couldn't delete file (locked?). Try closing other apps.`, null, null);
                } else {
                    showToast(`Deleted "${item.title}"`, null, null);
                }
            } catch (e) {
                console.error('[deleteVideoFromDisk] failed:', e);
                showToast(`Couldn't delete: ${e?.message || 'unknown error'}`, null, null);
            }
        }

        function updateItemProgress(id, pct, speed, playlistId, downloadedBytes, totalBytes, speedBytes) {
            let video = null;
            let parentPlaylist = null;
            if (playlistId) {
                parentPlaylist = app.videosInQueue.find(i => i.type === 'playlist' && i.id === playlistId);
                if (parentPlaylist) {
                    video = parentPlaylist.videos.find(v => v.id === id);
                }
            } else {
                video = app.videosInQueue.find(v => v.id === id);
            }
            if (video) {
                video.progressPct = pct;
                video.progressSpeed = speed;
                video.downloadedBytes = downloadedBytes || 0;
                video.totalBytes = totalBytes || 0;
            }

            // Update main-queue card if present
            const card = document.getElementById(`item-${id}`);
            if (card) {
                let bar = card.querySelector('.progress-bar');
                let text = card.querySelector('.progress-text');
                // If the status container was rebuilt (e.g. by renderQueue for a restored queue or
                // filter switch), the progress DOM is gone. Rebuild it so the active download stays visible.
                if (!bar || !text) {
                    const container = card.querySelector('.item-status-container');
                    if (container) {
                        container.innerHTML = `
                            <div class="progress-container">
                                <div class="progress-text">0% (0 KB/s)</div>
                                <div class="progress-bar-bg"><div class="progress-bar"></div></div>
                            </div>
                        `;
                        bar = card.querySelector('.progress-bar');
                        text = card.querySelector('.progress-text');
                    }
                }
                if (bar) bar.style.width = pct + '%';
                // Drop the "(speed)" suffix when speed is empty — the final
                // 99%/100% ticks come through with an empty speed string, and
                // "100% ()" looks broken. Show just the percentage then.
                if (text) text.textContent = speed ? `${Math.floor(pct)}% (${speed})` : `${Math.floor(pct)}%`;
            }

            // Update detail-view row if its playlist is open
            if (parentPlaylist && app.currentPlaylistId === parentPlaylist.id) {
                const row = document.getElementById(`pd-v-${id}`);
                if (row) {
                    if (row.classList.contains('library-card')) {
                        // Channel grid card — updates the watch-progress bar
                        // pinned at the thumb's bottom + the small %xx pill
                        // top-right. Without this the bar stayed at 0% width
                        // and the pill stayed at 0% until status flipped to
                        // Done, making downloads look frozen.
                        const watchBar = row.querySelector('.library-card-watch-progress');
                        if (watchBar) watchBar.style.width = pct + '%';
                        const pill = row.querySelector('.library-card-state-badge.s-downloading');
                        if (pill) pill.textContent = `${Math.floor(pct)}%`;
                    } else {
                        // Legacy playlist row layout
                        const bar = row.querySelector('.pd-video-progress-bar');
                        const text = row.querySelector('.pd-video-progress-text');
                        if (bar) bar.style.width = pct + '%';
                        if (text) text.textContent = speed ? `${Math.floor(pct)}% · ${speed}` : `${Math.floor(pct)}%`;
                    }
                }
            }

            // Update playlist card rollup
            if (parentPlaylist) {
                app.updatePlaylistCardRollup(parentPlaylist.id);
            }
        }

        // Refetch progress — called as each library entry gets its metadata refreshed.
        // We don't re-render the whole library per tick (too jittery); instead we refresh
        // in small batches so new thumbnails pop into the grid progressively.
        async function refetchProgress(idx, total, title) {
            if (idx % 5 === 0 || idx === total) {
                try {
                    app.videosInLibrary = (await pywebview.api.load_library()) || [];
                    app.renderLibrary();
                } catch(_) {}
            }
        }

        function refetchComplete(updated, failed) {
            pywebview.api.load_library().then(lib => {
                app.videosInLibrary = lib || [];
                app.renderLibrary();
            });
        }

        function finishProcessing(completedCount) {
            app.elements.downloadButton.classList.remove('hidden');
            app.elements.cancelButton.classList.add('hidden');
            app.updateDashboard();
            // Toast only if videos actually completed in this batch. When the batch ends
            // because everything got paused/cancelled/errored, completedCount is 0 and we stay quiet.
            const count = typeof completedCount === 'number' ? completedCount : 0;
            if (count > 0) {
                showToast(`${count} download${count === 1 ? '' : 's'} complete`, 'OK', () => {});
            }
        }

        function formatBytes(b) {
            if (b === 0) return '0 B';
            const i = Math.floor(Math.log(b) / Math.log(1024));
            return (b / Math.pow(1024, i)).toFixed(2) * 1 + ' ' + ['B', 'KB', 'MB', 'GB', 'TB'][i];
        }

        // 'channel' for channel-style URLs (Videos tab of @handle / /c/ / /channel/ / /user/),
        // 'playlist' otherwise. Mirrors the Python _classify_playlist_url helper so
        // entries fetched before the subtype field existed still classify correctly.
        function classifyPlaylistEntry(entry) {
            if (!entry) return 'playlist';
            if (entry.subtype === 'channel' || entry.subtype === 'playlist') return entry.subtype;
            const u = (entry.url || '').toLowerCase();
            if (u.includes('/@') || u.includes('/channel/') || u.includes('/c/') || u.includes('/user/')) return 'channel';
            return 'playlist';
        }

        // ============================================================
