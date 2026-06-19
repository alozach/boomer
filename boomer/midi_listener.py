import logging
from collections.abc import Callable
import mido
from boomer.sound_player import SoundPlayer, MIDI_ACTIONS

logger = logging.getLogger(__name__)


class MidiListener:
    def __init__(self, player: SoundPlayer):
        self.player = player
        self._running = False
        # When set, receives every note_on and returns True to intercept it (skip normal playback)
        self._note_interceptor: Callable[[int], bool] | None = None

    def set_note_interceptor(self, callback: Callable[[int], bool]):
        self._note_interceptor = callback

    def clear_note_interceptor(self):
        self._note_interceptor = None

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
        logger.info("Opening MIDI port: %s", port_name)

        with mido.open_input(port_name) as port:
            for msg in port:
                if not self._running:
                    break
                if msg.type == "note_on" and msg.velocity > 0:
                    self._handle_note(msg.note)

    def stop(self):
        self._running = False

    def _handle_note(self, note: int):
        if self._note_interceptor is not None:
            if self._note_interceptor(note):
                return
        mappings = self.player.get_midi_mapping()
        name = mappings.get(note)
        if name is None:
            return
        if name in MIDI_ACTIONS:
            if name == "volume+":
                self.player.volume_up()
            elif name == "volume-":
                self.player.volume_down()
            return
        played = self.player.play(name)
        if not played:
            logger.warning("Sound '%s' mapped to note %d not found.", name, note)
