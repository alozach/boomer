import logging
import os
import signal
import threading
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from boomer.sound_player import SoundPlayer
from boomer.midi_listener import MidiListener
from boomer.tts_engine import TtsEngine
from boomer.slack_bot import create_slack_app


def main():
    player = SoundPlayer("sounds", "config.json")
    tts = TtsEngine(player)
    midi = MidiListener(player)

    midi_thread = threading.Thread(target=midi.start, daemon=True, name="midi-listener")
    midi_thread.start()

    app = create_slack_app(player, tts)

    def shutdown(sig, frame):
        logging.info("Arrêt en cours...")
        midi.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    app_token = os.getenv("SLACK_APP_TOKEN")
    if app_token:
        from slack_bolt.adapter.socket_mode import SocketModeHandler
        handler = SocketModeHandler(app, app_token)
        logging.info("Démarrage en mode Socket.")
        handler.start()
    else:
        port = int(os.getenv("PORT", 3000))
        logging.info("Démarrage en mode HTTP sur le port %d.", port)
        app.start(port=port)


if __name__ == "__main__":
    main()
