# Bot powitalny Discord

Bot wysyła wiadomość, gdy ktoś dołączy do serwera lub go opuści.

## Uruchomienie

1. Zainstaluj Python 3.10 lub nowszy.
2. Utwórz aplikację i bota w [Discord Developer Portal](https://discord.com/developers/applications).
3. Włącz **Server Members Intent** w zakładce `Bot`.
4. Zaproś bota na serwer z uprawnieniami `View Channel` i `Send Messages`.
5. W terminalu projektu uruchom:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

6. Otwórz `.env` i wpisz token bota w `DISCORD_TOKEN`.
7. Ustaw `WELCOME_CHANNEL_ID` na ID kanału powitalnego, a `GOODBYE_CHANNEL_ID` na ID kanału pożegnalnego. Wartość `0` oznacza użycie kanału systemowego serwera.
8. Włącz bota:

```powershell
python bot.py
```

Aby skopiować ID kanału, włącz tryb deweloperski Discorda, kliknij kanał prawym przyciskiem i wybierz **Kopiuj ID kanału**.

Nie udostępniaj tokena bota. Plik `.env` jest wykluczony z repozytorium przez `.gitignore`.
