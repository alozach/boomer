SERVICE = boomer
VENV    = .venv/bin/python

.PHONY: start stop restart status logs update install

start:
	sudo systemctl start $(SERVICE)

stop:
	sudo systemctl stop $(SERVICE)

restart:
	sudo systemctl restart $(SERVICE)

status:
	sudo systemctl status $(SERVICE)

logs:
	sudo journalctl -u $(SERVICE) -f

# Met à jour le code et redémarre
update:
	git pull
	.venv/bin/pip install -r requirements.txt -q
	sudo systemctl restart $(SERVICE)
	@echo "Mis à jour et redémarré."

# Lance directement sans systemd (debug)
run:
	$(VENV) main.py

install:
	bash install.sh
