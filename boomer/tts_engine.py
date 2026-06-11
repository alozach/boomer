import logging
import os
import tempfile
import threading
import pyttsx3
from boomer.sound_player import SoundPlayer

logger = logging.getLogger(__name__)


class TtsEngine:
    def __init__(self, player: SoundPlayer):
        self.player = player
        self._lock = threading.Lock()
        self._current_voice: str | None = None

    def list_voices(self) -> list[dict]:
        engine = pyttsx3.init()
        voices = engine.getProperty("voices") or []
        result = [{"id": v.id, "name": v.name or v.id} for v in voices]
        engine.stop()
        return result

    def set_voice(self, identifier: str) -> str | None:
        """Find a voice by partial ID or name match. Returns the voice ID if found, None otherwise."""
        engine = pyttsx3.init()
        voices = engine.getProperty("voices") or []
        engine.stop()
        identifier_lower = identifier.lower()
        for v in voices:
            if identifier_lower in v.id.lower() or identifier_lower in (v.name or "").lower():
                self._current_voice = v.id
                return v.id
        return None

    def get_current_voice(self) -> str | None:
        return self._current_voice

    def speak(self, text: str):
        with self._lock:
            tmp_path = None
            try:
                engine = pyttsx3.init()
                if self._current_voice:
                    engine.setProperty("voice", self._current_voice)
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                    tmp_path = f.name
                engine.save_to_file(text, tmp_path)
                engine.runAndWait()
                engine.stop()
                self.player.play_file(tmp_path)
            except Exception:
                logger.exception("TTS failed for text: %s", text)
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)
