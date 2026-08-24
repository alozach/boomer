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

# Maps short lang codes to (voice name, human-readable label)
LANG_MAP: dict[str, tuple[str, str]] = {
    "fr":     ("fr-FR-DeniseNeural",     "Français (France)"),
    "fr-ca":  ("fr-CA-SylvieNeural",     "Français (Canada)"),
    "fr-be":  ("fr-BE-CharlineNeural",   "Français (Belgique)"),
    "en":     ("en-US-JennyNeural",      "English (US)"),
    "en-gb":  ("en-GB-SoniaNeural",      "English (UK)"),
    "es":     ("es-ES-ElviraNeural",     "Espagnol"),
    "de":     ("de-DE-KatjaNeural",      "Allemand"),
    "it":     ("it-IT-ElsaNeural",       "Italien"),
    "pt":     ("pt-PT-RaquelNeural",     "Portugais"),
    "nl":     ("nl-NL-ColetteNeural",    "Néerlandais"),
    "ru":     ("ru-RU-SvetlanaNeural",   "Russe"),
    "uk":     ("uk-UA-PolinaNeural",     "Ukrainien"),
    "cs":     ("cs-CZ-VlastaNeural",     "Tchèque"),
    "ro":     ("ro-RO-AlinaNeural",      "Roumain"),
    "hu":     ("hu-HU-NoemiNeural",      "Hongrois"),
    "sv":     ("sv-SE-SofieNeural",      "Suédois"),
    "da":     ("da-DK-ChristelNeural",   "Danois"),
    "fi":     ("fi-FI-NooraNeural",      "Finnois"),
    "el":     ("el-GR-AthinaNeural",     "Grec"),
    "tr":     ("tr-TR-EmelNeural",       "Turc"),
    "ar":     ("ar-SA-ZariyahNeural",    "Arabe"),
    "he":     ("he-IL-HilaNeural",       "Hébreu"),
    "hi":     ("hi-IN-SwaraNeural",      "Hindi"),
    "zh":     ("zh-CN-XiaoxiaoNeural",   "Chinois"),
    "ja":     ("ja-JP-NanamiNeural",     "Japonais"),
    "ko":     ("ko-KR-SunHiNeural",      "Coréen"),
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
        return [{"code": code, "voice": voice, "label": label}
                for code, (voice, label) in LANG_MAP.items()]

    def get_rate(self) -> int:
        return self._rate

    def set_rate(self, rate: int) -> int:
        self._rate = max(50, min(400, rate))
        return self._rate

    def speak(self, text: str, lang: str | None = None, actor: str | None = None):
        voice, _ = LANG_MAP.get((lang or DEFAULT_LANG).lower(), LANG_MAP[DEFAULT_LANG])
        rate_str = _rate_to_pct(self._rate)
        logger.info("TTS request: voice=%s rate=%s, %d chars%s", voice, rate_str, len(text),
                    f" from {actor}" if actor else "")
        with self._lock:
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                    tmp_path = f.name
                communicate = edge_tts.Communicate(text, voice, rate=rate_str)
                asyncio.run(communicate.save(tmp_path))
                logger.info("TTS synthesised %d bytes, playing it", os.path.getsize(tmp_path))
                self.player.play_file(tmp_path, actor)
            except Exception:
                logger.exception("TTS failed for text: %s", text)
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)
