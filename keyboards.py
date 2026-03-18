from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

# --- Block 1: Welcome & Consent ---
def get_welcome_keyboard():
    # Links are in text, here only "Agreed"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Ознакомился", callback_data="agreed_to_terms")]
    ])
    return keyboard

# --- Block 2: Subscription & Manager ---
def get_subscription_keyboard(manager_link: str):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        # Placeholder for payment
        [InlineKeyboardButton(text="💳 Оформить подписку", callback_data="simulate_payment")],
        
        [InlineKeyboardButton(text="👤 Вопрос менеджеру", url=manager_link)]
    ])
    return keyboard

# --- Block 3: Support & Management (After Subscription) ---
def get_member_keyboard(manager_link: str, invite_link: str = None):
    # Support links to admin/manager
    buttons = []
    
    if invite_link:
        buttons.append([InlineKeyboardButton(text="🔗 Вступить в канал", url=invite_link)])
    
    buttons.append([InlineKeyboardButton(text="🆘 Поддержка", url=manager_link)])
    buttons.append([InlineKeyboardButton(text="❌ Отменить подписку", callback_data="cancel_subscription")])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    return keyboard


def get_cancel_subscription_confirm_keyboard():
    """Инлайн-кнопки Да/Нет для подтверждения отмены подписки."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Да", callback_data="cancel_subscription_confirm"),
            InlineKeyboardButton(text="Нет", callback_data="cancel_subscription_abort"),
        ]
    ])


# --- Admin Keyboards ---
def get_admin_keyboard():
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="📝 Изм. приветствие (Текст)", callback_data="admin_edit_welcome_text")],
        [InlineKeyboardButton(text="🖼 Изм. приветствие (Фото)", callback_data="admin_edit_welcome_photo")],
        [InlineKeyboardButton(text="📝 Изм. текст после оплаты", callback_data="admin_edit_payment_text")],
        [InlineKeyboardButton(text="💰 Изм. цену подписки", callback_data="admin_edit_price")]
    ])
    return keyboard

def get_cancel_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="cancel_action")]])
