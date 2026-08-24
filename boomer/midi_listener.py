import logging
import time
from collections.abc import Callable
import mido
from boomer.sound_player import SoundPlayer, MIDI_ACTIONS
from boomer.stats import ACTOR_MIDI

logger = logging.getLogger(__name__)


class MidiListener:
    def __init__(self, player: SoundPlayer):
        self.player = player
        self._running = False
        # When set, receives every note_on and returns True to intercept it (skip normal playback)
        self._note_interceptor: Callable[[int], bool] | None = None
        self._volume_action_callback: Callable[[str, float], None] | None = None
        self._play_callback: Callable[[str], None] | None = None

    def set_note_interceptor(self, callback: Callable[[int], bool]):
        self._note_interceptor = callback

    def clear_note_interceptor(self):
        self._note_interceptor = None

    def set_play_callback(self, callback: Callable[[str], None]):
        self._play_callback = callback

    def set_volume_action_callback(self, callback: Callable[[str, float], None]):
        self._volume_action_callback = callback

    def has_interceptor(self) -> bool:
        return self._note_interceptor is not None

    def start(self):
        self._running = True
        available = mido.get_input_names()
        if not available:
            logger.warning("No MIDI device detected. MIDI listening is disabled.")
            return

        hardware = [p for p in available if "Midi Through" not in p]
        port_name = (hardware or available)[0]
        logger.info("MIDI ports available: %s", available)
        logger.info("Opening MIDI port '%s' with %d mapping(s)",
                    port_name, len(self.player.get_midi_mapping()))

        try:
            with mido.open_input(port_name) as port:
                while self._running:
                    msg = port.receive(block=False)
                    if msg is None:
                        time.sleep(0.005)
                        continue
                    logger.debug("MIDI message: %s", msg)
                    if msg.type == "note_on" and msg.velocity > 0:
                        self._handle_note(msg.note)
        except Exception:
            # Without this the thread dies quietly and MIDI stops for good
            logger.exception("MIDI listener crashed on port '%s'", port_name)
            raise
        logger.info("MIDI listener stopped.")

    def stop(self):
        self._running = False

    def _handle_note(self, note: int):
        if self._note_interceptor is not None:
            if self._note_interceptor(note):
                logger.info("Note %d intercepted by an ongoing assignment", note)
                return
        mappings = self.player.get_midi_mapping()
        name = mappings.get(note)
        if name is None:
            logger.info("Note %d pressed but mapped to nothing", note)
            return
        logger.info("Note %d -> '%s'", note, name)
        if name in MIDI_ACTIONS:
            if name == "volume+":
                vol = self.player.volume_up(actor=ACTOR_MIDI)
                self.player.beep(frequency=900, duration=0.07)
            elif name == "volume-":
                vol = self.player.volume_down(actor=ACTOR_MIDI)
                self.player.beep(frequency=500, duration=0.07)
            else:
                return
            if self._volume_action_callback:
                self._volume_action_callback(name, vol)
            return
        played = self.player.play(name, actor=ACTOR_MIDI)
        if played:
            if self._play_callback:
                self._play_callback(name)
        else:
            logger.warning("Sound '%s' mapped to note %d not found.", name, note)
