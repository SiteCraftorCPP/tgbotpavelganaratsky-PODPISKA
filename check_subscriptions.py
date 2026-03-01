#!/usr/bin/env python3
"""
Проверка подписок и привязки карт. Запуск на сервере:
  cd /opt/tgbotpavelganaratsky-podpiska && ./venv/bin/python check_subscriptions.py
"""
import asyncio
import os
import time
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
import aiosqlite

load_dotenv(Path(__file__).resolve().parent / ".env")
DB_NAME = Path(__file__).resolve().parent / "bot_database.db"


async def main():
    now = time.time()
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("""
            SELECT id, subscription_active, subscription_end_date, 
                   CASE WHEN card_token IS NOT NULL AND card_token != '' THEN 1 ELSE 0 END
            FROM users
            ORDER BY subscription_end_date DESC
            LIMIT 30
        """) as cur:
            rows = await cur.fetchall()
    print("user_id      | active | sub_until           | card_saved")
    print("-" * 60)
    for (uid, active, end_ts, has_card) in rows:
        end_str = datetime.utcfromtimestamp(end_ts).strftime("%Y-%m-%d %H:%M UTC") if end_ts else "-"
        print(f"{uid:<12} | {active}      | {end_str} | {'YES' if has_card else 'NO'}")
    print()
    print("card_saved=YES -> карта привязана, будут автосписания.")
    print("sub_until — до этой даты подписка; потом бот попытается списать с карты.")


if __name__ == "__main__":
    asyncio.run(main())
