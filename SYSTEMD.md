# Uruchamianie bota jako usługi systemd

Konfiguracja zakłada Linuksa z systemd oraz instalację bota w `/opt/nht-bot`.
Plik `.env` musi zawierać wymagane zmienne opisane w `.env.example`.

## Instalacja

```sh
sudo useradd --system --create-home --home-dir /opt/nht-bot --shell /usr/sbin/nologin nht-bot
sudo mkdir -p /opt/nht-bot
```

Skopiuj repozytorium do `/opt/nht-bot`, a następnie jako użytkownik `nht-bot`
ustaw właściciela plików, utwórz środowisko i zainstaluj zależności:

```sh
sudo chown -R nht-bot:nht-bot /opt/nht-bot
sudo -u nht-bot python3 -m venv /opt/nht-bot/.venv
sudo -u nht-bot /opt/nht-bot/.venv/bin/pip install -r /opt/nht-bot/requirements.txt
```

Utwórz `/opt/nht-bot/.env` na podstawie `.env.example` i ustaw jego właściciela
oraz ograniczone uprawnienia:

```sh
sudo chown nht-bot:nht-bot /opt/nht-bot/.env
sudo chmod 600 /opt/nht-bot/.env
```

Zainstaluj usługę i uruchom ją:

```sh
sudo cp /opt/nht-bot/systemd/nht-bot.service /etc/systemd/system/nht-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now nht-bot.service
```

Przy innej ścieżce instalacji zmień `WorkingDirectory`, `EnvironmentFile`
i `ExecStart` w pliku usługi przed skopiowaniem.

## Zarządzanie

Z terminala:

```sh
sudo systemctl status nht-bot.service
sudo systemctl restart nht-bot.service
sudo systemctl stop nht-bot.service
sudo journalctl -u nht-bot.service -f
```

Autoryzowany użytkownik Discorda (`AUTHORIZED_USER_ID`) może też użyć:

```text
!systemctl status
!systemctl start
!systemctl stop
!systemctl restart
```

Komenda `!update` pobiera zmiany z Git i zleca restart przez systemd. Dawne
komendy `!reload` i `!stop` zostały usunięte — cyklem życia procesu zarządza
teraz usługa.

Komendy Discord wymagają `sudo` bez pytania o hasło. Utwórz plik przez
`sudo visudo -f /etc/sudoers.d/nht-bot-systemctl`, zastępując `nht-bot`
użytkownikiem ustawionym w `User=` w `/etc/systemd/system/nht-bot.service`
(domyślnie `nht-bot`):

```sudoers
nht-bot ALL=(root) NOPASSWD: /usr/bin/systemctl status nht-bot.service --no-pager, /usr/bin/systemctl --no-block start nht-bot.service, /usr/bin/systemctl --no-block stop nht-bot.service, /usr/bin/systemctl --no-block restart nht-bot.service
```

Zakres uprawnień sudo jest ograniczony do tych czterech operacji na tej jednej
usłudze. `--no-block` pozwala komendom `stop` i `restart` wrócić bez oczekiwania
na zakończenie procesu bota.

Jeśli Discord pokazuje `sudo: interactive authentication is required`, bot
nie ma dopasowanej reguły `NOPASSWD`. Zapisz regułę z właściwym użytkownikiem,
sprawdź jej składnię i przetestuj bezpieczną komendę statusu:

```sh
sudo chmod 0440 /etc/sudoers.d/nht-bot-systemctl
sudo visudo -cf /etc/sudoers.d/nht-bot-systemctl
sudo -u nht-bot sudo -n /usr/bin/systemctl status nht-bot.service --no-pager
```

W ostatniej komendzie użyj użytkownika z `User=` w usłudze. Jeśli test nadal
nie przechodzi, sprawdź, czy plik sudoers zawiera tę samą nazwę użytkownika
i dokładne polecenia z bloku powyżej.
