# =============================================================================
# tts.py
# TTS Vanguard — Text-to-Speech dispatch module for the agent framework.
#
# Responsibilities:
#   - Chunk long text into segments no larger than TTS_SPLIT_CHUNKS characters
#   - Dispatch each chunk sequentially to the running tts_daemon_turbo.py
#     socket service with batch_mode=True and autoplay=False, preventing the
#     daemon from playing individual chunks before the full passage is ready
#   - Stitch all chunk WAVs (plus silence gaps) into one combined output WAV
#   - Play the combined WAV via aplay (single controlled playback event)
#   - Prune per-chunk WAVs and silence WAV after successful assembly
#   - Enforce a 10-second inter-passage cooldown so the daemon is never flooded
#     by rapid successive Kobold responses
#   - Gate silently on INTERACTIVE_MODE — TTS never fires in daemon/cron mode
#   - Suppress all console output when TTS_DEBUG=0 in settings_boolean
#
# Public API:
#   speak(text, settings, values)  — main entry point called from agent.py
#
# Configuration (all sourced from already-loaded runtime arrays — zero SQLite):
#   settings_boolean:  TTS              — 1 = enabled, 0 = disabled
#                      INTERACTIVE_MODE — must be 1 or TTS is suppressed
#                      TTS_DEBUG        — 1 = print progress, 0 = silent
#   settings_values:   TTS_SPLIT_CHUNKS — max characters per chunk (default 200)
#                      TTS_VOICE_REF    — path to the voice reference WAV
#
# Daemon protocol additions used here:
#   "autoplay":   false — daemon writes WAV but does NOT call aplay per chunk.
#                         tts.py plays only the final stitched combined WAV.
#   "batch_mode": true  — trusted caller flag; daemon skips FLOOD_COOLDOWN
#                         between chunks so batch dispatch is not rate-limited.
#
# Cooldown:
#   A 10-second module-level timestamp prevents a second passage from flooding
#   the daemon before the previous audio has finished playing. The cooldown is
#   per-process and resets when the application restarts.
# =============================================================================

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import wave

import db

# =============================================================================
# ── SECTION I: Configuration Constants ────────────────────────────────────────
# =============================================================================

_SOCKET_PATH         = "/tmp/echo_tts.sock"
_SOCKET_TIMEOUT      = 90          # Seconds to wait per synthesis job
_BUSY_RETRY_INTERVAL = 2.5         # Seconds between busy-retries per chunk
_BUSY_MAX_RETRIES    = 20          # Maximum retries before aborting a chunk
_GAP_SECONDS         = 0.5         # Silence between stitched chunks (seconds)
_PASSAGE_COOLDOWN    = 10.0        # Minimum seconds between successive passages
_SAMPLE_RATE         = 22050       # Chatterbox output sample rate (Hz, mono, 16-bit)

# Module-level cooldown timestamp — shared across all speak() calls this session
_last_passage_time: float = 0.0

# =============================================================================
# ── SECTION II: Debug Logger ──────────────────────────────────────────────────
# =============================================================================

# Module-level debug flag — set once at the top of speak() from the runtime
# arrays and consulted by _log() for the rest of the call. This avoids
# threading a debug parameter through every private function.
_debug: bool = False


def _log(msg: str):
    """Print msg to stdout only when TTS_DEBUG is active."""
    if _debug:
        print(msg)


def _log_err(msg: str):
    """Always print errors to stderr regardless of debug mode."""
    print(msg, file=sys.stderr)


# =============================================================================
# ── SECTION III: Text Chunker ─────────────────────────────────────────────────
# =============================================================================

def _chunk_text(text: str, max_chars: int) -> list[str]:
    """
    Split text into a list of segments where each segment is at most
    max_chars characters long.

    Strategy: split on sentence-ending punctuation first to avoid cutting
    mid-sentence. If a single sentence still exceeds max_chars, it is split
    hard at the character boundary as a last resort.

    Returns a list of non-empty stripped strings.
    """
    if len(text) <= max_chars:
        return [text.strip()] if text.strip() else []

    chunks: list[str] = []
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())

    current = ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        # Single sentence exceeds limit — hard-split it
        if len(sentence) > max_chars:
            if current:
                chunks.append(current.strip())
                current = ""
            for i in range(0, len(sentence), max_chars):
                piece = sentence[i:i + max_chars].strip()
                if piece:
                    chunks.append(piece)
            continue

        candidate = (current + " " + sentence).strip() if current else sentence
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current.strip())
            current = sentence

    if current.strip():
        chunks.append(current.strip())

    return [c for c in chunks if c]


# =============================================================================
# ── SECTION IV: Courier — Socket Dispatch ─────────────────────────────────────
# =============================================================================

def _courier_attempt(payload: str) -> dict:
    """
    Raw single socket throw — connect, send, receive one newline-terminated
    JSON response. Raises on hard failure (timeout, connection refused, etc.).
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(_SOCKET_TIMEOUT)
        sock.connect(_SOCKET_PATH)
        sock.sendall(payload.encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.decode("utf-8").strip())


def _courier_dispatch(text: str, voice_ref: str, out_path: str) -> float:
    """
    Send one chunk to the TTS daemon with automatic busy-retry.

    Sends batch_mode=True (bypasses daemon FLOOD_COOLDOWN between chunks)
    and autoplay=False (daemon writes WAV only — tts.py fires aplay once on
    the final stitched output, never on individual chunks).

    Returns the duration_sec of the produced WAV on success.
    Logs a warning and returns 0.0 if the daemon is unreachable or errors.
    """
    if not os.path.exists(_SOCKET_PATH):
        _log_err("[TTS] Daemon socket not found — is tts_daemon_turbo.py running?")
        return 0.0

    payload = json.dumps({
        "text":       text,
        "out":        out_path,
        "voice_ref":  voice_ref,
        "autoplay":   False,   # Suppress per-chunk playback — caller controls output
        "batch_mode": True,    # Bypass daemon FLOOD_COOLDOWN for sequential chunks
    }) + "\n"

    for attempt in range(1, _BUSY_MAX_RETRIES + 1):
        try:
            response = _courier_attempt(payload)
        except socket.timeout:
            _log_err("[TTS] Timed out waiting for daemon.")
            return 0.0
        except (ConnectionRefusedError, OSError) as e:
            _log_err(f"[TTS] Connection failed: {e}")
            return 0.0
        except json.JSONDecodeError as e:
            _log_err(f"[TTS] Malformed daemon response: {e}")
            return 0.0

        status  = response.get("status", "unknown")
        message = response.get("message", "")

        if status == "ok":
            return response.get("duration_sec", 0.0)
        elif status == "busy":
            _log(f"[TTS] Daemon busy (attempt {attempt}/{_BUSY_MAX_RETRIES}) — "
                 f"retrying in {_BUSY_RETRY_INTERVAL}s.")
            time.sleep(_BUSY_RETRY_INTERVAL)
        else:
            _log_err(f"[TTS] Daemon error: {message}")
            return 0.0

    _log_err(f"[TTS] Gave up after {_BUSY_MAX_RETRIES} busy retries.")
    return 0.0


# =============================================================================
# ── SECTION V: Silence Forge — Gap WAV Generator ──────────────────────────────
# =============================================================================

def _forge_silence(out_path: str, sample_rate: int, channels: int,
                   sampwidth: int, duration_sec: float = _GAP_SECONDS):
    """
    Write a silent WAV file of the given duration using parameters sourced
    from the first synthesised chunk WAV. This guarantees the silence segment
    is bit-identical in format to the chunks it separates, preventing the
    assembler from flagging a mismatch and skipping it.

    Call this AFTER at least one chunk has been synthesised so the real
    sample_rate, channels, and sampwidth are known from the actual output.
    """
    n_frames    = int(sample_rate * duration_sec)
    silent_data = bytes(sampwidth * channels * n_frames)

    with wave.open(out_path, "w") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(sample_rate)
        wf.writeframes(silent_data)


# =============================================================================
# ── SECTION VI: Assembler — WAV Stitcher ──────────────────────────────────────
# =============================================================================

def _assembler_stitch(wav_paths: list[str], out_path: str) -> bool:
    """
    Concatenate a list of WAV files into one output file.
    Parameters are read from the first file; mismatched files are skipped
    with a warning rather than corrupting the output.

    Returns True on success, False if wav_paths is empty.
    """
    if not wav_paths:
        _log_err("[TTS-Assembler] No WAVs to stitch.")
        return False

    with wave.open(wav_paths[0], "r") as ref:
        channels  = ref.getnchannels()
        sampwidth = ref.getsampwidth()
        framerate = ref.getframerate()

    with wave.open(out_path, "w") as out_wf:
        out_wf.setnchannels(channels)
        out_wf.setsampwidth(sampwidth)
        out_wf.setframerate(framerate)

        for path in wav_paths:
            with wave.open(path, "r") as wf:
                if (wf.getnchannels() != channels
                        or wf.getsampwidth() != sampwidth
                        or wf.getframerate() != framerate):
                    _log_err(f"[TTS-Assembler] Skipping mismatched WAV: "
                             f"{os.path.basename(path)}")
                    continue
                out_wf.writeframes(wf.readframes(wf.getnframes()))

    return True


# =============================================================================
# ── SECTION VII: Pruner — Temp File Cleanup ───────────────────────────────────
# =============================================================================

def _pruner_cull(paths: list[str]):
    """Delete a list of temporary WAV paths. Ignores already-missing files."""
    for path in paths:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            _log(f"[TTS-Pruner] Could not delete {os.path.basename(path)}: {e}")


# =============================================================================
# ── SECTION VIII: Public Entry Point ──────────────────────────────────────────
# =============================================================================

def speak(text: str, settings: list, values: list):
    """
    Main TTS entry point. Called from agent.py after every Kobold response
    when INTERACTIVE_MODE=1 and TTS=1.

    Pipeline:
      1. Gate checks  — INTERACTIVE_MODE, TTS flag, cooldown, socket present,
                        TTS_VOICE_REF path validated
      2. Chunk        — split text at TTS_SPLIT_CHUNKS boundary
      3. Dispatch     — send each chunk to daemon (autoplay=False, batch_mode=True)
                        daemon writes WAV only; no audio fires during this phase
      4. Silence      — forge gap WAV from first chunk's actual audio parameters
                        (read from the synthesised file — eliminates format mismatch)
      5. Stitch       — assemble chunks + silence gaps into one combined WAV
      6. Play         — fire aplay on the combined WAV only (single playback event)
      7. Prune        — delete per-chunk WAVs and silence WAV
      8. Cooldown     — record passage timestamp

    All configuration is read from the already-loaded runtime arrays.
    Zero SQLite queries are performed here.
    """
    global _debug, _last_passage_time

    # ── Resolve debug flag first — controls all subsequent _log() calls ───────
    _debug = db.resolve_setting(settings, "TTS_DEBUG", fallback=0) == 1

    # ── Gate 1: INTERACTIVE_MODE ──────────────────────────────────────────────
    if not db.resolve_setting(settings, "INTERACTIVE_MODE", fallback=0):
        return

    # ── Gate 2: TTS enabled ───────────────────────────────────────────────────
    if not db.resolve_setting(settings, "TTS", fallback=0):
        return

    # ── Gate 3: Cooldown ──────────────────────────────────────────────────────
    now     = time.monotonic()
    elapsed = now - _last_passage_time
    if elapsed < _PASSAGE_COOLDOWN:
        remaining = _PASSAGE_COOLDOWN - elapsed
        _log(f"[TTS] Cooldown active — {remaining:.1f}s remaining. Skipping.")
        return

    # ── Gate 4: Sanity checks ─────────────────────────────────────────────────
    text = text.strip()
    if not text:
        return

    if not os.path.exists(_SOCKET_PATH):
        _log_err("[TTS] Daemon socket not found — TTS skipped. "
                 "Start tts_daemon_turbo.py first.")
        return

    # ── Configuration from runtime arrays (zero SQLite) ──────────────────────
    try:
        max_chars = int(db.resolve_value(values, "TTS_SPLIT_CHUNKS", fallback="200"))
    except ValueError:
        max_chars = 200

    voice_ref = db.resolve_value(values, "TTS_VOICE_REF", fallback="")
    if not voice_ref or not os.path.isfile(voice_ref):
        _log_err(f"[TTS] TTS_VOICE_REF not set or file missing: '{voice_ref}'. TTS skipped.")
        return

    # ── Chunk ─────────────────────────────────────────────────────────────────
    chunks = _chunk_text(text, max_chars)
    if not chunks:
        return

    _log(f"[TTS] Speaking {len(chunks)} chunk(s) (max {max_chars} chars each)...")

    # ── Dispatch ──────────────────────────────────────────────────────────────
    # autoplay=False + batch_mode=True: daemon synthesises silently.
    # No audio plays during this entire phase. The speakers stay quiet
    # until the full passage has been assembled and handed to aplay below.
    tmp_dir    = tempfile.mkdtemp(prefix="tts_agent_")
    chunk_wavs : list[str] = []
    all_ok     = True

    for idx, chunk in enumerate(chunks, start=1):
        chunk_wav = os.path.join(tmp_dir, f"chunk_{idx:03d}.wav")
        _log(f"[TTS]   [{idx}/{len(chunks)}] "
             f"\"{chunk[:60]}{'...' if len(chunk) > 60 else ''}\"")
        duration = _courier_dispatch(chunk, voice_ref, chunk_wav)

        if duration > 0.0 and os.path.isfile(chunk_wav):
            chunk_wavs.append(chunk_wav)
        else:
            _log_err(f"[TTS] Chunk {idx} failed — aborting.")
            all_ok = False
            break

    if not all_ok or not chunk_wavs:
        _pruner_cull(chunk_wavs)
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass
        return

    # ── Silence Forge ─────────────────────────────────────────────────────────
    # Read format parameters from the first synthesised chunk WAV so the silence
    # segment is bit-identical. This eliminates the mismatch the assembler
    # previously reported when silence was forged with hardcoded values.
    stitch_queue : list[str] = []
    silence_path  = os.path.join(tmp_dir, "_silence.wav")

    if len(chunk_wavs) > 1:
        with wave.open(chunk_wavs[0], "r") as ref:
            real_rate     = ref.getframerate()
            real_channels = ref.getnchannels()
            real_width    = ref.getsampwidth()
        _forge_silence(silence_path, real_rate, real_channels, real_width)

    # Build stitch queue: chunk, silence, chunk, silence, ..., last chunk
    for idx, wav_path in enumerate(chunk_wavs):
        stitch_queue.append(wav_path)
        if idx < len(chunk_wavs) - 1:
            stitch_queue.append(silence_path)

    # ── Stitch ────────────────────────────────────────────────────────────────
    combined_path = os.path.join(tmp_dir, "combined.wav")
    success = _assembler_stitch(stitch_queue, combined_path)

    if not success or not os.path.isfile(combined_path):
        _log_err("[TTS] Stitch failed — no audio played.")
        _pruner_cull(chunk_wavs + ([silence_path] if os.path.exists(silence_path) else []))
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass
        return

    # ── Play — single controlled playback event ───────────────────────────────
    # This is the ONLY aplay call in the entire pipeline.
    # The daemon was instructed autoplay=False for every chunk, so no audio
    # has reached the speakers until this exact moment.
    subprocess.Popen(
        ["aplay", combined_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _log(f"[TTS] Playing combined WAV ({len(chunks)} chunk(s)).")

    # ── Prune per-chunk WAVs and silence ──────────────────────────────────────
    # combined.wav is intentionally left alive — aplay holds a file handle to it.
    # The OS will reclaim the inode once aplay closes it.
    _pruner_cull(chunk_wavs + ([silence_path] if os.path.exists(silence_path) else []))

    # ── Cooldown timestamp ────────────────────────────────────────────────────
    _last_passage_time = time.monotonic()
