"""A second opinion on every wake: the board's clip of what the wake word heard goes to xAI speech-to-text.

The wake word model on the board sometimes fires on room noise (a creak, a whistle). Its clip (the last 3 s it heard,
server.py, /keryx/wake-clip) goes to xAI's batch speech-to-text with the wake phrase as key terms (`keyterm`), which
pull an unclear "Hey Kerix" towards the right spelling: without them xAI wrote "Hey kids", "Take care", "Hey, Gary"
for real wakes; with them all 36 of an evening's wakes came back as "Hey Keryx" or "Хэй Керикс", while a whistle, a
squeak, a hiss and clicks came back empty, in 0.3-0.5 s. A transcript without a word that is Keryx in any of its
spellings means a false wake: the bridge ends the conversation it started (server.py, Bridge.check_wake).

The key terms pull towards Keryx also what only sounds a bit like it ("Hey, Eric"): this catches noise, not words.
"""

import aiohttp

from .voice import KERYX, LATIN, words

URL = "https://api.x.ai/v1/stt"
DEFAULT_KEYTERMS = "Hey Keryx,Хэй Керикс,Керикс,Keryx"


def says_keryx(text):
    """Whether a transcript has a word that is Keryx in one of its spellings (Kerix, Kirex, Керикс, …)."""
    return any(KERYX.fullmatch("".join(LATIN.get(c, c) for c in word)) for word in words(text))


async def transcribe(session, headers, wav, keyterms, timeout):
    """xAI's transcript of a WAV clip with the key terms; raises on an HTTP error or after timeout seconds."""
    form = aiohttp.FormData()
    for term in keyterms:
        form.add_field("keyterm", term)
    form.add_field("file", wav, filename="wake.wav", content_type="audio/wav")
    async with session.post(URL, data=form, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
        body = await resp.json(content_type=None)
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}: {str(body)[:200]}")
        return str(body.get("text", ""))
