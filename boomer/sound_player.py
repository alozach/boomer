import array as arr
import difflib
import json
import logging
import math
import os
import random
import shutil
import threading
import time
import pygame

from boomer import audio_effects

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".wav", ".mp3", ".ogg", ".flac", ".aiff"}
MAX_VOLUME = 1.0
MIDI_ACTIONS = {"volume+", "volume-"}


class SoundPlayer:
    def __init__(self, sounds_dir: str, config_path: str):
        self.sounds_dir = sounds_dir
        self.config_path = config_path
        self._lock = threading.Lock()

        pygame.mixer.init()
        os.makedirs(sounds_dir, exist_ok=True)
        self._config = self._load_config()
        self._current_volume: float = 0.02
        self._muted: bool = False
        self._volume_before_mute: float = 0.02
        # Bumped on every new playback so a running sequence knows it has been superseded
        self._playback_token: int = 0

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

    def _start_playback(self) -> int:
        """Cancel whatever is playing and claim a new playback token. Caller holds the lock."""
        self._playback_token += 1
        pygame.mixer.stop()
        return self._playback_token

    def _play_path(self, path: str, effects: dict | None):
        """Load, transform and start a file. Returns (channel, sound) or None. Caller holds the lock."""
        try:
            sound = pygame.mixer.Sound(path)
        except pygame.error:
            # e.g. extension lying about the real container/codec
            logger.exception("Cannot decode sound file: %s", path)
            return None
        if effects:
            sound = audio_effects.apply(sound, effects)
        sound.set_volume(self._current_volume)
        channel = sound.play()
        return channel, sound

    def play(self, name: str, effects: dict | None = None) -> bool:
        path = self._find_sound_file(name)
        if path is None:
            return False
        with self._lock:
            self._start_playback()
            return self._play_path(path, effects) is not None

    def play_sequence(self, names: list[str], effects: dict | None = None) -> bool:
        """Play sounds back to back in a background thread. Any new playback interrupts it."""
        paths = [p for p in (self._find_sound_file(n) for n in names) if p is not None]
        if not paths:
            return False
        with self._lock:
            token = self._start_playback()
        thread = threading.Thread(
            target=self._run_sequence, args=(paths, effects, token), daemon=True, name="sequence"
        )
        thread.start()
        return True

    def _run_sequence(self, paths: list[str], effects: dict | None, token: int):
        for path in paths:
            with self._lock:
                if token != self._playback_token:
                    return
                started = self._play_path(path, effects)
            if started is None:
                continue
            channel, sound = started
            deadline = time.monotonic() + sound.get_length()
            while time.monotonic() < deadline and channel.get_busy():
                if token != self._playback_token:
                    return
                time.sleep(0.02)

    def play_file(self, path: str) -> bool:
        with self._lock:
            self._start_playback()
            if self._play_path(path, None) is None:
                return False
            while pygame.mixer.get_busy():
                pygame.time.wait(50)
        return True

    def random_sound(self) -> str | None:
        sounds = self.list_sounds()
        return random.choice(sounds) if sounds else None

    def list_sounds(self) -> list[str]:
        names = []
        for filename in sorted(os.listdir(self.sounds_dir)):
            name, ext = os.path.splitext(filename)
            if ext.lower() in SUPPORTED_EXTENSIONS:
                names.append(name)
        return names

    def sound_exists(self, name: str) -> bool:
        return self._find_sound_file(name) is not None

    def find_closest_sound(self, name: str, cutoff: float = 0.6) -> str | None:
        candidates = self.list_sounds()
        matches = difflib.get_close_matches(name.lower(), [s.lower() for s in candidates], n=1, cutoff=cutoff)
        if not matches:
            return None
        return candidates[[s.lower() for s in candidates].index(matches[0])]

    def add_sound(self, name: str, source_path: str, overwrite: bool = False) -> bool:
        """Copy an audio file into the sounds directory. Returns False if the sound already exists and overwrite=False."""
        if self.sound_exists(name) and not overwrite:
            return False
        if self.sound_exists(name) and overwrite:
            existing = self._find_sound_file(name)
            os.remove(existing)

        ext = os.path.splitext(source_path)[1].lower()
        dest = os.path.join(self.sounds_dir, name + ext)
        shutil.copy2(source_path, dest)
        return True

    def delete_sound(self, name: str) -> bool:
        path = self._find_sound_file(name)
        if path is None:
            return False
        os.remove(path)
        mappings = self._config.get("midi_mappings", {})
        for note in [k for k, v in mappings.items() if v == name]:
            del mappings[note]
        self._save_config()
        return True

    def get_midi_mapping(self) -> dict[int, str]:
        return {int(k): v for k, v in self._config.get("midi_mappings", {}).items()}

    def set_midi_mapping(self, note: int, name: str):
        self._config.setdefault("midi_mappings", {})[str(note)] = name
        self._save_config()

    def rename_sound(self, old_name: str, new_name: str) -> tuple[bool, str]:
        old_path = self._find_sound_file(old_name)
        if old_path is None:
            return False, f"Son `{old_name}` introuvable."
        if self.sound_exists(new_name):
            return False, f"Un son nommé `{new_name}` existe déjà."
        ext = os.path.splitext(old_path)[1]
        os.rename(old_path, os.path.join(self.sounds_dir, new_name + ext))
        mappings = self._config.get("midi_mappings", {})
        for note in list(mappings.keys()):
            if mappings[note] == old_name:
                mappings[note] = new_name
        self._save_config()
        return True, ""

    def is_muted(self) -> bool:
        return self._muted

    def get_panel_info(self, key: str = "panel") -> dict | None:
        return self._config.get(key)

    def set_panel_info(self, channel: str, ts: str, key: str = "panel"):
        self._config[key] = {"channel": channel, "ts": ts}
        self._save_config()

    def clear_panel_info(self, key: str = "panel"):
        self._config.pop(key, None)
        self._save_config()

    def get_volume(self) -> float:
        return self._current_volume

    def set_volume(self, level: float):
        # pygame.mixer.Sound.set_volume is per-instance; store level to apply on future playback calls
        self._current_volume = max(0.0, min(MAX_VOLUME, level))

    def volume_up(self, step: float = 0.02) -> float:
        new_vol = min(MAX_VOLUME, self._current_volume + step)
        self.set_volume(new_vol)
        return new_vol

    def volume_down(self, step: float = 0.02) -> float:
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

    def beep(self, frequency: int = 800, duration: float = 0.08):
        sample_rate, _, channels = pygame.mixer.get_init()
        n = int(sample_rate * duration)
        buf = arr.array("h", [0] * (n * channels))
        amplitude = 3000
        for i in range(n):
            val = int(amplitude * math.sin(2 * math.pi * frequency * i / sample_rate))
            for c in range(channels):
                buf[i * channels + c] = val
        sound = pygame.mixer.Sound(buffer=buf)
        sound.set_volume(self._current_volume)
        sound.play()

    def stop(self):
        with self._lock:
            self._start_playback()
