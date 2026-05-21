import aiosqlite
import os
import time
from typing import List, Optional, Tuple

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
        try:
            await db.execute("ALTER TABLE users ADD COLUMN start_payload TEXT")
        except: pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN utm_source TEXT")
        except: pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN utm_medium TEXT")
        except: pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN utm_campaign TEXT")
        except: pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN tapped_buy BOOLEAN DEFAULT 0")
        except: pass

        await db.execute("""
            CREATE TABLE IF NOT EXISTS campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_payload TEXT NOT NULL UNIQUE,
                created_at REAL
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS payment_success (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                bepaid_uid TEXT,
                tracking_id TEXT,
                amount_cents INTEGER,
                currency TEXT,
                paid_at REAL NOT NULL,
                recurring INTEGER NOT NULL DEFAULT 0,
                start_payload TEXT,
                utm_source TEXT,
                utm_medium TEXT,
                utm_campaign TEXT
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_payment_payload ON payment_success (start_payload)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_payment_user ON payment_success (user_id)
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
        
        # Настройка цены и периода подписки
        # По задаче: 35 BYN и срок 1 месяц (30 дней).
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('subscription_price', '35')") # BYN
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('subscription_days', '30')")
        # Применяем актуальные значения для уже существующей БД.
        await db.execute("UPDATE settings SET value='35' WHERE key='subscription_price'")
        await db.execute("UPDATE settings SET value='30' WHERE key='subscription_days'")
        
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


async def sync_campaign_catalog() -> None:
    """Подтягивает в справочник все метки, которые уже есть в users и payment_success (для админских кнопок)."""
    now = time.time()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO campaigns (start_payload, created_at)
            SELECT DISTINCT TRIM(start_payload), ?
            FROM users
            WHERE COALESCE(TRIM(start_payload), '') <> ''
            """,
            (now,),
        )
        await db.execute(
            """
            INSERT OR IGNORE INTO campaigns (start_payload, created_at)
            SELECT DISTINCT TRIM(start_payload), ?
            FROM payment_success
            WHERE COALESCE(TRIM(start_payload), '') <> ''
            """,
            (now,),
        )
        await db.commit()


async def register_campaign(payload: str) -> None:
    """Регистрирует кампанию по raw start_payload."""
    cleaned = payload.strip()
    if not cleaned:
        return
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR IGNORE INTO campaigns (start_payload, created_at) VALUES (?, ?)",
            (cleaned, time.time()),
        )
        await db.commit()


async def count_campaigns() -> int:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM campaigns") as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 0


async def fetch_campaign_summaries_page(limit: int, offset: int) -> List[Tuple[int, str, int, int]]:
    """id кампании, payload, число входов (/start по метке), число успешных оплат с этим payload."""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            """
            SELECT c.id, c.start_payload,
              COALESCE((
                  SELECT COUNT(*)
                  FROM users u
                  WHERE COALESCE(TRIM(u.start_payload), '') = TRIM(c.start_payload)
              ), 0) AS lc,
              COALESCE((
                  SELECT COUNT(*)
                  FROM payment_success ps
                  WHERE COALESCE(TRIM(ps.start_payload), '') = TRIM(c.start_payload)
              ), 0) AS pc
            FROM campaigns c
            ORDER BY c.created_at DESC, c.id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ) as cursor:
            rows = await cursor.fetchall()
    return [(int(r[0]), str(r[1]), int(r[2]), int(r[3])) for r in rows]


async def get_campaign_by_id(campaign_id: int) -> Optional[Tuple[int, str, int, int]]:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            """
            SELECT c.id, c.start_payload,
              COALESCE((
                  SELECT COUNT(*)
                  FROM users u
                  WHERE COALESCE(TRIM(u.start_payload), '') = TRIM(c.start_payload)
              ), 0),
              COALESCE((
                  SELECT COUNT(*)
                  FROM payment_success ps
                  WHERE COALESCE(TRIM(ps.start_payload), '') = TRIM(c.start_payload)
              ), 0)
            FROM campaigns c
            WHERE c.id = ?
            """,
            (campaign_id,),
        ) as cursor:
            row = await cursor.fetchone()
    if not row:
        return None
    return int(row[0]), str(row[1]), int(row[2]), int(row[3])


async def get_attribution_for_user(user_id: int) -> Tuple[str, str, str, str]:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            """
            SELECT
              COALESCE(start_payload,''),
              COALESCE(utm_source,''),
              COALESCE(utm_medium,''),
              COALESCE(utm_campaign,'')
            FROM users
            WHERE id = ?
            """,
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
    if not row:
        return ("", "", "", "")
    return tuple(str(x) for x in row)  # type: ignore[misc]


async def maybe_set_first_touch_utm(
    user_id: int,
    start_payload_raw: str,
    utm_source: str,
    utm_medium: str,
    utm_campaign: str,
) -> bool:
    """
    First-touch: записывает метку только если ранее её не было.
    Возвращает True если появилась новая метка первого захода.
    """
    raw = start_payload_raw.strip()
    if not raw:
        return False
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            UPDATE users
               SET start_payload = ?,
                   utm_source = ?,
                   utm_medium = ?,
                   utm_campaign = ?
             WHERE id = ?
               AND (
                    start_payload IS NULL OR TRIM(start_payload) = ''
               )
            """,
            (raw, utm_source.strip(), utm_medium.strip(), utm_campaign.strip(), user_id),
        )
        async with db.execute("SELECT changes()") as cs:
            ch_row = await cs.fetchone()
        changed_rows = int(ch_row[0] if ch_row and ch_row[0] is not None else 0)
        if changed_rows > 0:
            await db.execute(
                "INSERT OR IGNORE INTO campaigns (start_payload, created_at) VALUES (?, ?)",
                (raw, time.time()),
            )
        await db.commit()

    return changed_rows > 0


async def touch_tapped_buy(user_id: int) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET tapped_buy = 1 WHERE id = ?", (user_id,))
        await db.commit()


async def cancel_auto_renew_keep_access(user_id: int) -> None:
    """
    Отмена автосписаний без потери доступа до subscription_end_date.
    Не трогаем subscription_active, дату доступа и грейс-периоды — кик остаётся только на планировщике.
    """
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE users SET card_token = '' WHERE id = ?",
            (user_id,),
        )
        await db.commit()


async def record_payment_success_row(
    user_id: int,
    *,
    bepaid_uid: Optional[str],
    tracking_id: Optional[str],
    amount_cents: Optional[int],
    currency: Optional[str],
    paid_at_ts: float,
    recurring: bool,
    payload: Optional[str],
    utm_source: Optional[str],
    utm_medium: Optional[str],
    utm_campaign: Optional[str],
) -> None:
    payload = payload or ""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            INSERT INTO payment_success (
               user_id, bepaid_uid, tracking_id,
               amount_cents, currency, paid_at,
               recurring, start_payload, utm_source, utm_medium, utm_campaign
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                user_id,
                bepaid_uid,
                tracking_id,
                amount_cents,
                currency,
                paid_at_ts,
                1 if recurring else 0,
                payload.strip(),
                utm_source or "",
                utm_medium or "",
                utm_campaign or "",
            ),
        )
        await db.commit()

    await register_campaign(payload)


async def count_payments_same_payload_exact(payload: str) -> int:
    token = payload.strip()
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM payment_success WHERE COALESCE(TRIM(start_payload),'')=?",
            (token,),
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 0


async def count_landings_exact(payload: str) -> int:
    token = payload.strip()
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE COALESCE(TRIM(start_payload),'')=?",
            (token,),
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 0


async def paginate_users_by_payload_landings(payload: str, limit: int, offset: int) -> List[
    Tuple[int, Optional[str], Optional[str], int, int, int, int]
]:
    """id, username, full_name, agreed, tapped_buy, sub_active, in_grace."""
    token = payload.strip()
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            """
            SELECT id,
                   username,
                   full_name,
                   agreed_to_terms,
                   tapped_buy,
                   subscription_active,
                   CASE WHEN grace_until_ts IS NOT NULL THEN 1 ELSE 0 END
              FROM users
             WHERE COALESCE(TRIM(start_payload), '') = ?
             ORDER BY join_date DESC
             LIMIT ? OFFSET ?
            """,
            (token, limit, offset),
        ) as cursor:
            rows = await cursor.fetchall()
    out: List[Tuple[int, Optional[str], Optional[str], int, int, int, int]] = []
    for r in rows:
        out.append(
            (
                int(r[0]),
                r[1],
                r[2],
                int(r[3] or 0),
                int(r[4] or 0),
                int(r[5] or 0),
                int(r[6] or 0),
            )
        )
    return out


async def paginate_payments_panel(payload: str, limit: int, offset: int) -> List[
    Tuple[int, Optional[str], Optional[str], int, float, str]
]:
    """user_id, username, recurring, paid_at_unix, truncated tracking."""
    token = payload.strip()
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            """
            SELECT p.user_id, u.username, p.recurring, p.paid_at,
                   COALESCE(p.tracking_id,'')
              FROM payment_success p
              LEFT JOIN users u ON u.id = p.user_id
             WHERE COALESCE(TRIM(p.start_payload), '') = ?
             ORDER BY p.paid_at DESC
             LIMIT ? OFFSET ?
            """,
            (token, limit, offset),
        ) as cursor:
            rows = await cursor.fetchall()
    return [(int(r[0]), r[1], int(r[2] or 0), float(r[3]), str(r[4] or "")) for r in rows]


async def build_csv_landings(payload: str) -> List[List[object]]:
    token = payload.strip()
    rows = await build_campaign_csv_landings_tuple(token)
    header = [
        "user_id",
        "username",
        "full_name",
        "agreed_to_terms",
        "tapped_buy",
        "subscription_active",
        "in_grace",
        "subscription_end_date_unix",
        "card_saved",
        "start_payload",
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "join_date",
    ]
    data: List[List[object]] = [header]
    for r in rows:
        data.append(list(r))
    return data


async def build_campaign_csv_landings_tuple(token: str):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            """
            SELECT id, username, full_name,
                   agreed_to_terms,
                   tapped_buy,
                   subscription_active,
                   CASE WHEN grace_until_ts IS NOT NULL THEN 1 ELSE 0 END,
                   subscription_end_date,
                   CASE WHEN COALESCE(TRIM(card_token), '') <> '' THEN 1 ELSE 0 END,
                   COALESCE(start_payload,''),
                   COALESCE(utm_source,''),
                   COALESCE(utm_medium,''),
                   COALESCE(utm_campaign,''),
                   join_date
            FROM users
            WHERE COALESCE(TRIM(start_payload), '') = ?
            ORDER BY join_date DESC
            """,
            (token,),
        ) as cursor:
            return await cursor.fetchall()


async def build_csv_payments_campaign(token: str) -> List[List[object]]:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            """
            SELECT p.user_id,
                   u.username,
                   u.full_name,
                   p.recurring,
                   datetime(p.paid_at, 'unixepoch', 'utc'),
                   COALESCE(p.bepaid_uid,''),
                   COALESCE(p.tracking_id,''),
                   COALESCE(CAST(p.amount_cents AS TEXT), ''),
                   COALESCE(p.currency,''),
                   COALESCE(p.start_payload,''),
                   COALESCE(p.utm_source,''),
                   COALESCE(p.utm_medium,''),
                   COALESCE(p.utm_campaign,'')
              FROM payment_success p
              LEFT JOIN users u ON u.id = p.user_id
             WHERE COALESCE(TRIM(p.start_payload), '') = ?
             ORDER BY p.paid_at DESC
            """,
            (token,),
        ) as cursor:
            flat = await cursor.fetchall()

    hdr = [
        "user_id",
        "username",
        "full_name",
        "recurring",
        "paid_at_utc",
        "bepaid_uid",
        "tracking_id",
        "amount_cents",
        "currency",
        "start_payload",
        "utm_source",
        "utm_medium",
        "utm_campaign",
    ]
    return [hdr] + [list(row) for row in flat]


async def payment_success_already_recorded(bepaid_uid: Optional[str], tracking_id: Optional[str]) -> bool:
    """Защита от повторных вебхуков bePaid."""
    bu = bepaid_uid.strip() if isinstance(bepaid_uid, str) else ""
    tt = tracking_id.strip() if isinstance(tracking_id, str) else ""
    async with aiosqlite.connect(DB_NAME) as db:
        if bu:
            async with db.execute(
                "SELECT 1 FROM payment_success WHERE bepaid_uid = ? LIMIT 1",
                (bu,),
            ) as cursor:
                if await cursor.fetchone():
                    return True
        if tt:
            async with db.execute(
                "SELECT 1 FROM payment_success WHERE tracking_id = ? LIMIT 1",
                (tt,),
            ) as cursor:
                if await cursor.fetchone():
                    return True
        return False


async def fetch_export_users_rows():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            """
            SELECT id, username, full_name,
                   agreed_to_terms,
                   tapped_buy,
                   subscription_active,
                   subscription_end_date,
                   CASE WHEN COALESCE(TRIM(card_token), '') <> '' THEN 1 ELSE 0 END,
                   COALESCE(start_payload,''),
                   COALESCE(utm_source,''),
                   COALESCE(utm_medium,''),
                   COALESCE(utm_campaign,''),
                   join_date
            FROM users
            ORDER BY join_date DESC
            """
        ) as cursor:
            return await cursor.fetchall()


async def fetch_export_payment_rows():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            """
            SELECT p.user_id, u.username, u.full_name,
                   p.recurring,
                   datetime(p.paid_at, 'unixepoch', 'utc'),
                   COALESCE(p.bepaid_uid,''),
                   COALESCE(p.tracking_id,''),
                   COALESCE(CAST(p.amount_cents AS TEXT), ''),
                   COALESCE(p.currency,''),
                   COALESCE(p.start_payload,''),
                   COALESCE(p.utm_source,''),
                   COALESCE(p.utm_medium,''),
                   COALESCE(p.utm_campaign,'')
            FROM payment_success p
            LEFT JOIN users u ON u.id = p.user_id
            ORDER BY p.paid_at DESC
            """
        ) as cursor:
            return await cursor.fetchall()


async def build_export_all_users() -> List[List[object]]:
    rows = await fetch_export_users_rows()
    hdr = [
        "user_id",
        "username",
        "full_name",
        "agreed_to_terms",
        "tapped_buy",
        "subscription_active",
        "subscription_end_date_unix",
        "card_saved",
        "start_payload",
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "join_date_sql",
    ]
    return [hdr] + [list(row) for row in rows]


async def build_export_all_payments() -> List[List[object]]:
    rows = await fetch_export_payment_rows()
    hdr = [
        "user_id",
        "username",
        "full_name",
        "recurring",
        "paid_at_utc",
        "bepaid_uid",
        "tracking_id",
        "amount_cents",
        "currency",
        "start_payload",
        "utm_source",
        "utm_medium",
        "utm_campaign",
    ]
    return [hdr] + [list(row) for row in rows]
