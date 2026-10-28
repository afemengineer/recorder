# Recorder — Narration Studio

A self-hosted script-guided narration studio for **phone recording + desktop review**. All recordings and metadata stay on your own PC. No cloud APIs, accounts or GPU required for recording.

## What works in v0.1

- Import scripts as paragraph-based projects; edit individual reference paragraphs or append sections.
- Responsive phone teleprompter, adjustable font size, Record / Stop / Retake, in-browser WAV/PCM capture.
- Lossless **mono PCM16** audio captured through an AudioWorklet, split into ~0.5-second chunks.
- Persist recording chunks to phone IndexedDB **before uploading**; retry uploads, recover interrupted sessions, server-side idempotent chunk storage.
- Store every completed take; automatically select the most recent one and optionally switch back to previous versions.
- Listen to alternatives, view waveform, trim the selected audio non-destructively, and edit/verify the *actual* spoken transcript.
- Export an assembled rough-cut WAV (250 ms between sections) or ZIP of original selected WAVs, reference script and a structured corpus manifest.
- Optional local transcription using `faster-whisper`. Script text is a reference, never falsely presented as an exact audio transcript.
- SQLite-backed library and optional password-protected LAN access.

**Not yet included:** reliable automated retake detection inside a single take, audio-LLM performance scoring, semantic script alignment, native NLE timeline editing, automatic subtitle export. The foundation deliberately does not claim to perform these tasks.

## Run on Windows (PowerShell)

```powershell
git clone https://github.com/afemengineer/recorder.git
cd recorder
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
$env:RECORDER_PASSWORD = "choose-a-strong-password"
python -m recorder
```

Open **http://127.0.0.1:8000** on the PC. The recorder uses `./recorder-data` by default; override with `RECORDER_DATA_DIR` to store recordings on a larger disk. Back up this directory: it contains original audio chunks, finalized WAVs, and the SQLite database.

### Use the phone over Wi-Fi

Browsers block microphone access on ordinary LAN HTTP addresses. **Use HTTPS**. One simple approach is [Caddy](https://caddyserver.com/docs/) as a reverse proxy:

1. Assign your PC a stable LAN IP, e.g. `192.168.1.50`, and install Caddy on Windows.
2. Copy `Caddyfile.example` to `Caddyfile`. Set `$env:LAN_IP = "192.168.1.50"` in PowerShell, then run `caddy run --config Caddyfile`.
3. Install/trust the Caddy **local CA root certificate** on your phone (Caddy documents its local HTTPS trust procedure). The phone must trust the root, and the certificate must cover the exact LAN IP you visit.
4. Open `https://192.168.1.50:8443` on the phone, on the same Wi-Fi. Allow microphone access. Ensure Windows Firewall permits inbound TCP 8443 to Caddy.

The Python app remains bound to **127.0.0.1**; the reverse proxy handles LAN access and HTTPS. Set `RECORDER_PASSWORD` before allowing other LAN devices to connect. Do **not** port-forward the recorder to the public internet. On mobile, keep the browser foreground and screen unlocked during recording: background capture cannot be guaranteed by the browser.

If using a directly exposed server (`python -m recorder --host 0.0.0.0`), a password is required by the launcher, but **that alone does not provide HTTPS** and therefore will not enable phone microphone access. The recommended architecture is Caddy + loopback-only backend.

## Optional offline speech-to-text

```powershell
pip install -e ".[transcribe]"
$env:RECORDER_WHISPER_MODEL = "small.en"
$env:RECORDER_WHISPER_DEVICE = "cpu"
$env:RECORDER_WHISPER_COMPUTE = "int8"
python -m recorder
```

The desktop review page exposes a **Transcribe locally** button when faster-whisper is installed. The model downloads weights on first use unless cached. Transcription is **unverified** until you inspect and approve the result. Technical terms, improvised transitions and false starts may need correction. It is not a semantic quality score.

If using a supported NVIDIA CUDA configuration, faster-whisper can be configured for GPU inference; GPU backend requirements depend on your CUDA/cuDNN stack.

## Dataset policy

`export.zip` preserves original WAVs and an `manifest.json` including script reference, spoken transcript (if present), human verification flag and trim boundaries. **Only `transcript_reviewed=true` audio/text pairs should become supervised TTS training examples.** Trims affect the rough cut, but original recordings remain intact. Do not use the reference script as the true transcript when you improvise words.

## Tests

```bash
pip install -e '.[dev]'
pytest -q
```

Tests exercise project creation, upload idempotency, gap detection, WAV integrity, selection, trimming, export, authentication, and input validation. Browser recording still requires a real-device smoke test (especially iOS/Android microphone behavior and HTTPS certificate trust).

## API / storage overview

```text
recorder-data/
  recorder.sqlite3
  .session_key
  takes/<take-id>/
    0000000.pcm   # immutable source chunk
    0000001.pcm
    audio.wav     # finalized mono 16-bit WAV
```

Chunks are stored immutably; duplicate PUTs are safe when bytes match. `finish` checks that all indices from 0 to `expected_chunks-1` exist before writing WAV. A separate phone-side IndexedDB queue persists audio chunks until the server acknowledges them. Keep the phone app open until the upload is confirmed when possible.

*Roadmap:* proper editor decision lists, click-to-mark trims, automatic disfluency detection, pairwise audio-LLM evaluation, SRT export, human preference annotation, speaker-dataset curation and automated quality gates.