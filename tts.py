# =============================================================================
# tts.py
# TTS Vanguard — Text-to-Speech dispatch module for the agent framework.
#
# Responsibilities:
#   - Chunk long text into segments no larger than TTS_SPLIT_CHUNKS characters
#   - Dispatch each chunk sequentially to the running tts_daemon_turbo.py
#     socket service, with busy-retry logic on each throw
#   - Stitch all chunk WAVs (plus silence gaps) into one combined output WAV
#   - Play the combined WAV via aplay
#   - Prune per-chunk WAVs and silence WAV after successful assembly
#   - Enforce a 10-second inter-passage cooldown so the daemon is never flooded
#     by rapid successive Kobold responses
#   - Gate silently on INTERACTIVE_MODE — TTS never fires in daemon/cron mode
#
# Public API:
#   speak(text, settings, values)  — main entry point called from agent.py
#
# Configuration (all sourced from already-loaded runtime arrays — zero SQLite):
#   settings_boolean:  TTS              — 1 = enabled, 0 = disabled
#                      INTERACTIVE_MODE — must be 1 or TTS is suppressed
#   settings_values:   TTS_SPLIT_CHUNKS — max characters per chunk (default 200)
#                      TTS_VOICE_REF    — path to the voice reference WAV
#
# Socket protocol (mirrors tts_daemon_turbo.py):
#   Request:  {"text": "...", "out": "/path/chunk.wav", "voice_ref": "/path/ref.wav"}
#   Response: {"status": "ok"|"busy"|"error", "message": "...", "duration_sec": 1.23}
#
# Cooldown:
#   A 10-second module-level timestamp prevents a second passage from flooding
#   the daemon before the previous audio has finished playing. If cooldown has
#   not elapsed, speak() logs a warning and returns without dispatching.
#   The cooldown is per-process — it resets when the application restarts.
# =============================================================================

import json
import os
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
# ── SECTION II: Text Chunker ──────────────────────────────────────────────────
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
    # Split on sentence boundaries — period/exclamation/question followed by space
    import re
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
            # Walk the long sentence in max_chars windows
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
# ── SECTION III: Courier — Socket Dispatch ────────────────────────────────────
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
    The daemon enforces its own FLOOD_COOLDOWN between jobs; 'busy' means
    back off and knock again — it is a queue position, not a fatal error.

    Returns the duration_sec of the produced WAV on success.
    Prints a warning and returns 0.0 if the daemon is unreachable or errors.
    """
    if not os.path.exists(_SOCKET_PATH):
        print("[TTS] Daemon socket not found — is tts_daemon_turbo.py running?",
              file=sys.stderr)
        return 0.0

    payload = json.dumps({
        "text":      text,
        "out":       out_path,
        "voice_ref": voice_ref,
    }) + "\n"

    for attempt in range(1, _BUSY_MAX_RETRIES + 1):
        try:
            response = _courier_attempt(payload)
        except socket.timeout:
            print("[TTS] Timed out waiting for daemon.", file=sys.stderr)
            return 0.0
        except (ConnectionRefusedError, OSError) as e:
            print(f"[TTS] Connection failed: {e}", file=sys.stderr)
            return 0.0
        except json.JSONDecodeError as e:
            print(f"[TTS] Malformed daemon response: {e}", file=sys.stderr)
            return 0.0

        status  = response.get("status", "unknown")
        message = response.get("message", "")

        if status == "ok":
            return response.get("duration_sec", 0.0)
        elif status == "busy":
            print(f"[TTS] Daemon busy (attempt {attempt}/{_BUSY_MAX_RETRIES}) — "
                  f"retrying in {_BUSY_RETRY_INTERVAL}s. ({message})")
            time.sleep(_BUSY_RETRY_INTERVAL)
        else:
            print(f"[TTS] Daemon error: {message}", file=sys.stderr)
            return 0.0

    print(f"[TTS] Gave up after {_BUSY_MAX_RETRIES} busy retries.", file=sys.stderr)
    return 0.0


# =============================================================================
# ── SECTION IV: Silence Forge — Gap WAV Generator ─────────────────────────────
# =============================================================================

def _forge_silence(out_path: str, duration_sec: float,
                   sample_rate: int = _SAMPLE_RATE):
    """
    Write a silent WAV file of the given duration.
    Mono, 16-bit, matching Chatterbox output spec so the assembler
    does not hit a sample-rate mismatch when stitching.
    """
    n_frames    = int(sample_rate * duration_sec)
    silent_data = b"\x00\x00" * n_frames   # 16-bit silence = two zero bytes per frame

    with wave.open(out_path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(silent_data)


# =============================================================================
# ── SECTION V: Assembler — WAV Stitcher ───────────────────────────────────────
# =============================================================================

def _assembler_stitch(wav_paths: list[str], out_path: str) -> bool:
    """
    Concatenate a list of WAV files into one output file.
    All inputs must share the same sample rate, channels, and bit depth.
    Parameters are read from the first file; mismatched files are skipped
    with a warning rather than corrupting the output.

    Returns True on success, False if wav_paths is empty.
    """
    if not wav_paths:
        print("[TTS-Assembler] No WAVs to stitch.", file=sys.stderr)
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
                    print(f"[TTS-Assembler] Skipping mismatched WAV: "
                          f"{os.path.basename(path)}")
                    continue
                out_wf.writeframes(wf.readframes(wf.getnframes()))

    return True


# =============================================================================
# ── SECTION VI: Pruner — Temp File Cleanup ────────────────────────────────────
# =============================================================================

def _pruner_cull(paths: list[str]):
    """Delete a list of temporary WAV paths. Ignores already-missing files."""
    for path in paths:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"[TTS-Pruner] Could not delete {os.path.basename(path)}: {e}")


# =============================================================================
# ── SECTION VII: Public Entry Point ───────────────────────────────────────────
# =============================================================================

def speak(text: str, settings: list, values: list):
    """
    Main TTS entry point. Called from agent.py after every Kobold response
    when INTERACTIVE_MODE=1 and TTS=1.

    Pipeline:
      1. Gate checks  — INTERACTIVE_MODE, TTS flag, cooldown, daemon socket
      2. Chunk        — split text at TTS_SPLIT_CHUNKS boundary
      3. Dispatch     — send each chunk to daemon, receive per-chunk WAV
      4. Stitch       — assemble chunks + silence gaps into one combined WAV
      5. Play         — fire aplay on the combined WAV (non-blocking)
      6. Prune        — delete per-chunk WAVs and silence WAV
      7. Cooldown     — record passage timestamp

    All configuration is read from the already-loaded runtime arrays.
    Zero SQLite queries are performed here.
    """
    global _last_passage_time

    # ── Gate 1: INTERACTIVE_MODE ──────────────────────────────────────────────
    # TTS is exclusively a human-facing feature. Never fire in daemon/cron mode.
    interactive = db.resolve_setting(settings, "INTERACTIVE_MODE", fallback=0)
    if not interactive:
        return

    # ── Gate 2: TTS enabled ───────────────────────────────────────────────────
    tts_enabled = db.resolve_setting(settings, "TTS", fallback=0)
    if not tts_enabled:
        return

    # ── Gate 3: Cooldown ──────────────────────────────────────────────────────
    now     = time.monotonic()
    elapsed = now - _last_passage_time
    if elapsed < _PASSAGE_COOLDOWN:
        remaining = _PASSAGE_COOLDOWN - elapsed
        print(f"[TTS] Cooldown active — {remaining:.1f}s remaining. Skipping.")
        return

    # ── Gate 4: Sanity checks ─────────────────────────────────────────────────
    text = text.strip()
    if not text:
        return

    if not os.path.exists(_SOCKET_PATH):
        print("[TTS] Daemon socket not found — TTS skipped. "
              "Start tts_daemon_turbo.py first.", file=sys.stderr)
        return

    # ── Configuration from runtime arrays (zero SQLite) ──────────────────────
    try:
        max_chars = int(db.resolve_value(values, "TTS_SPLIT_CHUNKS", fallback="200"))
    except ValueError:
        max_chars = 200

    voice_ref = db.resolve_value(values, "TTS_VOICE_REF", fallback="")
    if not voice_ref or not os.path.isfile(voice_ref):
        print(f"[TTS] TTS_VOICE_REF not set or file missing: '{voice_ref}'. "
              f"TTS skipped.", file=sys.stderr)
        return

    # ── Chunk ─────────────────────────────────────────────────────────────────
    chunks = _chunk_text(text, max_chars)
    if not chunks:
        return

    print(f"[TTS] Speaking {len(chunks)} chunk(s) "
          f"(max {max_chars} chars each)...")

    # ── Dispatch + stitch queue ───────────────────────────────────────────────
    tmp_dir      = tempfile.mkdtemp(prefix="tts_agent_")
    stitch_queue : list[str] = []
    chunk_wavs   : list[str] = []

    # Build one reusable silence WAV for gaps between chunks
    silence_path = os.path.join(tmp_dir, "_silence.wav")
    _forge_silence(silence_path, _GAP_SECONDS)

    all_ok = True
    for idx, chunk in enumerate(chunks, start=1):
        chunk_wav = os.path.join(tmp_dir, f"chunk_{idx:03d}.wav")
        print(f"[TTS]   [{idx}/{len(chunks)}] \"{chunk[:60]}{'...' if len(chunk) > 60 else ''}\"")
        duration = _courier_dispatch(chunk, voice_ref, chunk_wav)

        if duration > 0.0 and os.path.isfile(chunk_wav):
            stitch_queue.append(chunk_wav)
            chunk_wavs.append(chunk_wav)
            # Silence gap after every chunk except the last
            if idx < len(chunks):
                stitch_queue.append(silence_path)
        else:
            print(f"[TTS] Chunk {idx} failed — aborting stitch.", file=sys.stderr)
            all_ok = False
            break

    if not all_ok or not stitch_queue:
        _pruner_cull(chunk_wavs + [silence_path])
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass
        return

    # ── Stitch ────────────────────────────────────────────────────────────────
    combined_path = os.path.join(tmp_dir, "combined.wav")
    success = _assembler_stitch(stitch_queue, combined_path)

    if not success or not os.path.isfile(combined_path):
        print("[TTS] Stitch failed — no audio played.", file=sys.stderr)
        _pruner_cull(chunk_wavs + [silence_path])
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass
        return

    # ── Play ──────────────────────────────────────────────────────────────────
    subprocess.Popen(
        ["aplay", combined_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"[TTS] Playing combined WAV ({len(chunks)} chunk(s)).")

    # ── Prune per-chunk WAVs and silence (combined is left for aplay) ─────────
    _pruner_cull(chunk_wavs + [silence_path])

    # ── Cooldown timestamp ────────────────────────────────────────────────────
    _last_passage_time = time.monotonic()
