"""Разовая утилита: получить token.json через OAuth2 на локальном ПК.

Запуск (на машине с браузером, где лежит oauth_client.json):
    python get_token.py

Затем загрузить token.json на сервер рядом с bot.py.
На сервере эта утилита не нужна — токен обновляется автоматически.
"""

import os

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

CLIENT_FILE = os.getenv("OAUTH_CLIENT_FILE", "oauth_client.json")
TOKEN_FILE = os.getenv("TOKEN_FILE", "token.json")

if not os.path.exists(CLIENT_FILE):
    raise SystemExit(f"Файл {CLIENT_FILE} не найден. Скачайте OAuth client (Desktop app) из Google Cloud Console.")

flow = InstalledAppFlow.from_client_secrets_file(CLIENT_FILE, SCOPES)
creds = flow.run_local_server(port=0)

with open(TOKEN_FILE, "w", encoding="utf-8") as token_file:
    token_file.write(creds.to_json())

print(f"✅ Файл {TOKEN_FILE} успешно создан! Загрузите его на сервер в папку /root/Prachka/")