import logging
import asyncio
import os
import time
import html
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple
from aiohttp import web
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

import attribution
import database as db
import keyboards as kb
from bepaid_api import BePaidAPI

# Загружаем .env из папки, где лежит bot.py (важно для systemd: не зависим от текущей директории)
_env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(_env_path)

TOKEN = os.getenv("BOT_TOKEN")
# Единственный канал: сюда выдаём инвайты после оплаты и отсюда кикаем при неуплате (кроме админов)
CHANNEL_ID = os.getenv("CHANNEL_ID")
if CHANNEL_ID is not None:
    CHANNEL_ID = CHANNEL_ID.strip()
CHANNEL_INVITE_LINK = (os.getenv("CHANNEL_INVITE_LINK") or "https://t.me/+DxKiacUx8M9mMjBi").strip()
MANAGER_LINK = (os.getenv("MANAGER_LINK") or "https://t.me/nastyaprostozhit").strip()

BEPAID_SHOP_ID = os.getenv("BEPAID_SHOP_ID")
BEPAID_SECRET_KEY = os.getenv("BEPAID_SECRET_KEY")
# Тестовый режим магазина (должен совпадать с настройками в ЛК bePaid)
BEPAID_TEST = os.getenv("BEPAID_TEST", "").strip().lower() in ("1", "true", "yes")
# Webhook settings
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "http://194.62.19.77:8080")
WEBHOOK_PATH = "/bepaid/webhook"
WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = 8080

PAYMENT_ADMIN_NOTIFY = os.getenv("PAYMENT_ADMIN_NOTIFY", "").strip().lower() in ("1", "true", "yes")
CMP_LIST_PAGE = 8
CMP_ROWS_USERS = 30
CMP_ROWS_PAY = 25

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize bot and dispatcher
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())
bepaid = BePaidAPI(shop_id=BEPAID_SHOP_ID, secret_key=BEPAID_SECRET_KEY, test_mode=BEPAID_TEST)

# Захардкоженные ссылки в приветствии
WELCOME_LINKS_HTML = """• <a href="https://psyprosto-help.by/policy">Политика конфиденциальности</a>
• <a href="https://psyprosto-help.by/polozhenie">Положение</a>
• <a href="https://school.psy-prosto.school/oferta">Оферта</a>"""

# States
class AdminStates(StatesGroup):
    waiting_for_broadcast = State()
    waiting_for_welcome_text = State()
    waiting_for_welcome_photo = State()
    waiting_for_payment_text = State()
    waiting_for_price = State()

# --- Helpers ---
async def is_admin(user_id: int):
    # Приводим к строке для надежного сравнения
    user_id_str = str(user_id)
    
    # 1. Проверяем ADMIN_IDS из .env
    env_admins_raw = os.getenv("ADMIN_IDS", "")
    # Разбиваем по запятой, чистим от пробелов и пустых элементов
    env_admins = [x.strip() for x in env_admins_raw.split(",") if x.strip()]
    
    if user_id_str in env_admins:
        return True
        
    # 2. Проверяем таблицу admins в БД
    db_admins = await db.get_admins()
    # db.get_admins() возвращает список int, приводим к str
    if user_id in db_admins:
        return True
        
    return False


def coerce_tx_amount_currency(transaction: dict) -> Tuple[Optional[int], Optional[str]]:
    raw = transaction.get("amount") or transaction.get("paid_amount")
    curr = transaction.get("currency") or ""
    if raw is None:
        pay = transaction.get("payment") if isinstance(transaction.get("payment"), dict) else None
        if pay:
            raw = pay.get("amount")
            curr = pay.get("currency") or curr
    try:
        if raw is None:
            return None, str(curr or "").strip() or None
        amt = float(raw)
        return int(round(amt)), str(curr or "").strip() or None
    except (TypeError, ValueError):
        return None, str(curr or "").strip() or None


def tx_is_recurring_mark(transaction: dict) -> bool:
    r = transaction.get("recurring_type") or transaction.get("type") or ""
    r = str(r).strip().lower()
    if any(x in r for x in ("recurrent", "recurring", "repeat")):
        return True
    rt = transaction.get("recurring")
    if rt is True:
        return True
    return False


async def cmp_edit_or_answer(
    callback: types.CallbackQuery,
    text: str,
    reply_markup: Optional[types.InlineKeyboardMarkup] = None,
    parse_mode: Optional[str] = None,
):
    try:
        await callback.message.edit_text(
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )
    except Exception:
        await callback.message.answer(
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )


def chunks_for_telegram(text: str, max_len: int = 3900):
    chunk = ""
    for line in (text.splitlines() if text else [""]):
        if len(chunk) + len(line) + 1 > max_len:
            yield chunk.strip() or "(…)"
            chunk = ""
        chunk += ("\n" if chunk else "") + line
    if chunk.strip():
        yield chunk


async def notify_admins_after_payment(from_user_id: int, marker: str, recurring: bool, pays_with_marker: int):
    """Короткое уведомление админам (по желанию .env PAYMENT_ADMIN_NOTIFY)."""
    if not PAYMENT_ADMIN_NOTIFY:
        return
    admins: List[int] = []
    raw = os.getenv("ADMIN_IDS", "")
    env_admins = [int(x.strip()) for x in raw.split(",") if x.strip().isdigit()]
    admins.extend(env_admins)
    for uid in await db.get_admins():
        if uid not in admins:
            admins.append(uid)
    if not admins:
        return

    rk = "рекуррент" if recurring else "первая/разовая"
    label = marker.strip() if marker.strip() else "— без метки (старый трафик) —"

    txt = (
        f"💳 Оплата OK\nПользователь: {from_user_id}\n"
        f"Метка: {label}\nТип платежа: {rk}\n"
        f"Успешных оплат с тем же хвостом start= сейчас: {pays_with_marker}"
    )

    for aid in admins:
        try:
            await bot.send_message(aid, txt)
        except Exception as e:
            logger.warning("Не удалось уведомить админа %s: %s", aid, e)



async def bepaid_webhook_handler(request):
    try:
        data = await request.json()
        # Карточные уведомления: https://docs.bepaid.by/ru/using_api/webhooks/
        transaction = data.get("transaction") if isinstance(data.get("transaction"), dict) else {}
        if not transaction and isinstance(data, dict) and data.get("uid") and data.get("tracking_id"):
            transaction = data
        status = transaction.get("status")
        tracking_id = transaction.get("tracking_id")  # format: user_id:timestamp

        logger.info(
            "Received webhook: uid=%s status=%s tracking_id=%s recurring_type=%s",
            transaction.get("uid"),
            status,
            tracking_id,
            transaction.get("recurring_type"),
        )

        if status == "successful" and tracking_id:
            try:
                user_id = int(tracking_id.split(":")[0])
                buid_raw = transaction.get("uid")
                buid = str(buid_raw).strip() if buid_raw is not None else ""
                tid = str(tracking_id).strip()
                if await db.payment_success_already_recorded(buid if buid else None, tid):
                    logger.info(
                        "Duplicate BePaid webhook ignored: user_id=%s uid=%s tracking_id=%s",
                        user_id,
                        buid or "-",
                        tid,
                    )
                    return web.Response(text="OK", status=200)

                # Метки first-touch уже на пользователе; снимок на момент оплаты
                pl_snapshot, utm_s, utm_m, utm_c = await db.get_attribution_for_user(user_id)
                amt_cents, amt_currency = coerce_tx_amount_currency(transaction)
                recur_mark = tx_is_recurring_mark(transaction)

                # Токен и email для последующих списаний (см. saved_cards)
                credit_card = transaction.get("credit_card", {}) or {}
                card_token = credit_card.get("token")
                customer = transaction.get("customer", {}) or {}
                paid_email = customer.get("email")

                if not card_token:
                    logger.error(
                        "Webhook OK but credit_card.token is empty — автосписания будут невозможны. "
                        "user_id=%s uid=%s (нужна инициализирующая оплата с contract recurring+card_on_file)",
                        user_id,
                        transaction.get("uid"),
                    )

                # Снимаем возможный бан и продлеваем подписку (например, на 30 дней)
                days_str = await db.get_setting("subscription_days") or "30"
                days = int(days_str)
                new_end_date = time.time() + (days * 24 * 60 * 60)
                try:
                    await bot.unban_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
                except Exception as e:
                    logger.warning("Unban before invite failed for user %s: %s", user_id, e)

                await db.clear_grace_period(user_id)
                await db.set_subscription(
                    user_id,
                    status=True,
                    end_date=new_end_date,
                    card_token=card_token,
                    email=paid_email,
                )
                end_date_str = datetime.utcfromtimestamp(new_end_date).strftime("%Y-%m-%d %H:%M UTC")
                logger.info(
                    f"Payment OK: user_id={user_id}, card_saved={'yes' if card_token else 'no'}, "
                    f"subscription_until={end_date_str}, auto_renew={'yes' if card_token else 'no'}"
                )

                await db.record_payment_success_row(
                    user_id,
                    bepaid_uid=buid if buid else None,
                    tracking_id=tid if tid else None,
                    amount_cents=amt_cents,
                    currency=amt_currency,
                    paid_at_ts=time.time(),
                    recurring=recur_mark,
                    payload=pl_snapshot,
                    utm_source=utm_s,
                    utm_medium=utm_m,
                    utm_campaign=utm_c,
                )
                pay_cnt = await db.count_payments_same_payload_exact(pl_snapshot)
                asyncio.create_task(
                    notify_admins_after_payment(user_id, pl_snapshot, recur_mark, pay_cnt)
                )
                
                invite_link = CHANNEL_INVITE_LINK
                
                payment_text = await db.get_setting("payment_success_text") or "✅ Оплата прошла успешно!\n\nНажмите кнопку ниже, чтобы вступить в канал."
                
                await bot.send_message(
                    chat_id=user_id,
                    text=payment_text,
                    reply_markup=kb.get_member_keyboard(MANAGER_LINK, invite_link=invite_link)
                )
                
            except Exception as e:
                logger.error(f"Error processing webhook logic: {e}")
        
        return web.Response(text="OK", status=200)
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return web.Response(text="Error", status=500)

# --- Scheduler for Recurring Payments ---
async def check_recurring_payments():
    """Ежедневная проверка подписок"""
    while True:
        try:
            # Ждем 24 часа (или запускаем раз в день в определенное время)
            # Для теста можно уменьшить
            users_due = await db.get_users_due_payment()
            
            price_str = await db.get_setting("subscription_price") or "30"
            price = float(price_str)
            days_str = await db.get_setting("subscription_days") or "30"
            days = int(days_str)

            for user in users_due:
                user_id, card_token, email, grace_until_ts, last_notice_ts = user

                # Никогда не трогаем админов (из .env и из БД)
                if await is_admin(user_id):
                    continue
                
                if not card_token:
                    continue
                
                logger.info(f"Attempting to charge user {user_id}")

                order_tracking = f"{user_id}:{int(time.time())}"
                success, result = await bepaid.charge_recurrent(
                    amount=price,
                    currency="BYN",
                    description=f"Продление подписки (Bot) для {user_id}",
                    order_id=order_tracking,
                    card_token=card_token,
                    email=email or "no-email@example.com",
                )

                if success:
                    trx = result if isinstance(result, dict) else {}
                    buid = str(trx.get("uid") or "").strip()
                    tid_trace = str(trx.get("tracking_id") or order_tracking).strip()
                    new_end_date = time.time() + (days * 24 * 60 * 60)
                    await db.clear_grace_period(user_id)
                    await db.set_subscription(user_id, status=True, end_date=new_end_date)
                    if not await db.payment_success_already_recorded(
                        buid if buid else None, tid_trace if tid_trace else None
                    ):
                        pl0, utm_s, utm_m, utm_c = await db.get_attribution_for_user(user_id)
                        amt_cents, amt_curr = coerce_tx_amount_currency(trx)
                        await db.record_payment_success_row(
                            user_id,
                            bepaid_uid=buid if buid else None,
                            tracking_id=tid_trace if tid_trace else None,
                            amount_cents=amt_cents,
                            currency=amt_curr,
                            paid_at_ts=time.time(),
                            recurring=True,
                            payload=pl0,
                            utm_source=utm_s,
                            utm_medium=utm_m,
                            utm_campaign=utm_c,
                        )
                        pay_cnt = await db.count_payments_same_payload_exact(pl0)
                        asyncio.create_task(
                            notify_admins_after_payment(user_id, pl0, True, pay_cnt)
                        )

                    await bot.send_message(user_id, f"✅ Подписка успешно продлена на {days} дней!")
                else:
                    now_ts = time.time()
                    grace_until = now_ts + (3 * 24 * 60 * 60)

                    # Отключаем автосписание по токену (чтобы не долбить карту) и включаем грейс 3 дня
                    await db.set_subscription(user_id, status=True, card_token="")
                    await db.set_grace_period(
                        user_id=user_id,
                        grace_until_ts=grace_until,
                        fail_ts=now_ts,
                        notice_ts=now_ts,
                    )

                    logger.info(
                        "Payment failed, grace started: user_id=%s, grace_until=%s, reason=%s",
                        user_id,
                        datetime.utcfromtimestamp(grace_until).strftime("%Y-%m-%d %H:%M UTC"),
                        result,
                    )

                    # Сообщаем и предлагаем оплатить заново по кнопке (с актуальной суммой)
                    retry_kb = types.InlineKeyboardMarkup(
                        inline_keyboard=[
                            [types.InlineKeyboardButton(text="💳 Оплатить заново", callback_data="pay_again")]
                        ]
                    )
                    await bot.send_message(
                        user_id,
                        "❌ Автосписание не прошло.\n\n"
                        "У вас есть 3 дня, чтобы пополнить карту или оплатить заново по кнопке ниже.\n"
                        "После 3 дней доступ к каналу будет отключён.",
                        reply_markup=retry_kb,
                    )

            # Уведомления в грейс-период (раз в 24 часа)
            users_in_grace = await db.get_users_in_grace_to_notify()
            for row in users_in_grace:
                user_id, email, grace_until_ts, last_notice_ts = row
                if await is_admin(user_id):
                    continue
                retry_kb = types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [types.InlineKeyboardButton(text="💳 Оплатить заново", callback_data="pay_again")]
                    ]
                )
                await bot.send_message(
                    user_id,
                    "⏳ Напоминание: оплата подписки не прошла.\n\n"
                    "Пополните карту или оплатите заново по кнопке ниже.\n"
                    "Иначе доступ к каналу будет отключён по окончании 3 дней.",
                    reply_markup=retry_kb,
                )
                await db.update_grace_notice_ts(user_id, time.time())

            # Истёкшая подписка без карты: запускаем грейс (если ещё не запускали)
            expired_no_card_start = await db.get_users_expired_no_card_start_grace()
            for user_id, email in expired_no_card_start:
                if await is_admin(user_id):
                    continue
                now_ts = time.time()
                grace_until = now_ts + (3 * 24 * 60 * 60)
                await db.set_grace_period(
                    user_id=user_id,
                    grace_until_ts=grace_until,
                    fail_ts=now_ts,
                    notice_ts=now_ts,
                )
                retry_kb = types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [types.InlineKeyboardButton(text="💳 Оплатить заново", callback_data="pay_again")]
                    ]
                )
                await bot.send_message(
                    user_id,
                    "❌ Срок подписки истёк.\n\n"
                    "У вас есть 3 дня, чтобы оплатить подписку заново по кнопке ниже.\n"
                    "После 3 дней доступ к каналу будет отключён.",
                    reply_markup=retry_kb,
                )

            # Истёкшая подписка без карты — выгоняем после окончания грейса (админов не трогаем)
            expired_no_card_to_kick = await db.get_users_expired_no_card_to_kick()
            for user_id in expired_no_card_to_kick:
                if await is_admin(user_id):
                    continue
                await db.set_subscription(user_id, status=False)
                try:
                    await bot.ban_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
                    logger.info(f"Kicked user {user_id} (subscription expired, no card, grace ended)")
                except Exception as k_err:
                    logger.error(f"Failed to kick user {user_id}: {k_err}")

            # Проверка раз в час (чтобы не пропустить)
            await asyncio.sleep(3600) 
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
            await asyncio.sleep(3600)


@dp.callback_query(F.data == "pay_again")
async def pay_again(callback: types.CallbackQuery):
    """Сгенерировать новую ссылку на оплату с актуальной суммой подписки."""
    user_id = callback.from_user.id
    await db.touch_tapped_buy(user_id)
    price_str = await db.get_setting("subscription_price") or "30"
    try:
        price = float(price_str)
    except ValueError:
        price = 30.0

    order_id = f"{user_id}:{int(time.time())}"
    email = f"user{user_id}@telegram.bot"

    payment_url = await bepaid.create_checkout_link(
        amount=price,
        currency="BYN",
        description="Подписка на закрытый канал (повторная оплата)",
        order_id=order_id,
        email=email,
        notification_url=f"{WEBHOOK_HOST}{WEBHOOK_PATH}",
        return_url=os.getenv("BOT_LINK") or "https://t.me/n_deniseva_bot",
    )

    if not payment_url:
        await callback.message.answer("❌ Не удалось сформировать ссылку на оплату. Попробуйте позже.")
        await callback.answer()
        return

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text=f"💳 Оплатить {price} BYN", url=payment_url)]]
    )
    await callback.message.answer("Ссылка на оплату сформирована:", reply_markup=keyboard)
    await callback.answer()

# --- User Handlers ---

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user = message.from_user
    print(f"--- USER ID: {user.id} ---")
    deep_raw = ""
    if message.text:
        chunks = message.text.split(maxsplit=1)
        if len(chunks) > 1:
            deep_raw = chunks[1].strip()

    await db.add_user(user.id, user.username, user.full_name)

    clean_payload = attribution.sanitize_start_payload(deep_raw or None)
    if deep_raw and not clean_payload:
        logger.warning(
            "Игнорирован неподходящий /start параметр user_id=%s raw=%r "
            "(нужно латиница/цифры/_/- до 64 симв.)",
            user.id,
            deep_raw[:80],
        )
    elif clean_payload:
        utm_src, utm_med, utm_cmp, raw_full = attribution.parse_utm_payload(clean_payload)
        applied = await db.maybe_set_first_touch_utm(user.id, raw_full, utm_src, utm_med, utm_cmp)
        if applied:
            logger.info("First-touch: user=%s payload=%s src=%s med=%s camp=%s",
                        user.id, raw_full, utm_src, utm_med or "-", utm_cmp or "-")
    
    intro = await db.get_setting("welcome_text") or "Добро пожаловать в наш бот!\n\nПожалуйста, ознакомьтесь с правилами ниже.\n\nНажмите кнопку ниже, чтобы продолжить."
    if WELCOME_LINKS_HTML not in intro:
        full_welcome = intro.rstrip() + "\n\n" + WELCOME_LINKS_HTML
    else:
        full_welcome = intro
    welcome_photo = await db.get_setting("welcome_photo")
    
    if welcome_photo:
        await message.answer_photo(photo=welcome_photo, caption=full_welcome, parse_mode="HTML", reply_markup=kb.get_welcome_keyboard())
    else:
        await message.answer(text=full_welcome, parse_mode="HTML", disable_web_page_preview=True, reply_markup=kb.get_welcome_keyboard())

    # Админу показываем отдельную кнопку над клавиатурой для входа в админ-панель
    if await is_admin(user.id):
        admin_kb = types.ReplyKeyboardMarkup(
            keyboard=[[types.KeyboardButton(text="Админ-панель")]],
            resize_keyboard=True
        )
        # Отправляем явное сообщение, чтобы клавиатура точно появилась
        await message.answer("🔧 Вы администратор. Меню управления доступно по кнопке ниже.", reply_markup=admin_kb)

@dp.callback_query(F.data == "agreed_to_terms")
async def process_agreement(callback: types.CallbackQuery):
    await db.set_agreed(callback.from_user.id)
    await callback.message.answer("Спасибо! Выберите действие:", reply_markup=kb.get_subscription_keyboard(MANAGER_LINK))
    await callback.answer()

@dp.callback_query(F.data == "simulate_payment")
async def start_payment(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await db.touch_tapped_buy(user_id)

    # Админ пропускает оплату — сразу выдаём блок подписчика (инвайт + кнопки)
    if await is_admin(user_id):
        days_str = await db.get_setting("subscription_days") or "30"
        days = int(days_str)
        new_end_date = time.time() + (days * 24 * 60 * 60)
        await db.set_subscription(user_id, status=True, end_date=new_end_date)
        invite_link = CHANNEL_INVITE_LINK
        payment_text = await db.get_setting("payment_success_text") or "✅ Оплата прошла успешно!\n\nНажмите кнопку ниже, чтобы вступить в канал."
        await callback.message.answer(
            f"✅ [Админ] Доступ открыт без оплаты.\n\n{payment_text}",
            reply_markup=kb.get_member_keyboard(MANAGER_LINK, invite_link=invite_link)
        )
        await callback.answer()
        return

    price_str = await db.get_setting("subscription_price") or "10"
    price = float(price_str)
    order_id = f"{user_id}:{int(time.time())}"
    email = f"user{user_id}@telegram.bot" # Заглушка, т.к. мы не знаем email
    
    payment_url = await bepaid.create_checkout_link(
        amount=price,
        currency="BYN",
        description="Подписка на закрытый канал",
        order_id=order_id,
        email=email,
        notification_url=f"{WEBHOOK_HOST}{WEBHOOK_PATH}",
        return_url=os.getenv("BOT_LINK") or "https://t.me/n_deniseva_bot"
    )
    
    if payment_url:
        # Отправляем кнопку с ссылкой на оплату
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text=f"💳 Оплатить {price} BYN", url=payment_url)]
        ])
        await callback.message.answer("Для оформления подписки нажмите кнопку ниже:", reply_markup=keyboard)
    else:
        await callback.message.answer("❌ Ошибка создания платежа. Попробуйте позже или свяжитесь с менеджером.")
    
    await callback.answer()

@dp.callback_query(F.data == "cancel_subscription")
async def process_cancel_sub(callback: types.CallbackQuery):
    """Показать подтверждение: Отменить подписку? Да / Нет."""
    await callback.message.answer(
        "Отменить автопродление?\n\n"
        "Спишем больше не будем автоматически. Доступ к каналу останется до окончания уже оплаченного периода "
        "(и не пропадает из-за этого нажатия).",
        reply_markup=kb.get_cancel_subscription_confirm_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "cancel_subscription_confirm")
async def process_cancel_sub_confirm(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    sub_row = await db.get_user_subscription(user_id)
    await db.cancel_auto_renew_keep_access(user_id)
    logger.info(
        "Auto-renew cancelled by user user_id=%s (card_token cleared); channel access untouched here",
        user_id,
    )

    msg = (
        "✅ Автосписание отключено.\n\n"
        "С вашей карты больше не будут брать деньги за продление через бота.\n"
        "Доступ к каналу сохранится до текущего срока подписки (как указано ниже)."
    )

    now_ts = time.time()
    try:
        if sub_row:
            _, end_ts_sub, tok = sub_row
            tok_non_empty = tok and str(tok).strip()
            if end_ts_sub and float(end_ts_sub) > float(now_ts):
                until = datetime.utcfromtimestamp(float(end_ts_sub)).strftime("%Y-%m-%d %H:%M UTC")
                msg += f"\n\n📅 Доступ по подписке до: {until} (UTC)."
                if tok_non_empty:
                    msg += (
                        "\nКарта отвязана: автоматических продлений больше не будет."
                    )
            elif end_ts_sub:
                msg += (
                    "\n\nПо времени доступ уже после оплаченного периода. "
                    "Если нужны детали, напишите в поддержку."
                )
    except Exception as e:
        logger.warning("Cancel confirm: formatting dates failed uid=%s: %s", user_id, e)

    try:
        await callback.message.edit_text(msg)
    except Exception:
        await callback.message.answer(msg)

    await callback.answer()


@dp.callback_query(F.data == "cancel_subscription_abort")
async def process_cancel_sub_abort(callback: types.CallbackQuery):
    try:
        await callback.message.edit_text("Действие отменено.")
    except Exception:
        await callback.message.answer("Действие отменено.")
    await callback.answer()


# --- Debug / Service commands ---

@dp.message(Command("whoami"))
async def cmd_whoami(message: types.Message):
    """Отладочная команда: показывает ID пользователя, его статус и данные подписки."""
    user = message.from_user
    user_id = user.id
    isadm = await is_admin(user_id)
    sub = await db.get_user_subscription(user_id)
    if sub:
        active, sub_end_ts, card_token = sub
        if sub_end_ts:
            sub_end = datetime.utcfromtimestamp(sub_end_ts).strftime("%Y-%m-%d %H:%M UTC")
        else:
            sub_end = "нет"
        card_saved = "YES" if card_token else "NO"
    else:
        active, sub_end, card_saved = "нет записи", "нет", "NO"

    text = (
        f"Ваш ID: {user_id}\n"
        f"Админ: {'YES' if isadm else 'NO'}\n"
        f"Подписка активна: {active}\n"
        f"Подписка до: {sub_end}\n"
        f"Карта привязана (для автосписаний): {card_saved}"
    )
    await message.answer(text)


@dp.message(Command("check_user"))
async def cmd_check_user(message: types.Message):
    """
    /check_user <id>
    Команда только для админов: посмотреть статус пользователя в канале и его подписку.
    """
    if not await is_admin(message.from_user.id):
        return

    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /check_user <telegram_id>")
        return

    uid = int(parts[1])

    # Проверяем подписку в БД
    sub = await db.get_user_subscription(uid)
    if sub:
        active, sub_end_ts, card_token = sub
        if sub_end_ts:
            sub_end = datetime.utcfromtimestamp(sub_end_ts).strftime("%Y-%m-%d %H:%M UTC")
        else:
            sub_end = "нет"
        card_saved = "YES" if card_token else "NO"
    else:
        active, sub_end, card_saved = "нет записи", "нет", "NO"

    # Проверяем статус в канале
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=uid)
        status = member.status
    except Exception as e:
        status = f"ошибка получения статуса: {e}"

    text = (
        f"Пользователь ID: {uid}\n"
        f"Статус в канале: {status}\n"
        f"Подписка активна: {active}\n"
        f"Подписка до: {sub_end}\n"
        f"Карта привязана (для автосписаний): {card_saved}"
    )
    await message.answer(text)


@dp.message(Command("force_kick"))
async def cmd_force_kick(message: types.Message):
    """
    /force_kick <id>
    Жёстко кикнуть пользователя из канала по его Telegram ID.
    Нужна только для отладки/ручного вмешательства.
    """
    if not await is_admin(message.from_user.id):
        return

    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /force_kick <telegram_id>")
        return

    uid = int(parts[1])

    try:
        await bot.ban_chat_member(chat_id=CHANNEL_ID, user_id=uid)
        await db.set_subscription(uid, status=False, card_token="")
        await message.answer(f"Пользователь {uid} забанен (кикнут) из канала и подписка отключена.")
        logger.info("Force kick by admin: uid=%s", uid)
    except Exception as e:
        await message.answer(f"Не удалось кикнуть {uid}: {e}")
        logger.error("Force kick failed for uid=%s: %s", uid, e)


# --- Admin Handlers (Оставил основные, добавил цену) ---

@dp.message(Command("admin"))
@dp.message(F.text == "Админ-панель")
async def cmd_admin(message: types.Message):
    user_id = message.from_user.id
    if not await is_admin(user_id):
        await message.answer(
            "⛔ Эта команда только для администраторов бота.\n\n"
            f"Ваш Telegram ID: <code>{user_id}</code>\n\n"
            "Добавьте этот ID в список <code>ADMIN_IDS</code> в файле "
            "<code>.env</code> на сервере (через запятую без пробелов) и выполните "
            "<code>systemctl restart tgbot-podpiska.service</code>.\n\n"
            "<i>Раздел со статистикой по ссылкам находится здесь после входа:</i> "
            "кнопка «📊 Метки и кампании» или команды /stats или /campaigns.",
            parse_mode="HTML",
        )
        logger.warning("/admin denied: user_id=%s not in ADMIN_IDS/admins DB", user_id)
        return

    # Инлайн-меню админа
    await message.answer("🔧 Админ-панель:", reply_markup=kb.get_admin_keyboard())

    # И гарантированно показываем reply-клавиатуру с кнопкой «Админ-панель»
    admin_kb = types.ReplyKeyboardMarkup(
        keyboard=[[types.KeyboardButton(text="Админ-панель")]],
        resize_keyboard=True,
    )
    await message.answer("Клавиатура управления:", reply_markup=admin_kb)


@dp.message(Command("stats", "campaigns"))
async def cmd_stats_campaigns(message: types.Message):
    """Быстрый вход в раздел меток (переходы по ?start= и оплаты с snapshot метки в журнале)."""
    uid = message.from_user.id
    if not await is_admin(uid):
        await message.answer(
            f"⛔ Только админ. Ваш ID: <code>{uid}</code> — см. ADMIN_IDS в .env на VPS.",
            parse_mode="HTML",
        )
        return
    await message.answer(
        "📊 Метки и кампании\n\n"
        "Примеры ссылок:\n"
        "https://t.me/n_deniseva_bot?start=vk__march\n"
        "https://t.me/n_deniseva_bot?start=viber__broadcast__nov",
        reply_markup=kb.get_campaigns_hub_keyboard(),
    )


@dp.callback_query(F.data == "open_admin_panel")
async def open_admin_panel(callback: types.CallbackQuery):
    """Вход в админку по инлайн-кнопке (для тех, у кого не появлялась reply-клавиатура)."""
    user_id = callback.from_user.id
    if not await is_admin(user_id):
        await callback.answer("У вас нет прав администратора.", show_alert=True)
        return

    # То же поведение, что и у /admin
    await callback.message.answer("🔧 Админ-панель:", reply_markup=kb.get_admin_keyboard())
    admin_kb = types.ReplyKeyboardMarkup(
        keyboard=[[types.KeyboardButton(text="Админ-панель")]],
        resize_keyboard=True,
    )
    await callback.message.answer("Клавиатура управления:", reply_markup=admin_kb)
    await callback.answer()

# ... (остальные хендлеры админки те же, добавлю только один для цены) ...

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id): return
    await state.set_state(AdminStates.waiting_for_broadcast)
    await callback.message.answer("Введите текст рассылки:", reply_markup=kb.get_cancel_keyboard())
    await callback.answer()

@dp.message(AdminStates.waiting_for_broadcast)
async def admin_broadcast_send(message: types.Message, state: FSMContext):
    users = await db.get_users()
    count = 0
    status_msg = await message.answer(f"Начинаю рассылку для {len(users)} пользователей...")
    for user_id in users:
        try:
            await message.copy_to(chat_id=user_id)
            count += 1
            await asyncio.sleep(0.05) 
        except Exception:
            pass
    await status_msg.edit_text("✅Рассылка завершена.")
    await state.clear()
    await message.answer("🔧 Админ-панель:", reply_markup=kb.get_admin_keyboard())

# --- Другие админские хендлеры нужно восстановить из старого файла (welcome, photo, cancel, payment text) ---
# Я их сократил для примера, но в финальном файле они будут.
# Добавляю хендлеры из предыдущего файла чтобы ничего не сломать

@dp.callback_query(F.data == "admin_edit_welcome_text")
async def admin_edit_welcome_text(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id): return
    await state.set_state(AdminStates.waiting_for_welcome_text)
    await callback.message.answer("Введите новый текст приветствия:", reply_markup=kb.get_cancel_keyboard())
    await callback.answer()

@dp.message(AdminStates.waiting_for_welcome_text)
async def admin_save_welcome_text(message: types.Message, state: FSMContext):
    await db.set_setting("welcome_text", message.text)
    await message.answer("✅ Текст приветствия обновлен.")
    await state.clear()
    await message.answer("🔧 Админ-панель:", reply_markup=kb.get_admin_keyboard())

@dp.callback_query(F.data == "admin_edit_welcome_photo")
async def admin_edit_welcome_photo(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id): return
    await state.set_state(AdminStates.waiting_for_welcome_photo)
    await callback.message.answer("Отправьте новое фото приветствия:", reply_markup=kb.get_cancel_keyboard())
    await callback.answer()

@dp.message(AdminStates.waiting_for_welcome_photo, F.photo)
async def admin_save_welcome_photo(message: types.Message, state: FSMContext):
    photo_id = message.photo[-1].file_id
    await db.set_setting("welcome_photo", photo_id)
    await message.answer("✅ Фото приветствия обновлено.")
    await state.clear()
    await message.answer("🔧 Админ-панель:", reply_markup=kb.get_admin_keyboard())

@dp.callback_query(F.data == "admin_edit_payment_text")
async def admin_edit_payment_text(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id): return
    await state.set_state(AdminStates.waiting_for_payment_text)
    await callback.message.answer("Введите новый текст сообщения после оплаты:", reply_markup=kb.get_cancel_keyboard())
    await callback.answer()

@dp.message(AdminStates.waiting_for_payment_text)
async def admin_save_payment_text(message: types.Message, state: FSMContext):
    await db.set_setting("payment_success_text", message.text)
    await message.answer("✅ Текст после оплаты обновлен.")
    await state.clear()
    await message.answer("🔧 Админ-панель:", reply_markup=kb.get_admin_keyboard())


@dp.callback_query(F.data == "admin_edit_price")
async def admin_edit_price(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        return
    current_price = await db.get_setting("subscription_price") or "30"
    await state.set_state(AdminStates.waiting_for_price)
    await callback.message.answer(
        f"Текущая стоимость подписки: {current_price} BYN.\n"
        f"Введите новую стоимость в BYN (например 30 или 29.9):",
        reply_markup=kb.get_cancel_keyboard()
    )
    await callback.answer()


@dp.message(AdminStates.waiting_for_price)
async def admin_save_price(message: types.Message, state: FSMContext):
    text = (message.text or "").replace(",", ".").strip()
    try:
        price = float(text)
        if price <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Некорректное значение. Введите положительное число, например 30 или 29.9.")
        return

    # Сохраняем как строку (например '30' или '29.9')
    await db.set_setting("subscription_price", str(price))
    await message.answer(f"✅ Стоимость подписки обновлена: {price} BYN.")
    await state.clear()
    await message.answer("🔧 Админ-панель:", reply_markup=kb.get_admin_keyboard())

@dp.callback_query(F.data == "cancel_action")
async def cancel_handler(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.edit_text("🔧 Админ-панель:", reply_markup=kb.get_admin_keyboard())
    except Exception:
        await callback.message.answer("🔧 Админ-панель:", reply_markup=kb.get_admin_keyboard())
    await callback.answer()


@dp.callback_query(F.data.startswith("CMP|"))
async def cmp_attribution_dashboard(callback: types.CallbackQuery):
    """Метки, кампании, сводки, CSV для администратора."""
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    pieces = callback.data.split("|")
    mode = pieces[1] if len(pieces) > 1 else ""

    await callback.answer()

    hub_intro = (
        "📊 Метки и кампании\n\n"
        "Примеры ссылок:\n"
        "https://t.me/n_deniseva_bot?start=vk__march\n"
        "https://t.me/n_deniseva_bot?start=viber__broadcast__nov"
    )

    async def sender(txt: str, markup=None, html: bool = False):
        pm = "HTML" if html else None
        await cmp_edit_or_answer(callback, txt, markup, parse_mode=pm)

    hub_kb = kb.get_campaigns_hub_keyboard()

    try:
        if mode == "hub":
            await sender(hub_intro, hub_kb)

        elif mode == "adm":
            await callback.message.answer("🔧 Админ-панель:", reply_markup=kb.get_admin_keyboard())

        elif mode == "sum":
            await db.sync_campaign_catalog()
            rows_all: List[Tuple[int, str, int, int]] = []
            offset = 0
            batch_size = 200
            while True:
                chunk = await db.fetch_campaign_summaries_page(batch_size, offset)
                if not chunk:
                    break
                rows_all.extend(chunk)
                offset += batch_size

            if not rows_all:
                await sender("Пока нет ни одной метки ни в заходах, ни в журнале оплат.", hub_kb)
                return

            body_lines = [
                "Сводка по меткам:",
                "",
            ]
            for _cid, payl, lc, pc in rows_all:
                disp = payl if len(payl) <= 500 else payl[:497] + "..."
                body_lines.append(disp)
                body_lines.append(f"👣 зашли по метке: {lc}")
                body_lines.append(f"💳 успешные оплаты: {pc}")
                body_lines.append("")

            assembled = "\n".join(body_lines).strip("\n")

            fragments = list(chunks_for_telegram(assembled)) or ["(нет данных для отображения)"]
            if len(fragments) == 1:
                await cmp_edit_or_answer(callback, fragments[0], hub_kb, parse_mode=None)
            else:
                await cmp_edit_or_answer(callback, fragments[0], None, parse_mode=None)
                for frag in fragments[1:-1]:
                    await callback.message.answer(frag)
                await callback.message.answer(fragments[-1], reply_markup=hub_kb)

        elif mode == "lst":
            page = int(pieces[2]) if len(pieces) > 2 and pieces[2].isdigit() else 0
            await db.sync_campaign_catalog()
            total_entries = await db.count_campaigns()
            if total_entries == 0:
                await sender("Кампаний пока нет — добавьте ?start= в ссылке и хотя бы один /start пользователей.", hub_kb)
                return

            pages = max(1, (total_entries + CMP_LIST_PAGE - 1) // CMP_LIST_PAGE)
            if page >= pages:
                page = pages - 1

            summaries = await db.fetch_campaign_summaries_page(CMP_LIST_PAGE, page * CMP_LIST_PAGE)
            markup_rows: List[List[types.InlineKeyboardButton]] = []
            for cid, payl, lc, pc in summaries:
                short_pay = payl if len(payl) <= 52 else payl[:49] + "…"
                label = f"👤{lc} 💳{pc}"
                markup_rows.append(
                    [
                        types.InlineKeyboardButton(
                            text=f"{label}: {short_pay}",
                            callback_data=f"CMP|det|{cid}",
                        )
                    ]
                )

            nav_row = []
            if page > 0:
                nav_row.append(
                    types.InlineKeyboardButton(text="« Назад", callback_data=f"CMP|lst|{page-1}")
                )
            if page < pages - 1:
                nav_row.append(
                    types.InlineKeyboardButton(text="Вперёд »", callback_data=f"CMP|lst|{page+1}")
                )
            if nav_row:
                markup_rows.append(nav_row)

            markup_rows.append(
                [
                    types.InlineKeyboardButton(
                        text="⬅️ В раздел кампаний", callback_data="CMP|hub"
                    )
                ]
            )

            markup = types.InlineKeyboardMarkup(inline_keyboard=markup_rows)
            headline = (
                f"Выберите метку кампании (стр. {page+1}/{pages}, всего {total_entries}):\n\n"
            )
            await sender(headline, markup)

        elif mode == "det" and len(pieces) > 2 and pieces[2].isdigit():
            cid = int(pieces[2])
            meta = await db.get_campaign_by_id(cid)
            if not meta:
                await sender("Кампания не найдена (возможно, список устарел).", hub_kb)
                return

            _, payl_raw, lc, pc = meta
            txt = (
                f"📌 Метка: {payl_raw}\n"
                f"👣 First-touch заходов: {lc}\n"
                f"💳 Успешных оплат: {pc}"
            )
            await cmp_edit_or_answer(
                callback,
                txt,
                kb.campaign_detail_keyboard(cid),
                parse_mode=None,
            )

        elif mode == "drq" and len(pieces) > 2 and pieces[2].isdigit():
            cid = int(pieces[2])
            meta = await db.get_campaign_by_id(cid)
            if not meta:
                await sender("Раздел уже удалён или не найден.", hub_kb)
                return
            _, payl_raw, _, _ = meta
            confirm_txt = (
                "Удалить этот раздел из списка кампаний?\n\n"
                f"Метка: {payl_raw}\n\n"
                "Данные пользователей и оплат в базе не удаляются — раздел пропадает из админки; "
                "та же текстовая метка не будет снова добавляться в список автоматически на синхронизации."
            )
            await cmp_edit_or_answer(
                callback,
                confirm_txt,
                kb.campaign_delete_confirm_keyboard(cid),
                parse_mode=None,
            )

        elif mode == "dok" and len(pieces) > 2 and pieces[2].isdigit():
            cid = int(pieces[2])
            payload_removed = await db.purge_campaign_from_admin_lists(cid)
            if payload_removed is None:
                await sender("Не удалось убрать раздел.", hub_kb)
                return
            await cmp_edit_or_answer(
                callback,
                f"Раздел «{payload_removed}» убран из списка.",
                kb.get_campaigns_hub_keyboard(),
                parse_mode=None,
            )

        elif mode == "u" and len(pieces) > 3 and pieces[2].isdigit() and pieces[3].isdigit():
            cid = int(pieces[2])
            page = int(pieces[3])
            meta = await db.get_campaign_by_id(cid)
            if not meta:
                await sender("Кампания не найдена.", kb.get_campaigns_hub_keyboard())
                return
            payload_value = meta[1]
            pay_esc = html.escape(payload_value)
            total_ln = await db.count_landings_exact(payload_value)
            if total_ln == 0:
                await sender(
                    f"Заходов по метке <code>{pay_esc}</code> не найдено.",
                    kb.campaign_detail_keyboard(cid),
                    html=True,
                )
                return

            pages = max(1, (total_ln + CMP_ROWS_USERS - 1) // CMP_ROWS_USERS)
            if page >= pages:
                page = pages - 1

            landed = await db.paginate_users_by_payload_landings(
                payload_value, CMP_ROWS_USERS, page * CMP_ROWS_USERS
            )
            rows_txt = []
            for uid_val, unm, fname, agr, tapped, sub_act, grace_fl in landed:
                flags = []
                if not agr:
                    flags.append("оферта?")
                if tapped:
                    flags.append("«купить»")
                if sub_act:
                    flags.append("подписан")
                if grace_fl:
                    flags.append("grace")
                fl_txt = "[" + ",".join(flags) + "]" if flags else "[]"
                handle = unm or "(нет username)"
                rows_txt.append(
                    f"• <code>{uid_val}</code> @{html.escape(handle)} — {html.escape(fl_txt)}"
                )

            header = (
                f"👣 Кто зашёл первым контактом с <code>{pay_esc}</code> "
                f"(стр. {page+1}/{pages}, всего {total_ln})\n\n"
                + ("\n".join(rows_txt))
            )

            markup_rows = []
            nav_btn = []
            if page > 0:
                nav_btn.append(types.InlineKeyboardButton(text="« Назад", callback_data=f"CMP|u|{cid}|{page-1}"))
            if page < pages - 1:
                nav_btn.append(
                    types.InlineKeyboardButton(text="Вперёд »", callback_data=f"CMP|u|{cid}|{page+1}")
                )
            if nav_btn:
                markup_rows.append(nav_btn)
            markup_rows.append(
                [types.InlineKeyboardButton(text="🔙 Карточка кампании", callback_data=f"CMP|det|{cid}")]
            )
            markup = types.InlineKeyboardMarkup(inline_keyboard=markup_rows)
            await cmp_edit_or_answer(callback, header, markup, parse_mode="HTML")

        elif mode == "p" and len(pieces) > 3 and pieces[2].isdigit() and pieces[3].isdigit():
            cid = int(pieces[2])
            page = int(pieces[3])
            meta = await db.get_campaign_by_id(cid)
            if not meta:
                await sender("Кампания не найдена.", kb.get_campaigns_hub_keyboard())
                return
            payload_value = meta[1]
            pay_esc = html.escape(payload_value)
            total_pay = await db.count_payments_same_payload_exact(payload_value)
            if total_pay == 0:
                await sender(
                    f"Успешных оплат для <code>{pay_esc}</code> пока не найдено.",
                    kb.campaign_detail_keyboard(cid),
                    html=True,
                )
                return

            pages = max(1, (total_pay + CMP_ROWS_PAY - 1) // CMP_ROWS_PAY)
            if page >= pages:
                page = pages - 1

            pays = await db.paginate_payments_panel(payload_value, CMP_ROWS_PAY, page * CMP_ROWS_PAY)
            lines = []
            for uid_val, unm, recur, pts, trk in pays:
                when = datetime.utcfromtimestamp(float(pts)).strftime("%Y-%m-%d %H:%M UTC")
                recur_txt = "(рекуррент)" if recur else "(чек)"
                handle = unm or "-"
                tr_short = trk if len(trk) <= 32 else trk[:29] + "…"
                lines.append(
                    f"• <code>{uid_val}</code> @{html.escape(handle)} {recur_txt} {when} "
                    f"track=<code>{html.escape(tr_short)}</code>"
                )

            header = (
                f"💳 Оплаты с меткой <code>{pay_esc}</code> "
                f"(стр. {page+1}/{pages}, всего {total_pay})\n\n" + ("\n".join(lines))
            )

            nav_row = []
            if page > 0:
                nav_row.append(types.InlineKeyboardButton(text="« Назад", callback_data=f"CMP|p|{cid}|{page-1}"))
            if page < pages - 1:
                nav_row.append(types.InlineKeyboardButton(text="Вперёд »", callback_data=f"CMP|p|{cid}|{page+1}"))
            rows_mk = []
            if nav_row:
                rows_mk.append(nav_row)
            rows_mk.append(
                [
                    types.InlineKeyboardButton(
                        text="🔙 Карточка кампании", callback_data=f"CMP|det|{cid}"
                    )
                ]
            )
            await cmp_edit_or_answer(
                callback,
                header,
                types.InlineKeyboardMarkup(inline_keyboard=rows_mk),
                parse_mode="HTML",
            )

        else:
            await sender("Неизвестная команда CMP. Откройте раздел заново.", hub_kb)

    except Exception as exc:
        logger.error("CMP dashboard handler failed: %s", exc)
        await callback.message.answer(f"⚠️ Ошибка CMP-меню: {exc}")

# --- Main ---
async def main():
    if not CHANNEL_ID:
        logger.critical("CHANNEL_ID не задан в .env. Проверьте файл .env в папке с ботом.")
        raise SystemExit(1)
    logger.info(f"Канал для инвайтов и кика (один и тот же): CHANNEL_ID={CHANNEL_ID}")
    logger.info("BePaid test_mode=%s (в .env: BEPAID_TEST=1 для тестового магазина)", BEPAID_TEST)

    await db.init_db()
    
    # Создаем aiohttp приложение для вебхуков
    app = web.Application()
    app.router.add_post(WEBHOOK_PATH, bepaid_webhook_handler)
    
    # Запускаем сервер в фоне
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_SERVER_HOST, WEB_SERVER_PORT)
    await site.start()
    
    print(f"Bot started. Webhook listening on {WEBHOOK_HOST}{WEBHOOK_PATH}")
    
    # Запускаем планировщик
    asyncio.create_task(check_recurring_payments())
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
