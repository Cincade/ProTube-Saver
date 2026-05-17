# Changelog

All notable user-facing changes to ProTube Saver. Latest at the top.

Format: brief, plain-language one-liners. The contents of each release section
become the `releaseNotes` field in the landing site's `version.json` — paste
verbatim when shipping. Drop one line under "Unreleased" every time you land a
user-facing change so this is already written when it's release time.

Release cadence: ~monthly, plus same-day hotfixes for crashes / data loss /
"app won't launch". 200MB download is the constraint — don't push more often.

---

## Unreleased

- (move user-facing changes here as you land them)

---

## v1.3.2 — 2026-05-17 (hotfix)

- Fixes: **In-app update Download button did nothing on Mac.** The GitHub-releases asset picker filtered for `.exe` only, so on Mac (where the release asset is a `.zip`) the resolved `downloadUrl` came back empty — the update pill would fire and the modal would open, but clicking Download was a no-op. Now platform-aware: matches `.dmg`/`.zip` (Mac), `.exe`/`.msi`/`.zip` (Windows), or `.AppImage`/`.deb`/`.tar.gz`/`.zip` (Linux), and disambiguates with a platform keyword (`mac`/`win`/`linux`) in the asset name so a Mac install doesn't accidentally grab the Windows zip when both are attached to one release. Existing Mac users on v1.3.0 or v1.3.1 need one manual update to v1.3.2 from the GitHub Releases page — after that, in-app auto-update works for every release going forward.

---

## v1.3.1 — 2026-05-17 (hotfix)

- Fixes: **2K / 4K downloads silently saved as 1080p.** The format selector preferred AVC1 (H.264) MP4 first for maximum device compatibility — but YouTube only serves AVC1 up to 1080p. At 1440p / 2160p the streams are AV1-in-MP4 or VP9-in-WebM, so the AVC1-preferred selector quietly resolved to the 1080p AVC1 stream even when you'd picked 2K or 4K. Above 1080p we now skip the AVC1 filter and take AV1-in-MP4 (or VP9 as a fallback); the in-app player already decodes both natively, so playback is unchanged. ≤1080p selection is untouched (still AVC1-preferred for Smart-TV-grade compatibility).

---

## v1.3.0 — 2026-05-17

**Mac support + music-feature parity + queue/library polish + search-side For-You.**

- Adds: **Native macOS build.** First Mac release. App runs as a `.app` bundle (universal2 — Apple Silicon + Intel), launches into native fullscreen on first open, and stores data under `~/Library/Application Support/ProTube Saver/` like every other Mac app (Windows install layout is unchanged). All Windows-only code paths (WebView2 env vars, `os.startfile`, named-mutex single-instance lock, DWM corner polish) are now `sys.platform`-gated, and ffmpeg/ffprobe ship as bundled universal2 binaries.
- Adds: **Album cover art is now real.** YouTube's `i9.ytimg.com/s_p/.../maxresdefault.jpg` cover URLs frequently 404 (even with the signed query string yt-dlp captures). Backend now extracts the embedded album art directly from the downloaded `.m4a` file via ffmpeg the moment the first track of an album finishes, caches it locally, and serves it through the `pt:thumb:` marker scheme. Existing albums with broken covers get repaired automatically on the next Music tab open.
- Adds: **Per-track artwork everywhere.** Album-detail rows, the player view, the dock mini-player, and the For-You "Pick up where you left off" cards now all show each track's own thumbnail (not the album cover repeated N times). One-shot data repair restores the YouTube video thumbnail for any track whose art got incorrectly rewritten to the album cover marker by an earlier build.
- Adds: **Search "For You" landing on the video search tab.** When the search box is empty, the page now mirrors the music side's 4-row landing — Recent searches (clickable chips), Pick up where you left off (recently added library videos), Because you have [your top uploader] (6 fresh YouTube results, 24 h cached), Trending on YouTube (FEtrending feed, 24 h cached), and From your library (shuffled). Clicking the × in the search box also returns you to this landing instead of a blank prompt.
- Adds: **Music search infinite scroll.** When you scroll near the bottom of music search results, the next page loads automatically via YouTube Music's continuation tokens.
- Changes: **Music download queue rebuilt on the video-playlist row primitive.** The album queue row now uses the exact same `.playlist-row` markup as the video queue's playlist rows — same blue hover, same right-side controls, same flex behavior. Done state across both queues is now a green check badge (matches the "already in your library" indicator from search results), not a bare text label.
- Fixes: **Queue thumbnail flicker.** Per-track progress no longer re-renders the entire queue list on every update; the affected row is patched in place and the parent album header's aggregate bar updates surgically. Backend also coalesces the 4 settings.json writes per track completion into a single write (`_deferred_save()` context), cutting disk I/O ~4× and stopping the visible flash on each track flip.
- Changes: **One toast per album.** Multi-track album downloads no longer fire a toast for every track completion — just one when the whole album finishes.
- Fixes: **"Already in your queue" badge fires too early.** Search-result cards now show "Adding…" while the queue fetch is in flight and only flip to the green "Already in your queue" check once the backend confirms the video is in the queue.
- Polish: **Album cover load gate.** Album entries in the library wait to appear until the cover is resolved + the artist is non-Unknown — no more flash-of-default-placeholder for the first second after a download finishes.
- Adds: **Music player fullscreen button.** Discoverable button next to the volume control on the player view (also still bound to **F**). Enters the immersive mode (hides rail + dock, player fills the viewport). Press F or Esc to exit.
- Polish: **Stable progress text.** The "Downloading X%" pill no longer cycles widths as the percentage ticks up — it's a fixed-width "Downloading" label, with the live percentage in the bar text beneath. Eliminates the per-tick row reflow.
- Polish: **Up Next pill** lifted a row higher on the player view (was flush at the bottom edge). Music dock made a touch thicker (78 → 88 px).
- Polish: **Rail logo center-aligned** with the nav-icon column.
- Polish: **Music card hover** matches the video library card's blue outline. Single-click opens the album detail; double-click immediately starts playing.
- Polish: **Per-track thumbnails in album view** — small 44 px thumb in each row so the table isn't visually empty.
- Polish: **Search autocomplete dismisses on Enter / suggestion click** (defended against re-show races with the input's `input` event handler).
- Fixes: **App starts maximized on Windows, native fullscreen on Mac** — Windows honors `maximized=True` natively; Mac uses the right combination of `NSWindowCollectionBehaviorFullScreenPrimary` + `toggleFullScreen_` dispatched on the main thread via `AppHelper.callAfter`, hooked into pywebview's `window.events.shown` so the timing is correct.

### Music feature (also shipped in this release)

- Adds: **Music mode.** New Music tab in the rail with two sub-tabs — "Your Library" (Spotify-style album-art grid of your downloaded tracks) and "Search YouTube Music" (compact list-row results from YouTube Music's own API, with Songs / Videos / Albums / Artists / Playlists filters). Click + on a search row to download the track as M4A with embedded ID3 tags + cover art (lands in `data/music/<Artist>/<Album>/<Title>.m4a`).
- Adds: **Music search "For You" landing.** Empty-state landing shows Recent searches as clickable chips, Pick up where you left off, Because you have [your top artist] (YT Music recommendations keyed off your library's top artist — cached 24 h), Trending on YouTube Music (anonymous Innertube charts — cached 24 h), and From your library (shuffled).
- Adds: **Real album / playlist / artist downloads.** Clicking the + on a YT Music album, playlist, or artist row now bulk-downloads its tracks (artist capped at 25 to avoid accidental whole-channel pulls).
- Redesigns: **Music player view** to the "Centered Hero" layout — small album art (300 px) centered horizontally over the title/artist, blurred album-art backdrop fills the stage, and a flat bottom panel with Up Next / Lyrics / Related tabs.
- Adds: **Full music player view + mini-player dock** that stays visible across every tab once music starts playing.
- Adds: **Shuffle + repeat actually work.** Shuffle picks a random unplayed track from a "bag" that refills when exhausted. Repeat cycles off → all → one. State persists across launches.
- Adds: **× clear button** inside the YouTube search and YouTube Music search boxes.
- Adds: **Mouse-wheel volume.** Scroll anywhere over the music dock to nudge volume (5% per notch).
- Fixes: **Volume slider felt useless past 25%.** Audio volume now follows a perceptual log curve so the bottom half gives fine control where you actually use it.
- Fixes: **Music seek bar fights you on drag.** Rebuilt both the dock and full-player seek bars with Pointer Events + `setPointerCapture`.

---

## v1.2.0 — 2026-05-13

**Feature release: in-app YouTube search + B4/B6 fixes + nightly yt-dlp opt-in.**

- Adds: **YouTube search inside the app.** New Search tab in the rail. Type a query → get videos, channels, or playlists right in ProTube — no need to open YouTube. Search is fast (sub-second typical) because it hits YouTube's own Innertube API directly instead of scraping the HTML page. Suggestions appear as you type. Result cards mirror YouTube's own search layout: 16:9 thumbnail, title, "X views · Y ago", channel name with avatar and verified ✓ badge, and a 2-line description snippet. Two interaction modes: **video and playlist** results are click-to-select with a bottom action bar to push the batch; **channel** results have their own one-click "Subscribe" button that queues the channel immediately. Items already in your queue/library are marked, and clicking jumps to them. True infinite scroll — scrolling past the bottom keeps loading more results until YouTube runs out. Adding to queue keeps you in the search view (no auto-navigate) so you can keep browsing.
- Fixes: Quality picker mislabeling ultrawide / 2:1-aspect videos. Was reading raw `height` and rendering anamorphic 4K (3840×1920) as "1920p", making users think no 4K was available. Now reads yt-dlp's `format_note` first so 4K reads as "2160p" regardless of aspect ratio.
- Fixes: Player view going unresponsive after a long idle (~4hr). Disabled additional Chromium throttling features (TabFreeze, PageFreeze, FreezePolicy, HighEfficiencyModeAvailable, BackForwardCache) and added a 30-second JS heartbeat so the page scheduler never sees us as inactive.
- Adds: Opt-in nightly yt-dlp updates. Stable yt-dlp lags YouTube extraction fixes by weeks — the auto-updater now respects a `yt_dlp_use_nightly` setting and pulls the latest dev build when enabled.

---

## v1.1.1 — 2026-05-06 (hotfix)

- Fixes: A subtitle download failure (network blip / YouTube endpoint timeout) was killing the whole video download with an ERROR badge — even though the actual video bytes downloaded fine. Subtitles are now fetched in a separate, non-fatal step after the video succeeds, so a curl/network hiccup on the subtitle endpoint just means no subs for that video instead of losing the whole download.

---

## v1.1.0 — 2026-05-05

**Major feature release: AI summaries + chat, subtitles, 300% volume, and channel-to-queue.**

### Subtitles
- Every video now automatically downloads its subtitles — real captions when YouTube has them, auto-captions as fallback. New **CC** button in the player toggles them on/off (always starts off per video). Subtitles render in DM Sans with a soft backdrop, not the browser's default look. Strictly single-line; rolling-caption duplicates are smartly deduped; `[Music]` / `[Applause]` annotations are filtered out.

### AI features (optional — needs a free Groq API key)
- **AI summaries**: floating side panel in the player with a structured video summary — Overview, What it covers, Key points, Bottom line. Cached per video; one ↻ regenerates if needed; Copy button puts the summary on the clipboard. Sparkle icon button in the player controls.
- **AI chat**: ask anything about the video below the summary. Answers are grounded in the video's transcript. Conversation resets per video; typing in the chat box doesn't trigger player keyboard shortcuts.
- **Auto-polish subtitles**: optional Settings toggle that runs YouTube auto-captions through Llama 3.3 70B for punctuation, capitalization, and homophone fixes. Cached so each video only costs one API call.

### Volume
- Volume boost now goes up to **300%** via Web Audio GainNode + DynamicsCompressor limiter (slider 1/3 = 100%, 2/3 = 200%, full = 300%). Fixed the silent-above-100% bug at the source — the localhost video server now sends `Access-Control-Allow-Origin` so Web Audio gets untainted samples.

### Quality of life
- New **"Add channel to queue"** button in any video's detail panel. One click queues the entire channel; smart duplicate detection so you don't accidentally re-add channels already in your queue or library.
- New **default playback speed** setting in Settings → Defaults — applied each time a video opens.
- yt-dlp upgraded to **2026.5.3** (nightly). Fixes recent YouTube extraction breakages that the older stable build couldn't keep up with.

---

## v1.0.2 — 2026-05-02

- App launches significantly faster — yt-dlp import is now deferred until you actually paste a URL (was costing ~4.5s on every startup).
- Offline pill auto-fades after a few seconds instead of staying onscreen the whole time you're offline.
- Offline pill now reliably reappears as "Back online" when connection returns (was getting stuck on WebView2).
- URL paste while offline gives an instant friendly message instead of spinning for 15 seconds.
- "Add" button and "Check for updates" buttons go visually disabled while offline.
- "Update available" pill auto-hides while offline (no point — download needs internet).

## v1.0.1 — 2026-05-02

- Adds: Open To preference (Library or Queue at startup).
- Adds: Update check + corner pill that surfaces when a new release is published.
- Adds: Bulk pin in selection mode.

## v1.0.0 — 2026-05-01

- Initial release of ProTube Saver as a standalone Windows desktop app.
- YouTube downloader (videos, playlists, channels) with in-app player.
- Library + queue with selection mode, hide/unhide, pin-to-top.
- Settings drawer (Downloads, Defaults, About).
- Portable: all state lives in `data/` next to the exe.
