import aiosqlite
import os
import time
from typing import Optional

DB_NAME = "bot_database.db"

# Только вступительный текст (ссылки добавляются в коде — захардкожены)
WELCOME_INTRO_DEFAULT = """Добро пожаловать в наш бот!

Пожалуйста, ознакомьтесь с правилами ниже.

Нажмите кнопку ниже, чтобы продолжить."""

NEW_CANCEL_TEXT = """Для отмены подписки:
1. Перейдите в настройки бота.
2. Нажмите 'Отменить'.
3. Если возникли вопросы, напишите в поддержку."""

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        # Обновляем таблицу users: добавляем поля для подписки
        # SQLite не поддерживает ADD COLUMN IF NOT EXISTS в старых версиях,
        # поэтому делаем через try/except или проверку PRAGMA table_info,
        # но для простоты здесь добавим колонки, если их нет (игнорируя ошибки дубликатов)
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                agreed_to_terms BOOLEAN DEFAULT 0,
                subscription_active BOOLEAN DEFAULT 0,
                join_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                
                bepaid_uid TEXT,
                card_token TEXT,
                subscription_end_date TIMESTAMP,
                last_payment_date TIMESTAMP,
                email TEXT
            )
        """)
        
        # Миграции для существующих баз данных (безопасное добавление колонок)
        try:
            await db.execute("ALTER TABLE users ADD COLUMN bepaid_uid TEXT")
        except: pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN card_token TEXT")
        except: pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN subscription_end_date TIMESTAMP")
        except: pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN last_payment_date TIMESTAMP")
        except: pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN email TEXT")
        except: pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN grace_until_ts REAL")
        except: pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN last_payment_fail_ts REAL")
        except: pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN last_payment_fail_notice_ts REAL")
        except: pass


        await db.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                id INTEGER PRIMARY KEY
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        
        # При первом запуске — только вступление; ссылки всегда подставляются в коде
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('welcome_text', ?)", (WELCOME_INTRO_DEFAULT,))
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('cancel_text', ?)", (NEW_CANCEL_TEXT,))
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('payment_success_text', ?)", ("✅ Оплата прошла успешно!\n\nНажмите кнопку ниже, чтобы вступить в канал.",))
        
        # Настройка цены и периода по умолчанию
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('subscription_price', '30')") # BYN
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('subscription_days', '30')")
        # Если раньше была цена 10 BYN и не меняли вручную — обновим до 30
        await db.execute("UPDATE settings SET value='30' WHERE key='subscription_price' AND value='10'")
        
        await db.commit()

async def add_user(user_id, username, full_name):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO users (id, username, full_name) VALUES (?, ?, ?)", (user_id, username, full_name))
        await db.commit()

async def set_agreed(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET agreed_to_terms = 1 WHERE id = ?", (user_id,))
        await db.commit()

async def set_subscription(user_id, status=True, end_date=None, card_token=None, email=None):
    async with aiosqlite.connect(DB_NAME) as db:
        query = "UPDATE users SET subscription_active = ?"
        params = [1 if status else 0]
        
        if end_date:
            query += ", subscription_end_date = ?"
            params.append(end_date)
        
        # Если передан card_token=None, не обновляем его (чтобы не затереть).
        # Если передан "", значит хотим стереть (например, при отмене).
        if card_token is not None:
             query += ", card_token = ?"
             params.append(card_token)

        if email is not None:
            query += ", email = ?"
            params.append(email)
            
        query += " WHERE id = ?"
        params.append(user_id)
        
        await db.execute(query, tuple(params))
        await db.commit()


async def set_grace_period(
    user_id: int,
    grace_until_ts: float,
    fail_ts: float,
    notice_ts: Optional[float],
):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE users "
            "SET grace_until_ts = ?, last_payment_fail_ts = ?, last_payment_fail_notice_ts = ? "
            "WHERE id = ?",
            (grace_until_ts, fail_ts, notice_ts, user_id),
        )
        await db.commit()


async def clear_grace_period(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE users "
            "SET grace_until_ts = NULL, last_payment_fail_ts = NULL, last_payment_fail_notice_ts = NULL "
            "WHERE id = ?",
            (user_id,),
        )
        await db.commit()


async def update_grace_notice_ts(user_id: int, notice_ts: float):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE users SET last_payment_fail_notice_ts = ? WHERE id = ?",
            (notice_ts, user_id),
        )
        await db.commit()

async def get_all_active_users():
    """Получить всех пользователей с активной подпиской"""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id FROM users WHERE subscription_active = 1") as cursor:
            return [row[0] for row in await cursor.fetchall()]

async def get_user_subscription(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT subscription_active, subscription_end_date, card_token FROM users WHERE id = ?", (user_id,)) as cursor:
            return await cursor.fetchone()

async def get_users_due_payment():
    """Пользователи с истёкшей подпиской и привязанной картой (пробуем автосписание)."""
    async with aiosqlite.connect(DB_NAME) as db:
        now = time.time()
        async with db.execute("""
            SELECT id, card_token, email, grace_until_ts, last_payment_fail_notice_ts
            FROM users 
            WHERE subscription_active = 1 
              AND card_token IS NOT NULL 
              AND card_token != ''
              AND subscription_end_date <= ?
              AND (grace_until_ts IS NULL OR grace_until_ts <= ?)
        """, (now, now)) as cursor:
            return await cursor.fetchall()


async def get_users_expired_no_card_start_grace():
    """Истёкшая подписка без карты, грейс ещё не запускали — надо запустить грейс и уведомить."""
    async with aiosqlite.connect(DB_NAME) as db:
        now = time.time()
        async with db.execute(
            """
            SELECT id, email
            FROM users
            WHERE subscription_active = 1
              AND subscription_end_date <= ?
              AND (card_token IS NULL OR card_token = '')
              AND grace_until_ts IS NULL
            """,
            (now,),
        ) as cursor:
            return await cursor.fetchall()


async def get_users_expired_no_card_to_kick():
    """Истёкшая подписка без карты, грейс закончился — пора отключать доступ (кик)."""
    async with aiosqlite.connect(DB_NAME) as db:
        now = time.time()
        async with db.execute(
            """
            SELECT id
            FROM users
            WHERE subscription_active = 1
              AND subscription_end_date <= ?
              AND (card_token IS NULL OR card_token = '')
              AND grace_until_ts IS NOT NULL
              AND grace_until_ts <= ?
            """,
            (now, now),
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]


async def get_users_in_grace_to_notify():
    """
    Пользователи, у которых подписка истекла, но действует грейс-период.
    Уведомляем максимум раз в 24 часа.
    """
    async with aiosqlite.connect(DB_NAME) as db:
        now = time.time()
        day_ago = now - 86400
        async with db.execute(
            """
            SELECT id, email, grace_until_ts, last_payment_fail_notice_ts
            FROM users
            WHERE subscription_active = 1
              AND subscription_end_date <= ?
              AND grace_until_ts IS NOT NULL
              AND grace_until_ts > ?
              AND (last_payment_fail_notice_ts IS NULL OR last_payment_fail_notice_ts <= ?)
            """,
            (now, now, day_ago),
        ) as cursor:
            return await cursor.fetchall()

async def get_users():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id FROM users") as cursor:
            return [row[0] for row in await cursor.fetchall()]

async def add_admin(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO admins (id) VALUES (?)", (user_id,))
        await db.commit()

async def get_admins():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id FROM admins") as cursor:
            return [row[0] for row in await cursor.fetchall()]

async def get_setting(key):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cursor:
            result = await cursor.fetchone()
            return result[0] if result else None

async def set_setting(key, value):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        await db.commit()
