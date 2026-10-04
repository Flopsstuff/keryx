# Speech providers

Cloud speech-to-text and text-to-speech candidates for the voice bridge between Keryx and Hermes. STT and TTS run
as external APIs so the Raspberry Pi 5 next to Hermes is not loaded with speech models.

## Shortlist for a Russian-speaking speaker

Monthly cost estimates for about 600 conversations a month, from external research (2026-10).

| Option | Estimate / month | Strengths |
|---|---:|---|
| Azure STT + TTS | ~$0 within the free tier | cheap baseline, Russian supported |
| Google STT + Chirp 3 HD | ~$1.6 | strong general-purpose stack |
| xAI STT + TTS | ~$1.6 | very cheap streaming STT with Smart Turn / VAD |
| Deepgram Flux + Azure/Google TTS | ~$1–3 | strong conversational STT and endpointing |
| OpenAI STT + TTS | ~$4–5 | good realtime stack, easy integration with an LLM |
| ElevenLabs STT + TTS | ~$5 | focus on voice quality and naturalness |
| AWS Transcribe + Polly | ~$2–3 | cheap, but Russian Polly ranks lower on quality |

## Measured latency: xAI and Groq

Measured 2026-10-04 from a Mac behind the same home router as the Pi (the Pi reaches `api.x.ai` and
`api.groq.com` in about 30 ms TCP / 55 ms TLS). Input: 20 short Russian voice commands synthesized by xAI TTS, so
the speech is clean; xAI streaming STT received it in real time as 20 ms PCM frames at 16 kHz, the way the board
will send it.

### Speech-to-text: end of speech → final text

| Option | Median | p90 | Notes |
|---|---:|---:|---|
| xAI streaming, `endpointing=400` (default) | 1233 ms | 1368 ms | |
| xAI streaming, `endpointing=200` | 1031 ms | 1160 ms | cut one phrase at an inner pause |
| xAI streaming, `smart_turn=0.5` | 878 ms | 1176 ms | one outlier at 1.9 s |
| Groq `whisper-large-v3-turbo` | 188 ms | 307 ms | request time only, see below |
| Groq `whisper-large-v3` | 310 ms | 362 ms | request time only, see below |

- Groq is batch-only: the bridge has to detect the end of speech itself, so a local VAD silence wait (typically
  400–600 ms) adds to its numbers. With that, Groq comes to roughly 0.6–0.8 s against 0.9–1.2 s for xAI.
- Accuracy was the same on both: every transcript was right except «хлоп» for «хлеб» (all models) and «Виключи»
  (Groq turbo). Numbers come back as digits.
- Opening the xAI STT WebSocket takes 410–460 ms, so the bridge should open it on `wake`, in parallel with the
  pre-roll.
- Groq rejected the 20th request in a row with a rate limit (about 20 requests a minute per model on the current
  tier).

### Text-to-speech: request → first audio

First sentences of 8 typical replies (40–110 characters), 24 kHz PCM.

| Option | Median | p90 | Notes |
|---|---:|---:|---|
| xAI WebSocket, connection already open | 360 ms | 406 ms | |
| xAI WebSocket, `optimize_streaming_latency=2` | 391 ms | 429 ms | no gain |
| xAI WebSocket, new connection per phrase | 997 ms | 1021 ms | connecting costs ~520 ms |
| xAI REST | 1086 ms | 1334 ms | returns the whole audio at once |
| Edge TTS `ru-RU-SvetlanaNeural` | 290 ms | 307 ms | 1313 ms median in an earlier run; `ru-RU-DmitryNeural` returned no audio |
| Groq `canopylabs/orpheus-v1-english` | ~800 ms | | single phrase; whole WAV at once |

- Groq TTS has no Russian model (English and Saudi Arabic only). It does read Cyrillic, but with a strong English
  accent: Whisper with automatic language detection hears «Готово, таймер на десять минут запущен» as English,
  «Gatova! Timer na 10 minutes zapushan». It is also capped at 200 characters per request and costs more than xAI.
- xAI TTS is billed per character, not per connection, so the bridge can keep its WebSocket open all the time.

### Prices used

| Service | Price |
|---|---|
| xAI STT | $0.20 / h streaming, $0.10 / h REST |
| xAI TTS | $15 / 1M characters |
| Groq `whisper-large-v3-turbo` | $0.04 / h, at least 10 s billed per request |
| Groq `whisper-large-v3` | $0.111 / h, at least 10 s billed per request |
| Groq Orpheus TTS (English) | $22 / 1M characters |

TTS is about 90 % of the speech bill, so the provider is chosen by TTS and STT comes from the same provider. An xAI
STT session should stay open only for one utterance: how xAI bills an idle streaming connection is not documented,
and a session left open around the clock would cost about $144 a month.
