"""Async client for the xAI text-to-speech API.

POST /v1/tts returns base64 audio plus per-character timestamps; we always ask for 16 kHz 16-bit mono PCM, the
format the wake word front-end consumes. See https://docs.x.ai/developers/model-capabilities/audio/text-to-speech.
"""

import asyncio
import base64
import os
from pathlib import Path

import aiohttp

API = "https://api.x.ai/v1"
RATE = 16000
REPO = Path(__file__).resolve().parent.parent

# wrapping speech tags the API understands; "none" sends the text as is
STYLES = ["none", "soft", "loud", "whisper", "singing", "fast", "slow", "pitch-high", "pitch-low"]


class TTSError(RuntimeError):
    pass


def api_key():
    """XAI_API_KEY from the environment, else from the repository's .env."""
    if key := os.environ.get("XAI_API_KEY"):
        return key
    env = REPO / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "XAI_API_KEY":
                return value.strip().strip("'\"")
    raise TTSError(f"XAI_API_KEY is not set and not found in {env}")


def styled(text, style):
    return text if style == "none" else f"<{style}>{text}</{style}>"


class XAITTS:
    def __init__(self, key=None, concurrency=8, retries=6):
        self.key = key or api_key()
        self.limit = asyncio.Semaphore(concurrency)
        self.retries = retries
        self.session = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self.key}"},
            timeout=aiohttp.ClientTimeout(total=120),
        )
        return self

    async def __aexit__(self, *exc):
        await self.session.close()

    async def voices(self):
        async with self.session.get(f"{API}/tts/voices") as resp:
            if resp.status != 200:
                raise TTSError(f"voices: HTTP {resp.status}: {await resp.text()}")
            return (await resp.json())["voices"]

    async def synthesize(self, text, voice, language, speed=1.0):
        """Returns (pcm bytes, timestamps) where timestamps is a list of (char, start_s, end_s)."""
        body = {
            "text": text,
            "voice_id": voice,
            "language": language,
            "speed": speed,
            "output_format": {"codec": "pcm", "sample_rate": RATE},
            "with_timestamps": True,
        }
        delay = 1.0
        async with self.limit:
            for attempt in range(self.retries):
                try:
                    async with self.session.post(f"{API}/tts", json=body) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            ts = data.get("audio_timestamps") or {}
                            chars = zip(ts.get("graph_chars", []), ts.get("graph_times", []))
                            return base64.b64decode(data["audio"]), [(c, t[0], t[1]) for c, t in chars]
                        error = f"HTTP {resp.status}: {(await resp.text())[:300]}"
                        if resp.status != 429 and resp.status < 500:
                            raise TTSError(error)
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    error = repr(e)
                if attempt + 1 < self.retries:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)
        raise TTSError(f"gave up after {self.retries} attempts: {error}")
