import logging
import os
import signal
import threading
from dotenv import load_dotenv

load_dotenv()

_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=_LOG_LEVEL,
    # The thread name tells a MIDI press apart from a Slack click or a schedule
    format="%(asctime)s [%(levelname)s] %(threadName)s %(name)s: %(message)s",
)
# DEBUG is meant for our own modules; the Slack and HTTP stacks stay at INFO
if _LOG_LEVEL == "DEBUG" and os.getenv("LOG_LEVEL_LIBS") is None:
    for noisy in ("slack_bolt", "slack_sdk", "urllib3", "websocket", "asyncio", "edge_tts"):
        logging.getLogger(noisy).setLevel(logging.INFO)

from boomer.sound_player import SoundPlayer
from boomer.midi_listener import MidiListener
from boomer.tts_engine import TtsEngine
from boomer.slack_bot import create_slack_app
from boomer.scheduler import Scheduler
from boomer.stats import Stats


def main():
    player = SoundPlayer("sounds", "config.json")
    tts = TtsEngine(player)
    midi = MidiListener(player)
    scheduler = Scheduler(player)
    stats = Stats("stats.json")

    logging.info("Boomer starting (log level %s)", _LOG_LEVEL)
    midi_thread = threading.Thread(target=midi.start, daemon=True, name="midi-listener")
    midi_thread.start()
    scheduler.start()

    app = create_slack_app(player, tts, midi, scheduler, stats)

    app_token = os.getenv("SLACK_APP_TOKEN")
    if app_token:
        from slack_bolt.adapter.socket_mode import SocketModeHandler
        handler = SocketModeHandler(app, app_token)

        def shutdown(sig, frame):
            logging.info("Shutting down...")
            midi.stop()
            scheduler.stop()
            stats.flush()
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
            stats.flush()

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        port = int(os.getenv("PORT", 3000))
        logging.info("Starting in HTTP mode on port %d.", port)
        app.start(port=port)


if __name__ == "__main__":
    main()
