import datetime
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import requests
from slack_bolt import App
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from boomer import audio_effects
from boomer.audio_effects import EffectError
from boomer.sound_player import (SoundPlayer, MIDI_ACTIONS, SUPPORTED_EXTENSIONS,
                                 can_decode, sniff_extension)
from boomer.stats import Stats, ACTOR_MIDI, ACTOR_SCHEDULE
from boomer.tts_engine import TtsEngine, LANG_MAP
from boomer.midi_listener import MidiListener
from boomer.scheduler import Scheduler, parse_days, days_label

logger = logging.getLogger(__name__)

_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

_EFFECTS_HELP = (
    "Effets cumulables : `--reverse`, `--speed <0.25-4>`, "
    "`--nightcore`, `--chipmunk`, `--vaporwave`, `--slow`, `--deep`"
)

# Weekly recap: Friday 17:00
_RECAP_WEEKDAY = 4
_RECAP_HOUR = 17

_PERIOD_ALIASES = {
    "jour": "day", "aujourdhui": "day", "day": "day", "today": "day",
    "semaine": "week", "week": "week",
    "mois": "month", "month": "month",
    "tout": "all", "all": "all", "total": "all",
}
_PERIOD_LABELS = {"day": "aujourd'hui", "week": "cette semaine", "month": "ce mois-ci", "all": "depuis toujours"}

# Slack caps a view at 100 blocks; keep a margin for the control panel
_MAX_HOME_BLOCKS = 90

# Above this, a request is worth a warning in the logs
_SLOW_REQUEST_SECONDS = 1.0

def _note_name(note: int) -> str:
    return f"{_NOTE_NAMES[note % 12]}{(note // 12) - 1}"


# Slack user IDs -> display names, to keep the logs readable
_user_names: dict[str, str] = {}


def _remember_user(body: dict) -> None:
    """Interactive payloads carry the name along with the ID: cache it for free."""
    user = body.get("user")
    if isinstance(user, dict):
        user_id, name = user.get("id"), user.get("username") or user.get("name")
    else:
        user_id, name = body.get("user_id"), body.get("user_name")
    if user_id and name:
        _user_names.setdefault(user_id, name)


def _user_label(user_id: str, client: WebClient | None = None) -> str:
    """Name and ID of a Slack user, looked up once then cached."""
    if not user_id:
        return "?"
    name = _user_names.get(user_id)
    if name is None and client is not None:
        try:
            profile = client.users_info(user=user_id)["user"]
            name = (profile.get("profile") or {}).get("display_name") or profile.get("real_name")
        except SlackApiError as exc:
            logger.debug("Cannot resolve user %s: %s", user_id, exc)
        # Cache the failure too, so a lookup is never retried on every request
        name = _user_names.setdefault(user_id, name or "")
    return f"{name} ({user_id})" if name else user_id


def _describe_request(body: dict, client: WebClient | None = None) -> str:
    """One-line summary of an incoming Slack payload, whatever its shape."""
    _remember_user(body)
    event = body.get("event") or {}
    user_id = ((body.get("user") or {}).get("id") if isinstance(body.get("user"), dict)
               else body.get("user")) or body.get("user_id") or event.get("user")
    user = _user_label(user_id, client)
    if body.get("command"):
        return f"command {body['command']} {body.get('text', '')!r} from {user}"
    actions = body.get("actions") or []
    if actions:
        surface = "App Home" if _is_home(body) else "message panel"
        return (f"action {actions[0].get('action_id')} value={actions[0].get('value')!r} "
                f"from {user} on the {surface}")
    if body.get("type") == "shortcut" or body.get("callback_id"):
        return f"shortcut {body.get('callback_id')} from {user}"
    if event:
        files = event.get("files") or []
        extra = f" with {len(files)} file(s)" if files else ""
        return f"event {event.get('type')} from {user}{extra}"
    return f"{body.get('type', 'unknown')} from {user}"

# (channel_id, user_id) -> sound name waiting for a file upload
_pending_additions: dict[tuple[str, str], str] = {}

# (channel_id, user_id) -> ongoing MIDI assignment state
_pending_maps: dict[tuple[str, str], dict] = {}
_pending_maps_lock = threading.Lock()

# Last sound played, shown on the panels and the App Home
_last_played: str | None = None

# Volume announcements are debounced: holding a MIDI volume key fires one press per step
_VOLUME_NOTICE_DELAY = 1.5
_volume_notice_timer: threading.Timer | None = None
_volume_notice_lock = threading.Lock()


def create_slack_app(player: SoundPlayer, tts: TtsEngine, midi: MidiListener,
                     scheduler: Scheduler, stats: Stats) -> App:
    app = App(
        token=os.environ["SLACK_BOT_TOKEN"],
        signing_secret=os.environ["SLACK_SIGNING_SECRET"],
    )
    # Reusable WebClient for async callbacks outside the Slack request context
    slack_client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])

    @app.middleware
    def log_requests(body, client, next):
        """Trace what Slack sends us, then split the blame for a late message:
        Slack delivery lag vs. our own handling time."""
        logger.info("Slack request: %s", _describe_request(body, client))
        started = time.monotonic()
        try:
            next()
        finally:
            elapsed = time.monotonic() - started
            event_time = body.get("event_time")
            lag = time.time() - event_time if event_time else None
            if elapsed > _SLOW_REQUEST_SECONDS or (lag is not None and lag > _SLOW_REQUEST_SECONDS):
                logger.warning(
                    "Slow Slack request: handled in %.1f s, delivered %s after the event (type=%s)",
                    elapsed,
                    f"{lag:.1f} s" if lag is not None else "n/a",
                    body.get("type") or body.get("command") or "?",
                )

    @app.command("/boomer_v3")
    def handle_boomer(ack, command, say):
        ack()
        text = command.get("text", "").strip()
        actor = _user_label(command.get("user_id"), slack_client)
        logger.info("Command from %s in %s: /boomer_v3 %s",
                    actor, command.get("channel_id"), text or "(no argument)")
        parts = text.split(maxsplit=1)
        action = parts[0].lower() if parts else ""
        arg = parts[1].strip().strip("`") if len(parts) > 1 else ""

        if action in ("help", "aide", ""):
            say(_usage())
        elif action == "play":
            _cmd_play(say, player, stats, command["user_id"], arg)
        elif action in ("random", "aleatoire", "hasard"):
            _cmd_random(say, player, stats, command["user_id"], arg)
        elif action in ("stats", "recap", "top"):
            _cmd_stats(say, stats, command["user_id"], arg)
        elif action == "add":
            _cmd_add(say, player, command, arg)
        elif action == "rename":
            _cmd_rename(say, player, arg)
        elif action == "list":
            _cmd_list(say, player, stats)
        elif action in ("sounds", "sons"):
            _cmd_sounds_panel(say, player, command["channel_id"])
        elif action in ("delete", "supprimer", "remove"):
            _cmd_delete(say, player, arg)
        elif action == "map":
            _cmd_map(say, slack_client, player, midi, command["channel_id"], command["user_id"], arg)
        elif action == "tts":
            _cmd_tts(say, tts, stats, command["user_id"], arg)
        elif action == "panel":
            _cmd_panel(say, player, command["channel_id"])
        elif action == "stop":
            player.stop(actor)
            say(":black_square_for_stop: Lecture arrêtée.")
            _refresh_stored_panel(slack_client, player)
        elif action == "mute":
            player.mute(actor)
            say(":mute: Son coupé.")
            _refresh_stored_panel(slack_client, player)
        elif action == "unmute":
            vol = player.unmute(actor)
            say(f":loud_sound: Son rétabli à {int(vol * 100)} %.")
            _refresh_stored_panel(slack_client, player)
        elif action in ("volume", "vol"):
            _cmd_volume(say, player, arg, actor)
            _refresh_stored_panel(slack_client, player)
        elif action == "schedule":
            _cmd_schedule(say, scheduler, player, arg)
        else:
            say(f":x: Commande inconnue : `{action}`\n\n{_usage()}")

    def on_midi_volume(action: str, vol: float):
        _schedule_volume_notice(slack_client, player, action)

    midi.set_volume_action_callback(on_midi_volume)

    def on_midi_play(name: str):
        global _last_played
        _last_played = name
        stats.record(name, ACTOR_MIDI)
        info = player.get_panel_info() or player.get_panel_info("sounds_panel")
        if not info:
            logger.info("MIDI played '%s' but no panel channel is known: nothing announced", name)
            return
        def _notify():
            slack_client.chat_postMessage(
                channel=info["channel"],
                text=f":musical_keyboard: `{name}`",
            )
        threading.Thread(target=_notify, daemon=True).start()

    midi.set_play_callback(on_midi_play)

    def play_from_button(body, client, sound_name: str) -> bool:
        global _last_played
        if not player.play(sound_name, actor=_user_label(body["user"]["id"], client)):
            _in_background(
                _notify_from_surface, body, client,
                f":x: Le fichier `{sound_name}` n'a pas pu être décodé (format audio non reconnu).",
            )
            return False
        _last_played = sound_name
        stats.record(sound_name, body["user"]["id"])
        _in_background(_refresh_sounds_panel, client, player)
        return True

    @app.action(re.compile(r"^boomer_play_\d+$"))
    def handle_play_button(ack, body, client):
        ack()
        play_from_button(body, client, body["actions"][0]["value"])
        _in_background(_refresh_surface, body, client, player, stats)

    @app.action("boomer_random")
    def handle_action_random(ack, body, client):
        ack()
        name = player.random_sound()
        if name is None:
            _in_background(_notify_from_surface, body, client, ":speaker: Aucun son disponible.")
            return
        if play_from_button(body, client, name):
            _in_background(_notify_from_surface, body, client, f":game_die: Au hasard : `{name}`")
        _in_background(_refresh_surface, body, client, player, stats)

    @app.action("boomer_stop")
    def handle_action_stop(ack, body, client):
        ack()
        player.stop(_user_label(body["user"]["id"], client))
        _in_background(_refresh_surface, body, client, player, stats)

    @app.action("boomer_mute_toggle")
    def handle_action_mute_toggle(ack, body, client):
        ack()
        actor = _user_label(body["user"]["id"], client)
        if player.is_muted():
            player.unmute(actor)
        else:
            player.mute(actor)
        _in_background(_refresh_surface, body, client, player, stats)

    @app.action("boomer_vol_down")
    def handle_action_vol_down(ack, body, client):
        ack()
        player.volume_down(actor=_user_label(body["user"]["id"], client))
        _in_background(_refresh_surface, body, client, player, stats)

    @app.action("boomer_vol_up")
    def handle_action_vol_up(ack, body, client):
        ack()
        player.volume_up(actor=_user_label(body["user"]["id"], client))
        _in_background(_refresh_surface, body, client, player, stats)

    @app.shortcut("boomer_speak")
    def handle_speak_shortcut(ack, shortcut, respond, client):
        ack()
        text = _clean_slack_text(shortcut.get("message", {}).get("text", ""))
        if not text:
            respond(":x: Ce message ne contient pas de texte à lire.")
            return
        stats.record("tts", shortcut["user"]["id"])
        actor = _user_label(shortcut["user"]["id"], client)
        threading.Thread(target=tts.speak, args=(text, None, actor), daemon=True).start()
        _announce_speak(client, respond, player, shortcut, text)

    @app.event("app_home_opened")
    def handle_home_opened(event, client):
        _in_background(_publish_home, client, player, stats, event["user"])

    def on_scheduled_fire(schedule_id: str, sound: str):
        global _last_played
        _last_played = sound
        stats.record(sound, ACTOR_SCHEDULE)
        info = player.get_panel_info() or player.get_panel_info("sounds_panel")
        if not info:
            logger.info("Schedule played '%s' but no panel channel is known: nothing announced", sound)
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
            logger.info("File from %s in %s ignored: no pending `add`", user, channel)
            return

        name = _pending_additions.pop(key)
        file_info = files[0]
        logger.info("Receiving '%s' for sound '%s' (filetype=%s)",
                    file_info.get("name"), name, file_info.get("filetype"))
        _download_and_save(say, player, name, file_info, overwrite=False)

    _start_weekly_recap(slack_client, player, stats)

    return app

def _start_weekly_recap(client: WebClient, player: SoundPlayer, stats: Stats):
    """Post the week's leaderboard in the panel channel, then re-arm for next week."""
    def fire():
        _start_weekly_recap(client, player, stats)
        info = player.get_panel_info() or player.get_panel_info("sounds_panel")
        if not info:
            logger.info("Weekly recap skipped: no known panel channel.")
            return
        if stats.total("week") == 0:
            return
        try:
            client.chat_postMessage(
                channel=info["channel"],
                text="Récap de la semaine",
                blocks=_stats_blocks(stats, "week", title=":trophy: *Récap de la semaine*"),
            )
        except Exception:
            logger.exception("Cannot post the weekly recap")

    timer = threading.Timer(_seconds_until_recap(), fire)
    timer.daemon = True
    timer.start()


def _seconds_until_recap() -> float:
    now = datetime.datetime.now()
    days_ahead = (_RECAP_WEEKDAY - now.weekday()) % 7
    target = (now + datetime.timedelta(days=days_ahead)).replace(
        hour=_RECAP_HOUR, minute=0, second=0, microsecond=0
    )
    if target <= now:
        target += datetime.timedelta(days=7)
    return (target - now).total_seconds()





def _resolve_names(say, player: SoundPlayer, raw: str) -> list[str] | None:
    """Turn `a+b+c` into existing sound names, fuzzy-matching each one. None if any is missing."""
    resolved = []
    for part in (p.strip() for p in raw.split("+")):
        if not part:
            continue
        if player.sound_exists(part):
            resolved.append(part)
            continue
        closest = player.find_closest_sound(part)
        if closest is None:
            say(f":x: Son `{part}` introuvable. Utilise `/boomer_v3 list` pour voir les sons disponibles.")
            return None
        say(f":mag: Son le plus proche de `{part}` : `{closest}`.")
        resolved.append(closest)
    return resolved or None


def _cmd_play(say, player: SoundPlayer, stats: Stats, user: str, arg: str):
    global _last_played
    if not arg:
        say(f"Usage : `/boomer_v3 play <nom>[+<nom>…] [effets]`\n_{_EFFECTS_HELP}_")
        return
    try:
        names_part, effects = audio_effects.parse_effects(arg)
    except EffectError as e:
        say(f":x: {e}\n_{_EFFECTS_HELP}_")
        return

    names = _resolve_names(say, player, names_part)
    if not names:
        return

    suffix = audio_effects.describe(effects)
    actor = _user_label(user)
    if len(names) == 1:
        if not player.play(names[0], effects, actor):
            say(f":x: Le fichier `{names[0]}` n'a pas pu être décodé (format audio non reconnu).")
            return
        say(f":arrow_forward: Lecture de `{names[0]}`{suffix}.")
        _last_played = names[0]
        stats.record(names[0], user)
        return
    if not player.play_sequence(names, effects, actor):
        say(":x: Aucun de ces sons n'a pu être joué.")
        return
    say(":arrow_forward: Enchaînement : " + " → ".join(f"`{n}`" for n in names) + suffix)
    _last_played = names[-1]
    for name in names:
        stats.record(name, user)


def _cmd_random(say, player: SoundPlayer, stats: Stats, user: str, arg: str):
    global _last_played
    try:
        _, effects = audio_effects.parse_effects(arg)
    except EffectError as e:
        say(f":x: {e}")
        return
    name = player.random_sound()
    if name is None:
        say(":speaker: Aucun son disponible pour le moment.")
        return
    if not player.play(name, effects, _user_label(user)):
        say(f":x: Le fichier `{name}` n'a pas pu être décodé (format audio non reconnu).")
        return
    _last_played = name
    stats.record(name, user)
    say(f":game_die: Au hasard : `{name}`{audio_effects.describe(effects)}")


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


def _split_rename(player: SoundPlayer, arg: str) -> tuple[str, str] | None:
    """Tell both names apart even when they contain spaces.

    Quotes win (`rename "mon son" "nouveau nom"`); otherwise the longest leading
    part that matches an existing sound is taken as the old name.
    """
    try:
        quoted = shlex.split(arg)
    except ValueError:
        quoted = []
    if len(quoted) == 2:
        return quoted[0], quoted[1]

    parts = arg.split()
    if len(parts) < 2:
        return None
    for i in range(len(parts) - 1, 0, -1):
        if player.sound_exists(" ".join(parts[:i])):
            return " ".join(parts[:i]), " ".join(parts[i:])
    return parts[0], " ".join(parts[1:])


def _cmd_rename(say, player: SoundPlayer, arg: str):
    names = _split_rename(player, arg)
    if names is None:
        say('Usage : `/boomer_v3 rename <ancien-nom> <nouveau-nom>`\n'
            '_Avec des espaces : `/boomer_v3 rename "mon son" "nouveau nom"`_')
        return
    old_name, new_name = names
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
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🎲 Au hasard"},
                    "action_id": "boomer_random",
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
        logger.exception("Cannot refresh the control panel in %s, forgetting it", info["channel"])
        player.clear_panel_info()


def _schedule_volume_notice(client: WebClient, player: SoundPlayer, action: str):
    """Announce the volume once a burst of MIDI presses is over, not 2 % at a time."""
    global _volume_notice_timer
    with _volume_notice_lock:
        if _volume_notice_timer is not None:
            _volume_notice_timer.cancel()
        _volume_notice_timer = threading.Timer(
            _VOLUME_NOTICE_DELAY, _post_volume_notice, args=(client, player, action)
        )
        _volume_notice_timer.daemon = True
        _volume_notice_timer.start()


def _post_volume_notice(client: WebClient, player: SoundPlayer, action: str):
    global _volume_notice_timer
    with _volume_notice_lock:
        _volume_notice_timer = None
    _refresh_stored_panel(client, player)
    info = player.get_panel_info() or player.get_panel_info("sounds_panel")
    if not info:
        logger.info("Volume changed from MIDI but no panel channel is known.")
        return
    if player.is_muted():
        text = ":mute: Son coupé."
    else:
        icon = ":loud_sound:" if action == "volume+" else ":sound:"
        text = f"{icon} Volume : {int(player.get_volume() * 100)} %"
    try:
        client.chat_postMessage(channel=info["channel"], text=text)
    except SlackApiError:
        logger.exception("Cannot post the volume notice")


def _in_background(fn, *args):
    """Slack API calls do not gate the answer: keep the handler thread free."""
    def run():
        try:
            fn(*args)
        except Exception:
            logger.exception("Background Slack call failed: %s", getattr(fn, "__name__", fn))

    threading.Thread(target=run, daemon=True).start()


def _refresh_sounds_panel(client: WebClient, player: SoundPlayer):
    info = player.get_panel_info("sounds_panel")
    if not info:
        return
    try:
        client.chat_update(
            channel=info["channel"],
            ts=info["ts"],
            blocks=_sounds_panel_blocks(player, last_played=_last_played),
            text="Sons disponibles",
        )
    except Exception:
        logger.exception("Cannot refresh the sounds panel in %s, forgetting it", info["channel"])
        player.clear_panel_info("sounds_panel")


def _is_home(body: dict) -> bool:
    return body.get("container", {}).get("type") == "view"


def _refresh_surface(body: dict, client: WebClient, player: SoundPlayer, stats: Stats):
    """Redraw whichever surface the button was clicked from: App Home or a posted panel."""
    if _is_home(body):
        _publish_home(client, player, stats, body["user"]["id"])
        return
    message = body.get("message") or {}
    channel = (body.get("channel") or {}).get("id")
    if not message.get("ts") or not channel:
        return
    # The sounds panel carries its own blocks; only the control panel is refreshed here
    if any(b.get("action_id", "").startswith("boomer_play_")
           for block in message.get("blocks", []) for b in block.get("elements", [])):
        return
    client.chat_update(channel=channel, ts=message["ts"], blocks=_panel_blocks(player),
                       text="Boomer Control Panel")


def _notify_from_surface(body: dict, client: WebClient, text: str):
    channel = (body.get("channel") or {}).get("id") or body["user"]["id"]
    try:
        client.chat_postMessage(channel=channel, text=text)
    except Exception:
        logger.exception("Cannot post notification to %s", channel)


def _home_view(player: SoundPlayer, stats: Stats) -> dict:
    header = ":musical_note: *Sons disponibles*"
    if _last_played:
        header += f"  |  :arrow_forward: `{_last_played}`"

    blocks: list = _panel_blocks(player)
    blocks.append({"type": "divider"})
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": header}})
    blocks.extend(_sound_button_blocks(player)[:_MAX_HOME_BLOCKS])
    plays = stats.total("week")
    hint = (f"{plays} lectures cette semaine  |  " if plays else "")
    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": hint + "`/boomer_v3 play a+b+c --reverse` pour enchaîner  |  `/boomer_v3 stats` pour le classement",
        }],
    })
    return {"type": "home", "blocks": blocks}


def _publish_home(client: WebClient, player: SoundPlayer, stats: Stats, user_id: str):
    try:
        client.views_publish(user_id=user_id, view=_home_view(player, stats))
    except Exception:
        logger.exception("Cannot publish the App Home view")


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


def _cmd_list(say, player: SoundPlayer, stats: Stats):
    sounds = player.list_sounds()
    if not sounds:
        say(":speaker: Aucun son disponible pour le moment.")
        return
    counts = dict(stats.top_sounds(limit=None))
    lines = "\n".join(
        f"• `{s}` — {counts[s]} lecture{'s' if counts[s] > 1 else ''}" if s in counts else f"• `{s}` — jamais joué"
        for s in sounds
    )
    say(f":musical_note: Sons disponibles :\n{lines}")


def _sound_button_blocks(player: SoundPlayer) -> list:
    sounds = player.list_sounds()
    blocks: list = []
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


def _sounds_panel_blocks(player: SoundPlayer, last_played: str | None = None) -> list:
    header = ":musical_note: *Sons disponibles*"
    if last_played:
        header += f"  |  :arrow_forward: `{last_played}`"
    return [{"type": "section", "text": {"type": "mrkdwn", "text": header}}] + _sound_button_blocks(player)


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


def _actor_label(actor: str) -> str:
    if actor == ACTOR_MIDI:
        return ":musical_keyboard: clavier MIDI"
    if actor == ACTOR_SCHEDULE:
        return ":alarm_clock: planifications"
    return f"<@{actor}>"


def _stats_blocks(stats: Stats, period: str, title: str | None = None) -> list:
    total = stats.total(period)
    head = title or f":bar_chart: *Classement — {_PERIOD_LABELS[period]}*"
    if not total:
        return [{"type": "section", "text": {"type": "mrkdwn", "text": f"{head}\n_Aucune lecture sur la période._"}}]

    medals = [":first_place_medal:", ":second_place_medal:", ":third_place_medal:"]
    sounds = "\n".join(
        f"{medals[i] if i < 3 else '   •'} `{name}` — {count}"
        for i, (name, count) in enumerate(stats.top_sounds(period, limit=5))
    )
    humans = stats.top_actors(period, limit=5, humans_only=True)
    if humans:
        actors = "\n".join(
            f"{medals[i] if i < 3 else '   •'} {_actor_label(actor)} — {count}"
            f" _(`{stats.signature_sound(actor, period)}` en tête)_"
            for i, (actor, count) in enumerate(humans)
        )
    else:
        actors = "_Personne depuis Slack sur la période._"

    # The MIDI keyboard would top every ranking, so it gets its own line
    offstage = []
    midi_total = stats.total(period, actor=ACTOR_MIDI)
    if midi_total:
        offstage.append(
            f"{_actor_label(ACTOR_MIDI)} — {midi_total}"
            f" _(`{stats.signature_sound(ACTOR_MIDI, period)}` en tête)_"
        )
    scheduled = stats.top_sounds(period, actor=ACTOR_SCHEDULE, limit=None)
    if scheduled:
        offstage.append(
            f"{_actor_label(ACTOR_SCHEDULE)}\n"
            + "\n".join(f"• `{name}` — {count}" for name, count in scheduled)
        )

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"{head}\n{total} lectures au total."}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Sons les plus joués*\n{sounds}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Déclencheurs*\n{actors}"}},
    ]
    if offstage:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "\n".join(offstage)}]})
    return blocks


def _cmd_stats(say, stats: Stats, user: str, arg: str):
    parts = arg.lower().split()
    period = "week"
    mine = False
    for part in parts:
        if part in _PERIOD_ALIASES:
            period = _PERIOD_ALIASES[part]
        elif part in ("me", "moi"):
            mine = True
        else:
            say("Usage : `/boomer_v3 stats [jour|semaine|mois|tout] [moi]`")
            return

    if not mine:
        say(blocks=_stats_blocks(stats, period), text="Classement Boomer")
        return

    total = stats.total(period, actor=user)
    if not total:
        say(f":bar_chart: Aucune lecture à ton actif {_PERIOD_LABELS[period]}. Timide.")
        return
    top = "\n".join(f"• `{name}` — {count}" for name, count in stats.top_sounds(period, actor=user, limit=5))
    say(f":bar_chart: *Tes stats — {_PERIOD_LABELS[period]}*\n{total} lectures.\n{top}")


_MENTION_RE = re.compile(r"<[@#!][^>|]+(?:\|([^>]*))?>")
_LINK_RE = re.compile(r"<(https?://[^>|]+)(?:\|([^>]*))?>")
_EMOJI_RE = re.compile(r":[a-z0-9_+-]+:")
_MAX_SPEAK_CHARS = 300


def _announce_speak(client: WebClient, respond, player: SoundPlayer, shortcut: dict, text: str):
    """Announce in the panel channel, where the soundboard activity is followed.

    The shortcut fires from any message, often far from that channel, so the
    message it came from is only a fallback, and the caller alone the last resort.
    """
    user = shortcut["user"]["id"]
    info = player.get_panel_info() or player.get_panel_info("sounds_panel")
    origin = (shortcut.get("channel") or {}).get("id")
    # dict.fromkeys keeps the order and drops the duplicate when both are the same channel
    targets = [c for c in dict.fromkeys([info["channel"] if info else None, origin]) if c]
    for channel in targets:
        try:
            client.chat_postMessage(
                channel=channel,
                text=f":speaking_head_in_silhouette: <@{user}> a fait lire à voix haute : « {text} »",
            )
            return
        except SlackApiError as e:
            # not_in_channel / channel_not_found: the shortcut works everywhere, posting does not
            logger.info("Cannot announce in %s (%s)", channel, e)
    logger.info("Nowhere to announce the TTS (tried %s), answering %s privately",
                targets or "no channel", user)
    respond(f":speaking_head_in_silhouette: Lecture à voix haute de « {text} »")


def _clean_slack_text(text: str) -> str:
    """Strip Slack markup so the TTS engine does not read raw user IDs and URLs aloud."""
    text = _LINK_RE.sub(lambda m: m.group(2) or m.group(1), text)
    text = _MENTION_RE.sub(lambda m: m.group(1) or "", text)
    text = _EMOJI_RE.sub("", text)
    text = re.sub(r"^\s*>+", " ", text, flags=re.MULTILINE)
    text = re.sub(r"[*_~`]", " ", text)
    # Unescape last, so the entities do not get parsed as markup above
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_MAX_SPEAK_CHARS]


def _cmd_tts(say, tts: TtsEngine, stats: Stats, user: str, arg: str):
    if not arg:
        say("Usage : `/boomer_v3 tts <texte> [lang]` | `tts rate <50-400>` | `tts list`")
        return
    if arg in ("list", "liste"):
        lines = [f"• `{v['code']}` — {v['label']}" for v in tts.list_voices()]
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
    stats.record("tts", user)
    threading.Thread(target=tts.speak, args=(text, lang, _user_label(user)), daemon=True).start()


def _cmd_volume(say, player: SoundPlayer, arg: str, actor: str | None = None):
    if arg in ("up", "haut", "+"):
        vol = player.volume_up(actor=actor)
        say(f":loud_sound: Volume : {int(vol * 100)} %")
    elif arg in ("down", "bas", "-"):
        vol = player.volume_down(actor=actor)
        say(f":sound: Volume : {int(vol * 100)} %")
    elif arg.rstrip("%").isdigit():
        requested = int(arg.rstrip("%")) / 100
        player.set_volume(requested, actor)
        actual = player.get_volume()
        if actual < requested:
            say(f":loud_sound: Volume réglé à {int(actual * 100)} % (max autorisé — valeur demandée : {int(requested * 100)} %).")
        else:
            say(f":loud_sound: Volume réglé à {int(actual * 100)} %.")
    else:
        say("Usage : `/boomer_v3 volume up|down|<0-100>`")


def _pick_extension(file_info: dict, content: bytes) -> str | None:
    """Trust the bytes, never the name: Slack labels unrecognised uploads 'binary',
    and a QuickTime container happily calls itself .mp3."""
    sniffed = sniff_extension(content)
    if sniffed:
        return sniffed
    # Unknown header: only keep a claimed extension if the mixer can really decode it
    named = os.path.splitext(file_info.get("name", ""))[1].lower()
    claimed = f".{file_info.get('filetype', '').lower()}"
    for candidate in (named, claimed):
        if candidate in SUPPORTED_EXTENSIONS and _is_decodable(content, candidate):
            return candidate
    return None


def _is_decodable(content: bytes, ext: str) -> bool:
    with tempfile.NamedTemporaryFile(suffix=ext) as probe:
        probe.write(content)
        probe.flush()
        return can_decode(probe.name)


def _convert_to_mp3(content: bytes) -> bytes | None:
    """Re-encode a container pygame cannot read (QuickTime, MP4…), if ffmpeg is around."""
    if shutil.which("ffmpeg") is None:
        return None
    with tempfile.TemporaryDirectory() as workdir:
        src = os.path.join(workdir, "upload")
        dst = os.path.join(workdir, "converted.mp3")
        with open(src, "wb") as f:
            f.write(content)
        try:
            result = subprocess.run(
                ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", src,
                 "-vn", "-c:a", "libmp3lame", "-q:a", "4", dst],
                capture_output=True, timeout=120,
            )
        except (subprocess.SubprocessError, OSError):
            logger.exception("ffmpeg failed on the uploaded file")
            return None
        if result.returncode != 0 or not os.path.exists(dst):
            logger.warning("ffmpeg rejected the upload: %s", result.stderr[:300].decode(errors="replace"))
            return None
        with open(dst, "rb") as f:
            return f.read()


def _unsupported_upload_message(content: bytes) -> str:
    accepted = ", ".join(f"`{e}`" for e in sorted(SUPPORTED_EXTENSIONS))
    if content[4:8] == b"ftyp":
        return (
            ":x: Ce fichier est un conteneur MP4/QuickTime, pas un vrai MP3 : "
            "son extension est trompeuse et pygame ne sait pas le lire.\n"
            "Convertis-le avant de l'envoyer (`ffmpeg -i fichier.mp3 -c:a libmp3lame converti.mp3`), "
            "ou installe `ffmpeg` sur le Pi pour que Boomer le fasse tout seul."
        )
    return f":x: Format audio non reconnu. Formats acceptés : {accepted}"


def _download_and_save(say, player: SoundPlayer, name: str, file_info: dict, overwrite: bool):
    if name.startswith("__overwrite__"):
        name = name[len("__overwrite__"):]
        overwrite = True

    url = file_info.get("url_private_download") or file_info.get("url_private")
    if not url:
        say(":x: Impossible de récupérer l'URL du fichier.")
        return

    token = os.environ["SLACK_BOT_TOKEN"]
    try:
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        say(f":x: Échec du téléchargement : {e}")
        return

    content = resp.content
    ext = _pick_extension(file_info, content)
    if ext is None:
        converted = _convert_to_mp3(content)
        if converted is None:
            say(_unsupported_upload_message(content))
            return
        say(":arrows_counterclockwise: Format exotique converti en MP3.")
        content, ext = converted, ".mp3"

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
        f.write(content)
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
        "• `/boomer_v3 play <nom>[+<nom>…] [effets]` — jouer un son, ou plusieurs à la suite\n"
        "• `/boomer_v3 random [effets]` — jouer un son au hasard\n"
        "• `/boomer_v3 stats [jour|semaine|mois|tout] [moi]` — classement des sons et des personnes\n"
        f"    _{_EFFECTS_HELP}_\n"
        "• `/boomer_v3 stop` — arrêter la lecture en cours\n"
        "• `/boomer_v3 vol up|down|<0-100>` — régler le volume\n"
        "• `/boomer_v3 list` — lister les sons disponibles\n"
        "• `/boomer_v3 add <nom>` — ajouter un son (puis envoyer le fichier)\n"
        "• `/boomer_v3 rename <ancien> <nouveau>` — renommer un son "
        "(guillemets si le nom contient des espaces)\n"
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
