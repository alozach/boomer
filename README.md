# Boomer

Soundboard pour Raspberry Pi, pilotable depuis Slack et depuis un clavier MIDI.

Boomer tourne en service systemd sur le Pi, écoute les touches d'un clavier MIDI branché en USB
et expose une commande Slack `/boomer_v3` pour jouer des sons, en ajouter, faire parler une voix
de synthèse ou planifier des lectures automatiques.

## Fonctionnalités

- **Lecture de sons** — bibliothèque de fichiers audio (`.mp3`, `.wav`, `.ogg`, `.flac`, `.aiff`)
  dans [sounds/](sounds/), jouée via pygame.
- **Clavier MIDI** — chaque touche peut être associée à un son ou à une action volume ±,
  avec un bip de retour sur les touches de volume.
- **Bot Slack** — commande slash `/boomer_v3` + panneaux interactifs à boutons
  (contrôle du volume, un bouton par son).
- **Ajout de sons depuis Slack** — `add <nom>` puis dépôt du fichier dans le canal.
- **Synthèse vocale** — [edge-tts](https://github.com/rany2/edge-tts), voix neuronales,
  26 langues, vitesse réglable.
- **Planification** — jouer un son à une heure donnée, tous les jours ou sur des jours choisis.
- **Recherche approximative** — les noms de sons sont résolus en *fuzzy matching*,
  `/boomer_v3 play maarc` trouve `maaaarc`.

## Architecture

| Fichier | Rôle |
| --- | --- |
| [main.py](main.py) | Point d'entrée : instancie les composants, lance le thread MIDI et le bot Slack |
| [boomer/sound_player.py](boomer/sound_player.py) | Bibliothèque de sons, lecture, volume/mute, mappings MIDI, persistance de `config.json` |
| [boomer/midi_listener.py](boomer/midi_listener.py) | Boucle d'écoute MIDI (mido), dispatch note → son ou action |
| [boomer/slack_bot.py](boomer/slack_bot.py) | Commandes slash, panneaux interactifs, upload de fichiers |
| [boomer/tts_engine.py](boomer/tts_engine.py) | Synthèse vocale edge-tts (nécessite une connexion internet) |
| [boomer/scheduler.py](boomer/scheduler.py) | Planifications persistées dans `schedules.json` |

L'état persistant vit dans deux fichiers JSON à la racine : `config.json`
(mappings MIDI + références des panneaux Slack) et `schedules.json` (planifications).
Ces fichiers sont écrits par le bot au fil de l'eau et ne sont donc pas versionnés :
seul le modèle `config.example.json` l'est, copié vers `config.json` à l'installation.

## Installation

Sur le Raspberry Pi :

```bash
git clone <url-du-repo> boomer
cd boomer
make install
```

[install.sh](install.sh) installe les dépendances système (ALSA, portmidi, SDL2), crée le
virtualenv `.venv`, installe les dépendances Python, ajoute l'utilisateur au groupe `audio`,
copie `.env.example` vers `.env` et crée le service systemd `boomer` activé au boot.

Renseigner ensuite les tokens Slack dans `.env` :

```
SLACK_BOT_TOKEN=xoxb-...
SLACK_SIGNING_SECRET=...
SLACK_APP_TOKEN=xapp-...
```

Puis `make start`.

### Configuration de l'app Slack

1. Créer une app sur [api.slack.com/apps](https://api.slack.com/apps).
2. **Socket Mode** : activer et générer un app token (`xapp-...`) avec le scope `connections:write`.
   C'est le mode recommandé : pas besoin d'IP publique ni de reverse proxy pour le Pi.
   Sans `SLACK_APP_TOKEN`, Boomer démarre en mode HTTP sur `PORT` (3000 par défaut).
3. **Slash command** : `/boomer_v3`.
4. **Bot token scopes** : `chat:write`, `commands`, `files:read`.
5. **Event subscriptions** : `message.channels` (et `message.groups` pour les canaux privés),
   nécessaire pour récupérer les fichiers audio envoyés après un `add`.
6. Inviter le bot dans le canal voulu.

## Utilisation

| Commande | Effet |
| --- | --- |
| `/boomer_v3 play <nom>` | Jouer un son |
| `/boomer_v3 stop` | Arrêter la lecture en cours |
| `/boomer_v3 list` | Lister les sons disponibles |
| `/boomer_v3 sounds` | Panneau interactif, un bouton par son |
| `/boomer_v3 panel` | Panneau de contrôle (stop, mute, volume) |
| `/boomer_v3 add <nom>` | Ajouter un son (envoyer ensuite le fichier dans le canal) |
| `/boomer_v3 rename <ancien> <nouveau>` | Renommer un son (met à jour les mappings MIDI) |
| `/boomer_v3 delete <nom>` | Supprimer un son |
| `/boomer_v3 map <nom>` | Assigner un son à une touche MIDI (appuyer sur la touche, 60 s) |
| `/boomer_v3 vol up\|down\|<0-100>` | Régler le volume |
| `/boomer_v3 mute` / `unmute` | Couper / rétablir le son |
| `/boomer_v3 tts <texte> [lang]` | Synthèse vocale (défaut : `fr`) |
| `/boomer_v3 tts rate <50-400>` | Vitesse de la synthèse, en mots/min |
| `/boomer_v3 tts list` | Lister les langues disponibles |
| `/boomer_v3 schedule <HH:MM> [jours] <son>` | Planifier un son |
| `/boomer_v3 schedule list` / `cancel <id>` | Gérer les planifications |
| `/boomer_v3 help` | Afficher l'aide |

## Exploitation

```bash
make start      # démarrer le service
make stop       # arrêter
make restart    # redémarrer
make status     # état systemd
make logs       # suivre les logs (journalctl -f)
make update     # git pull + pip install + restart
make run        # lancer en avant-plan sans systemd (debug)
```

## Notes

- Le volume par défaut est bas (2 %) : les enceintes visées saturent vite. Il monte par pas de 2 %.
- La synthèse vocale passe par les serveurs Microsoft Edge : sans réseau, `tts` échoue
  (l'erreur est loguée, le reste continue de fonctionner).
- Sans clavier MIDI branché au démarrage, l'écoute MIDI est simplement désactivée
  (un avertissement est logué) ; le bot Slack reste pleinement fonctionnel.
