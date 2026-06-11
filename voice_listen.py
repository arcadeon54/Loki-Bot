"""
voice_listen.py — wake-word voice-channel listening (Phase 6).

Opt-in per session via /loki_ears. While enabled, per-user audio from the
voice channel is buffered, cut into utterances on ~0.8s of silence, and
transcribed LOCALLY with faster-whisper (nothing leaves the machine, no API
cost). Transcripts that don't contain the wake word are discarded
immediately — that's the privacy line: Loki only acts on speech addressed
to him.

CPU notes (dex247 N100): "base" int8 transcribes a 5s utterance in ~1-2s.
Override with LOKI_STT_MODEL=tiny|base|small.
"""

import asyncio
import audioop
import logging
import os
import re
import time

log = logging.getLogger("VoiceListen")

STT_MODEL  = os.getenv("LOKI_STT_MODEL", "base")
WAKE_WORDS = os.getenv("LOKI_WAKE_WORDS", "loki,lowkey,low-key,low key")
_WAKE_RE = re.compile(
    r"\b(" + "|".join(re.escape(w.strip()) for w in WAKE_WORDS.split(",") if w.strip()) + r")\b",
    re.IGNORECASE,
)

SILENCE_FLUSH_S = 0.8     # gap that ends an utterance
MIN_UTTERANCE_S = 0.4
MAX_UTTERANCE_S = 30.0
MIN_RMS         = 250     # discard near-silence so whisper doesn't hallucinate
SRC_RATE, DST_RATE = 48000, 16000
BYTES_PER_S = SRC_RATE * 2 * 2   # 48kHz, 16-bit, stereo

_model = None


def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        log.info(f"Loading faster-whisper '{STT_MODEL}' (int8, cpu)...")
        _model = WhisperModel(STT_MODEL, device="cpu", compute_type="int8")
        log.info("STT model loaded.")
    return _model


def is_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        from discord.ext import voice_recv  # noqa: F401
        return True
    except ImportError:
        return False


def has_wake_word(text: str) -> bool:
    return bool(_WAKE_RE.search(text))


def _transcribe_pcm(pcm: bytes) -> str:
    """48kHz stereo s16 PCM → text. Runs in a worker thread."""
    import numpy as np
    mono = audioop.tomono(pcm, 2, 0.5, 0.5)
    mono16k, _ = audioop.ratecv(mono, 2, 1, SRC_RATE, DST_RATE, None)
    audio = np.frombuffer(mono16k, dtype=np.int16).astype(np.float32) / 32768.0
    segments, _info = _get_model().transcribe(
        audio, language="en", beam_size=1, vad_filter=True,
        condition_on_previous_text=False,
    )
    return " ".join(s.text.strip() for s in segments).strip()


class SpeechCollector:
    """Buffers per-user PCM from a VoiceRecvClient and emits wake-word
    utterances to an async callback(member, transcript)."""

    def __init__(self, vc, on_wake, loop):
        self.vc = vc
        self.on_wake = on_wake
        self.loop = loop
        self.buffers: dict[int, dict] = {}   # user_id -> {pcm, last, member}
        self._task = None
        self._busy = False

    # Called from voice-recv's audio thread — keep it allocation-cheap.
    def _on_voice(self, member, data):
        if member is None or getattr(member, "bot", False):
            return
        buf = self.buffers.setdefault(
            member.id, {"pcm": bytearray(), "last": 0.0, "member": member}
        )
        if len(buf["pcm"]) < int(MAX_UTTERANCE_S * BYTES_PER_S):
            buf["pcm"] += data.pcm
        buf["last"] = time.monotonic()

    def start(self):
        from discord.ext import voice_recv
        self.vc.listen(voice_recv.BasicSink(self._on_voice))
        self._task = self.loop.create_task(self._flush_loop())
        log.info("Voice listening started")

    def stop(self):
        try:
            self.vc.stop_listening()
        except Exception:
            pass
        if self._task:
            self._task.cancel()
            self._task = None
        self.buffers.clear()
        log.info("Voice listening stopped")

    async def _flush_loop(self):
        while True:
            await asyncio.sleep(0.3)
            if not self.vc.is_connected():
                log.info("Voice client gone — listener shutting down")
                self.buffers.clear()
                return
            now = time.monotonic()
            for uid, buf in list(self.buffers.items()):
                if not buf["pcm"] or now - buf["last"] < SILENCE_FLUSH_S:
                    continue
                pcm = bytes(buf["pcm"])
                buf["pcm"] = bytearray()
                duration = len(pcm) / BYTES_PER_S
                if duration < MIN_UTTERANCE_S or audioop.rms(pcm, 2) < MIN_RMS:
                    continue
                if self._busy:
                    continue   # one utterance at a time on a small CPU
                self._busy = True
                try:
                    text = await asyncio.to_thread(_transcribe_pcm, pcm)
                    if text and has_wake_word(text):
                        log.info(f"Wake word from {buf['member'].display_name}: {text[:120]}")
                        await self.on_wake(buf["member"], text)
                    elif text:
                        log.debug("Utterance without wake word discarded")
                except Exception as e:
                    log.error(f"Transcription failed: {e}")
                finally:
                    self._busy = False
