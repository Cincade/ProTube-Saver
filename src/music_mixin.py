"""Music domain mixin — YT Music search, download queue, library, and albums.

Consumed by logic.API via multiple inheritance.  All methods land on the same
`self` as the rest of the API, so cross-domain calls like
self._store.set()/self._send_to_js()/self._find_ffmpeg_exe() resolve normally.
"""

import os
import re
import json
import shutil
import threading
import time
import subprocess

import requests

from ydl_utils import YoutubeDL, _MusicDownloadCancelled


class MusicMixin:
    """Music search, download queue, library, and album management."""

    def _get_music_innertube_session(self):
        """Lazy persistent session for YT Music Innertube calls. Reuses connections."""
        if self._MUSIC_INNERTUBE_SESSION is None:
            s = requests.Session()
            s.headers.update({
                'Content-Type': 'application/json',
                'User-Agent': 'Mozilla/5.0',
                'Origin': 'https://music.youtube.com',
                'Referer': 'https://music.youtube.com/',
            })
            self._MUSIC_INNERTUBE_SESSION = s
        return self._MUSIC_INNERTUBE_SESSION

    def record_music_search(self, query):
        """Append a query to the recent-searches list (most recent first, capped at 12)."""
        try:
            q = (query or '').strip()
            if not q:
                return {'ok': True}
            # Atomic read-modify-write: read the list, prepend, trim, and persist
            # all under one lock so two rapid searches can't clobber each other.
            with self._store.mutate() as s:
                lst = s.get('music_recent_searches', []) or []
                lst = [x for x in lst if x.lower() != q.lower()]
                lst.insert(0, q)
                s['music_recent_searches'] = lst[:12]
            return {'ok': True}
        except Exception as e:
            return {'error': str(e)}

    def record_video_search(self, query):
        """Same as record_music_search but for the video Search tab."""
        try:
            q = (query or '').strip()
            if not q:
                return {'ok': True}
            with self._store.mutate() as s:
                lst = s.get('video_recent_searches', []) or []
                lst = [x for x in lst if x.lower() != q.lower()]
                lst.insert(0, q)
                s['video_recent_searches'] = lst[:12]
            return {'ok': True}
        except Exception as e:
            return {'error': str(e)}

    def get_video_for_you(self):
        """Search-landing data. User-requested redesign: NO generic recommendations
        (trending / shuffled / "pick up where you left off") and no infinite scroll —
        just the user's own recent searches plus up to 3 recommendation shelves
        seeded by their LIBRARY (top channels) and recent searches.

          {
            recent_searches: [str, ...]   # cap 8, each removable
            shelves: [ {title, kind:'channel'|'search', seed, items:[search-result,...]}, ... ]  # <=3
          }
        Shelf items are search-result videos (carry view_count_string + published_time),
        library dupes filtered, cached 24h per seed."""
        result = {'recent_searches': [], 'recommendations': []}
        try:
            recents = (self.settings.get('video_recent_searches', []) or [])[:8]
            result['recent_searches'] = recents

            lib = list(self.settings.get('library', []) or [])
            dedup = self._build_dedup_sets()
            lib_ids = dedup.get('library_ids', set()) if isinstance(dedup, dict) else set()

            # Seeds: top library channels (by how many of their videos you've saved),
            # then recent searches to round it out.
            counts, order = {}, []
            for v in lib:
                u = (v.get('uploader') or '').strip()
                if u:
                    if u not in counts:
                        order.append(u)
                    counts[u] = counts.get(u, 0) + 1
            top_channels = sorted(order, key=lambda u: counts[u], reverse=True)[:3]
            seeds = list(top_channels)
            for q in recents:
                if len(seeds) >= 4:
                    break
                if q and q not in seeds:
                    seeds.append(q)

            # Interleave each seed's results into ONE "Recommended for you" grid
            # (~2-3 rows of library-style cards), library dupes filtered, deduped
            # across seeds.
            import itertools
            pools = [self._for_you_shelf_items(s, lib_ids) for s in seeds]
            recs, seen = [], set()
            for group in itertools.zip_longest(*pools):
                for it in group:
                    if it and it.get('id') and it['id'] not in seen:
                        seen.add(it['id'])
                        recs.append(it)
                if len(recs) >= 12:
                    break
            result['recommendations'] = recs[:12]
            return result
        except Exception as e:
            print(f'[ProTube] video for-you build failed: {e}')
            return result

    def _for_you_shelf_items(self, seed, lib_ids):
        """Up to ~10 search-result videos for a landing shelf seed, library dupes
        filtered out, cached 24h per seed so the landing doesn't re-hit Innertube
        on every mount."""
        cached_key = f'video_fy_shelf_{seed}'
        cached = self.settings.get(cached_key)
        if cached and (int(time.time()) - cached.get('at', 0)) < 86400:
            return cached.get('items', [])
        try:
            sr = self.search_youtube(seed, count=15, kind='videos')
            items = (sr or {}).get('results', []) or []
            items = [i for i in items if isinstance(i, dict) and i.get('type') == 'video'
                     and i.get('id') not in lib_ids][:10]
            self._store.set(cached_key, {'at': int(time.time()), 'items': items})
            return items
        except Exception:
            return []

    def remove_video_recent_search(self, query):
        """Remove a single recent search query (the user can't clear individual
        chips otherwise)."""
        try:
            cur = self.settings.get('video_recent_searches', []) or []
            ql = (query or '').strip().lower()
            self._store.set('video_recent_searches', [q for q in cur if (q or '').strip().lower() != ql])
            return {'ok': True}
        except Exception as e:
            return {'error': str(e)}

    def _fetch_yt_trending(self):
        """Hit YouTube's trending feed (browseId=FEtrending) via Innertube
        and pluck 6 trending videos. Mirrors _fetch_yt_music_trending."""
        body = {
            'context': {'client': dict(self._INNERTUBE_CLIENT)},
            'browseId': 'FEtrending',
        }
        sess = self._get_innertube_session()
        try:
            resp = sess.post('https://www.youtube.com/youtubei/v1/browse',
                             params={'prettyPrint': 'false'}, json=body, timeout=15)
        except requests.RequestException:
            return []
        if resp.status_code != 200:
            return []
        try:
            data = resp.json()
        except ValueError:
            return []
        # Walk the response for any videoRenderer items (the trending tab
        # nests them several levels deep under sectionListRenderer →
        # itemSectionRenderer → contents → shelfRenderer → content →
        # expandedShelfContentsRenderer → items; recurse to keep it simple).
        items = []
        dedup = self._build_dedup_sets()
        def walk(node):
            if not isinstance(node, (dict, list)) or len(items) >= 6:
                return
            if isinstance(node, dict):
                if 'videoRenderer' in node:
                    parsed = self._innertube_parse_item({'videoRenderer': node['videoRenderer']}, dedup)
                    if parsed and parsed.get('type') == 'video':
                        if not any(p.get('id') == parsed.get('id') for p in items):
                            items.append(parsed)
                    return
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(data)
        return items[:6]

    def clear_video_recent_searches(self):
        try:
            self._store.set('video_recent_searches', [])
            return {'ok': True}
        except Exception as e:
            return {'error': str(e)}

    def get_music_for_you(self):
        """Build the 'For You' search-empty landing rows. Returns:
          {
            recent_searches: [str, ...]      # cap 8
            recent_library:  [track, ...]    # last 8 added
            top_artist:      str|None        # most-represented artist in library
            because_you:     [search-result, ...]  # 6 tracks by top_artist (cached)
            trending:        [search-result, ...]  # 6 trending from YT Music charts
            shuffled_lib:    [track, ...]    # 6 random library tracks
          }
        All sub-arrays may be empty. Frontend should hide empty rows.
        """
        import random
        result = {
            'recent_searches': [],
            'recent_library': [],
            'top_artist': None,
            'because_you': [],
            'trending': [],
            'shuffled_lib': [],
        }
        try:
            # Recent searches
            result['recent_searches'] = (self.settings.get('music_recent_searches', []) or [])[:8]

            # Library-derived rows
            lib = list(self.settings.get('music_library', []) or [])
            if lib:
                # Last 8 added
                lib_sorted = sorted(lib, key=lambda t: t.get('added_at', 0), reverse=True)
                result['recent_library'] = lib_sorted[:8]
                # Top artist by track count
                counts = {}
                for t in lib:
                    a = (t.get('artist') or '').strip()
                    if a: counts[a] = counts.get(a, 0) + 1
                if counts:
                    top = max(counts.items(), key=lambda kv: kv[1])
                    result['top_artist'] = top[0]
                # Shuffled sample (6, excluding the top-artist tracks to feel different)
                pool = [t for t in lib if (t.get('artist') or '') != result['top_artist']]
                if len(pool) < 6:
                    pool = lib
                random.shuffle(pool)
                result['shuffled_lib'] = pool[:6]

            # "Because you have [Top Artist]" — search YT Music for that artist's songs
            if result['top_artist']:
                cached_key = f'music_because_you_cache_{result["top_artist"]}'
                cached = self.settings.get(cached_key)
                # Cache for 24h
                if cached and (int(time.time()) - cached.get('at', 0)) < 86400:
                    result['because_you'] = cached.get('items', [])
                else:
                    try:
                        sr = self.search_youtube_music(result['top_artist'], kind='songs')
                        items = (sr or {}).get('results', [])[:6]
                        # Filter out items already in library
                        lib_ids = self._build_music_library_id_set()
                        items = [i for i in items if i.get('id') not in lib_ids]
                        result['because_you'] = items[:6]
                        self._store.set(cached_key, {'at': int(time.time()), 'items': result['because_you']})
                    except Exception:
                        pass

            # Trending — YT Music charts via Innertube. Cache 24h.
            cached_trend = self.settings.get('music_trending_cache')
            if cached_trend and (int(time.time()) - cached_trend.get('at', 0)) < 86400:
                result['trending'] = cached_trend.get('items', [])
            else:
                try:
                    result['trending'] = self._fetch_yt_music_trending()
                    self._store.set('music_trending_cache', {'at': int(time.time()), 'items': result['trending']})
                except Exception:
                    pass

            return result
        except Exception as e:
            print(f'[ProTube] for-you build failed: {e}')
            return result

    def _fetch_yt_music_trending(self):
        """Hit YT Music's charts browse endpoint and pluck 6 trending songs."""
        body = {
            'context': {'client': dict(self._MUSIC_INNERTUBE_CLIENT)},
            'browseId': 'FEmusic_charts',
        }
        sess = self._get_music_innertube_session()
        resp = sess.post('https://music.youtube.com/youtubei/v1/browse',
                         params={'prettyPrint': 'false'}, json=body, timeout=15)
        if resp.status_code != 200:
            return []
        data = resp.json()
        # Walk the response for any musicResponsiveListItemRenderer items.
        items = []
        lib_ids = self._build_music_library_id_set()
        def walk(node):
            if not isinstance(node, (dict, list)) or len(items) >= 6:
                return
            if isinstance(node, dict):
                if 'musicResponsiveListItemRenderer' in node:
                    parsed = self._parse_music_shelf_item(node['musicResponsiveListItemRenderer'], 'songs', lib_ids)
                    if parsed and parsed.get('kind') == 'song':
                        items.append(parsed)
                        return
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(data)
        return items[:6]

    def search_youtube_music(self, query, kind='songs'):
        """Search YouTube Music and return music-shaped results.

        kind: 'songs' | 'videos' | 'albums' | 'artists' | 'playlists' (default 'songs')

        Each result for kind='songs':
          { id, type='music', kind='song', title, artist, album, duration_string,
            play_count, thumbnail, url, in_library }

        For other kinds, returned fields shift to match the type. The frontend can
        adapt the card layout per result.kind.
        """
        try:
            q = (query or '').strip()
            if not q:
                return {'results': [], 'kind': kind}

            body = {
                'context': {'client': dict(self._MUSIC_INNERTUBE_CLIENT)},
                'query': q,
            }
            if kind in self._MUSIC_INNERTUBE_FILTER_PARAMS:
                body['params'] = self._MUSIC_INNERTUBE_FILTER_PARAMS[kind].replace('%3D', '=')

            sess = self._get_music_innertube_session()
            try:
                resp = sess.post(
                    self._MUSIC_INNERTUBE_URL,
                    params={'prettyPrint': 'false'},
                    json=body,
                    timeout=15,
                )
            except requests.RequestException as exc:
                return {'error': f'Network error: {exc}'}
            if resp.status_code != 200:
                return {'error': f'YT Music HTTP {resp.status_code}'}
            try:
                data = resp.json()
            except ValueError:
                return {'error': 'YT Music returned non-JSON'}

            # Walk YT Music's response: tabbedSearchResultsRenderer → tabs[0] → tabRenderer
            # → content.sectionListRenderer.contents[] → musicShelfRenderer.contents[].
            tabs = (data.get('contents', {})
                        .get('tabbedSearchResultsRenderer', {})
                        .get('tabs', []) or [])
            if not tabs:
                return {'results': [], 'kind': kind}
            sections = (tabs[0].get('tabRenderer', {})
                              .get('content', {})
                              .get('sectionListRenderer', {})
                              .get('contents', []) or [])

            music_lib_ids = self._build_music_library_id_set()
            results = []
            continuation = ''
            for s in sections:
                shelf = s.get('musicShelfRenderer')
                if not shelf:
                    continue
                for c in (shelf.get('contents') or []):
                    item = c.get('musicResponsiveListItemRenderer')
                    if not item:
                        continue
                    parsed = self._parse_music_shelf_item(item, kind, music_lib_ids)
                    if parsed:
                        results.append(parsed)
                # YT Music puts the "next page" token on the shelf itself. Only
                # the FIRST shelf we hit gets its continuation captured (other
                # shelves would represent a different kind, which we don't mix
                # across pages).
                if not continuation:
                    continuation = self._extract_music_continuation(shelf)

            return {'results': results, 'kind': kind, 'count': len(results),
                    'continuation': continuation}
        except Exception as exc:
            print(f'[ProTube] music search failed: {exc}')
            return {'error': f'Music search failed: {exc}'}

    @staticmethod
    def _extract_music_continuation(shelf):
        """Pull the continuation token out of a musicShelfRenderer or
        musicShelfContinuation. Returns '' when there's no next page."""
        try:
            conts = shelf.get('continuations') or []
            for c in conts:
                tok = (c.get('nextContinuationData') or {}).get('continuation')
                if tok:
                    return tok
                # InnerTube sometimes wraps the same token under reloadContinuationData
                tok = (c.get('reloadContinuationData') or {}).get('continuation')
                if tok:
                    return tok
        except Exception:
            pass
        return ''

    def search_youtube_music_continuation(self, continuation, kind='songs'):
        """Fetch the next page of music search results. Frontend calls this
        when the user scrolls near the bottom of the results list. Returns
        the same shape as search_youtube_music — { results, kind, continuation }.
        Empty continuation in the response means we've hit the end."""
        try:
            tok = (continuation or '').strip()
            if not tok:
                return {'results': [], 'kind': kind, 'continuation': ''}
            body = {
                'context': {'client': dict(self._MUSIC_INNERTUBE_CLIENT)},
            }
            sess = self._get_music_innertube_session()
            try:
                resp = sess.post(
                    self._MUSIC_INNERTUBE_URL,
                    params={'prettyPrint': 'false', 'continuation': tok},
                    json=body,
                    timeout=15,
                )
            except requests.RequestException as exc:
                return {'error': f'Network error: {exc}'}
            if resp.status_code != 200:
                return {'error': f'YT Music HTTP {resp.status_code}'}
            try:
                data = resp.json()
            except ValueError:
                return {'error': 'YT Music returned non-JSON'}

            # Continuation response: continuationContents.musicShelfContinuation
            cont = (data.get('continuationContents') or {}).get('musicShelfContinuation') or {}
            music_lib_ids = self._build_music_library_id_set()
            results = []
            for c in (cont.get('contents') or []):
                item = c.get('musicResponsiveListItemRenderer')
                if not item:
                    continue
                parsed = self._parse_music_shelf_item(item, kind, music_lib_ids)
                if parsed:
                    results.append(parsed)
            next_token = self._extract_music_continuation(cont)
            return {'results': results, 'kind': kind, 'count': len(results),
                    'continuation': next_token}
        except Exception as exc:
            print(f'[ProTube] music search continuation failed: {exc}')
            return {'error': f'Music search continuation failed: {exc}'}

    def _parse_music_shelf_item(self, mrlir, kind, lib_ids):
        """Convert a musicResponsiveListItemRenderer into a clean result dict.

        Column layout varies by kind:
          - songs:    [Title, Artist • Album • Duration, Play count]
          - videos:   [Title, Channel • Views • Duration]
          - albums:   [Title, Album • Artist • Year]
          - artists:  [Name, Subscribers]
          - playlists:[Title, Playlist • Author • TrackCount]
        We pull text out of each flex column, then map to fields based on kind.
        """
        cols = mrlir.get('flexColumns') or []
        col_texts = []
        for col in cols:
            t = col.get('musicResponsiveListItemFlexColumnRenderer', {}).get('text', {})
            runs = t.get('runs') or []
            # Join all run texts (skips the " • " separators which are runs themselves)
            col_texts.append(''.join(r.get('text', '') for r in runs).strip())

        # Track ID lives in playlistItemData.videoId for songs/videos.
        playlist_item = mrlir.get('playlistItemData') or {}
        video_id = playlist_item.get('videoId') or ''

        # Thumbnail
        thumbs = (mrlir.get('thumbnail') or {}).get('musicThumbnailRenderer', {}) \
                 .get('thumbnail', {}).get('thumbnails', []) or []
        thumb = thumbs[-1].get('url') if thumbs else ''

        title = col_texts[0] if col_texts else ''
        if not title:
            return None

        # Second column is the "Artist • Album • Duration" line; we split on " • ".
        meta_bits = []
        if len(col_texts) > 1:
            meta_bits = [b.strip() for b in col_texts[1].split('•') if b.strip()]

        if kind in ('songs', 'videos'):
            if not video_id:
                return None
            artist = meta_bits[0] if meta_bits else ''
            duration = meta_bits[-1] if len(meta_bits) >= 2 else ''
            album = meta_bits[1] if len(meta_bits) >= 3 else ''  # songs only — videos don't have an album
            play_count = col_texts[2] if len(col_texts) > 2 else ''
            return {
                'id': video_id,
                'type': 'music',
                'kind': 'song' if kind == 'songs' else 'video',
                'title': title,
                'artist': artist,
                'album': album,
                'duration_string': duration,
                'play_count': play_count,
                'thumbnail': thumb,
                'url': f'https://music.youtube.com/watch?v={video_id}',
                'in_library': video_id in lib_ids,
            }

        # albums / artists / playlists — for v1 we only return the basics so the
        # frontend can render the card; clicking these opens the YT Music URL.
        nav = mrlir.get('navigationEndpoint', {}) or {}
        browse = (nav.get('browseEndpoint') or {})
        browse_id = browse.get('browseId') or ''
        return {
            'id': browse_id,
            'type': 'music',
            'kind': kind[:-1],   # 'albums' → 'album', etc.
            'title': title,
            'subtitle': col_texts[1] if len(col_texts) > 1 else '',
            'thumbnail': thumb,
            'url': f'https://music.youtube.com/browse/{browse_id}' if browse_id else '',
            'in_library': False,
        }

    def _build_music_library_id_set(self):
        """Snapshot the music_library's video IDs for cross-checking search results."""
        ids = set()
        for t in (self.settings.get('music_library', []) or []):
            if t.get('id'):
                ids.add(t['id'])
        return ids

    # ---- Music download + library ----------------------------------------------------

    @staticmethod
    def _strip_collection_prefix(title):
        """Strip the leading 'Album – ' / 'Single – ' / 'EP – ' / 'Playlist – '
        / 'Soundtrack – ' / 'Mixtape – ' classifier that YT Music's browse
        endpoint puts on collection titles. Without this, the library shows
        cards like 'Album – ICEMAN' instead of just 'ICEMAN'.
        Handles em-dash, en-dash, and hyphen variants because YT Music uses
        different separators across regions."""
        if not title:
            return title
        import re
        return re.sub(
            r'^(Album|Single|EP|Playlist|Soundtrack|Mixtape|Compilation)\s*[–—\-]\s*',
            '', title, flags=re.IGNORECASE,
        )

    @staticmethod
    def _resolve_album_artist(info):
        """Resolve an album's artist. YT Music sometimes leaves the top-level
        `artist`/`uploader` fields empty on browse pages, so we also walk the
        playlist's child entries and pick the most common artist (excluding
        'Various Artists' style placeholders). Fall back chain:
        info-level artist/creator/uploader/channel → entry-level most-common
        artist → playlist_uploader → 'Unknown Artist'.
        Without the ' - Topic' strip, Drake's album would read 'Drake - Topic'."""
        def _strip_topic(v):
            v = (v or '').strip()
            if v.endswith(' - Topic'):
                v = v[:-len(' - Topic')]
            return v
        for key in ('artist', 'creator'):
            v = info.get(key)
            if v:
                return v
        for key in ('uploader', 'channel'):
            v = _strip_topic(info.get(key))
            if v:
                return v
        # Walk the album's tracks — most YT Music album tracks carry artist.
        entries = info.get('entries') or []
        from collections import Counter
        counts = Counter()
        for e in entries:
            if not isinstance(e, dict):
                continue
            a = e.get('artist') or e.get('creator') or _strip_topic(e.get('uploader') or e.get('channel'))
            if a and a.lower() not in ('various artists', 'various', ''):
                counts[a] += 1
        if counts:
            return counts.most_common(1)[0][0]
        # Last-ditch: playlist_uploader from the YT extractor
        v = _strip_topic(info.get('playlist_uploader'))
        if v:
            return v
        return 'Unknown Artist'

    @staticmethod
    def _sanitize_path_segment(s, fallback='Unknown'):
        """Strip filesystem-unsafe characters from a string so we can use it as a
        path segment. Keeps Unicode letters/digits, replaces the Windows-forbidden
        set (`<>:"/\\|?*`) and trims trailing dots/spaces (also forbidden on Windows)."""
        import re
        s = (s or '').strip()
        if not s:
            return fallback
        # Replace forbidden chars with a space, collapse runs of whitespace.
        s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', ' ', s)
        s = re.sub(r'\s+', ' ', s).strip()
        # Trim trailing dots/spaces (Windows weirdness)
        s = s.rstrip('. ')
        # Cap length to keep paths sane (Windows MAX_PATH = 260 total).
        if len(s) > 80:
            s = s[:80].rstrip('. ')
        return s or fallback

    def add_music_track(self, video_id_or_url):
        """Enqueue a single track for background download. The queue processor
        promotes it to 'downloading' up to `max_concurrent_music_downloads` at a
        time and runs `_music_download_worker`. Progress flows through both the
        per-track `updateMusicDownload` event AND the queue-wide
        `updateMusicQueue` event."""
        try:
            url = video_id_or_url
            if not url.startswith('http'):
                url = f'https://music.youtube.com/watch?v={video_id_or_url}'
            # Cheap dedup — already in library? (only catches the bare-id case)
            lib_ids = self._build_music_library_id_set()
            if video_id_or_url in lib_ids:
                return {'already_in_library': True}
            # Resolve a stable id for the queue entry. If the caller passed an
            # 11-char video id, use it; otherwise try to extract `v=<id>` from
            # the URL. Falls back to '' if neither yields one (rare — the
            # worker will still process the URL but cancel won't work).
            if len(video_id_or_url) == 11 and not video_id_or_url.startswith('http'):
                track_id = video_id_or_url
            else:
                import re as _re
                m = _re.search(r'[?&]v=([A-Za-z0-9_-]{11})', url)
                track_id = m.group(1) if m else ''
            with self._music_queue_lock:
                q = self.settings.get('music_queue', []) or []
                for entry in q:
                    if entry.get('id') == track_id and entry.get('status') in ('queued', 'downloading'):
                        return {'already_queued': True, 'id': track_id}
                # Quick partial entry — title/artist will be filled in by the worker
                # after it does the metadata probe. We don't block to extract here
                # because the user just clicked '+' and wants instant feedback.
                entry = {
                    'id': track_id,
                    'title': '',
                    'artist': '',
                    'album': '',
                    'album_id': '',
                    'thumbnail': '',
                    'url': url,
                    'status': 'queued',
                    'progress': 0,
                    'queued_at': int(time.time()),
                    'started_at': None,
                    'completed_at': None,
                    'error': None,
                }
                q.append(entry)
                self._store.set('music_queue', q)
                queue_len = sum(1 for e in q if e.get('status') in ('queued', 'downloading'))
            self._music_queue_wake()
            self._emit_music_queue()
            return {'queued': True, 'id': track_id, 'queue_len': queue_len}
        except Exception as e:
            return {'error': str(e)}

    def add_music_collection(self, browse_id_or_url, kind=None):
        """Bulk-download a YT Music album / playlist / artist's top tracks.

        Resolves the browse/playlist ID to a list of video IDs via yt-dlp's flat
        extraction, then queues each one through `add_music_track`. Fire-and-forget.
        kind: 'album' | 'playlist' | 'artist' — only used to label progress toasts.
        For kind='album', the resolved collection is also persisted as a first-class
        `music_albums` entry with aggregate progress (track grouping in the library).
        """
        try:
            url = browse_id_or_url
            collection_id = browse_id_or_url if not browse_id_or_url.startswith('http') else ''
            if not url.startswith('http'):
                # YT Music browseIds: albums (MPREb_), artists (UC), playlists (VLPLor RDCLAK5)
                if url.startswith('MPREb_') or url.startswith('OLAK5uy_'):
                    url = f'https://music.youtube.com/browse/{url}'
                elif url.startswith('UC'):
                    url = f'https://music.youtube.com/channel/{url}'
                elif url.startswith('VL') or url.startswith('PL') or url.startswith('RD'):
                    pid = url[2:] if url.startswith('VL') else url
                    url = f'https://music.youtube.com/playlist?list={pid}'
                else:
                    url = f'https://music.youtube.com/browse/{url}'
            threading.Thread(
                target=self._music_collection_worker,
                args=(url, kind or 'collection', collection_id),
                daemon=True,
            ).start()
            return {'ok': True, 'started': True}
        except Exception as e:
            return {'error': str(e)}

    def _music_collection_worker(self, url, kind, collection_id=''):
        """Resolve a YT Music collection URL → list of video IDs, then download each.

        For kind='album', also writes a `music_albums` entry up-front so the library
        grid can show a single album card with aggregate progress instead of N
        independent track rows.
        """
        try:
            self._send_to_js('showToast', f'Resolving {kind}…', None, None)
            opts = self._get_ydl_opts('browser', 'none')
            opts.update({
                'quiet': True, 'no_warnings': True,
                'skip_download': True,
                'extract_flat': 'in_playlist',
            })
            info = self._ydl_extract(url, opts)
            entries = info.get('entries') or []
            # Some YT Music browse responses wrap tracks under a "Songs" tab; flat-extract
            # exposes them as nested entries with 'entries' key in each parent. Flatten.
            flat = []
            for e in entries:
                if not e:
                    continue
                if isinstance(e, dict) and e.get('entries'):
                    flat.extend(x for x in e['entries'] if x)
                else:
                    flat.append(e)
            video_ids = []
            for e in flat:
                vid = e.get('id') if isinstance(e, dict) else None
                if vid and len(vid) == 11:   # YT video IDs are 11 chars
                    video_ids.append(vid)
            if not video_ids:
                self._send_to_js('showToast', f'No tracks found in this {kind}.', None, None)
                # Also clear any search-row spinner waiting on this collection.
                try:
                    self._send_to_js(
                        'updateMusicCollectionResolveError',
                        collection_id or '',
                        f'No tracks found in this {kind}.',
                    )
                except Exception:
                    pass
                return
            # Cap artist downloads at 25 tracks so we don't accidentally grab a whole channel
            if kind == 'artist' and len(video_ids) > 25:
                video_ids = video_ids[:25]

            # -- Album entity: persist BEFORE spawning workers so the library grid
            # can pick up the in-progress album card immediately. --
            album_id = ''
            if kind == 'album':
                # Resolve a stable album_id. Prefer the original browse id (MPREb_/OLAK5uy_),
                # otherwise yt-dlp's own id field, otherwise hash the URL.
                album_id = collection_id or (info.get('id') or '') or ''
                if not album_id:
                    import hashlib as _hashlib
                    album_id = 'AL' + _hashlib.md5(url.encode('utf-8')).hexdigest()[:14]
                # YT Music browse pages return titles like 'Album – ICEMAN' /
                # 'Single – ...' / 'Playlist – ...' / 'EP – ...'. The classifier
                # prefix is redundant inside the app and noisy in the UI — strip it
                # so the user sees just the album name.
                raw_title = info.get('title') or info.get('album') or 'Untitled Album'
                album_title = self._strip_collection_prefix(raw_title)
                # Artist resolution: YT Music sets `artist` directly on the info
                # dict for album pages. Fall back to uploader (with the auto-channel
                # " - Topic" suffix stripped — that suffix is how YT Music names
                # its per-artist topic channels and looks ugly in the UI).
                album_artist = self._resolve_album_artist(info)
                # Best cover candidate: largest thumbnail in the info dict.
                cover_url = ''
                thumbs = info.get('thumbnails') or []
                if thumbs and isinstance(thumbs, list):
                    try:
                        best = max(
                            (t for t in thumbs if isinstance(t, dict) and t.get('url')),
                            key=lambda t: (t.get('width') or 0) * (t.get('height') or 0),
                            default=None,
                        )
                        if best:
                            cover_url = best.get('url') or ''
                    except Exception:
                        cover_url = ''
                if not cover_url:
                    cover_url = info.get('thumbnail') or ''
                # Filter to the IDs we'll actually attempt — keep the originally resolved
                # ordered list (including already-in-library ones) as the album manifest,
                # so the album detail view always shows the full track list.
                self._upsert_music_album({
                    'id': album_id,
                    'title': album_title,
                    'artist': album_artist,
                    'cover_url': cover_url,
                    'source_url': url,
                    'added_at': int(time.time()),
                    'total_tracks': len(video_ids),
                    'downloaded_count': 0,
                    'status': 'downloading',
                    'track_ids': list(video_ids),
                    'seen_at': None,
                })
                # If some tracks were already in the user's library (singles), stamp
                # them with this album_id so they get absorbed under the album card.
                self._stamp_existing_tracks_with_album(video_ids, album_id)
                # Recompute downloaded_count to reflect already-owned tracks before
                # spawning workers — the album might already be partially complete.
                self._recount_album(album_id)
                # Initial 0% event so the JS can render the ring + paint the card.
                done = self._album_downloaded_count(album_id)
                self._send_to_js(
                    'updateMusicAlbumProgress', album_id, done, len(video_ids)
                )

            lib_ids = self._build_music_library_id_set()
            # Build a quick lookup of already-queued tracks so we don't re-enqueue.
            with self._music_queue_lock:
                existing_q = self.settings.get('music_queue', []) or []
                queued_ids = {
                    e.get('id') for e in existing_q
                    if e.get('status') in ('queued', 'downloading')
                }
            # Per-track metadata stamped on each queue entry up-front: artist/title
            # come from the flat extraction (yt-dlp gives us 'title' which is
            # usually "Artist - Track" on YT Music). Album metadata comes from the
            # collection-level info dict.
            album_title = info.get('title') or info.get('album') or ''
            album_artist = (
                info.get('uploader') or info.get('artist')
                or info.get('creator') or info.get('channel') or ''
            )
            album_cover = ''
            thumbs = info.get('thumbnails') or []
            if thumbs and isinstance(thumbs, list):
                try:
                    best = max(
                        (t for t in thumbs if isinstance(t, dict) and t.get('url')),
                        key=lambda t: (t.get('width') or 0) * (t.get('height') or 0),
                        default=None,
                    )
                    if best:
                        album_cover = best.get('url') or ''
                except Exception:
                    pass
            if not album_cover:
                album_cover = info.get('thumbnail') or ''
            flat_by_id = {e.get('id'): e for e in flat if isinstance(e, dict) and e.get('id')}

            queued = 0
            skipped = 0
            now_ts = int(time.time())
            with self._music_queue_lock:
                q = self.settings.get('music_queue', []) or []
                for vid in video_ids:
                    if vid in lib_ids:
                        skipped += 1
                        continue
                    if vid in queued_ids:
                        skipped += 1
                        continue
                    src = flat_by_id.get(vid, {}) or {}
                    raw_title = src.get('title') or ''
                    # YT Music flat-extract titles often look like "Artist - Track".
                    # Split on " - " if present, else fall back to album-level artist.
                    track_artist = src.get('artist') or src.get('uploader') or ''
                    track_title = raw_title
                    if not track_artist and ' - ' in raw_title:
                        parts = raw_title.split(' - ', 1)
                        track_artist, track_title = parts[0].strip(), parts[1].strip()
                    if not track_artist:
                        track_artist = album_artist
                    # Pick best thumb from per-track thumbnails, else fall back to album cover.
                    track_thumb = ''
                    t_thumbs = src.get('thumbnails') or []
                    if t_thumbs and isinstance(t_thumbs, list):
                        try:
                            best = max(
                                (t for t in t_thumbs if isinstance(t, dict) and t.get('url')),
                                key=lambda t: (t.get('width') or 0) * (t.get('height') or 0),
                                default=None,
                            )
                            if best:
                                track_thumb = best.get('url') or ''
                        except Exception:
                            pass
                    if not track_thumb:
                        track_thumb = album_cover
                    entry = {
                        'id': vid,
                        'title': track_title or 'Untitled',
                        'artist': track_artist,
                        'album': album_title if kind == 'album' else '',
                        'album_id': album_id or '',
                        'thumbnail': track_thumb,
                        'url': f'https://music.youtube.com/watch?v={vid}',
                        'status': 'queued',
                        'progress': 0,
                        'queued_at': now_ts,
                        'started_at': None,
                        'completed_at': None,
                        'error': None,
                    }
                    q.append(entry)
                    queued += 1
                    queued_ids.add(vid)
                self._store.set('music_queue', q)
            # Initial 0% events so search-row rings paint immediately.
            for vid in video_ids:
                if vid not in lib_ids:
                    self._send_to_js('updateMusicDownload', vid, 0)
            if queued:
                self._music_queue_wake()
            self._emit_music_queue()
            if queued:
                self._send_to_js('showToast', f'Added {queued} tracks to download queue.', None, None)
            elif skipped:
                self._send_to_js('showToast', f'{skipped} already in library or queue', None, None)
            else:
                self._send_to_js('showToast', 'Nothing to download', None, None)
            # Edge case: album with zero new downloads (everything was already in
            # library). Flip status to complete immediately so the card doesn't
            # sit forever on 'downloading'.
            if kind == 'album' and queued == 0:
                self._mark_album_complete_if_done(album_id)
        except Exception as e:
            # Log full traceback to protube.log; print() under pythonw is dropped.
            tb = traceback.format_exc()
            self._log_to_protube_log(
                f'[ProTube/music-collection] failed: {kind} {url}: {e}\n{tb}'
            )
            self._send_to_js('showToast', f'{kind.title()} download failed: {e}', None, None)
            # Tell the frontend the collection resolve failed so any search-row
            # that's been spinning on this collection_id can clear its
            # .downloading state — otherwise it sits forever (no per-track
            # event ever fires for an empty/erroring resolve).
            try:
                self._send_to_js(
                    'updateMusicCollectionResolveError',
                    collection_id or '',
                    str(e),
                )
            except Exception:
                pass

    def _music_download_worker(self, url, album_id=None, queue_id=None):
        """Background worker: extract metadata, download audio, embed tags + art,
        stamp the library. If `album_id` is provided, the resulting track entry is
        stamped with it AND the parent album's downloaded_count is incremented.

        `queue_id` is the music_queue entry id this worker is fulfilling — used to
        update status/progress on that entry. Provided when the queue processor
        spawns us; None for direct calls (legacy)."""

        def _is_cancelled():
            if not queue_id:
                return False
            with self._music_queue_lock:
                return queue_id in self._music_queue_cancelled_ids

        captured_filepath = {'path': None}
        try:
            from app_paths import music_dir
            base_dir = music_dir()

            # Mark queue entry as 'downloading'.
            if queue_id:
                self._update_music_queue_entry(
                    queue_id,
                    {'status': 'downloading', 'started_at': int(time.time()), 'progress': 0},
                )

            if _is_cancelled():
                self._finalize_cancelled(queue_id)
                return

            # Phase 1: metadata probe (no download) so we know artist/album for
            # the final filename layout and library entry.
            opts = self._get_ydl_opts('browser', 'none')
            opts.update({'quiet': True, 'no_warnings': True, 'skip_download': True})
            info = self._ydl_extract(url, opts)

            vid = info.get('id') or ''
            if not vid:
                self._send_to_js('showToast', 'Music download failed: no video id', None, None)
                if queue_id:
                    self._update_music_queue_entry(
                        queue_id,
                        {'status': 'failed', 'error': 'no video id', 'completed_at': int(time.time())},
                    )
                return

            # Re-check dedup with the actual extracted ID
            if vid in self._build_music_library_id_set():
                self._send_to_js('showToast', 'Already in your music library', None, None)
                if queue_id:
                    self._update_music_queue_entry(
                        queue_id,
                        {'status': 'done', 'progress': 100, 'completed_at': int(time.time())},
                    )
                return

            # Metadata: YT Music gives us 'artist'/'album'/'track' directly; regular YT
            # falls back to uploader/title with no album info.
            artist = info.get('artist') or info.get('creator') or info.get('uploader') or 'Unknown Artist'
            album = info.get('album') or 'Singles'
            title = info.get('track') or info.get('title') or 'Untitled'
            release_year = info.get('release_year') or info.get('upload_date', '')[:4]
            duration_s = info.get('duration') or 0

            safe_artist = self._sanitize_path_segment(artist, 'Unknown Artist')
            safe_album = self._sanitize_path_segment(album, 'Singles')
            safe_title = self._sanitize_path_segment(title, 'Untitled')

            target_dir = os.path.join(base_dir, safe_artist, safe_album)
            os.makedirs(target_dir, exist_ok=True)

            # Phase 2: actual download.
            dl_opts = self._get_ydl_opts('browser', 'none')
            dl_opts.update({
                'format': 'bestaudio[ext=m4a]/bestaudio/best',
                'outtmpl': os.path.join(target_dir, f'{safe_title}.%(ext)s'),
                'writethumbnail': True,
                'postprocessors': [
                    {'key': 'FFmpegMetadata', 'add_metadata': True},
                    {'key': 'EmbedThumbnail'},
                ],
                'quiet': True,
                'no_warnings': True,
                # Force a fresh download every time. Without this, yt-dlp tries to
                # resume any partial .m4a sitting in target_dir from a previous
                # failed run, which trips 'HTTP Error 416: Requested range not
                # satisfiable' when the URL/server state has changed since the
                # partial was written. Music files are small (~3-5MB) so the
                # retry cost is negligible.
                'continue': False,
                'nopart': False,
            })

            def hook(d):
                # Cancellation: raise to abort the ydl.download() call below.
                if _is_cancelled():
                    raise _MusicDownloadCancelled()
                if d.get('status') == 'finished':
                    captured_filepath['path'] = d.get('filename')
                if d.get('status') == 'downloading':
                    pct = 0
                    total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                    if total:
                        pct = (d.get('downloaded_bytes') or 0) * 100 / total
                    pct_i = int(round(pct))
                    # Throttle: only emit on whole-percent change. Avoids the
                    # ~10x/sec progress-hook spam from yt-dlp causing the queue
                    # to flicker through full re-renders.
                    if queue_id and pct_i == self._music_queue_last_pct.get(queue_id):
                        return
                    if queue_id:
                        self._music_queue_last_pct[queue_id] = pct_i
                    self._send_to_js('updateMusicDownload', vid, pct_i)
                    # In-memory only — don't persist progress to disk on every
                    # tick (was triggering settings-write + full queue refresh).
                    # Frontend updates the row in place via updateMusicDownload.
                    if queue_id:
                        with self._music_queue_lock:
                            q = self.settings.get('music_queue', []) or []
                            for entry in q:
                                if entry.get('id') == queue_id:
                                    entry['progress'] = pct_i
                                    break

            def pp_hook(d):
                if d.get('status') == 'finished':
                    fp = (d.get('info_dict') or {}).get('filepath')
                    if fp:
                        captured_filepath['path'] = fp

            dl_opts['progress_hooks'] = [hook]
            dl_opts['postprocessor_hooks'] = [pp_hook]

            self._send_to_js('updateMusicDownload', vid, 0)
            # Bot-wall resilience (mirrors the video _download_worker): on a
            # no-cookies "confirm you're not a bot" error, retry once through the
            # alternate player clients. Not gated by _ytdlp_gate — the music queue
            # processor already serialises downloads, and holding the gate for a
            # whole download would starve concurrent metadata fetches.
            try:
                with YoutubeDL(dl_opts) as ydl:
                    ydl.download([url])
            except Exception as e_dl:
                if self._is_bot_check_error(str(e_dl)):
                    retry_dl = {**dl_opts, 'extractor_args': {'youtube': {'player_client': list(self._BOT_FALLBACK_CLIENTS)}}}
                    with YoutubeDL(retry_dl) as ydl:
                        ydl.download([url])
                else:
                    raise

            final_path = captured_filepath['path'] or ''
            # If postprocessors changed the extension (likely .m4a) the captured path
            # may be the pre-conversion file. Find the actual final file.
            if final_path:
                base = os.path.splitext(final_path)[0]
                for ext in ('.m4a', '.mp3', '.opus', '.webm'):
                    cand = base + ext
                    if os.path.exists(cand):
                        final_path = cand
                        break

            # The EmbedThumbnail postprocessor put the album art inside the .m4a's
            # metadata stream; we also kept the loose image next to the file (so
            # external players see album art too). The library entry just stores
            # the remote thumbnail URL — fast to render from YouTube's CDN, and
            # works fine as long as we have internet. (Offline album art is a
            # post-MVP polish — would extend the stream server to serve images.)
            local_thumb = ''
            for ext in ('.webp', '.jpg', '.png'):
                cand = os.path.join(target_dir, f'{safe_title}{ext}')
                if os.path.exists(cand):
                    local_thumb = cand
                    break

            # Per-track thumbnail = the YouTube video thumb for this song.
            # Cache it locally via _cache_thumbnail (returns a 'pt:thumb:' marker)
            # so the album-detail view + player view render instantly instead of
            # waiting on i.ytimg.com over the network on every paint. Falls back
            # to the remote URL if the cache attempt fails.
            remote_thumb = info.get('thumbnail') or ''
            if not remote_thumb and vid:
                remote_thumb = f'https://i.ytimg.com/vi/{vid}/hqdefault.jpg'
            thumb = self._cache_thumbnail(remote_thumb, vid) if (remote_thumb and vid) else remote_thumb

            # Build library entry.
            entry = {
                'id': vid,
                'type': 'music',
                'title': title,
                'artist': artist,
                'album': album,
                'year': str(release_year) if release_year else '',
                'duration_string': self._format_duration(duration_s),
                'duration_seconds': int(duration_s) if duration_s else 0,
                'filepath': final_path,
                'thumbnail': thumb,
                'local_thumbnail': local_thumb,
                'url': url,
                'added_at': int(time.time()),
            }
            # Stamp the album link BEFORE persisting so the track is grouped on
            # first paint of the music library grid.
            if album_id:
                entry['album_id'] = album_id
            # Wrap the post-download finalize in a single deferred-save context.
            # Previously each of add_to_music_library / _bump_album_progress /
            # _ensure_album_cover_local / _update_music_queue_entry triggered
            # its own settings.json write — 4 atomic writes per track. For a
            # 50-track album that's 200 writes; visible delay before the
            # library reflects "done". Now: 1 write per track.
            new_cover_marker = ''
            with self._deferred_save():
                self.add_to_music_library(entry)
                if album_id:
                    self._bump_album_progress(album_id)
                    # Extract album cover from the just-downloaded track
                    # immediately so the library card has real art on first
                    # paint instead of a placeholder.
                    try:
                        albums = self.settings.get('music_albums', []) or []
                        alb = next((a for a in albums if a.get('id') == album_id), None)
                        if alb and not (alb.get('cover_url') or '').startswith('pt:thumb:'):
                            new_cover = self._ensure_album_cover_local(alb)
                            if new_cover and new_cover != alb.get('cover_url'):
                                alb['cover_url'] = new_cover
                                self._store.set('music_albums', albums)
                                new_cover_marker = new_cover
                    except Exception:
                        pass
                if queue_id:
                    self._update_music_queue_entry(
                        queue_id,
                        {
                            'status': 'done',
                            'progress': 100,
                            'completed_at': int(time.time()),
                            'title': title,
                            'artist': artist,
                            'album': album,
                            'thumbnail': thumb,
                        },
                    )
            # Emit JS events AFTER the single flush — frontend reads from
            # persistent state next refresh and we don't want the inflight
            # write to race with the load_music_library callback.
            if album_id and new_cover_marker:
                self._send_to_js('musicAlbumCoverResolved', album_id, new_cover_marker)
            self._send_to_js('musicDownloadDone', entry)
        except _MusicDownloadCancelled:
            # User cancelled while ydl.download() was running. Clean up any
            # partial file the download left behind so a retry starts fresh.
            try:
                fp = captured_filepath.get('path')
                if fp and os.path.exists(fp):
                    os.remove(fp)
                # yt-dlp's .part files also leak; clean those siblings too.
                if fp:
                    part = fp + '.part'
                    if os.path.exists(part):
                        os.remove(part)
            except Exception:
                pass
            self._finalize_cancelled(queue_id)
        except Exception as e:
            # Persist the full traceback to data/protube.log so the user can
            # see why a track failed after the fact. The toast is transient
            # and `print()` under pythonw is discarded.
            tb = traceback.format_exc()
            self._log_to_protube_log(
                f'[ProTube/music-dl] failed: {url}: {e}\n{tb}'
            )
            self._send_to_js('showToast', f'Music download failed: {e}', None, None)
            if queue_id:
                self._update_music_queue_entry(
                    queue_id,
                    {'status': 'failed', 'error': str(e), 'completed_at': int(time.time())},
                )

    # ----------------------------- music queue ---------------------------------- #

    def _music_queue_wake(self):
        """Nudge the queue processor to take another pass. Cheap, idempotent."""
        self._music_queue_event.set()

    def _sanitize_music_queue_on_startup(self):
        """Called once during __init__. Resets stuck 'downloading' entries (the
        process was killed mid-download — partial file is incomplete, retry from
        scratch) and drops 'done' / 'cancelled' entries older than 1 hour so the
        queue doesn't grow unbounded across sessions."""
        try:
            q = self.settings.get('music_queue', []) or []
            now = int(time.time())
            cleaned = []
            for e in q:
                st = e.get('status')
                if st == 'downloading':
                    e['status'] = 'queued'
                    e['progress'] = 0
                    e['started_at'] = None
                    cleaned.append(e)
                elif st in ('done', 'cancelled'):
                    age = now - int(e.get('completed_at') or e.get('queued_at') or now)
                    if age < 3600:
                        cleaned.append(e)
                    # else: drop
                else:
                    cleaned.append(e)
            if cleaned != q:
                self._store.set('music_queue', cleaned)
        except Exception as ex:
            print(f'[ProTube/music] queue sanitize failed: {ex}')

    def _emit_music_queue(self):
        """Push the current queue to the frontend so the Downloads panel
        re-renders. Called on every state change. The queue is small (<100 items
        in practice), so re-rendering the whole list is cheap and avoids diffing."""
        try:
            self._send_to_js('updateMusicQueue')
        except Exception:
            pass

    def _update_music_queue_entry(self, queue_id, patch):
        """Patch fields on a queue entry (matched by id), persist, and emit."""
        if not queue_id or not patch:
            return
        with self._music_queue_lock:
            q = self.settings.get('music_queue', []) or []
            changed = False
            for e in q:
                if e.get('id') == queue_id:
                    e.update(patch)
                    changed = True
                    break
            if changed:
                self._store.set('music_queue', q)
        if changed:
            self._emit_music_queue()

    def _finalize_cancelled(self, queue_id):
        """Mark a queue entry as cancelled + clear the cancellation flag."""
        if not queue_id:
            return
        with self._music_queue_lock:
            self._music_queue_cancelled_ids.discard(queue_id)
        self._update_music_queue_entry(
            queue_id,
            {'status': 'cancelled', 'completed_at': int(time.time())},
        )

    def _music_queue_processor(self):
        """Background daemon: drains 'queued' entries up to
        `max_concurrent_music_downloads` at a time. Waits on
        `_music_queue_event` between drains — no busy-poll."""
        while True:
            try:
                self._music_queue_event.wait()
                # Clear immediately so wakes during this drain still trigger
                # another pass after we finish.
                self._music_queue_event.clear()
                self._drain_music_queue()
            except Exception as e:
                print(f'[ProTube/music] queue processor error: {e}')
                # Brief backoff so a persistent bug doesn't hot-loop.
                time.sleep(0.5)

    def _drain_music_queue(self):
        """Promote 'queued' entries to 'downloading' and spawn workers until we
        hit the concurrency cap. Each worker decrements the in-flight count + wakes
        the processor when it finishes so we pick up the next item."""
        while True:
            with self._music_queue_lock:
                if self._music_queue_active >= self.max_concurrent_music_downloads:
                    return
                q = self.settings.get('music_queue', []) or []
                next_entry = next((e for e in q if e.get('status') == 'queued'), None)
                if not next_entry:
                    return
                # Reserve a slot — flip its status here (the worker also sets it,
                # but doing it inside the lock avoids racing the next iteration).
                next_entry['status'] = 'downloading'
                next_entry['started_at'] = int(time.time())
                self._store.set('music_queue', q)
                self._music_queue_active += 1
            self._emit_music_queue()
            queue_id = next_entry.get('id')
            url = next_entry.get('url')
            album_id = next_entry.get('album_id') or None
            threading.Thread(
                target=self._run_music_queue_worker,
                args=(url, album_id, queue_id),
                daemon=True,
            ).start()

    def _run_music_queue_worker(self, url, album_id, queue_id):
        """Wrap _music_download_worker so we always decrement the in-flight
        counter and re-wake the processor for the next item, even on exceptions."""
        try:
            self._music_download_worker(url, album_id=album_id, queue_id=queue_id)
        finally:
            with self._music_queue_lock:
                self._music_queue_active = max(0, self._music_queue_active - 1)
            self._music_queue_wake()

    def get_music_queue(self):
        """Frontend reads this on the Downloads tab mount and after every
        `updateMusicQueue` event."""
        return self.settings.get('music_queue', []) or []

    def cancel_music_queue_item(self, track_id):
        """If status is 'queued', drop from queue. If 'downloading', set the
        cancellation flag so the worker bails at the next progress tick."""
        if not track_id:
            return {'ok': False, 'error': 'no track_id'}
        with self._music_queue_lock:
            q = self.settings.get('music_queue', []) or []
            target = next((e for e in q if e.get('id') == track_id), None)
            if not target:
                return {'ok': False, 'error': 'not in queue'}
            was = target.get('status')
            if was == 'queued':
                # Drop immediately — worker never started for this one.
                self._store.set('music_queue', [e for e in q if e.get('id') != track_id])
            elif was == 'downloading':
                # Worker is running — flag for cancellation. The hook checks
                # this on every progress tick and raises to abort.
                self._music_queue_cancelled_ids.add(track_id)
            else:
                # done / failed / cancelled — nothing to cancel
                return {'ok': False, 'was_status': was}
        self._emit_music_queue()
        return {'ok': True, 'was_status': was}

    def clear_music_queue_done(self):
        """Drop all 'done' + 'cancelled' entries."""
        with self._music_queue_lock:
            q = self.settings.get('music_queue', []) or []
            kept = [e for e in q if e.get('status') not in ('done', 'cancelled')]
            cleared = len(q) - len(kept)
            if cleared:
                self._store.set('music_queue', kept)
        if cleared:
            self._emit_music_queue()
        return {'cleared': cleared}

    def retry_music_queue_item(self, track_id):
        """Flip a 'failed' (or 'cancelled') item back to 'queued', clear error,
        wake the processor."""
        if not track_id:
            return {'ok': False, 'error': 'no track_id'}
        with self._music_queue_lock:
            q = self.settings.get('music_queue', []) or []
            target = next((e for e in q if e.get('id') == track_id), None)
            if not target:
                return {'ok': False, 'error': 'not in queue'}
            if target.get('status') not in ('failed', 'cancelled'):
                return {'ok': False, 'was_status': target.get('status')}
            target['status'] = 'queued'
            target['progress'] = 0
            target['error'] = None
            target['started_at'] = None
            target['completed_at'] = None
            self._store.set('music_queue', q)
        self._music_queue_wake()
        self._emit_music_queue()
        return {'ok': True}

    def cancel_music_album_queued(self, album_id):
        """Drop all 'queued' entries that belong to this album. Lets any
        currently-downloading tracks finish. Returns how many were cancelled."""
        if not album_id:
            return {'cancelled': 0}
        with self._music_queue_lock:
            q = self.settings.get('music_queue', []) or []
            kept = []
            cancelled = 0
            for e in q:
                if e.get('album_id') == album_id and e.get('status') == 'queued':
                    cancelled += 1
                    continue
                kept.append(e)
            if cancelled:
                self._store.set('music_queue', kept)
        if cancelled:
            self._emit_music_queue()
        return {'cancelled': cancelled}

    def set_max_concurrent_music_downloads(self, n):
        """Live-update the concurrency cap. Existing workers continue; new ones
        are spawned/throttled against the new value on the next drain."""
        try:
            n = max(1, min(8, int(n)))
        except Exception:
            return {'ok': False, 'error': 'invalid value'}
        self.max_concurrent_music_downloads = n
        self._store.set('max_concurrent_music_downloads', n)
        self._music_queue_wake()
        return {'ok': True, 'value': n}

    def load_music_library(self):
        """Frontend reads this on the Music view's mount."""
        self._repair_music_thumbnails_from_album_cover()
        self._repair_album_artists_from_tracks()
        # Background-cache remote per-track thumbnails so album-view rows render
        # instantly. Async so the Music tab opens immediately; the swaps land
        # row-by-row as caches complete.
        try:
            t = threading.Thread(target=self._backfill_track_thumb_cache, daemon=True)
            t.start()
        except Exception:
            pass
        return self.settings.get('music_library', []) or []

    def _backfill_track_thumb_cache(self):
        """For every music_library track whose thumbnail is a remote URL,
        download + cache locally so subsequent renders use pt:thumb: markers
        (resolved from disk, no network)."""
        try:
            lib = self.settings.get('music_library', []) or []
            changed = False
            for t in lib:
                tb = t.get('thumbnail') or ''
                vid = t.get('id') or ''
                if not vid or not tb or tb.startswith('pt:thumb:'):
                    continue
                marker = self._cache_thumbnail(tb, vid)
                if marker.startswith('pt:thumb:'):
                    t['thumbnail'] = marker
                    changed = True
            if changed:
                with self._music_queue_lock:
                    self._store.set('music_library', lib)
        except Exception:
            pass

    def _extract_embedded_art(self, audio_path, dest_path):
        """Pull the embedded album-art image out of an audio file via ffmpeg.
        Returns True on success. We try this because YouTube's i9.ytimg.com
        album-cover URLs frequently 404 even with the signed query params
        yt-dlp captures, so the remote URL can't be trusted as a thumbnail
        source. The embedded art always works (yt-dlp puts it there)."""
        try:
            ffmpeg = self._find_ffmpeg_exe()
            if not ffmpeg or not os.path.isfile(audio_path):
                return False
            r = subprocess.run(
                [ffmpeg, '-y', '-i', audio_path, '-an', '-vcodec', 'copy', dest_path],
                capture_output=True, timeout=15,
                creationflags=(0x08000000 if sys.platform == 'win32' else 0),
            )
            return r.returncode == 0 and os.path.isfile(dest_path) and os.path.getsize(dest_path) > 100
        except Exception:
            return False

    def _ensure_album_cover_local(self, album):
        """If an album's cover_url is a remote URL (or absent), try to extract
        the embedded art from any downloaded track and rewrite cover_url to a
        local 'pt:thumb:' marker. Returns the new marker, or the existing
        cover_url if extraction wasn't possible."""
        if not album:
            return ''
        cur = album.get('cover_url') or ''
        if cur.startswith('pt:thumb:'):
            return cur
        aid = album.get('id') or ''
        if not aid:
            return cur
        safe_id = re.sub(r'[^a-zA-Z0-9_\-]', '_', f'alb_{aid}')[:80]
        local_path = os.path.join(self.thumbnail_cache_dir, f'{safe_id}.jpg')
        if not os.path.isfile(local_path) or os.path.getsize(local_path) <= 100:
            lib = self.settings.get('music_library', []) or []
            src = None
            for t in lib:
                if t.get('album_id') == aid and t.get('filepath') and os.path.isfile(t['filepath']):
                    src = t['filepath']
                    break
            if not src:
                return cur
            if not self._extract_embedded_art(src, local_path):
                return cur
        return f'pt:thumb:{safe_id}.jpg'

    def _repair_album_artists_from_tracks(self):
        """Existing albums saved before _resolve_album_artist had its track-
        derived fallback may show 'Unknown Artist'. Fix by walking the album's
        downloaded tracks and picking the most-common artist. Cheap — only
        touches albums whose artist is empty or literally 'Unknown Artist'."""
        try:
            albums = self.settings.get('music_albums', []) or []
            if not albums:
                return
            lib = self.settings.get('music_library', []) or []
            from collections import Counter
            changed = False
            for a in albums:
                cur = (a.get('artist') or '').strip()
                if cur and cur.lower() not in ('unknown artist', 'unknown'):
                    continue
                aid = a.get('id')
                if not aid:
                    continue
                tracks = [t for t in lib if t.get('album_id') == aid]
                counts = Counter()
                for t in tracks:
                    artist = (t.get('artist') or '').strip()
                    if artist and artist.lower() not in ('unknown artist', 'unknown', 'various artists', 'various'):
                        counts[artist] += 1
                if counts:
                    a['artist'] = counts.most_common(1)[0][0]
                    changed = True
            if changed:
                self._store.set('music_albums', albums)
        except Exception:
            pass

    def _repair_music_thumbnails_from_album_cover(self):
        """Cheap-to-run repair: ensure every album has a local cover (extracted
        from a downloaded track's embedded art) and undo an earlier mistake
        where I overwrote per-track thumbnails with the album cover marker.
        Tracks should keep their own YouTube video thumbnail — that's what
        renders next to each song in the album detail view and in the player
        view; the album cover is only for the album card."""
        try:
            albums = self.settings.get('music_albums', []) or []
            if not albums:
                return
            changed_albums = False
            for a in albums:
                aid = a.get('id')
                if not aid:
                    continue
                new_cover = self._ensure_album_cover_local(a)
                if new_cover and new_cover != a.get('cover_url'):
                    a['cover_url'] = new_cover
                    changed_albums = True
            lib = self.settings.get('music_library', []) or []
            changed_lib = False
            for t in lib:
                # Undo the earlier wrong rewrite: if a track's thumbnail points
                # at an album cover marker (pt:thumb:alb_*), restore it to the
                # YouTube video thumb derived from the video id. i.ytimg.com's
                # hqdefault.jpg is reliable for any public YT video id.
                tb = t.get('thumbnail') or ''
                vid = t.get('id') or ''
                if vid and tb.startswith('pt:thumb:alb_'):
                    t['thumbnail'] = f'https://i.ytimg.com/vi/{vid}/hqdefault.jpg'
                    changed_lib = True
            _upd = {}
            if changed_albums:
                _upd['music_albums'] = albums
            if changed_lib:
                _upd['music_library'] = lib
            if _upd:
                self._store.update(_upd)
        except Exception:
            pass

    def add_to_music_library(self, track):
        """Append/replace a track in the music library. Dedup by id (latest wins)."""
        if not track or not track.get('id'):
            return False
        lib = self.settings.get('music_library', []) or []
        lib = [t for t in lib if t.get('id') != track['id']]
        lib.append(track)
        self._store.set('music_library', lib)
        return True

    def mark_music_seen(self, track_id):
        """Stamp the track as 'seen' so the NEW badge on its card disappears.
        Called when the user plays the track for the first time."""
        if not track_id:
            return {'ok': False}
        lib = self.settings.get('music_library', []) or []
        changed = False
        for t in lib:
            if t.get('id') == track_id and not t.get('seen_at'):
                t['seen_at'] = int(time.time())
                changed = True
                break
        if changed:
            self._store.save()
        return {'ok': True}

    def remove_from_music_library(self, track_id):
        """Drop a track from the library. Doesn't delete the file on disk."""
        lib = self.settings.get('music_library', []) or []
        self._store.set('music_library', [t for t in lib if t.get('id') != track_id])
        return {'ok': True}

    def delete_music_track(self, track_id):
        """Drop from library AND delete the file. Hard remove."""
        lib = self.settings.get('music_library', []) or []
        target = next((t for t in lib if t.get('id') == track_id), None)
        if target and target.get('filepath') and os.path.exists(target['filepath']):
            try:
                os.remove(target['filepath'])
            except OSError as e:
                print(f'[ProTube/music] failed to delete {target["filepath"]}: {e}')
        self._store.set('music_library', [t for t in lib if t.get('id') != track_id])
        return {'ok': True}

    def hide_music_track(self, track_id, hidden=True):
        """Toggle a music track's `hidden` flag. Hidden tracks render dimmed
        with a 'Hidden' badge when the user has the 'Show hidden' toggle on,
        and don't render at all when it's off. Mirrors the video library's
        hide-card affordance. Returns the new state so the frontend can flip
        optimistically and reconcile."""
        if not track_id:
            return {'ok': False, 'hidden': False}
        lib = self.settings.get('music_library', []) or []
        changed = False
        new_state = bool(hidden)
        for t in lib:
            if t.get('id') == track_id:
                if bool(t.get('hidden')) != new_state:
                    if new_state:
                        t['hidden'] = True
                    else:
                        t.pop('hidden', None)
                    changed = True
                break
        if changed:
            self._store.save()
        return {'ok': True, 'hidden': new_state}

    def bulk_hide_music_tracks(self, track_ids, hidden=True):
        """Apply hide_music_track to many ids in one shot (one save). Used by
        the album-card "Hide album" right-click action and any future multi-
        select flow."""
        if not track_ids:
            return {'ok': True, 'hidden': bool(hidden), 'count': 0}
        ids = set(track_ids)
        lib = self.settings.get('music_library', []) or []
        changed = False
        new_state = bool(hidden)
        count = 0
        for t in lib:
            if t.get('id') in ids:
                if bool(t.get('hidden')) != new_state:
                    if new_state:
                        t['hidden'] = True
                    else:
                        t.pop('hidden', None)
                    changed = True
                count += 1
        if changed:
            self._store.save()
        return {'ok': True, 'hidden': new_state, 'count': count}

    def bulk_remove_music_tracks(self, track_ids):
        """Drop many tracks from the library in one save. Files on disk are
        preserved (mirrors the video library's 'Remove from library' bulk
        action — the file stays so a re-import can restore the entry).
        Returns the list of ids actually removed so the frontend can size its
        Undo toast accurately."""
        if not track_ids:
            return {'ok': True, 'removed': [], 'count': 0}
        ids = set(track_ids)
        lib = self.settings.get('music_library', []) or []
        removed = [t.get('id') for t in lib if t.get('id') in ids]
        if not removed:
            return {'ok': True, 'removed': [], 'count': 0}
        self._store.set('music_library', [t for t in lib if t.get('id') not in ids])
        return {'ok': True, 'removed': removed, 'count': len(removed)}

    def bulk_delete_music_tracks(self, track_ids):
        """Drop many tracks AND delete their files on disk. One settings save.
        Mirrors delete_music_track for each id but batched so the UI doesn't
        block on per-track round-trips. Returns counts so the frontend can
        report how many succeeded vs failed (locked files, etc.)."""
        if not track_ids:
            return {'ok': True, 'deleted': 0, 'errors': 0, 'count': 0}
        ids = set(track_ids)
        lib = self.settings.get('music_library', []) or []
        deleted = 0
        errors = 0
        for t in lib:
            if t.get('id') not in ids:
                continue
            fp = t.get('filepath') or ''
            if fp and os.path.exists(fp):
                try:
                    os.remove(fp)
                    deleted += 1
                except OSError as e:
                    errors += 1
                    print(f'[ProTube/music] failed to delete {fp}: {e}')
            else:
                # No file on disk to delete, but still drop the entry — count
                # it as deleted so the user sees the row vanish.
                deleted += 1
        self._store.set('music_library', [t for t in lib if t.get('id') not in ids])
        return {'ok': True, 'deleted': deleted, 'errors': errors, 'count': deleted + errors}

    # ----------------------------------------------------------------- #
    # Music albums (first-class library entity grouping multiple tracks) #
    # ----------------------------------------------------------------- #

    def load_music_albums(self):
        """Frontend reads this on the Music view's mount, alongside load_music_library."""
        return self.settings.get('music_albums', []) or []

    def get_music_album(self, album_id):
        """Return one album joined with its full track objects, ordered by
        track_ids. Missing tracks (still downloading) appear as placeholder
        stubs with `pending=True` so the detail view can render a greyed row."""
        if not album_id:
            return None
        albums = self.settings.get('music_albums', []) or []
        album = next((a for a in albums if a.get('id') == album_id), None)
        if not album:
            return None
        lib = self.settings.get('music_library', []) or []
        by_id = {t.get('id'): t for t in lib if t.get('id')}
        tracks = []
        for tid in album.get('track_ids', []) or []:
            t = by_id.get(tid)
            if t:
                tracks.append(t)
            else:
                # Pending stub — track not yet downloaded (or download failed).
                tracks.append({
                    'id': tid,
                    'type': 'music',
                    'title': '',          # will be filled once download lands
                    'artist': album.get('artist', ''),
                    'album': album.get('title', ''),
                    'album_id': album_id,
                    'thumbnail': album.get('cover_url', ''),
                    'duration_string': '',
                    'duration_seconds': 0,
                    'pending': True,
                })
        joined = dict(album)
        joined['tracks'] = tracks
        return joined

    def delete_music_album(self, album_id, delete_files=True):
        """Remove an album record. If delete_files is true, also remove every
        track in track_ids from music_library AND delete the underlying M4As.
        Singles stamped with this album_id (from a re-import flow) are also
        unlinked so they don't leak back into the top-level grid as orphans
        pointing at a deleted album."""
        if not album_id:
            return {'ok': False, 'error': 'no album_id'}
        albums = self.settings.get('music_albums', []) or []
        album = next((a for a in albums if a.get('id') == album_id), None)
        if not album:
            return {'ok': False, 'error': 'album not found'}
        if delete_files:
            lib = self.settings.get('music_library', []) or []
            track_id_set = set(album.get('track_ids') or [])
            # Delete files for tracks linked to this album (by id or album_id stamp).
            for t in lib:
                if t.get('id') in track_id_set or t.get('album_id') == album_id:
                    fp = t.get('filepath') or ''
                    if fp and os.path.exists(fp):
                        try:
                            os.remove(fp)
                        except OSError as e:
                            print(f'[ProTube/music] failed to delete {fp}: {e}')
            lib = [
                t for t in lib
                if not (t.get('id') in track_id_set or t.get('album_id') == album_id)
            ]
        else:
            # Unlink without deleting: clear the album_id stamp so tracks remain
            # as singles in the top-level grid.
            lib = self.settings.get('music_library', []) or []
            for t in lib:
                if t.get('album_id') == album_id:
                    t.pop('album_id', None)
        # Drop the album record and persist both changes atomically.
        self._store.update({
            'music_library': lib,
            'music_albums': [a for a in albums if a.get('id') != album_id],
        })
        return {'ok': True}

    def mark_album_seen(self, album_id):
        """Stamp the album as 'seen' so the NEW pill on its card disappears.
        Called when the user opens the album detail view."""
        if not album_id:
            return {'ok': False}
        albums = self.settings.get('music_albums', []) or []
        changed = False
        for a in albums:
            if a.get('id') == album_id and not a.get('seen_at'):
                a['seen_at'] = int(time.time())
                changed = True
                break
        if changed:
            self._store.save()
        return {'ok': True}

    # ---- Internal album helpers -------------------------------------- #

    def _upsert_music_album(self, album_entry):
        """Insert or replace an album record by id. Latest write wins on the
        mutable fields (downloaded_count, status, etc.) but preserves the
        seen_at stamp from any existing record so reopening the album later
        doesn't re-light the NEW pill."""
        if not album_entry or not album_entry.get('id'):
            return False
        albums = self.settings.get('music_albums', []) or []
        existing = next((a for a in albums if a.get('id') == album_entry['id']), None)
        if existing:
            # Preserve seen_at + original added_at across re-downloads (e.g.
            # user re-adds an album after deleting it — the NEW pill will
            # re-light because added_at gets refreshed, which is the desired
            # behavior; but a partial re-resolve shouldn't reset seen_at if
            # the user has already opened it). We keep both fields from the
            # existing record when present.
            album_entry['seen_at'] = existing.get('seen_at') or album_entry.get('seen_at')
            album_entry['added_at'] = existing.get('added_at') or album_entry.get('added_at')
            albums = [a for a in albums if a.get('id') != album_entry['id']]
        albums.append(album_entry)
        self._store.set('music_albums', albums)
        return True

    def _stamp_existing_tracks_with_album(self, track_ids, album_id):
        """When an album re-download starts and some tracks are already in the
        user's library as singles, stamp them with the album_id so they get
        absorbed into the album card instead of showing as duplicates."""
        if not album_id or not track_ids:
            return
        lib = self.settings.get('music_library', []) or []
        target = set(track_ids)
        changed = False
        for t in lib:
            if t.get('id') in target and t.get('album_id') != album_id:
                t['album_id'] = album_id
                changed = True
        if changed:
            self._store.set('music_library', lib)

    def _album_downloaded_count(self, album_id):
        """Count how many of the album's track_ids are currently in the library."""
        albums = self.settings.get('music_albums', []) or []
        album = next((a for a in albums if a.get('id') == album_id), None)
        if not album:
            return 0
        track_id_set = set(album.get('track_ids') or [])
        lib = self.settings.get('music_library', []) or []
        return sum(1 for t in lib if t.get('id') in track_id_set)

    def _recount_album(self, album_id):
        """Recompute downloaded_count from the library and persist."""
        albums = self.settings.get('music_albums', []) or []
        album = next((a for a in albums if a.get('id') == album_id), None)
        if not album:
            return
        album['downloaded_count'] = self._album_downloaded_count(album_id)
        if album['downloaded_count'] >= album.get('total_tracks', 0) and album.get('total_tracks', 0) > 0:
            album['status'] = 'complete'
        self._store.save()

    def _bump_album_progress(self, album_id):
        """A track from this album just landed. Increment the counter, emit a
        progress event, and flip to 'complete' status if we're done."""
        if not album_id:
            return
        albums = self.settings.get('music_albums', []) or []
        album = next((a for a in albums if a.get('id') == album_id), None)
        if not album:
            return
        # Always recount from library (cheap, ~hundreds of entries max) — avoids
        # off-by-one drift if a worker double-fires or a track is re-downloaded.
        album['downloaded_count'] = self._album_downloaded_count(album_id)
        total = album.get('total_tracks', 0) or 0
        if total and album['downloaded_count'] >= total:
            album['status'] = 'complete'
        self._save_settings()
        self._send_to_js(
            'updateMusicAlbumProgress',
            album_id,
            album['downloaded_count'],
            total,
        )

    def _mark_album_complete_if_done(self, album_id):
        """Flip status to 'complete' if all tracks are already in library
        (used for the case where every track was already owned before download)."""
        if not album_id:
            return
        albums = self.settings.get('music_albums', []) or []
        album = next((a for a in albums if a.get('id') == album_id), None)
        if not album:
            return
        done = self._album_downloaded_count(album_id)
        total = album.get('total_tracks', 0) or 0
        album['downloaded_count'] = done
        if total and done >= total:
            album['status'] = 'complete'
        self._save_settings()
        self._send_to_js('updateMusicAlbumProgress', album_id, done, total)

    def get_music_stream_url(self, track_id):
        """Return a localhost-served URL for an M4A in the music library so the
        <audio> element can play it. Reuses the existing video server (same port,
        same `/v?p=<base64-path>` URL scheme — works for audio files too because
        the server just streams bytes with Range + CORS headers)."""
        try:
            if not self._video_server_port:
                return {'error': 'Stream server not running'}
            lib = self.settings.get('music_library', []) or []
            target = next((t for t in lib if t.get('id') == track_id), None)
            if not target:
                return {'error': 'Track not in music library'}
            fp = target.get('filepath') or ''
            if not fp or not os.path.exists(fp):
                return {'error': 'Audio file missing from disk'}
            import base64 as _b64
            encoded = _b64.urlsafe_b64encode(fp.encode('utf-8')).decode('ascii')
            return {
                'url': f'http://127.0.0.1:{self._video_server_port}/v?p={encoded}',
                'filepath': fp,
                'title': target.get('title', ''),
                'artist': target.get('artist', ''),
                'album': target.get('album', ''),
                'thumbnail': target.get('thumbnail', ''),
                'duration_seconds': target.get('duration_seconds', 0),
            }
        except Exception as e:
            return {'error': f'Stream prep failed: {e}'}

    def _innertube_parse_lockup(self, lv, dedup):
        """Newer 'lockupViewModel' shape YouTube returns for playlists when the
        Playlists filter is active. Best-effort extraction; falls back to skipping
        if we can't pin down a playlist ID.

        Real-world shape (sampled 2026-05):
          contentId: 'PLmNvVoj...' (the playlist ID directly — no VL prefix)
          contentType: 'LOCKUP_CONTENT_TYPE_PLAYLIST'
          metadata.lockupMetadataViewModel.title.content: '<title>'
          contentImage.collectionThumbnailViewModel.primaryThumbnail.thumbnailViewModel.image.sources[]
        """
        try:
            content_type = lv.get('contentType') or ''
            if 'PLAYLIST' not in content_type.upper():
                return None
            pid = lv.get('contentId') or ''
            if not pid:
                return None
            meta = lv.get('metadata', {}).get('lockupMetadataViewModel', {})
            title = (meta.get('title') or {}).get('content') or ''
            # Thumbnail
            thumb = ''
            image = (lv.get('contentImage') or {}).get('collectionThumbnailViewModel', {})
            primary = (image.get('primaryThumbnail') or {}).get('thumbnailViewModel', {})
            thumbs = (primary.get('image') or {}).get('sources') or []
            if thumbs:
                thumb = thumbs[-1].get('url') or ''
            # Video count — look for the badge overlay text "15 videos"
            vcount_text = ''
            overlays = (primary.get('overlays') or [])
            for ov in overlays:
                badge_vm = (ov.get('thumbnailOverlayBadgeViewModel') or {})
                badges = badge_vm.get('thumbnailBadges') or []
                for b in badges:
                    bvm = (b.get('thumbnailBadgeViewModel') or {})
                    if bvm.get('text'):
                        vcount_text = bvm['text']
                        break
                if vcount_text:
                    break
            return {
                'id': pid,
                'type': 'playlist',
                'title': title or 'Untitled',
                'uploader': '',
                'url': f'https://www.youtube.com/playlist?list={pid}',
                'thumbnail': thumb,
                'duration_string': vcount_text,
                'view_count_string': '',
                'in_queue': pid in dedup['queue_playlist_ids'],
                'in_library': pid in dedup['lib_playlist_ids'],
            }
        except Exception:
            return None

