import asyncio
import logging
import os
import tempfile
import threading
import edge_tts
from boomer.sound_player import SoundPlayer

logger = logging.getLogger(__name__)


DEFAULT_RATE = 160
DEFAULT_LANG = "fr"

# Maps short lang codes to edge-tts neural voice names
LANG_MAP: dict[str, str] = {
    "fr": "fr-FR-DeniseNeural",
    "fr-ca": "fr-CA-SylvieNeural",
    "fr-be": "fr-BE-CharlineNeural",
    "en": "en-US-JennyNeural",
    "en-gb": "en-GB-SoniaNeural",
    "es": "es-ES-ElviraNeural",
    "es-lat": "es-MX-DaliaNeural",
    "de": "de-DE-KatjaNeural",
    "it": "it-IT-ElsaNeural",
    "pt": "pt-PT-RaquelNeural",
    "pt-br": "pt-BR-FranciscaNeural",
    "nl": "nl-NL-ColetteNeural",
    "pl": "pl-PL-ZofiaNeural",
    "ru": "ru-RU-SvetlanaNeural",
    "uk": "uk-UA-PolinaNeural",
    "cs": "cs-CZ-VlastaNeural",
    "ro": "ro-RO-AlinaNeural",
    "hu": "hu-HU-NoemiNeural",
    "sv": "sv-SE-SofieNeural",
    "da": "da-DK-ChristelNeural",
    "fi": "fi-FI-NooraNeural",
    "el": "el-GR-AthinaNeural",
    "tr": "tr-TR-EmelNeural",
    "ar": "ar-SA-ZariyahNeural",
    "he": "he-IL-HilaNeural",
    "hi": "hi-IN-SwaraNeural",
    "zh": "zh-CN-XiaoxiaoNeural",
    "ja": "ja-JP-NanamiNeural",
    "ko": "ko-KR-SunHiNeural",
}


def _rate_to_pct(rate: int) -> str:
    pct = round((rate - 160) / 1.6)
    return f"+{pct}%" if pct >= 0 else f"{pct}%"


class TtsEngine:
    def __init__(self, player: SoundPlayer):
        self.player = player
        self._lock = threading.Lock()
        self._rate: int = DEFAULT_RATE

    def list_voices(self) -> list[dict]:
        return [{"code": code, "voice": voice} for code, voice in LANG_MAP.items()]

    def get_rate(self) -> int:
        return self._rate

    def set_rate(self, rate: int) -> int:
        self._rate = max(50, min(400, rate))
        return self._rate

    def speak(self, text: str, lang: str | None = None):
        voice = LANG_MAP.get((lang or DEFAULT_LANG).lower(), LANG_MAP[DEFAULT_LANG])
        rate_str = _rate_to_pct(self._rate)
        with self._lock:
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                    tmp_path = f.name
                communicate = edge_tts.Communicate(text, voice, rate=rate_str)
                asyncio.run(communicate.save(tmp_path))
                self.player.play_file(tmp_path)
            except Exception:
                logger.exception("TTS failed for text: %s", text)
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)
