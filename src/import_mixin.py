import os, re, sys, time, threading, subprocess, io


class ImportMixin:
    # ============================================================
    # Import from folder — scans a disk folder and builds library entries
    # from video files found there. Used to rebuild library from files that
    # exist on disk but aren't in the app's memory (e.g. after a data loss).
    # ============================================================
    VIDEO_EXTS = ('.mp4', '.mkv', '.webm', '.m4a', '.mov', '.avi', '.flv')

    def scan_folder_preview(self, folder=None):
        """Walk the folder (defaults to current download folder), count how many
        videos would be imported. Returns a preview summary the UI can show before
        the user commits to the import.

        A subfolder is treated as a 'playlist' ONLY if it contains more than one video.
        Single-video subfolders are how ProTube organizes individual downloads (one folder
        per video) — those should become standalone entries, not 1-video playlists."""
        target = folder or self.download_folder
        if not target or not os.path.isdir(target):
            return {'error': 'Folder does not exist', 'folder': target}

        standalone = 0
        playlists = {}  # subfolder_name -> list of video filenames (only when >1)
        total_bytes = 0

        try:
            # Scan top-level files first
            for entry in os.scandir(target):
                if entry.is_file() and entry.name.lower().endswith(self.VIDEO_EXTS):
                    standalone += 1
                    try:
                        total_bytes += entry.stat().st_size
                    except OSError:
                        pass
                elif entry.is_dir():
                    children = []
                    try:
                        for sub in os.scandir(entry.path):
                            if sub.is_file() and sub.name.lower().endswith(self.VIDEO_EXTS):
                                children.append(sub.name)
                                try:
                                    total_bytes += sub.stat().st_size
                                except OSError:
                                    pass
                    except OSError:
                        continue
                    if len(children) > 1:
                        playlists[entry.name] = children
                    elif len(children) == 1:
                        # Single-video folder — treat as standalone
                        standalone += 1
        except OSError as e:
            return {'error': str(e), 'folder': target}

        total = standalone + sum(len(v) for v in playlists.values())
        return {
            'folder': target,
            'standalone_count': standalone,
            'playlist_count': len(playlists),
            'total_videos': total,
            'total_bytes': total_bytes,
            'playlist_names': list(playlists.keys())[:10]
        }

    def scan_folder_full(self, folder=None):
        """Walk the folder and return every video file grouped into standalone + playlists,
        with the per-file detail the import-picker UI needs (name, path, size, and whether
        the file is already in library or has archived metadata waiting to be restored).

        Cheap scan — no ffprobe. Single-video subfolders are treated as standalone, matching
        scan_folder_preview's behavior. Items already in library are still listed but flagged
        in_library=True so the UI can render them as disabled."""
        target = folder or self.download_folder
        if not target or not os.path.isdir(target):
            return {'error': 'Folder does not exist', 'folder': target}

        # Build the lookups once: known filepaths in library, archived filepaths
        library = self.settings.get('library', [])
        in_lib = set()
        for v in library:
            if v.get('type') == 'playlist':
                for c in v.get('videos', []):
                    key = self._archive_key(c.get('filepath'))
                    if key:
                        in_lib.add(key)
            else:
                key = self._archive_key(v.get('filepath'))
                if key:
                    in_lib.add(key)
        archive = self.settings.get('library_archive', {}) or {}

        def _file_entry(path, name):
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            key = self._archive_key(path)
            return {
                'path': path,
                'name': name,
                'size_bytes': size,
                'in_library': key in in_lib if key else False,
                'in_archive': key in archive if key else False,
            }

        standalone = []
        playlists = []
        total_bytes = 0

        try:
            entries = sorted(os.scandir(target), key=lambda e: e.name.lower())
        except OSError as e:
            return {'error': str(e), 'folder': target}

        for entry in entries:
            try:
                if entry.is_file() and entry.name.lower().endswith(self.VIDEO_EXTS):
                    item = _file_entry(entry.path, entry.name)
                    total_bytes += item['size_bytes']
                    standalone.append(item)
                elif entry.is_dir():
                    try:
                        sub_entries = sorted(os.scandir(entry.path), key=lambda e: e.name.lower())
                    except OSError:
                        continue
                    video_subs = [s for s in sub_entries
                                  if s.is_file() and s.name.lower().endswith(self.VIDEO_EXTS)]
                    if len(video_subs) == 1:
                        # Single-video folder — treat as standalone
                        sub = video_subs[0]
                        item = _file_entry(sub.path, sub.name)
                        total_bytes += item['size_bytes']
                        standalone.append(item)
                    elif len(video_subs) > 1:
                        children = []
                        for sub in video_subs:
                            item = _file_entry(sub.path, sub.name)
                            total_bytes += item['size_bytes']
                            children.append(item)
                        playlists.append({
                            'name': entry.name,
                            'folder_path': entry.path,
                            'videos': children,
                        })
            except OSError:
                continue

        total_videos = len(standalone) + sum(len(p['videos']) for p in playlists)
        return {
            'folder': target,
            'standalone': standalone,
            'playlists': playlists,
            'total_videos': total_videos,
            'total_bytes': total_bytes,
        }

    def import_from_folder(self, folder=None, merge=True, selected_paths=None):
        """Walk the folder and build library entries for every video file found.
        - Top-level files become standalone library entries
        - Subfolders become playlists (folder name = playlist title, contents = children)
        - If merge=True, skip items whose filepath is already in the library
        - If selected_paths is a list, only import files whose normalized path is in
          that set (lets the UI offer a checklist instead of all-or-nothing). None
          imports everything.
        - Returns counts so the UI can toast a summary.

        Progress is tracked in self._import_progress (current/total/phase) so the
        frontend can poll get_import_progress() while this runs and show a bar.
        Per-file ffprobe is the slow part; pre-counting lets us give a denominator."""
        self._import_progress = {'current': 0, 'total': 0, 'phase': 'counting'}
        target = folder or self.download_folder
        if not target or not os.path.isdir(target):
            self._import_progress = {'current': 0, 'total': 0, 'phase': 'error'}
            return {'error': 'Folder does not exist', 'folder': target}

        library = self.settings.get('library', [])

        # When selected_paths is provided, we only walk files whose filepath is in the
        # set. None means "import everything" (legacy behavior).
        selected_set = None
        if selected_paths is not None:
            selected_set = set()
            for p in selected_paths:
                key = self._archive_key(p)
                if key:
                    selected_set.add(key)

        def _should_import(path):
            if selected_set is None:
                return True
            key = self._archive_key(path)
            return bool(key) and key in selected_set

        # Build set of known filepaths so we don't duplicate entries on re-import
        known_paths = set()
        if merge:
            for v in library:
                if v.get('type') == 'playlist':
                    for c in v.get('videos', []):
                        if c.get('filepath'):
                            known_paths.add(os.path.normcase(os.path.normpath(c['filepath'])))
                elif v.get('filepath'):
                    known_paths.add(os.path.normcase(os.path.normpath(v['filepath'])))

        # Pre-count total videos so the UI has a denominator. Cheap (no ffprobe).
        # Honors selected_paths so the bar reflects only what we'll actually process.
        total_count = 0
        try:
            for entry in os.scandir(target):
                if entry.is_file() and entry.name.lower().endswith(self.VIDEO_EXTS):
                    if _should_import(entry.path):
                        total_count += 1
                elif entry.is_dir():
                    try:
                        for sub in os.scandir(entry.path):
                            if sub.is_file() and sub.name.lower().endswith(self.VIDEO_EXTS):
                                if _should_import(sub.path):
                                    total_count += 1
                    except OSError:
                        pass
        except OSError:
            pass
        self._import_progress = {'current': 0, 'total': total_count, 'phase': 'importing'}

        imported_videos = 0
        imported_playlists = 0
        skipped = 0

        try:
            for entry in os.scandir(target):
                if entry.is_file() and entry.name.lower().endswith(self.VIDEO_EXTS):
                    # Standalone video
                    if not _should_import(entry.path):
                        continue
                    fp_key = os.path.normcase(os.path.normpath(entry.path))
                    if fp_key in known_paths:
                        skipped += 1
                        self._import_progress['current'] += 1
                        continue
                    video = self._build_video_entry_from_file(entry.path, parent_folder=target)
                    if video:
                        library.append(video)
                        known_paths.add(fp_key)
                        imported_videos += 1
                    self._import_progress['current'] += 1

                elif entry.is_dir():
                    # Collect videos in this subfolder
                    try:
                        sub_entries = sorted(os.scandir(entry.path), key=lambda e: e.name.lower())
                    except OSError:
                        continue
                    video_subs = [s for s in sub_entries
                                  if s.is_file() and s.name.lower().endswith(self.VIDEO_EXTS)]

                    if len(video_subs) == 1:
                        # Single-video folder — treat as standalone, not a 1-item playlist.
                        # ProTube saves each downloaded video into its own folder by default.
                        sub = video_subs[0]
                        if not _should_import(sub.path):
                            continue
                        fp_key = os.path.normcase(os.path.normpath(sub.path))
                        if fp_key in known_paths:
                            skipped += 1
                            self._import_progress['current'] += 1
                            continue
                        video = self._build_video_entry_from_file(sub.path, parent_folder=entry.path)
                        if video:
                            library.append(video)
                            known_paths.add(fp_key)
                            imported_videos += 1
                        self._import_progress['current'] += 1
                    elif len(video_subs) > 1:
                        # Multi-video folder → real playlist (built only from selected children)
                        children = []
                        for sub in video_subs:
                            if not _should_import(sub.path):
                                continue
                            fp_key = os.path.normcase(os.path.normpath(sub.path))
                            if fp_key in known_paths:
                                skipped += 1
                                self._import_progress['current'] += 1
                                continue
                            child = self._build_video_entry_from_file(sub.path, parent_folder=entry.path)
                            if child:
                                child['isFromPlaylist'] = True
                                child['playlistTitle'] = entry.name
                                children.append(child)
                                known_paths.add(fp_key)
                            self._import_progress['current'] += 1

                        if children:
                            # Sanitize folder name before baking it into the ID.
                            # Imported playlist IDs have historically been the only
                            # source of user-controlled characters in entry IDs —
                            # YouTube IDs are alphanumeric, but a folder name can
                            # contain anything (`'`, `"`, `<`, even spaces). Without
                            # this, the ID would later flow into inline `onclick="
                            # foo('${id}')"` templates and a folder named `Bob's`
                            # would inject JS via the `'`. Strip everything outside
                            # `[a-zA-Z0-9_-]` and cap length so even pathological
                            # folder names produce a clean, safe identifier.
                            safe_name = re.sub(r'[^A-Za-z0-9_-]', '_', entry.name)[:50]
                            playlist_id = f'imported_{safe_name}_{abs(hash(entry.path)) % (10**9)}'
                            playlist = {
                                'type': 'playlist',
                                'id': playlist_id,
                                'title': entry.name,
                                'uploader': '',
                                'status': 'Done',
                                'imported': True,
                                'videos': children,
                                'thumbnails': [],
                                # Stamp so the playlist also fires the NEW badge.
                                # Children already get their own added_at via
                                # _build_video_entry_from_file.
                                'added_at': int(time.time()),
                            }
                            library.append(playlist)
                            imported_playlists += 1
                            imported_videos += len(children)
        except OSError as e:
            self._import_progress = {'current': 0, 'total': 0, 'phase': 'error'}
            return {'error': str(e), 'folder': target}

        self._store.set('library', library)
        self._import_progress = {'current': total_count, 'total': total_count, 'phase': 'done'}

        # Kick off a background pass to extract frame thumbnails for the
        # imported videos. Runs in a daemon thread so import_from_folder can
        # return immediately. Auto-extracted frames get replaced if/when the
        # user later refetches metadata and gets a real YouTube thumb.
        self._start_frame_extraction_worker()

        return {
            'folder': target,
            'imported_videos': imported_videos,
            'imported_playlists': imported_playlists,
            'skipped': skipped
        }

    def get_import_progress(self):
        """Snapshot of the current import_from_folder progress for UI polling.
        Returns {current, total, phase} where phase is one of:
        'idle' | 'counting' | 'importing' | 'done' | 'error'."""
        return getattr(self, '_import_progress',
                       {'current': 0, 'total': 0, 'phase': 'idle'})

    def _build_video_entry_from_file(self, filepath, parent_folder):
        """Build a library entry for a single video file on disk. Uses ffprobe for
        duration when available, otherwise leaves it blank. Never raises — returns
        None if the file can't be read.

        If a previous library entry for this filepath was archived (via remove_from_library),
        restore it instead of building a sparse new entry. This preserves the original
        title/url/thumbnail/uploader/formats so the user doesn't have to refetch metadata
        after a remove → re-import cycle."""
        try:
            if not os.path.isfile(filepath):
                return None

            archive = self.settings.get('library_archive', {})
            arc_key = self._archive_key(filepath)
            archived = archive.get(arc_key) if arc_key else None
            if archived:
                restored = dict(archived)
                # Refresh disk-derived fields in case the file changed since archival
                try:
                    new_size = os.path.getsize(filepath)
                    if isinstance(restored.get('sizeMap'), dict):
                        sel = restored.get('selectedQuality') or 'imported'
                        restored['sizeMap'] = dict(restored['sizeMap'])
                        restored['sizeMap'][sel] = new_size
                    else:
                        restored['sizeMap'] = {restored.get('selectedQuality') or 'imported': new_size}
                except OSError:
                    pass
                restored['filepath'] = filepath
                restored['folderpath'] = parent_folder
                restored['missing'] = False
                # Re-stamp added_at to "now" so the NEW badge fires again — the user
                # is importing it fresh into their library, even if it was previously
                # archived. From their POV this is a new addition.
                restored['added_at'] = int(time.time())
                # Drop the archive entry now that it's back in the active library
                try:
                    del archive[arc_key]
                except KeyError:
                    pass
                return restored

            filename = os.path.basename(filepath)
            name_no_ext = os.path.splitext(filename)[0]
            size = 0
            try:
                size = os.path.getsize(filepath)
            except OSError:
                pass

            duration_str = self._probe_duration(filepath)

            # Use a stable id based on filepath so re-imports dedupe correctly
            stable_id = 'imported_' + str(abs(hash(os.path.normcase(os.path.normpath(filepath)))) % (10**12))

            return {
                'type': 'video',
                'id': stable_id,
                'title': name_no_ext,
                'uploader': '',
                'thumbnail': '',
                'url': '',
                'status': 'Done',
                'imported': True,
                'filepath': filepath,
                'folderpath': parent_folder,
                'duration_string': duration_str or '',
                'sizeMap': {'imported': size},
                'selectedQuality': 'imported',
                'formats': [],
                'missing': False,
                # Stamp so the "NEW" badge (48h window) fires for freshly imported
                # videos — matches the behavior of add_to_library / add_playlist_to_library.
                'added_at': int(time.time()),
            }
        except Exception:
            return None

    def _probe_duration(self, filepath):
        """Return a duration string like '26:16' for a video file using ffprobe.
        Returns empty string if ffprobe isn't available or the probe fails."""
        ffprobe = self._find_ffprobe()
        if not ffprobe:
            return ''
        try:
            result = subprocess.run(
                [ffprobe, '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', filepath],
                capture_output=True, text=True, timeout=10,
                creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
            )
            if result.returncode != 0:
                return ''
            seconds = float(result.stdout.strip() or 0)
            if seconds <= 0:
                return ''
            h = int(seconds // 3600)
            m = int((seconds % 3600) // 60)
            s = int(seconds % 60)
            if h > 0:
                return f'{h}:{m:02d}:{s:02d}'
            return f'{m}:{s:02d}'
        except (subprocess.TimeoutExpired, ValueError, OSError):
            return ''

    def _find_ffprobe(self):
        """Locate ffprobe similar to how _resolve_ffmpeg_location handles ffmpeg."""
        exe = 'ffprobe.exe' if sys.platform == 'win32' else 'ffprobe'
        if hasattr(sys, '_MEIPASS'):
            bundled = os.path.join(sys._MEIPASS, exe)
            if os.path.exists(bundled):
                return bundled
        if sys.platform == 'darwin':
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            local = os.path.join(root, 'assets', 'mac', 'ffprobe')
            if os.path.isfile(local):
                return local
        for path_dir in os.environ.get('PATH', '').split(os.pathsep):
            candidate = os.path.join(path_dir, exe)
            if os.path.isfile(candidate):
                return candidate
        return None

    def _find_ffmpeg_exe(self):
        """Locate the ffmpeg executable. self.ffmpeg_location stores a directory
        (yt-dlp wants a directory), but for direct invocation we need the actual
        ffmpeg(.exe) path."""
        exe = 'ffmpeg.exe' if sys.platform == 'win32' else 'ffmpeg'
        if hasattr(sys, '_MEIPASS'):
            bundled = os.path.join(sys._MEIPASS, exe)
            if os.path.exists(bundled):
                return bundled
        if sys.platform == 'darwin':
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            local = os.path.join(root, 'assets', 'mac', 'ffmpeg')
            if os.path.isfile(local):
                return local
        for path_dir in os.environ.get('PATH', '').split(os.pathsep):
            candidate = os.path.join(path_dir, exe)
            if os.path.isfile(candidate):
                return candidate
        return None

    # ============================================================
    # Auto frame thumbnails — extract a representative frame from a
    # video file and use it as the thumbnail for imported entries
    # that lack a YouTube thumb. Two flags track origin:
    #   frame_thumbnail_auto   = True  -> extracted as fallback,
    #                                     metadata refetch may replace it.
    #   frame_thumbnail_forced = True  -> user explicitly pinned this
    #                                     frame, refetch leaves it alone.
    # ============================================================
    def _duration_string_to_seconds(self, s):
        """'15:26' -> 926.0, '1:23:45' -> 5025.0. Returns None on parse error."""
        if not s:
            return None
        try:
            parts = [int(p) for p in str(s).split(':')]
        except ValueError:
            return None
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        if len(parts) == 1:
            return parts[0]
        return None

    def _extract_video_frame(self, filepath, video_id, timestamp_sec=5.0, force_refresh=False):
        """Run ffmpeg to grab a single frame at `timestamp_sec`, save it to the
        thumbnail cache as <safe_id>.jpg, return a 'pt:thumb:...' marker the
        frontend can resolve. Returns None on failure.

        force_refresh=True will re-encode even if a cached file already exists,
        used by the manual 'Use video frame' action so the user can change the
        timestamp and see the result."""
        if not filepath or not os.path.isfile(filepath) or not video_id:
            return None
        ffmpeg = self._find_ffmpeg_exe()
        if not ffmpeg:
            return None
        safe_id = re.sub(r'[^a-zA-Z0-9_\-]', '_', str(video_id))[:80]
        if not safe_id:
            return None
        local_path = os.path.join(self.thumbnail_cache_dir, f'{safe_id}.jpg')
        if not force_refresh and os.path.exists(local_path) and os.path.getsize(local_path) > 100:
            return f'pt:thumb:{safe_id}.jpg'
        # Clear any older-extension cached file for this id so the .jpg we're
        # about to write becomes canonical.
        if force_refresh:
            for old_ext in ('.webp', '.png', '.jpeg'):
                stale = os.path.join(self.thumbnail_cache_dir, f'{safe_id}{old_ext}')
                if os.path.exists(stale):
                    try:
                        os.remove(stale)
                    except OSError:
                        pass
        try:
            ts = max(0.0, float(timestamp_sec))
        except (TypeError, ValueError):
            ts = 5.0
        try:
            # -ss BEFORE -i = fast input seek. -vframes 1 = one frame.
            # -q:v 4 = good JPEG quality. -y = overwrite output.
            # -an = drop audio. scale=480:-2 keeps 16:9-ish at modest size.
            result = subprocess.run(
                [ffmpeg, '-hide_banner', '-loglevel', 'error',
                 '-ss', f'{ts:.3f}', '-i', filepath,
                 '-frames:v', '1', '-q:v', '4', '-an',
                 '-vf', 'scale=480:-2',
                 '-y', local_path],
                capture_output=True, timeout=30,
                creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
            )
            if result.returncode != 0 or not os.path.exists(local_path):
                return None
            if os.path.getsize(local_path) < 100:
                try:
                    os.remove(local_path)
                except OSError:
                    pass
                return None
            return f'pt:thumb:{safe_id}.jpg'
        except (subprocess.TimeoutExpired, OSError):
            return None

    def _needs_auto_frame_thumb(self, entry):
        """True if entry has a real file on disk and no usable thumbnail.

        Critical: we do NOT skip on the frame_thumbnail_auto flag. That flag is
        informational only — it records that auto-extraction has been attempted,
        not that it succeeded. The thing that actually matters is whether a
        valid thumbnail exists. If extraction failed last time and the entry
        still has no thumbnail, retry. (This is what was breaking 'auto' before:
        the flag got set on every entry on first run, and a single transient
        failure permanently locked the entry out of future retries.)

        Only frame_thumbnail_forced blocks extraction — that's a user-pinned
        choice, we never overwrite it.
        """
        if not entry or entry.get('type') == 'playlist':
            return False
        if entry.get('frame_thumbnail_forced'):
            return False
        fp = entry.get('filepath')
        if not fp or not os.path.isfile(fp):
            return False
        thumb = entry.get('thumbnail') or ''
        if not thumb:
            return True
        # If the marker points at a missing/tiny cache file, treat as no thumb.
        if thumb.startswith('pt:thumb:'):
            cached = os.path.join(self.thumbnail_cache_dir, thumb[len('pt:thumb:'):])
            if not os.path.exists(cached) or os.path.getsize(cached) < 100:
                return True
            return False
        # Any other thumbnail value (remote URL, etc.) — leave it alone
        return False

    def _next_frame_extraction_target(self):
        """Walk the library and return the next entry needing a frame thumb,
        or None when there's nothing left to process."""
        for v in self.settings.get('library', []):
            if v.get('type') == 'playlist':
                for c in v.get('videos', []):
                    if self._needs_auto_frame_thumb(c):
                        return c
            elif self._needs_auto_frame_thumb(v):
                return v
        return None

    def _run_frame_extraction_worker(self):
        """Background worker that processes pending frame extractions one by one.
        Daemon thread, swallows errors per entry so one bad file doesn't stop
        the queue. Saves settings after each successful extraction so progress
        survives a crash. Self-terminates when there's nothing left.

        On failure: we no longer mark the entry. The next worker pass (next
        launch, next import, etc.) will retry. This means a corrupted file
        will burn ~30s of timeout per launch — acceptable. The previous
        approach of marking on failure caused 'auto extraction silently does
        nothing' because one transient failure permanently locked an entry
        out of retries. To prevent runaway retries within a single session,
        we cap consecutive failures at 5 — beyond that, we stop the worker
        and let the next launch try again.
        """
        consecutive_failures = 0
        processed = 0
        succeeded = 0
        self._frame_worker_log('worker started')
        try:
            while True:
                if not self.settings.get('auto_extract_frame_thumbnails', True):
                    self._frame_worker_log('disabled via setting; stopping')
                    break
                target = self._next_frame_extraction_target()
                if not target:
                    break
                processed += 1
                ts = 5.0
                duration_secs = self._duration_string_to_seconds(target.get('duration_string'))
                if duration_secs is not None and duration_secs > 0:
                    if duration_secs < 5:
                        ts = 0.5
                    elif duration_secs < 50:
                        ts = max(1.0, duration_secs * 0.1)
                marker = self._extract_video_frame(target.get('filepath'), target.get('id'), ts)
                if marker:
                    target['thumbnail'] = marker
                    target['frame_thumbnail_auto'] = True
                    try:
                        self._save_settings()
                    except Exception as e:
                        self._frame_worker_log(f'settings save failed: {e}')
                    succeeded += 1
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    self._frame_worker_log(
                        f'extraction failed for "{target.get("title", "?")[:50]}" '
                        f'(filepath={target.get("filepath", "?")[:80]}, ts={ts:.1f}); '
                        f'consecutive_failures={consecutive_failures}'
                    )
                    if consecutive_failures >= 5:
                        self._frame_worker_log('5 consecutive failures, stopping for this session')
                        break
        except Exception as e:
            self._frame_worker_log(f'unexpected error: {e}')
        finally:
            self._frame_worker_running = False
            self._frame_worker_log(f'worker finished: {succeeded}/{processed} succeeded')

    def _frame_worker_log(self, msg):
        """Append a worker-progress line to data/protube.log. Never throws.
        Use this for diagnostics so the user can show what the worker did."""
        try:
            # data/ folder is the parent of thumbnail_cache_dir
            data_folder = os.path.dirname(self.thumbnail_cache_dir)
            log_path = os.path.join(data_folder, 'protube.log')
            with open(log_path, 'a', encoding='utf-8') as f:
                import time as _time
                ts = _time.strftime('%Y-%m-%d %H:%M:%S')
                f.write(f'[{ts}] [frame-worker] {msg}\n')
        except Exception:
            pass

    def _start_frame_extraction_worker(self):
        """Spawn the background worker if it's not already running. Idempotent."""
        if getattr(self, '_frame_worker_running', False):
            self._frame_worker_log('start requested but worker already running')
            return
        if not self.settings.get('auto_extract_frame_thumbnails', True):
            self._frame_worker_log('start requested but auto_extract_frame_thumbnails is disabled')
            return
        ffmpeg = self._find_ffmpeg_exe()
        if not ffmpeg:
            self._frame_worker_log('start requested but ffmpeg not found — skipping')
            return
        # Quick scan for pending entries so we know whether spawning is worth it
        pending = 0
        try:
            for v in self.settings.get('library', []):
                if v.get('type') == 'playlist':
                    for c in v.get('videos', []):
                        if self._needs_auto_frame_thumb(c):
                            pending += 1
                elif self._needs_auto_frame_thumb(v):
                    pending += 1
        except Exception:
            pass
        self._frame_worker_log(f'starting worker (ffmpeg={ffmpeg}, pending={pending})')
        self._frame_worker_running = True
        threading.Thread(target=self._run_frame_extraction_worker, daemon=True).start()

    def get_frame_extraction_status(self):
        """Status snapshot for the UI to poll while the background worker is running.
        Returns {'running': bool, 'pending': int}. The frontend uses this to know
        when to refresh the library and pick up newly-extracted thumbnails — the
        worker mutates settings in place, but the rendered library is a snapshot."""
        pending = 0
        for v in self.settings.get('library', []):
            if v.get('type') == 'playlist':
                for c in v.get('videos', []):
                    if self._needs_auto_frame_thumb(c):
                        pending += 1
            elif self._needs_auto_frame_thumb(v):
                pending += 1
        return {
            'running': bool(getattr(self, '_frame_worker_running', False)),
            'pending': pending,
        }

    def force_video_frame_thumbnail(self, video_id, timestamp_sec=None):
        """Manual override: replace this entry's thumbnail with an extracted
        video frame, regardless of any existing YouTube thumb. Sets the
        forced flag so future metadata refetches can't replace it.

        timestamp_sec: optional float seconds. Defaults to 5.0 (or 10% of
        duration for short videos)."""
        target = None
        for v in self.settings.get('library', []):
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
            return {'ok': False, 'error': 'Video not found in library'}
        fp = target.get('filepath')
        if not fp or not os.path.isfile(fp):
            return {'ok': False, 'error': 'Video file is missing on disk'}
        if not self._find_ffmpeg_exe():
            return {'ok': False, 'error': 'ffmpeg not available'}
        if timestamp_sec is None:
            ts = 5.0
            duration_secs = self._duration_string_to_seconds(target.get('duration_string'))
            if duration_secs is not None and duration_secs > 0 and duration_secs < 50:
                ts = max(1.0, duration_secs * 0.1)
        else:
            try:
                ts = max(0.0, float(timestamp_sec))
            except (TypeError, ValueError):
                ts = 5.0
        marker = self._extract_video_frame(fp, target.get('id'), ts, force_refresh=True)
        if not marker:
            return {'ok': False, 'error': 'Frame extraction failed'}
        target['thumbnail'] = marker
        target['frame_thumbnail_forced'] = True
        target.pop('frame_thumbnail_auto', None)
        self._save_settings()
        return {'ok': True, 'thumbnail': marker}

    def _apply_refetched_thumbnail(self, target, remote_url, force_refresh=False):
        """Helper: cache and apply a refetched YouTube thumbnail to a library
        entry. Skips if the user explicitly pinned a frame thumbnail (forced).
        Clears the auto-frame flag if it was set, since the entry now has a
        real curated thumb."""
        if not remote_url or not target:
            return
        if target.get('frame_thumbnail_forced'):
            return
        cached = self._cache_thumbnail(remote_url, target.get('id'), force_refresh=force_refresh)
        target['thumbnail'] = cached
        target.pop('frame_thumbnail_auto', None)

    def _migrate_queue_done_to_library(self):
        """
        One-time migration: move queue entries with status='Done' into the library.
        Silent purge for entries whose files don't exist on disk.
        Sets _library_migrated=True so this never runs twice.
        """
        queue = self.settings.get('queue', [])
        library = self.settings.get('library', [])
        library_ids = {v.get('id') for v in library}

        moved_count = 0
        purged_count = 0
        new_queue = []

        for item in queue:
            if item.get('type') == 'playlist':
                # Playlist: count how many children are Done. If all done, move playlist.
                # Otherwise leave the playlist in queue (Done children stay inside, will move
                # with the playlist when the rest finish).
                children = item.get('videos', [])
                selected_children = [c for c in children if c.get('selected') is not False]
                all_done = len(selected_children) > 0 and all(
                    c.get('status') == 'Done' for c in selected_children
                )
                if all_done:
                    # Move whole playlist. Flag any with missing files rather than purging,
                    # so the user can see what's gone and decide what to do.
                    for c in selected_children:
                        if self._is_file_missing(c):
                            c['missing'] = True
                    if item.get('id') not in library_ids:
                        item['videos'] = selected_children
                        library.append(item)
                        library_ids.add(item.get('id'))
                        moved_count += len(selected_children)
                    # Don't put back in queue — we moved it
                else:
                    # Some children not done yet; leave playlist in queue
                    new_queue.append(item)
            else:
                # Single video
                if item.get('status') == 'Done':
                    # Move to library. Flag-as-missing rather than purging — if the file
                    # is actually gone, the user sees it marked missing and can decide.
                    if self._is_file_missing(item):
                        item['missing'] = True
                    if item.get('id') not in library_ids:
                        library.append(item)
                        library_ids.add(item.get('id'))
                        moved_count += 1
                    # Don't put back in queue
                else:
                    # Not done: keep in queue
                    new_queue.append(item)

        # Commit changes atomically.
        self._store.update({'library': library, 'queue': new_queue, '_library_migrated': True})

        # Announce to UI if anything moved. Frontend listens for this toast on first load.
        if moved_count > 0 or purged_count > 0:
            # Schedule the toast on the next tick — webview may not be ready yet at startup
            def announce():
                time.sleep(1.2)  # give UI time to finish initial render
                parts = []
                if moved_count > 0:
                    parts.append(f"{moved_count} video{'s' if moved_count != 1 else ''} moved to Library")
                if purged_count > 0:
                    parts.append(f"{purged_count} missing file{'s' if purged_count != 1 else ''} cleaned up")
                msg = " · ".join(parts)
                self._send_to_js('showToast', msg, None, None)
            threading.Thread(target=announce, daemon=True).start()

        return {'moved': moved_count, 'purged': purged_count}

