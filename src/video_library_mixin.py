import os, time, shutil
from service_base import Service


class VideoLibraryMixin(Service):
    def __init__(self, ctx):
        super().__init__(ctx)
        self._import_svc = None     # wired: _migrate_queue_done_to_library, frame extraction

    def wire(self, *, import_svc, **_):
        self._import_svc = import_svc
    def load_library(self):
        """Return the full library. Runs migration on first call if needed.
        Also kicks off a background pass that extracts frame thumbnails for
        any imported videos that lack one — covers pre-existing imports from
        before the auto-frame feature existed."""
        if not self.settings.get('_library_migrated'):
            self._import_svc._migrate_queue_done_to_library()
        try:
            if self._has_pending_frame_extraction():
                self._import_svc._start_frame_extraction_worker()
        except Exception:
            pass
        return self.settings.get('library', [])

    def _has_pending_frame_extraction(self):
        """Cheap pending-check used by load_library to avoid spawning the
        frame-extraction worker when there's nothing for it to do."""
        try:
            if not self.settings.get('auto_extract_frame_thumbnails', True):
                return False
            for v in self.settings.get('library', []):
                if v.get('type') == 'playlist':
                    for c in v.get('videos', []):
                        if self._import_svc._needs_auto_frame_thumb(c):
                            return True
                elif self._import_svc._needs_auto_frame_thumb(v):
                    return True
        except Exception:
            return False
        return False

    def save_library(self, lib):
        """Overwrite the library. Frontend calls this after reorder/remove operations."""
        self._store.set('library', lib)

    def add_to_library(self, video):
        """Append a single video (or playlist with all children Done) to the library."""
        if not video or not video.get('id'):
            return False
        lib = self.settings.get('library', [])
        # Stamp added_at so we can show "NEW" badge for recently-added videos.
        # Only stamp if not already present (preserves the original add date on undo).
        if not video.get('added_at'):
            video['added_at'] = int(time.time())
        # Dedupe by id — if somehow already in the library, replace it (latest wins)
        lib = [v for v in lib if v.get('id') != video['id']]
        lib.append(video)
        self.settings['library'] = lib  # noqa: direct-to-live-dict
        # If this was previously removed (archived by filepath), pop the archive
        # entry — the live library is now canonical again.
        archive = self.settings.get('library_archive')
        if archive:
            arc_key = self._archive_key(video.get('filepath'))
            if arc_key and arc_key in archive:
                try:
                    del archive[arc_key]
                except KeyError:
                    pass
        self._store.save()
        return True

    def _archive_key(self, filepath):
        """Normalize a filepath into the canonical key used by library_archive."""
        if not filepath:
            return None
        try:
            return os.path.normcase(os.path.normpath(filepath))
        except (TypeError, ValueError):
            return None

    def _archive_entry(self, video):
        """Snapshot a library entry into settings['library_archive'] keyed by filepath
        so a later import-from-folder can restore the original metadata (url, thumbnail,
        uploader, formats, etc.) instead of rebuilding a sparse ffprobe-only entry."""
        if not video:
            return
        archive = self.settings.setdefault('library_archive', {})
        if video.get('type') == 'playlist':
            for child in video.get('videos', []):
                key = self._archive_key(child.get('filepath'))
                if key:
                    archive[key] = child
        else:
            key = self._archive_key(video.get('filepath'))
            if key:
                archive[key] = video

    def remove_from_library(self, video_id):
        """Remove a single entry from the library by id. Does not delete the file on disk.
        Archives the entry's metadata by filepath so re-importing the file later restores
        the original title/thumbnail/url instead of rebuilding a sparse ffprobe entry."""
        lib = self.settings.get('library', [])
        for v in lib:
            if v.get('id') == video_id:
                self._archive_entry(v)
                break
        self._store.set('library', [v for v in lib if v.get('id') != video_id])
        return True

    def delete_video_from_library_and_disk(self, video_id):
        """Hard delete: remove the entry AND delete the file from disk. For STANDALONE
        videos (not part of a playlist), also delete the containing folder since each
        standalone video lives in its own folder. For playlist CHILDREN, leave the
        folder alone — siblings may still live in the playlist's folder structure.

        Errors are non-fatal; the entry is removed from the library either way so the
        user isn't stuck with phantom entries."""
        lib = self.settings.get('library', [])
        target = None
        is_playlist_child = False
        for v in lib:
            if v.get('id') == video_id:
                target = v
                break
            if v.get('type') == 'playlist':
                for c in v.get('videos', []):
                    if c.get('id') == video_id:
                        target = c
                        is_playlist_child = True
                        break
                if target:
                    break
        if not target:
            return {'ok': False, 'error': 'Video not found in library'}

        deleted_files = []
        skipped_files = []
        deleted_folder = None

        # Delete the video file itself
        filepath = target.get('filepath')
        if filepath and os.path.exists(filepath):
            try:
                os.remove(filepath)
                deleted_files.append(filepath)
            except OSError as e:
                skipped_files.append({'path': filepath, 'reason': str(e)})

        # For standalone videos: each lives in its own folder (download_folder/title/file.mp4).
        # After removing the file, the folder usually still has the .info.json, .description,
        # subtitles, thumbnail variants etc. We delete the whole folder for standalones.
        # For playlist children: leave the folder alone — peer videos may share it via the
        # playlist's folder structure, and we don't want surprises.
        if not is_playlist_child and filepath:
            v_folder = os.path.dirname(filepath)
            # Safety: never delete the user's main download folder (only sub-folders of it)
            if v_folder and os.path.isdir(v_folder):
                try:
                    download_root = os.path.abspath(self.download_folder)
                    target_folder = os.path.abspath(v_folder)
                    if target_folder != download_root and target_folder.startswith(download_root + os.sep):
                        shutil.rmtree(v_folder, ignore_errors=False)
                        deleted_folder = v_folder
                except OSError as e:
                    skipped_files.append({'path': v_folder, 'reason': str(e)})

        # Delete the cached thumbnail (a 'pt:thumb:<id>.<ext>' marker resolves to a
        # local path under thumbnail_cache_dir).
        thumb_marker = target.get('thumbnail') or ''
        if thumb_marker.startswith('pt:thumb:'):
            thumb_filename = thumb_marker[len('pt:thumb:'):]
            thumb_path = os.path.join(self.thumbnail_cache_dir, thumb_filename)
            if os.path.exists(thumb_path):
                try:
                    os.remove(thumb_path)
                    deleted_files.append(thumb_path)
                except OSError as e:
                    skipped_files.append({'path': thumb_path, 'reason': str(e)})

        # Remove from library — top-level OR playlist child.
        if is_playlist_child:
            for v in lib:
                if v.get('type') == 'playlist':
                    v['videos'] = [c for c in v.get('videos', []) if c.get('id') != video_id]
        else:
            self.settings['library'] = [v for v in lib if v.get('id') != video_id]  # noqa: direct-to-live-dict

        # Also fix the download QUEUE. A completed download lives in BOTH the
        # library and the queue (status 'Done'); the frontend re-adds Done queue
        # items to the library on load, so a delete that only touched the library
        # resurrected the entry on the next launch (the "deleted videos come back
        # from the dead" bug).
        #   - Standalone queued copy: drop it.
        #   - Channel/playlist child: do NOT remove it — the playlist/channel queue
        #     entry IS that catalog, so removing the child would make the video
        #     vanish from the channel grid on restart. Instead clear its 'Done'
        #     state so (a) it isn't re-added to the library on next load and (b) the
        #     card unlocks and is selectable/re-downloadable again.
        queue = self.settings.get('queue', []) or []
        new_queue = []
        for q in queue:
            if q.get('id') == video_id and q.get('type') != 'playlist':
                continue
            if q.get('type') == 'playlist':
                for c in q.get('videos', []) or []:
                    if c.get('id') == video_id:
                        c.pop('status', None)
                        c.pop('progressPct', None)
                        c.pop('missing', None)
                        c['selected'] = False
            new_queue.append(q)
        self.settings['queue'] = new_queue  # noqa: direct-to-live-dict

        # Drop any archived metadata snapshot too, so a later import-from-folder
        # can't silently restore the entry the user just deleted "forever".
        archive = self.settings.get('library_archive')
        if archive and target.get('filepath'):
            arc_key = self._archive_key(target.get('filepath'))
            if arc_key and arc_key in archive:
                try:
                    del archive[arc_key]
                except KeyError:
                    pass

        self._store.save()

        return {
            'ok': True,
            'deleted': deleted_files,
            'deleted_folder': deleted_folder,
            'skipped': skipped_files,
            'is_playlist_child': is_playlist_child,
        }

