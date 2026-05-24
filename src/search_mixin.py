"""Search domain mixin — YouTube + YT Music Innertube search, suggestions,
for-you landing, and recent-search management.

Consumed by logic.API via multiple inheritance.
"""

import re
import time

import requests

from service_base import Service


class SearchMixin(Service):
    """YouTube and YT Music Innertube search, suggestions, and for-you logic."""

    def __init__(self, ctx):
        super().__init__(ctx)
        self._settings_svc = None   # wired: _normalize_channel_url_str

    def wire(self, *, settings_svc, **_):
        self._settings_svc = settings_svc

    # ---- YouTube search (F9) ---------------------------------------------------------
    # Hits YouTube's internal "Innertube" API directly — the same JSON endpoint
    # the YouTube website itself uses. Way faster than yt-dlp's ytsearch (which has
    # to fetch + parse the full HTML search page server-side). No API key needed,
    # no quota. Tradeoff: unofficial, so YouTube could change the schema — but it's
    # the API powering their own website, so they have strong incentive to keep it
    # stable. We added it 2026-05 after yt-dlp's ytsearch was taking 30-75s on the
    # user's network. Innertube on the same network: 1-3s.

    _INNERTUBE_URL = 'https://www.youtube.com/youtubei/v1/search'
    _INNERTUBE_CLIENT = {
        'clientName': 'WEB',
        'clientVersion': '2.20240101.00.00',
        'hl': 'en',
        'gl': 'US',
    }
    # Base64-encoded protobuf filter params (the strings YouTube uses in its own
    # "Filters" dropdown). These restrict results to a single type — without them
    # results are mixed.
    _INNERTUBE_FILTER_PARAMS = {
        'videos':    'EgIQAQ%3D%3D',
        'channels':  'EgIQAg%3D%3D',
        'playlists': 'EgIQAw%3D%3D',
    }
    _INNERTUBE_SESSION = None  # lazy, persistent for connection reuse

    # YouTube's unofficial suggestion endpoint. Same one the YouTube search box itself
    # uses. No API key, no quota. Stable for years.
    _YT_SUGGEST_URL = 'https://suggestqueries.google.com/complete/search'

    # YouTube MUSIC Innertube — same protocol as regular YT but the WEB_REMIX client
    # gets music-shaped responses (Songs / Albums / Artists / Playlists) with proper
    # track/artist/album metadata instead of generic videoRenderer blobs.
    _MUSIC_INNERTUBE_URL = 'https://music.youtube.com/youtubei/v1/search'
    _MUSIC_INNERTUBE_CLIENT = {
        'clientName': 'WEB_REMIX',
        'clientVersion': '1.20240101.01.00',
        'hl': 'en',
        'gl': 'US',
    }
    # Filter params (base64-encoded protobuf) for YT Music's category tabs.
    _MUSIC_INNERTUBE_FILTER_PARAMS = {
        'songs':     'EgWKAQIIAWoKEAkQBRADEAoQBA%3D%3D',
        'videos':    'EgWKAQIQAWoKEAkQAxAEEAUQCg%3D%3D',
        'albums':    'EgWKAQIYAWoKEAkQChAFEAMQBA%3D%3D',
        'artists':   'EgWKAQIgAWoKEAkQBRAKEAMQBA%3D%3D',
        'playlists': 'EgWKAQIoAWoKEAkQAxAEEAUQCQ%3D%3D',
    }
    _MUSIC_INNERTUBE_SESSION = None

    def search_youtube_suggestions(self, query):
        """Return up to 8 search-suggestion strings for a partial query. Powers the
        autocomplete dropdown under the search input."""
        try:
            q = (query or '').strip()
            if not q:
                return {'suggestions': []}
            resp = requests.get(
                self._YT_SUGGEST_URL,
                params={'client': 'firefox', 'ds': 'yt', 'q': q},
                timeout=4,
            )
            if resp.status_code != 200:
                return {'suggestions': []}
            data = resp.json()
            sugg = data[1] if isinstance(data, list) and len(data) > 1 else []
            return {'suggestions': [s for s in sugg[:8] if isinstance(s, str)]}
        except Exception:
            return {'suggestions': []}

    def _get_innertube_session(self):
        """Persistent requests.Session for keep-alive across search calls.
        Without this each call goes through TLS handshake + connection setup."""
        if self._INNERTUBE_SESSION is None:
            sess = requests.Session()
            sess.headers.update({
                'Content-Type': 'application/json',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36',
                'Accept-Language': 'en-US,en;q=0.9',
            })
            # Stash on the class so all instances share the connection pool.
            type(self)._INNERTUBE_SESSION = sess
        return self._INNERTUBE_SESSION

    def _build_dedup_sets(self):
        """Build per-type sets of IDs/URLs from the current queue and library so
        search results can be tagged in_queue / in_library without per-result loops."""
        queue_video_ids = set()
        queue_playlist_ids = set()
        queue_channel_urls = set()
        for q_item in self.settings.get('queue', []):
            if q_item.get('type') == 'playlist':
                pid = q_item.get('id')
                if pid:
                    queue_playlist_ids.add(pid)
                if q_item.get('subtype') == 'channel' and q_item.get('url'):
                    queue_channel_urls.add(self._settings_svc._normalize_channel_url_str(q_item['url']))
                for c in (q_item.get('videos') or []):
                    if c.get('id'):
                        queue_video_ids.add(c['id'])
            elif q_item.get('id'):
                queue_video_ids.add(q_item['id'])
        lib_video_ids = set()
        lib_playlist_ids = set()
        lib_channel_urls = set()
        for l_item in self.settings.get('library', []):
            if l_item.get('type') == 'playlist':
                pid = l_item.get('id')
                if pid:
                    lib_playlist_ids.add(pid)
                if l_item.get('subtype') == 'channel' and l_item.get('url'):
                    lib_channel_urls.add(self._settings_svc._normalize_channel_url_str(l_item['url']))
                for c in (l_item.get('videos') or []):
                    if c.get('id'):
                        lib_video_ids.add(c['id'])
            elif l_item.get('id'):
                lib_video_ids.add(l_item['id'])
        return {
            'queue_video_ids': queue_video_ids,
            'queue_playlist_ids': queue_playlist_ids,
            'queue_channel_urls': queue_channel_urls,
            'lib_video_ids': lib_video_ids,
            'lib_playlist_ids': lib_playlist_ids,
            'lib_channel_urls': lib_channel_urls,
        }

    def _innertube_extract_text(self, node, *keys):
        """Pull a text string out of YouTube's nested {runs:[{text:...}], simpleText:...}
        shapes. Returns the first non-empty value found across the given keys."""
        if not isinstance(node, dict):
            return ''
        for k in keys:
            v = node.get(k)
            if not v:
                continue
            if isinstance(v, str):
                return v
            if isinstance(v, dict):
                if v.get('simpleText'):
                    return v['simpleText']
                runs = v.get('runs') or []
                if runs:
                    return ''.join(r.get('text', '') for r in runs if isinstance(r, dict))
        return ''

    def _innertube_parse_video(self, vr, dedup):
        """Convert a videoRenderer dict into the unified result shape.

        Extracts the same fields YouTube's own search UI shows: title, channel name,
        view count, time-ago, channel avatar, verified-creator badge, and the
        description snippet. The frontend uses these to render YouTube-style cards.
        """
        vid = vr.get('videoId') or ''
        if not vid:
            return None
        title = self._innertube_extract_text(vr, 'title')
        uploader = self._innertube_extract_text(vr, 'ownerText', 'longBylineText', 'shortBylineText')
        duration = self._innertube_extract_text(vr, 'lengthText')
        # Prefer the short ("5.8M views") form over the long ("5,886,105 views") form —
        # matches YouTube's own search UI and reads better on the card.
        view_count = self._innertube_extract_text(vr, 'shortViewCountText', 'viewCountText')
        published_time = self._innertube_extract_text(vr, 'publishedTimeText')

        # Description: "detailedMetadataSnippets" wraps the text in {snippetText: {runs:[]}}.
        description = ''
        dms = vr.get('detailedMetadataSnippets') or []
        if dms:
            description = self._innertube_extract_text(dms[0], 'snippetText')

        # Channel avatar (small circular thumb shown next to channel name).
        channel_thumbnail = ''
        ctsr = (vr.get('channelThumbnailSupportedRenderers') or {})
        ctwlr = ctsr.get('channelThumbnailWithLinkRenderer') or {}
        ct_thumbs = (ctwlr.get('thumbnail') or {}).get('thumbnails') or []
        if ct_thumbs:
            channel_thumbnail = ct_thumbs[-1].get('url') or ''
        if channel_thumbnail.startswith('//'):
            channel_thumbnail = 'https:' + channel_thumbnail

        # Verified-creator badge — YouTube renders a small checkmark next to the channel name.
        channel_verified = False
        for badge in (vr.get('ownerBadges') or []):
            mbr = (badge or {}).get('metadataBadgeRenderer') or {}
            if 'VERIFIED' in str(mbr.get('style') or '').upper():
                channel_verified = True
                break

        # Channel URL for the "open the creator's channel" preview. The owner link
        # lives in the byline runs' navigationEndpoint; prefer the @handle path,
        # fall back to /channel/<id>.
        channel_url = ''
        def _url_from_be(_be):
            _canon = (_be or {}).get('canonicalBaseUrl') or ''
            _bid = (_be or {}).get('browseId') or ''
            if _canon.startswith('/'):
                return f'https://www.youtube.com{_canon}'
            if _bid.startswith('UC'):
                return f'https://www.youtube.com/channel/{_bid}'
            return ''
        for _key in ('ownerText', 'longBylineText', 'shortBylineText'):
            for _run in ((vr.get(_key) or {}).get('runs') or []):
                channel_url = _url_from_be((_run.get('navigationEndpoint') or {}).get('browseEndpoint'))
                if channel_url:
                    break
            if channel_url:
                break
        # Fallback: the channel-avatar link carries the same browseEndpoint and is
        # present on videos whose byline runs omit it (was leaving channel_url empty
        # for some results, so their creator row wasn't clickable).
        if not channel_url:
            channel_url = _url_from_be((ctwlr.get('navigationEndpoint') or {}).get('browseEndpoint'))

        thumbs = (vr.get('thumbnail') or {}).get('thumbnails') or []
        thumb = thumbs[-1].get('url') if thumbs else f'https://i.ytimg.com/vi/{vid}/hqdefault.jpg'
        return {
            'id': vid,
            'type': 'video',
            'title': title or 'Untitled',
            'uploader': uploader,
            'url': f'https://www.youtube.com/watch?v={vid}',
            'thumbnail': thumb,
            'duration_string': duration,
            'view_count_string': view_count,
            'published_time': published_time,           # "2 years ago"
            'description': description,                  # short snippet line
            'channel_thumbnail': channel_thumbnail,      # small avatar URL
            'channel_verified': channel_verified,        # bool
            'channel_url': channel_url,                   # creator channel → preview
            'in_queue': vid in dedup['queue_video_ids'],
            'in_library': vid in dedup['lib_video_ids'],
        }

    def _innertube_parse_channel(self, cr, dedup):
        """Convert a channelRenderer dict into the unified result shape.

        Heads-up on YouTube's misleading field names:
        - `videoCountText.simpleText` actually holds the SUBSCRIBER COUNT ("2.28K subscribers")
        - `subscriberCountText.simpleText` actually holds the @HANDLE ("@PastaWord")
        Yes, they're swapped from what the names suggest. Don't ask me why.
        """
        cid = cr.get('channelId') or ''
        if not cid:
            return None
        title = self._innertube_extract_text(cr, 'title')
        # Channel URL: prefer the canonical @handle path when present (looks cleaner
        # to the user and is what the existing channel-fetch flow expects).
        nav = cr.get('navigationEndpoint') or {}
        browse = nav.get('browseEndpoint') or {}
        canonical = browse.get('canonicalBaseUrl') or ''
        if canonical and canonical.startswith('/'):
            url = f'https://www.youtube.com{canonical}'
        else:
            url = f'https://www.youtube.com/channel/{cid}'
        # Pull subscribers + handle from the (swapped) fields.
        subs = self._innertube_extract_text(cr, 'videoCountText')
        handle = self._innertube_extract_text(cr, 'subscriberCountText')
        # descriptionSnippet — usually has runs of text + bold markers; join and trim.
        desc = self._innertube_extract_text(cr, 'descriptionSnippet')
        thumbs = (cr.get('thumbnail') or {}).get('thumbnails') or []
        thumb = thumbs[-1].get('url') if thumbs else ''
        if thumb.startswith('//'):
            thumb = 'https:' + thumb
        norm = self._settings_svc._normalize_channel_url_str(url)
        # uploader field carries the description for the channel-card subtitle line —
        # frontend already renders this as the secondary text under the title.
        # view_count_string carries "@handle · 2.28K subscribers" so the bottom stats
        # row reads naturally without us needing a new field.
        stats_bits = []
        if handle:
            stats_bits.append(handle)
        if subs:
            stats_bits.append(subs)
        return {
            'id': cid,
            'type': 'channel',
            'title': title or 'Untitled',
            'uploader': desc,
            'url': url,
            'thumbnail': thumb,
            'duration_string': '',
            'view_count_string': ' · '.join(stats_bits),
            'in_queue': norm in dedup['queue_channel_urls'],
            'in_library': norm in dedup['lib_channel_urls'],
        }

    def _innertube_parse_playlist(self, pr, dedup):
        """Convert a playlistRenderer dict into the unified result shape."""
        pid = pr.get('playlistId') or ''
        if not pid:
            return None
        title = self._innertube_extract_text(pr, 'title')
        uploader = self._innertube_extract_text(pr, 'shortBylineText', 'longBylineText')
        vid_count = pr.get('videoCount') or ''
        # Playlist thumbnails are nested differently: thumbnails: [{ thumbnails: [...] }]
        thumb = ''
        thumbs_outer = pr.get('thumbnails') or []
        if thumbs_outer:
            inner = (thumbs_outer[0] or {}).get('thumbnails') or []
            if inner:
                thumb = inner[-1].get('url') or ''
        if not thumb:
            # Fall back to the first video in the playlist if exposed
            videos = pr.get('videos') or []
            if videos:
                vid_id = (videos[0] or {}).get('videoId')
                if vid_id:
                    thumb = f'https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg'
        return {
            'id': pid,
            'type': 'playlist',
            'title': title or 'Untitled',
            'uploader': uploader,
            'url': f'https://www.youtube.com/playlist?list={pid}',
            'thumbnail': thumb,
            'duration_string': f'{vid_count} videos' if vid_count else '',
            'view_count_string': '',
            'in_queue': pid in dedup['queue_playlist_ids'],
            'in_library': pid in dedup['lib_playlist_ids'],
        }

    def search_youtube(self, query, count=20, kind='all', continuation=None):
        """Run a YouTube search via Innertube. Supports continuation tokens for true
        infinite scroll — pass the token from a previous call's `continuation` field
        to fetch the next batch instead of restarting the query.

        kind: 'all' | 'videos' | 'channels' | 'playlists' (ignored when using continuation;
                                                          YouTube already remembered the filter).

        Returns:
          { results: [...], continuation: '<token-or-null>', kind, count }
        """
        try:
            body = {'context': {'client': dict(self._INNERTUBE_CLIENT)}}
            if continuation:
                # Continuation request — no query/params needed; the token carries them.
                body['continuation'] = continuation
            else:
                q = (query or '').strip()
                if not q:
                    return {'results': [], 'continuation': None, 'kind': kind}
                body['query'] = q
                if kind in self._INNERTUBE_FILTER_PARAMS:
                    body['params'] = self._INNERTUBE_FILTER_PARAMS[kind].replace('%3D', '=')

            sess = self._get_innertube_session()
            try:
                resp = sess.post(
                    self._INNERTUBE_URL,
                    params={'prettyPrint': 'false'},
                    json=body,
                    timeout=15,
                )
            except requests.RequestException as exc:
                return {'error': f'Network error: {exc}'}
            if resp.status_code != 200:
                return {'error': f'Innertube HTTP {resp.status_code}'}

            try:
                data = resp.json()
            except ValueError:
                return {'error': 'Innertube returned non-JSON'}

            dedup = self._build_dedup_sets()
            results = []
            next_continuation = None

            # Two response shapes — initial search vs continuation fetch.
            if continuation:
                # Continuation: onResponseReceivedCommands[].appendContinuationItemsAction.continuationItems[]
                for cmd in (data.get('onResponseReceivedCommands') or []):
                    a = (cmd or {}).get('appendContinuationItemsAction') or {}
                    for item in (a.get('continuationItems') or []):
                        if not isinstance(item, dict):
                            continue
                        # Each item can be: itemSectionRenderer (wrapping result items),
                        # continuationItemRenderer (next-page token), or a bare result item.
                        if 'itemSectionRenderer' in item:
                            for sub in (item['itemSectionRenderer'].get('contents') or []):
                                if isinstance(sub, dict):
                                    parsed = self._innertube_parse_item(sub, dedup)
                                    if parsed: results.append(parsed)
                        elif 'continuationItemRenderer' in item:
                            ce = item['continuationItemRenderer'].get('continuationEndpoint') or {}
                            cc = ce.get('continuationCommand') or {}
                            tok = cc.get('token')
                            if tok: next_continuation = tok
                        else:
                            parsed = self._innertube_parse_item(item, dedup)
                            if parsed: results.append(parsed)
            else:
                # Initial: contents → twoColumnSearchResultsRenderer → primaryContents
                # → sectionListRenderer → contents[]
                try:
                    sections = (data.get('contents', {})
                                    .get('twoColumnSearchResultsRenderer', {})
                                    .get('primaryContents', {})
                                    .get('sectionListRenderer', {})
                                    .get('contents', [])) or []
                except AttributeError:
                    sections = []
                for section in sections:
                    if not isinstance(section, dict):
                        continue
                    if 'continuationItemRenderer' in section:
                        ce = section['continuationItemRenderer'].get('continuationEndpoint') or {}
                        cc = ce.get('continuationCommand') or {}
                        tok = cc.get('token')
                        if tok: next_continuation = tok
                        continue
                    for item in ((section.get('itemSectionRenderer') or {}).get('contents') or []):
                        if isinstance(item, dict):
                            parsed = self._innertube_parse_item(item, dedup)
                            if parsed: results.append(parsed)

            return {
                'results': results,
                'continuation': next_continuation,
                'kind': kind,
                'count': len(results),
            }
        except Exception as exc:
            print(f'[ProTube] search failed: {exc}')
            return {'error': f'Search failed: {exc}'}

    def _innertube_parse_item(self, item, dedup):
        """Dispatch a raw Innertube item to the appropriate parser based on which
        renderer key it carries. Returns None for shapes we don't care about (shelves,
        ads, radio, etc.)."""
        if 'videoRenderer' in item:
            return self._innertube_parse_video(item['videoRenderer'], dedup)
        if 'channelRenderer' in item:
            return self._innertube_parse_channel(item['channelRenderer'], dedup)
        if 'playlistRenderer' in item:
            return self._innertube_parse_playlist(item['playlistRenderer'], dedup)
        if 'lockupViewModel' in item:
            return self._innertube_parse_lockup(item['lockupViewModel'], dedup)
        return None

