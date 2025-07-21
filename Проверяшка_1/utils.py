import re
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram import Update
from telegram.ext import ContextTypes
from states import CHOOSE_ROLE, STUDENT_MAIN, TEACHER_MAIN
from functools import wraps
from tenacity import retry, stop_after_attempt, wait_fixed
from telegram.error import BadRequest, NetworkError, TimedOut
import logging

logger = logging.getLogger(__name__)

def create_back_button() -> InlineKeyboardButton:
    return InlineKeyboardButton("🔙 Назад", callback_data="back")

def create_navigation_buttons(current: int, total: int) -> list:
    buttons = []
    if current > 0:
        buttons.append(InlineKeyboardButton("◀️ Назад", callback_data="prev"))
    if current < total - 1:
        buttons.append(InlineKeyboardButton("Вперёд ▶️", callback_data="next"))
    return buttons

def validate_class(class_str: str) -> bool:
    return re.fullmatch(r"^(5|6|7|8|9|10|11)$", class_str.strip()) is not None

def generate_test_report(test_data: dict) -> str:
    test_count = sum(1 for q in test_data["questions"] if q["type"] == "test")
    open_count = len(test_data["questions"]) - test_count
    return (
        f"📋 Тест: {test_data['name']}\n"
        f"🏫 Классы: {', '.join(test_data['classes'])}\n"
        f"📚 Предмет: {test_data['subject']}\n"
        f"🔢 Вопросов: {len(test_data['questions'])}\n"
        f"📝 Тестовые: {test_count}\n"
        f"📄 Развернутые: {open_count}"
    )

def push_state(context: ContextTypes.DEFAULT_TYPE, state: int):
    """Добавляет состояние в стек, избегая дублирования."""
    if "state_history" not in context.user_data:
        context.user_data["state_history"] = []
    # Добавляем только если последнее состояние отличается
    if not context.user_data["state_history"] or context.user_data["state_history"][-1] != state:
        context.user_data["state_history"].append(state)
    logger.debug(f"Добавлено состояние {state}, state_history: {context.user_data['state_history']}")

def pop_state(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Извлекает последнее состояние из стека."""
    if context.user_data.get("state_history"):
        state = context.user_data["state_history"].pop()
        logger.debug(f"Извлечено состояние {state}, state_history: {context.user_data['state_history']}")
        return state
    logger.debug("Стек состояний пуст")
    return None

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Завершает диалог, очищая временные данные."""
    keys_to_clear = [
        "current_test_id", "tests_page", "students_page", "pending_notifications",
        "current_result_id", "current_question_idx", "temp_test_id", "state_history",
        "answers_page", "appeals_page", "question_text_part"
    ]
    for key in keys_to_clear:
        context.user_data.pop(key, None)
    await update.message.reply_text("❌ Действие отменено", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def back_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возвращает к предыдущему состоянию."""
    query = update.callback_query
    await query.answer()
    
    prev_state = pop_state(context)
    logger.debug(f"Нажата кнопка 'Назад', предыдущее состояние: {prev_state}, user_data: {context.user_data}")
    
    try:
        if prev_state in [STUDENT_MAIN, TEACHER_MAIN, None]:
            # Возвращаемся к CHOOSE_ROLE
            await query.edit_message_text(
                "🎓 Возвращаемся к выбору роли...",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Учащийся", callback_data='student'),
                     InlineKeyboardButton("Учитель", callback_data='teacher')]
                ])
            )
            context.user_data["state_history"] = [CHOOSE_ROLE]
            return CHOOSE_ROLE
        else:
            logger.warning(f"Неизвестное состояние {prev_state}, возврат к CHOOSE_ROLE")
            await query.edit_message_text(
                "🎓 Возвращаемся к выбору роли...",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Учащийся", callback_data='student'),
                     InlineKeyboardButton("Учитель", callback_data='teacher')]
                ])
            )
            context.user_data["state_history"] = [CHOOSE_ROLE]
            return CHOOSE_ROLE
    except BadRequest as e:
        if "Message is not modified" in str(e):
            logger.debug("Сообщение не изменилось, игнорируем")
            context.user_data["state_history"] = [CHOOSE_ROLE]
            return CHOOSE_ROLE
        raise

def network_retry(func):
    @wraps(func)
    @retry(stop=stop_after_attempt(3), wait=wait_fixed(1))
    async def wrapper(self, *args, **kwargs):
        try:
            return await func(self, *args, **kwargs)
        except (NetworkError, TimedOut) as e:
            logger.warning(f"Network error: {e}")
            raise
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
            logger.info("Игнорируем ошибку 'Message is not modified'")
    return wrapper