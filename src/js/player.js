        // VIDEO PLAYER — in-app HTML5 video with custom controls.
        // The video element gets its src from a localhost HTTP server (set up in
        // logic.py) that streams files with proper Range support for seek.
        // ============================================================
        // Background-transcode coordination. When the backend determines a
        // file isn't directly playable in WebView2, get_video_stream_url
        // returns {preparing: true, job_id} immediately and kicks off ffmpeg
        // in a thread. The thread streams progress + completion through these
        // window-level handlers. Each open() that hits the preparing path
        // installs a job entry here keyed by job_id, and resolves its waiter
        // promise when done/error fires.
        window._prepJobs = window._prepJobs || {};
        window.protubePrepProgress = function(jobId, percent) {
            const job = window._prepJobs[jobId];
            if (job && job.onProgress) job.onProgress(percent);
        };
        window.protubePrepDone = function(jobId, response) {
            const job = window._prepJobs[jobId];
            if (job) {
                delete window._prepJobs[jobId];
                job.resolve(response);
            }
        };
        window.protubePrepError = function(jobId, message) {
            const job = window._prepJobs[jobId];
            if (job) {
                delete window._prepJobs[jobId];
                job.resolve({ error: message });
            }
        };

        // In-app updater events (Mac only — Windows falls back to opening the
        // release URL in the user's browser). Fired by logic.py during
        // start_update_download() and install_staged_update().
        window.protubeUpdateProgress = function(data) {
            try {
                const wrap = document.getElementById('update-modal-progress');
                const bar = document.getElementById('update-modal-progress-bar');
                const pct = document.getElementById('update-modal-progress-pct');
                const label = document.getElementById('update-modal-progress-label');
                if (!wrap || !bar || !pct || !label) return;
                wrap.classList.add('visible');
                wrap.classList.remove('indeterminate');
                const p = Math.max(0, Math.min(100, data.percent || 0));
                bar.style.width = p + '%';
                pct.textContent = p + '%';
                if (data.state === 'extracting') {
                    // Extraction doesn't report fine progress — show a busy bar.
                    wrap.classList.add('indeterminate');
                    pct.textContent = '';
                }
                if (data.msg) label.textContent = data.msg;
            } catch (_) {}
        };

        window.protubeUpdateReady = function(data) {
            try {
                window._stagedUpdateReady = true;
                const wrap = document.getElementById('update-modal-progress');
                const bar = document.getElementById('update-modal-progress-bar');
                const pct = document.getElementById('update-modal-progress-pct');
                const label = document.getElementById('update-modal-progress-label');
                const btn = document.getElementById('update-modal-download');
                if (wrap) {
                    wrap.classList.remove('indeterminate');
                    wrap.classList.add('visible');
                }
                if (bar) bar.style.width = '100%';
                if (pct) pct.textContent = '';
                if (label) label.textContent = 'Ready to install';
                if (btn) {
                    btn.disabled = false;
                    btn.textContent = 'Restart & install';
                    btn.dataset.state = 'ready';
                }
            } catch (_) {}
        };

        window.protubeUpdateError = function(data) {
            try {
                const errEl = document.getElementById('update-modal-error');
                const wrap = document.getElementById('update-modal-progress');
                const btn = document.getElementById('update-modal-download');
                if (errEl) {
                    errEl.textContent = (data && data.msg) || 'Update failed.';
                    errEl.removeAttribute('hidden');
                }
                if (wrap) wrap.classList.remove('indeterminate');
                if (btn) {
                    btn.disabled = false;
                    btn.textContent = 'Open download page';
                    btn.dataset.state = 'fallback';
                }
            } catch (_) {}
        };

        window.player = (function() {
            // Cached element references (looked up once on first open)
            let video, canvas, playBtn, muteBtn, fullscreenBtn, speedBtn, speedMenu, upnextBtn, ccBtn, subtitleEl, aiBtn, summaryPanel, summaryBody, summaryRegen, summaryClose, summaryCopy, summaryScroll, chatHistory, chatForm, chatInput, chatSend;
            let backBtn, externalBtn, back10Btn, fwd10Btn;
            let titleEl, metaEl, timeEl;
            let seekBar, seekTrack, seekProgress, seekBuffered, seekThumb;
            let volSlider, volTrack, volFill;
            let bigPlay, errorOverlay, errorHint, errorAction;
            let controls;
            let spinner, resumeToast, resumeText, resumeRestart;
            let volHud, volHudFill, volHudPercent;
            let volHudHideTimer = null;

            // Web Audio boost state. _rawVolume is the logical volume in 0..3 (0%..300%).
            // Values 0..1 use native video.volume; values >1 add a GainNode for amplification.
            // _limiterNode is a DynamicsCompressorNode acting as a soft limiter so high gains
            // don't push peaks past 0 dBFS into harsh clipping at the OS DAC.
            // _audioCtx / _gainNode are initialized lazily on first >100% use.
            const VOLUME_MAX = 3;  // 300% absolute ceiling
            let _rawVolume = 1.0;
            let _audioCtx = null;
            let _gainNode = null;
            let _limiterNode = null;
            let _audioSrc = null;

            // Subtitle state. _subCues is the parsed VTT — array of {start, end, text} sorted
            // by start. _subEnabled is the per-session CC toggle (always starts off per the
            // user's choice — no persistence across video opens). _subActiveCueIdx caches the
            // current cue index so timeupdate doesn't re-scan the whole array every tick.
            let _subCues = [];
            let _subEnabled = false;
            let _subActiveCueIdx = -1;
            let _subAvailable = false;


            let currentItem = null;
            let hideTimer = null;
            let wasPlayingBeforeSeek = false;
            let bound = false;  // event listeners attached only once

            // Resume position state. Set in open() from backend response, applied on
            // loadedmetadata (when video.duration becomes available). Cleared after one use
            // so seeking back to start manually doesn't auto-resume.
            let pendingResumeSeconds = 0;

            // True for a brief window after a natural end-of-video, freezing the seek
            // bar at 100% to mask Chromium's stray "currentTime=0" timeupdate after
            // ended. Cleared on play, seek, or open() of a new video.
            let _videoEndedPinned = false;

            // Position-save throttle. We save the playback position back to backend every
            // ~5 seconds during playback so resume works even if app crashes / power cuts.
            let lastSavedPosition = 0;
            let positionSaveTimer = null;
            // Volume save throttle — debounce saves while user drags the slider
            let persistVolumeTimer = null;

            // Error state: we only show the error overlay if the video genuinely can't play.
            // A real unplayable file fails FAST — error fires within ~1-2 seconds of loadstart
            // with no progress. A transient hiccup during successful streaming fires random
            // error events that should be ignored. We use a monotonic load-id to track which
            // load attempt each error belongs to, and a timer that only surfaces error if
            // playback hasn't started within a grace window.
            let loadId = 0;
            let errorArmTimer = null;
            let errorArmed = false;  // errors are ignored until armed; armed only if playback fails to start

            function $(id) { return document.getElementById(id); }

            function bindOnce() {
                if (bound) return;
                bound = true;

                video = $('player-video');
                canvas = $('player-canvas');
                playBtn = $('player-play-btn');
                muteBtn = $('player-mute-btn');
                fullscreenBtn = $('player-fullscreen-btn');
                speedBtn = $('player-speed-btn');
                speedMenu = $('player-speed-menu');
                upnextBtn = $('player-upnext-btn');
                ccBtn = $('player-cc-btn');
                subtitleEl = $('player-subtitle');
                aiBtn = $('player-ai-btn');
                summaryPanel = $('player-summary-panel');
                summaryBody = $('player-summary-body');
                summaryRegen = $('player-summary-regen');
                summaryClose = $('player-summary-close');
                summaryCopy = $('player-summary-copy');
                summaryScroll = $('player-summary-scroll');
                chatHistory = $('player-chat-history');
                chatForm = $('player-chat-form');
                chatInput = $('player-chat-input');
                chatSend = $('player-chat-send');
                backBtn = $('player-back');
                externalBtn = $('player-external');
                back10Btn = $('player-back10-btn');
                fwd10Btn = $('player-fwd10-btn');
                titleEl = $('player-title');
                metaEl = $('player-meta');
                timeEl = $('player-time');
                seekBar = $('player-seek');
                seekProgress = $('player-seek-progress');
                seekBuffered = $('player-seek-buffered');
                seekThumb = $('player-seek-thumb');
                volSlider = $('player-volume-slider');
                volTrack = volSlider?.querySelector('.player-volume-track');
                volFill = $('player-volume-fill');
                bigPlay = $('player-big-play');
                errorOverlay = $('player-error');
                errorHint = $('player-error-hint');
                errorAction = $('player-error-action');
                controls = $('player-controls');
                spinner = $('player-spinner');
                resumeToast = $('player-resume-toast');
                resumeText = $('player-resume-text');
                resumeRestart = $('player-resume-restart');
                volHud = $('player-vol-hud');
                volHudFill = $('player-vol-hud-fill');
                volHudPercent = $('player-vol-hud-percent');

                // Seed _rawVolume from the native video element's current volume so
                // the fill bar shows the right position before the user touches it.
                _rawVolume = video.muted ? 0 : (video.volume || 1);

                // Scroll wheel on the canvas adjusts volume — phone-pattern. Right half
                // of canvas only (left half could be reserved for brightness/seek later).
                // We listen on the canvas itself so we don't hijack page-level scrolling.
                canvas.addEventListener('wheel', (e) => {
                    const t = e.target;
                    // Skip if scroll happened over controls, speed menu, HUD, resume toast,
                    // top overlay (back/title/VLC chips), or the side panel — those have
                    // their own scroll behaviors and we shouldn't hijack them.
                    if (t.closest('.player-controls') || t.closest('.player-speed-menu')
                            || t.closest('.player-vol-hud') || t.closest('.player-resume-toast')
                            || t.closest('.player-top-overlay')
                            || t.closest('.player-side-panel')
                            || t.closest('.player-summary-panel')) {
                        return;
                    }
                    // Right half of the canvas only — the gesture is "scroll on the right
                    // edge for volume". Check the click position against the canvas bounds.
                    const rect = canvas.getBoundingClientRect();
                    const xRel = (e.clientX - rect.left) / rect.width;
                    if (xRel < 0.5) return;  // left half is currently free / reserved

                    e.preventDefault();
                    // deltaY > 0 = scroll down = volume DOWN (intuitive direction).
                    // Step size 10% per "click" of the wheel — covers 0..300% in 30 ticks.
                    const step = e.deltaY > 0 ? -0.1 : 0.1;
                    const next = getEffectiveVolume() + step;
                    setEffectiveVolume(next);
                    showVolumeHud(getEffectiveVolume());
                }, { passive: false });

                // --- Video element events ---
                video.addEventListener('loadstart', () => {
                    // New video starting to load — show spinner until 'playing' fires.
                    showSpinner();
                });
                video.addEventListener('waiting', () => {
                    // Buffering during playback — re-show spinner
                    showSpinner();
                });
                video.addEventListener('canplay', () => {
                    hideSpinner();
                });
                video.addEventListener('play', () => {
                    setPlayIcon(true);
                    bigPlay.classList.remove('visible');
                    // User pressed play — release the end-of-video bar pin so
                    // updateProgress can drive the seek bar normally again.
                    _videoEndedPinned = false;
                    // Audio focus: video starts → pause the music player so
                    // both don't blast through the speakers at once. Mirror
                    // listener on _musicPlayer.audio pauses the video on
                    // music play. Standard "audio focus" desktop behavior.
                    try {
                        if (_musicPlayer && _musicPlayer.audio && !_musicPlayer.audio.paused) {
                            _musicPlayer.audio.pause();
                        }
                    } catch (_) {}
                });
                video.addEventListener('seeking', () => {
                    // Any user-initiated seek also releases the pin.
                    _videoEndedPinned = false;
                });
                video.addEventListener('playing', () => {
                    // Playback is actually flowing. Disarm error detection for this load and
                    // clear any stale overlay. Once disarmed, only a fresh load can re-arm.
                    errorArmed = false;
                    if (errorArmTimer) {
                        clearTimeout(errorArmTimer);
                        errorArmTimer = null;
                    }
                    clearError();
                    hideSpinner();
                    bigPlay.classList.remove('visible');
                });
                video.addEventListener('pause', () => {
                    setPlayIcon(false);
                    bigPlay.classList.add('visible');
                });
                video.addEventListener('ended', () => {
                    setPlayIcon(false);
                    bigPlay.classList.add('visible');
                    showControls();
                    // Video finished — explicitly mark as watched-to-end so the NEW
                    // badge stays hidden, AND clear stored position so next open
                    // starts fresh. We pass the duration so the backend can run its
                    // 95%-mark logic too (defense-in-depth).
                    if (currentItem) {
                        try {
                            const dur = isFinite(video.duration) ? video.duration : null;
                            // Save at 99% of duration to trip the 95% completion path,
                            // which marks watched_to_end AND clears position.
                            if (dur && dur > 0) {
                                pywebview.api.save_playback_position(currentItem.id, dur * 0.99, dur);
                            } else {
                                // Duration unknown — fall back to direct mark
                                pywebview.api.mark_watched_to_end(currentItem.id);
                                pywebview.api.save_playback_position(currentItem.id, 0);
                            }
                        } catch (_) {}
                    }
                    // Pin the seek bar at 100% and freeze updateProgress until the
                    // user does something. Chromium/WebView2 can fire a stray
                    // timeupdate with currentTime reset to 0 after a natural end,
                    // which would otherwise make the bar visually snap to the start
                    // even though playback has clearly finished. The flag clears
                    // on play, seek, or new video load (handlers below).
                    _videoEndedPinned = true;
                    if (isFinite(video.duration) && video.duration > 0) {
                        seekProgress.style.width = '100%';
                        seekThumb.style.left = '100%';
                        timeEl.textContent = `${formatTime(video.duration)} / ${formatTime(video.duration)}`;
                    }
                    // Auto-open the side panel so user can pick what to watch next.
                    // No surprise auto-advance; user keeps full agency.
                    showSidePanel();
                });
                video.addEventListener('timeupdate', () => {
                    updateProgress();
                    // Active playback demonstrably invalidates any error overlay.
                    // If currentTime is advancing, the video works — kill any stale error.
                    if (video.currentTime > 0.1 && errorOverlay && !errorOverlay.hasAttribute('hidden')) {
                        clearError();
                    }
                });
                video.addEventListener('progress', updateBuffered);
                video.addEventListener('loadedmetadata', () => {
                    updateProgress();
                    updateBuffered();
                    // Apply pending resume position now that duration is known.
                    // Only resume if it's substantial (>5s) and not too close to the end.
                    if (pendingResumeSeconds > 5 && isFinite(video.duration)
                            && pendingResumeSeconds < video.duration * 0.95) {
                        try {
                            video.currentTime = pendingResumeSeconds;
                            showResumeToast(pendingResumeSeconds);
                        } catch (e) {
                            console.warn('[player] resume seek failed', e);
                        }
                    }
                    pendingResumeSeconds = 0;  // one-shot — manual seeks shouldn't re-trigger
                });
                video.addEventListener('volumechange', () => {
                    setMuteIcon(video.muted || (_rawVolume === 0));
                    updateVolumeFill();
                    // Persist _rawVolume (0..2) so the boost survives app restarts.
                    clearTimeout(persistVolumeTimer);
                    persistVolumeTimer = setTimeout(() => {
                        try {
                            pywebview.api.set_setting('player_volume', _rawVolume);
                            pywebview.api.set_setting('player_muted', !!video.muted);
                        } catch (_) {}
                    }, 400);
                });
                video.addEventListener('error', onVideoError);

                // Wire Windows SMTC / MediaSession action handlers once. The
                // metadata (title, channel, artwork) gets refreshed in open() per
                // video; these handlers route the system play/pause/seek buttons
                // (taskbar tray, keyboard media keys, headset buttons) back to
                // the same code paths the in-app controls use, so the tray
                // controls stay in sync with the player.
                if ('mediaSession' in navigator) {
                    try {
                        navigator.mediaSession.setActionHandler('play', () => {
                            video.play().catch(() => {});
                        });
                        navigator.mediaSession.setActionHandler('pause', () => video.pause());
                        navigator.mediaSession.setActionHandler('seekbackward', (d) => seek(-(d.seekOffset || 10)));
                        navigator.mediaSession.setActionHandler('seekforward',  (d) => seek( (d.seekOffset || 10)));
                        navigator.mediaSession.setActionHandler('seekto', (d) => {
                            if (d.fastSeek && 'fastSeek' in video) { video.fastSeek(d.seekTime); return; }
                            video.currentTime = d.seekTime;
                        });
                        // previoustrack/nexttrack route to side-panel adjacent items if any.
                        navigator.mediaSession.setActionHandler('previoustrack', () => {
                            try { if (typeof playPrevInPanel === 'function') playPrevInPanel(); } catch(_) {}
                        });
                        navigator.mediaSession.setActionHandler('nexttrack', () => {
                            try { if (typeof playNextInPanel === 'function') playNextInPanel(); } catch(_) {}
                        });
                    } catch (_) { /* older WebView2 builds may not support all actions */ }
                    // Mirror playback state so the tray's play/pause icon flips correctly.
                    video.addEventListener('play',  () => { try { navigator.mediaSession.playbackState = 'playing'; } catch(_) {} });
                    video.addEventListener('pause', () => { try { navigator.mediaSession.playbackState = 'paused'; } catch(_) {} });
                    video.addEventListener('ended', () => { try { navigator.mediaSession.playbackState = 'none'; } catch(_) {} });
                    // Position state lets the tray's scrubber bar render and seek.
                    const _pushPositionState = () => {
                        if (!('setPositionState' in navigator.mediaSession)) return;
                        if (!isFinite(video.duration) || video.duration <= 0) return;
                        try {
                            navigator.mediaSession.setPositionState({
                                duration: video.duration,
                                playbackRate: video.playbackRate || 1,
                                position: Math.min(video.currentTime, video.duration),
                            });
                        } catch(_) {}
                    };
                    video.addEventListener('loadedmetadata', _pushPositionState);
                    video.addEventListener('ratechange', _pushPositionState);
                    video.addEventListener('seeked', _pushPositionState);
                    // Throttle: timeupdate fires every ~250ms, but the tray only
                    // needs a refresh ~once a second to keep the scrubber accurate.
                    let _lastPosPush = 0;
                    video.addEventListener('timeupdate', () => {
                        const now = Date.now();
                        if (now - _lastPosPush < 1000) return;
                        _lastPosPush = now;
                        _pushPositionState();
                    });
                }

                // Position-save during playback. Throttled to ~5s so we don't spam backend.
                video.addEventListener('timeupdate', () => {
                    if (!currentItem || video.paused) return;
                    const cur = video.currentTime;
                    if (Math.abs(cur - lastSavedPosition) >= 5) {
                        lastSavedPosition = cur;
                        try {
                            pywebview.api.save_playback_position(currentItem.id, cur, video.duration || 0);
                        } catch (_) {}
                    }
                });

                // Subtitle cue updates — fire on every timeupdate so the active cue is in
                // sync (~250ms granularity, fine for reading speed). Cheap because we
                // cache the active index and only re-scan when the time leaves the window.
                video.addEventListener('timeupdate', () => {
                    if (!_subEnabled || !_subCues.length) return;
                    _renderSubtitleAtTime(video.currentTime);
                });
                // Also clear the overlay on seek-back so we don't show a stale cue while
                // the new position's cue is still being computed.
                video.addEventListener('seeking', () => {
                    if (subtitleEl) {
                        subtitleEl.setAttribute('hidden', '');
                        subtitleEl.textContent = '';
                    }
                    _subActiveCueIdx = -1;
                });

                // Restart-from-beginning button on resume toast
                if (resumeRestart) {
                    resumeRestart.addEventListener('click', () => {
                        video.currentTime = 0;
                        hideResumeToast();
                    });
                }

                // --- Canvas click → toggle play/pause; double-click → fullscreen ---
                let _canvasClickTimer = null;
                canvas.addEventListener('click', (e) => {
                    // Ignore clicks on controls, overlays — those have their own handlers.
                    if (e.target.closest('.player-controls') ||
                        e.target.closest('.player-top-overlay') ||
                        e.target.closest('.player-error') ||
                        e.target.closest('.player-speed-menu') ||
                        e.target.closest('.player-resume-toast')) return;

                    // If the click was inside any side panel itself, ignore (the panel's
                    // own row handlers deal with it; we don't want to toggle play either).
                    if (e.target.closest('.player-side-panel') || e.target.closest('.player-summary-panel')) return;

                    // If the Up Next side panel is currently open and the user clicked OUTSIDE
                    // of it (i.e. on the video area), close the panel and bail. This is
                    // the "click outside to dismiss" UX. Without this short-circuit,
                    // we'd fall through to togglePlay which would pause the video too.
                    const panel = document.getElementById('player-side-panel');
                    if (panel && panel.classList.contains('visible')) {
                        hideSidePanel();
                        if (_canvasClickTimer) {
                            clearTimeout(_canvasClickTimer);
                            _canvasClickTimer = null;
                        }
                        return;
                    }
                    // Same dismiss-on-outside-click for the AI summary panel — keep behavior
                    // parallel to Up Next so users learn one mental model for floating panels.
                    const sumPanel = document.getElementById('player-summary-panel');
                    if (sumPanel && sumPanel.classList.contains('visible')) {
                        hideSummaryPanel();
                        if (_canvasClickTimer) {
                            clearTimeout(_canvasClickTimer);
                            _canvasClickTimer = null;
                        }
                        return;
                    }

                    if (_canvasClickTimer) {
                        clearTimeout(_canvasClickTimer);
                        _canvasClickTimer = null;
                        return;
                    }
                    _canvasClickTimer = setTimeout(() => {
                        _canvasClickTimer = null;
                        togglePlay();
                    }, 220);
                });
                canvas.addEventListener('dblclick', (e) => {
                    if (e.target.closest('.player-controls') ||
                        e.target.closest('.player-top-overlay') ||
                        e.target.closest('.player-error') ||
                        e.target.closest('.player-speed-menu') ||
                        e.target.closest('.player-side-panel') ||
                        e.target.closest('.player-summary-panel') ||
                        e.target.closest('.player-resume-toast')) return;
                    if (_canvasClickTimer) {
                        clearTimeout(_canvasClickTimer);
                        _canvasClickTimer = null;
                    }
                    toggleFullscreen();
                });

                // --- Mouse movement → show controls ---
                canvas.addEventListener('mousemove', () => showControls());
                canvas.addEventListener('mouseleave', () => {
                    // When the mouse leaves the canvas while video is playing, hide quickly
                    if (!video.paused) hideControlsSoon(800);
                });

                // --- Buttons ---
                playBtn.addEventListener('click', togglePlay);
                back10Btn.addEventListener('click', () => seek(-10));
                fwd10Btn.addEventListener('click', () => seek(10));
                muteBtn.addEventListener('click', () => { video.muted = !video.muted; });
                fullscreenBtn.addEventListener('click', toggleFullscreen);

                backBtn.addEventListener('click', back);
                externalBtn.addEventListener('click', () => {
                    if (currentItem?.filepath) pywebview.api.open_file(currentItem.filepath);
                });

                // --- Speed menu ---
                speedBtn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    speedMenu.toggleAttribute('hidden');
                });

                // --- Subtitle (CC) toggle ---
                if (ccBtn) {
                    ccBtn.addEventListener('click', (e) => {
                        e.stopPropagation();
                        if (!_subAvailable) {
                            // Button is always visible now — explain why nothing happens
                            // when subs aren't available (most common cause: the video
                            // was downloaded before the subtitle feature shipped, so no
                            // .vtt sidecar exists. Re-downloading fetches subs).
                            showToast('No subtitles for this video. Re-download to fetch them.', null, null);
                            return;
                        }
                        _subEnabled = !_subEnabled;
                        ccBtn.classList.toggle('active', _subEnabled);
                        ccBtn.setAttribute('data-tip', _subEnabled ? 'Subtitles (on)' : 'Subtitles (off)');
                        if (!_subEnabled) {
                            subtitleEl.setAttribute('hidden', '');
                            subtitleEl.textContent = '';
                            _subActiveCueIdx = -1;
                        } else {
                            // Force a re-eval immediately so the cue at current time appears.
                            _renderSubtitleAtTime(video.currentTime);
                        }
                    });
                }
                if (upnextBtn) {
                    upnextBtn.addEventListener('click', (e) => {
                        e.stopPropagation();
                        toggleSidePanel();
                    });
                }
                const sidePanelClose = document.getElementById('player-side-panel-close');
                if (sidePanelClose) {
                    sidePanelClose.addEventListener('click', () => hideSidePanel());
                }

                // --- AI summary side panel ---
                if (aiBtn) {
                    aiBtn.addEventListener('click', (e) => {
                        e.stopPropagation();
                        toggleSummaryPanel();
                    });
                }
                if (summaryClose) {
                    summaryClose.addEventListener('click', () => hideSummaryPanel());
                }
                if (summaryRegen) {
                    summaryRegen.addEventListener('click', async () => {
                        if (!currentItem || !currentItem.id) return;
                        try { await pywebview.api.clear_video_ai_summary(currentItem.id); } catch (_) {}
                        _renderSummaryLoading();
                        _loadSummaryForCurrent(/*forceFetch*/ true);
                    });
                }
                if (summaryCopy) {
                    summaryCopy.addEventListener('click', async () => {
                        // Find current item's summary from local cache (avoids backend roundtrip)
                        const findEntry = () => {
                            for (const v of (app.videosInLibrary || [])) {
                                if (v.id === currentItem.id) return v;
                                if (v.type === 'playlist') {
                                    const c = (v.videos || []).find(x => x.id === currentItem.id);
                                    if (c) return c;
                                }
                            }
                            return null;
                        };
                        const entry = findEntry();
                        if (!entry || !entry.ai_summary) return;
                        try {
                            await navigator.clipboard.writeText(entry.ai_summary);
                            summaryCopy.classList.add('flash-ok');
                            setTimeout(() => summaryCopy.classList.remove('flash-ok'), 900);
                        } catch (_) {
                            showToast('Copy failed', null, null);
                        }
                    });
                }
                if (chatForm) {
                    chatForm.addEventListener('submit', (e) => {
                        e.preventDefault();
                        _sendChatMessage();
                    });
                }
                // Stop ALL keyboard events at the chat input from bubbling up to the player's
                // global key handler (otherwise typing 'f' would toggle fullscreen, 'p' PiP,
                // arrow keys would scrub, etc.). Spacebar is the worst offender — it'd pause
                // the video while the user is mid-sentence.
                if (chatInput) {
                    chatInput.addEventListener('keydown', (e) => e.stopPropagation());
                    chatInput.addEventListener('keypress', (e) => e.stopPropagation());
                    chatInput.addEventListener('keyup', (e) => e.stopPropagation());
                }
                speedMenu.addEventListener('click', (e) => {
                    const opt = e.target.closest('.player-speed-option');
                    if (!opt) return;
                    const speed = parseFloat(opt.getAttribute('data-speed'));
                    setSpeed(speed);
                    speedMenu.setAttribute('hidden', '');
                });
                document.addEventListener('click', (e) => {
                    // Click outside menu closes it
                    if (!speedMenu.hasAttribute('hidden')
                        && !e.target.closest('.player-speed-menu')
                        && !e.target.closest('.player-speed-btn')) {
                        speedMenu.setAttribute('hidden', '');
                    }
                });

                // --- Seek bar ---
                let seeking = false;
                const seekFromEvent = (e) => {
                    const rect = seekBar.getBoundingClientRect();
                    const x = Math.max(0, Math.min(e.clientX - rect.left, rect.width));
                    const ratio = rect.width > 0 ? x / rect.width : 0;
                    if (video.duration && isFinite(video.duration)) {
                        video.currentTime = ratio * video.duration;
                    }
                };
                seekBar.addEventListener('mousedown', (e) => {
                    seeking = true;
                    wasPlayingBeforeSeek = !video.paused;
                    if (wasPlayingBeforeSeek) video.pause();
                    seekFromEvent(e);
                });
                document.addEventListener('mousemove', (e) => {
                    if (seeking) seekFromEvent(e);
                });
                document.addEventListener('mouseup', () => {
                    if (seeking) {
                        seeking = false;
                        if (wasPlayingBeforeSeek) video.play().catch(() => {});
                    }
                });

                // --- Volume slider ---
                // Dragging the full track = 0..VOLUME_MAX (currently 300%). 1/3 of the
                // way = 100% (normal); 2/3 = 200%; full = 300%.
                let dragVol = false;
                const volFromEvent = (e) => {
                    const rect = volTrack.getBoundingClientRect();
                    const x = Math.max(0, Math.min(e.clientX - rect.left, rect.width));
                    const ratio = rect.width > 0 ? x / rect.width : 0;
                    setEffectiveVolume(ratio * VOLUME_MAX);
                };
                volTrack.addEventListener('mousedown', (e) => {
                    dragVol = true;
                    volFromEvent(e);
                });
                document.addEventListener('mousemove', (e) => {
                    if (dragVol) volFromEvent(e);
                });
                document.addEventListener('mouseup', () => { dragVol = false; });

                // --- Error overlay action ---
                errorAction.addEventListener('click', () => {
                    if (currentItem?.filepath) pywebview.api.open_file(currentItem.filepath);
                });

                // --- Keyboard shortcuts ---
                document.addEventListener('keydown', (e) => {
                    if (app.currentView !== 'player') return;
                    // Don't hijack typing in inputs (shouldn't happen in player, but defensive)
                    const tag = (e.target?.tagName || '').toLowerCase();
                    if (tag === 'input' || tag === 'textarea') return;

                    if (e.key === ' ' || e.code === 'Space') {
                        e.preventDefault();
                        togglePlay();
                    } else if (e.key === 'ArrowLeft') {
                        e.preventDefault();
                        seek(-10);
                    } else if (e.key === 'ArrowRight') {
                        e.preventDefault();
                        seek(10);
                    } else if (e.key === 'ArrowUp') {
                        // Volume up — 10% per press, can go up to 300% via Web Audio gain
                        e.preventDefault();
                        const next = getEffectiveVolume() + 0.1;
                        setEffectiveVolume(next);
                        showVolumeHud(getEffectiveVolume());
                    } else if (e.key === 'ArrowDown') {
                        e.preventDefault();
                        const next = getEffectiveVolume() - 0.1;
                        setEffectiveVolume(next);
                        showVolumeHud(getEffectiveVolume());
                    } else if (e.key === 'f' || e.key === 'F') {
                        e.preventDefault();
                        toggleFullscreen();
                    } else if (e.key === 'm' || e.key === 'M') {
                        e.preventDefault();
                        video.muted = !video.muted;
                    } else if (e.key === 'Escape') {
                        // The global Esc handler above exits player fullscreen first
                        // when active. If we're NOT in fullscreen, treat Esc as "back
                        // to library". Branch on isPlayerFullscreen so the two handlers
                        // don't both run on the same Esc press.
                        if (!isPlayerFullscreen) back();
                    } else if (e.key >= '0' && e.key <= '9') {
                        e.preventDefault();
                        if (video.duration && isFinite(video.duration)) {
                            const ratio = parseInt(e.key, 10) / 10;
                            video.currentTime = ratio * video.duration;
                        }
                    }
                });
            }

            function showSpinner() {
                if (spinner) spinner.removeAttribute('hidden');
            }

            // Walk the library to find the item that comes after `current`. Handles
            // both playlist children (walks within playlist; falls through to next
            // library entry after last child) and top-level items. Returns null if
            // current is the very last item or current isn't found at all.
            function findNextLibraryItem(current) {
                if (!current || !app.videosInLibrary) return null;
                const lib = app.videosInLibrary;

                // First, try to find current in a playlist
                for (let i = 0; i < lib.length; i++) {
                    const entry = lib[i];
                    if (entry.type === 'playlist' && Array.isArray(entry.videos)) {
                        const idx = entry.videos.findIndex(c => c.id === current.id);
                        if (idx >= 0) {
                            // Found in playlist. Next sibling within playlist?
                            if (idx + 1 < entry.videos.length) {
                                const candidate = entry.videos[idx + 1];
                                if (!candidate.missing && candidate.filepath && !candidate.hidden) return candidate;
                            }
                            // No more in playlist — fall through to next top-level entry
                            return findNextTopLevel(lib, i);
                        }
                    }
                }

                // Current is a top-level item — find it and grab the next
                const idx = lib.findIndex(v => v.id === current.id);
                if (idx >= 0) {
                    return findNextTopLevel(lib, idx);
                }
                return null;
            }

            // Helper: find the next top-level VIDEO (skipping playlists since we don't
            // want to suddenly jump into a playlist's first child without the user asking).
            // Returns the first standalone non-missing video after `startIdx`, or null.
            function findNextTopLevel(lib, startIdx) {
                for (let i = startIdx + 1; i < lib.length; i++) {
                    const entry = lib[i];
                    if (entry.type === 'playlist') continue;
                    if (entry.missing) continue;
                    if (!entry.filepath) continue;
                    if (entry.hidden) continue;
                    return entry;
                }
                return null;
            }

            function hideSpinner() {
                if (spinner) spinner.setAttribute('hidden', '');
            }

            function showResumeToast(positionSec) {
                if (!resumeToast || !resumeText) return;
                resumeText.textContent = `Resumed from ${formatTime(positionSec)}`;
                resumeToast.removeAttribute('hidden');
                // Force reflow so the transition runs
                void resumeToast.offsetHeight;
                resumeToast.classList.add('visible');
                // Auto-hide after 4 seconds
                clearTimeout(resumeToast._hideTimer);
                resumeToast._hideTimer = setTimeout(hideResumeToast, 4000);
            }

            function hideResumeToast() {
                if (!resumeToast) return;
                resumeToast.classList.remove('visible');
                clearTimeout(resumeToast._hideTimer);
                setTimeout(() => {
                    if (resumeToast && !resumeToast.classList.contains('visible')) {
                        resumeToast.setAttribute('hidden', '');
                    }
                }, 250);
            }

            // Volume HUD — vertical pill that flashes briefly on volume change.
            // Triggered by Up/Down arrows AND by scroll wheel on the right half of the canvas.
            function showVolumeHud(volume) {
                if (!volHud || !volHudFill || !volHudPercent) return;
                const v = Math.max(0, Math.min(VOLUME_MAX, volume));
                const pct = Math.round(v * 100);  // 0..(VOLUME_MAX*100)
                volHudFill.style.height = Math.min(100, (v / VOLUME_MAX) * 100) + '%';
                volHudPercent.textContent = pct + '%';
                volHud.removeAttribute('hidden');
                // Force reflow so the transition plays from invisible
                void volHud.offsetHeight;
                volHud.classList.add('visible');
                clearTimeout(volHudHideTimer);
                volHudHideTimer = setTimeout(() => {
                    volHud.classList.remove('visible');
                    setTimeout(() => {
                        if (volHud && !volHud.classList.contains('visible')) {
                            volHud.setAttribute('hidden', '');
                        }
                    }, 200);
                }, 900);
            }

            // Effective volume getter — returns 0 to 2.0 (0%..200%).
            function getEffectiveVolume() {
                if (video.muted) return 0;
                return _rawVolume;
            }

            // Lazily wire the Web Audio gain graph for >100% volume boost.
            // Chain: source → gain → limiter → destination.
            // CRITICAL ORDER: resume() MUST be awaited before createMediaElementSource().
            // Calling createMediaElementSource while the context is suspended immediately
            // reroutes audio through the dead context — you get silence, not a brief hiccup.
            // The limiter prevents harsh clipping when gain pushes peaks above 0 dBFS.
            async function _ensureAudioGraph() {
                if (_gainNode) {
                    // Already connected — just keep context alive.
                    if (_audioCtx && _audioCtx.state === 'suspended') {
                        try { await _audioCtx.resume(); } catch (_) {}
                    }
                    return true;
                }
                try {
                    _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
                    // Bring context to 'running' BEFORE connecting the element.
                    if (_audioCtx.state !== 'running') {
                        await _audioCtx.resume();
                    }
                    if (_audioCtx.state !== 'running') {
                        _audioCtx.close().catch(() => {});
                        _audioCtx = null;
                        return false;
                    }
                    _gainNode = _audioCtx.createGain();
                    _gainNode.gain.value = _rawVolume;
                    // Soft limiter (DynamicsCompressorNode tuned as a brick-wall limiter):
                    // threshold 0 dB, hard knee, 20:1 ratio, fast attack — only acts when peaks
                    // exceed 0 dBFS so it's transparent at gain=1 but tames distortion at 3x.
                    _limiterNode = _audioCtx.createDynamicsCompressor();
                    _limiterNode.threshold.value = 0;
                    _limiterNode.knee.value = 0;
                    _limiterNode.ratio.value = 20;
                    _limiterNode.attack.value = 0.001;
                    _limiterNode.release.value = 0.05;
                    // Connect AFTER context is running so audio reroutes into an active pipeline.
                    _audioSrc = _audioCtx.createMediaElementSource(video);
                    _audioSrc.connect(_gainNode);
                    _gainNode.connect(_limiterNode);
                    _limiterNode.connect(_audioCtx.destination);
                    video.volume = 1;  // gain node now controls amplitude
                    _audioCtx.addEventListener('statechange', () => {
                        if (_audioCtx && _audioCtx.state === 'suspended') {
                            _audioCtx.resume().catch(() => {});
                        }
                    });
                    return true;
                } catch (e) {
                    console.warn('[ProTube] Web Audio boost failed:', e);
                    _audioCtx = null; _gainNode = null; _limiterNode = null; _audioSrc = null;
                    return false;
                }
            }

            // Apply _rawVolume to the video/gain chain (async — fire and forget from callers).
            async function _applyVolume() {
                if (_rawVolume === 0) { video.muted = true; return; }
                video.muted = false;
                if (_rawVolume > 1 || _gainNode) {
                    const ok = await _ensureAudioGraph();
                    if (ok) {
                        _gainNode.gain.value = _rawVolume;
                        video.volume = 1;
                        return;
                    }
                    // Web Audio failed — fall through and cap at 100%.
                }
                if (_gainNode) {
                    // GainNode is connected but just reported ok=false (unexpected) — still use it.
                    _gainNode.gain.value = Math.min(1, _rawVolume);
                } else {
                    video.volume = Math.min(1, _rawVolume);
                }
            }

            // Set volume in the 0..VOLUME_MAX (currently 0%..300%). Above 1.0 uses Web Audio GainNode.
            function setEffectiveVolume(target) {
                _rawVolume = Math.max(0, Math.min(VOLUME_MAX, target));
                _applyVolume();  // async fire-and-forget; UI updates synchronously below
                updateVolumeFill();
            }

            // Up Next side panel — slide-in panel on right edge of player. Auto-opens
            // when video ends; toggleable from the controls bar at any time. Renders
            // a list of remaining library videos with their thumbnails + metadata.
            // The CURRENT video is shown at the top with a "NOW" badge for context.

            function renderSidePanelList() {
                const list = document.getElementById('player-side-panel-list');
                const countEl = document.getElementById('player-side-panel-count');
                if (!list) return;

                // Build the playable list. Order: current video first (with NOW badge),
                // then everything that comes after it via findNextLibraryItem chain,
                // then everything before it (so we have a full circular list).
                const allPlayable = collectPlayableLibraryItems();
                if (countEl) {
                    countEl.textContent = `${allPlayable.length} in library`;
                }

                if (allPlayable.length === 0) {
                    list.innerHTML = '<div class="player-side-panel-empty">No other videos in library yet.</div>';
                    return;
                }

                // Reorder so currentItem is first, followed by what comes after it,
                // then wrap back to the items before currentItem.
                let ordered = allPlayable;
                if (currentItem) {
                    const idx = allPlayable.findIndex(v => v.id === currentItem.id);
                    if (idx > 0) {
                        ordered = [...allPlayable.slice(idx), ...allPlayable.slice(0, idx)];
                    }
                }

                const cache = (app && app._thumbCache) || {};
                const escapeHtml = (s) => (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');

                list.innerHTML = ordered.map(item => {
                    const isCurrent = currentItem && item.id === currentItem.id;
                    const title = escapeHtml(item.title || 'Untitled');
                    const uploader = escapeHtml(item.uploader || '');
                    const duration = escapeHtml(item.duration_string || '');

                    // Resolve thumbnail
                    let thumbAttr = '';
                    if (item.thumbnail) {
                        if (item.thumbnail.startsWith('pt:thumb:')) {
                            const cached = cache[item.thumbnail];
                            if (cached) thumbAttr = `src="${cached}"`;
                            else thumbAttr = `data-thumb-marker="${item.thumbnail}"`;
                        } else {
                            thumbAttr = `src="${escapeHtml(item.thumbnail)}"`;
                        }
                    }

                    // Watch progress on thumbnails
                    let progressBar = '';
                    if (!isCurrent && item.last_position_seconds && item.last_position_seconds > 0) {
                        let durSec = item.last_duration_seconds || 0;
                        if (!durSec && item.duration_string) {
                            const parts = item.duration_string.split(':').map(p => parseInt(p, 10));
                            if (parts.length === 3) durSec = parts[0]*3600 + parts[1]*60 + parts[2];
                            else if (parts.length === 2) durSec = parts[0]*60 + parts[1];
                        }
                        if (durSec > 0) {
                            const pct = Math.min(100, (item.last_position_seconds / durSec) * 100);
                            progressBar = `<div class="player-side-row-progress" style="width: ${pct}%"></div>`;
                        }
                    }

                    return `
                        <button class="player-side-row ${isCurrent ? 'is-current' : ''}" data-video-id="${item.id}">
                            <div class="player-side-row-thumb">
                                ${thumbAttr ? `<img ${thumbAttr} alt="">` : ''}
                                ${isCurrent ? '<div class="player-side-row-now">Now</div>' : ''}
                                ${duration ? `<div class="player-side-row-duration">${duration}</div>` : ''}
                                ${progressBar}
                            </div>
                            <div class="player-side-row-body">
                                <div class="player-side-row-title">${title}</div>
                                <div class="player-side-row-meta">${uploader}</div>
                            </div>
                        </button>
                    `;
                }).join('');

                // Wire up click-to-play on each row (not the current one). Panel hides
                // immediately on click — that's the user's signal to focus on the video.
                // We MUST stopPropagation because the panel lives inside the canvas; without
                // this, the canvas's click handler fires on bubble-up and its 220ms-deferred
                // togglePlay() would pause the freshly-started video.
                list.querySelectorAll('.player-side-row').forEach(row => {
                    row.addEventListener('click', (e) => {
                        e.stopPropagation();
                        const id = row.dataset.videoId;
                        if (currentItem && id === currentItem.id) return;
                        const item = ordered.find(v => v.id === id);
                        if (!item) return;
                        hideSidePanel();
                        window.player.open(item);
                    });
                });

                // Resolve any lazy thumbnails in the panel (markers without cached data URLs)
                if (app._resolvePendingThumbnails) {
                    app._resolvePendingThumbnails();
                }
            }

            // Flatten library to all videos. We include items even without a filepath
            // string set in the entry — they're in the library, so the user expects to
            // see them in the up-next panel. The player itself will surface an error
            // if a particular file genuinely can't play. Only items explicitly flagged
            // as missing (file gone from disk) are excluded.
            function collectPlayableLibraryItems() {
                const out = [];
                if (!app || !app.videosInLibrary) return out;
                for (const entry of app.videosInLibrary) {
                    if (entry.hidden) continue;
                    if (entry.type === 'playlist') {
                        for (const c of (entry.videos || [])) {
                            if (!c.missing && !c.hidden) out.push(c);
                        }
                    } else {
                        if (!entry.missing) out.push(entry);
                    }
                }
                return out;
            }

            function showSidePanel() {
                const panel = document.getElementById('player-side-panel');
                if (!panel) return;
                renderSidePanelList();
                panel.removeAttribute('hidden');
                // Force reflow so transition plays
                void panel.offsetHeight;
                panel.classList.add('visible');
            }

            function hideSidePanel() {
                const panel = document.getElementById('player-side-panel');
                if (!panel) return;
                panel.classList.remove('visible');
                // After transition, hide so it doesn't accept clicks
                setTimeout(() => {
                    if (panel && !panel.classList.contains('visible')) {
                        panel.setAttribute('hidden', '');
                    }
                }, 280);
            }

            function toggleSidePanel() {
                const panel = document.getElementById('player-side-panel');
                if (!panel) return;
                if (panel.hasAttribute('hidden') || !panel.classList.contains('visible')) {
                    showSidePanel();
                } else {
                    hideSidePanel();
                }
            }

            function setPlayIcon(isPlaying) {
                if (isPlaying) {
                    playBtn.classList.add('is-playing');
                } else {
                    playBtn.classList.remove('is-playing');
                }
            }

            function setMuteIcon(muted) {
                // Driven by data-vol-state: 'high' (default), 'low', or 'muted'
                if (muted || video.volume === 0) {
                    muteBtn.dataset.volState = 'muted';
                } else if (video.volume < 0.5) {
                    muteBtn.dataset.volState = 'low';
                } else {
                    muteBtn.dataset.volState = 'high';
                }
            }

            function updateProgress() {
                // After a natural end, ignore stray timeupdate events that
                // Chromium/WebView2 sometimes emits with currentTime reset to
                // 0 — the bar should stay pinned at 100% until the user starts
                // playing again or seeks somewhere.
                if (_videoEndedPinned) return;
                const dur = video.duration;
                const cur = video.currentTime;
                if (!isFinite(dur) || dur <= 0) {
                    seekProgress.style.width = '0%';
                    seekThumb.style.left = '0%';
                    timeEl.textContent = '0:00 / 0:00';
                    return;
                }
                const pct = (cur / dur) * 100;
                seekProgress.style.width = pct + '%';
                seekThumb.style.left = pct + '%';
                timeEl.textContent = `${formatTime(cur)} / ${formatTime(dur)}`;
            }

            function updateBuffered() {
                const dur = video.duration;
                if (!isFinite(dur) || dur <= 0 || video.buffered.length === 0) {
                    seekBuffered.style.width = '0%';
                    return;
                }
                const end = video.buffered.end(video.buffered.length - 1);
                seekBuffered.style.width = ((end / dur) * 100) + '%';
            }

            function updateVolumeFill() {
                const v = video.muted ? 0 : _rawVolume;
                const pct = (v / VOLUME_MAX) * 100;  // 0..VOLUME_MAX maps to 0..100% track fill
                volFill.style.width = pct + '%';
                const thumb = document.getElementById('player-volume-thumb');
                if (thumb) thumb.style.left = pct + '%';
            }

            function formatTime(s) {
                if (!isFinite(s) || s < 0) return '0:00';
                s = Math.floor(s);
                const h = Math.floor(s / 3600);
                const m = Math.floor((s % 3600) / 60);
                const sec = s % 60;
                if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
                return `${m}:${String(sec).padStart(2, '0')}`;
            }

            // --- Subtitle (VTT) helpers ---
            // Parse a WebVTT string into an array of {start, end, text} cues sorted by start.
            // Tolerant of: WEBVTT header, NOTE blocks, optional cue identifiers before the
            // arrow line, multi-line cue text, and Windows \r\n line endings. Strips inline
            // tags like <c.color>, <i>, <00:00:00.000> timestamp markers since we render plain.
            function parseVTT(vttText) {
                if (!vttText) return [];
                const cues = [];
                const blocks = vttText.replace(/\r\n/g, '\n').split(/\n\n+/);
                const tsRe = /(\d+:)?(\d{1,2}):(\d{2})\.(\d{3})\s*-->\s*(\d+:)?(\d{1,2}):(\d{2})\.(\d{3})/;
                const toSec = (h, m, s, ms) => (parseInt(h || '0', 10) * 3600) + (parseInt(m, 10) * 60) + parseInt(s, 10) + (parseInt(ms, 10) / 1000);
                for (const block of blocks) {
                    if (!block.trim() || /^WEBVTT/.test(block) || /^NOTE\b/.test(block)) continue;
                    const lines = block.split('\n');
                    let arrowLineIdx = -1;
                    for (let i = 0; i < lines.length; i++) {
                        if (tsRe.test(lines[i])) { arrowLineIdx = i; break; }
                    }
                    if (arrowLineIdx === -1) continue;
                    const m = lines[arrowLineIdx].match(tsRe);
                    if (!m) continue;
                    const start = toSec(m[1] && m[1].slice(0, -1), m[2], m[3], m[4]);
                    const end = toSec(m[5] && m[5].slice(0, -1), m[6], m[7], m[8]);
                    let text = lines.slice(arrowLineIdx + 1).join('\n')
                        .replace(/<\d{2}:\d{2}:\d{2}\.\d{3}>/g, '')  // word-level timestamps
                        .replace(/<\/?[^>]+>/g, '');                  // <c.color>, <i>, etc.
                    text = text.replace(/\s*\n\s*/g, ' ').replace(/\s+/g, ' ').trim();
                    // Strip inline non-speech annotations like [Music], [Applause], (laughter).
                    // Auto-captions sprinkle these into otherwise spoken cues. Removing them
                    // mid-cue can leave double spaces, hence the second collapse.
                    text = text.replace(/[\[(](music|applause|laughter|laughs|cheering|cheers|crowd noise|noise|sound effect|sfx|inaudible|crosstalk|silence|background noise)[\])]/gi, '');
                    text = text.replace(/\s+/g, ' ').trim();
                    // Skip cues that are nothing but a bracketed annotation — no speech to show.
                    if (!text || /^\s*[\[(].*[\])]\s*$/.test(text)) continue;
                    // Collapse immediate duplicate words ("the the" → "the").
                    text = text.replace(/\b(\w+)(\s+\1\b)+/gi, '$1');
                    cues.push({ start, end, text });
                }
                cues.sort((a, b) => a.start - b.start);
                // Pass 1 — full-prefix dedup. YouTube auto-captions emit "rolling" cues where
                // each cue's text is the previous cue's text + a few new words. Drop the
                // shorter version when the longer one strictly contains it as a prefix.
                const deduped = [];
                for (const c of cues) {
                    const prev = deduped[deduped.length - 1];
                    if (prev && c.text.startsWith(prev.text + ' ')) {
                        prev.text = c.text;
                        prev.end = c.end;
                    } else if (prev && prev.text === c.text) {
                        prev.end = c.end;
                    } else if (prev && prev.text.startsWith(c.text + ' ')) {
                        continue;
                    } else {
                        deduped.push(c);
                    }
                }
                // Pass 2 — sliding-window boundary trim. Even after pass 1, neighboring cues
                // can have N's suffix == N+1's prefix (YouTube emits overlapping windows so
                // the rendered line "rolls" — the same phrase shows up twice on the boundary).
                // Trim the matching prefix off cue N+1. Require >=4 word match so we don't
                // overzealously trim coincidental short repetitions ("the the", "and and").
                const result = [];
                for (let i = 0; i < deduped.length; i++) {
                    const c = { ...deduped[i] };
                    const prev = result[result.length - 1];
                    if (prev) {
                        const prevWords = prev.text.split(/\s+/);
                        const curWords = c.text.split(/\s+/);
                        const maxN = Math.min(prevWords.length, curWords.length);
                        let overlap = 0;
                        for (let n = maxN; n >= 4; n--) {
                            const a = prevWords.slice(-n).join(' ').toLowerCase().replace(/[^\w\s]/g, '');
                            const b = curWords.slice(0, n).join(' ').toLowerCase().replace(/[^\w\s]/g, '');
                            if (a && a === b) { overlap = n; break; }
                        }
                        if (overlap > 0) {
                            c.text = curWords.slice(overlap).join(' ').trim();
                        }
                    }
                    if (c.text) result.push(c);
                }
                return result;
            }

            // Find the cue active at a given video time and update the overlay.
            // Linear scan starting from cached index — typical timeupdate is forward in
            // small increments so we usually walk 0-1 cues per call.
            function _renderSubtitleAtTime(t) {
                if (!subtitleEl) return;
                if (_subActiveCueIdx >= 0 && _subActiveCueIdx < _subCues.length) {
                    const c = _subCues[_subActiveCueIdx];
                    if (t >= c.start && t < c.end) return;  // still inside the cached cue
                }
                let lo = 0, hi = _subCues.length - 1, found = -1;
                // Binary search for the cue whose [start,end) contains t.
                while (lo <= hi) {
                    const mid = (lo + hi) >> 1;
                    const c = _subCues[mid];
                    if (t < c.start) hi = mid - 1;
                    else if (t >= c.end) lo = mid + 1;
                    else { found = mid; break; }
                }
                _subActiveCueIdx = found;
                if (found === -1) {
                    if (!subtitleEl.hasAttribute('hidden')) {
                        subtitleEl.setAttribute('hidden', '');
                        subtitleEl.textContent = '';
                    }
                } else {
                    subtitleEl.textContent = _subCues[found].text;
                    subtitleEl.removeAttribute('hidden');
                }
            }

            // Reset subtitle state (called every time a new video opens). Always starts OFF
            // per the user's design choice — no persistence across videos. Button stays
            // visible at all times; the .no-subs class dims it until cues load.
            function _resetSubtitles() {
                _subCues = [];
                _subEnabled = false;
                _subActiveCueIdx = -1;
                _subAvailable = false;
                if (subtitleEl) {
                    subtitleEl.setAttribute('hidden', '');
                    subtitleEl.textContent = '';
                }
                if (ccBtn) {
                    ccBtn.classList.remove('active');
                    ccBtn.classList.add('no-subs');
                    ccBtn.setAttribute('data-tip', 'Subtitles (loading…)');
                }
            }

            // Fetch subtitles for the current video from the backend, parse, and unlock the
            // CC button. On failure the button stays visible but dimmed (.no-subs) so the
            // user gets a toast on click instead of silently nothing.
            // If "Auto-polish subtitles" is on AND a Groq API key is configured, kick off
            // an AI cleanup pass after the raw cues load and quietly swap them in when ready.
            async function _loadSubtitlesForCurrent() {
                if (!currentItem || !currentItem.id) return;
                try {
                    const res = await pywebview.api.get_subtitles_for_video(currentItem.id);
                    if (!res || res.error || !res.vtt) {
                        if (ccBtn) ccBtn.setAttribute('data-tip', 'No subtitles for this video');
                        return;
                    }
                    const cues = parseVTT(res.vtt);
                    if (!cues.length) {
                        if (ccBtn) ccBtn.setAttribute('data-tip', 'No subtitles for this video');
                        return;
                    }
                    _subCues = cues;
                    _subAvailable = true;
                    if (ccBtn) {
                        ccBtn.classList.remove('no-subs');
                        ccBtn.setAttribute('data-tip', 'Subtitles (off)');
                    }
                    // F8 auto-polish — opt-in setting, no-op when off or no API key.
                    // Backend caches the cleaned VTT so subsequent opens are instant.
                    if (window._autoPolishSubtitles && window._hasGroqKey) {
                        _polishSubtitlesNow(/*silent*/ true);
                    }
                } catch (_) {
                    if (ccBtn) ccBtn.setAttribute('data-tip', 'No subtitles for this video');
                }
            }

            // Manual or auto-triggered subtitle polish via Groq (F8). On success, replaces
            // the in-memory cue list so the active overlay updates seamlessly.
            async function _polishSubtitlesNow(silent) {
                if (!currentItem || !currentItem.id || !_subAvailable) return;
                const wasEnabled = _subEnabled;
                if (!silent) showToast('Polishing subtitles…', null, null);
                try {
                    const res = await pywebview.api.polish_subtitles_with_ai(currentItem.id);
                    if (!res || res.error) {
                        if (!silent) showToast(res?.error || 'AI polish failed', null, null);
                        return;
                    }
                    const cues = parseVTT(res.vtt);
                    if (!cues.length) return;
                    _subCues = cues;
                    _subActiveCueIdx = -1;
                    if (wasEnabled) _renderSubtitleAtTime(video.currentTime);
                    if (!silent) {
                        showToast(res.cached ? 'Loaded cleaned subtitles' : 'Subtitles cleaned with AI', null, null);
                    }
                } catch (e) {
                    if (!silent) showToast('AI polish failed', null, null);
                }
            }

            // --- AI summary panel (F7) — minimal markdown renderer + state machine ---
            // We render a small markdown subset (## headers, **bold**, *italic*, - bullets,
            // paragraphs). Full markdown libraries are overkill; the LLM emits a constrained
            // format defined by our system prompt. HTML-escape every text node to avoid XSS.
            function _escapeHtml(s) {
                return String(s == null ? '' : s)
                    .replace(/&/g, '&amp;')
                    .replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;')
                    .replace(/"/g, '&quot;')
                    .replace(/'/g, '&#39;');
            }
            function _formatInline(text) {
                // Bold first (greedy guard so ** doesn't eat across the whole text), then italic.
                return text
                    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
                    .replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<em>$2</em>');
            }
            // Line-by-line markdown renderer. The previous block-based version glued
            // header lines together with following content when the LLM didn't insert
            // a blank line between them — so "## Main topics\n- Bullet..." rendered the
            // entire thing inside <h3>. This walker processes each line in its own pass.
            function _renderMarkdown(md) {
                if (!md) return '';
                const lines = md.replace(/\r\n/g, '\n').split('\n');
                const out = [];
                let i = 0;
                const isHeader = s => /^#{1,6}\s+/.test(s);
                const isBullet = s => /^[-*•]\s+/.test(s);
                const isNumbered = s => /^\d+\.\s+/.test(s);
                while (i < lines.length) {
                    const trimmed = lines[i].trim();
                    if (!trimmed) { i++; continue; }
                    // Code fence — collect the body verbatim
                    if (trimmed.startsWith('```')) {
                        i++;
                        const code = [];
                        while (i < lines.length && !lines[i].trim().startsWith('```')) {
                            code.push(lines[i]);
                            i++;
                        }
                        i++;  // skip closing fence
                        if (code.length) out.push(`<pre><code>${_escapeHtml(code.join('\n'))}</code></pre>`);
                        continue;
                    }
                    // ## H2/H3 — render as h3 to match panel typography hierarchy
                    const m23 = trimmed.match(/^#{2,3}\s+(.+)$/);
                    if (m23) {
                        out.push(`<h3>${_formatInline(_escapeHtml(m23[1].trim()))}</h3>`);
                        i++;
                        continue;
                    }
                    // # H1 — render as h2 (we don't use h1 inside the panel)
                    const m1 = trimmed.match(/^#\s+(.+)$/);
                    if (m1) {
                        out.push(`<h2>${_formatInline(_escapeHtml(m1[1].trim()))}</h2>`);
                        i++;
                        continue;
                    }
                    // Bullet list — collect consecutive bullet lines
                    if (isBullet(trimmed)) {
                        const items = [];
                        while (i < lines.length) {
                            const t = lines[i].trim();
                            if (!t || !isBullet(t)) break;
                            items.push(t.replace(/^[-*•]\s+/, ''));
                            i++;
                        }
                        out.push('<ul>' + items.map(t => `<li>${_formatInline(_escapeHtml(t))}</li>`).join('') + '</ul>');
                        continue;
                    }
                    // Numbered list
                    if (isNumbered(trimmed)) {
                        const items = [];
                        while (i < lines.length) {
                            const t = lines[i].trim();
                            if (!t || !isNumbered(t)) break;
                            items.push(t.replace(/^\d+\.\s+/, ''));
                            i++;
                        }
                        out.push('<ol>' + items.map(t => `<li>${_formatInline(_escapeHtml(t))}</li>`).join('') + '</ol>');
                        continue;
                    }
                    // Paragraph — collect adjacent non-special lines, join with space
                    const para = [];
                    while (i < lines.length) {
                        const t = lines[i].trim();
                        if (!t || isHeader(t) || isBullet(t) || isNumbered(t) || t.startsWith('```')) break;
                        para.push(t);
                        i++;
                    }
                    if (para.length) {
                        out.push(`<p>${para.map(l => _formatInline(_escapeHtml(l))).join(' ')}</p>`);
                    }
                }
                return out.join('');
            }

            function _renderSummaryLoading() {
                if (!summaryBody) return;
                summaryBody.innerHTML = `
                    <div class="summary-loading">
                        <div class="spinner"></div>
                        <div>Reading subtitles + thinking…</div>
                    </div>`;
                if (summaryRegen) summaryRegen.setAttribute('hidden', '');
                if (summaryCopy) summaryCopy.setAttribute('hidden', '');
            }

            function _renderSummaryEmpty(reason) {
                if (!summaryBody) return;
                if (reason === 'no-key') {
                    summaryBody.innerHTML = `
                        <div class="summary-empty">
                            <div>Add a Groq API key in Settings → AI to enable AI summaries and chat.</div>
                        </div>`;
                } else if (reason === 'no-subs') {
                    summaryBody.innerHTML = `
                        <div class="summary-empty">
                            <div>This video doesn't have subtitles to summarize. Re-download to fetch them, then try again.</div>
                        </div>`;
                } else {
                    summaryBody.innerHTML = `
                        <div class="summary-empty">
                            <div>Generate an AI summary of what this video covers, or just ask a question below.</div>
                            <button id="player-summary-generate-btn">Generate summary</button>
                        </div>`;
                    // Wire the button after innerHTML so we don't need a global handler reference.
                    const btn = document.getElementById('player-summary-generate-btn');
                    if (btn) btn.addEventListener('click', () => _loadSummaryForCurrent(true));
                }
                if (summaryRegen) summaryRegen.setAttribute('hidden', '');
                if (summaryCopy) summaryCopy.setAttribute('hidden', '');
            }

            function _renderSummaryArticle(md) {
                if (!summaryBody) return;
                summaryBody.innerHTML = _renderMarkdown(md);
                if (summaryRegen) summaryRegen.removeAttribute('hidden');
                if (summaryCopy) summaryCopy.removeAttribute('hidden');
            }

            // --- Chat about the current video ---
            // Conversation history is per-session, per-video. Reset whenever a new video opens.
            // We DON'T persist to disk — it's transient context, and storing every chat would
            // bloat settings.json fast. User can copy any answer they want to keep.
            let _chatTurns = [];  // [{role: 'user'|'assistant', content: string}, ...]
            let _chatBusy = false;

            function _resetChat() {
                _chatTurns = [];
                if (chatHistory) {
                    chatHistory.innerHTML = '';
                    chatHistory.setAttribute('hidden', '');
                }
                if (chatInput) chatInput.value = '';
            }

            function _appendChatBubble(role, html, opts) {
                if (!chatHistory) return null;
                chatHistory.removeAttribute('hidden');
                const div = document.createElement('div');
                div.className = 'player-chat-msg ' + role;
                if (opts && opts.thinking) div.classList.add('thinking');
                div.innerHTML = html;
                chatHistory.appendChild(div);
                if (summaryScroll) summaryScroll.scrollTop = summaryScroll.scrollHeight;
                return div;
            }

            async function _sendChatMessage() {
                if (_chatBusy) return;
                if (!chatInput) return;
                const question = chatInput.value.trim();
                if (!question) return;
                if (!currentItem || !currentItem.id) {
                    showToast('Open a video first to chat about it', null, null);
                    return;
                }
                if (!window._hasGroqKey) {
                    showToast('Add a Groq API key in Settings → AI to chat', null, null);
                    return;
                }
                _chatBusy = true;
                if (chatSend) chatSend.disabled = true;
                chatInput.value = '';

                // Render the user's message
                _appendChatBubble('user', _escapeHtml(question));
                _chatTurns.push({ role: 'user', content: question });

                // Render a "thinking..." placeholder we'll replace when the response arrives
                const thinking = _appendChatBubble('assistant', 'thinking', { thinking: true });

                try {
                    const res = await pywebview.api.chat_about_video(currentItem.id, question, _chatTurns.slice(0, -1));
                    if (!res || res.error) {
                        if (thinking) thinking.remove();
                        _appendChatBubble('assistant', `<em>${_escapeHtml(res?.error || 'Chat failed')}</em>`);
                        // Don't push a fake assistant turn — keeps the next call accurate.
                    } else {
                        const reply = res.reply || '';
                        if (thinking) thinking.remove();
                        _appendChatBubble('assistant', _renderMarkdown(reply));
                        _chatTurns.push({ role: 'assistant', content: reply });
                    }
                } catch (e) {
                    if (thinking) thinking.remove();
                    _appendChatBubble('assistant', `<em>Chat failed.</em>`);
                } finally {
                    _chatBusy = false;
                    if (chatSend) chatSend.disabled = false;
                    if (chatInput) chatInput.focus();
                }
            }

            // Load (and optionally generate) the AI summary for the current video. The
            // backend caches per-video so a cached summary returns instantly; first-time
            // generation takes ~3-8s on Groq's free tier.
            async function _loadSummaryForCurrent(forceFetch) {
                if (!currentItem || !currentItem.id) return;
                if (!window._hasGroqKey) {
                    _renderSummaryEmpty('no-key');
                    return;
                }
                // Check the local library copy first — if a cached summary is already
                // there, render instantly without a backend round-trip.
                const findEntry = () => {
                    for (const v of (app.videosInLibrary || [])) {
                        if (v.id === currentItem.id) return v;
                        if (v.type === 'playlist') {
                            const c = (v.videos || []).find(x => x.id === currentItem.id);
                            if (c) return c;
                        }
                    }
                    return null;
                };
                const entry = findEntry();
                if (!forceFetch && entry && entry.ai_summary) {
                    _renderSummaryArticle(entry.ai_summary);
                    return;
                }
                if (!forceFetch) {
                    _renderSummaryEmpty('init');
                    return;
                }
                _renderSummaryLoading();
                try {
                    const res = await pywebview.api.generate_video_summary(currentItem.id);
                    if (!res || res.error) {
                        const err = (res && res.error) || 'Summary generation failed';
                        if (/subtitles/i.test(err)) {
                            _renderSummaryEmpty('no-subs');
                        } else {
                            summaryBody.innerHTML = `<div class="summary-empty"><div>${_escapeHtml(err)}</div></div>`;
                        }
                        return;
                    }
                    _renderSummaryArticle(res.summary);
                    // Refresh local library so the cached summary is reflected in detail panel too.
                    try { app.videosInLibrary = (await pywebview.api.load_library()) || []; } catch (_) {}
                } catch (e) {
                    summaryBody.innerHTML = `<div class="summary-empty"><div>Summary generation failed.</div></div>`;
                }
            }

            function showSummaryPanel() {
                if (!summaryPanel) return;
                summaryPanel.removeAttribute('hidden');
                void summaryPanel.offsetWidth;
                summaryPanel.classList.add('visible');
                if (aiBtn) aiBtn.classList.add('active');
                _loadSummaryForCurrent(/*forceFetch*/ false);
            }
            function hideSummaryPanel() {
                if (!summaryPanel) return;
                summaryPanel.classList.remove('visible');
                if (aiBtn) aiBtn.classList.remove('active');
                setTimeout(() => {
                    if (summaryPanel && !summaryPanel.classList.contains('visible')) {
                        summaryPanel.setAttribute('hidden', '');
                    }
                }, 280);
            }
            function toggleSummaryPanel() {
                if (!summaryPanel) return;
                if (summaryPanel.classList.contains('visible')) hideSummaryPanel();
                else showSummaryPanel();
            }

            function togglePlay() {
                if (!video.src) return;
                if (video.paused || video.ended) {
                    // Resume AudioContext before play (autoplay policy may have suspended it).
                    if (_audioCtx && _audioCtx.state === 'suspended') {
                        _audioCtx.resume().catch(() => {});
                    }
                    video.play().catch(err => {
                        // play() can reject for autoplay or codec reasons — surface as error
                        console.error('play failed', err);
                        onVideoError();
                    });
                } else {
                    video.pause();
                }
            }

            function seek(deltaSec) {
                if (!isFinite(video.duration) || video.duration <= 0) return;
                video.currentTime = Math.max(0, Math.min(video.duration, video.currentTime + deltaSec));
            }

            function setSpeed(rate) {
                video.playbackRate = rate;
                speedBtn.textContent = (rate === 1 ? '1' : rate) + '×';
                // Update active state in menu
                speedMenu.querySelectorAll('.player-speed-option').forEach(opt => {
                    opt.classList.toggle('active', parseFloat(opt.getAttribute('data-speed')) === rate);
                });
            }

            // Fullscreen state. Tracked here ONLY — Python tracks its own copy via
            // set_fullscreen() being idempotent. We do NOT use the browser's element-
            // level requestFullscreen API anymore, even though we used to. Why:
            //
            // The previous implementation called BOTH canvas.requestFullscreen() (Chromium
            // element fullscreen) AND pywebview.set_fullscreen() (OS window borderless).
            // Two state machines, two exit paths (Esc fires only the element one, the
            // fullscreen button fires both). After a few toggles the two would drift and
            // the OS window would get stuck in a weird borderless-but-not-quite state —
            // visible as a black sliver of UI with no title bar after exit, which is what
            // the user just reported.
            //
            // The simpler design: only toggle OS-level borderless. The .player-canvas is
            // `position: absolute; inset: 0` of the player view, which fills cockpit-main,
            // which fills the WebView2 client area. When the OS title bar goes away,
            // cockpit-main grows by ~32px and the canvas fills the whole window. Same
            // visual result, one state machine, no race.
            let isPlayerFullscreen = false;

            function toggleFullscreen() {
                if (isPlayerFullscreen) {
                    exitPlayerFullscreen();
                } else {
                    enterPlayerFullscreen();
                }
            }

            // Black overlay used to mask the OS-toggle transition. WebView2 +
            // WinForms take ~300-500ms to settle a borderless resize, and during
            // that window the host paints flashes of white and the WebView2 child
            // visibly jumps. Rather than chase every paint glitch, we just cover
            // everything with black for the duration of the toggle and fade out
            // once polish has settled.
            let _fsMask = null;
            function _showFsMask() {
                if (!_fsMask) {
                    _fsMask = document.createElement('div');
                    _fsMask.style.cssText =
                        'position:fixed;inset:0;background:#000;z-index:9999;' +
                        'pointer-events:none;opacity:0;';
                    document.body.appendChild(_fsMask);
                }
                _fsMask.style.transition = 'none';
                _fsMask.style.opacity = '1';
                void _fsMask.offsetHeight; // flush so the next transition takes effect
            }
            function _hideFsMask() {
                if (!_fsMask) return;
                _fsMask.style.transition = 'opacity 0.18s ease-out';
                _fsMask.style.opacity = '0';
            }

            // Mac: skip the pywebview OS-fullscreen toggle (NSWindow
            // toggleFullScreen: animates to a separate Space and clashes with
            // our in-view immersive mode). The CSS class alone gives the
            // borderless feel inside the existing window. Users who want true
            // OS fullscreen still have the green button / Cmd+Ctrl+F.
            const _isMacUA = (navigator.platform || '').toLowerCase().includes('mac');

            function enterPlayerFullscreen() {
                if (isPlayerFullscreen) return;
                isPlayerFullscreen = true;
                _showFsMask();
                document.body.classList.add('player-is-fullscreen');
                if (!_isMacUA) {
                    try { pywebview.api.set_fullscreen(true); } catch(_) {}
                }
                setTimeout(_hideFsMask, 550);
            }

            function exitPlayerFullscreen() {
                if (!isPlayerFullscreen) return;
                isPlayerFullscreen = false;
                _showFsMask();
                // Disable the .cockpit grid-template-columns transition before
                // removing the body class. Otherwise the rail expands over 240ms
                // concurrently with the OS toggle and WebView2 resize, which
                // glitches the video pipeline. Restore the transition after the
                // OS toggle + all polish calls have settled.
                const cockpit = document.querySelector('.cockpit');
                if (cockpit) cockpit.style.transition = 'none';
                document.body.classList.remove('player-is-fullscreen');
                if (!_isMacUA) {
                    try { pywebview.api.set_fullscreen(false); } catch(_) {}
                }
                setTimeout(() => {
                    if (cockpit) cockpit.style.transition = '';
                    _hideFsMask();
                }, 700);
            }

            // Esc to exit — this used to come for free from the browser's element-
            // fullscreen API. Since we're not using that anymore, we listen ourselves.
            // Only react when we're in player fullscreen AND no modal/menu is open
            // (those have their own Esc handlers and should win).
            document.addEventListener('keydown', (e) => {
                if (e.key !== 'Escape') return;
                if (!isPlayerFullscreen) return;
                // Don't steal Esc if there's an open speed menu or side panel
                const speedOpen = speedMenu && !speedMenu.hasAttribute('hidden');
                const sidePanelOpen = document.getElementById('player-side-panel')
                    && !document.getElementById('player-side-panel').hasAttribute('hidden');
                if (speedOpen || sidePanelOpen) return;
                e.preventDefault();
                exitPlayerFullscreen();
            });

            // Safety net: if the user leaves the player view (back button, switching to
            // queue/library) while in fullscreen, exit fullscreen first. Otherwise the
            // window would stay borderless on a non-player page.
            window.addEventListener('beforeunload', () => {
                if (isPlayerFullscreen) {
                    try { pywebview.api.set_fullscreen(false); } catch(_) {}
                }
            });

            function showControls() {
                controls.classList.remove('hidden');
                canvas.classList.remove('controls-hidden');
                const topOverlay = document.getElementById('player-top-overlay');
                if (topOverlay) topOverlay.classList.remove('hidden');
                if (hideTimer) clearTimeout(hideTimer);
                if (!video.paused) hideControlsSoon();
            }

            function hideControlsSoon(delay = 2200) {
                if (hideTimer) clearTimeout(hideTimer);
                hideTimer = setTimeout(() => {
                    if (video.paused) return;  // never hide while paused
                    if (speedMenu && !speedMenu.hasAttribute('hidden')) return;  // never hide if menu is open
                    controls.classList.add('hidden');
                    canvas.classList.add('controls-hidden');
                    const topOverlay = document.getElementById('player-top-overlay');
                    if (topOverlay) topOverlay.classList.add('hidden');
                }, delay);
            }

            function onVideoError() {
                // Intentionally NOT showing the error overlay here. HTML5 video error
                // events fire spuriously during normal streaming (Range request blips,
                // buffer hiccups, previous-src teardown) and we kept getting false-positive
                // overlays on videos that play fine. The "Open in VLC" button in the top bar
                // is always visible as a manual escape hatch if playback genuinely fails.
                const err = video.error;
                console.log('[player] video error event (non-fatal, suppressed)', {
                    code: err?.code,
                    currentTime: video.currentTime,
                    readyState: video.readyState
                });
            }

            function clearError() {
                errorOverlay.setAttribute('hidden', '');
            }

            function back() {
                // Always exit fullscreen first — we don't want the OS window to stay
                // borderless on the library/queue pages.
                if (isPlayerFullscreen) exitPlayerFullscreen();
                stop();
                app.switchView('library');
            }

            // Public API
            return {
                async open(item) {
                    bindOnce();
                    currentItem = item;
                    clearError();
                    hideResumeToast();
                    // New video — release any leftover pin from a previous video that
                    // ended. Otherwise the new video would inherit the 100% seek-bar lock.
                    _videoEndedPinned = false;
                    // Don't hide side panel here — if the user is using it to navigate
                    // between videos, slamming it closed every open() is jarring. Panel
                    // stays in whatever state the user left it in (matches YouTube).
                    titleEl.textContent = item.title || 'Untitled';
                    const metaParts = [];
                    if (item.uploader) metaParts.push(item.uploader);
                    if (item.duration_string) metaParts.push(item.duration_string);
                    metaEl.textContent = metaParts.join(' · ');

                    app.switchView('player');

                    // Reset state
                    video.pause();
                    video.removeAttribute('src');
                    video.load();
                    if (speedMenu) speedMenu.setAttribute('hidden', '');
                    pendingResumeSeconds = 0;
                    lastSavedPosition = 0;
                    // Subtitles: reset overlay + state (always starts OFF) and kick off a
                    // background load. The CC button stays hidden until cues arrive.
                    _resetSubtitles();
                    _loadSubtitlesForCurrent();
                    // AI summary + chat: chat history is per-video and per-session — reset
                    // every time a new video opens. Summary content is per-video and cached
                    // server-side; refresh the rendered article only if the panel is open.
                    _resetChat();
                    if (summaryPanel && summaryPanel.classList.contains('visible')) {
                        _loadSummaryForCurrent(false);
                    }

                    // Apply saved volume preference (one-time, lazy — only on first open
                    // since subsequent opens use whatever the user set during last session)
                    if (!window._volumeRestored) {
                        try {
                            const savedVol = await pywebview.api.get_setting('player_volume');
                            const savedMuted = await pywebview.api.get_setting('player_muted');
                            if (typeof savedVol === 'number' && savedVol >= 0 && savedVol <= VOLUME_MAX) {
                                setEffectiveVolume(savedVol);
                            }
                            if (savedMuted) video.muted = true;
                        } catch (_) {}
                        window._volumeRestored = true;
                    }

                    console.log('[player] opening item', { id: item.id, filepath: item.filepath });

                    // Show the spinner during the prep step. For most files this resolves
                    // in <100ms (just an ffprobe call on the backend), but legacy MKV/HEVC
                    // entries trigger an ffmpeg transcode which can take seconds-to-minutes.
                    // The spinner reassures the user the app isn't frozen during that wait.
                    showSpinner();
                    let result;
                    try {
                        result = await pywebview.api.get_video_stream_url(item.id);
                    } catch (e) {
                        // The Python side now wraps its body in try/except and
                        // surfaces structured errors via {error: ...}, so reaching
                        // this catch path implies a pywebview-level failure (API
                        // bridge dead, JSON serialization, etc.) — surface what we
                        // can so the user has something to act on.
                        console.error('[player] get_video_stream_url threw', e);
                        hideSpinner();
                        const detail = (e && (e.message || e.toString())) || 'unknown';
                        errorHint.textContent = `Stream API failed: ${detail}`;
                        errorOverlay.removeAttribute('hidden');
                        return;
                    }
                    // Backend returned a "preparing" marker — a transcode is
                    // running in a thread. Show progress in the player title
                    // and wait for protubePrepDone/Error before continuing.
                    if (result && result.preparing) {
                        const origTitle = titleEl.textContent;
                        titleEl.textContent = 'Preparing video for in-app playback…';
                        result = await new Promise(resolve => {
                            window._prepJobs[result.job_id] = {
                                resolve,
                                onProgress: (pct) => {
                                    titleEl.textContent =
                                        `Preparing video for in-app playback… ${pct}%`;
                                },
                            };
                        });
                        // Restore the real title; if result is a success, the
                        // normal path below will leave it alone (already set
                        // earlier from item.title), but in the error case we
                        // want the original visible behind the overlay.
                        if (origTitle) titleEl.textContent = origTitle;
                    }

                    if (!result || result.error) {
                        hideSpinner();
                        errorHint.textContent = result?.error || 'Could not start video stream.';
                        errorOverlay.removeAttribute('hidden');
                        return;
                    }

                    // Stash the resume position. loadedmetadata applies it once duration is known.
                    pendingResumeSeconds = result.last_position_seconds || 0;
                    lastSavedPosition = pendingResumeSeconds;

                    video.src = result.url;
                    video.load();
                    _applyVolume();  // reapply gain after src change
                    bigPlay.classList.add('visible');
                    showControls();
                    setSpeed(app._defaultSpeed || 1);

                    // Update the Windows SMTC tray metadata for this video. Title and
                    // channel are immediate; artwork is fetched async via the same
                    // marker→data-url pipeline the library cards use, then patched
                    // onto the existing MediaMetadata object once it resolves.
                    if ('mediaSession' in navigator) {
                        try {
                            const md = new MediaMetadata({
                                title: item.title || 'ProTube Saver',
                                artist: item.uploader || '',
                                album: 'ProTube Saver',
                                artwork: [],
                            });
                            navigator.mediaSession.metadata = md;
                            // Resolve thumbnail. Library entries store `thumbnail` as either
                            // a remote URL (legacy) or a "marker:..." token resolvable via
                            // get_thumbnail_data. Pass through whatever resolves.
                            const thumb = item.thumbnail;
                            if (thumb) {
                                const apply = (src) => {
                                    if (!src) return;
                                    try {
                                        navigator.mediaSession.metadata = new MediaMetadata({
                                            title: md.title,
                                            artist: md.artist,
                                            album: md.album,
                                            artwork: [
                                                { src, sizes: '512x512', type: 'image/jpeg' },
                                                { src, sizes: '256x256', type: 'image/jpeg' },
                                                { src, sizes: '96x96',  type: 'image/jpeg' },
                                            ],
                                        });
                                    } catch (_) {}
                                };
                                if (thumb.startsWith('http')) {
                                    apply(thumb);
                                } else if (app._thumbCache && app._thumbCache[thumb]) {
                                    apply(app._thumbCache[thumb]);
                                } else {
                                    pywebview.api.get_thumbnail_data(thumb).then(dataUrl => {
                                        if (dataUrl) {
                                            if (app._thumbCache) app._thumbCache[thumb] = dataUrl;
                                            apply(dataUrl);
                                        }
                                    }).catch(() => {});
                                }
                            }
                        } catch (_) { /* MediaSession not available on this build */ }
                    }

                    // If the side panel is currently visible, re-render its list so the
                    // NOW badge moves to the freshly-opened video and the row order
                    // updates. Cheap operation, only fires when panel is actually open.
                    const sidePanel = document.getElementById('player-side-panel');
                    if (sidePanel && sidePanel.classList.contains('visible')) {
                        renderSidePanelList();
                    }

                    video.play().catch(err => {
                        console.warn('[player] autoplay rejected (user can click play)', err);
                    });
                },
                stop() {
                    if (!video) return;
                    // Save final position before tearing down — covers the "back button"
                    // case where timeupdate's throttled save might have missed the last seek.
                    if (currentItem && video.currentTime > 0 && !isNaN(video.duration)) {
                        try {
                            pywebview.api.save_playback_position(
                                currentItem.id,
                                video.currentTime,
                                video.duration || 0
                            );
                        } catch (_) {}
                    }
                    try {
                        video.pause();
                        video.removeAttribute('src');
                        video.load();
                    } catch (_) {}
                    // Clear the tray entry so the SMTC widget collapses after the
                    // user leaves the player. Without this, the tray would keep
                    // showing the last-played title indefinitely.
                    if ('mediaSession' in navigator) {
                        try {
                            navigator.mediaSession.metadata = null;
                            navigator.mediaSession.playbackState = 'none';
                        } catch (_) {}
                    }
                    if (hideTimer) clearTimeout(hideTimer);
                    hideSpinner();
                    hideResumeToast();
                    hideSidePanel();
                    // Always exit fullscreen on stop — same reason as back(), we don't
                    // want the window to stay borderless after leaving the player.
                    if (isPlayerFullscreen) exitPlayerFullscreen();
                    currentItem = null;
                }
            };
        })();



        window.addEventListener('pywebviewready', () => app.init());

        // ----- Idle-throttle keep-alive (B6) -----
        // Chromium will eventually freeze a page that does nothing for hours, even with
        // our disable-features flags. This heartbeat does the smallest possible activity
        // every 30s so the scheduler always sees recent work and never marks us frozen.
        // requestAnimationFrame keeps the compositor active; the dataset write is a
        // no-op DOM mutation that still increments lifecycle counters.
        (function startKeepAlive() {
            let tick = 0;
            const beat = () => {
                tick++;
                try {
                    document.body && document.body.setAttribute('data-heartbeat', String(tick));
                    requestAnimationFrame(() => {});
                } catch (_) {}
            };
            setInterval(beat, 30000);
            beat();
            // Also flush activity on visibilitychange — covers cases where the user
            // returns after a long absence and Chromium needs a poke to fully resume.
            document.addEventListener('visibilitychange', () => {
                if (document.visibilityState === 'visible') beat();
            });
            window.addEventListener('focus', beat);
        })();

        // Heartbeat ticker — keeps Chromium's foreground-idle throttling from
        // freezing the UI after several minutes of no user input. Pairs with the
        // IntensiveWakeUpThrottling flags in main.py: those should kill the
        // throttling, but on some WebView2 builds the flags are partially
        // ignored. A periodic API call from JS guarantees the renderer event
        // loop has work scheduled regardless of what the flags do or don't do.
        // 30s interval is well under the ~5min throttle threshold but rare
        // enough that overhead is unmeasurable. Pure no-op on the Python side.
        window.addEventListener('pywebviewready', () => {
            setInterval(() => {
                try { window.pywebview.api.heartbeat(); } catch (_) { /* swallow */ }
            }, 30 * 1000);
        });

        // Splash safety net — if init() throws or hangs, dismiss splash anyway after 8s
        // so the user isn't stuck staring at a logo. Better to show a partial app than
        // a frozen one. The normal happy-path dismissal in init() runs much earlier.
        setTimeout(() => {
            const splash = document.getElementById('splash');
            if (splash && !splash.classList.contains('fade-out')) {
                console.warn('[splash] safety-net dismissal — init() may have hung');
                splash.classList.add('fade-out');
                setTimeout(() => { try { splash.remove(); } catch(_) {} }, 400);
            }
        }, 8000);

        // ================================================================
        // BACKGROUND HEARTBEAT — keeps the UI thread alive when unfocused.
        // ================================================================
        // WebView2/Chromium aggressively throttles JS timers on background
        // windows, even with all the --disable-* flags set. The flags help
        // but don't fully eliminate it. Without intervention, the app
        // appears "frozen" until the user clicks back into it.
        //
        // Workaround: spawn a Web Worker that posts a heartbeat message
        // every 250ms. Workers run on a separate thread and aren't subject
        // to the same throttling rules as the main UI thread. Each
        // postMessage triggers an event-loop tick on the main thread,
        // which keeps timers, animations, and pending state changes
        // running at near-normal cadence even when the window is hidden.
        //
        // Cost: negligible CPU (<0.1%), negligible battery. The worker
        // does literally nothing except setInterval(postMessage, 250).
        // ================================================================
        try {
            const workerCode = `
                setInterval(() => { self.postMessage('tick'); }, 250);
            `;
            const blob = new Blob([workerCode], { type: 'application/javascript' });
            const heartbeatWorker = new Worker(URL.createObjectURL(blob));
            // We don't need to do anything with the message — just receiving it
            // is enough to wake the event loop. We do increment a counter so we
            // can verify it's running if we ever need to debug.
            window._heartbeatTicks = 0;
            heartbeatWorker.onmessage = () => { window._heartbeatTicks++; };
            console.log('[heartbeat] worker started — UI stays alive when unfocused');
        } catch (e) {
            console.warn('[heartbeat] worker failed to start, app may freeze when unfocused', e);
        }

        // When the user alt-tabs back to the app, Chromium's background throttling lifts
        // on focus — but any in-flight setTimeout that was delayed may have fallen behind.
        // We force a render refresh on focus to show the latest state from backend.
        // Also pings any active downloads' progress state so UI catches up.
        function _onAppRefocus() {
            if (!window.app || !app.elements) return;
            // Refresh the active view's render
            if (app.currentView === 'library') {
                app.renderLibrary();
            } else {
                app.renderQueue && app.renderQueue();
            }
            // Pull latest download speeds/progress from backend if anything is active
            if (typeof pywebview !== 'undefined' && pywebview.api && pywebview.api.get_active_progress) {
                pywebview.api.get_active_progress().then(progress => {
                    if (!progress || !Array.isArray(progress)) return;
                    progress.forEach(p => {
                        if (typeof updateItemProgress === 'function') {
                            updateItemProgress(p.id, p.pct, p.speed, p.playlist_id || null);
                        }
                    });
                }).catch(() => {});
            }
            // Wake WebView2's paint compositor.
            //
            // The CSS transform flush we used to do here was insufficient — it can
            // force a repaint when the compositor is alive but pausing frames, but
            // does NOTHING when the compositor surface is paused at the Win32 level.
            // That's the actual state after a long unfocused interval: WebView2's
            // host child HWND stops receiving WM_PAINT from the parent WinForm,
            // which is why only a *physical click* used to wake the UI (a click
            // forces RedrawWindow internally).
            //
            // Real fix: call into Python to invoke RedrawWindow + SetWindowPos
            // with RDW_ALLCHILDREN, propagating the invalidate down into the
            // WebView2 child surface. apply_window_polish() already does exactly
            // this (it was originally written for the fullscreen corner fix) so
            // we just reuse it. Three calls staggered over 240 ms — the compositor
            // sometimes needs more than one nudge to fully resume from a long pause.
            //
            // Source/research: WebView2Feedback issues #5171 and #3674 (paint
            // omitted after restore from minimized / unfocused — only input wakes it).
            try {
                if (typeof pywebview !== 'undefined' && pywebview.api && pywebview.api.apply_window_polish) {
                    pywebview.api.apply_window_polish().catch(() => {});
                    setTimeout(() => { pywebview.api.apply_window_polish().catch(() => {}); }, 80);
                    setTimeout(() => { pywebview.api.apply_window_polish().catch(() => {}); }, 240);
                }
            } catch (_) { /* swallow */ }

            // Belt-and-suspenders CSS flush — cheap, harmless, and on the rare
            // case where Win32 RedrawWindow doesn't reach far enough this can
            // still pop a stale frame loose.
            try {
                // eslint-disable-next-line no-unused-expressions
                document.body.offsetHeight;
                requestAnimationFrame(() => {
                    document.body.style.transform = 'translateZ(0)';
                    requestAnimationFrame(() => {
                        document.body.style.transform = '';
                    });
                });
            } catch (_) { /* swallow */ }
        }
        window.addEventListener('focus', _onAppRefocus);
        document.addEventListener('visibilitychange', () => {
            if (!document.hidden) _onAppRefocus();
        });

        // ================================================================
