import logging
import mido
from boomer.sound_player import SoundPlayer

logger = logging.getLogger(__name__)


class MidiListener:
    def __init__(self, player: SoundPlayer):
        self.player = player
        self._running = False

    def start(self):
        self._running = True
        available = mido.get_input_names()
        if not available:
            logger.warning("Aucun périphérique MIDI détecté. L'écoute MIDI est désactivée.")
            return

        port_name = available[0]
        logger.info("Ouverture du port MIDI : %s", port_name)

        with mido.open_input(port_name) as port:
            for msg in port:
                if not self._running:
                    break
                if msg.type == "note_on" and msg.velocity > 0:
                    self._handle_note(msg.note)

    def stop(self):
        self._running = False

    def _handle_note(self, note: int):
        mappings = self.player.get_midi_mapping()
        name = mappings.get(note)
        if name is None:
            return
        played = self.player.play(name)
        if not played:
            logger.warning("Son '%s' mappé sur la note %d introuvable.", name, note)
