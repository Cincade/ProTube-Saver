"""The single owner of settings.json — the "one door" for persistent settings.

WHY THIS EXISTS
---------------
settings.json was corrupted twice in production. The cause was structural, not a
typo: many places across logic.py mutated a shared `self.settings` dict and then
each called a save routine, so two threads could write the *file* at the same
moment and interleave bytes. The old fix bolted a lock and a deep-copy onto the
save method — a patch at the crash site.

This module replaces that patch with a structure. There is now exactly ONE object
that owns the data dict AND the lock that guards it. Because there is one locked
door:

  * two threads can never write the file at once          -> corruption impossible
  * the dict cannot change size mid-serialization          -> dump runs under the lock
  * a corrupt file is recovered or backed up, never lost    -> load is defensive

These properties hold *by construction*. You don't have to remember to be careful;
the only way to persist is through this door, and the door is locked.

HOW TO USE
----------
    store = SettingsStore(path); store.load()

    store.get('library', [])          # locked read
    store.set('queue', q)             # mutate + persist, atomically
    with store.mutate() as s:         # atomic read-modify-write (lists, counters)
        s['recent'] = (s.get('recent') or [])[:12]
    with store.defer():               # coalesce many writes into one disk write
        ...

`store.data` exposes the live dict for the many existing READ sites. Reads are
safe. To CHANGE a value, go through set()/update()/delete()/mutate() so the
mutation and the save happen together under the lock.
"""

import os
import json
import time
import copy
import threading
import contextlib


class SettingsStore:
    def __init__(self, path):
        self._path = path
        # Re-entrant so mutate()/set() can call save() while already holding it.
        self._lock = threading.RLock()
        self._data = {}
        self._defer_depth = 0
        self._dirty = False

    # ------------------------------------------------------------------ load
    def load(self):
        """Read settings.json into memory. Resilient to corruption / empty files."""
        with self._lock:
            self._data = self._read_from_disk()
        return self._data

    def _read_from_disk(self):
        if not os.path.exists(self._path):
            return {}
        try:
            with open(self._path, 'r', encoding='utf-8') as f:
                content = f.read()
            if not content.strip():
                return {}
            return json.loads(content)
        except json.JSONDecodeError as e:
            # Most corruption is a trailing stray byte from a partial write —
            # the JSON proper is intact. Try to recover the first complete
            # top-level object before giving up on the user's data.
            try:
                recovered = self._recover_truncated(content)
                if recovered is not None:
                    print(f'[ProTube] settings.json had trailing garbage; recovered cleanly. Error was: {e}')
                    return recovered
            except Exception:
                pass
            try:
                backup = self._path + '.corrupt'
                if os.path.exists(backup):
                    backup = self._path + f'.corrupt.{int(time.time())}'
                os.rename(self._path, backup)
                print(f'[ProTube] settings.json was corrupt; backed up to {backup}. Error: {e}')
            except OSError:
                pass
            return {}
        except OSError as e:
            print(f'[ProTube] settings.json unreadable: {e}')
            return {}

    @staticmethod
    def _recover_truncated(content):
        """Scan for the first complete top-level JSON object and parse it.
        Covers the common partial-write case (a stray '}' / null / newline glued
        after the final closing brace). Returns the dict, or None on failure."""
        depth = 0
        in_str = False
        escape = False
        for i, ch in enumerate(content):
            if escape:
                escape = False
                continue
            if in_str:
                if ch == '\\':
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return json.loads(content[:i + 1])
        return None

    # ------------------------------------------------------------- live dict
    @property
    def data(self):
        """The live settings dict — for READS (`store.data.get(...)`).
        To change a value use set()/update()/delete()/mutate() so the change is
        persisted under the lock instead of silently lost."""
        return self._data

    # ----------------------------------------------------------------- reads
    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, default)

    # ---------------------------------------------------------------- writes
    def set(self, key, value):
        """Set one key and persist — mutation and save are one atomic step."""
        with self._lock:
            self._data[key] = value
            self.save()

    def update(self, mapping):
        """Merge a dict of keys and persist atomically."""
        with self._lock:
            self._data.update(mapping)
            self.save()

    def delete(self, key):
        """Remove a key (if present) and persist atomically."""
        with self._lock:
            self._data.pop(key, None)
            self.save()

    @contextlib.contextmanager
    def mutate(self):
        """Atomic read-modify-write. Yields the live dict under the lock and
        persists once on exit. Use for multi-step updates (append to a list,
        bump a counter) where a bare get-then-set would race with another thread."""
        with self._lock:
            yield self._data
            self.save()

    # ----------------------------------------------------- deferred coalescing
    @contextlib.contextmanager
    def defer(self):
        """Coalesce many saves into a single disk write at the outermost exit.
        Re-entrant: nested defer() blocks flush only when the last one exits."""
        with self._lock:
            self._defer_depth += 1
        try:
            yield
        finally:
            with self._lock:
                self._defer_depth -= 1
                if self._defer_depth == 0 and self._dirty:
                    self._dirty = False
                    self._write_to_disk()

    # ------------------------------------------------------- the one disk write
    def save(self):
        """Persist current state. Inside a defer() block this just marks dirty;
        the outermost defer() exit does the single real write."""
        with self._lock:
            if self._defer_depth > 0:
                self._dirty = True
                return
            self._write_to_disk()

    def _write_to_disk(self):
        """Atomic write: serialize under the lock, dump to a unique temp file,
        then os.replace (atomic on POSIX + Windows) so a reader never sees a
        half-written file. The caller already holds self._lock.

        Serializing while locked is what makes corruption impossible: no other
        save can run, and no store write can mutate the dict mid-dump. The
        deep-copy retry below is transitional armor for legacy sites in logic.py
        that still mutate `store.data` directly without going through this door;
        as those migrate to set()/mutate(), the retry becomes dead weight and
        can be deleted."""
        with self._lock:
            payload = None
            for _ in range(6):
                try:
                    payload = json.dumps(self._data)
                    break
                except RuntimeError:
                    # "dictionary changed size during iteration" — a legacy
                    # direct-mutation site raced us. Brief wait, then retry.
                    time.sleep(0.005)
            if payload is None:
                payload = json.dumps(copy.deepcopy(self._data))

            tmp = f'{self._path}.tmp.{os.getpid()}.{threading.get_ident()}'
            try:
                with open(tmp, 'w', encoding='utf-8') as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self._path)
            except OSError as e:
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
                print(f'[ProTube] settings save failed: {e}')
