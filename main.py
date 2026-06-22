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
from boomer.scheduler import Scheduler


def main():
    player = SoundPlayer("sounds", "config.json")
    tts = TtsEngine(player)
    midi = MidiListener(player)
    scheduler = Scheduler(player)

    midi_thread = threading.Thread(target=midi.start, daemon=True, name="midi-listener")
    midi_thread.start()
    scheduler.start()

    app = create_slack_app(player, tts, midi, scheduler)

    app_token = os.getenv("SLACK_APP_TOKEN")
    if app_token:
        from slack_bolt.adapter.socket_mode import SocketModeHandler
        handler = SocketModeHandler(app, app_token)

        def shutdown(sig, frame):
            logging.info("Shutting down...")
            midi.stop()
            scheduler.stop()
            handler.close()
            os._exit(0)

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        logging.info("Starting in Socket Mode.")
        handler.start()
    else:
        def shutdown(sig, frame):
            logging.info("Shutting down...")
            midi.stop()

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        port = int(os.getenv("PORT", 3000))
        logging.info("Starting in HTTP mode on port %d.", port)
        app.start(port=port)


if __name__ == "__main__":
    main()
