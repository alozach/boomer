import logging
import os
import tempfile
import threading
import requests
from slack_bolt import App
from boomer.sound_player import SoundPlayer
from boomer.tts_engine import TtsEngine

logger = logging.getLogger(__name__)

# (channel_id, user_id) -> nom de son en attente d'un fichier
_pending_additions: dict[tuple[str, str], str] = {}


def create_slack_app(player: SoundPlayer, tts: TtsEngine) -> App:
    app = App(
        token=os.environ["SLACK_BOT_TOKEN"],
        signing_secret=os.environ["SLACK_SIGNING_SECRET"],
    )

    @app.command("/boomer")
    def handle_boomer(ack, command, say):
        ack()
        text = command.get("text", "").strip()
        parts = text.split(maxsplit=1)
        action = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""

        if action == "play":
            _cmd_play(say, player, arg)
        elif action == "add":
            _cmd_add(say, player, command, arg)
        elif action == "list":
            _cmd_list(say, player)
        elif action == "tts":
            _cmd_tts(say, tts, arg)
        elif action == "mute":
            player.mute()
            say(":mute: Son coupé.")
        elif action == "unmute":
            vol = player.unmute()
            say(f":loud_sound: Son rétabli à {int(vol * 100)} %.")
        elif action in ("volume", "vol"):
            _cmd_volume(say, player, arg)
        else:
            say(_usage())

    @app.event("message")
    def handle_message(event, client, say):
        channel = event.get("channel")
        user = event.get("user")
        files = event.get("files")
        if not files or not user:
            return

        key = (channel, user)
        if key not in _pending_additions:
            return

        name = _pending_additions.pop(key)
        file_info = files[0]
        _download_and_save(say, player, name, file_info, overwrite=False)

    return app


def _cmd_play(say, player: SoundPlayer, name: str):
    if not name:
        say("Usage : `/boomer play <nom>`")
        return
    if player.play(name):
        say(f":arrow_forward: Lecture de `{name}`.")
    else:
        say(f":x: Son `{name}` introuvable. Utilise `/boomer list` pour voir les sons disponibles.")


def _cmd_add(say, player: SoundPlayer, command: dict, name: str):
    if not name:
        say("Usage : `/boomer add <nom>`")
        return
    if player.sound_exists(name):
        # on stocke le nom avec un flag overwrite
        key = (command["channel_id"], command["user_id"])
        _pending_additions[key] = f"__overwrite__{name}"
        say(
            f":warning: Un son nommé `{name}` existe déjà. "
            f"Envoie le nouveau fichier dans ce canal pour le remplacer, "
            f"ou ignore ce message pour annuler."
        )
    else:
        key = (command["channel_id"], command["user_id"])
        _pending_additions[key] = name
        say(f":inbox_tray: Prêt à ajouter `{name}`. Envoie maintenant le fichier audio dans ce canal.")


def _cmd_list(say, player: SoundPlayer):
    sounds = player.list_sounds()
    if not sounds:
        say(":speaker: Aucun son disponible pour le moment.")
        return
    lines = "\n".join(f"• `{s}`" for s in sounds)
    say(f":musical_note: Sons disponibles :\n{lines}")


def _cmd_tts(say, tts: TtsEngine, text: str):
    if not text:
        say("Usage : `/boomer tts <texte>`")
        return
    say(f":speaking_head_in_silhouette: *{text}*")
    threading.Thread(target=tts.speak, args=(text,), daemon=True).start()


def _cmd_volume(say, player: SoundPlayer, arg: str):
    if arg in ("up", "haut", "+"):
        vol = player.volume_up()
        say(f":loud_sound: Volume : {int(vol * 100)} %")
    elif arg in ("down", "bas", "-"):
        vol = player.volume_down()
        say(f":sound: Volume : {int(vol * 100)} %")
    elif arg.rstrip("%").isdigit():
        level = int(arg.rstrip("%")) / 100
        player.set_volume(level)
        say(f":loud_sound: Volume réglé à {int(level * 100)} %.")
    else:
        say("Usage : `/boomer volume up|down|<0-100>`")


def _download_and_save(say, player: SoundPlayer, name: str, file_info: dict, overwrite: bool):
    if name.startswith("__overwrite__"):
        name = name[len("__overwrite__"):]
        overwrite = True

    url = file_info.get("url_private_download") or file_info.get("url_private")
    if not url:
        say(":x: Impossible de récupérer l'URL du fichier.")
        return

    filetype = file_info.get("filetype", "").lower()
    ext = f".{filetype}" if filetype else os.path.splitext(file_info.get("name", ""))[1]

    token = os.environ["SLACK_BOT_TOKEN"]
    try:
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        say(f":x: Échec du téléchargement : {e}")
        return

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
        f.write(resp.content)
        tmp_path = f.name

    try:
        added = player.add_sound(name, tmp_path, overwrite=overwrite)
    finally:
        os.unlink(tmp_path)

    if added:
        say(f":white_check_mark: Son `{name}` ajouté avec succès.")
    else:
        say(f":x: Un son `{name}` existe déjà et l'écrasement n'a pas été confirmé.")


def _usage() -> str:
    return (
        "*Commandes disponibles :*\n"
        "• `/boomer play <nom>` — jouer un son\n"
        "• `/boomer add <nom>` — ajouter un son (puis envoyer le fichier)\n"
        "• `/boomer list` — lister les sons\n"
        "• `/boomer tts <texte>` — synthèse vocale\n"
        "• `/boomer mute` — couper le son\n"
        "• `/boomer unmute` — rétablir le son\n"
        "• `/boomer volume up|down|<0-100>` — régler le volume"
    )
