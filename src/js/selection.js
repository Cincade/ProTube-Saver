        // SELECTION MODE — multi-select for library cards.
        // ================================================================
        // Activation: right-click any library card, OR click the per-card
        // checkbox that appears on hover. Once active, every card click
        // toggles selection (instead of opening detail). Selected cards get
        // a blue ring + checkmark. An action bar slides up showing Delete /
        // Remove / Cancel options. Esc cancels.
        // ================================================================
        const Selection = (() => {
            let active = false;
            let selectedIds = new Set();
            // Long-press was previously implemented here — removed because mouse jitter
            // on desktop made it cancel before the timer fired, and the suppress-next-click
            // flag was fragile. Right-click and the per-card checkbox both still enter
            // selection mode reliably, so no functionality is lost.

            function enter(initialId = null) {
                active = true;
                document.body.classList.add('in-selection-mode');
                document.querySelectorAll('.library-card').forEach(card => {
                    card.classList.add('is-selectable');
                });
                if (initialId) toggleId(initialId);
                else updateActionBar();
            }

            function exit() {
                active = false;
                selectedIds.clear();
                document.body.classList.remove('in-selection-mode');
                document.querySelectorAll('.library-card').forEach(card => {
                    card.classList.remove('is-selectable', 'is-selected');
                });
                hideActionBar();
            }

            function toggleId(id) {
                if (selectedIds.has(id)) selectedIds.delete(id);
                else selectedIds.add(id);
                const card = document.querySelector(`.library-card[data-item-id="${id}"]`);
                if (card) card.classList.toggle('is-selected', selectedIds.has(id));
                if (selectedIds.size === 0) {
                    // Auto-exit when last selection cleared — feels natural
                    exit();
                } else {
                    updateActionBar();
                }
            }

            function selectAll() {
                document.querySelectorAll('.library-card').forEach(card => {
                    const id = card.getAttribute('data-item-id');
                    if (id) {
                        selectedIds.add(id);
                        card.classList.add('is-selected');
                    }
                });
                updateActionBar();
            }

            function updateActionBar() {
                const bar = document.getElementById('selection-actionbar');
                const countEl = document.getElementById('selection-actionbar-count');
                if (!bar) return;
                if (active && selectedIds.size > 0) {
                    countEl.textContent = `${selectedIds.size} selected`;
                    bar.classList.add('visible');
                } else if (active) {
                    countEl.textContent = `0 selected`;
                    bar.classList.add('visible');
                } else {
                    hideActionBar();
                }

                // Dynamic Hide/Show label — if every selected item is already
                // hidden, the button unhides; otherwise it hides. Mirrors the
                // pattern of "Deselect all" appearing when you've selected all.
                const hideLabel = document.getElementById('selection-hide-btn-label');
                if (hideLabel && selectedIds.size > 0) {
                    let allHidden = true;
                    for (const id of selectedIds) {
                        const item = findLibraryItemById(id);
                        if (!item || !item.hidden) { allHidden = false; break; }
                    }
                    hideLabel.textContent = allHidden ? 'Show' : 'Hide';
                } else if (hideLabel) {
                    hideLabel.textContent = 'Hide';
                }

                // Same logic for the Pin button — if everything selected is
                // already pinned, button reads "Unpin"; otherwise "Pin".
                const pinLabel = document.getElementById('selection-pin-btn-label');
                if (pinLabel && selectedIds.size > 0) {
                    let allPinned = true;
                    for (const id of selectedIds) {
                        const item = findLibraryItemById(id);
                        if (!item || !item.pinned) { allPinned = false; break; }
                    }
                    pinLabel.textContent = allPinned ? 'Unpin' : 'Pin';
                } else if (pinLabel) {
                    pinLabel.textContent = 'Pin';
                }
            }

            // Locate a library item by id — top-level OR inside a playlist's videos[].
            // Used by hide/unhide bulk action since users can multi-select children.
            function findLibraryItemById(id) {
                let item = app.videosInLibrary.find(i => i.id === id);
                if (item) return item;
                for (const v of app.videosInLibrary) {
                    if (v.type === 'playlist') {
                        const c = (v.videos || []).find(c => c.id === id);
                        if (c) return c;
                    }
                }
                return null;
            }

            function hideActionBar() {
                const bar = document.getElementById('selection-actionbar');
                if (bar) bar.classList.remove('visible');
            }

            // Bulk delete: removes file + library entry for each selected id.
            // Single confirm dialog mentioning the count, no per-item prompts.
            async function deleteSelected() {
                if (selectedIds.size === 0) return;
                const count = selectedIds.size;
                const ok = await confirmDialog({
                    title: `Delete ${count} ${count === 1 ? 'video' : 'videos'} from disk?`,
                    body: `${count} ${count === 1 ? 'video file' : 'video files'} will be deleted from disk along with their folders (for standalone videos). Items in playlists keep their folders. This cannot be undone.`,
                    confirmText: `Delete ${count}`,
                    cancelText: 'Cancel',
                    danger: true,
                });
                if (!ok) return;

                const ids = Array.from(selectedIds);
                const total = ids.length;
                const progEl = document.getElementById('import-progress');
                const textEl = progEl?.querySelector('.ipro-text');
                const countEl = progEl?.querySelector('.ipro-count');
                const fillEl = progEl?.querySelector('.ipro-bar-fill');
                if (progEl) {
                    if (textEl) textEl.textContent = 'Deleting…';
                    if (countEl) countEl.textContent = `0 / ${total}`;
                    if (fillEl) fillEl.style.width = '0%';
                    progEl.removeAttribute('hidden');
                }

                let deleted = 0;
                let errors = 0;
                let processed = 0;
                for (const id of ids) {
                    try {
                        const res = await pywebview.api.delete_video_from_library_and_disk(id);
                        if (res?.ok) deleted++;
                        else errors++;
                    } catch (e) {
                        errors++;
                    }
                    processed++;
                    if (countEl) countEl.textContent = `${processed} / ${total}`;
                    if (fillEl) fillEl.style.width = `${(processed / total) * 100}%`;
                }
                if (progEl) progEl.setAttribute('hidden', '');
                exit();
                app.videosInLibrary = (await pywebview.api.load_library()) || [];
                app.renderLibrary();
                if (errors === 0) {
                    showToast(`Deleted ${deleted} ${deleted === 1 ? 'video' : 'videos'}`, null, null);
                } else {
                    showToast(`Deleted ${deleted}, ${errors} failed (files locked?)`, null, null);
                }
            }

            // Bulk remove from library: clears entries, leaves files on disk.
            // Single Undo via toast — restores all items at once.
            async function removeSelected() {
                if (selectedIds.size === 0) return;
                const count = selectedIds.size;
                const ids = Array.from(selectedIds);
                // Snapshot full items for potential undo
                const snapshots = [];
                for (const id of ids) {
                    let item = app.videosInLibrary.find(i => i.id === id);
                    if (!item) {
                        for (const v of app.videosInLibrary) {
                            if (v.type === 'playlist') {
                                const c = (v.videos || []).find(c => c.id === id);
                                if (c) { item = c; break; }
                            }
                        }
                    }
                    if (item) snapshots.push(JSON.parse(JSON.stringify(item)));
                }
                for (const id of ids) {
                    try { await pywebview.api.remove_from_library(id); } catch (_) {}
                }
                exit();
                app.videosInLibrary = (await pywebview.api.load_library()) || [];
                app.renderLibrary();
                showToast(
                    `Removed ${count} ${count === 1 ? 'item' : 'items'} from library`,
                    'Undo',
                    async () => {
                        for (const snap of snapshots) {
                            try {
                                if (snap.type === 'playlist') {
                                    await pywebview.api.add_playlist_to_library(snap);
                                } else {
                                    await pywebview.api.add_to_library(snap);
                                }
                            } catch (_) {}
                        }
                        app.videosInLibrary = (await pywebview.api.load_library()) || [];
                        app.renderLibrary();
                    }
                );
            }

            // Bulk hide / unhide. Default action: hide everything selected.
            // If EVERY selected item is already hidden, flips to unhide them
            // (the action bar's button label updates live to match — see
            // updateActionBar above). Always offers an Undo toast for one-click
            // reversal.
            //
            // Uses the backend's batch method (set_videos_hidden_batch) so all
            // 49+ items become a single settings.json write — sequential awaits
            // were taking long enough to race or stall on big selections.
            //
            // For UX: shows a progress card while the backend works, then plays
            // a fade-out animation on the affected cards so they don't all
            // disappear in one frame. Renders the updated library only after
            // the fade completes.
            async function hideSelected() {
                if (selectedIds.size === 0) return;
                const ids = Array.from(selectedIds);
                // Decide direction: unhide only if EVERY selected is currently hidden
                let allHidden = true;
                for (const id of ids) {
                    const item = findLibraryItemById(id);
                    if (!item || !item.hidden) { allHidden = false; break; }
                }
                const wantHidden = !allHidden;

                // Show the progress card — same one bulk delete uses. We don't
                // get per-item progress from the batch call (it's one quick
                // round-trip), but the card serves as confirmation + prevents
                // the user from thinking nothing happened on slower machines.
                const total = ids.length;
                const progEl = document.getElementById('import-progress');
                const textEl = progEl?.querySelector('.ipro-text');
                const countEl = progEl?.querySelector('.ipro-count');
                const fillEl = progEl?.querySelector('.ipro-bar-fill');
                if (progEl) {
                    if (textEl) textEl.textContent = wantHidden ? 'Hiding…' : 'Showing…';
                    if (countEl) countEl.textContent = `0 / ${total}`;
                    if (fillEl) fillEl.style.width = '0%';
                    progEl.removeAttribute('hidden');
                }

                // Smooth fade on the affected cards BEFORE we re-render. Without
                // this, app.renderLibrary() would replace innerHTML and every
                // card would vanish in one frame — feels jarring at 49 items.
                // CSS class .library-card.hiding-out drives a 280ms fade+scale.
                if (wantHidden) {
                    ids.forEach(id => {
                        const card = document.querySelector(`.library-card[data-item-id="${id}"]`);
                        if (card) card.classList.add('hiding-out');
                    });
                }

                // One backend call instead of 49 — atomic save, no race window.
                let flipped = [];
                try {
                    const res = await pywebview.api.set_videos_hidden_batch(ids, wantHidden);
                    flipped = (res && res.flipped) || [];
                } catch (e) {
                    if (progEl) progEl.setAttribute('hidden', '');
                    showToast('Hide failed', null, null);
                    // Revert the visual fade so cards come back
                    ids.forEach(id => {
                        const card = document.querySelector(`.library-card[data-item-id="${id}"]`);
                        if (card) card.classList.remove('hiding-out');
                    });
                    return;
                }
                if (countEl) countEl.textContent = `${flipped.length} / ${total}`;
                if (fillEl) fillEl.style.width = `${total > 0 ? (flipped.length / total) * 100 : 100}%`;

                // Wait for the fade-out animation to play before swapping the DOM.
                // Skip the wait when unhiding — those cards aren't in view at all,
                // they're being added BACK to the grid, so there's nothing to fade.
                if (wantHidden && flipped.length > 0) {
                    await new Promise(r => setTimeout(r, 280));
                }

                exit();
                if (progEl) progEl.setAttribute('hidden', '');
                app.videosInLibrary = (await pywebview.api.load_library()) || [];
                app.renderLibrary();

                const n = flipped.length;
                if (n === 0) return;  // nothing actually changed
                const verb = wantHidden ? 'Hidden' : 'Shown';
                showToast(
                    `${verb} ${n} ${n === 1 ? 'item' : 'items'}`,
                    'Undo',
                    async () => {
                        try { await pywebview.api.set_videos_hidden_batch(flipped, !wantHidden); } catch (_) {}
                        app.videosInLibrary = (await pywebview.api.load_library()) || [];
                        app.renderLibrary();
                    }
                );
            }

            // Bulk pin / unpin. Mirrors hideSelected: dynamic direction (unpin
            // only when EVERY selected is already pinned; otherwise pin all
            // un-pinned ones), single batched backend call, Undo toast that
            // only flips items actually changed.
            //
            // No fade animation — pinning re-sorts the grid (pinned to top)
            // rather than removing cards, so we just wait for renderLibrary to
            // place things in the new order. The visual reorder itself is the
            // confirmation.
            async function pinSelected() {
                if (selectedIds.size === 0) return;
                const ids = Array.from(selectedIds);
                let allPinned = true;
                for (const id of ids) {
                    const item = findLibraryItemById(id);
                    if (!item || !item.pinned) { allPinned = false; break; }
                }
                const wantPinned = !allPinned;
                let flipped = [];
                try {
                    const res = await pywebview.api.set_videos_pinned_batch(ids, wantPinned);
                    flipped = (res && res.flipped) || [];
                } catch (e) {
                    showToast('Pin failed', null, null);
                    return;
                }
                exit();
                app.videosInLibrary = (await pywebview.api.load_library()) || [];
                app.renderLibrary();
                const n = flipped.length;
                if (n === 0) return;
                const verb = wantPinned ? 'Pinned' : 'Unpinned';
                showToast(
                    `${verb} ${n} ${n === 1 ? 'item' : 'items'}`,
                    'Undo',
                    async () => {
                        try { await pywebview.api.set_videos_pinned_batch(flipped, !wantPinned); } catch (_) {}
                        app.videosInLibrary = (await pywebview.api.load_library()) || [];
                        app.renderLibrary();
                    }
                );
            }

            return {
                isActive: () => active,
                handleCardMouseDown(e, cardEl, id) {
                    // No-op: long-press removed. Right-click and per-card checkbox
                    // handle entry into selection mode.
                },
                handleCardMouseMove(e) { /* no-op — long-press removed */ },
                handleCardMouseUp() { /* no-op — long-press removed */ },
                handleCardContextMenu(e, id) {
                    e.preventDefault();
                    if (!active) enter(id);
                    else toggleId(id);
                },
                handleCardClick(e, id) {
                    // Returns true if click was consumed (selection mode toggle).
                    if (!active) return false;
                    e.preventDefault();
                    e.stopPropagation();
                    toggleId(id);
                    return true;
                },
                refreshAfterRender() {
                    // Called after app.renderLibrary() — re-applies selection state
                    // to newly-rendered DOM (which loses the .is-selected class).
                    if (!active) return;
                    document.querySelectorAll('.library-card').forEach(card => {
                        card.classList.add('is-selectable');
                        const id = card.getAttribute('data-item-id');
                        if (id && selectedIds.has(id)) {
                            card.classList.add('is-selected');
                        }
                    });
                },
                exit,
                deleteSelected,
                removeSelected,
                hideSelected,
                pinSelected,
                selectAll,
            };
        })();

        // Wire up listeners on document body — one delegated handler covers all cards
        // even ones rendered after page load.
        // Right-click on a library card enters selection mode (or toggles the card).
        document.addEventListener('contextmenu', (e) => {
            const card = e.target.closest('.library-card');
            if (!card) return;
            const id = card.getAttribute('data-item-id');
            if (!id) return;
            Selection.handleCardContextMenu(e, id);
        });
        // Intercept clicks on cards during selection mode — prevent openLibraryDetail
        document.addEventListener('click', (e) => {
            if (!Selection.isActive()) return;
            const card = e.target.closest('.library-card');
            if (!card) return;
            // Only intercept clicks ON cards, not on action bar buttons etc.
            if (e.target.closest('.selection-actionbar')) return;
            const id = card.getAttribute('data-item-id');
            if (!id) return;
            Selection.handleCardClick(e, id);
        }, true /* capture phase so we beat the inline onclick */);
        // Esc cancels
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && Selection.isActive()) {
                Selection.exit();
            }
        });

        // Action bar buttons
        window.addEventListener('DOMContentLoaded', () => {
            const wire = (id, fn) => {
                const el = document.getElementById(id);
                if (el) el.addEventListener('click', fn);
            };
            wire('selection-cancel-btn', () => Selection.exit());
            wire('selection-delete-btn', () => Selection.deleteSelected());
            wire('selection-remove-btn', () => Selection.removeSelected());
            wire('selection-hide-btn', () => Selection.hideSelected());
            wire('selection-pin-btn', () => Selection.pinSelected());
            wire('selection-select-all-btn', () => Selection.selectAll());
        });

        // Toast Notification System
        let removedVideos = [];
        let currentToast = null;
        let toastTimer = null;

        // ============================================================
