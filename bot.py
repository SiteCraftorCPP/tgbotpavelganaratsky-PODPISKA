import logging
import asyncio
import os
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

import database as db
import keyboards as kb

# Load environment variables
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
# Ссылка на админа (Вопрос менеджеру / Поддержка). По умолчанию — открыть чат с админом по ID
MANAGER_LINK = os.getenv("MANAGER_LINK") or "tg://user?id=6933111964"

# Захардкоженные ссылки в приветствии (не редактируются в админке)
WELCOME_LINKS_HTML = """• <a href="https://psyprosto-help.by/policy">Политика конфиденциальности</a>
• <a href="https://psyprosto-help.by/polozhenie">Положение</a>
• <a href="https://school.psy-prosto.school/oferta">Оферта</a>"""

# Configure logging
logging.basicConfig(level=logging.INFO)

# Initialize bot and dispatcher
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# States
class AdminStates(StatesGroup):
    waiting_for_broadcast = State()
    waiting_for_welcome_text = State()
    waiting_for_welcome_photo = State()
    waiting_for_cancel_text = State()
    waiting_for_payment_text = State()

# --- Helpers ---
async def is_admin(user_id: int):
    admins = await db.get_admins()
    env_admins = os.getenv("ADMIN_IDS", "").split(",")
    # Удаляем пробелы и пустые строки
    env_admins = [x.strip() for x in env_admins if x.strip()]
    
    if str(user_id) in env_admins:
        return True
    return user_id in admins

# --- User Handlers ---

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user = message.from_user
    # Пишем ID в консоль для удобства
    print(f"--- USER ID: {user.id} ---")
    
    await db.add_user(user.id, user.username, user.full_name)
    
    intro = await db.get_setting("welcome_text") or "Добро пожаловать в наш бот!\n\nПожалуйста, ознакомьтесь с правилами ниже.\n\nНажмите кнопку ниже, чтобы продолжить."
    if WELCOME_LINKS_HTML not in intro:
        full_welcome = intro.rstrip() + "\n\n" + WELCOME_LINKS_HTML
    else:
        full_welcome = intro
    welcome_photo = await db.get_setting("welcome_photo")
    
    if welcome_photo:
        await message.answer_photo(
            photo=welcome_photo, 
            caption=full_welcome, 
            parse_mode="HTML", 
            reply_markup=kb.get_welcome_keyboard()
        )
    else:
        await message.answer(
            text=full_welcome, 
            parse_mode="HTML", 
            disable_web_page_preview=True, 
            reply_markup=kb.get_welcome_keyboard()
        )

@dp.callback_query(F.data == "agreed_to_terms")
async def process_agreement(callback: types.CallbackQuery):
    await db.set_agreed(callback.from_user.id)
    await callback.message.answer("Спасибо! Выберите действие:", reply_markup=kb.get_subscription_keyboard(MANAGER_LINK))
    await callback.answer()

@dp.callback_query(F.data == "simulate_payment")
async def process_payment_simulation(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await db.set_subscription(user_id, True)
    
    try:
        invite_link_obj = await bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            member_limit=1,
            name=f"Sub_{user_id}"
        )
        invite_link = invite_link_obj.invite_link
        
        # Ссылка спрятана в кнопку в get_member_keyboard
        payment_text = await db.get_setting("payment_success_text") or "✅ Оплата прошла успешно!\n\nНажмите кнопку ниже, чтобы вступить в канал."
        await callback.message.answer(
            payment_text,
            reply_markup=kb.get_member_keyboard(MANAGER_LINK, invite_link=invite_link)
        )
    except Exception as e:
        print(f"Error creating invite link: {e}")
        payment_text = await db.get_setting("payment_success_text") or "✅ Оплата прошла успешно!\n\nНажмите кнопку ниже, чтобы вступить в канал."
        await callback.message.answer(
            payment_text + "\n\n(Ошибка создания ссылки: проверьте права бота в канале.)",
            reply_markup=kb.get_member_keyboard(MANAGER_LINK)
        )
    
    await callback.answer()

@dp.callback_query(F.data == "cancel_subscription")
async def process_cancel_sub(callback: types.CallbackQuery):
    cancel_text = await db.get_setting("cancel_text")
    await callback.message.answer(cancel_text, parse_mode="HTML")
    await callback.answer()

# --- Admin Handlers ---

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    user_id = message.from_user.id
    if not await is_admin(user_id):
        return
    
    await message.answer("🔧 Админ-панель:", reply_markup=kb.get_admin_keyboard())

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

@dp.callback_query(F.data == "admin_edit_cancel_text")
async def admin_edit_cancel_text(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id): return
    await state.set_state(AdminStates.waiting_for_cancel_text)
    await callback.message.answer("Введите новый текст инструкции по отмене подписки:", reply_markup=kb.get_cancel_keyboard())
    await callback.answer()

@dp.message(AdminStates.waiting_for_cancel_text)
async def admin_save_cancel_text(message: types.Message, state: FSMContext):
    await db.set_setting("cancel_text", message.text)
    await message.answer("✅ Текст отмены подписки обновлен.")
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
    await db.init_db()
    
    print("Bot started...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
