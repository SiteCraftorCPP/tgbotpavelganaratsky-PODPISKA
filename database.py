import aiosqlite
import os

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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                agreed_to_terms BOOLEAN DEFAULT 0,
                subscription_active BOOLEAN DEFAULT 0,
                join_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
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
        
        await db.commit()

async def add_user(user_id, username, full_name):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO users (id, username, full_name) VALUES (?, ?, ?)", (user_id, username, full_name))
        await db.commit()

async def set_agreed(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET agreed_to_terms = 1 WHERE id = ?", (user_id,))
        await db.commit()

async def set_subscription(user_id, status=True):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET subscription_active = ? WHERE id = ?", (1 if status else 0, user_id))
        await db.commit()

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
