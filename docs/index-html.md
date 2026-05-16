# index.html — file map and architecture

The entire frontend lives in this one file: `~14,000 lines, ~620 KB`. No build step, no bundler, no framework. CSS + HTML + JS inline, hand-edited. PyInstaller bundles it as a static asset that pywebview loads via `webview.create_window(url=resource_path('index.html'))`.

This doc is a **map**: where things live, how they connect, and where to add new stuff. For project-wide stuff (build commands, WebView2 quirks, the Python backend) see `CLAUDE.md` at the project root.

---

## Big picture

```
┌─────────────────────────────────────────────────────────────────┐
│ <head>                                                          │
│  ↳ <style> ........................ ~6,000 lines of CSS         │
│       Cockpit shell + rail + library + queue + player +         │
│       detail panel + drawers + modals + pills + animations      │
├─────────────────────────────────────────────────────────────────┤
│ <body>                                                          │
│  ├─ Splash overlay                                              │
│  ├─ #cockpit ─ <aside.rail> + <main.cockpit-main>               │
│  │              └─ <view-pane>'s: library, queue, player,       │
│  │                  playlist-detail (rendered inside main)      │
│  ├─ Modals (siblings of cockpit, position:fixed when open):     │
│  │    fix-modal, confirm-modal, import-picker (imp-),           │
│  │    updates-modal (upd-), update-modal, settings-drawer,      │
│  │    onboarding (onb-)                                         │
│  ├─ Selection action bar (bottom slide-up)                      │
│  ├─ DnD overlay                                                 │
│  ├─ Toast container                                             │
│  ├─ Update-available pill (corner)                              │
│  └─ Queue-new-pill (bottom-center, "↓ N new")                   │
│                                                                 │
│  ↳ <script> ....................... ~7,000 lines of JS          │
│       const app = { … };  // single state object                │
│       Top-level helpers (handleFullFetch, showToast, etc.)      │
│       Backend bridge functions (_send_to_js targets)            │
└─────────────────────────────────────────────────────────────────┘
```

Anything position-fixed (modals, pills, drawers, toasts) is a sibling of `#cockpit` so it can escape `cockpit-main`'s `overflow: hidden`. Don't put fixed-position elements inside the cockpit.

---

## CSS map (line ranges drift; use the comment markers)

Sections are anchored by `/* ============== */` blocks. Search for the keyword:

| Block | Where to grep | Owns |
|---|---|---|
| Splash | `SPLASH SCREEN` | first-paint loader |
| Scrollbars | `GLOBAL SCROLLBARS` | dark Firefox + WebKit scrollbar tint |
| Cockpit shell | `COCKPIT SHELL` | `.cockpit` 2-col grid (rail + main) |
| Left rail | `LEFT RAIL` | rail items, badges, collapse animation |
| Library | `LIBRARY VIEW` / `library-grid` / `library-card` | library grid + cards + hidden/pinned badges |
| Queue | `queue-box` / `playlist-row` / `video-row` | queue cards (single + playlist + channel variants) |
| Player | `PLAYER VIEW` / `player-canvas` / `player-controls` | the in-app player (canvas, controls, side panel, error overlay, fullscreen) |
| Playlist detail | `Playlist detail view` | full-page hero + per-video rows |
| Modals | `modal-backdrop` / `confirm-modal` / `fix-modal` / `imp-` / `upd-` / `update-modal` / `settings-` | all overlay surfaces |
| Pills | `queue-new-pill` / `update-available-pill` / `offline-pill` | floating notifications |
| DnD overlay | `DRAG & DROP OVERLAY` | drop-zone overlay |

Same surface language across all overlays: `background: #141414`, `border: 1px solid #262626`, `box-shadow: 0 8px 24px rgba(0,0,0,0.5)`. Don't invent new colors — the pill redesign (see git history) shows what happens when surfaces drift apart.

---

## The `app` object

A single object literal at `<script>` top. Holds **all UI state** and most rendering methods. Method names are stable; line numbers drift.

### State (top of the object)

```js
videosInQueue       // Array of queue items (video or playlist)
videosInLibrary     // Array of library items (same shape)
librarySearchQuery  // Current search filter
currentView         // 'library' | 'queue' | 'player'
selectedVideoIds    // Set — videos picked in queue's URL bar checkbox
elements            // { libraryGrid: <div>, videoList: <div>, ... } — populated by mapElements()
currentFilter       // 'all' | 'videos' | 'playlists' | 'channels' | 'failed'
currentPlaylistId   // The playlist whose detail view is open (queue side)
```

### Init flow (`async init()`)

Order matters; don't reorder casually.

1. `mapElements()` — caches `getElementById` lookups by camelCase key.
2. Hydrate `window._showHiddenLibrary` from `get_setting('show_hidden_library')`.
3. Load queue from backend → `videosInQueue`. Clean stale "Downloading" status from prior session (mark as Cancelled).
4. Load library from backend → `videosInLibrary`.
5. Fire-and-forget `_checkForAppUpdates()` — pings `version.json`, shows pill if newer.
6. Apply `default_startup_view` setting — switches to queue if user picked it.
7. `addEventListeners()`, render queue + library, set up dashboard, kick frame-extraction poll.
8. `_checkOnboarding()` — shows onboarding modal on first launch only.

### Key render entry points

| Method | What it renders | Trigger |
|---|---|---|
| `renderLibrary()` | The library grid | After load_library, hide/unhide, pin/unpin, delete, search-input |
| `renderQueue()` | The queue list | After fetch, status update, drag reorder, filter chip change |
| `renderPlaylistDetail(pl)` | Queue's playlist hero + video rows | When user opens a playlist from queue |
| `buildDetailPanelHTML(item)` | Library's right-side detail sidebar | Single click on a library card |
| `updateDashboard()` | Filter chip counts + total bytes | After every queue mutation |

### Per-card render builders

| Builder | Returns | Used by |
|---|---|---|
| `createLibraryCardHTML(item)` | `<div class="library-card">…` | `renderLibrary()` |
| `createPlaylistItemHTML(p)` | `<div class="playlist-row">…` | `renderQueue()` (playlists) |
| `createVideoItemHTML(v)` | `<div class="video-row">…` | `renderQueue()` (single videos) |
| `createPdVideoRowHTML(v, idx)` | `<div class="pd-video-row">…` | `renderPlaylistDetail()` |

Every builder **must** pre-escape user-controlled fields (title, uploader, id) via `this.escapeHtml(...)`. See "XSS rules" below.

---

## Top-level (non-app) helpers

These live outside the `app` object because they're called as global functions — either by inline `onclick` handlers or by the backend via `_send_to_js`.

### Backend bridge — these are `_send_to_js` targets

| Function | Called from logic.py | Purpose |
|---|---|---|
| `handleFullFetch(videos, title, isPlaylist)` | `_handle_single_video_fetch` / `_handle_playlist_fetch` | Append fetched items to queue, render, scroll/pill |
| `finishFetch(msg)` | `_fetch_worker` (success or error) | Stops loading state; advances multi-URL batch; toasts on error |
| `updateItemThumbnailBatch(updates)` | `_start_thumbnail_caching_for_queue` | Bulk swap remote URLs → cached marker (~10 per batch) |
| `updateItemThumbnail(id, marker, playlistId, dataUrl)` | (legacy single-update path) | Single thumb swap |
| `updateItemStatus(id, status, ...)` | `_download_worker` (Done/Error/Cancelled/etc.) | Update card's status badge + trigger Rule A/B (see below) |
| `updateItemProgress(id, pct, speed, ...)` | `_download_worker` (per-tick) | Update progress bar on a card |
| `onVideoFormatsResolved(playlistId, payload)` | `_resolve_formats_worker` | Stamp formats on a playlist child |
| `onVideoFormatsFailed(playlistId, vid, msg)` | (same) | Mark failed format resolution |
| `onPlaylistFormatsComplete(playlistId)` | (same) | Triggers auto-download for `_update_target_id` temp playlists |
| `onUpdateCheckProgress(playlistId, n)` | `check_playlist_updates` (during yt-dlp lazy walk) | Live counter in the "Checking…" progress card |

### Detail-panel + library-card actions (called by inline `onclick`)

`openLibraryDetail`, `openLibraryItemDirect`, `playLibraryItem`, `openExternalLibraryItem`, `showDetailPanel`, `hideDetailPanel`, `buildDetailPanelHTML`, `fixMetadata`, `removeFromLibrary`, `deleteVideoFromDisk`, `revealItemInFolder`, `copyItemUrl`, `openItemOnYoutube`, `toggleHideLibraryItem`, `togglePinLibraryItem`, `checkPlaylistUpdates`, `openUpdatesModal`, `queuePlaylistUpdates`.

### Queue-card actions (called by inline `onclick`)

`pauseMainVideo`, `resumeMainVideo`, `openVideoFile`, `revealVideoFile`, `retryVideoDownload`, `toggleQualityMenu`, `toggleExtrasMenu`, `toggleExtra`, `pickQuality`, `pickPlaylistDefaultQuality`, `flipMenuIfNeeded`.

### Toasts + offline + utilities

`showToast(message, actionText, onAction)` — message goes through `.textContent`, action button uses `.textContent` — XSS-safe by construction. Replaces previous toast if one's already showing.

`showRetryToast`, `formatBytes`, `formatTime`, `errorCategoryLabel`, `escape` (local helpers in some functions).

---

## Major UI subsystems

### 1. Cockpit shell

```
.cockpit (display: grid; grid-template-columns: var(--rail-width) 1fr)
├─ .rail (collapsible sidebar)
│   ├─ rail header (logo + collapse btn)
│   ├─ .rail-nav (Library / Queue rail items)
│   └─ .rail-bottom (Settings rail item)
└─ .cockpit-main (position: relative; flex column)
    └─ .view-pane#library-view / #queue-view / #playlist-detail-view / #player-view
       (Only one has .active at a time → display: flex; others display: none)
```

Switching views: `app.switchView('library' | 'queue' | 'player')`. The player view is `position: absolute; inset: 0` over cockpit-main (z-index: 20) — it overlays the active pane rather than swapping it. Critical: don't add transforms or filters on `.cockpit` or its ancestors — that'd make `position: fixed` elements (modals, pills) compute against the cockpit instead of the viewport.

### 2. Library

`renderLibrary()` reads `videosInLibrary`, applies hide-filter, applies search-filter, sorts pinned-first by `pinned_at`, builds card HTML, swaps `innerHTML` of `.library-grid`. Selection mode (right-click or per-card checkbox) calls `Selection.refreshAfterRender()` after to re-apply `.is-selected` classes.

Cards have flags rendered as overlay badges:
- **NEW** (top-right): `added_at` within 48h, no engagement
- **Hidden** (top-right, slightly inset): `hidden: true`
- **Pinned** (top-left): `pinned: true` — for playlists, this shifts the Channel/Playlist badge down 26px so they don't overlap
- **Missing** (top-right): file no longer at `filepath`
- **Watch progress** (bottom strip): `last_position_seconds / last_duration_seconds`

Detail panel (`buildDetailPanelHTML`) is the right-side sidebar, **not** a separate view. It uses local `escape()` helper everywhere — its escaping is independent of `app.escapeHtml`.

### 3. Queue

Same shape as library but with status (Done/Downloading/Error/Cancelled/Paused/Retrying/Queued) and per-video quality picker. Multi-URL paste path lives in `fetch()` → `_fetchBatch()` → `_pumpFetchBatch()` (sequential calls; backend's `is_fetching` flag enforces serial).

Filter chips are `'all' | 'videos' | 'playlists' | 'channels' | 'failed'` — type-based, not status-based (status is per-card already). `matchesFilter(item, filter)` is the gate.

When user removes a queue item (`removeItem`), it cancels in-flight downloads first via `pywebview.api.cancel_download(id)` for each child. Don't forget this if you ever add another removal path.

### 4. Player

Single `<video>` element + custom controls overlay. Heavily commented in-place; see WebView2 landmines section in `CLAUDE.md` before changing CSS on `#player-video`, `.player-canvas`, or fullscreen handling.

Resume position pattern: `pendingResumeSeconds` is set in `open()` from backend, applied on `loadedmetadata`. `_videoEndedPinned` flag freezes the seek bar at 100% after natural end (Chromium fires a stray `currentTime=0` `timeupdate` after `ended`).

### 5. Settings drawer

`#settings-drawer` slides in from the right. Read-on-open + write-on-change pattern (see `openSettings()`):
- Pulls all values via `pywebview.api.get_setting(...)` + `get_about_info()`
- Hydrates each control
- Each control's change handler calls `pywebview.api.set_setting(key, value)` immediately — no Save button

To add a new setting:
1. Add a `.settings-row` to the markup with a unique `id`
2. In `openSettings()`, `await pywebview.api.get_setting('your_key')` and set the control's value
3. In the listeners IIFE near the bottom, wire the `change` event to `pywebview.api.set_setting('your_key', e.target.value)`
4. If the setting needs to be honored at boot, add a hydration step in `app.init()`

---

## Modals + pills (overlay catalog)

| Element | Purpose | Backdrop dismissable? |
|---|---|---|
| `#fix-modal-backdrop` | Manual metadata fix (paste correct URL) | Yes |
| `#confirm-modal-backdrop` | Generic destructive confirm (delete) | Yes (via JS) |
| `#imp-backdrop` | Import-from-folder file picker | Yes |
| `#upd-backdrop` | "Check for updates" — pick which new videos to queue | Yes |
| `#update-modal-backdrop` | App-update modal (release notes + Download) | Yes |
| `#settings-backdrop` + `#settings-drawer` | Settings drawer | Yes |
| `#onb-backdrop` | First-launch onboarding | Get-started only (no backdrop dismiss) |
| `.queue-new-pill` (`#queue-new-pill`) | "↓ N new" — items landed while user scrolled up | Click body → scroll to bottom; auto-hides when user scrolls to bottom |
| `.update-available-pill` (`#update-available-pill`) | App-update available | Click body → opens update modal; X dismisses for session |
| Offline pill (`#offline-pill`) | Network down indicator | Auto-shown/hidden by `online` / `offline` events |
| `.toast` (`#toast-container > .toast`) | Transient feedback | Auto-dismiss after 4s (8s if action button) |

z-index ladder: pills < drawers < modals < toasts (~800 < 9200 < 9300 < higher). Don't fight it; new overlays should fit in the existing ladder, not introduce a new tier.

---

## Critical conventions (read before editing)

### XSS rules

`logic.py` returns yt-dlp data verbatim — titles, uploader names, descriptions, etc. **All of it is user-controlled** (a malicious YouTube title is a real attack surface). Two rules:

1. **Never interpolate raw strings into `innerHTML` template literals.** Pre-escape via `this.escapeHtml(s)` (inside `app`) or local `escape(s)` helper.
2. **`textContent` is XSS-safe by construction** — prefer it when you can. `showToast`, `confirmDialog`, settings field hydration all use textContent.

The pattern that exists across builders:
```js
const eTitle = this.escapeHtml(item.title || '');
return `<div class="card">${eTitle}</div>`;
```

If you add a new render path, copy this pattern. Don't trust that "just YouTube IDs" are safe — imported-from-disk entries used to have folder names baked into IDs (now sanitized at the source via `_classify_playlist_url` + the import path).

### Backend bridge etiquette

`_send_to_js` is a synchronous `evaluate_js` call on the WebView2 bridge. Each one takes 5-50ms. **Don't fire one per item in a loop** — batch instead (see `updateItemThumbnailBatch` for the pattern).

### Layout-during-resize

WebView2 blanks the video pipeline if you animate `grid-template-columns` while the OS window is resizing (e.g., during fullscreen toggle). The `transition: none` on `.cockpit` inside `body.player-is-fullscreen` is **not optional**. Same goes for `display: none` on `.rail` during fullscreen — keep the rail in flow, just collapse its grid column to 0.

### Scroll containers + `position: fixed`

Anything `position: fixed` is anchored to the viewport. Don't put a `transform`, `filter`, or `will-change` on any ancestor — they make the ancestor a containing block for fixed positioning, which breaks pills and modals.

### Stable IDs

`item-${id}` is the convention for queue + library cards. The id used must be the **escaped** form (the same `escapeHtml(item.id)` you wrote into the markup), so `getElementById` lookups match. For YouTube IDs (alphanumeric) escape is a no-op; for legacy imported IDs that contained special chars, the source has been sanitized — but any new ID source should go through the same sanitizer (`re.sub(r'[^A-Za-z0-9_-]', '_', name)` in Python).

---

## "Where do I add…" cookbook

### A new setting

1. Markup: add a `.settings-row` inside the relevant `.settings-section` (Downloads / Defaults / About). Pick a control type that already has CSS (`.settings-picker`, `.settings-toggle`, `.settings-slider`).
2. `openSettings()`: `await pywebview.api.get_setting('key')` and hydrate the control.
3. Listeners IIFE: `addEventListener('change', e => pywebview.api.set_setting('key', e.target.value))`.
4. If it needs boot-time effect: hydrate in `app.init()`.
5. No Python changes needed — the existing `get_setting` / `set_setting` cover any key.

### A new modal

1. Add `<div class="my-modal-backdrop" id="..." hidden> … </div>` as a sibling of `#cockpit`.
2. Style it with the toast/confirm-modal language (`#141414` base, `#262626` border, soft drop shadow).
3. Open: `removeAttribute('hidden')`. Close: `setAttribute('hidden', '')`. ESC handler + backdrop-click dismiss (see `wireUpdateModal` IIFE for the pattern).

### A new card type for the library

1. Inside `createLibraryCardHTML`, add a branch (`if (item.type === 'whatever')`) that returns the new HTML.
2. Pre-escape every user-controlled field via `this.escapeHtml`.
3. If the card needs a click action that isn't `openLibraryDetail`, add a top-level handler function and call it via `onclick="..."` — but prefer dataset + delegated listener for new code.

### A new backend → frontend event

1. In `logic.py`: `self._send_to_js('myEventName', arg1, arg2)`.
2. In `index.html`: define `function myEventName(arg1, arg2) { ... }` at the script top level (not inside `app`). It must be a global function for `evaluate_js` to find it.
3. Either delegate into `app.someMethod(...)` or do the work in the global function directly.

### A new icon for landing-page features

(Different file — `protube-landing/index.html`.) Add an entry to the `ICONS` object in that file's `<script>`, then reference it by name from `config.json`'s `features[].icon`.

---

## Things that look weird but are intentional

- **`escapeHtml` is duplicated in three places**: once on `app`, once as a local `escape` inside `buildDetailPanelHTML`, once in the player side panel. Each is intentional — keeps the function close to its render block. Don't refactor them into a single shared helper without reading why.
- **`.queue-empty` and `.video-list-container` were both `flex: 1`**, causing the empty state to sit in the lower half. Fix: `renderQueue` now sets `listEl.style.display = 'none'` when showing the empty state. Same pattern for `.library-grid` / `.library-empty`.
- **`scrollbar-gutter: stable` on `.library-grid`** — without it, toggling Show Hidden between few-cards (no scrollbar) and many-cards (scrollbar) reflows horizontally by ~6-15px.
- **Multi-instance mutex in `main.py`** prevents two ProTube processes corrupting `settings.json` via concurrent writes. If you ever launch multiple windows intentionally, you'll need to relax this.
- **Window state for the new-items pill** uses `window._pillStickyUntil` (a timestamp) so render-triggered scroll events don't auto-hide the pill in the 800ms after it appears.

---

## Quick orientation for a future Claude session

If you've never touched this file before:

1. Read **CLAUDE.md** at the project root first — it has the WebView2 quirks list and the build/run commands.
2. Skim this map to learn the section layout.
3. When changing UI: find the section by grepping for the `/* ============== */` comment header, then edit in place.
4. When adding a new render path: copy from the closest existing one (e.g., copy `createLibraryCardHTML` for a new card variant, copy `openUpdatesModal` for a new modal flow).
5. After every JS edit, run the parse check:
   ```
   node -e "const html=require('fs').readFileSync('src/index.html','utf-8'); const m=html.match(/<script>([\\s\\S]*?)<\\/script>/g); for (const s of m) { const b=s.slice(8,-9); if(b.length<100) continue; try { new Function(b); } catch(e){console.log('ERR:',e.message); process.exit(1);} } console.log('JS OK');"
   ```
6. After every CSS edit: open the app via VBS (`pythonw src/main.py`) and visually verify. There's no test suite.
