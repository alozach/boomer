# Boomer

Soundboard pour Raspberry Pi, pilotable depuis Slack et depuis un clavier MIDI.

Boomer tourne en service systemd sur le Pi, écoute les touches d'un clavier MIDI branché en USB
et expose une commande Slack `/boomer_v3` pour jouer des sons, en ajouter, faire parler une voix
de synthèse ou planifier des lectures automatiques.

## Fonctionnalités

- **Lecture de sons** — bibliothèque de fichiers audio (`.mp3`, `.wav`, `.ogg`, `.flac`, `.aiff`)
  dans [sounds/](sounds/), jouée via pygame.
- **Enchaînements et effets** — `play a+b+c` joue les sons à la suite, `--reverse`,
  `--speed`, `--nightcore` et consorts les déforment à la volée.
- **Clavier MIDI** — chaque touche peut être associée à un son ou à une action volume ±,
  avec un bip de retour sur les touches de volume.
- **Bot Slack** — commande slash `/boomer_v3` + panneaux interactifs à boutons
  (contrôle du volume, un bouton par son), et un onglet *Accueil* permanent.
- **Ajout de sons depuis Slack** — `add <nom>` puis dépôt du fichier dans le canal ; le format
  est reconnu d'après le contenu, et converti en MP3 si pygame ne sait pas le lire.
- **Synthèse vocale** — [edge-tts](https://github.com/rany2/edge-tts), voix neuronales,
  26 langues, vitesse réglable, déclenchable sur n'importe quel message via un raccourci.
- **Planification** — jouer un son à une heure donnée, tous les jours ou sur des jours choisis.
- **Statistiques** — qui a joué quoi, combien de fois, et récap automatique le vendredi.
- **Recherche approximative** — les noms de sons sont résolus en *fuzzy matching*,
  `/boomer_v3 play maarc` trouve `maaaarc`.

## Architecture

| Fichier | Rôle |
| --- | --- |
| [main.py](main.py) | Point d'entrée : instancie les composants, lance le thread MIDI et le bot Slack |
| [boomer/sound_player.py](boomer/sound_player.py) | Bibliothèque de sons, lecture, enchaînements, volume/mute, mappings MIDI, persistance de `config.json` |
| [boomer/audio_effects.py](boomer/audio_effects.py) | Parsing des flags d'effets et transformation des échantillons (numpy) |
| [boomer/midi_listener.py](boomer/midi_listener.py) | Boucle d'écoute MIDI (mido), dispatch note → son ou action |
| [boomer/slack_bot.py](boomer/slack_bot.py) | Commandes slash, panneaux interactifs, upload de fichiers |
| [boomer/tts_engine.py](boomer/tts_engine.py) | Synthèse vocale edge-tts (nécessite une connexion internet) |
| [boomer/scheduler.py](boomer/scheduler.py) | Planifications persistées dans `schedules.json` |
| [boomer/stats.py](boomer/stats.py) | Historique des lectures persisté dans `stats.json` |

L'état persistant vit dans trois fichiers JSON à la racine : `config.json`
(mappings MIDI + références des panneaux Slack), `schedules.json` (planifications)
et `stats.json` (historique des lectures).
Ces fichiers sont écrits par le bot au fil de l'eau et ne sont donc pas versionnés :
seul le modèle `config.example.json` l'est, copié vers `config.json` à l'installation.

## Installation

Sur le Raspberry Pi :

```bash
git clone <url-du-repo> boomer
cd boomer
make install
```

[install.sh](install.sh) installe les dépendances système (ALSA, portmidi, SDL2, ffmpeg), crée le
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
   nécessaire pour récupérer les fichiers audio envoyés après un `add`, et `app_home_opened`
   pour l'onglet *Accueil*.
6. **App Home** : activer l'onglet *Accueil* (*Home Tab*) dans les *App Features*.
7. **Shortcut** : créer un raccourci *sur message* de callback ID `boomer_speak`
   (nom suggéré : « Lire à voix haute »).
8. Inviter le bot dans le canal voulu.

## Utilisation

| Commande | Effet |
| --- | --- |
| `/boomer_v3 play <nom>[+<nom>…] [effets]` | Jouer un son, ou plusieurs à la suite |
| `/boomer_v3 random [effets]` | Jouer un son au hasard |
| `/boomer_v3 stats [période] [moi]` | Classement des sons et des personnes |
| `/boomer_v3 stop` | Arrêter la lecture en cours |
| `/boomer_v3 list` | Lister les sons disponibles et leur nombre de lectures |
| `/boomer_v3 sounds` | Panneau interactif, un bouton par son |
| `/boomer_v3 panel` | Panneau de contrôle (stop, mute, volume, aléatoire) |
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

### Effets

`[effets]` désigne partout la même liste de flags, cumulables, acceptés aussi bien par
`play` que par `random` :

| Flag | Effet |
| --- | --- |
| `--reverse` | Joue le son à l'envers |
| `--speed <0.25-4>` | Rééchantillonne : la vitesse et la hauteur changent ensemble |
| `--nightcore` / `--chipmunk` | Accéléré (×1.35 / ×1.8) |
| `--vaporwave` / `--slow` / `--deep` | Ralenti (×0.75 / ×0.7 / ×0.6) |

Sur un enchaînement, les effets s'appliquent à tous les sons :
`/boomer_v3 play bonk+atchoum --nightcore`.

### Statistiques

Chaque lecture est enregistrée avec son déclencheur : la personne qui a tapé la commande ou
cliqué le bouton, le clavier MIDI ou les planifications.

Le clavier étant de loin le premier usage, il domine sinon tous les classements : le podium
des personnes ne retient donc que les déclencheurs Slack, et le clavier et les
planifications sont comptés à part, sur leur propre ligne. Les compteurs par son, eux,
additionnent bien toutes les origines.

`/boomer_v3 stats [jour|semaine|mois|tout] [moi]` affiche les sons les plus joués, avec leur
nombre de lectures, et les personnes qui les déclenchent, avec le son que chacune joue le plus.
`/boomer_v3 list` affiche le compteur de chaque son.

Le vendredi à 17 h, Boomer poste de lui-même le récap de la semaine dans le canal du dernier
panneau enregistré.

### Lire un message à voix haute

Le menu *Plus d'actions* (⋮) de n'importe quel message propose « Lire à voix haute » : le
texte du message part dans la synthèse vocale, et Boomer annonce dans le canal qui a fait
lire quoi.

Le raccourci fonctionne dans tous les canaux du workspace, y compris ceux où le bot n'est
pas invité — mais il ne peut alors pas y poster : la confirmation est dans ce cas visible
de la seule personne qui l'a déclenché.

Le markup Slack (mentions, liens, emojis, citations) est nettoyé avant lecture, et le texte
est tronqué à 300 caractères.

### Onglet Accueil

L'onglet *Accueil* de l'app affiche en permanence le panneau de contrôle (stop, mute,
volume, aléatoire), puis les boutons de tous les sons. Il se met à jour à chaque ouverture et après
chaque clic, sans avoir à reposter un panneau dans un canal.

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
- Les changements de volume au clavier MIDI sont annoncés dans le canal une fois la rafale
  terminée (1,5 s sans nouvel appui) : une montée de 2 à 20 % donne un message, pas neuf.
  L'annonce a besoin d'un canal connu, c'est-à-dire d'un `/boomer_v3 panel` ou `sounds`
  posté au moins une fois.
- Les effets sont calculés en mémoire avec numpy : les fichiers d'origine ne sont jamais modifiés.
- Toute nouvelle lecture (bouton, commande, touche MIDI) interrompt l'enchaînement en cours.
- `stats.json` est écrit par lots (10 s) pour épargner la carte SD, et à l'arrêt du service.
- La synthèse vocale passe par les serveurs Microsoft Edge : sans réseau, `tts` échoue
  (l'erreur est loguée, le reste continue de fonctionner).
- L'extension d'un fichier envoyé n'est jamais prise au mot : le format est déterminé par
  l'en-tête du fichier, puis vérifié en le faisant charger par le mixer. Un enregistrement
  iPhone ou macOS nommé `.mp3` mais réellement encapsulé en QuickTime est converti par
  `ffmpeg` ; sans `ffmpeg`, l'ajout est refusé avec un message explicite plutôt que de créer
  un fichier injouable.
- Les noms de sons peuvent contenir des espaces. Pour `rename`, la partie la plus longue qui
  correspond à un son existant est prise comme ancien nom ; en cas de doute, entourer les deux
  noms de guillemets : `/boomer_v3 rename "mon son" "nouveau nom"`.
- Sans clavier MIDI branché au démarrage, l'écoute MIDI est simplement désactivée
  (un avertissement est logué) ; le bot Slack reste pleinement fonctionnel.
- Les appels à l'API Slack (rafraîchissement des panneaux, notifications) partent en
  arrière-plan : ni le son ni l'accusé de réception n'attendent le réseau.
- Toute requête Slack traitée en plus d'une seconde, ou livrée avec plus d'une seconde de
  retard, est signalée dans les logs (`make logs`) avec les deux durées séparées, ce qui
  distingue une lenteur de Boomer d'une lenteur de livraison.
