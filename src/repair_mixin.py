import os, re, requests
from ydl_utils import YoutubeDL, _richness
from service_base import Service


class RepairMixin(Service):
    def __init__(self, ctx):
        super().__init__(ctx)
        self._channel_svc = None    # wired: _is_file_missing
        self._import_svc = None     # wired: _apply_refetched_thumbnail
        self._download_svc = None   # wired: _format_duration

    def wire(self, *, channel_svc, import_svc, download_svc, **_):
        self._channel_svc = channel_svc
        self._import_svc = import_svc
        self._download_svc = download_svc
    def debug_queue_status(self):
        """Dump what's in queue and library for debugging. Called by frontend on demand."""
        queue = self.settings.get('queue', [])
        library = self.settings.get('library', [])
        migrated = self.settings.get('_library_migrated', False)

        summary = {
            'migrated': migrated,
            'queue_count': len(queue),
            'library_count': len(library),
            'queue_items': [],
            'library_items': []
        }
        for item in queue:
            if item.get('type') == 'playlist':
                children_status = [c.get('status') for c in item.get('videos', [])]
                summary['queue_items'].append({
                    'id': item.get('id'),
                    'title': item.get('title'),
                    'type': 'playlist',
                    'children_status': children_status,
                    'done_children': sum(1 for s in children_status if s == 'Done')
                })
            else:
                summary['queue_items'].append({
                    'id': item.get('id'),
                    'title': item.get('title'),
                    'type': 'video',
                    'status': item.get('status'),
                    'has_filepath': bool(item.get('filepath'))
                })
        for item in library:
            summary['library_items'].append({
                'id': item.get('id'),
                'title': item.get('title'),
                'type': item.get('type', 'video'),
                'has_filepath': bool(item.get('filepath')),
                'missing': item.get('missing', False)
            })
        return summary

    def force_remigrate(self):
        """Clear migration flag and re-run. For testing / recovery when migration failed."""
        self._store.set('_library_migrated', False)
        result = self._migrate_queue_done_to_library()
        return result

    def repair_library(self):
        """One-shot cleanup pass over the library. Fixes:
          - Entries duplicated across imported + downloaded flows (same filepath/title)
          - Entries with stale intermediate filepaths (.f140.m4a etc.) — heals to merged .mp4
          - Entries whose filepath doesn't exist and has no recoverable folder (removed)
          - Playlist entries with 0 or 1 children that aren't real playlists (flattened or dropped)
          - Items in library that are ALSO in queue as non-Done (they belong only in queue)

        Returns counts so the UI can summarize what happened."""
        lib = self.settings.get('library', [])
        queue = self.settings.get('queue', [])

        # Build queue id set for cross-check (items paused/queued in queue shouldn't be in library)
        queue_non_done_ids = set()
        for q in queue:
            if q.get('type') == 'playlist':
                for c in q.get('videos', []):
                    if c.get('status') != 'Done':
                        queue_non_done_ids.add(c.get('id'))
            else:
                if q.get('status') != 'Done':
                    queue_non_done_ids.add(q.get('id'))

        fixed_paths = 0
        dropped_broken = 0
        dropped_queue_conflict = 0
        dedupe_merged = 0
        flattened_playlists = 0

        new_lib = []
        seen_filepaths = {}  # normalized filepath -> index in new_lib

        def normalize_fp(fp):
            if not fp:
                return None
            try:
                return os.path.normcase(os.path.normpath(fp))
            except Exception:
                return fp

        for v in lib:
            # Rule: if this id exists in queue as non-Done, it doesn't belong in library
            if v.get('id') in queue_non_done_ids:
                dropped_queue_conflict += 1
                continue

            if v.get('type') == 'playlist':
                children = v.get('videos', [])
                # Heal each child's filepath + drop children we can't find
                good_children = []
                for c in children:
                    is_missing = self._channel_svc._is_file_missing(c)  # may heal filepath in-place
                    if c.get('filepath') and is_missing:
                        # Path is genuinely broken — drop the child
                        continue
                    good_children.append(c)

                if len(good_children) == 0:
                    # Empty playlist is garbage — drop it
                    dropped_broken += 1
                    continue
                if len(good_children) == 1:
                    # "Playlist" with 1 child is just a standalone — flatten it
                    only = good_children[0]
                    flat = {**only, 'isFromPlaylist': False}
                    flat.pop('playlistTitle', None)
                    # Dedupe by filepath with existing entries
                    fp_key = normalize_fp(flat.get('filepath'))
                    if fp_key and fp_key in seen_filepaths:
                        dedupe_merged += 1
                    else:
                        if fp_key:
                            seen_filepaths[fp_key] = len(new_lib)
                        new_lib.append(flat)
                        flattened_playlists += 1
                    continue

                # Real playlist — keep with healed children
                v['videos'] = good_children
                new_lib.append(v)
                continue

            # Single video
            prev_fp = v.get('filepath')
            is_missing = self._channel_svc._is_file_missing(v)  # self-heals filepath
            if v.get('filepath') != prev_fp:
                fixed_paths += 1

            # If it has a filepath AND the filepath is still bad, drop it
            if v.get('filepath') and is_missing:
                dropped_broken += 1
                continue

            # Dedupe by filepath
            fp_key = normalize_fp(v.get('filepath'))
            if fp_key and fp_key in seen_filepaths:
                # Merge: keep the richer entry (one with a url / thumbnail / uploader)
                idx = seen_filepaths[fp_key]
                existing = new_lib[idx]
                # Prefer entry with more metadata
                better = v if _richness(v) > _richness(existing) else existing
                new_lib[idx] = better
                dedupe_merged += 1
                continue

            if fp_key:
                seen_filepaths[fp_key] = len(new_lib)
            new_lib.append(v)

        self._store.set('library', new_lib)
        return {
            'fixed_paths': fixed_paths,
            'dropped_broken': dropped_broken,
            'dropped_queue_conflict': dropped_queue_conflict,
            'deduped': dedupe_merged,
            'flattened_playlists': flattened_playlists,
            'final_count': len(new_lib)
        }

    # ============================================================
    # YouTube refetch — enriches imported library entries by searching YouTube
    # for their titles and pulling thumbnail, channel, and real URL.
    # ============================================================
    def refetch_count_needs(self):
        """Count library entries that would benefit from a refetch (imported + missing URL
        or missing thumbnail). Called by the UI to show 'Refetch 37 videos' or similar."""
        lib = self.settings.get('library', [])
        count = 0
        for v in lib:
            if v.get('type') == 'playlist':
                for c in v.get('videos', []):
                    if self._needs_refetch(c):
                        count += 1
            else:
                if self._needs_refetch(v):
                    count += 1
        return count

    def _needs_refetch(self, video):
        """True if a video entry would benefit from a refetch.
        Either missing URL/thumbnail entirely, OR was previously refetched with the
        old (less accurate) v1 matcher and hasn't been upgraded yet."""
        if not video.get('imported') and not video.get('_refetched'):
            # Native ProTube downloads have everything they need from the start
            return False
        if not video.get('url'):
            return True
        if not video.get('thumbnail'):
            return True
        # Items refetched with v1 matcher get re-run by v2 (better matching)
        if video.get('_refetched') and not video.get('_refetched_v2'):
            return True
        return False

    def _extract_youtube_id(self, url):
        """Pull the 11-character video ID out of a YouTube URL. Returns None if the URL
        isn't a recognizable YouTube URL. Handles youtube.com/watch?v=..., youtu.be/...,
        youtube.com/shorts/..., and youtube.com/embed/... formats."""
        if not url:
            return None
        try:
            import re as _re
            patterns = [
                r'(?:youtube\.com/watch\?(?:[^&]*&)*v=)([A-Za-z0-9_\-]{11})',
                r'(?:youtu\.be/)([A-Za-z0-9_\-]{11})',
                r'(?:youtube\.com/shorts/)([A-Za-z0-9_\-]{11})',
                r'(?:youtube\.com/embed/)([A-Za-z0-9_\-]{11})',
                r'(?:youtube\.com/v/)([A-Za-z0-9_\-]{11})',
            ]
            for p in patterns:
                m = _re.search(p, url)
                if m:
                    return m.group(1)
        except Exception:
            pass
        return None

    def _fetch_by_id(self, video_id):
        """Fetch metadata for a specific YouTube video by ID. This is the most reliable
        path — we know exactly which video to grab, no search-and-guess. Returns dict
        with url/thumbnail/uploader/duration_string/matched_title or {'error': ...}."""
        if not video_id or len(video_id) != 11:
            return {'error': 'Invalid video ID'}
        opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'noprogress': True,
        }
        if self.ffmpeg_location:
            opts['ffmpeg_location'] = self.ffmpeg_location
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f'https://www.youtube.com/watch?v={video_id}', download=False)
        except Exception as e:
            return {'error': f'Direct fetch failed: {str(e)[:120]}'}
        if not info:
            return {'error': 'No info returned'}

        thumbnail = info.get('thumbnail') or ''
        if not thumbnail:
            thumbs = info.get('thumbnails') or []
            if thumbs:
                thumbnail = thumbs[-1].get('url', '')

        return {
            'url': info.get('webpage_url') or f'https://www.youtube.com/watch?v={video_id}',
            'thumbnail': thumbnail,
            'uploader': info.get('uploader') or info.get('channel') or '',
            'duration_string': info.get('duration_string') or self._download_svc._format_duration(info.get('duration')) or '',
            'matched_title': info.get('title') or '',
            'duration_seconds': info.get('duration') or 0,
            'method': 'id',
        }

    def _title_similarity(self, a, b):
        """Returns 0.0-1.0 similarity between two title strings. Uses character-level
        ratio after normalization (lowercased, special chars stripped). Robust to typos,
        emoji removal during yt-dlp filename sanitization, etc."""
        if not a or not b:
            return 0.0
        import re as _re
        from difflib import SequenceMatcher
        def norm(s):
            s = s.lower()
            # Strip non-alphanumeric except spaces (mirrors yt-dlp's restriction roughly)
            s = _re.sub(r'[^a-z0-9 ]+', ' ', s)
            s = _re.sub(r'\s+', ' ', s).strip()
            return s
        return SequenceMatcher(None, norm(a), norm(b)).ratio()

    def _search_and_extract(self, title, target_duration_sec=None):
        """Search YouTube for a title and pick the BEST match using a scoring function.
        Considers: title similarity, duration match (if we know the local file's duration),
        channel name presence. Refuses to pick a result that scores too low — better to
        flag 'no good match' than to confidently pick the wrong video.

        Returns metadata dict on success, or {'error': '...'} on failure."""
        # ytsearch5: returns top 5 candidates so we can score and pick the best
        search_url = f'ytsearch5:{title}'
        opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'extract_flat': False,
            'noprogress': True,
        }
        if self.ffmpeg_location:
            opts['ffmpeg_location'] = self.ffmpeg_location

        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(search_url, download=False)
        except Exception as e:
            return {'error': f'Search failed: {str(e)[:120]}'}

        entries = info.get('entries') if info else None
        if not entries:
            return {'error': 'No search results'}

        # Score each candidate
        best = None
        best_score = 0.0
        for entry in entries:
            if not entry:
                continue
            entry_title = entry.get('title') or ''
            entry_duration = entry.get('duration') or 0

            # Title similarity (0.0 - 1.0). Weight: 0.7
            title_sim = self._title_similarity(title, entry_title)

            # Duration similarity (0.0 - 1.0). Weight: 0.3
            # If we have a target duration and the candidate's duration matches within
            # 5% (or 5 seconds for short videos), give full points. Otherwise scale linearly.
            duration_sim = 0.5  # neutral if no target
            if target_duration_sec and target_duration_sec > 0 and entry_duration > 0:
                diff = abs(target_duration_sec - entry_duration)
                tolerance = max(5, target_duration_sec * 0.05)
                if diff <= tolerance:
                    duration_sim = 1.0
                elif diff <= target_duration_sec * 0.20:
                    duration_sim = 0.6
                else:
                    duration_sim = max(0.0, 1.0 - (diff / target_duration_sec))

            score = (title_sim * 0.7) + (duration_sim * 0.3)
            if score > best_score:
                best_score = score
                best = entry

        # Threshold: refuse to commit to a match that's clearly wrong.
        # title_sim alone of 0.5 means "rough word overlap" — below that is suspect.
        # Combined with the weights, a total score of 0.45 is the floor.
        if not best or best_score < 0.45:
            return {'error': f'No confident match (best score {best_score:.2f})'}

        thumbnail = best.get('thumbnail') or ''
        if not thumbnail:
            thumbs = best.get('thumbnails') or []
            if thumbs:
                thumbnail = thumbs[-1].get('url', '')

        return {
            'url': best.get('webpage_url') or best.get('url') or '',
            'thumbnail': thumbnail,
            'uploader': best.get('uploader') or best.get('channel') or '',
            'duration_string': best.get('duration_string') or self._download_svc._format_duration(best.get('duration')) or '',
            'matched_title': best.get('title') or '',
            'duration_seconds': best.get('duration') or 0,
            'method': 'search',
            'score': round(best_score, 3),
        }

    def _resolve_video_metadata(self, video_entry):
        """Single entry point that decides which strategy to use and returns metadata.
        Tries Layer 1 (direct ID lookup) first if we have a URL with extractable ID.
        Falls back to Layer 2 (scored title search). Probes local file duration if
        we don't have one stored, to improve match accuracy."""
        # Layer 1 — direct ID lookup
        existing_url = video_entry.get('url') or ''
        yt_id = self._extract_youtube_id(existing_url)
        if yt_id:
            result = self._fetch_by_id(yt_id)
            if not result.get('error'):
                return result
            # If ID lookup failed (private video, deleted, network) fall through to search

        # Layer 2 — scored search. Use file duration as additional signal if available.
        target_duration = None
        # Already have duration_string? parse it back to seconds
        dur_str = video_entry.get('duration_string') or ''
        if dur_str:
            target_duration = self._parse_duration_string(dur_str)
        # No stored duration but we have a filepath? Probe the file via ffprobe.
        if not target_duration and video_entry.get('filepath'):
            target_duration = self._probe_duration(video_entry['filepath'])

        title = video_entry.get('title', '').strip()
        if not title:
            return {'error': 'No title to search with'}

        return self._search_and_extract(title, target_duration_sec=target_duration)

    def _parse_duration_string(self, dur_str):
        """Parse '1:23:45' or '12:34' or '34' into seconds. Returns None if unparseable."""
        if not dur_str:
            return None
        try:
            parts = str(dur_str).strip().split(':')
            parts = [int(p) for p in parts]
            if len(parts) == 3:
                return parts[0] * 3600 + parts[1] * 60 + parts[2]
            elif len(parts) == 2:
                return parts[0] * 60 + parts[1]
            elif len(parts) == 1:
                return parts[0]
        except (ValueError, AttributeError):
            pass
        return None

    def refetch_single(self, video_id):
        """Refetch metadata for a single library entry by id. Searches YouTube by title,
        grabs the top result, updates the library entry. Returns the updated entry or
        an error dict. Used for individual retry if bulk refetch mismatches."""
        lib = self.settings.get('library', [])
        target = None
        target_container = None  # reference for saving the mutation
        for v in lib:
            if v.get('id') == video_id:
                target = v
                target_container = v
                break
            if v.get('type') == 'playlist':
                for c in v.get('videos', []):
                    if c.get('id') == video_id:
                        target = c
                        break
                if target:
                    break
        if not target:
            return {'error': 'Video not found in library', 'id': video_id}

        result = self._resolve_video_metadata(target)
        if result.get('error'):
            return result

        # Merge result into target; preserve filepath, folderpath, id, title (don't overwrite
        # local title with whatever YouTube has — user might have renamed the file)
        if result.get('url'):
            target['url'] = result['url']
        if result.get('thumbnail'):
            # Helper respects frame_thumbnail_forced (user-pinned frames stay)
            # and clears frame_thumbnail_auto (real thumb beats the fallback).
            self._import_svc._apply_refetched_thumbnail(target, result['thumbnail'])
        if result.get('uploader'):
            target['uploader'] = result['uploader']
        if result.get('duration_string') and not target.get('duration_string'):
            target['duration_string'] = result['duration_string']
        target['_refetched'] = True
        target['_refetched_v2'] = True
        target['_refetch_method'] = result.get('method', 'unknown')
        if 'score' in result:
            target['_refetch_score'] = result['score']

        self._save_settings()
        return {
            'id': video_id,
            'updated': True,
            'matched_title': result.get('matched_title', ''),
            'method': result.get('method'),
            'score': result.get('score'),
        }

    def fix_metadata_from_url(self, video_id, youtube_url):
        """User-driven manual fix: given a library entry id and the correct YouTube URL,
        fetch metadata for THAT exact video and update the library entry. This is the
        bulletproof fix path — no search, no scoring, just direct lookup of the user's
        pasted URL. Returns a result dict with updated metadata or {'error': ...}."""
        if not youtube_url or not isinstance(youtube_url, str):
            return {'error': 'No URL provided'}
        youtube_url = youtube_url.strip()

        # Find the library entry
        lib = self.settings.get('library', [])
        target = None
        for v in lib:
            if v.get('id') == video_id:
                target = v
                break
            if v.get('type') == 'playlist':
                for c in v.get('videos', []):
                    if c.get('id') == video_id:
                        target = c
                        break
                if target:
                    break
        if not target:
            return {'error': 'Video not found in library'}

        # Extract the YouTube ID from the user's URL
        yt_id = self._extract_youtube_id(youtube_url)
        if not yt_id:
            return {'error': 'That URL doesn\'t look like a YouTube video link'}

        # Fetch metadata for that specific video
        result = self._fetch_by_id(yt_id)
        if result.get('error'):
            return result

        # Apply the metadata to the library entry. Title is updated too here
        # because the user is explicitly opting in to "this is the right video"
        # and wants the official title.
        if result.get('url'):
            target['url'] = result['url']
        if result.get('thumbnail'):
            # force_refresh — user explicitly asked for the new thumbnail; bypass
            # the "already cached" shortcut so we actually re-download.
            self._import_svc._apply_refetched_thumbnail(target, result['thumbnail'], force_refresh=True)
        if result.get('uploader'):
            target['uploader'] = result['uploader']
        if result.get('duration_string'):
            target['duration_string'] = result['duration_string']
        if result.get('matched_title'):
            target['title'] = result['matched_title']
        target['_refetched'] = True
        target['_refetched_v2'] = True
        target['_refetch_method'] = 'manual'

        self._save_settings()
        return {
            'id': video_id,
            'updated': True,
            'title': result.get('matched_title', ''),
            'uploader': result.get('uploader', ''),
            'url': result.get('url', ''),
        }

    def force_refetch_now(self):
        """Force-clear the v2 flag and immediately re-run refetch with the new matcher.
        Also clears _refetched_v2 from every library item so they all re-process. Useful
        when user wants to manually retrigger after auto-refetch didn't run or didn't help."""
        self.settings.pop('_library_refetched_v2', None)
        lib = self.settings.get('library', [])
        cleared = 0
        for v in lib:
            if v.get('type') == 'playlist':
                for c in v.get('videos', []):
                    if c.pop('_refetched_v2', None):
                        cleared += 1
            else:
                if v.pop('_refetched_v2', None):
                    cleared += 1
        self._save_settings()
        # Now run the refetch
        return self.refetch_all()

    def refetch_all(self):
        """Bulk refetch for all library entries that need it. This is a blocking call —
        the frontend should show a spinner/progress toast while it runs. Returns a summary
        with counts of successes, failures, and the updated library."""
        lib = self.settings.get('library', [])
        updated_count = 0
        failed = []

        # Flatten the targets so we hit playlist children too
        targets = []
        for v in lib:
            if v.get('type') == 'playlist':
                for c in v.get('videos', []):
                    if self._needs_refetch(c):
                        targets.append(c)
            else:
                if self._needs_refetch(v):
                    targets.append(v)

        total = len(targets)
        method_counts = {'id': 0, 'search': 0}
        for i, target in enumerate(targets):
            self._send_to_js('refetchProgress', i + 1, total, target.get('title', ''))

            title = target.get('title', '').strip()
            if not title:
                failed.append({'id': target.get('id'), 'reason': 'no title'})
                continue

            try:
                result = self._resolve_video_metadata(target)
                if result.get('error'):
                    failed.append({
                        'id': target.get('id'),
                        'title': title[:60],
                        'reason': result['error']
                    })
                    continue
                if result.get('url'):
                    target['url'] = result['url']
                if result.get('thumbnail'):
                    self._import_svc._apply_refetched_thumbnail(target, result['thumbnail'])
                if result.get('uploader'):
                    target['uploader'] = result['uploader']
                if result.get('duration_string') and not target.get('duration_string'):
                    target['duration_string'] = result['duration_string']
                target['_refetched'] = True
                target['_refetched_v2'] = True
                method = result.get('method', 'unknown')
                target['_refetch_method'] = method
                if 'score' in result:
                    target['_refetch_score'] = result['score']
                method_counts[method] = method_counts.get(method, 0) + 1
                updated_count += 1
            except Exception as e:
                failed.append({'id': target.get('id'), 'reason': str(e)[:100]})

        self._save_settings()
        self._send_to_js('refetchComplete', updated_count, len(failed))
        return {
            'total': total,
            'updated': updated_count,
            'failed_count': len(failed),
            'failed': failed[:10],
            'methods': method_counts,
        }

    def _cache_thumbnail(self, remote_url, video_id, force_refresh=False):
        """Download a thumbnail image and save it locally. Returns an opaque marker that
        the frontend can send back to get_thumbnail_data() to retrieve the actual image
        bytes. We DON'T return a file:// URL because pywebview's webview blocks loading
        local files as <img src>. Instead we use a scheme 'pt:thumb:<video_id>' that the
        frontend resolves on render by calling back to the Python API for base64 data.

        force_refresh=True bypasses the "already cached" shortcut — used when we KNOW
        the remote thumbnail has changed (e.g. user manually fixed metadata to point at
        a different YouTube video). Without this, a stale cached image stays forever.

        On failure, returns the remote URL (still works online) or empty string."""
        if not remote_url or not video_id:
            return remote_url or ''
        safe_id = re.sub(r'[^a-zA-Z0-9_\-]', '_', str(video_id))[:80]
        if not safe_id:
            return remote_url

        # Guess extension from URL
        ext = '.jpg'
        low = remote_url.lower().split('?')[0]
        for candidate in ('.webp', '.png', '.jpg', '.jpeg'):
            if low.endswith(candidate):
                ext = candidate
                break

        local_path = os.path.join(self.thumbnail_cache_dir, f'{safe_id}{ext}')

        # Reuse cached file unless caller explicitly asked for a fresh download.
        if not force_refresh and os.path.exists(local_path) and os.path.getsize(local_path) > 100:
            return f'pt:thumb:{safe_id}{ext}'

        # When force_refresh, also clear any older-extension cached files for this id
        # so a stale .webp doesn't sit alongside a new .jpg
        if force_refresh:
            for old_ext in ('.webp', '.png', '.jpg', '.jpeg'):
                stale = os.path.join(self.thumbnail_cache_dir, f'{safe_id}{old_ext}')
                if stale != local_path and os.path.exists(stale):
                    try:
                        os.remove(stale)
                    except OSError:
                        pass

        try:
            resp = requests.get(remote_url, timeout=15, stream=True)
            if resp.status_code != 200:
                return remote_url
            with open(local_path, 'wb') as f:
                for chunk in resp.iter_content(8192):
                    if chunk:
                        f.write(chunk)
            if os.path.getsize(local_path) < 100:
                try:
                    os.remove(local_path)
                except OSError:
                    pass
                return remote_url
            return f'pt:thumb:{safe_id}{ext}'
        except Exception:
            return remote_url

    def migrate_file_thumbnails_to_markers(self):
        """One-time migration: existing library entries may have thumbnails stored as
        'file:///C:/Users/.../thumbnails/abc.jpg' URLs that the webview can't render.
        Convert those to 'pt:thumb:abc.jpg' markers which the frontend resolves through
        get_thumbnail_data(). Returns the count that was migrated."""
        lib = self.settings.get('library', [])
        changed = 0

        def convert_one(entry):
            nonlocal changed
            thumb = entry.get('thumbnail') or ''
            if thumb.startswith('file:///') and 'thumbnails/' in thumb.replace('\\', '/'):
                # Extract filename after 'thumbnails/'
                normalized = thumb.replace('\\', '/')
                idx = normalized.rfind('thumbnails/')
                if idx >= 0:
                    filename = normalized[idx + len('thumbnails/'):]
                    filename = filename.split('?')[0].split('#')[0]
                    entry['thumbnail'] = f'pt:thumb:{filename}'
                    changed += 1

        for item in lib:
            if item.get('type') == 'playlist':
                for child in item.get('videos', []):
                    convert_one(child)
            else:
                convert_one(item)

        if changed:
            self._save_settings()
        return {'migrated': changed}

    def get_all_thumbnails(self):
        """Return a {marker: data_url} dict for every library thumbnail. Used at app
        launch to pre-warm the frontend's _thumbCache so cards render with thumbnails
        already in place — no flash, no per-card backend round-trip on first paint."""
        result = {}
        lib = self.settings.get('library', [])

        def collect(entry):
            thumb = entry.get('thumbnail') or ''
            if thumb.startswith('pt:thumb:'):
                data = self.get_thumbnail_data(thumb)
                if data:
                    result[thumb] = data

        for v in lib:
            if v.get('type') == 'playlist':
                # Playlists may have their own thumbs (the first child's), and children
                # have their own. Collect both since either may be rendered.
                collect(v)
                for c in v.get('videos', []):
                    collect(c)
            else:
                collect(v)
        return result

    def cache_remote_thumb_on_demand(self, video_id, remote_url):
        """Frontend calls this when it encounters a library item with a remote-URL
        thumbnail. Downloads + caches, then persists the new marker back to settings
        so future loads skip this round-trip. Returns the marker (or empty string
        on failure / no internet)."""
        if not video_id or not remote_url:
            return ''
        if remote_url.startswith('pt:thumb:'):
            return remote_url  # already cached
        if not (remote_url.startswith('http://') or remote_url.startswith('https://')):
            return ''
        marker = self._cache_thumbnail(remote_url, video_id)
        if not marker or not marker.startswith('pt:thumb:'):
            return ''  # caching failed (offline most likely)

        # Persist the new marker into the library so we don't refetch
        lib = self.settings.get('library', [])
        changed = False
        for v in lib:
            if v.get('id') == video_id:
                v['thumbnail'] = marker
                changed = True
                break
            if v.get('type') == 'playlist':
                for c in v.get('videos', []):
                    if c.get('id') == video_id:
                        c['thumbnail'] = marker
                        changed = True
                        break
        if changed:
            self._save_settings()
        return marker

    def get_thumbnail_data(self, marker):
        """Resolve a 'pt:thumb:<filename>' marker to a base64 data URL the frontend
        can use as <img src>. Returns empty string if not found.
        Also accepts bare file basenames for resilience."""
        if not marker:
            return ''
        # Strip the prefix if present
        if marker.startswith('pt:thumb:'):
            filename = marker[len('pt:thumb:'):]
        else:
            filename = marker
        # Security: make sure we only read from inside the cache dir
        filename = os.path.basename(filename)  # strips any path components
        full_path = os.path.join(self.thumbnail_cache_dir, filename)
        if not os.path.exists(full_path):
            return ''
        try:
            with open(full_path, 'rb') as f:
                data = f.read()
            if not data:
                return ''
            # Pick MIME type from extension
            ext = os.path.splitext(filename)[1].lower()
            mime = {
                '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                '.png': 'image/png', '.webp': 'image/webp'
            }.get(ext, 'image/jpeg')
            import base64
            b64 = base64.b64encode(data).decode('ascii')
            return f'data:{mime};base64,{b64}'
        except OSError:
            return ''

