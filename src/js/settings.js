        // SETTINGS DRAWER — opened from the rail's settings button.
        // Read-on-open + write-on-change pattern: each control commits its
        // change to the backend immediately; no Save button. ESC, backdrop
        // click, and the X all close the drawer. About section is rendered
        // from a single get_about_info() snapshot at open time.
        // ============================================================
        let _settingsKeyHandler = null;
        let _settingsConcurrentTimer = null;

        async function openSettings() {
            const drawer = document.getElementById('settings-drawer');
            const backdrop = document.getElementById('settings-backdrop');
            if (!drawer || !backdrop) return;

            // Pull current values from backend — settings are stored in the
            // same settings.json as everything else, fetched via the existing
            // get_setting / get_about_info API surface.
            let about = null;
            let downloadFolder = '';
            let defaultQuality = '1080p';
            let defaultView = 'library';
            let defaultSpeed = 1;
            let autoAdd = true;
            let groqKey = '';
            let autoPolish = false;
            try {
                about = await pywebview.api.get_about_info();
                downloadFolder = (await pywebview.api.get_setting('download_folder')) || '';
                defaultQuality = (await pywebview.api.get_setting('default_quality')) || '1080p';
                defaultView = (await pywebview.api.get_setting('default_startup_view')) || 'library';
                defaultSpeed = parseFloat((await pywebview.api.get_setting('default_speed')) || '1') || 1;
                const autoAddSetting = await pywebview.api.get_setting('auto_add_to_library');
                // Treat unset as "on" (current default behavior — only flip OFF
                // is meaningful; the existing playlist-completion code auto-adds
                // by default).
                autoAdd = (autoAddSetting === null || autoAddSetting === undefined) ? true : !!autoAddSetting;
                groqKey = (await pywebview.api.get_setting('groq_api_key')) || '';
                autoPolish = !!(await pywebview.api.get_setting('auto_polish_subtitles'));
            } catch (e) {
                console.error('Settings load failed:', e);
            }

            // Concurrent slider
            const slider = document.getElementById('settings-concurrent');
            const valueEl = document.getElementById('settings-concurrent-value');
            const concurrent = (about && about.max_concurrent_downloads) || 2;
            slider.value = concurrent;
            valueEl.textContent = concurrent;

            // Default quality picker
            const qPicker = document.getElementById('settings-default-quality');
            qPicker.value = ['best','2160p','1440p','1080p','720p','480p','audio'].includes(defaultQuality)
                ? defaultQuality : '1080p';

            // Default startup view picker — applied at next launch (changing
            // it now doesn't switch the current view, just the boot default).
            const viewPicker = document.getElementById('settings-default-view');
            if (viewPicker) {
                viewPicker.value = (defaultView === 'queue') ? 'queue' : 'library';
            }

            // Default playback speed picker
            const sPicker = document.getElementById('settings-default-speed');
            if (sPicker) {
                const validSpeeds = ['0.5','0.75','1','1.25','1.5','1.75','2'];
                sPicker.value = validSpeeds.includes(String(defaultSpeed)) ? String(defaultSpeed) : '1';
                app._defaultSpeed = defaultSpeed;
            }

            // Download folder
            const folderEl = document.getElementById('settings-folder-path');
            folderEl.textContent = downloadFolder || '(default)';
            folderEl.title = downloadFolder;

            // Auto-add toggle
            const toggle = document.getElementById('settings-auto-add');
            toggle.classList.toggle('on', autoAdd);
            toggle.setAttribute('aria-checked', autoAdd ? 'true' : 'false');

            // Groq API key field + auto-polish toggle (AI section)
            const groqInput = document.getElementById('settings-groq-key');
            if (groqInput) groqInput.value = groqKey || '';
            const polishToggle = document.getElementById('settings-auto-polish');
            if (polishToggle) {
                polishToggle.classList.toggle('on', autoPolish);
                polishToggle.setAttribute('aria-checked', autoPolish ? 'true' : 'false');
            }
            // Cache for the player to read without another backend round-trip.
            window._autoPolishSubtitles = autoPolish;
            window._hasGroqKey = !!groqKey;

            // About section
            const fmtBytes = (b) => {
                if (!b || b === 0) return '0 B';
                const i = Math.floor(Math.log(b) / Math.log(1024));
                return (b / Math.pow(1024, i)).toFixed(2) * 1 + ' ' + ['B','KB','MB','GB','TB'][i];
            };
            document.getElementById('settings-about-version').textContent = about?.version || '—';
            document.getElementById('settings-about-ytdlp').textContent = about?.ytdlp_version || '—';
            const libBytes = about?.library_size_bytes || 0;
            const libCount = about?.library_video_count || 0;
            document.getElementById('settings-about-library').textContent =
                libCount > 0 ? `${fmtBytes(libBytes)} · ${libCount} ${libCount === 1 ? 'video' : 'videos'}` : 'empty';
            document.getElementById('settings-about-queue').textContent = String(about?.queue_count ?? 0);

            // Update-check row — populated from the cached check fired at boot.
            // Hidden when up-to-date (no row clutter); shows v→v + Download
            // when a newer version is available.
            const updateRow = document.getElementById('settings-about-update-row');
            const updateStatus = document.getElementById('settings-about-update-status');
            const info = window._updateInfo;
            if (info && info.has_update) {
                updateStatus.textContent = `v${info.latest} ready`;
                updateRow.removeAttribute('hidden');
            } else {
                updateRow.setAttribute('hidden', '');
            }
            // Cache data-folder path for the "Open data folder" button — handed
            // to open_folder() on click.
            window._settingsDataDir = about?.data_dir || '';

            // Show
            drawer.removeAttribute('hidden');
            drawer.setAttribute('aria-hidden', 'false');
            backdrop.removeAttribute('hidden');
            // Force a reflow before adding .visible so the transform transition fires
            void drawer.offsetWidth;
            drawer.classList.add('visible');
            backdrop.classList.add('visible');

            // ESC closes
            _settingsKeyHandler = (e) => {
                if (e.key === 'Escape') closeSettings();
            };
            document.addEventListener('keydown', _settingsKeyHandler);
        }

        function closeSettings() {
            const drawer = document.getElementById('settings-drawer');
            const backdrop = document.getElementById('settings-backdrop');
            if (!drawer || !backdrop) return;
            drawer.classList.remove('visible');
            backdrop.classList.remove('visible');
            // Hide after transition completes so focus/aria don't leak
            setTimeout(() => {
                drawer.setAttribute('hidden', '');
                drawer.setAttribute('aria-hidden', 'true');
                backdrop.setAttribute('hidden', '');
            }, 240);
            if (_settingsKeyHandler) {
                document.removeEventListener('keydown', _settingsKeyHandler);
                _settingsKeyHandler = null;
            }
        }

        // Sticky window for the "↓ N new" pill. When we just showed the pill,
        // the queue often re-renders (innerHTML replace) which can trigger a
        // spurious scroll event. The auto-hide listener would then check
        // "near-bottom" against a list whose scrollHeight just changed, and
        // for barely-scrollable queues this satisfies the condition and
        // hides the pill instantly. Setting this timestamp via _bumpNewQueuePill
        // gives a 800ms grace period during which auto-hide refuses to fire.
        window._pillStickyUntil = 0;

        // Cache for the auto-add-to-library setting. The all-children-done
        // detector reads this synchronously, so we hydrate it asynchronously
        // at boot. Default true preserves existing behavior until the read
        // completes; only an explicit false flips the gate.
        window._autoAddToLibrary = true;
        (function loadAutoAddCache() {
            if (typeof pywebview === 'undefined' || !pywebview.api || !pywebview.api.get_setting) {
                setTimeout(loadAutoAddCache, 150);
                return;
            }
            pywebview.api.get_setting('auto_add_to_library').then(v => {
                if (v === false) window._autoAddToLibrary = false;
            }).catch(() => { /* ignore — keep default true */ });
        })();

        // Wire up persistent listeners once on first load.
        (function setupSettingsListeners() {
            const drawer = document.getElementById('settings-drawer');
            const backdrop = document.getElementById('settings-backdrop');
            if (!drawer || !backdrop) return;

            document.getElementById('settings-close')?.addEventListener('click', closeSettings);
            backdrop.addEventListener('click', closeSettings);

            // Concurrent slider — debounce so dragging doesn't flood the backend
            // with set_max_concurrent_downloads calls. 250ms is the natural
            // settle-after-release window.
            const slider = document.getElementById('settings-concurrent');
            const valueEl = document.getElementById('settings-concurrent-value');
            slider?.addEventListener('input', () => {
                valueEl.textContent = slider.value;
                if (_settingsConcurrentTimer) clearTimeout(_settingsConcurrentTimer);
                _settingsConcurrentTimer = setTimeout(() => {
                    pywebview.api.set_max_concurrent_downloads(parseInt(slider.value, 10));
                }, 250);
            });

            // Default quality — fires immediately on change
            document.getElementById('settings-default-quality')?.addEventListener('change', (e) => {
                pywebview.api.set_setting('default_quality', e.target.value);
            });

            // Default startup view — saves immediately; takes effect at next
            // launch (we don't switch the current view because the user is
            // mid-session and that'd be confusing).
            document.getElementById('settings-default-view')?.addEventListener('change', (e) => {
                pywebview.api.set_setting('default_startup_view', e.target.value);
            });

            // Default playback speed — applies immediately to app._defaultSpeed so
            // any video opened after the change uses the new rate without a restart.
            document.getElementById('settings-default-speed')?.addEventListener('change', (e) => {
                const rate = parseFloat(e.target.value) || 1;
                app._defaultSpeed = rate;
                pywebview.api.set_setting('default_speed', rate);
            });

            // Download folder — opens native folder picker; backend returns the
            // chosen path (or the unchanged one on cancel).
            document.getElementById('settings-folder-btn')?.addEventListener('click', async () => {
                try {
                    const newPath = await pywebview.api.choose_folder();
                    if (newPath) {
                        const folderEl = document.getElementById('settings-folder-path');
                        folderEl.textContent = newPath;
                        folderEl.title = newPath;
                    }
                } catch (e) {
                    console.error('Folder pick failed:', e);
                }
            });

            // Auto-add toggle — flips visual state, persists the new value,
            // updates the synchronous cache the all-children-done detector reads.
            document.getElementById('settings-auto-add')?.addEventListener('click', (e) => {
                const btn = e.currentTarget;
                const next = !btn.classList.contains('on');
                btn.classList.toggle('on', next);
                btn.setAttribute('aria-checked', next ? 'true' : 'false');
                window._autoAddToLibrary = next;
                pywebview.api.set_setting('auto_add_to_library', next);
            });

            // Groq API key — persist on blur (not every keystroke). Updates the
            // _hasGroqKey cache so the player's AI buttons can show/hide accordingly.
            let _groqKeySaveTimer = null;
            document.getElementById('settings-groq-key')?.addEventListener('input', (e) => {
                if (_groqKeySaveTimer) clearTimeout(_groqKeySaveTimer);
                _groqKeySaveTimer = setTimeout(() => {
                    const v = e.target.value.trim();
                    pywebview.api.set_setting('groq_api_key', v);
                    window._hasGroqKey = !!v;
                }, 500);
            });
            // External link to the Groq console — open in default browser via the
            // existing open_external_url helper (don't load it inside WebView2).
            document.getElementById('settings-groq-link')?.addEventListener('click', (e) => {
                e.preventDefault();
                if (pywebview?.api?.open_external_url) {
                    pywebview.api.open_external_url('https://console.groq.com/keys');
                }
            });
            // Auto-polish toggle
            document.getElementById('settings-auto-polish')?.addEventListener('click', (e) => {
                const btn = e.currentTarget;
                const next = !btn.classList.contains('on');
                btn.classList.toggle('on', next);
                btn.setAttribute('aria-checked', next ? 'true' : 'false');
                window._autoPolishSubtitles = next;
                pywebview.api.set_setting('auto_polish_subtitles', next);
            });

            // Open data folder — uses the cached path from get_about_info().
            // Handy for "I want to look at settings.json / logs / downloads"
            // without leaving the app.
            document.getElementById('settings-open-data-folder')?.addEventListener('click', () => {
                const path = window._settingsDataDir;
                if (path) pywebview.api.open_folder(path);
            });

            // Manual yt-dlp update — yt-dlp ships fixes ~weekly when YouTube
            // changes things; the daily auto-check usually catches them, but
            // this is the escape valve when a user is mid-bug. The backend
            // streams the result through showToast.
            document.getElementById('settings-ytdlp-update')?.addEventListener('click', async (e) => {
                const btn = e.currentTarget;
                btn.disabled = true;
                const originalText = btn.textContent;
                btn.textContent = 'Updating…';
                try {
                    await pywebview.api.force_update_ytdlp();
                    // The backend toasts the result asynchronously when done;
                    // re-enable the button after a beat so the user can retry.
                    setTimeout(() => {
                        btn.disabled = false;
                        btn.textContent = originalText;
                    }, 4000);
                } catch (err) {
                    showToast('Update check failed', null, null);
                    btn.disabled = false;
                    btn.textContent = originalText;
                }
            });

            // App update — force-check the landing site for a newer release.
            // Bypasses the 24h cache so the user can re-check on demand.
            document.getElementById('settings-app-update-check')?.addEventListener('click', async (e) => {
                const btn = e.currentTarget;
                btn.disabled = true;
                const orig = btn.textContent;
                btn.textContent = 'Checking…';
                try {
                    const info = await app._checkForAppUpdates(true);
                    // Refresh the About row inline based on the fresh result
                    const updateRow = document.getElementById('settings-about-update-row');
                    const updateStatus = document.getElementById('settings-about-update-status');
                    if (info && info.has_update) {
                        updateStatus.textContent = `v${info.latest} ready`;
                        updateRow.removeAttribute('hidden');
                        showToast(`Update v${info.latest} available`, null, null);
                    } else {
                        updateRow.setAttribute('hidden', '');
                        if (info && info.error) {
                            showToast(`Couldn't check: ${info.error}`, null, null);
                        } else {
                            showToast('Up to date', null, null);
                        }
                    }
                } finally {
                    btn.disabled = false;
                    btn.textContent = orig;
                }
            });

            // App update download — opens the release URL in the user's browser
            // so they can grab the new exe (manual install: close ProTube,
            // replace the .exe in the install folder, reopen).
            document.getElementById('settings-app-update-download')?.addEventListener('click', () => {
                const url = (window._updateInfo && window._updateInfo.downloadUrl) || '';
                if (url) {
                    try { pywebview.api.open_external_url(url); } catch (_) {}
                } else {
                    showToast('No download URL on the latest release', null, null);
                }
            });
        })();

        function showToast(message, actionText, onAction) {
            const container = document.getElementById('toast-container');

            // Clear existing timer
            if (toastTimer) {
                clearTimeout(toastTimer);
            }

            // If we already have a toast onscreen, replace it rather than mutate it partially.
            // Mutating only the message left stale icons and action buttons behind.
            if (currentToast) {
                currentToast.remove();
                currentToast = null;
            }
            createToast(message, actionText, onAction);

            // Reset auto-hide timer. Give toasts with actions more time to be
            // seen/clicked. Also give error-shaped toasts longer (failed
            // downloads can have multi-line yt-dlp output the user needs to
            // read). Detection is lowercase substring match — fragile, but
            // every error/failed/can't toast text in the codebase contains
            // one of these tokens, so it's reliable enough.
            const m = (message || '').toLowerCase();
            const isError = m.includes('fail') || m.includes('error') || m.includes("can't");
            const lifetime = actionText ? 8000 : (isError ? 10000 : 4000);
            toastTimer = setTimeout(() => {
                hideToast();
            }, lifetime);
        }

        function createToast(message, actionText, onAction) {
            const container = document.getElementById('toast-container');

            const toast = document.createElement('div');
            toast.className = 'toast';
            currentToast = toast;

            // Choose icon based on context. Trash-can only makes sense for "removed" toasts
            // (which explicitly pass 'Undo' as the action). Everything else gets a neutral
            // info-circle icon.
            const isUndoToast = actionText === 'Undo';
            const iconSvg = isUndoToast
                ? '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/>'
                : '<circle cx="12" cy="12" r="10" stroke-width="2"/><line x1="12" y1="8" x2="12" y2="12" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="16" r="1" fill="currentColor"/>';

            // Build the toast structurally instead of interpolating `message` and
            // `actionText` into innerHTML. Both come from callers that often pass
            // YouTube-derived content (video titles, playlist names) — a title like
            // `<img src=x onerror=...>` would otherwise execute in our app context
            // with full pywebview.api access. textContent assignment is XSS-safe
            // by definition; iconSvg below is the only innerHTML write and the
            // string is hard-coded above (no user data).
            toast.innerHTML = `
                <svg class="toast-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    ${iconSvg}
                </svg>
                <span class="toast-message"></span>
            `;
            toast.querySelector('.toast-message').textContent = message;

            // Only render the action button when actionText is a non-empty string.
            // Previously a "null" button showed whenever callers passed null/undefined.
            if (actionText && typeof actionText === 'string') {
                const btn = document.createElement('button');
                btn.className = 'toast-action';
                btn.textContent = actionText;
                toast.appendChild(btn);
            }

            container.appendChild(toast);

            const actionBtn = toast.querySelector('.toast-action');
            if (actionBtn) {
                actionBtn.onclick = () => {
                    if (onAction) onAction();
                    hideToast();
                };
            }
        }

        function hideToast() {
            if (currentToast) {
                currentToast.classList.add('hiding');
                setTimeout(() => {
                    currentToast.remove();
                    currentToast = null;
                    removedVideos = [];
                }, 200);
            }
            
            if (toastTimer) {
                clearTimeout(toastTimer);
                toastTimer = null;
            }
        }



        // Open/close the custom quality dropdown. Close any other open menus first.
        function toggleQualityMenu(event, videoId) {
            event.stopPropagation();
            const picker = document.getElementById(`qpicker-${videoId}`);
            if (!picker) return;
            const wasOpen = picker.classList.contains('open');
            // Single source of truth for closing — also resets inline positioning.
            closeAllDropdownMenus();
            if (!wasOpen) {
                picker.classList.add('open');
                // Position via fixed coords (escapes scroll-container clipping)
                // and pick flip direction based on viewport space.
                // Must be AFTER `.open` is set so the menu has real dimensions.
                flipMenuIfNeeded(picker, picker.querySelector('.quality-menu'));
            }
        }

        // User picked a quality from the custom dropdown
        function pickQuality(videoId, quality) {
            const video = app.videosInQueue.find(v => v.id === videoId);
            if (!video) return;
            video.selectedQuality = quality;
            app.saveQueueState();
            app.renderQueue();
        }

        // Set the default quality for the current playlist
        function pickPlaylistDefaultQuality(playlistId, quality) {
            const pl = app.videosInQueue.find(i => i.type === 'playlist' && i.id === playlistId);
            if (!pl) return;
            pl.defaultQuality = quality;
            // Propagate to any child video that hasn't been individually set
            pl.videos.forEach(v => {
                // Only apply default if the child doesn't have an override OR if the override matches the old default
                // Simple approach: just set all children's selectedQuality to the new default
                if (v.formats) v.selectedQuality = quality;
            });
            // Close the dropdown (also resets inline positioning)
            closeAllDropdownMenus();
            // CHANNEL mode: the quality picker lives in the bottom selection action
            // bar (the "pill"). A full renderPlaylistDetail() here tore that bar AND
            // the current selection down, so the pill vanished and the user had to
            // re-click a video to bring it back. Update the label IN PLACE instead —
            // the data (defaultQuality + child selectedQuality) is already set above,
            // so nothing else needs to repaint.
            const chanQ = document.getElementById('pd-channel-action-quality');
            const chanLabel = document.getElementById('pd-channel-action-quality-label');
            if (chanQ && chanLabel) {
                chanQ.classList.remove('open');
                chanLabel.textContent = quality;
                app.saveQueueState();
                return;
            }
            // Playlist (non-channel) hero view: per-card quality badges rely on the
            // re-render to reflect the new default, and there's no floating pill to
            // lose there, so keep the original behavior.
            if (app.currentPlaylistId === playlistId) {
                app.renderPlaylistDetail(pl);
            }
            app.saveQueueState();
        }

        // Called by backend when a download picks up mid-way through a .part file
        function showResumeToast(title, pct) {
            showToast(`Resuming "${title}" from ${pct}%`, 'OK', () => {});
        }

        // Backend → frontend callbacks for playlist format resolution
        function onVideoFormatsResolved(playlistId, payload, idx, total) {
            app.onVideoFormatsResolved(playlistId, payload);
        }
        function onVideoFormatsFailed(playlistId, videoId, errMsg) {
            app.onVideoFormatsFailed(playlistId, videoId, errMsg);
        }
        function onPlaylistFormatsComplete(playlistId) {
            app.onPlaylistFormatsComplete(playlistId);
        }

        // Backend tick from check_playlist_updates as yt-dlp streams entries.
        // Updates the count text in the import-progress card. Gated by
        // window._activeUpdateCheckId (set in checkPlaylistUpdates) so a stale
        // tick from a previous check can't write into a new one.
        function onUpdateCheckProgress(playlistId, count) {
            if (window._activeUpdateCheckId !== playlistId) return;
            const progEl = document.getElementById('import-progress');
            if (!progEl || progEl.hasAttribute('hidden')) return;
            const countEl = progEl.querySelector('.ipro-count');
            if (countEl) countEl.textContent = `${count} new`;
        }

        // ============================================================
        // App update pill — shown when check_for_updates() reports a newer
        // version is published. Click main body opens the download link;
        // the X dismisses for the rest of the session (re-checks next launch).
        // ============================================================
        function showUpdatePill(info) {
            if (!info || !info.has_update) return;
            const pill = document.getElementById('update-available-pill');
            const verEl = document.getElementById('update-pill-version');
            if (!pill || !verEl) return;
            verEl.textContent = `v${info.current} → v${info.latest}`;
            pill.removeAttribute('hidden');
            requestAnimationFrame(() => pill.classList.add('visible'));
        }

        function hideUpdatePill() {
            const pill = document.getElementById('update-available-pill');
            if (!pill) return;
            pill.classList.remove('visible');
            setTimeout(() => {
                if (!pill.classList.contains('visible')) pill.setAttribute('hidden', '');
            }, 220);
        }

        // Wire pill click + dismiss once the DOM is ready. Pill click no
        // longer opens the download URL directly — it pops the update modal
        // so the user can read release notes BEFORE deciding to download.
        (function wireUpdatePill() {
            const pill = document.getElementById('update-available-pill');
            const closeBtn = document.getElementById('update-pill-close');
            if (!pill) return;
            pill.addEventListener('click', (e) => {
                // X click dismisses for the session.
                if (closeBtn && closeBtn.contains(e.target)) {
                    e.stopPropagation();
                    window._updatePillDismissed = true;
                    hideUpdatePill();
                    return;
                }
                showUpdateModal(window._updateInfo);
            });
        })();

        // ============================================================
        // Update modal — opened from pill click. Surfaces release notes from
        // version.json so the user knows what they're getting before they
        // hit Download. "Later" / X / backdrop click all dismiss for the
        // session (next launch will re-check). Download opens the URL in the
        // user's browser via open_external_url.
        // ============================================================
        function showUpdateModal(info) {
            if (!info || !info.has_update) return;
            const backdrop = document.getElementById('update-modal-backdrop');
            const titleEl = document.getElementById('update-modal-title');
            const versionEl = document.getElementById('update-modal-version');
            const notesEl = document.getElementById('update-modal-notes');
            const metaEl = document.getElementById('update-modal-meta');
            const downloadBtn = document.getElementById('update-modal-download');
            if (!backdrop || !versionEl || !notesEl || !downloadBtn) return;

            titleEl.textContent = 'Update available';
            versionEl.textContent = `v${info.current} → v${info.latest}`;

            // Release notes — render a tiny, XSS-safe markdown subset so
            // **bold** and "- " bullets show formatted instead of as literal
            // asterisks/dashes. We escape all HTML first, THEN inject only our
            // own <strong> tags, so notes content can't smuggle markup in.
            // Newlines are preserved by the container's white-space:pre-wrap.
            if (info.releaseNotes && info.releaseNotes.trim()) {
                const esc = info.releaseNotes
                    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
                notesEl.innerHTML = esc
                    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')  // **bold**
                    .replace(/^[ \t]*[-*]\s+/gm, '• ');           // "- " / "* " -> bullet
            } else {
                notesEl.textContent = 'No release notes provided.';
            }

            // Meta row — size + date if known. Hidden via :empty CSS otherwise.
            const metaParts = [];
            if (info.downloadSizeMB) metaParts.push(`~${info.downloadSizeMB} MB`);
            if (info.releasedAt) metaParts.push(`Released ${info.releasedAt}`);
            metaEl.textContent = metaParts.join(' · ');

            // Reset modal to default state each open — handles re-opening after
            // a previous attempt errored or was abandoned mid-download.
            downloadBtn.textContent = `Install v${info.latest}`;
            downloadBtn.disabled = false;
            downloadBtn.dataset.state = 'idle';
            const progEl = document.getElementById('update-modal-progress');
            const errEl = document.getElementById('update-modal-error');
            const barEl = document.getElementById('update-modal-progress-bar');
            const pctEl = document.getElementById('update-modal-progress-pct');
            if (progEl) progEl.classList.remove('visible', 'indeterminate');
            if (errEl) { errEl.textContent = ''; errEl.setAttribute('hidden', ''); }
            if (barEl) barEl.style.width = '0%';
            if (pctEl) pctEl.textContent = '0%';

            backdrop.removeAttribute('hidden');
        }

        function hideUpdateModal(dismissForSession) {
            const backdrop = document.getElementById('update-modal-backdrop');
            if (!backdrop) return;
            backdrop.setAttribute('hidden', '');
            if (dismissForSession) {
                window._updatePillDismissed = true;
                hideUpdatePill();
            }
        }

        // Wire the modal's buttons + dismiss surfaces.
        (function wireUpdateModal() {
            const backdrop = document.getElementById('update-modal-backdrop');
            const closeBtn = document.getElementById('update-modal-close');
            const laterBtn = document.getElementById('update-modal-later');
            const downloadBtn = document.getElementById('update-modal-download');
            if (!backdrop) return;
            // Backdrop click closes (but only if click was on backdrop itself,
            // not bubbled from inside the modal).
            backdrop.addEventListener('click', (e) => {
                if (e.target === backdrop) hideUpdateModal(true);
            });
            closeBtn?.addEventListener('click', () => hideUpdateModal(true));
            laterBtn?.addEventListener('click', () => hideUpdateModal(true));
            downloadBtn?.addEventListener('click', async () => {
                const url = (window._updateInfo && window._updateInfo.downloadUrl) || '';
                const state = downloadBtn.dataset.state || 'idle';

                // State 'ready': user clicked "Restart & install" after a
                // successful in-app download — fire the helper script and exit.
                if (state === 'ready') {
                    downloadBtn.disabled = true;
                    downloadBtn.textContent = 'Restarting…';
                    try {
                        const ok = await pywebview.api.install_staged_update();
                        if (!ok) {
                            // Shouldn't happen — protubeUpdateError would have
                            // fired already and switched us to 'fallback'.
                            downloadBtn.disabled = false;
                            downloadBtn.textContent = 'Restart & install';
                        }
                    } catch (_) {
                        downloadBtn.disabled = false;
                        downloadBtn.textContent = 'Restart & install';
                    }
                    return;
                }

                // State 'fallback': in-app install errored — open the release
                // page in browser so the user can install manually.
                if (state === 'fallback') {
                    if (url) {
                        try { pywebview.api.open_external_url(url); } catch (_) {}
                    }
                    hideUpdateModal(false);
                    hideUpdatePill();
                    return;
                }

                // State 'idle' (default): try in-app install. start_update_download
                // returns false on Windows or in dev mode — fall back to opening
                // the URL in the browser in that case.
                if (!url) return;
                downloadBtn.disabled = true;
                downloadBtn.textContent = 'Starting…';
                let started = false;
                try {
                    started = await pywebview.api.start_update_download(url);
                } catch (_) {}
                if (started) {
                    downloadBtn.dataset.state = 'downloading';
                    downloadBtn.textContent = 'Downloading…';
                    // Stays disabled until protubeUpdateReady or
                    // protubeUpdateError flips it.
                } else {
                    // Not Mac, or no install location detected — manual fallback
                    try { pywebview.api.open_external_url(url); } catch (_) {}
                    hideUpdateModal(false);
                    hideUpdatePill();
                }
            });
            // Esc closes modal when it's open
            document.addEventListener('keydown', (e) => {
                if (e.key === 'Escape' && !backdrop.hasAttribute('hidden')) {
                    hideUpdateModal(true);
                }
            });
        })();

        // Toggle card selection (clickable cards)
        // Track scheduled single-click actions per card so a subsequent dblclick can cancel them.
        const pendingCardClicks = new Map();

        function toggleCardSelection(card, event) {
            // Don't toggle if clicking on buttons/select/inputs/status-actions
            if (event.target.closest('.quality-picker, .extras-wrapper, .remove-btn, .status-icon-btn')) {
                return;
            }

            // For Done cards, defer the selection toggle briefly so dblclick can preempt it.
            if (card.classList.contains('is-done')) {
                const cardId = card.id;
                // Clear any pending single-click timer; the previous click is being replaced/combined
                if (pendingCardClicks.has(cardId)) {
                    clearTimeout(pendingCardClicks.get(cardId));
                }
                const timer = setTimeout(() => {
                    pendingCardClicks.delete(cardId);
                    performSelectionToggle(card);
                }, 230); // slightly longer than typical dblclick window
                pendingCardClicks.set(cardId, timer);
                return;
            }

            // Non-done cards: select immediately, no dance
            performSelectionToggle(card);
        }

        function performSelectionToggle(card) {
            card.classList.toggle('selected');
            const checkbox = card.querySelector('.video-checkbox');
            if (checkbox) {
                checkbox.checked = card.classList.contains('selected');
                app.updateSelection();
            }
        }

        // Attached via ondblclick on rendered Done cards — cancels the pending click and opens file
        function openVideoFromCard(card) {
            if (!card) return;
            const cardId = card.id;
            if (pendingCardClicks.has(cardId)) {
                clearTimeout(pendingCardClicks.get(cardId));
                pendingCardClicks.delete(cardId);
            }
            const videoId = cardId.replace(/^item-/, '');
            openVideoFile(videoId, null);
        }

        // Playlist card: click body opens drilldown, click checkbox/remove stays local
        function handlePlaylistCardClick(event, playlistId) {
            // Controls inside the card should NOT trigger drilldown
            if (event.target.closest('.remove-btn, .drag-handle, input[type="checkbox"]')) {
                return;
            }
            app.openPlaylistDetail(playlistId);
        }

        // Toggle thumbnail/subtitle downloads
        function toggleExtra(btn, videoId, type) {
            const video = app.videosInQueue.find(v => v.id === videoId);
            if (!video) return;
            
            if (type === 'thumb') {
                video.downloadThumbnail = !video.downloadThumbnail;
            } else if (type === 'subs') {
                video.downloadSubtitles = !video.downloadSubtitles;
            }
            
            btn.classList.toggle('active');

            // Update the ⋯ trigger's "has-active" state so the user sees at-a-glance that something is on
            const card = document.getElementById(`item-${videoId}`);
            if (card) {
                const trigger = card.querySelector('.extras-trigger');
                const anyActive = video.downloadThumbnail || video.downloadSubtitles;
                if (trigger) trigger.classList.toggle('has-active', anyActive);
            }

            app.saveQueueState();
        }

        // Open/close the ⋯ extras menu, and close any other open menus first
        function toggleExtrasMenu(event, videoId) {
            event.stopPropagation();
            const target = document.getElementById(`extras-${videoId}`);
            if (!target) return;
            const wrapper = target.closest('.extras-wrapper');

            const wasOpen = target.classList.contains('open');
            closeAllDropdownMenus();
            if (!wasOpen) {
                target.classList.add('open');
                // Position via fixed coords + viewport-aware flip
                if (wrapper) flipMenuIfNeeded(wrapper, target);
            }
        }

        // Shared dropdown placement. Anchors the menu via position: fixed against
        // the trigger's viewport rect, escaping any overflow:hidden / overflow:auto
        // ancestor (the queue's .video-list-container clips absolute children, which
        // is why menus on bottom-of-scroll cards used to disappear). Decides flip
        // direction based on space above vs below the trigger.
        //
        // Lower bound: we cap "space below" at the queue list container's bottom
        // edge, not the viewport bottom. The path-box and Download CTA sit right
        // below the queue area in this layout — without the cap, a dropdown on a
        // mid-page card opens down and overlays those controls, which looks broken
        // even though it's technically standard browser behavior. Same idea on the
        // upper edge: cap at the list container's top so a flipped menu doesn't
        // overlay the URL bar / filter chips.
        //
        // Function name kept for legacy callers — it's now positioning, not just
        // flipping.
        function flipMenuIfNeeded(wrapper, menuEl) {
            if (!wrapper || !menuEl) return;
            const trigger = wrapper.querySelector('.quality-trigger, .extras-trigger') || wrapper.firstElementChild;
            if (!trigger) return;

            // Reset inline positioning so getBoundingClientRect returns the menu's
            // natural height (not whatever we set last time).
            menuEl.style.position = '';
            menuEl.style.top = '';
            menuEl.style.bottom = '';
            menuEl.style.left = '';
            menuEl.style.right = '';
            menuEl.style.maxHeight = '';

            // Force layout, then measure
            // eslint-disable-next-line no-unused-expressions
            menuEl.offsetHeight;
            const triggerRect = trigger.getBoundingClientRect();
            const menuRect = menuEl.getBoundingClientRect();
            const gap = 6;
            const edgeBuffer = 12;

            // Two layers of bounds. Container bounds = ideal (menu stays inside the
            // queue area, no overlay onto path-box / URL bar). Viewport bounds =
            // fallback when the menu doesn't fit either side of the trigger inside
            // the container — better to overlay surrounding UI than to clip the menu
            // and force the user to scroll inside it (which feels broken).
            const listContainer = trigger.closest('.video-list-container');
            const listRect = listContainer ? listContainer.getBoundingClientRect() : null;
            const containerLower = listRect ? Math.min(listRect.bottom, window.innerHeight) : window.innerHeight;
            const containerUpper = listRect ? Math.max(listRect.top, 0) : 0;
            const containerBelow = containerLower - triggerRect.bottom - gap - edgeBuffer;
            const containerAbove = triggerRect.top - containerUpper - gap - edgeBuffer;
            const viewportBelow = window.innerHeight - triggerRect.bottom - gap - edgeBuffer;
            const viewportAbove = triggerRect.top - gap - edgeBuffer;

            // Decide placement in priority order:
            //   (1) inside container, opening down
            //   (2) inside container, opening up
            //   (3) overlaying surrounding UI, opening down
            //   (4) overlaying surrounding UI, opening up
            //   (5) (last resort) pick the larger viewport side, clip with maxHeight
            let flipUp;
            let allowedHeight;  // null = no clipping needed
            if (menuRect.height <= containerBelow) {
                flipUp = false; allowedHeight = null;
            } else if (menuRect.height <= containerAbove) {
                flipUp = true; allowedHeight = null;
            } else if (menuRect.height <= viewportBelow && viewportBelow >= viewportAbove) {
                flipUp = false; allowedHeight = null;
            } else if (menuRect.height <= viewportAbove) {
                flipUp = true; allowedHeight = null;
            } else {
                // Genuinely tight — pick the larger viewport side and clip.
                flipUp = viewportAbove > viewportBelow;
                allowedHeight = Math.max(120, flipUp ? viewportAbove : viewportBelow);
            }

            // Switch to viewport-anchored fixed positioning. This bypasses every
            // overflow:hidden / overflow:auto ancestor in the DOM.
            menuEl.style.position = 'fixed';
            // Right-align to trigger's right edge (matches the natural CSS anchor)
            const rightFromViewport = Math.max(8, window.innerWidth - triggerRect.right);
            menuEl.style.right = `${rightFromViewport}px`;

            if (flipUp) {
                wrapper.classList.add('flip-up');
                menuEl.style.bottom = `${window.innerHeight - triggerRect.top + gap}px`;
                menuEl.style.top = 'auto';
            } else {
                wrapper.classList.remove('flip-up');
                menuEl.style.top = `${triggerRect.bottom + gap}px`;
                menuEl.style.bottom = 'auto';
            }
            if (allowedHeight !== null) {
                menuEl.style.maxHeight = `${allowedHeight}px`;
                menuEl.style.overflowY = 'auto';
            }
        }

        // Reset inline styles applied by flipMenuIfNeeded so a closed menu doesn't
        // hold onto stale positioning. Called by every close path.
        function _resetMenuPosition(menuEl) {
            if (!menuEl) return;
            menuEl.style.position = '';
            menuEl.style.top = '';
            menuEl.style.bottom = '';
            menuEl.style.left = '';
            menuEl.style.right = '';
            menuEl.style.maxHeight = '';
            menuEl.style.overflowY = '';
        }

        // Close all open dropdown menus and clean up their inline positioning.
        // Single source of truth for "the menus are closed now."
        function closeAllDropdownMenus() {
            document.querySelectorAll('.quality-picker.open').forEach(p => {
                p.classList.remove('open');
                p.classList.remove('flip-up');
                _resetMenuPosition(p.querySelector('.quality-menu'));
            });
            document.querySelectorAll('.extras-menu.open').forEach(m => {
                m.classList.remove('open');
                _resetMenuPosition(m);
            });
            document.querySelectorAll('.extras-wrapper.flip-up').forEach(w => w.classList.remove('flip-up'));
        }

        // Close any open extras/quality menus on outside click
        document.addEventListener('click', (e) => {
            if (!e.target.closest('.extras-wrapper')) {
                document.querySelectorAll('.extras-menu.open').forEach(m => {
                    m.classList.remove('open');
                    _resetMenuPosition(m);
                });
                document.querySelectorAll('.extras-wrapper.flip-up').forEach(w => w.classList.remove('flip-up'));
            }
            if (!e.target.closest('.quality-picker')) {
                document.querySelectorAll('.quality-picker.open').forEach(p => {
                    p.classList.remove('open');
                    p.classList.remove('flip-up');
                    _resetMenuPosition(p.querySelector('.quality-menu'));
                });
            }
        });

        // Fixed-positioned menus don't track the trigger when the page or a
        // scroll container moves, and they'd drift after a window resize. Close
        // them in either case so the user reopens at the correct anchor.
        // EXCEPT: don't close when the user is scrolling INSIDE an open menu
        // (last-resort clipped menus need their scrollbar to actually work).
        window.addEventListener('resize', closeAllDropdownMenus);
        document.addEventListener('scroll', (e) => {
            const t = e.target;
            // scroll target may be Document/HTMLDocument; closest only exists on Element
            if (t && typeof t.closest === 'function') {
                if (t.closest('.quality-menu, .extras-menu')) return;
            }
            closeAllDropdownMenus();
        }, true);  // capture catches all scroll containers

        // ==========================================================================
