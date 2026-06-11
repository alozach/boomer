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
        # pyttsx3 n'est pas thread-safe : on initialise dans le thread d'utilisation
        self._lock = threading.Lock()

    def speak(self, text: str):
        with self._lock:
            try:
                engine = pyttsx3.init()
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                    tmp_path = f.name
                engine.save_to_file(text, tmp_path)
                engine.runAndWait()
                engine.stop()
                self.player.play_file(tmp_path)
            except Exception:
                logger.exception("Erreur TTS pour le texte : %s", text)
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
