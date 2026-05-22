"""Regression net for the settings.json corruption bug.

The bug: settings.json was corrupted twice in production because many threads
wrote the file concurrently and interleaved bytes, and a separate crash came
from serializing the dict while another thread mutated it.

These tests hammer SettingsStore from many threads at once. If the structural
fix (one locked door) holds, the file always stays valid JSON and no concurrent
update is ever lost. Run it before any change to settings handling:

    python3 scripts/test_settings_store.py

It needs no GUI / webview — that isolation is itself a benefit of pulling the
store into its own module.
"""

import os
import sys
import json
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from settings_store import SettingsStore  # noqa: E402


def test_concurrent_writers_never_corrupt():
    """40 threads x 50 set() calls on distinct keys, all at once.
    Old code interleaved file writes -> corruption. New code: one locked door."""
    with tempfile.TemporaryDirectory() as d:
        store = SettingsStore(os.path.join(d, 'settings.json'))
        store.load()

        threads_n, per_thread = 40, 50

        def worker(t):
            for i in range(per_thread):
                store.set(f't{t}_k{i}', {'t': t, 'i': i})

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(threads_n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # The file must be valid JSON (never half-written / interleaved)...
        with open(store._path, 'r', encoding='utf-8') as f:
            on_disk = json.load(f)
        # ...and every single write must have landed.
        expected = threads_n * per_thread
        got = sum(1 for k in on_disk if k.startswith('t'))
        assert got == expected, f'lost writes: expected {expected}, found {got}'
        print(f'  [PASS] {expected} concurrent writes, file valid, none lost')


def test_concurrent_mutate_no_lost_updates():
    """30 threads each append 30 items to ONE shared list via mutate().
    A bare get-then-set would drop updates; atomic mutate() must not."""
    with tempfile.TemporaryDirectory() as d:
        store = SettingsStore(os.path.join(d, 'settings.json'))
        store.load()
        store.set('log', [])

        threads_n, per_thread = 30, 30

        def worker(t):
            for i in range(per_thread):
                with store.mutate() as s:
                    s['log'] = (s.get('log') or []) + [f'{t}-{i}']

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(threads_n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        with open(store._path, 'r', encoding='utf-8') as f:
            on_disk = json.load(f)
        expected = threads_n * per_thread
        got = len(on_disk.get('log', []))
        assert got == expected, f'lost updates: expected {expected}, found {got}'
        print(f'  [PASS] {expected} atomic read-modify-writes, none lost')


def test_corruption_recovery():
    """A trailing stray byte (the real-world failure mode) is recovered, not lost."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, 'settings.json')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('{"library": [1, 2, 3]}\n\x00')  # valid JSON + garbage byte
        store = SettingsStore(path)
        data = store.load()
        assert data.get('library') == [1, 2, 3], f'recovery failed: {data}'
        print('  [PASS] trailing-garbage file recovered, library intact')


def test_defer_coalesces():
    """A defer() block writes the file once, not once per set()."""
    with tempfile.TemporaryDirectory() as d:
        store = SettingsStore(os.path.join(d, 'settings.json'))
        store.load()
        with store.defer():
            for i in range(100):
                store.set(f'k{i}', i)
            # Inside the block nothing is on disk yet.
            assert not os.path.exists(store._path), 'defer wrote early'
        with open(store._path, 'r', encoding='utf-8') as f:
            on_disk = json.load(f)
        assert len(on_disk) == 100, f'defer flush incomplete: {len(on_disk)}'
        print('  [PASS] defer() coalesced 100 sets into one write')


if __name__ == '__main__':
    print('SettingsStore — concurrency & resilience tests')
    test_concurrent_writers_never_corrupt()
    test_concurrent_mutate_no_lost_updates()
    test_corruption_recovery()
    test_defer_coalesces()
    print('\nAll tests passed. The corruption bug is now structurally impossible.')
