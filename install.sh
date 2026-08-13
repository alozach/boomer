#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
SERVICE_NAME="boomer"
CURRENT_USER="$(whoami)"

echo "==> Installation de Boomer dans $PROJECT_DIR"

# Dépendances système
echo "==> Installation des paquets système..."
sudo apt-get update -qq
sudo apt-get install -y \
    espeak \
    python3-venv \
    python3-pip \
    libasound2-dev \
    libportmidi-dev \
    libsdl2-mixer-2.0-0 \
    libsdl2-2.0-0 \
    ffmpeg

# Accès audio et MIDI
if ! groups "$CURRENT_USER" | grep -q '\baudio\b'; then
    echo "==> Ajout de $CURRENT_USER au groupe audio..."
    sudo usermod -aG audio "$CURRENT_USER"
    echo "    (prendra effet à la prochaine connexion)"
fi

# Environnement Python
echo "==> Création du virtualenv..."
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$PROJECT_DIR/requirements.txt" -q
echo "    Dépendances Python installées."

# Fichier .env
if [ ! -f "$PROJECT_DIR/.env" ]; then
    cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
    echo ""
    echo "  ATTENTION : .env créé depuis .env.example."
    echo "  Renseigne tes tokens Slack dans $PROJECT_DIR/.env"
    echo "  avant de démarrer le service."
    echo ""
fi

# État persistant
if [ ! -f "$PROJECT_DIR/config.json" ]; then
    cp "$PROJECT_DIR/config.example.json" "$PROJECT_DIR/config.json"
    echo "==> config.json créé depuis config.example.json."
fi

# Service systemd
echo "==> Création du service systemd $SERVICE_NAME..."
sudo tee "/etc/systemd/system/$SERVICE_NAME.service" > /dev/null <<EOF
[Unit]
Description=Boomer RPi Soundboard
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=$PROJECT_DIR/.env
ExecStart=$VENV_DIR/bin/python $PROJECT_DIR/main.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

echo ""
echo "Installation terminée."
echo ""
echo "Prochaines étapes :"
echo "  1. Édite $PROJECT_DIR/.env avec tes tokens Slack"
echo "  2. sudo systemctl start $SERVICE_NAME"
echo ""
echo "Commandes utiles (depuis ce dossier) :"
echo "  make start    — démarrer"
echo "  make stop     — arrêter"
echo "  make logs     — suivre les logs"
echo "  make update   — mettre à jour et redémarrer"
