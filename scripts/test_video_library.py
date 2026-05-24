"""
Tests for VideoLibraryMixin — add, remove, load, archive, delete.

Uses a minimal stub that provides only what the mixin needs:
  - real SettingsStore + AppContext backed by a tmp file
  - cross-service dependencies (frame extraction, queue migration) stubbed as no-ops
"""
import sys
import os
import time
import pytest

# Make src/ importable without installing the package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from settings_store import SettingsStore
from app_context import AppContext
from video_library_mixin import VideoLibraryMixin


class _StubImportSvc:
    """No-op stand-in for ImportMixin — only the methods VideoLibraryMixin calls."""
    def _migrate_queue_done_to_library(self): pass
    def _has_pending_frame_extraction(self): return False
    def _start_frame_extraction_worker(self): pass
    def _needs_auto_frame_thumb(self, v): return False


class _API(VideoLibraryMixin):
    """Minimal test stub — real SettingsStore + AppContext, no-op cross-service deps."""

    def __init__(self, settings_file, download_folder, thumb_dir):
        store = SettingsStore(settings_file)
        store.load()
        store.data['_library_migrated'] = True   # skip queue→library migration
        ctx = AppContext(store, download_folder, thumb_dir)
        super().__init__(ctx)
        self.wire(import_svc=_StubImportSvc())


@pytest.fixture
def api(tmp_path):
    settings_file = str(tmp_path / 'settings.json')
    download_folder = str(tmp_path / 'downloads')
    thumb_dir = str(tmp_path / 'thumbs')
    os.makedirs(download_folder, exist_ok=True)
    os.makedirs(thumb_dir, exist_ok=True)
    return _API(settings_file, download_folder, thumb_dir)


def _video(vid_id='v1', title='Test Video', filepath=None):
    return {'id': vid_id, 'title': title, 'filepath': filepath or f'/fake/{vid_id}.mp4'}


# ── load_library ─────────────────────────────────────────────────────────────

def test_load_library_empty(api):
    assert api.load_library() == []


def test_load_library_reflects_settings(api):
    v = _video()
    api.settings['library'] = [v]
    result = api.load_library()
    assert len(result) == 1
    assert result[0]['id'] == 'v1'


# ── add_to_library ────────────────────────────────────────────────────────────

def test_add_basic(api):
    assert api.add_to_library(_video('v1')) is True
    lib = api.load_library()
    assert len(lib) == 1
    assert lib[0]['id'] == 'v1'


def test_add_stamps_added_at(api):
    before = int(time.time())
    api.add_to_library(_video('v1'))
    after = int(time.time())
    stamp = api.load_library()[0]['added_at']
    assert before <= stamp <= after


def test_add_preserves_existing_added_at(api):
    v = _video('v1')
    v['added_at'] = 9999
    api.add_to_library(v)
    assert api.load_library()[0]['added_at'] == 9999


def test_add_dedupes_by_id(api):
    api.add_to_library(_video('v1', title='Old'))
    api.add_to_library(_video('v1', title='New'))
    lib = api.load_library()
    assert len(lib) == 1
    assert lib[0]['title'] == 'New'


def test_add_multiple_distinct_ids(api):
    api.add_to_library(_video('v1'))
    api.add_to_library(_video('v2'))
    assert len(api.load_library()) == 2


def test_add_rejects_video_without_id(api):
    assert api.add_to_library({'title': 'No ID'}) is False
    assert api.load_library() == []


def test_add_persists_across_reload(api, tmp_path):
    api.add_to_library(_video('v1'))
    # Re-open the store from the same file
    api2 = _API(
        str(tmp_path / 'settings.json'),
        api.download_folder,
        api.thumbnail_cache_dir,
    )
    lib = api2.load_library()
    assert len(lib) == 1
    assert lib[0]['id'] == 'v1'


# ── save_library ──────────────────────────────────────────────────────────────

def test_save_library_overwrites(api):
    api.add_to_library(_video('v1'))
    api.add_to_library(_video('v2'))
    api.save_library([_video('v3')])
    lib = api.load_library()
    assert len(lib) == 1
    assert lib[0]['id'] == 'v3'


def test_save_library_empty_clears(api):
    api.add_to_library(_video('v1'))
    api.save_library([])
    assert api.load_library() == []


# ── remove_from_library ───────────────────────────────────────────────────────

def test_remove_existing(api):
    api.add_to_library(_video('v1'))
    api.add_to_library(_video('v2'))
    api.remove_from_library('v1')
    lib = api.load_library()
    assert len(lib) == 1
    assert lib[0]['id'] == 'v2'


def test_remove_nonexistent_is_safe(api):
    api.add_to_library(_video('v1'))
    api.remove_from_library('does-not-exist')
    assert len(api.load_library()) == 1


def test_remove_archives_metadata(api):
    v = _video('v1', filepath='/fake/v1.mp4')
    api.add_to_library(v)
    api.remove_from_library('v1')
    archive = api.settings.get('library_archive', {})
    assert any(entry.get('id') == 'v1' for entry in archive.values())


def test_remove_all_leaves_empty(api):
    api.add_to_library(_video('v1'))
    api.remove_from_library('v1')
    assert api.load_library() == []


# ── delete_video_from_library_and_disk ───────────────────────────────────────

def test_delete_not_found(api):
    result = api.delete_video_from_library_and_disk('ghost')
    assert result['ok'] is False
    assert 'not found' in result['error'].lower()


def test_delete_removes_from_library(api):
    api.add_to_library(_video('v1'))
    api.delete_video_from_library_and_disk('v1')
    assert api.load_library() == []


def test_delete_removes_file_on_disk(api, tmp_path):
    fpath = str(tmp_path / 'downloads' / 'myvid' / 'v1.mp4')
    os.makedirs(os.path.dirname(fpath), exist_ok=True)
    open(fpath, 'w').close()
    v = _video('v1', filepath=fpath)
    api.add_to_library(v)
    result = api.delete_video_from_library_and_disk('v1')
    assert result['ok'] is True
    assert not os.path.exists(fpath)


def test_delete_removes_containing_folder_for_standalone(api, tmp_path):
    folder = tmp_path / 'downloads' / 'myvid'
    folder.mkdir(parents=True)
    fpath = str(folder / 'v1.mp4')
    open(fpath, 'w').close()
    v = _video('v1', filepath=fpath)
    api.add_to_library(v)
    api.delete_video_from_library_and_disk('v1')
    assert not folder.exists()


def test_delete_skips_missing_file_gracefully(api):
    v = _video('v1', filepath='/nonexistent/v1.mp4')
    api.add_to_library(v)
    result = api.delete_video_from_library_and_disk('v1')
    assert result['ok'] is True
    assert api.load_library() == []


def test_delete_clears_cached_thumbnail(api, tmp_path):
    thumb_name = 'v1.jpg'
    thumb_path = tmp_path / 'thumbs' / thumb_name
    thumb_path.touch()
    v = _video('v1')
    v['thumbnail'] = f'pt:thumb:{thumb_name}'
    api.add_to_library(v)
    api.delete_video_from_library_and_disk('v1')
    assert not thumb_path.exists()


def test_delete_clears_done_queue_entry(api):
    v = _video('v1')
    api.add_to_library(v)
    api.settings['queue'] = [{'id': 'v1', 'status': 'Done'}]
    api.delete_video_from_library_and_disk('v1')
    # standalone Done queue entry should be dropped
    assert all(q.get('id') != 'v1' for q in api.settings.get('queue', []))


# ── _archive_key ──────────────────────────────────────────────────────────────

def test_archive_key_none_input(api):
    assert api._archive_key(None) is None


def test_archive_key_normalises_path(api):
    k1 = api._archive_key('/a/b/../b/f.mp4')
    k2 = api._archive_key('/a/b/f.mp4')
    assert k1 == k2


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
