## Telegram бот подписки

Проект бота на `aiogram 3` для оформления подписки и доступа в закрытый канал.

### Локальный запуск

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python bot.py
```

### Конфигурация

Создайте файл `.env` (не коммитится, см. `.gitignore`):

```env
BOT_TOKEN=...
ADMIN_IDS=6933111964,783321437,6804220900
CHANNEL_ID=-1003721182699
MANAGER_LINK=tg://user?id=6933111964
```

