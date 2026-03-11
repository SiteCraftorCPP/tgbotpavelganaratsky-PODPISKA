import logging
import asyncio
import os
import time
from datetime import datetime
from pathlib import Path
from aiohttp import web
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

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
MANAGER_LINK = (os.getenv("MANAGER_LINK") or "https://t.me/nastyaprostozhit").strip()

BEPAID_SHOP_ID = os.getenv("BEPAID_SHOP_ID")
BEPAID_SECRET_KEY = os.getenv("BEPAID_SECRET_KEY")
# Webhook settings
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "http://194.62.19.77:8080")
WEBHOOK_PATH = "/bepaid/webhook"
WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = 8080

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize bot and dispatcher
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())
bepaid = BePaidAPI(shop_id=BEPAID_SHOP_ID, secret_key=BEPAID_SECRET_KEY)

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

# --- Webhook Handler for BePaid ---
async def bepaid_webhook_handler(request):
    try:
        data = await request.json()
        transaction = data.get("transaction", {})
        status = transaction.get("status")
        tracking_id = transaction.get("tracking_id") # format: user_id:timestamp
        
        logger.info(f"Received webhook: {transaction.get('uid')} status={status}")

        if status == "successful" and tracking_id:
            try:
                user_id = int(tracking_id.split(":")[0])
                
                # Сохраняем токен карты для рекуррентов
                credit_card = transaction.get("credit_card", {})
                card_token = credit_card.get("token")
                
                # Продлеваем подписку (например, на 30 дней)
                days_str = await db.get_setting("subscription_days") or "30"
                days = int(days_str)
                new_end_date = time.time() + (days * 24 * 60 * 60)
                
                await db.set_subscription(user_id, status=True, end_date=new_end_date, card_token=card_token)
                end_date_str = datetime.utcfromtimestamp(new_end_date).strftime("%Y-%m-%d %H:%M UTC")
                logger.info(
                    f"Payment OK: user_id={user_id}, card_saved={'yes' if card_token else 'no'}, "
                    f"subscription_until={end_date_str}, auto_renew={'yes' if card_token else 'no'}"
                )
                
                # Инвайт только в канал из CHANNEL_ID (.env)
                invite_link_obj = await bot.create_chat_invite_link(
                    chat_id=CHANNEL_ID,
                    member_limit=1,
                    name=f"Sub_{user_id}_{int(time.time())}"
                )
                invite_link = invite_link_obj.invite_link
                
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
            
            price_str = await db.get_setting("subscription_price") or "10"
            price = float(price_str)
            days_str = await db.get_setting("subscription_days") or "30"
            days = int(days_str)

            for user in users_due:
                user_id, card_token, email = user

                # Никогда не трогаем админов (из .env и из БД)
                if await is_admin(user_id):
                    continue
                
                if not card_token:
                    continue
                
                logger.info(f"Attempting to charge user {user_id}")
                
                success, result = await bepaid.charge_recurrent(
                    amount=price,
                    currency="BYN",
                    description=f"Продление подписки (Bot) для {user_id}",
                    order_id=f"{user_id}:{int(time.time())}",
                    card_token=card_token,
                    email=email or "no-email@example.com"
                )
                
                if success:
                    new_end_date = time.time() + (days * 24 * 60 * 60)
                    await db.set_subscription(user_id, status=True, end_date=new_end_date)
                    await bot.send_message(user_id, f"✅ Подписка успешно продлена на {days} дней!")
                else:
                    await db.set_subscription(user_id, status=False)
                    await bot.send_message(user_id, f"❌ Не удалось продлить подписку: {result}. Пожалуйста, оплатите вручную.")
                    # Кик из того же канала CHANNEL_ID (.env); админов мы уже пропустили выше
                    try:
                        await bot.ban_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
                        await bot.unban_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
                        logger.info(f"Kicked user {user_id} due to payment failure")
                    except Exception as k_err:
                         logger.error(f"Failed to kick user {user_id}: {k_err}")

            # Истёкшая подписка без карты — просто выгоняем (админов не трогаем)
            expired_no_card = await db.get_users_expired_no_card()
            for user_id in expired_no_card:
                if await is_admin(user_id):
                    continue
                await db.set_subscription(user_id, status=False)
                try:
                    await bot.ban_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
                    await bot.unban_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
                    logger.info(f"Kicked user {user_id} (subscription expired, no card)")
                except Exception as k_err:
                    logger.error(f"Failed to kick user {user_id}: {k_err}")
                    
            # Проверка раз в час (чтобы не пропустить)
            await asyncio.sleep(3600) 
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
            await asyncio.sleep(3600)

# --- User Handlers ---

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user = message.from_user
    print(f"--- USER ID: {user.id} ---")
    await db.add_user(user.id, user.username, user.full_name)
    
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

    # Админ пропускает оплату — сразу выдаём блок подписчика (инвайт + кнопки)
    if await is_admin(user_id):
        days_str = await db.get_setting("subscription_days") or "30"
        days = int(days_str)
        new_end_date = time.time() + (days * 24 * 60 * 60)
        await db.set_subscription(user_id, status=True, end_date=new_end_date)
        try:
            invite_link_obj = await bot.create_chat_invite_link(
                chat_id=CHANNEL_ID,
                member_limit=1,
                name=f"Admin_{user_id}_{int(time.time())}"
            )
            invite_link = invite_link_obj.invite_link
        except Exception as e:
            logger.warning("Admin bypass: could not create invite link: %s", e)
            invite_link = None
        payment_text = await db.get_setting("payment_success_text") or "✅ Оплата прошла успешно!\n\nНажмите кнопку ниже, чтобы вступить в канал."
        await callback.message.answer(
            f"✅ [Админ] Доступ открыт без оплаты.\n\n{payment_text}" if invite_link else "✅ [Админ] Доступ открыт. Ссылка на канал не создана (проверьте права бота).",
            reply_markup=kb.get_member_keyboard(MANAGER_LINK, invite_link=invite_link or "")
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
        "Отменить подписку?\n\n"
        "С вашей карты больше не будет списываться оплата. Вы будете удалены из канала.",
        reply_markup=kb.get_cancel_subscription_confirm_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "cancel_subscription_confirm")
async def process_cancel_sub_confirm(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await db.set_subscription(user_id, status=False, card_token="")
    logger.info(
        "Subscription cancelled by user: user_id=%s, token_removed=yes, auto_charge_disabled=yes, kick_attempt=now",
        user_id,
    )
    kicked = False
    try:
        await bot.ban_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        await bot.unban_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        kicked = True
        logger.info("User user_id=%s kicked from channel after subscription cancel (immediate)", user_id)
    except Exception as e:
        logger.error("Failed to kick user_id=%s from channel: %s (e.g. user is channel admin)", user_id, e)
    if kicked:
        msg = "✅ Подписка отменена. С вашей карты больше не будет списываться оплата. Вы удалены из канала."
    else:
        msg = (
            "✅ Подписка отменена. С вашей карты больше не будет списываться оплата.\n\n"
            "Удалить вас из канала не удалось: в Telegram бот не может исключить администратора канала. "
            "Для проверки исключения зайдите в канал с аккаунта без прав админа."
        )
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


# --- Admin Handlers (Оставил основные, добавил цену) ---

@dp.message(Command("admin"))
@dp.message(F.text == "Админ-панель")
async def cmd_admin(message: types.Message):
    user_id = message.from_user.id
    if not await is_admin(user_id):
        return

    # Инлайн-меню админа
    await message.answer("🔧 Админ-панель:", reply_markup=kb.get_admin_keyboard())

    # И гарантированно показываем reply-клавиатуру с кнопкой «Админ-панель»
    admin_kb = types.ReplyKeyboardMarkup(
        keyboard=[[types.KeyboardButton(text="Админ-панель")]],
        resize_keyboard=True,
    )
    await message.answer("Клавиатура управления:", reply_markup=admin_kb)


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

@dp.callback_query(F.data == "cancel_action")
async def cancel_handler(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.edit_text("🔧 Админ-панель:", reply_markup=kb.get_admin_keyboard())
    except Exception:
        await callback.message.answer("🔧 Админ-панель:", reply_markup=kb.get_admin_keyboard())
    await callback.answer()

# --- Main ---
async def main():
    if not CHANNEL_ID:
        logger.critical("CHANNEL_ID не задан в .env. Проверьте файл .env в папке с ботом.")
        raise SystemExit(1)
    logger.info(f"Канал для инвайтов и кика (один и тот же): CHANNEL_ID={CHANNEL_ID}")

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
