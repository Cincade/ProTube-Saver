import os, re, time
from ydl_utils import YoutubeDL


class ChannelMixin:
    def _extract_channel_branding(self, probe):
        """Pull (avatar_url, banner_url) from a yt-dlp channel probe.

        yt-dlp's `thumbnails` list mixes the channel avatar (square thumbs at
        48/88/176/720/900px) and the channel banner (wide thumbs at aspect > 2).
        Sometimes entries have an `id` like "avatar_uncropped" / "banner_uncropped"
        which we prefer when present. Otherwise we use aspect ratio:
          - avatar: w ≈ h  → pick the highest-res square
          - banner: w / h > 2.5 → pick the highest-res wide
        Returns ('', '') when neither can be found.
        """
        thumbs = probe.get('thumbnails') or []
        avatar_url, avatar_area = '', 0
        banner_url, banner_area = '', 0
        for t in thumbs:
            url = (t or {}).get('url') or ''
            if not url:
                continue
            w = t.get('width') or 0
            h = t.get('height') or 0
            tid = (t.get('id') or '').lower()
            area = w * h
            is_avatar = 'avatar' in tid or (w and h and abs(w - h) <= 4)
            is_banner = 'banner' in tid or (w and h and h > 0 and (w / h) >= 2.5)
            if is_avatar and area >= avatar_area:
                avatar_url, avatar_area = url, area or avatar_area or 1
            elif is_banner and area >= banner_area:
                banner_url, banner_area = url, area or banner_area or 1
        return avatar_url, banner_url

    def _channel_about_url(self, url):
        """Turn `youtube.com/@handle/videos` (or any tabbed channel URL) into
        `youtube.com/@handle/about` so we can fetch the full About-page
        description. Returns '' if the input doesn't look like a channel URL.
        """
        if not url or not isinstance(url, str):
            return ''
        u = url.rstrip('/')
        # Strip any explicit tab suffix
        for tab in ('/videos', '/shorts', '/streams', '/live', '/playlists',
                    '/community', '/about', '/featured', '/podcasts',
                    '/courses', '/membership', '/store', '/releases'):
            if u.lower().endswith(tab):
                u = u[: -len(tab)]
                break
        # Only append /about for channel-style URLs
        ul = u.lower()
        if '/@' in ul or '/channel/' in ul or '/c/' in ul or '/user/' in ul:
            return u + '/about'
        return ''

    def _extract_channel_metadata(self, probe):
        """Pull the full channel-header bundle from a yt-dlp channel probe so
        the detail-view hero can render a YouTube-style header instead of just
        a tile + name. Returns a dict with avatar, banner, subscriberCount
        (int), subscriberCountString ("1.59M subscribers"), description.

        Only meaningful for channel probes — playlist probes won't populate
        most of these and the caller should skip the call.
        """
        avatar, banner = self._extract_channel_branding(probe)
        followers = probe.get('channel_follower_count')
        subs_string = ''
        if isinstance(followers, int) and followers > 0:
            subs_string = self._format_compact_count(followers) + ' subscribers'
        description = (probe.get('description') or '').strip()
        return {
            'avatar': avatar,
            'banner': banner,
            'subscriberCount': followers if isinstance(followers, int) else None,
            'subscriberCountString': subs_string,
            'description': description,
        }

    def _format_compact_count(self, n):
        """1_590_000 → '1.59M'. Matches YouTube's compact display style.
        Used for subscriber/video counts so the channel header reads naturally.
        """
        try:
            n = int(n)
        except (TypeError, ValueError):
            return ''
        if n < 1000:
            return str(n)
        if n < 1_000_000:
            return ('%.2f' % (n / 1000)).rstrip('0').rstrip('.') + 'K'
        if n < 1_000_000_000:
            return ('%.2f' % (n / 1_000_000)).rstrip('0').rstrip('.') + 'M'
        return ('%.2f' % (n / 1_000_000_000)).rstrip('0').rstrip('.') + 'B'

    def _classify_playlist_url(self, url):
        """Return 'channel' for YouTube channel-style URLs, 'playlist' otherwise.

        Channel patterns (videos tab is the canonical re-fetch target):
          youtube.com/@handle[/videos]
          youtube.com/c/customname[/videos]
          youtube.com/channel/UCxxx[/videos]
          youtube.com/user/legacyname[/videos]

        Playlist patterns:
          youtube.com/playlist?list=PLxxx
          youtube.com/watch?v=xxx&list=PLxxx
        """
        if not url:
            return 'playlist'
        u = url.lower()
        # Channel handle, custom URL, channel ID, or legacy user URL
        if '/@' in u or '/channel/' in u or '/c/' in u or '/user/' in u:
            return 'channel'
        return 'playlist'

    def add_playlist_to_library(self, playlist):
        """Move a fully-completed playlist to the library as a single entry with all its children.
        Called by the frontend when it detects a playlist has all children marked Done.
        Dedupes by playlist id and strips missing-file children."""
        if not playlist or not playlist.get('id'):
            return False
        # Backfill subtype from URL for entries fetched before subtype existed.
        if not playlist.get('subtype'):
            playlist['subtype'] = self._classify_playlist_url(playlist.get('url', ''))
        # Filter children: only Done and selected ones, and only those whose files exist
        children = playlist.get('videos', [])
        surviving = []
        for c in children:
            if c.get('selected') is False:
                continue
            if c.get('status') != 'Done':
                continue
            if self._is_file_missing(c):
                continue
            # Cache child thumbnail locally for offline use
            remote = c.get('thumbnail')
            if remote and remote.startswith('http'):
                c['thumbnail'] = self._cache_thumbnail(remote, c.get('id'))
            surviving.append(c)
        if not surviving:
            return False
        playlist_copy = {**playlist, 'videos': surviving, 'status': 'Done'}
        # Stamp added_at if not present so the "NEW" badge works for playlists too.
        if not playlist_copy.get('added_at'):
            playlist_copy['added_at'] = int(time.time())
        # Also stamp each child if not stamped
        for c in playlist_copy['videos']:
            if not c.get('added_at'):
                c['added_at'] = playlist_copy['added_at']
        lib = self.settings.get('library', [])
        lib = [v for v in lib if v.get('id') != playlist['id']]
        lib.append(playlist_copy)
        self._store.set('library', lib)
        return True

    def check_playlist_updates(self, playlist_id):
        """Re-fetch a playlist/channel via yt-dlp flat-playlist and diff against
        the entry's current children. Used by the "Check for updates" UI on the
        playlist detail panels — works for entries in EITHER the library (fully
        downloaded playlists) OR the queue (partially-downloaded playlists and
        channels that were just pasted in). Most playlists live in the queue
        because users rarely download every video, and channels keep growing
        forever — so checking queue is the common case.

        Returns:
          {ok: True, source: 'library'|'queue',
           new: [{id, url, title, uploader, thumbnail, duration_string}, ...],
           removed_ids: [...], total_now: int, last_checked_at: int, subtype: str}
          {ok: False, error: str}

        Side effect for library entries: stamps last_checked_at on the entry. Queue
        entries don't get stamped because the queue is owned by the frontend and
        re-saved on its own cadence — stamping here would race with frontend writes.
        """
        target = None
        source = None
        for v in self.settings.get('library', []):
            if v.get('id') == playlist_id and v.get('type') == 'playlist':
                target, source = v, 'library'
                break
        if not target:
            for v in self.settings.get('queue', []):
                if v.get('id') == playlist_id and v.get('type') == 'playlist':
                    target, source = v, 'queue'
                    break
        if not target:
            return {'ok': False, 'error': 'Playlist not found'}

        url = target.get('url')
        if not url:
            return {'ok': False, 'error': 'Playlist has no source URL — cannot re-fetch'}

        local_ids = {c.get('id') for c in target.get('videos', []) if c.get('id')}

        try:
            # extract_flat is the cheap call (IDs + titles only, no per-video
            # format probe). lazy_playlist=True turns probe['entries'] into a
            # generator so we can iterate page-by-page and report progress as
            # entries arrive, instead of blocking until yt-dlp finishes the walk.
            opts = self._get_ydl_opts('browser', 'none')
            opts.update({
                'extract_flat': True,
                'skip_download': True,
                'lazy_playlist': True,
            })
            with YoutubeDL(opts) as ydl:
                probe = ydl.extract_info(url, download=False)

                # Stream INSIDE the YoutubeDL context so any per-page fetches
                # complete before the session closes. The count we send to the
                # UI is "new videos found so far" (not in local library) — that
                # matches what the user actually cares about. Counting all
                # scanned entries instead would balloon to channel-total-size
                # for users who keep their library curated, and it's misleading.
                #
                # Defensive: dedupe by video id and skip null/no-id stubs.
                # yt-dlp's lazy iterator can yield empty or duplicate items
                # for some YouTube paginators, which inflates raw counts wildly
                # (we saw 100x on a small channel). Dedupe makes the count
                # match reality regardless of what yt-dlp emits.
                entries = []
                seen_ids = set()
                new_count = 0
                for e in (probe.get('entries') or []):
                    if not e:
                        continue
                    vid_id = e.get('id')
                    if not vid_id or vid_id in seen_ids:
                        continue
                    seen_ids.add(vid_id)
                    entries.append(e)
                    if vid_id not in local_ids:
                        new_count += 1
                        if new_count == 1 or new_count % 5 == 0:
                            try:
                                self._send_to_js('onUpdateCheckProgress', playlist_id, new_count)
                            except Exception:
                                pass
                # Final tick so the count lands on the real number even if it
                # didn't fall on a %5 boundary.
                try:
                    self._send_to_js('onUpdateCheckProgress', playlist_id, new_count)
                except Exception:
                    pass
        except Exception as e:
            return {'ok': False, 'error': f'Fetch failed: {e}'}

        remote_ids = seen_ids

        new_entries = []
        for e in entries:
            vid_id = e.get('id')
            if not vid_id or vid_id in local_ids:
                continue
            thumb = None
            thumbs = e.get('thumbnails') or []
            if thumbs:
                thumb = thumbs[-1].get('url')
            elif e.get('thumbnail'):
                thumb = e.get('thumbnail')
            else:
                thumb = f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg"
            new_entries.append({
                'id': vid_id,
                'url': e.get('url') or e.get('webpage_url') or f'https://www.youtube.com/watch?v={vid_id}',
                'title': e.get('title', 'Untitled'),
                'uploader': e.get('uploader') or e.get('channel') or target.get('uploader', ''),
                'thumbnail': thumb,
                'duration_string': self._format_duration(e.get('duration')),
            })

        removed_ids = sorted(local_ids - remote_ids)
        last_checked_at = int(time.time())

        # Refresh the full channel header bundle (avatar, banner, subscribers,
        # description) on every update check so existing channels that pre-date
        # these fields pick them up the next time the user hits "Check for new
        # videos" — or when the frontend auto-triggers it on first open. We
        # always overwrite: yt-dlp gives us fresh URLs each fetch.
        effective_subtype = target.get('subtype') or self._classify_playlist_url(url)
        channel_meta = {}
        if effective_subtype == 'channel':
            channel_meta = self._extract_channel_metadata(probe)
            # Mirror get_channel_metadata: pull the full About-page description
            # so "...more" reveals real long-form text, not the 1-2 line header
            # blurb the /videos tab returns. Best-effort.
            about_url = self._channel_about_url(url)
            if about_url:
                try:
                    about_opts = self._get_ydl_opts('browser', 'none')
                    about_opts.update({
                        'extract_flat': True,
                        'skip_download': True,
                        'lazy_playlist': True,
                    })
                    with YoutubeDL(about_opts) as ydl_about:
                        about_probe = ydl_about.extract_info(about_url, download=False)
                    about_desc = (about_probe.get('description') or '').strip()
                    if about_desc and len(about_desc) > len(channel_meta.get('description', '')):
                        channel_meta['description'] = about_desc
                except Exception:
                    pass

        # Stamp + persist only for library entries. The queue is owned by the
        # frontend (see save_queue / load_queue) and writing to it here would
        # race with the frontend's saveQueueState calls.
        if source == 'library':
            target['last_checked_at'] = last_checked_at
            if not target.get('subtype'):
                target['subtype'] = effective_subtype
            if channel_meta.get('avatar'):
                target['channelAvatar'] = channel_meta['avatar']
            if channel_meta.get('banner'):
                target['channelBanner'] = channel_meta['banner']
            if channel_meta.get('subscriberCount') is not None:
                target['subscriberCount'] = channel_meta['subscriberCount']
            if channel_meta.get('subscriberCountString'):
                target['subscriberCountString'] = channel_meta['subscriberCountString']
            if channel_meta.get('description'):
                target['channelDescription'] = channel_meta['description']
            self._save_settings()

        return {
            'ok': True,
            'source': source,
            'new': new_entries,
            'removed_ids': removed_ids,
            'total_now': len(remote_ids),
            'last_checked_at': last_checked_at,
            'subtype': effective_subtype,
            'channelAvatar': channel_meta.get('avatar', ''),
            'channelBanner': channel_meta.get('banner', ''),
            'subscriberCount': channel_meta.get('subscriberCount'),
            'subscriberCountString': channel_meta.get('subscriberCountString', ''),
            'channelDescription': channel_meta.get('description', ''),
        }

    def get_channel_metadata(self, playlist_id):
        """Cheap, entries-free fetch of a channel's header bundle (avatar,
        banner, subscribers, description). Used by the frontend to backfill
        legacy channel entries that pre-date these fields when the user
        opens the channel detail view — without the seconds-long page walk
        that `check_playlist_updates` does. Persists onto library entries.
        Returns {ok: bool, avatar, banner, subscriberCount,
        subscriberCountString, description, error?}.
        """
        target, source = None, None
        for v in self.settings.get('library', []):
            if v.get('id') == playlist_id and v.get('type') == 'playlist':
                target, source = v, 'library'
                break
        if not target:
            for v in self.settings.get('queue', []):
                if v.get('id') == playlist_id and v.get('type') == 'playlist':
                    target, source = v, 'queue'
                    break
        if not target or not target.get('url'):
            return {'ok': False, 'error': 'Not found'}

        try:
            opts = self._get_ydl_opts('browser', 'none')
            opts.update({
                'extract_flat': True,
                'skip_download': True,
                'lazy_playlist': True,
            })
            with YoutubeDL(opts) as ydl:
                # `extract_flat=True` + `lazy_playlist=True` already keeps entry
                # walks out of the path; we just never iterate probe['entries']
                # so no page fetches happen. We DO want full processing of the
                # channel-level metadata so the description / follower count come
                # through (process=False would strip those).
                probe = ydl.extract_info(target['url'], download=False)
            meta = self._extract_channel_metadata(probe or {})

            # The /videos tab returns the channel HEADER description (a 1-2
            # line blurb). The full About-page text only comes back when we
            # hit the /about endpoint specifically. Fetch it separately and
            # swap in whichever is longer — "Show more" should reveal real
            # content, not the same blurb.
            about_url = self._channel_about_url(target['url'])
            if about_url:
                try:
                    with YoutubeDL(opts) as ydl2:
                        about_probe = ydl2.extract_info(about_url, download=False)
                    about_desc = (about_probe.get('description') or '').strip()
                    if about_desc and len(about_desc) > len(meta.get('description', '')):
                        meta['description'] = about_desc
                except Exception:
                    pass   # /about fetch is best-effort
        except Exception as e:
            return {'ok': False, 'error': f'Fetch failed: {e}'}

        if source == 'library':
            if meta.get('avatar'):
                target['channelAvatar'] = meta['avatar']
            if meta.get('banner'):
                target['channelBanner'] = meta['banner']
            if meta.get('subscriberCount') is not None:
                target['subscriberCount'] = meta['subscriberCount']
            if meta.get('subscriberCountString'):
                target['subscriberCountString'] = meta['subscriberCountString']
            if meta.get('description'):
                target['channelDescription'] = meta['description']
            self._save_settings()

        return {
            'ok': True,
            'avatar': meta.get('avatar', ''),
            'banner': meta.get('banner', ''),
            'subscriberCount': meta.get('subscriberCount'),
            'subscriberCountString': meta.get('subscriberCountString', ''),
            'description': meta.get('description', ''),
        }

    def add_videos_to_library_playlist(self, target_playlist_id, videos):
        """Append downloaded videos to an existing library playlist/channel entry.
        Called by the frontend after a "Check for updates" download batch finishes,
        to merge the temp-playlist children into their original library home.

        Dedupes by video id (already-present videos are skipped). Stamps added_at
        on each newly-appended child so the NEW badge fires.

        Returns {ok: bool, added: int, error?: str}.
        """
        if not target_playlist_id or not videos:
            return {'ok': False, 'error': 'Missing target id or videos'}
        lib = self.settings.get('library', [])
        target = None
        for v in lib:
            if v.get('id') == target_playlist_id and v.get('type') == 'playlist':
                target = v
                break
        if not target:
            return {'ok': False, 'error': 'Target playlist not found in library'}

        existing_ids = {c.get('id') for c in target.get('videos', []) if c.get('id')}
        now = int(time.time())
        new_children = []
        for v in videos:
            vid_id = v.get('id')
            if not vid_id or vid_id in existing_ids:
                continue
            child = dict(v)
            # Match the shape of children placed by add_playlist_to_library —
            # cache thumbnail locally, stamp added_at, mark as Done playlist child.
            remote = child.get('thumbnail')
            if remote and isinstance(remote, str) and remote.startswith('http'):
                child['thumbnail'] = self._cache_thumbnail(remote, vid_id)
            child['added_at'] = now
            child['isFromPlaylist'] = True
            child['playlistTitle'] = target.get('title', '')
            child['status'] = 'Done'
            new_children.append(child)
            existing_ids.add(vid_id)

        # Prepend — these are the newest videos from the source. YouTube's flat
        # playlist response returns reverse-chronological for channels' Videos tabs
        # and playlist order otherwise, both of which mean "new stuff goes on top."
        target['videos'] = new_children + target.get('videos', [])
        target['videoCount'] = len(target['videos'])
        target['last_updated_at'] = now
        self._save_settings()
        return {'ok': True, 'added': len(new_children)}

    def check_library_files(self):
        """
        Scan every library entry and mark 'missing: True' on any whose file is gone.
        Returns a list of ids that are newly missing so the UI can refresh them.
        Also silently self-heals broken filepaths where the real file exists in the folder.
        Called periodically (or on library view mount) to catch user-deleted files.
        """
        lib = self.settings.get('library', [])
        newly_missing = []
        changed = False
        for v in lib:
            if v.get('type') == 'playlist':
                for child in v.get('videos', []):
                    was_missing = child.get('missing', False)
                    prev_fp = child.get('filepath')
                    is_missing = self._is_file_missing(child)  # may mutate filepath
                    if child.get('filepath') != prev_fp:
                        changed = True
                    child['missing'] = is_missing
                    if is_missing and not was_missing:
                        newly_missing.append(child['id'])
                        changed = True
                    elif not is_missing and was_missing:
                        changed = True
            else:
                was_missing = v.get('missing', False)
                prev_fp = v.get('filepath')
                is_missing = self._is_file_missing(v)
                if v.get('filepath') != prev_fp:
                    changed = True
                v['missing'] = is_missing
                if is_missing and not was_missing:
                    newly_missing.append(v['id'])
                    changed = True
                elif not is_missing and was_missing:
                    changed = True
        if changed:
            self._save_settings()
        return newly_missing

    def _is_file_missing(self, video):
        """True if this video claims to have a filepath but the file is gone from disk.
        Self-healing: if the recorded filepath is an intermediate stream (e.g. .f140.m4a)
        or a .part file that no longer exists, look in the same folder for the real
        merged video file. If we find one, silently UPDATE the video's filepath and
        return False. This fixes entries created when the download path was wrong."""
        fp = video.get('filepath')
        if not fp:
            # No filepath means we never tracked one — can't verify, assume OK
            return False

        if os.path.exists(fp):
            # But even if it exists — if it's an intermediate stream, try to upgrade to merged
            if self._looks_intermediate(fp) or fp.endswith('.part'):
                folder = os.path.dirname(fp)
                merged = self._find_merged_output(folder, video.get('title', ''))
                if merged and merged != fp:
                    video['filepath'] = merged
                    # Folder is fine as-is
            return False

        # File doesn't exist at recorded path. Try to find the real file in the folder.
        folder = video.get('folderpath') or os.path.dirname(fp)
        if folder and os.path.isdir(folder):
            merged = self._find_merged_output(folder, video.get('title', ''))
            if merged:
                video['filepath'] = merged
                return False

        return True

