import logging
import os
import tempfile
import threading
import pyttsx3
from boomer.sound_player import SoundPlayer

logger = logging.getLogger(__name__)


DEFAULT_RATE = 160
DEFAULT_LANG = "fr"

LANG_MAP: dict[str, str] = {
    # French variants
    "fr": "fr-fr",
    "fr-be": "fr-be",
    "fr-ch": "fr-ch",
    # English variants
    "en": "en-us",
    "en-gb": "en-gb",
    "en-us": "en-us",
    # Major European languages
    "es": "es",
    "es-lat": "es-419",
    "de": "de",
    "it": "it",
    "pt": "pt",
    "pt-br": "pt-br",
    "nl": "nl",
    "pl": "pl",
    "ru": "ru",
    "uk": "uk",
    "cs": "cs",
    "ro": "ro",
    "hu": "hu",
    "sv": "sv",
    "da": "da",
    "fi": "fi",
    "el": "el",
    "tr": "tr",
    # Other
    "ar": "ar",
    "he": "he",
    "hi": "hi",
    "zh": "cmn",
    "ja": "ja",
    "ko": "ko",
}


def _decode(val) -> str:
    return val.decode() if isinstance(val, bytes) else str(val)


class TtsEngine:
    def __init__(self, player: SoundPlayer):
        self.player = player
        self._lock = threading.Lock()
        self._rate: int = DEFAULT_RATE

    def _resolve_voice(self, lang: str) -> str | None:
        code = LANG_MAP.get(lang.lower(), lang.lower())
        engine = pyttsx3.init()
        voices = engine.getProperty("voices") or []
        engine.stop()
        for v in voices:
            langs = [_decode(l) for l in (v.languages or [])]
            if code in _decode(v.id).lower() or any(code in l.lower() for l in langs):
                return v.id
        return None

    def list_voices(self) -> list[dict]:
        engine = pyttsx3.init()
        voices = engine.getProperty("voices") or []
        engine.stop()
        result = []
        for code, espeak_code in LANG_MAP.items():
            matched = next(
                (v for v in voices
                 if espeak_code in _decode(v.id).lower()
                 or any(espeak_code in _decode(l).lower() for l in (v.languages or []))),
                None,
            )
            result.append({
                "code": code,
                "lang": espeak_code,
                "id": matched.id if matched else None,
            })
        return result

    def get_rate(self) -> int:
        return self._rate

    def set_rate(self, rate: int) -> int:
        self._rate = max(50, min(400, rate))
        return self._rate

    def speak(self, text: str, lang: str | None = None):
        voice_id = self._resolve_voice(lang or DEFAULT_LANG)
        with self._lock:
            tmp_path = None
            try:
                engine = pyttsx3.init()
                engine.setProperty("rate", self._rate)
                if voice_id:
                    engine.setProperty("voice", voice_id)
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
