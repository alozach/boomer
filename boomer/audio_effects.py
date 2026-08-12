import logging
import numpy as np
import pygame

logger = logging.getLogger(__name__)

MIN_SPEED = 0.25
MAX_SPEED = 4.0

# Shorthand flags expanding to a full effect set
_PRESETS = {
    "nightcore": {"speed": 1.35},
    "chipmunk": {"speed": 1.8},
    "vaporwave": {"speed": 0.75},
    "slow": {"speed": 0.7},
    "deep": {"speed": 0.6},
    "grave": {"speed": 0.6},
    "reverse": {"reverse": True},
    "inverse": {"reverse": True},
}


class EffectError(ValueError):
    pass


def parse_effects(text: str) -> tuple[str, dict]:
    """Split a command argument into its sound part and its effect flags.

    `maaaarc --reverse --speed 1.5` -> ("maaaarc", {"reverse": True, "speed": 1.5})
    Raises EffectError on an unknown flag or an out-of-range speed.
    """
    tokens = text.split()
    words: list[str] = []
    effects: dict = {}
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if not token.startswith("--"):
            words.append(token)
            i += 1
            continue

        flag = token[2:].lower()
        value = None
        if "=" in flag:
            flag, value = flag.split("=", 1)

        if flag in _PRESETS:
            effects.update(_PRESETS[flag])
        elif flag in ("speed", "vitesse"):
            if value is None:
                i += 1
                if i >= len(tokens):
                    raise EffectError("`--speed` attend une valeur, ex. `--speed 1.5`.")
                value = tokens[i]
            try:
                speed = float(value.replace(",", "."))
            except ValueError:
                raise EffectError(f"Vitesse invalide : `{value}`.")
            if not MIN_SPEED <= speed <= MAX_SPEED:
                raise EffectError(f"Vitesse hors limites : `{speed}` (attendu {MIN_SPEED}–{MAX_SPEED}).")
            effects["speed"] = speed
        else:
            raise EffectError(f"Effet inconnu : `--{flag}`.")
        i += 1

    return " ".join(words), effects


def describe(effects: dict) -> str:
    if not effects:
        return ""
    parts = []
    if effects.get("reverse"):
        parts.append("à l'envers")
    if "speed" in effects:
        parts.append(f"×{effects['speed']:g}")
    return " _(" + ", ".join(parts) + ")_"


def apply(sound: pygame.mixer.Sound, effects: dict) -> pygame.mixer.Sound:
    """Return a new Sound with the effects applied, or the original one on failure."""
    if not effects:
        return sound
    try:
        samples = pygame.sndarray.array(sound)
        if effects.get("reverse"):
            samples = samples[::-1]
        speed = effects.get("speed")
        if speed and speed != 1.0:
            samples = _resample(samples, speed)
        return pygame.sndarray.make_sound(np.ascontiguousarray(samples))
    except Exception:
        # Unsupported sample format: fall back to the untouched sound
        logger.exception("Cannot apply effects %s", effects)
        return sound


def _resample(samples: np.ndarray, speed: float) -> np.ndarray:
    """Linear resampling: playing fewer/more samples shifts both tempo and pitch."""
    source_len = samples.shape[0]
    target_len = max(1, int(source_len / speed))
    positions = np.linspace(0, source_len - 1, target_len)
    source_index = np.arange(source_len)

    if samples.ndim == 1:
        resampled = np.interp(positions, source_index, samples)
        return resampled.astype(samples.dtype)

    channels = [
        np.interp(positions, source_index, samples[:, c]) for c in range(samples.shape[1])
    ]
    return np.stack(channels, axis=1).astype(samples.dtype)
