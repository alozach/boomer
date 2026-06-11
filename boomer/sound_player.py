import json
import os
import shutil
import threading
import pygame


SUPPORTED_EXTENSIONS = {".wav", ".mp3", ".ogg", ".flac", ".aiff"}


class SoundPlayer:
    def __init__(self, sounds_dir: str, config_path: str):
        self.sounds_dir = sounds_dir
        self.config_path = config_path
        self._lock = threading.Lock()

        pygame.mixer.init()
        os.makedirs(sounds_dir, exist_ok=True)
        self._config = self._load_config()
        self._current_volume: float = 1.0
        self._muted: bool = False
        self._volume_before_mute: float = 1.0

    def _load_config(self) -> dict:
        if not os.path.exists(self.config_path):
            return {"midi_mappings": {}}
        with open(self.config_path) as f:
            return json.load(f)

    def _save_config(self):
        with open(self.config_path, "w") as f:
            json.dump(self._config, f, indent=2)

    def _find_sound_file(self, name: str) -> str | None:
        for ext in SUPPORTED_EXTENSIONS:
            path = os.path.join(self.sounds_dir, name + ext)
            if os.path.exists(path):
                return path
        return None

    def play(self, name: str) -> bool:
        path = self._find_sound_file(name)
        if path is None:
            return False
        with self._lock:
            sound = pygame.mixer.Sound(path)
            sound.set_volume(self._current_volume)
            sound.play()
        return True

    def play_file(self, path: str):
        with self._lock:
            sound = pygame.mixer.Sound(path)
            sound.play()
            # attendre la fin de lecture pour les fichiers temporaires (TTS)
            while pygame.mixer.get_busy():
                pygame.time.wait(50)

    def list_sounds(self) -> list[str]:
        names = []
        for filename in sorted(os.listdir(self.sounds_dir)):
            name, ext = os.path.splitext(filename)
            if ext.lower() in SUPPORTED_EXTENSIONS:
                names.append(name)
        return names

    def sound_exists(self, name: str) -> bool:
        return self._find_sound_file(name) is not None

    def add_sound(self, name: str, source_path: str, overwrite: bool = False) -> bool:
        """Copie un fichier audio dans le répertoire sounds. Retourne False si le son existe déjà et overwrite=False."""
        if self.sound_exists(name) and not overwrite:
            return False
        if self.sound_exists(name) and overwrite:
            existing = self._find_sound_file(name)
            os.remove(existing)

        ext = os.path.splitext(source_path)[1].lower()
        dest = os.path.join(self.sounds_dir, name + ext)
        shutil.copy2(source_path, dest)
        return True

    def get_midi_mapping(self) -> dict[int, str]:
        return {int(k): v for k, v in self._config.get("midi_mappings", {}).items()}

    def set_midi_mapping(self, note: int, name: str):
        self._config.setdefault("midi_mappings", {})[str(note)] = name
        self._save_config()

    def get_volume(self) -> float:
        return pygame.mixer.music.get_volume() if not self._muted else self._volume_before_mute

    def set_volume(self, level: float):
        level = max(0.0, min(1.0, level))
        self._volume = level
        if not self._muted:
            pygame.mixer.music.set_volume(level)
        # pygame.mixer.Sound.set_volume doit être appliqué par canal ; on stocke pour les prochains sons
        self._current_volume = level

    def volume_up(self, step: float = 0.1) -> float:
        new_vol = min(1.0, self._current_volume + step)
        self.set_volume(new_vol)
        return new_vol

    def volume_down(self, step: float = 0.1) -> float:
        new_vol = max(0.0, self._current_volume - step)
        self.set_volume(new_vol)
        return new_vol

    def mute(self):
        self._volume_before_mute = self._current_volume
        self._muted = True
        self._current_volume = 0.0

    def unmute(self) -> float:
        self._muted = False
        self._current_volume = self._volume_before_mute
        return self._current_volume
