import array as arr
import contextlib
import difflib
import glob
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

# Above this, waiting for the playback lock means someone else is holding it too long
_LOCK_WAIT_WARN = 0.5


def can_decode(path: str) -> bool:
    """The final say on a file: whatever its name, can the mixer actually load it?"""
    try:
        pygame.mixer.Sound(path)
        return True
    except pygame.error:
        return False


def sniff_extension(data: bytes) -> str | None:
    """Identify an audio container from its header, whatever the file claims to be.

    Slack reports filetype 'binary' for anything it does not recognise, and uploads
    are routinely misnamed, so the bytes are the only trustworthy source.
    """
    if data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        return ".mp3"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return ".wav"
    if data[:4] == b"OggS":
        return ".ogg"
    if data[:4] == b"fLaC":
        return ".flac"
    if data[:4] == b"FORM" and data[8:12] in (b"AIFF", b"AIFC"):
        return ".aiff"
    return None


def _alsa_cards() -> list[str]:
    """Cards as the kernel enumerated them. USB cards can change index across reboots."""
    try:
        with open("/proc/asound/cards") as f:
            content = f.read()
    except OSError:
        return []
    # Card lines start with an index; the description follows on the next line
    return [line.strip() for line in content.splitlines() if line[:2].strip().isdigit()]


def _alsa_playback_states() -> list[str]:
    """State of every ALSA playback substream: 'closed' or 'state: RUNNING'."""
    states = []
    for status_path in sorted(glob.glob("/proc/asound/card*/pcm*p/sub*/status")):
        try:
            with open(status_path) as f:
                first_line = f.readline().strip()
        except OSError:
            continue
        states.append(f"{status_path.split('/')[3]}: {first_line}")
    return states


def _warn_if_inaudible(label: str, delay: float = 0.25):
    """A playback that no ALSA stream backs is the silent failure: report it.

    The stream starts asynchronously, hence the short delay before looking.
    """
    def check():
        time.sleep(delay)
        states = _alsa_playback_states()
        if states and not any("RUNNING" in s for s in states):
            logger.warning(
                "Started %s but no ALSA stream is running: nothing is audible (%s)",
                label, "; ".join(states),
            )

    threading.Thread(target=check, daemon=True, name="audible-check").start()


def _sdl_audio_driver() -> str:
    """The driver SDL really picked. 'dummy' means playback goes nowhere, silently."""
    try:
        import ctypes
        sdl = ctypes.CDLL("libSDL2-2.0.so.0")
        sdl.SDL_GetCurrentAudioDriver.restype = ctypes.c_char_p
        raw = sdl.SDL_GetCurrentAudioDriver()
        if raw:
            return raw.decode()
    except Exception:
        logger.debug("Cannot ask SDL which audio driver it picked", exc_info=True)
    return f"unknown (SDL_AUDIODRIVER={os.environ.get('SDL_AUDIODRIVER', 'unset')})"


class SoundPlayer:
    def __init__(self, sounds_dir: str, config_path: str):
        self.sounds_dir = sounds_dir
        self.config_path = config_path
        self._lock = threading.Lock()

        try:
            pygame.mixer.init()
        except pygame.error:
            logger.exception("Cannot initialise the audio mixer: nothing will be audible")
            raise
        frequency, size, channels = pygame.mixer.get_init()
        logger.info(
            "Audio mixer ready: %d Hz, format %d, %d channel(s), %d mixing slots, SDL driver=%s",
            frequency, size, channels, pygame.mixer.get_num_channels(), _sdl_audio_driver(),
        )
        # Which card SDL landed on decides whether anything is audible at all
        for card in _alsa_cards() or ["(none — the kernel reports no ALSA card)"]:
            logger.info("ALSA card: %s", card)
        logger.info("Audio environment: AUDIODEV=%s, XDG_RUNTIME_DIR=%s",
                    os.environ.get("AUDIODEV", "unset"),
                    os.environ.get("XDG_RUNTIME_DIR", "unset"))

        os.makedirs(sounds_dir, exist_ok=True)
        self._config = self._load_config()
        self._current_volume: float = 0.02
        self._muted: bool = False
        self._volume_before_mute: float = 0.02
        # Bumped on every new playback so a running sequence knows it has been superseded
        self._playback_token: int = 0
        logger.info(
            "%d sound(s) in %s, %d MIDI mapping(s), starting volume %d %%",
            len(self.list_sounds()), sounds_dir, len(self.get_midi_mapping()),
            int(self._current_volume * 100),
        )

    @contextlib.contextmanager
    def _playback_lock(self, what: str):
        """Take the playback lock, reporting the wait when another playback still holds it."""
        started = time.monotonic()
        self._lock.acquire()
        waited = time.monotonic() - started
        if waited > _LOCK_WAIT_WARN:
            logger.warning("Waited %.1f s for the playback lock before %s", waited, what)
        try:
            yield
        finally:
            self._lock.release()

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
        if channel is None:
            # Every mixing slot is busy; pygame stays silent about it
            logger.error("No free mixer channel for %s: nothing will be audible", path)
        else:
            logger.info(
                "Playing %s (%.1f s) at %d %%%s",
                os.path.basename(path), sound.get_length(), int(self._current_volume * 100),
                f" with effects {effects}" if effects else "",
            )
            _warn_if_inaudible(os.path.basename(path))
        if self._muted:
            logger.warning("Sound is muted: %s will not be audible", os.path.basename(path))
        return channel, sound

    def play(self, name: str, effects: dict | None = None) -> bool:
        path = self._find_sound_file(name)
        if path is None:
            logger.warning("Sound '%s' has no file in %s", name, self.sounds_dir)
            return False
        with self._playback_lock(f"playing '{name}'"):
            self._start_playback()
            return self._play_path(path, effects) is not None

    def play_sequence(self, names: list[str], effects: dict | None = None) -> bool:
        """Play sounds back to back in a background thread. Any new playback interrupts it."""
        paths = [p for p in (self._find_sound_file(n) for n in names) if p is not None]
        if not paths:
            logger.warning("None of these sounds has a file: %s", names)
            return False
        if len(paths) < len(names):
            logger.warning("Only %d of the %d requested sounds were found", len(paths), len(names))
        with self._playback_lock(f"playing a sequence of {len(paths)} sounds"):
            token = self._start_playback()
        thread = threading.Thread(
            target=self._run_sequence, args=(paths, effects, token), daemon=True, name="sequence"
        )
        thread.start()
        return True

    def _run_sequence(self, paths: list[str], effects: dict | None, token: int):
        for path in paths:
            with self._playback_lock("the next sound of the sequence"):
                if token != self._playback_token:
                    logger.info("Sequence interrupted by a newer playback")
                    return
                started = self._play_path(path, effects)
            if started is None:
                continue
            channel, sound = started
            deadline = time.monotonic() + sound.get_length()
            while time.monotonic() < deadline and (channel is None or channel.get_busy()):
                if token != self._playback_token:
                    logger.info("Sequence interrupted by a newer playback")
                    return
                time.sleep(0.02)

    def play_file(self, path: str) -> bool:
        """Play a one-off file (TTS) and wait for the end, holding the lock throughout."""
        with self._playback_lock(f"playing the file {os.path.basename(path)}"):
            self._start_playback()
            started = self._play_path(path, None)
            if started is None:
                return False
            _, sound = started
            # Bounded wait: a stuck channel must not keep the lock forever
            deadline = time.monotonic() + sound.get_length() + 1.0
            while pygame.mixer.get_busy() and time.monotonic() < deadline:
                pygame.time.wait(50)
            if pygame.mixer.get_busy():
                logger.warning("Mixer still busy %.1f s after starting %s, giving up the wait",
                               sound.get_length() + 1.0, os.path.basename(path))
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
        logger.info("Sound '%s' added as %s (%d bytes)", name, dest, os.path.getsize(dest))
        return True

    def delete_sound(self, name: str) -> bool:
        path = self._find_sound_file(name)
        if path is None:
            logger.warning("Cannot delete '%s': no such sound", name)
            return False
        os.remove(path)
        mappings = self._config.get("midi_mappings", {})
        dropped = [k for k, v in mappings.items() if v == name]
        for note in dropped:
            del mappings[note]
        self._save_config()
        logger.info("Sound '%s' deleted (%d MIDI mapping(s) dropped)", name, len(dropped))
        return True

    def get_midi_mapping(self) -> dict[int, str]:
        return {int(k): v for k, v in self._config.get("midi_mappings", {}).items()}

    def set_midi_mapping(self, note: int, name: str):
        self._config.setdefault("midi_mappings", {})[str(note)] = name
        self._save_config()
        logger.info("MIDI note %d mapped to '%s'", note, name)

    def rename_sound(self, old_name: str, new_name: str) -> tuple[bool, str]:
        old_path = self._find_sound_file(old_name)
        if old_path is None:
            return False, f"Son `{old_name}` introuvable."
        if self.sound_exists(new_name):
            return False, f"Un son nommé `{new_name}` existe déjà."
        ext = os.path.splitext(old_path)[1]
        os.rename(old_path, os.path.join(self.sounds_dir, new_name + ext))
        logger.info("Sound '%s' renamed to '%s'", old_name, new_name)
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
        logger.info("%s now tracked in channel %s (ts=%s)", key, channel, ts)

    def clear_panel_info(self, key: str = "panel"):
        self._config.pop(key, None)
        self._save_config()
        logger.info("%s forgotten", key)

    def get_volume(self) -> float:
        return self._current_volume

    def set_volume(self, level: float):
        # pygame.mixer.Sound.set_volume is per-instance; store level to apply on future playback calls
        previous = self._current_volume
        self._current_volume = max(0.0, min(MAX_VOLUME, level))
        logger.info("Volume %d %% -> %d %% (requested %d %%, applies to the next playback)",
                    int(previous * 100), int(self._current_volume * 100), int(level * 100))

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
        logger.info("Muted (volume was %d %%)", int(self._volume_before_mute * 100))

    def unmute(self) -> float:
        self._muted = False
        self._current_volume = self._volume_before_mute
        logger.info("Unmuted at %d %%", int(self._current_volume * 100))
        return self._current_volume

    def beep(self, frequency: int = 800, duration: float = 0.08):
        try:
            self._beep(frequency, duration)
        except Exception:
            # A failed feedback beep must not take the caller (MIDI thread) down
            logger.exception("Cannot play the feedback beep")

    def _beep(self, frequency: int, duration: float):
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
        with self._playback_lock("stopping the playback"):
            logger.info("Playback stopped")
            self._start_playback()
