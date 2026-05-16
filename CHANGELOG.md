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

- Adds: **Music search "For You" landing.** When the music search box is empty, the page now shows up to 5 rows instead of a blank prompt — Recent searches as clickable chips, Pick up where you left off (recently added library tracks), Because you have [your top artist] (YT Music recommendations keyed off the artist with the most tracks in your library — cached 24 h), Trending on YouTube Music (anonymous Innertube charts — cached 24 h), and From your library (shuffled sample). All rows derived from local data + anonymous Innertube. Clicking a library card plays it; clicking a search-result card runs a fresh search for that title.
- Changes: **Music search click contract.** Single-click on a search row toggles selection (visible checkbox on the left). Multi-select lets you build a download batch — a floating action bar slides up at the bottom showing "N selected" + Download all. Esc clears the selection. Right-click any row for a context menu (Download / Add to selection / Copy YouTube link / Open in YouTube Music). The + button is now an explicit hover-reveal download — clicking a row no longer surprises you with a download.
- Adds: **Real album / playlist / artist downloads.** Clicking the + on a YT Music album, playlist, or artist row now bulk-downloads its tracks (artist capped at 25 to avoid accidental whole-channel pulls). Backend resolves the collection via yt-dlp's flat extraction, then queues each track through the existing single-track downloader so progress rings work per row. Replaces the previous "opens externally" fallback.
- Redesigns: **Music player view** to the "Centered Hero" layout (F3) — small album art (300 px) centered horizontally over the title/artist, blurred album-art backdrop fills the stage, and a flat bottom panel with **Up Next / Lyrics / Related** tabs. Up Next shows the next 5 tracks from the library (or the shuffle bag if shuffle is on). Lyrics + Related are placeholders for now. The volume slider moves out of the bottom-right corner pill and back into the transport row, right-aligned next to a clickable mute button — Spotify-desktop convention. Shuffle/repeat toggles live on the left of the transport row.
- Polish: **Music download progress is now a blue ring around the album art** (with a big % overlay), not a strip above the results or a tiny ring crammed into a button slot. The artwork dims while downloading so the progress reads clearly.
- Adds: **NEW pill** on library cards — small blue chip in the top-left of the artwork for any track added in the last 24 h.
- Polish: **Music play buttons** restyled to the app's standard (white circle, dark icon) with no scale-on-hover. The mini-bar play icon no longer visually shifts when you hover it — the new path's centroid sits at the geometric center of the button.
- Moves: **Music player volume** to the bottom-right corner of the player view (Spotify-desktop style), with a clickable mute button. Mute also now works in the mini-bar.
- Fixes: **Music seek bar fights you on drag.** Rebuilt both the dock and full-player seek bars with Pointer Events + `setPointerCapture`, so dragging the thumb tracks the cursor smoothly all the way to release (and keeps tracking even if your finger/mouse slips off the bar). An `isSeeking` flag suppresses the timeupdate repaint that was previously snapping the thumb back mid-drag.
- Adds: **Mouse-wheel volume.** Scroll anywhere over the music dock to nudge the volume (5% per notch). The volume track itself also responds. Long-overdue Spotify-style touch.
- Fixes: **Volume slider felt useless past 25%.** Audio volume now follows a perceptual log curve (`Math.pow(slider, 4)`) instead of the raw linear slider position, so the bottom half of the slider gives you fine control where you actually use it and the top half doesn't sound the same as 100%.
- Adds: **Shuffle + repeat actually work.** Shuffle picks a random unplayed track from the library each time the current one ends (no immediate repeats — uses a "bag" that refills when exhausted). Repeat cycles off → all → one (one shows a tiny "1" badge). State persists across launches.
- Adds: **× clear button** inside the YouTube search and YouTube Music search boxes — visible whenever the input has content, click to wipe.
- Polish: Music play buttons now use the Spotify-spec **#1ED760 green with a black icon** (32 px in the dock, 56 px in the full player). Seek thumbs scale and the fill turns green on hover/drag so it's obvious the thing is grabbable.
- Adds: **Music mode.** New Music tab in the rail with two sub-tabs — "Your Library" (Spotify-style album-art grid of your downloaded tracks) and "Search YouTube Music" (compact list-row results from YouTube Music's own API, with Songs / Videos / Albums / Artists / Playlists filters). Click + on a search row to download the track as M4A with embedded ID3 tags + cover art (lands in `data/music/<Artist>/<Album>/<Title>.m4a`).
- Adds: **Full music player view.** Click any track to open a dedicated now-playing view with big album art, title / artist / album, large transport controls, and a wider seek bar — like the video player but for audio. Prev/next walk the music library; shuffle and repeat are stubs (toast says "coming soon").
- Adds: **Mini-player dock** that stays visible across every tab once music starts playing. Clickable to re-open the full player view. Has an expand button and a close (×) button to dismiss playback entirely. Hidden during fullscreen video so it doesn't clash with the video player, and hidden inside the full music player view (would be redundant).
- Adds: **Download progress strip** in the music view — a compact spinner row per in-progress music download showing the track title + percent, so the user never has to wonder whether a download is happening.
- Fixes: Music playback failing with "could not load playback" — the localhost stream server's path-whitelist only included the video library, so requests for files in the music library were 404'd. Now music files in the library pass the same security check.

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
