"""Lightweight Groq API client for ProTube's AI features (F7 summary, F8 subtitle polish).

Why Groq specifically: free tier is generous, latency is fast (Groq's hardware), and the
Llama 3.3 70B model is more than capable for caption cleanup + article-style summarization.
User decision (2026-05-05): use Groq instead of paid OpenAI/Anthropic to keep ProTube
zero-cost-to-run for end users.

API key lives in settings.json under `groq_api_key`. Users get one free at console.groq.com.
No key configured → AI features stay disabled; the rest of the app works normally.
"""

import json
import requests


class GroqError(Exception):
    """Raised for any Groq call failure — network, auth, rate limit, bad response."""
    pass


class GroqClient:
    # llama-3.3-70b-versatile is the strongest model on Groq's free tier as of 2026-05.
    # 32k context — fits ~12k words of subtitles per call (≈ 1hr video transcript).
    DEFAULT_MODEL = 'llama-3.3-70b-versatile'
    URL = 'https://api.groq.com/openai/v1/chat/completions'

    def __init__(self, api_key, model=None):
        self.api_key = api_key
        self.model = model or self.DEFAULT_MODEL

    def chat(self, system, user, max_tokens=4000, temperature=0.3):
        """Run a single chat completion and return the assistant's text response.

        temperature=0.3 keeps subtitle cleanup deterministic-ish (we don't want creative
        rewrites — we want the same captions, fixed). Summary calls can pass higher.
        """
        if not self.api_key:
            raise GroqError('No Groq API key configured. Add one in Settings.')

        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
        }
        body = {
            'model': self.model,
            'messages': [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': user},
            ],
            'max_tokens': max_tokens,
            'temperature': temperature,
        }

        try:
            resp = requests.post(self.URL, json=body, headers=headers, timeout=60)
        except requests.RequestException as e:
            raise GroqError(f'Network error reaching Groq: {e}')

        if resp.status_code == 401:
            raise GroqError('Groq rejected the API key. Check it in Settings.')
        if resp.status_code == 429:
            raise GroqError('Groq rate limit hit. Try again in a minute.')
        if resp.status_code >= 500:
            raise GroqError(f'Groq is having a bad day (HTTP {resp.status_code}). Try later.')
        if resp.status_code != 200:
            raise GroqError(f'Groq HTTP {resp.status_code}: {resp.text[:200]}')

        try:
            data = resp.json()
            return data['choices'][0]['message']['content']
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            raise GroqError(f'Unexpected Groq response shape: {e}')
