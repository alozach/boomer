import logging
import os
import tempfile
import threading
import requests
from slack_bolt import App
from slack_sdk import WebClient
from boomer.sound_player import SoundPlayer, MIDI_ACTIONS
from boomer.tts_engine import TtsEngine
from boomer.midi_listener import MidiListener

logger = logging.getLogger(__name__)

_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

def _note_name(note: int) -> str:
    return f"{_NOTE_NAMES[note % 12]}{(note // 12) - 1}"

# (channel_id, user_id) -> sound name waiting for a file upload
_pending_additions: dict[tuple[str, str], str] = {}

# (channel_id, user_id) -> ongoing MIDI assignment state
_pending_maps: dict[tuple[str, str], dict] = {}
_pending_maps_lock = threading.Lock()


def create_slack_app(player: SoundPlayer, tts: TtsEngine, midi: MidiListener) -> App:
    app = App(
        token=os.environ["SLACK_BOT_TOKEN"],
        signing_secret=os.environ["SLACK_SIGNING_SECRET"],
    )
    # Reusable WebClient for async callbacks outside the Slack request context
    slack_client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])

    @app.command("/boomer_v3")
    def handle_boomer(ack, command, say):
        ack()
        text = command.get("text", "").strip()
        parts = text.split(maxsplit=1)
        action = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""

        if action in ("help", "aide", ""):
            say(_usage())
        elif action == "play":
            _cmd_play(say, player, arg)
        elif action == "add":
            _cmd_add(say, player, command, arg)
        elif action == "rename":
            _cmd_rename(say, player, arg)
        elif action == "list":
            _cmd_list(say, player)
        elif action == "map":
            _cmd_map(say, slack_client, player, midi, command["channel_id"], command["user_id"], arg)
        elif action == "tts":
            _cmd_tts(say, tts, arg)
        elif action == "voice":
            _cmd_voice(say, tts, arg)
        elif action == "panel":
            _cmd_panel(say, player)
        elif action == "stop":
            player.stop()
            say(":black_square_for_stop: Lecture arrêtée.")
        elif action == "mute":
            player.mute()
            say(":mute: Son coupé.")
        elif action == "unmute":
            vol = player.unmute()
            say(f":loud_sound: Son rétabli à {int(vol * 100)} %.")
        elif action in ("volume", "vol"):
            _cmd_volume(say, player, arg)
        else:
            say(f":x: Commande inconnue : `{action}`\n\n{_usage()}")

    @app.action("boomer_stop")
    def handle_action_stop(ack, body, client):
        ack()
        player.stop()
        _refresh_panel(body, client, player)

    @app.action("boomer_mute_toggle")
    def handle_action_mute_toggle(ack, body, client):
        ack()
        if player.is_muted():
            player.unmute()
        else:
            player.mute()
        _refresh_panel(body, client, player)

    @app.action("boomer_vol_down")
    def handle_action_vol_down(ack, body, client):
        ack()
        player.volume_down()
        _refresh_panel(body, client, player)

    @app.action("boomer_vol_up")
    def handle_action_vol_up(ack, body, client):
        ack()
        player.volume_up()
        _refresh_panel(body, client, player)

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
        say("Usage : `/boomer_v3 play <nom>`")
        return
    if player.play(name):
        say(f":arrow_forward: Lecture de `{name}`.")
        return
    closest = player.find_closest_sound(name)
    if closest and player.play(closest):
        say(f":arrow_forward: Lecture de `{closest}` _(plus proche de `{name}`)_.")
    else:
        say(f":x: Son `{name}` introuvable. Utilise `/boomer_v3 list` pour voir les sons disponibles.")


def _cmd_add(say, player: SoundPlayer, command: dict, name: str):
    if not name:
        say("Usage : `/boomer_v3 add <nom>`")
        return
    if player.sound_exists(name):
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


def _cmd_rename(say, player: SoundPlayer, arg: str):
    parts = arg.split(maxsplit=1)
    if len(parts) != 2:
        say("Usage : `/boomer_v3 rename <ancien-nom> <nouveau-nom>`")
        return
    old_name, new_name = parts
    if not player.sound_exists(old_name):
        closest = player.find_closest_sound(old_name)
        if closest:
            old_name = closest
            say(f":mag: Son le plus proche trouvé : `{old_name}`.")
        else:
            say(f":x: Son `{old_name}` introuvable.")
            return
    ok, reason = player.rename_sound(old_name, new_name)
    if ok:
        say(f":pencil2: Son `{old_name}` renommé en `{new_name}`.")
    else:
        say(f":x: {reason}")


def _panel_blocks(player: SoundPlayer) -> list:
    vol = int(player.get_volume() * 100)
    muted = player.is_muted()
    status = f":mute: Muté | Volume : {vol}%" if muted else f":loud_sound: Volume : {vol}%"
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f":boomer: *Boomer* — {status}"},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "⏹ Stop"},
                    "action_id": "boomer_stop",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🔇 Mute" if not muted else "🔊 Unmute"},
                    "action_id": "boomer_mute_toggle",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🔉 Vol −"},
                    "action_id": "boomer_vol_down",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🔊 Vol +"},
                    "action_id": "boomer_vol_up",
                },
            ],
        },
    ]


def _cmd_panel(say, player: SoundPlayer):
    say(blocks=_panel_blocks(player), text="Boomer Control Panel")


def _refresh_panel(body: dict, client: WebClient, player: SoundPlayer):
    channel = body["channel"]["id"]
    ts = body["message"]["ts"]
    client.chat_update(channel=channel, ts=ts, blocks=_panel_blocks(player), text="Boomer Control Panel")


def _cmd_map(say, client: WebClient, player: SoundPlayer, midi: MidiListener, channel: str, user: str, name: str):
    if not name:
        say("Usage : `/boomer_v3 map <nom>`")
        return
    if name not in MIDI_ACTIONS and not player.sound_exists(name):
        closest = player.find_closest_sound(name)
        if closest:
            name = closest
            say(f":mag: Son le plus proche trouvé : `{name}`.")
        else:
            say(f":x: Son `{name}` introuvable. Utilise `/boomer_v3 list` pour voir les sons disponibles.")
            return
    if midi.has_interceptor():
        say(":hourglass: Une assignation est déjà en cours. Attends qu'elle se termine (60 s max).")
        return

    key = (channel, user)

    def post(text: str):
        client.chat_postMessage(channel=channel, text=text)

    def cancel_pending():
        with _pending_maps_lock:
            state = _pending_maps.pop(key, None)
        if state:
            state.get("timer") and state["timer"].cancel()
            midi.clear_note_interceptor()

    def on_note(note: int) -> bool:
        with _pending_maps_lock:
            state = _pending_maps.get(key)
        if state is None:
            midi.clear_note_interceptor()
            return False

        note_label = _note_name(note)
        mappings = player.get_midi_mapping()
        existing = mappings.get(note)

        if state["awaiting_confirm"]:
            if note == state["conflict_note"]:
                # Confirmed: overwrite
                player.set_midi_mapping(note, state["name"])
                cancel_pending()
                post(f":white_check_mark: Touche `{note_label}` → `{state['name']}` (remplace `{state['conflict_name']}`).")
            else:
                prev_note_label = _note_name(state["conflict_note"])
                if existing is None:
                    player.set_midi_mapping(note, state["name"])
                    cancel_pending()
                    post(
                        f":leftwards_arrow_with_hook: Confirmation pour `{prev_note_label}` annulée.\n"
                        f":white_check_mark: Touche `{note_label}` → `{state['name']}`."
                    )
                else:
                    with _pending_maps_lock:
                        state["awaiting_confirm"] = True
                        state["conflict_note"] = note
                        state["conflict_name"] = existing
                    post(
                        f":leftwards_arrow_with_hook: Confirmation pour `{prev_note_label}` annulée.\n"
                        f":warning: La touche `{note_label}` joue déjà `{existing}`. "
                        f"Appuie à nouveau sur cette touche pour confirmer le remplacement."
                    )
            return True

        if existing is None:
            player.set_midi_mapping(note, state["name"])
            cancel_pending()
            post(f":white_check_mark: Touche `{note_label}` → `{state['name']}`.")
        else:
            with _pending_maps_lock:
                state["awaiting_confirm"] = True
                state["conflict_note"] = note
                state["conflict_name"] = existing
            post(
                f":warning: La touche `{note_label}` joue déjà `{existing}`. "
                f"Appuie à nouveau sur cette touche pour confirmer le remplacement."
            )
        return True

    def on_timeout():
        with _pending_maps_lock:
            if key not in _pending_maps:
                return
        cancel_pending()
        post(f":timer_clock: Assignation de `{name}` annulée (aucune touche pressée dans le délai imparti).")

    timer = threading.Timer(60.0, on_timeout)
    timer.daemon = True

    with _pending_maps_lock:
        _pending_maps[key] = {
            "name": name,
            "awaiting_confirm": False,
            "conflict_note": None,
            "conflict_name": None,
            "timer": timer,
        }

    midi.set_note_interceptor(on_note)
    timer.start()
    say(f":musical_keyboard: Appuie sur la touche MIDI à assigner à `{name}`… (60 s)")


def _cmd_list(say, player: SoundPlayer):
    sounds = player.list_sounds()
    if not sounds:
        say(":speaker: Aucun son disponible pour le moment.")
        return
    lines = "\n".join(f"• `{s}`" for s in sounds)
    say(f":musical_note: Sons disponibles :\n{lines}")


def _cmd_tts(say, tts: TtsEngine, text: str):
    if not text:
        say("Usage : `/boomer_v3 tts <texte>`")
        return
    voice = tts.get_current_voice()
    voice_hint = f" _(voix : `{voice}`)_" if voice else ""
    say(f":speaking_head_in_silhouette: *{text}*{voice_hint}")
    threading.Thread(target=tts.speak, args=(text,), daemon=True).start()


def _cmd_voice(say, tts: TtsEngine, arg: str):
    if arg in ("list", "liste"):
        voices = tts.list_voices()
        if not voices:
            say(":x: Aucune voix disponible.")
            return
        current = tts.get_current_voice()
        lines = []
        for v in voices:
            marker = " ◀ active" if v["id"] == current else ""
            lines.append(f"• `{v['id']}` — {v['name']}{marker}")
        say(f":microphone: Voix disponibles :\n" + "\n".join(lines))
    elif arg:
        voice_id = tts.set_voice(arg)
        if voice_id:
            say(f":white_check_mark: Voix changée : `{voice_id}`")
        else:
            say(f":x: Voix `{arg}` introuvable. Utilise `/boomer_v3 voice list` pour voir les voix disponibles.")
    else:
        say("Usage : `/boomer_v3 voice list` ou `/boomer_v3 voice <identifiant>`")


def _cmd_volume(say, player: SoundPlayer, arg: str):
    if arg in ("up", "haut", "+"):
        vol = player.volume_up()
        say(f":loud_sound: Volume : {int(vol * 100)} %")
    elif arg in ("down", "bas", "-"):
        vol = player.volume_down()
        say(f":sound: Volume : {int(vol * 100)} %")
    elif arg.rstrip("%").isdigit():
        requested = int(arg.rstrip("%")) / 100
        player.set_volume(requested)
        actual = player.get_volume()
        if actual < requested:
            say(f":loud_sound: Volume réglé à {int(actual * 100)} % (max autorisé — valeur demandée : {int(requested * 100)} %).")
        else:
            say(f":loud_sound: Volume réglé à {int(actual * 100)} %.")
    else:
        say("Usage : `/boomer_v3 volume up|down|<0-100>`")


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
        "• `/boomer_v3 play <nom>` — jouer un son\n"
        "• `/boomer_v3 panel` — afficher le panneau de contrôle interactif\n"
        "• `/boomer_v3 stop` — arrêter la lecture en cours\n"
        "• `/boomer_v3 add <nom>` — ajouter un son (puis envoyer le fichier)\n"
        "• `/boomer_v3 rename <ancien> <nouveau>` — renommer un son\n"
        "• `/boomer_v3 map <nom>` — assigner un son à une touche MIDI (interactif)\n"
        "• `/boomer_v3 map volume+` — assigner une touche MIDI au volume +\n"
        "• `/boomer_v3 map volume-` — assigner une touche MIDI au volume −\n"
        "• `/boomer_v3 list` — lister les sons disponibles\n"
        "• `/boomer_v3 tts <texte>` — synthèse vocale\n"
        "• `/boomer_v3 voice list` — lister les voix TTS disponibles\n"
        "• `/boomer_v3 voice <id>` — changer la voix TTS\n"
        "• `/boomer_v3 mute` — couper le son\n"
        "• `/boomer_v3 unmute` — rétablir le son\n"
        "• `/boomer_v3 volume up|down|<0-100>` — régler le volume\n"
        "• `/boomer_v3 help` — afficher cette aide"
    )
