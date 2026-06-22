import logging
import os
import re
import tempfile
import threading
import requests
from slack_bolt import App
from slack_sdk import WebClient
from boomer.sound_player import SoundPlayer, MIDI_ACTIONS
from boomer.tts_engine import TtsEngine, LANG_MAP
from boomer.midi_listener import MidiListener
from boomer.scheduler import Scheduler, parse_days, days_label

logger = logging.getLogger(__name__)

_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

def _note_name(note: int) -> str:
    return f"{_NOTE_NAMES[note % 12]}{(note // 12) - 1}"

# (channel_id, user_id) -> sound name waiting for a file upload
_pending_additions: dict[tuple[str, str], str] = {}

# (channel_id, user_id) -> ongoing MIDI assignment state
_pending_maps: dict[tuple[str, str], dict] = {}
_pending_maps_lock = threading.Lock()


def create_slack_app(player: SoundPlayer, tts: TtsEngine, midi: MidiListener,
                     scheduler: Scheduler) -> App:
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
        arg = parts[1].strip().strip("`") if len(parts) > 1 else ""

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
        elif action in ("sounds", "sons"):
            _cmd_sounds_panel(say, player, command["channel_id"])
        elif action in ("delete", "supprimer", "remove"):
            _cmd_delete(say, player, arg)
        elif action == "map":
            _cmd_map(say, slack_client, player, midi, command["channel_id"], command["user_id"], arg)
        elif action == "tts":
            _cmd_tts(say, tts, arg)
        elif action == "panel":
            _cmd_panel(say, player, command["channel_id"])
        elif action == "stop":
            player.stop()
            say(":black_square_for_stop: Lecture arrêtée.")
            _refresh_stored_panel(slack_client, player)
        elif action == "mute":
            player.mute()
            say(":mute: Son coupé.")
            _refresh_stored_panel(slack_client, player)
        elif action == "unmute":
            vol = player.unmute()
            say(f":loud_sound: Son rétabli à {int(vol * 100)} %.")
            _refresh_stored_panel(slack_client, player)
        elif action in ("volume", "vol"):
            _cmd_volume(say, player, arg)
            _refresh_stored_panel(slack_client, player)
        elif action == "schedule":
            _cmd_schedule(say, scheduler, player, arg)
        else:
            say(f":x: Commande inconnue : `{action}`\n\n{_usage()}")

    def on_midi_volume(action: str, vol: float):
        info = player.get_panel_info()
        if not info:
            return
        icon = ":loud_sound:" if action == "volume+" else ":sound:"

        def _notify():
            _refresh_stored_panel(slack_client, player)
            slack_client.chat_postMessage(
                channel=info["channel"],
                text=f"{icon} Volume : {int(vol * 100)} %",
            )

        threading.Thread(target=_notify, daemon=True).start()

    midi.set_volume_action_callback(on_midi_volume)

    def on_midi_play(name: str):
        info = player.get_panel_info() or player.get_panel_info("sounds_panel")
        if not info:
            return
        def _notify():
            slack_client.chat_postMessage(
                channel=info["channel"],
                text=f":musical_keyboard: `{name}`",
            )
        threading.Thread(target=_notify, daemon=True).start()

    midi.set_play_callback(on_midi_play)

    @app.action(re.compile(r"^boomer_play_\d+$"))
    def handle_play_button(ack, body, client):
        ack()
        sound_name = body["actions"][0]["value"]
        player.play(sound_name)
        info = player.get_panel_info("sounds_panel")
        if info:
            try:
                client.chat_update(
                    channel=info["channel"],
                    ts=info["ts"],
                    blocks=_sounds_panel_blocks(player, last_played=sound_name),
                    text="Sons disponibles",
                )
            except Exception:
                player.clear_panel_info("sounds_panel")

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

    def on_scheduled_fire(schedule_id: str, sound: str):
        info = player.get_panel_info() or player.get_panel_info("sounds_panel")
        if not info:
            return
        def _notify():
            slack_client.chat_postMessage(
                channel=info["channel"],
                text=f":alarm_clock: Son planifié : `{sound}`",
            )
        threading.Thread(target=_notify, daemon=True).start()

    scheduler.set_on_fire_callback(on_scheduled_fire)

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


def _cmd_panel(say, player: SoundPlayer, channel: str):
    result = say(blocks=_panel_blocks(player), text="Boomer Control Panel")
    if result and result.get("ts"):
        player.set_panel_info(channel, result["ts"])


def _refresh_stored_panel(client: WebClient, player: SoundPlayer):
    info = player.get_panel_info()
    if not info:
        return
    try:
        client.chat_update(
            channel=info["channel"],
            ts=info["ts"],
            blocks=_panel_blocks(player),
            text="Boomer Control Panel",
        )
    except Exception:
        player.clear_panel_info()


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


def _sounds_panel_blocks(player: SoundPlayer, last_played: str | None = None) -> list:
    sounds = player.list_sounds()
    header = ":musical_note: *Sons disponibles*"
    if last_played:
        header += f"  |  :arrow_forward: `{last_played}`"
    blocks: list = [{"type": "section", "text": {"type": "mrkdwn", "text": header}}]
    for i in range(0, len(sounds), 5):
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": name},
                    "value": name,
                    "action_id": f"boomer_play_{i + j}",
                }
                for j, name in enumerate(sounds[i:i + 5])
            ],
        })
    return blocks


def _cmd_sounds_panel(say, player: SoundPlayer, channel: str):
    result = say(blocks=_sounds_panel_blocks(player), text="Sons disponibles")
    if result and result.get("ts"):
        player.set_panel_info(channel, result["ts"], key="sounds_panel")


def _cmd_delete(say, player: SoundPlayer, name: str):
    if not name:
        say("Usage : `/boomer_v3 delete <nom>`")
        return
    if not player.sound_exists(name):
        closest = player.find_closest_sound(name)
        if closest:
            name = closest
            say(f":mag: Son le plus proche trouvé : `{name}`.")
        else:
            say(f":x: Son `{name}` introuvable.")
            return
    player.delete_sound(name)
    say(f":wastebasket: Son `{name}` supprimé.")


_SCHEDULE_HELP = (
    "*`/boomer_v3 schedule` — planifier un son*\n"
    "• `/boomer_v3 schedule <HH:MM> <son>` — tous les jours\n"
    "• `/boomer_v3 schedule <HH:MM> lun-ven <son>` — jours de semaine\n"
    "• `/boomer_v3 schedule <HH:MM> weekend <son>` — sam et dim\n"
    "• `/boomer_v3 schedule <HH:MM> lun,mer,ven <son>` — jours spécifiques\n"
    "• `/boomer_v3 schedule list` — lister les planifications actives\n"
    "• `/boomer_v3 schedule cancel <id>` — supprimer une planification\n"
    "_Jours supportés : lun mar mer jeu ven sam dim (ou mon tue wed thu fri sat sun)_"
)

def _cmd_schedule(say, scheduler: Scheduler, player: SoundPlayer, arg: str):
    parts = arg.split()
    if not parts or parts[0] in ("help", "aide"):
        say(_SCHEDULE_HELP)
        return
    if parts[0] in ("list", "liste"):
        schedules = scheduler.list_all()
        if not schedules:
            say(":calendar: Aucune planification active.")
            return
        lines = []
        for s in schedules:
            label = days_label(s.get("days"))
            lines.append(f"• `{s['id']}` — {s['time']} ({label}) → `{s['sound']}`")
        say(":calendar: Planifications :\n" + "\n".join(lines))
        return
    if parts[0] in ("cancel", "annuler") and len(parts) == 2:
        if scheduler.remove(parts[1]):
            say(f":white_check_mark: Planification `{parts[1]}` supprimée.")
        else:
            say(f":x: Identifiant `{parts[1]}` introuvable.")
        return
    # add: <heure> [jours] <son>
    if len(parts) < 2:
        say("Usage : `/boomer_v3 schedule <heure> [jours] <son>` | `list` | `cancel <id>`")
        return
    time_str = parts[0]
    if ":" not in time_str:
        say(f":x: Format d'heure invalide : `{time_str}` (attendu HH:MM).")
        return
    # detect optional day spec (contains '-', ',' or known day keyword)
    days = None
    sound_parts_start = 1
    if len(parts) >= 3:
        candidate = parts[1].lower()
        parsed = parse_days(candidate)
        if parsed is not None or candidate in ("tous", "all", "semaine", "weekend", "weekdays"):
            days = parsed
            sound_parts_start = 2
    sound = " ".join(parts[sound_parts_start:])
    if not player.sound_exists(sound):
        closest = player.find_closest_sound(sound)
        if closest:
            sound = closest
            say(f":mag: Son le plus proche : `{sound}`.")
        else:
            say(f":x: Son `{sound}` introuvable.")
            return
    sid = scheduler.add(time_str, sound, days)
    if sid is None:
        say(f":x: Heure invalide : `{time_str}`.")
        return
    label = days_label(days)
    say(f":white_check_mark: Planifié `{sound}` à {time_str} ({label}). ID : `{sid}`")


def _cmd_tts(say, tts: TtsEngine, arg: str):
    if not arg:
        say("Usage : `/boomer_v3 tts <texte> [lang]` | `tts rate <50-400>` | `tts list`")
        return
    if arg in ("list", "liste"):
        voices = tts.list_voices()
        lines = []
        for v in voices:
            status = f"`{v['id']}`" if v["id"] else ":x: non disponible"
            lines.append(f"• `{v['code']}` ({v['lang']}) → {status}")
        say(":microphone: Langues disponibles :\n" + "\n".join(lines))
        return
    if arg.startswith("rate ") or arg.startswith("vitesse "):
        val = arg.split(maxsplit=1)[1]
        if val.lstrip("-").isdigit():
            rate = tts.set_rate(int(val))
            say(f":speech_balloon: Vitesse TTS : {rate} mots/min.")
        else:
            say(":x: Valeur invalide. Utilise `/boomer_v3 tts rate <50-400>`.")
        return
    words = arg.split()
    lang = None
    if len(words) >= 2 and words[-1].lower() in LANG_MAP:
        lang = words[-1].lower()
        text = " ".join(words[:-1])
    else:
        text = arg
    lang_hint = f" _(lang : `{lang or 'fr'}`)_"
    say(f":speaking_head_in_silhouette: *{text}*{lang_hint}")
    threading.Thread(target=tts.speak, args=(text, lang), daemon=True).start()


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
        "• `/boomer_v3 stop` — arrêter la lecture en cours\n"
        "• `/boomer_v3 vol up|down|<0-100>` — régler le volume\n"
        "• `/boomer_v3 list` — lister les sons disponibles\n"
        "• `/boomer_v3 add <nom>` — ajouter un son (puis envoyer le fichier)\n"
        "• `/boomer_v3 rename <ancien> <nouveau>` — renommer un son\n"
        "• `/boomer_v3 map <nom>` — assigner un son à une touche MIDI (interactif)\n"
        "• `/boomer_v3 delete <nom>` — supprimer un son\n"
        "• `/boomer_v3 panel` — afficher le panneau de contrôle interactif\n"
        "• `/boomer_v3 sounds` — panneau interactif avec un bouton par son\n"
        "• `/boomer_v3 tts <texte> [lang]` — synthèse vocale (lang: fr, en, es, de… défaut: fr)\n"
        "• `/boomer_v3 tts rate <50-400>` — régler la vitesse TTS\n"
        "• `/boomer_v3 tts list` — lister les langues disponibles\n"
        "• `/boomer_v3 mute / unmute` — couper / rétablir le son\n"
        "• `/boomer_v3 schedule <HH:MM> [jours] <son>` — planifier un son (ex: `09:00 lun-ven matin`)\n"
        "• `/boomer_v3 schedule list / cancel <id>` — gérer les planifications\n"
        "• `/boomer_v3 help` — afficher cette aide"
    )
