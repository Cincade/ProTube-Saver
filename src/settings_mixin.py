import os, re, json, sys, time, threading, shutil, subprocess, requests, contextlib, webview
from packaging.version import parse
from ydl_utils import YoutubeDL
from service_base import Service

# Canonical version string — bump here when shipping a build.
__version__ = '1.5.0'

# Update manifest URLs — where check_for_updates() looks for new releases.
LANDING_VERSION_URL_DEFAULT = 'https://protubesaver.netlify.app/version.json'
GITHUB_RELEASES_URL_DEFAULT = 'https://api.github.com/repos/Cincade/ProTube-Saver/releases/latest'


class SettingsMixin(Service):
    def __init__(self, ctx, updater):
        super().__init__(ctx)
        self.updater = updater
    def _load_settings(self):
        """Back-compat shim. Settings loading + corruption recovery now live in
        SettingsStore (the single locked door). Kept so any caller still works."""
        return self._store.load()

    def _save_settings(self):
        """Back-compat shim. Every persist now goes through the one locked door
        in SettingsStore, so two threads can no longer interleave a write and
        corrupt the file. Kept under the old name so the ~30 existing callers in
        this file don't all need editing at once — they migrate to
        self._store.set/update/mutate incrementally."""
        self._store.save()

    @contextlib.contextmanager
    def _deferred_save(self):
        """Back-compat shim — write-coalescing now lives in SettingsStore.defer()."""
        with self._store.defer():
            yield
    def on_dom_ready(self): pass
    def choose_folder(self):
        result = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
        if result and len(result) > 0:
            self.download_folder = result[0]
            self._store.set('download_folder', self.download_folder)
            return self.download_folder
        return self.download_folder
    
    def get_download_folder(self): return self.download_folder

    def heartbeat(self):
        """No-op called by the JS heartbeat ticker every 30s. Belt-and-suspenders
        with the IntensiveWakeUpThrottling Chromium flags in main.py — even if
        a future WebView2 build ignores the flags, the regular API traffic from
        this method counts as 'work' and keeps the renderer's event loop active.
        Without it, Chromium foreground-idle throttling kicks in after ~5 min of
        no input and the whole UI freezes until the user clicks. Returns nothing
        the JS side cares about; the call itself is the entire point."""
        return {'ok': True}

    def reset_onboarding(self):
        """Clear the _onboarded flag so the welcome modal shows on next launch.
        Useful for re-triggering onboarding while testing."""
        # Migrated to the single door: delete-and-persist is one atomic step.
        self._store.delete('_onboarded')
        return {'ok': True}
    def save_queue(self, q): self._store.set('queue', q)
    def load_queue(self): return self._store.get('queue', [])

    def get_setting(self, key):
        """Generic settings read for frontend use (feature flags, one-time migration markers)."""
        return self._store.get(key)

    def get_subtitles_for_video(self, video_id):
        """Read the subtitle .vtt file for a library video and return its raw text.

        Looks up the library entry by id (top-level OR playlist child), falls back to
        scanning the video's folder if the stored subtitle_path is missing or stale (file
        moved / deleted). Returns {'vtt': '<text>'} on success, {'error': '...'} otherwise.
        Frontend parses the VTT itself and renders cues in a custom overlay so we get
        our fonts instead of the native <track> rendering.
        """
        try:
            # Library entries live under 'library' (not 'videos' — that key doesn't exist).
            # The bug here was returning 'video not in library' for every video, so the
            # frontend silently dimmed the CC button as if no subs existed.
            entry = None
            for v in self.settings.get('library', []):
                if v.get('type') == 'playlist':
                    for c in (v.get('videos') or []):
                        if c.get('id') == video_id:
                            entry = c
                            break
                    if entry:
                        break
                elif v.get('id') == video_id:
                    entry = v
                    break
            if not entry:
                return {'error': 'video not in library'}

            sub_path = entry.get('subtitle_path')
            # Fallback: stored path missing or file gone — scan the video's folder for a .vtt.
            # Two passes: first try a name match (Title.en.vtt next to Title.mp4) so we pick
                # the right file when a folder somehow has multiple videos. If that misses, take
                # any .vtt in the folder — yt-dlp can sanitize titles slightly differently for
                # video vs subtitle filenames in edge cases (Unicode, trailing dots, etc.) and
                # ProTube downloads each video into its own subfolder, so any .vtt here is ours.
            if (not sub_path or not os.path.exists(sub_path)) and entry.get('filepath'):
                folder = os.path.dirname(entry['filepath'])
                base = os.path.splitext(os.path.basename(entry['filepath']))[0]
                if os.path.isdir(folder):
                    matched = None
                    fallback_any = None
                    for fname in os.listdir(folder):
                        if not fname.endswith('.vtt'):
                            continue
                        if fname.startswith(base):
                            matched = fname
                            break
                        if fallback_any is None:
                            fallback_any = fname
                    pick = matched or fallback_any
                    if pick:
                        sub_path = os.path.join(folder, pick)
                        entry['subtitle_path'] = sub_path
                        self._save_settings()

            if not sub_path or not os.path.exists(sub_path):
                return {'error': 'no subtitles available'}

            with open(sub_path, 'r', encoding='utf-8', errors='replace') as f:
                return {'vtt': f.read()}
        except Exception as e:
            return {'error': f'failed to load subtitles: {e}'}

    def _find_library_entry(self, video_id):
        """Walk the library (top-level + playlist children) and return the matching entry,
        or None. Shared helper for AI features that need to read/write per-video metadata."""
        for v in self.settings.get('library', []):
            if v.get('type') == 'playlist':
                for c in (v.get('videos') or []):
                    if c.get('id') == video_id:
                        return c
            elif v.get('id') == video_id:
                return v
        return None

    def _vtt_to_plain_text(self, vtt_text):
        """Strip VTT timestamps and tags, return continuous prose for LLM input."""
        import re
        out_lines = []
        for line in vtt_text.replace('\r\n', '\n').split('\n'):
            s = line.strip()
            if not s or s.startswith('WEBVTT') or s.startswith('NOTE'):
                continue
            if '-->' in s:
                continue  # timestamp line
            if re.match(r'^\d+$', s):
                continue  # cue identifier (a number on its own line)
            # Strip inline tags + word-level timestamp markers
            s = re.sub(r'<\d{2}:\d{2}:\d{2}\.\d{3}>', '', s)
            s = re.sub(r'<\/?[^>]+>', '', s)
            s = s.strip()
            if s:
                out_lines.append(s)
        # Collapse adjacent duplicates (auto-captions repeat lines as the speaker
        # continues — we don't want each fragment counted multiple times in the prompt).
        deduped = []
        for line in out_lines:
            if not deduped or deduped[-1] != line:
                deduped.append(line)
        return ' '.join(deduped)

    def polish_subtitles_with_ai(self, video_id):
        """F8 — pass the video's .vtt through Groq for caption cleanup (punctuation,
        homophones, duplicate words) and cache the cleaned VTT next to the original.
        Frontend swaps the displayed cues to the cleaned set on success."""
        try:
            raw = self.get_subtitles_for_video(video_id)
            if 'error' in raw:
                return raw

            entry = self._find_library_entry(video_id)
            if not entry:
                return {'error': 'video not in library'}

            sub_path = entry.get('subtitle_path')
            cleaned_path = None
            if sub_path:
                cleaned_path = os.path.splitext(sub_path)[0] + '.cleaned.vtt'
                # Cache hit — skip the Groq call entirely.
                if os.path.exists(cleaned_path):
                    with open(cleaned_path, 'r', encoding='utf-8', errors='replace') as f:
                        return {'vtt': f.read(), 'cached': True}

            api_key = self.settings.get('groq_api_key', '').strip()
            if not api_key:
                return {'error': 'No Groq API key. Add one in Settings → AI.'}

            from groq_client import GroqClient, GroqError
            client = GroqClient(api_key)

            system = (
                "You fix YouTube auto-generated subtitles. The user will give you a WebVTT file. "
                "Return the SAME WebVTT file with these fixes:\n"
                "1. Add proper sentence punctuation (periods, commas, question marks).\n"
                "2. Capitalize the start of sentences and proper nouns.\n"
                "3. Fix obvious homophone errors (their/there/they're, your/you're, etc.) when context makes the right one obvious.\n"
                "4. Remove immediate duplicate words (\"the the\" -> \"the\").\n"
                "RULES:\n"
                "- Do NOT paraphrase or change meaning.\n"
                "- Do NOT change the cue count or merge cues.\n"
                "- Do NOT change the timestamps.\n"
                "- Do NOT remove the WEBVTT header.\n"
                "- Output ONLY the cleaned WebVTT file content, nothing before or after it."
            )

            try:
                cleaned = client.chat(system, raw['vtt'], max_tokens=8000, temperature=0.2)
            except GroqError as e:
                return {'error': str(e)}

            # Trim wrapping code fences if the model added them despite instructions.
            cleaned = cleaned.strip()
            if cleaned.startswith('```'):
                cleaned = cleaned.split('\n', 1)[1] if '\n' in cleaned else cleaned
                if cleaned.endswith('```'):
                    cleaned = cleaned.rsplit('```', 1)[0]
                cleaned = cleaned.strip()

            # Sanity: response must look like VTT (contains WEBVTT and at least one '-->' line).
            if 'WEBVTT' not in cleaned or '-->' not in cleaned:
                return {'error': 'AI response was not valid VTT — keeping original.'}

            if cleaned_path:
                try:
                    with open(cleaned_path, 'w', encoding='utf-8') as f:
                        f.write(cleaned)
                except OSError as e:
                    print(f'[ProTube] failed to cache cleaned VTT: {e}')

            return {'vtt': cleaned, 'cached': False}
        except Exception as e:
            return {'error': f'polish failed: {e}'}

    def generate_video_summary(self, video_id):
        """F7 — generate a 3-5 paragraph article-style summary from the video's subtitles
        using Groq. Result is cached on the library entry as `ai_summary` so subsequent
        opens are instant."""
        try:
            entry = self._find_library_entry(video_id)
            if not entry:
                return {'error': 'video not in library'}

            # Cache hit
            if entry.get('ai_summary'):
                return {'summary': entry['ai_summary'], 'cached': True}

            raw = self.get_subtitles_for_video(video_id)
            if 'error' in raw:
                return {'error': 'Need subtitles to generate a summary. Re-download to fetch them.'}

            api_key = self.settings.get('groq_api_key', '').strip()
            if not api_key:
                return {'error': 'No Groq API key. Add one in Settings → AI.'}

            transcript = self._vtt_to_plain_text(raw['vtt'])
            if not transcript or len(transcript) < 60:
                return {'error': 'Transcript too short to summarize.'}

            # Llama 3.3 70B has a 32k token context. Chars-to-tokens roughly 4:1, so
            # cap input at ~80k chars to leave room for prompt + output. Most videos
            # under ~3 hours fit. Longer ones get truncated to the first 80k chars.
            transcript = transcript[:80000]
            title = entry.get('title') or 'YouTube video'
            uploader = entry.get('uploader') or ''

            from groq_client import GroqClient, GroqError
            client = GroqClient(api_key)

            system = (
                "You are writing a high-signal summary of a YouTube video for someone deciding "
                "whether to watch it. Density matters. Specifics matter. Generic statements are "
                "a failure mode — every bullet should have something a reader couldn't have "
                "guessed from the title alone.\n\n"
                "## OUTPUT FORMAT (strict markdown)\n\n"
                "OPENING: 1-2 sentences. Name the creator (if mentioned), name the topic, name "
                "the angle/promise. Examples of GOOD openings:\n"
                "- \"Mert Yerlikaya breaks down the exact 3-year path to a $10M AI agency exit, "
                "drawing on his own $600k/year shop.\"\n"
                "- \"Andrej Karpathy walks through how he uses Cursor and Claude to ship side "
                "projects in a weekend, with concrete examples from his recent micro-app.\"\n\n"
                "Then a `## What it covers` section with 3-6 bullets. Each bullet captures a "
                "specific topic IN THE ORDER the video covers it. Lead with the topic, then a "
                "concrete detail (a number, name, example) that anchors what's actually said.\n\n"
                "Then a `## Key points` section with 3-6 bullets. These are the CLAIMS the "
                "creator makes — what they argue, recommend, or warn against. Each must be "
                "specific enough that a reader could quote it back. Lean on **bold** to pull "
                "out the most quotable phrase in each bullet.\n\n"
                "End with `## Bottom line` — ONE punchy sentence. Not two. The thesis of the "
                "video distilled to a tweetable line.\n\n"
                "## QUALITY BAR\n\n"
                "Re-read every bullet you write and ask: \"could a reader have written this "
                "without watching the video?\" If yes, the bullet is bad — replace it with "
                "something specific (a number, a name, a counterintuitive claim, a step in "
                "a process). EVERY summary should contain at least 4 specific numbers, names, "
                "or proper nouns drawn from the video. Bullets without specifics are forbidden.\n\n"
                "## STYLE\n\n"
                "- Keep bullets SHORT — one sentence, ideally under 25 words. Cut filler words.\n"
                "- Use **bold** sparingly: 1 bold phrase per Key Points bullet, max.\n"
                "- Don't introduce sections with sentences like \"Here are the key points:\" — "
                "the heading is the introduction. Just go.\n"
                "- TARGET LENGTH: 250-450 words total. Tighter is better than longer.\n\n"
                "## STRICT DON'TS\n\n"
                "- DO NOT add a title or H1 before the opening.\n"
                "- DO NOT mention transcripts or subtitles.\n"
                "- DO NOT invent details not in the transcript. If the transcript is sparse, "
                "the summary should be sparse too — never pad.\n"
                "- DO NOT close with meta-commentary.\n"
                "- DO NOT wrap output in code fences or quotes.\n"
                "- Output ONLY the markdown. Nothing before. Nothing after."
            )
            user_msg = (
                f"Title: {title}\n"
                f"Channel: {uploader}\n\n"
                f"Transcript:\n{transcript}"
            )

            try:
                summary = client.chat(system, user_msg, max_tokens=1500, temperature=0.5)
            except GroqError as e:
                return {'error': str(e)}

            summary = summary.strip()
            if not summary:
                return {'error': 'AI returned an empty summary.'}

            entry['ai_summary'] = summary
            self._save_settings()
            return {'summary': summary, 'cached': False}
        except Exception as e:
            return {'error': f'summary failed: {e}'}

    def clear_video_ai_summary(self, video_id):
        """Drop the cached summary so the next call regenerates. Useful if the user
        wants a fresh take or the first response was bad."""
        entry = self._find_library_entry(video_id)
        if not entry:
            return {'ok': False}
        if 'ai_summary' in entry:
            del entry['ai_summary']
            self._save_settings()
        return {'ok': True}

    def _normalize_channel_url_str(self, url):
        """Collapse channel URL variants to one key for dedup."""
        try:
            return (url or '').rstrip('/').replace('/videos', '').lower()
        except Exception:
            return ''

    def _format_view_count(self, n):
        """1234567 → '1.2M views'."""
        try:
            n = int(n)
            if n >= 1_000_000_000:
                return f'{n/1_000_000_000:.1f}B views'.replace('.0B', 'B')
            if n >= 1_000_000:
                return f'{n/1_000_000:.1f}M views'.replace('.0M', 'M')
            if n >= 1_000:
                return f'{n/1_000:.1f}K views'.replace('.0K', 'K')
            return f'{n} views'
        except Exception:
            return ''

    def _relative_published(self, info):
        """A 'N years/months/... ago' string from a yt-dlp info dict's timestamp or
        upload_date (YYYYMMDD). Empty when neither is present. Used for channel-
        preview cards so they read like the search results."""
        ts = info.get('timestamp') or info.get('release_timestamp')
        if not ts:
            ud = str(info.get('upload_date') or '')
            if len(ud) == 8:
                try:
                    import datetime as _dt
                    ts = _dt.datetime(int(ud[:4]), int(ud[4:6]), int(ud[6:8])).timestamp()
                except Exception:
                    ts = None
        if not ts:
            return ''
        try:
            secs = max(0, int(time.time()) - int(ts))
        except Exception:
            return ''
        for name, span in (('year', 31536000), ('month', 2592000), ('week', 604800),
                           ('day', 86400), ('hour', 3600), ('minute', 60)):
            if secs >= span:
                n = secs // span
                return f"{n} {name}{'s' if n != 1 else ''} ago"
        return 'just now'

    def find_channel_for_video(self, video_id):
        """Look up the canonical channel URL for a library video AND check whether that
        channel is already queued or in the library. Returns:
            {url: <channel-url>, already: <{id, title} or null>, source: 'stamped'|'probed'}
            or {error: ...}

        Resolution priority:
        1. `uploader_url` / `channel_url` stamped on the entry at fetch time.
        2. Fresh yt-dlp probe of the video's watch URL. yt-dlp returns the REAL channel
           URL (e.g. https://www.youtube.com/@MertYerlikaya) regardless of how messy the
           display name is — this is the only reliable path for older entries that pre-date
           the uploader_url stamping. Result is cached on the entry for next time.

        We deliberately removed the "derive @handle from display name" fallback — it was
        wrong often enough (display names rarely match handles for channels with spaces or
        rebranded handles) that the user lost trust in the feature.
        """
        try:
            entry = self._find_library_entry(video_id)
            if not entry:
                return {'error': 'video not in library'}

            channel_url = entry.get('uploader_url') or entry.get('channel_url')
            source = 'stamped' if channel_url else None

            # Fallback: probe yt-dlp on the video's watch URL to extract the real channel URL.
            # We use extract_flat='in_playlist' + skip downloads, so this is just metadata.
            if not channel_url and entry.get('url'):
                try:
                    probe_opts = self._get_ydl_opts('browser', 'none')
                    probe_opts.update({
                        'quiet': True,
                        'no_warnings': True,
                        'skip_download': True,
                        'extract_flat': False,  # we want the full channel_url field
                    })
                    with YoutubeDL(probe_opts) as ydl:
                        info = ydl.extract_info(entry['url'], download=False)
                    channel_url = (info.get('uploader_url')
                                   or info.get('channel_url'))
                    if channel_url:
                        source = 'probed'
                        # Stamp on entry so we don't probe again next time.
                        entry['uploader_url'] = channel_url
                        self._save_settings()
                except Exception as e:
                    print(f'[ProTube] channel probe failed for {video_id}: {e}')

            if not channel_url:
                return {'error': 'Could not find this channel URL. Try opening the video on YouTube and pasting the channel URL manually.'}

            # Smart-already check against both queue AND library (channels can live in either).
            normalized = channel_url.rstrip('/').replace('/videos', '').lower()
            this_uploader = (entry.get('uploader') or '').strip().lower()
            already = None
            sources = [
                ('queue', self.settings.get('queue', [])),
                ('library', self.settings.get('library', [])),
            ]
            for source_name, items in sources:
                for item in items:
                    if item.get('type') != 'playlist':
                        continue
                    item_url = (item.get('url') or '').rstrip('/').replace('/videos', '').lower()
                    if item_url and item_url == normalized:
                        already = {'id': item.get('id'), 'title': item.get('title') or 'Channel', 'where': source_name}
                        break
                    if (item.get('subtype') == 'channel'
                            and item.get('uploader')
                            and this_uploader
                            and item['uploader'].strip().lower() == this_uploader):
                        already = {'id': item.get('id'), 'title': item.get('title') or 'Channel', 'where': source_name}
                        break
                if already:
                    break

            return {'url': channel_url, 'already': already, 'source': source}
        except Exception as e:
            return {'error': f'channel lookup failed: {e}'}

    def chat_about_video(self, video_id, question, history=None):
        """Answer a question about a video using its transcript + the conversation history.
        history is a list of {role: 'user'|'assistant', content: str} (capped on the frontend
        so we don't blow the context window). Returns {'reply': str} or {'error': str}."""
        try:
            entry = self._find_library_entry(video_id)
            if not entry:
                return {'error': 'video not in library'}

            api_key = self.settings.get('groq_api_key', '').strip()
            if not api_key:
                return {'error': 'No Groq API key. Add one in Settings → AI.'}

            raw = self.get_subtitles_for_video(video_id)
            if 'error' in raw:
                return {'error': 'Need subtitles to chat about this video. Re-download it to fetch them.'}

            transcript = self._vtt_to_plain_text(raw['vtt'])
            if not transcript:
                return {'error': 'Transcript is empty.'}

            # Cap transcript to leave room for prompt + history + answer.
            transcript = transcript[:60000]
            title = entry.get('title') or 'YouTube video'
            uploader = entry.get('uploader') or ''

            from groq_client import GroqClient, GroqError
            client = GroqClient(api_key)

            system = (
                "You answer questions about a specific YouTube video using its transcript as "
                "the source of truth. Ground every answer in what the video actually says.\n\n"
                "RULES:\n"
                "- Answer concisely (1-3 short paragraphs unless the question demands more).\n"
                "- Quote or paraphrase specific points from the video — name people, numbers, "
                "examples that appear in it.\n"
                "- If the video doesn't address the question, say so plainly: \"The video doesn't "
                "cover that.\" Don't make things up.\n"
                "- Use markdown: short paragraphs, bullet points if listing, **bold** for key "
                "terms. No headers (the answer is short).\n"
                "- Don't preface with \"Great question!\" or similar fluff. Just answer."
            )

            # Build the messages array: system, transcript context, then chat history, then new question.
            # The transcript is provided as a one-off context message so it doesn't get repeated in
            # every turn — but each call still includes it because Groq is stateless.
            user_context = (
                f"VIDEO TITLE: {title}\n"
                f"CHANNEL: {uploader}\n\n"
                f"TRANSCRIPT:\n{transcript}\n\n"
                f"---\nThe user will now ask questions about this video. Use the transcript above "
                f"as your source of truth."
            )

            messages = [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': user_context},
                {'role': 'assistant', 'content': 'Got it. I have the transcript and I\'m ready for your questions.'},
            ]

            # Append prior turns (cap to last 8 to keep context tight)
            if isinstance(history, list):
                for turn in history[-8:]:
                    role = turn.get('role')
                    content = (turn.get('content') or '').strip()
                    if role in ('user', 'assistant') and content:
                        messages.append({'role': role, 'content': content})

            # Append the current question
            messages.append({'role': 'user', 'content': str(question or '').strip()})

            # Direct call (bypass GroqClient.chat which only takes system+user).
            try:
                resp = requests.post(
                    GroqClient.URL,
                    json={
                        'model': client.model,
                        'messages': messages,
                        'max_tokens': 1200,
                        'temperature': 0.4,
                    },
                    headers={
                        'Authorization': f'Bearer {api_key}',
                        'Content-Type': 'application/json',
                    },
                    timeout=60,
                )
            except requests.RequestException as e:
                return {'error': f'Network error: {e}'}

            if resp.status_code == 401:
                return {'error': 'Groq rejected the API key.'}
            if resp.status_code == 429:
                return {'error': 'Rate limit hit. Try again in a minute.'}
            if resp.status_code != 200:
                return {'error': f'Groq HTTP {resp.status_code}'}

            try:
                data = resp.json()
                reply = data['choices'][0]['message']['content'].strip()
            except (ValueError, KeyError, IndexError) as e:
                return {'error': f'Bad response from Groq: {e}'}

            return {'reply': reply}
        except Exception as e:
            return {'error': f'chat failed: {e}'}

    def set_setting(self, key, value):
        """Generic settings write. Frontend uses this for things like migration markers.
        Migrated to the single door: mutation + persist happen atomically under one lock."""
        self._store.set(key, value)
        return True

    def set_video_hidden(self, video_id, hidden=True):
        """Mark a library entry as hidden so it's filtered out of the default
        library view. Hidden items are still in the library AND on disk — this
        is purely a display preference. Toggle off via hidden=False.

        Works on top-level entries (video or playlist) and on playlist children.
        Returns {ok: bool, error?: str}.
        """
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
            return {'ok': False, 'error': 'Library entry not found'}
        target['hidden'] = bool(hidden)
        self._save_settings()
        return {'ok': True}

    def set_video_pinned(self, video_id, pinned=True):
        """Toggle the `pinned` flag on a top-level library entry. Pinned items
        sort to the top of the library grid. Frontend handles the visual
        ordering — backend just stores the flag and timestamp.

        We stamp `pinned_at` so multiple pinned items sort by pin-time
        (most-recently-pinned on top). Unpinning clears both fields.

        Pinning is for top-level library entries only (videos + playlists).
        Playlist children aren't pinnable individually — they live inside
        their parent and pin the whole entry to elevate them.
        """
        lib = self.settings.get('library', [])
        for v in lib:
            if v.get('id') == video_id:
                if pinned:
                    v['pinned'] = True
                    v['pinned_at'] = int(time.time())
                else:
                    v.pop('pinned', None)
                    v.pop('pinned_at', None)
                self._save_settings()
                return {'ok': True}
        return {'ok': False, 'error': 'Library entry not found'}

    def set_videos_pinned_batch(self, video_ids, pinned=True):
        """Bulk pin/unpin top-level library entries. One pass through the
        library, ONE settings.json write at the end. Same shape as
        set_videos_hidden_batch — returns the ids actually flipped (skips
        items already in the target state) so the Undo only reverses what
        changed.

        Pinning playlist children isn't supported here (set_video_pinned
        rejects them too) — pin the whole entry to elevate its position.
        """
        if not video_ids:
            return {'ok': True, 'flipped': []}
        wanted = {vid for vid in video_ids if vid}
        if not wanted:
            return {'ok': True, 'flipped': []}
        flipped = []
        now = int(time.time())
        for v in self.settings.get('library', []):
            if v.get('id') in wanted:
                if pinned and not v.get('pinned'):
                    v['pinned'] = True
                    v['pinned_at'] = now
                    flipped.append(v.get('id'))
                elif not pinned and v.get('pinned'):
                    v.pop('pinned', None)
                    v.pop('pinned_at', None)
                    flipped.append(v.get('id'))
        if flipped:
            self._save_settings()
        return {'ok': True, 'flipped': flipped}

    def set_videos_hidden_batch(self, video_ids, hidden=True):
        """Bulk hide/unhide. One pass through the library, ONE settings.json
        write at the end. Avoids the "49 sequential awaits, each saving the
        whole 1MB+ settings.json" pattern that was failing silently for big
        selections — each save took a moment and the JSON bridge was getting
        backed up.

        video_ids: list of ids to flip. Missing ids are skipped silently
                   (callers shouldn't have to filter ahead of time).
        hidden: True to hide, False to unhide.

        Returns {ok: True, flipped: [ids actually changed]}. Items already in
        the target state are NOT counted as flipped, so the frontend's Undo
        toast only reverses the ones it actually changed.
        """
        if not video_ids:
            return {'ok': True, 'flipped': []}
        wanted_set = {vid for vid in video_ids if vid}
        if not wanted_set:
            return {'ok': True, 'flipped': []}
        target_state = bool(hidden)
        flipped = []
        lib = self.settings.get('library', [])
        for v in lib:
            if v.get('id') in wanted_set:
                if bool(v.get('hidden')) != target_state:
                    v['hidden'] = target_state
                    flipped.append(v.get('id'))
            if v.get('type') == 'playlist':
                for c in v.get('videos', []):
                    if c.get('id') in wanted_set:
                        if bool(c.get('hidden')) != target_state:
                            c['hidden'] = target_state
                            flipped.append(c.get('id'))
        if flipped:
            self._save_settings()
        return {'ok': True, 'flipped': flipped}

    def set_max_concurrent_downloads(self, n):
        """Settings drawer setter. Clamps to [1, 8] and replaces the semaphore so
        new downloads queue against the new limit. In-flight downloads keep the
        old semaphore reference and finish naturally — they don't get killed.
        Persisted to settings.json so the limit survives restarts."""
        try:
            n = max(1, min(8, int(n)))
        except (TypeError, ValueError):
            n = 2
        self.max_concurrent_downloads = n
        self.download_semaphore = threading.Semaphore(n)
        self._store.set('max_concurrent_downloads', n)
        return n

    def _user_default_quality(self):
        """Translate the user's `default_quality` setting (Settings drawer) into
        a string the queue picker understands. The picker only knows resolutions
        ('2160p', '1440p', '1080p', '720p', '480p'); 'best' and 'audio' are
        Settings-only abstractions that we resolve here.
        - 'best' → '2160p' (yt-dlp auto-picks best available <= this)
        - 'audio' → '1080p' (audio-only is a per-video Extra toggle, not a quality)
        - resolution string → use as-is
        - unset / unknown → '1080p'
        """
        pref = self.settings.get('default_quality') or '1080p'
        if pref == 'best':
            return '2160p'
        if pref == 'audio':
            return '1080p'
        if pref in ('2160p', '1440p', '1080p', '720p', '480p'):
            return pref
        return '1080p'

    def get_about_info(self):
        """Snapshot for the Settings drawer's About section.
        - version: app version (bumped per release in __version__ at top of file)
        - ytdlp_version: whichever yt-dlp the runtime ended up using (auto-updated
          copy in data/yt-dlp-runtime/ takes precedence over the bundled one)
        - library_count: number of top-level library entries (videos + playlists)
        - library_video_count: flattened — playlists' children count individually
        - library_size_bytes: sum of selected-quality file sizes from sizeMap
        - queue_count: number of items currently in queue
        """
        try:
            from yt_dlp.version import __version__ as ytdlp_ver
        except Exception:
            ytdlp_ver = 'unknown'

        lib = self.settings.get('library', [])
        queue = self.settings.get('queue', [])

        library_video_count = 0
        library_size_bytes = 0
        for item in lib:
            if item.get('type') == 'playlist':
                children = item.get('videos', [])
                library_video_count += len(children)
                for c in children:
                    q = c.get('selectedQuality')
                    sm = c.get('sizeMap') or {}
                    if q and isinstance(sm.get(q), (int, float)):
                        library_size_bytes += sm[q]
            else:
                library_video_count += 1
                q = item.get('selectedQuality')
                sm = item.get('sizeMap') or {}
                if q and isinstance(sm.get(q), (int, float)):
                    library_size_bytes += sm[q]

        # Data dir — surfaced so the Settings drawer's "Open data folder" button
        # has a path to hand to open_folder(). Computed via app_paths so it
        # works in both dev and frozen-PyInstaller modes.
        try:
            from app_paths import data_dir as _dd
            data_dir_path = _dd()
        except Exception:
            data_dir_path = ''

        return {
            'version': __version__,
            'ytdlp_version': ytdlp_ver,
            'library_count': len(lib),
            'library_video_count': library_video_count,
            'library_size_bytes': library_size_bytes,
            'queue_count': len(queue),
            'max_concurrent_downloads': self.max_concurrent_downloads,
            'data_dir': data_dir_path,
        }

    def _fetch_update_manifest_landing(self):
        """Hit the landing site's version.json. Returns (error_str, data_dict) —
        exactly one of the two is set. data_dict already matches the contract
        check_for_updates() consumes (latest, downloadUrl, downloadSizeMB,
        releaseNotes, releasedAt)."""
        url = self.settings.get('update_check_url') or LANDING_VERSION_URL_DEFAULT
        try:
            resp = requests.get(url, timeout=6, headers={'Cache-Control': 'no-cache'})
            if resp.status_code != 200:
                return f'HTTP {resp.status_code}', None
            return None, resp.json()
        except Exception as e:
            return str(e), None

    def _fetch_update_manifest_github(self):
        """Hit GitHub's Releases API and adapt the response to the same shape
        the landing-site fetcher returns. Picks the first asset whose name ends
        in .exe as the download. Public repo → no auth needed; GitHub's
        anonymous quota (60 req/IP/hour) is plenty since we throttle to once
        per 24h via the on-disk cache anyway.

        Returns (error_str, data_dict) — exactly one of the two is set."""
        url = self.settings.get('github_releases_url') or GITHUB_RELEASES_URL_DEFAULT
        try:
            resp = requests.get(url, timeout=6, headers={
                'Accept': 'application/vnd.github+json',
                'Cache-Control': 'no-cache',
            })
            if resp.status_code != 200:
                return f'GitHub HTTP {resp.status_code}', None
            gh = resp.json()
        except Exception as e:
            return f'GitHub: {e}', None

        # GitHub tag convention is 'v1.2.0' — strip the v so the comparator
        # sees the same shape it gets from version.json's bare '1.2.0'.
        tag = str(gh.get('tag_name') or '').strip()
        latest = tag[1:] if tag.lower().startswith('v') else tag

        # Pick the right asset for the platform we're running on. The original
        # filter was `.exe`-only, which left downloadUrl empty for Mac users
        # (their release asset is a .zip) so the in-app Download button did
        # nothing. Match by extension AND a platform keyword so Mac doesn't
        # accidentally grab a Windows zip when both are attached to one release.
        # Two-pass: keyword+ext required first, ext-only as fallback for legacy
        # releases (v1.0.0–v1.2.0 named the .exe without a 'windows' keyword).
        if sys.platform == 'darwin':
            kw, exts = ('mac', 'osx', 'darwin'), ('.dmg', '.zip')
        elif sys.platform == 'win32':
            kw, exts = ('win',), ('.exe', '.msi', '.zip')
        else:
            kw, exts = ('linux',), ('.appimage', '.deb', '.tar.gz', '.zip')

        assets = gh.get('assets') or []
        picked = None
        for ext in exts:
            for asset in assets:
                name = (asset.get('name') or '').lower()
                if name.endswith(ext) and any(k in name for k in kw):
                    picked = asset
                    break
            if picked:
                break
        if not picked:
            for ext in exts:
                for asset in assets:
                    name = (asset.get('name') or '').lower()
                    if name.endswith(ext):
                        picked = asset
                        break
                if picked:
                    break

        download_url = ''
        download_size_mb = None
        if picked:
            download_url = picked.get('browser_download_url') or ''
            size_bytes = picked.get('size') or 0
            if size_bytes:
                download_size_mb = round(size_bytes / (1024 * 1024), 1)

        return None, {
            'latest': latest or '0.0.0',
            'downloadUrl': download_url,
            'downloadSizeMB': download_size_mb,
            'releaseNotes': gh.get('body') or '',
            'releasedAt': gh.get('published_at') or '',
        }

    def check_for_updates(self, force=False):
        """Compare local __version__ against the landing site's version.json
        and report whether a newer release is available.

        Throttled to once per 24h via data/update_check.json so we don't hit
        the site on every launch. Pass force=True (e.g., from the settings
        drawer's "Check now" button) to bypass the cache.

        Returns:
            {has_update: True, current, latest, downloadUrl, downloadSizeMB,
             releaseNotes, releasedAt}     — when newer version is published
            {has_update: False, current, latest}                  — up to date
            {has_update: False, current, error: str}              — fetch failed
        """
        try:
            from app_paths import data_dir
            state_path = os.path.join(data_dir(), 'update_check.json')
        except Exception:
            state_path = None

        # Cache hit?
        if not force and state_path and os.path.exists(state_path):
            try:
                with open(state_path, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                age = time.time() - state.get('checked_at', 0)
                if age < 86400 and state.get('result'):
                    cached = state['result']
                    # The cache file is keyed by time only, but the user might
                    # have upgraded between writes — e.g., v1.0.0 cached
                    # `has_update: true, latest: 1.0.1`, then the user installed
                    # v1.0.1, and the new exe reads the OLD cache. Trusting
                    # `has_update` verbatim would spam an upgrade pill on a
                    # version that's already current. Re-evaluate the comparison
                    # against the CURRENT __version__ before returning.
                    latest = str(cached.get('latest', '0.0.0')).strip()
                    try:
                        cached['has_update'] = parse(latest) > parse(__version__)
                    except Exception:
                        cached['has_update'] = latest > __version__
                    cached['current'] = __version__
                    cached['cached'] = True
                    return cached
            except Exception:
                pass  # corrupt cache file, just re-fetch

        source = (self.settings.get('update_source') or 'github').lower()
        if source == 'github':
            fetch_err, data = self._fetch_update_manifest_github()
        else:
            fetch_err, data = self._fetch_update_manifest_landing()
        if fetch_err:
            return {'has_update': False, 'current': __version__, 'error': fetch_err}

        latest = str(data.get('latest', '0.0.0')).strip()
        try:
            has_update = parse(latest) > parse(__version__)
        except Exception:
            # Fallback to lex-compare if version strings don't parse cleanly
            has_update = latest > __version__

        result = {
            'has_update': bool(has_update),
            'current': __version__,
            'latest': latest,
            'downloadUrl': data.get('downloadUrl', ''),
            'downloadSizeMB': data.get('downloadSizeMB'),
            'releaseNotes': data.get('releaseNotes', ''),
            'releasedAt': data.get('releasedAt', ''),
            'source': source,
        }

        # Persist the result so the next 24 launches don't re-fetch
        if state_path:
            try:
                with open(state_path, 'w', encoding='utf-8') as f:
                    json.dump({'checked_at': time.time(), 'result': result}, f)
            except Exception:
                pass

        return result

    # ----- In-app auto-update (Mac) -------------------------------------------
    # The flow: frontend calls start_update_download(url) → backend streams the
    # .dmg (or .zip) into the data dir, mounts/extracts the .app, stages it,
    # then emits 'protubeUpdateReady'. Frontend then calls
    # install_staged_update() which spawns a detached bash helper script and
    # quits the app. The helper waits for the parent to exit, swaps the .app
    # bundle on disk (sending the old one to Trash, not rm -rf), and relaunches.
    # Mac-only — Windows still uses the open-URL fallback because a running
    # .exe can't be replaced in place without a separate updater binary.

    def _current_app_bundle_path(self):
        """Return absolute path to the running .app bundle on Mac, or None if
        we're in dev mode (`python main.py`) or running on Windows."""
        if sys.platform != 'darwin' or not getattr(sys, 'frozen', False):
            return None
        exe = sys.executable
        marker = '/Contents/MacOS/'
        if marker in exe:
            return exe.split(marker)[0]
        return None

    def _update_staging_dir(self):
        """Where in-progress update downloads + extracted bundles live."""
        from app_paths import data_dir
        return os.path.join(data_dir(), 'update_staging')

    def _resolve_update_helper_path(self):
        """Locate the bundled update helper bash script."""
        candidates = []
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            candidates.append(os.path.join(sys._MEIPASS, 'protube_update_helper.sh'))
        here = os.path.dirname(os.path.abspath(__file__))
        candidates.append(os.path.join(here, '..', 'assets', 'mac', 'protube_update_helper.sh'))
        candidates.append(os.path.join(here, 'protube_update_helper.sh'))
        for p in candidates:
            if os.path.isfile(p):
                return os.path.abspath(p)
        return None

    def start_update_download(self, url):
        """Kick off an in-app update: download the .dmg/.zip at `url`, extract
        the .app inside, stage it for install. Returns immediately; progress
        is pushed to the frontend via _send_to_js events:
            protubeUpdateProgress {percent, state, msg}
            protubeUpdateReady    {staged_app_path, install_to}
            protubeUpdateError    {msg}
        """
        if sys.platform != 'darwin':
            self._send_to_js('protubeUpdateError', {
                'msg': 'In-app install is Mac-only. Use the Download button to grab the new version from GitHub.'
            })
            return False
        if not self._current_app_bundle_path():
            self._send_to_js('protubeUpdateError', {
                'msg': "Couldn't detect the install location (dev mode?). Open the release page to install manually."
            })
            return False
        if getattr(self, '_update_in_progress', False):
            return False
        self._update_in_progress = True

        def worker():
            try:
                self._update_download_and_stage(url)
            except Exception as e:
                self._send_to_js('protubeUpdateError', {'msg': f'Update failed: {e}'})
            finally:
                self._update_in_progress = False
        threading.Thread(target=worker, daemon=True).start()
        return True

    def _update_download_and_stage(self, url):
        """Worker: download archive, extract .app, emit ready event."""
        staging = self._update_staging_dir()
        shutil.rmtree(staging, ignore_errors=True)  # wipe prior attempt
        os.makedirs(staging, exist_ok=True)

        parsed_name = url.rsplit('/', 1)[-1].split('?', 1)[0] or 'protube_update.bin'
        download_path = os.path.join(staging, parsed_name)

        self._send_to_js('protubeUpdateProgress', {
            'percent': 0, 'state': 'downloading', 'msg': 'Downloading update…'
        })

        with requests.get(url, stream=True, timeout=30) as resp:
            if resp.status_code != 200:
                self._send_to_js('protubeUpdateError', {
                    'msg': f'Download failed (HTTP {resp.status_code})'
                })
                return
            total = int(resp.headers.get('Content-Length') or 0)
            downloaded = 0
            last_pct = -1
            with open(download_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = int(downloaded * 100 / total)
                        if pct != last_pct:
                            last_pct = pct
                            self._send_to_js('protubeUpdateProgress', {
                                'percent': pct, 'state': 'downloading',
                                'msg': f'Downloading… {pct}%'
                            })

        self._send_to_js('protubeUpdateProgress', {
            'percent': 100, 'state': 'extracting', 'msg': 'Extracting…'
        })
        staged_app = self._update_extract_app(download_path, staging)
        if not staged_app:
            return  # error already sent

        if not os.path.isdir(os.path.join(staged_app, 'Contents', 'MacOS')):
            self._send_to_js('protubeUpdateError', {
                'msg': "Downloaded archive didn't contain a valid .app bundle."
            })
            return

        self._staged_update_app = staged_app
        self._send_to_js('protubeUpdateReady', {
            'staged_app_path': staged_app,
            'install_to': self._current_app_bundle_path(),
        })

    def _update_extract_app(self, archive_path, staging):
        """Pull the .app out of a downloaded .dmg or .zip into staging/extracted.
        Returns absolute path to the staged .app, or None on failure (after
        having sent a protubeUpdateError event)."""
        name_lower = archive_path.lower()
        extract_dir = os.path.join(staging, 'extracted')
        os.makedirs(extract_dir, exist_ok=True)

        if name_lower.endswith('.dmg'):
            try:
                mount = subprocess.run(
                    ['hdiutil', 'attach', '-nobrowse', '-noverify',
                     '-mountrandom', '/tmp', archive_path],
                    capture_output=True, text=True, timeout=60,
                )
                if mount.returncode != 0:
                    self._send_to_js('protubeUpdateError', {
                        'msg': f'DMG mount failed: {mount.stderr[:200]}'
                    })
                    return None
                # hdiutil prints lines like: "/dev/diskNsM\t<fs>\t<mountpoint>"
                mount_point = None
                for line in mount.stdout.splitlines():
                    parts = line.split('\t')
                    if len(parts) >= 3 and parts[-1].strip().startswith('/'):
                        mount_point = parts[-1].strip()
                        break
                if not mount_point:
                    self._send_to_js('protubeUpdateError', {
                        'msg': 'Could not determine DMG mount point.'
                    })
                    return None
                try:
                    app_in_dmg = None
                    for name in os.listdir(mount_point):
                        if name.endswith('.app'):
                            app_in_dmg = os.path.join(mount_point, name)
                            break
                    if not app_in_dmg:
                        self._send_to_js('protubeUpdateError', {
                            'msg': '.dmg did not contain a .app bundle.'
                        })
                        return None
                    dest = os.path.join(extract_dir, os.path.basename(app_in_dmg))
                    shutil.copytree(app_in_dmg, dest, symlinks=True)
                    return dest
                finally:
                    subprocess.run(['hdiutil', 'detach', mount_point, '-force'],
                                   capture_output=True, timeout=30)
            except Exception as e:
                self._send_to_js('protubeUpdateError', {'msg': f'DMG extract failed: {e}'})
                return None

        if name_lower.endswith('.zip'):
            try:
                # ditto handles resource forks + symlinks correctly, unlike unzip
                r = subprocess.run(['ditto', '-x', '-k', archive_path, extract_dir],
                                   capture_output=True, text=True, timeout=120)
                if r.returncode != 0:
                    self._send_to_js('protubeUpdateError', {
                        'msg': f'Unzip failed: {r.stderr[:200]}'
                    })
                    return None
                for name in os.listdir(extract_dir):
                    if name.endswith('.app'):
                        return os.path.join(extract_dir, name)
                self._send_to_js('protubeUpdateError', {
                    'msg': '.zip did not contain a .app bundle.'
                })
                return None
            except Exception as e:
                self._send_to_js('protubeUpdateError', {'msg': f'Zip extract failed: {e}'})
                return None

        self._send_to_js('protubeUpdateError', {
            'msg': f'Unsupported archive type: {os.path.basename(archive_path)}'
        })
        return None

    def install_staged_update(self):
        """Spawn the detached helper script to swap the .app on disk, then
        quit this process. Returns True if helper was spawned successfully."""
        staged = getattr(self, '_staged_update_app', None)
        install_to = self._current_app_bundle_path()
        if not staged or not install_to or not os.path.isdir(staged):
            self._send_to_js('protubeUpdateError', {'msg': 'No staged update to install.'})
            return False

        helper = self._resolve_update_helper_path()
        if not helper:
            self._send_to_js('protubeUpdateError', {
                'msg': 'Update helper script not found in app bundle.'
            })
            return False

        try:
            subprocess.Popen(
                ['/bin/bash', helper, staged, install_to],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,  # detach so it survives our exit
            )
        except Exception as e:
            self._send_to_js('protubeUpdateError', {'msg': f"Couldn't start updater: {e}"})
            return False

        # Quit on a short delay so the JS side can show a "Restarting…" toast
        # before the window vanishes. Helper sleeps 2s before swapping, giving
        # the OS plenty of room to fully release the .app bundle's file locks.
        def _quit():
            try:
                for w in webview.windows:
                    try:
                        w.destroy()
                    except Exception:
                        pass
            except Exception:
                pass
            os._exit(0)
        threading.Timer(0.6, _quit).start()
        return True

    def get_active_progress(self):
        """Return current progress snapshot for any in-flight downloads. Used by the frontend
        after the window regains focus to re-sync any progress UI that may have missed
        ticks during background throttling."""
        out = []
        for vid in list(self.active_downloads.keys()):
            info = self._last_progress.get(vid) if hasattr(self, '_last_progress') else None
            if info:
                out.append({
                    'id': vid,
                    'pct': info.get('pct'),
                    'speed': info.get('speed'),
                    'playlist_id': info.get('playlist_id')
                })
        return out

    # ============================================================
    # Library — permanent collection of completed downloads.
    # Data shape: same as queue video entries, but with status always 'Done'
    # plus optional 'missing': True if the file no longer exists on disk.
    # ============================================================
