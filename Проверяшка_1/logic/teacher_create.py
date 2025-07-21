from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
    CommandHandler
)
from telegram.error import BadRequest, NetworkError, TimedOut
from datetime import datetime
import uuid
import logging
import re
from typing import Any, Dict, List, Optional
from functools import wraps
from tenacity import retry, stop_after_attempt, wait_fixed
from states import (
    TEACHER_MAIN, TEACHER_SELECT_SUBJECT, TEACHER_SELECT_CLASS, TEACHER_ENTER_NAME,
    TEACHER_QUESTION_TYPE, TEACHER_ENTER_QUESTION, TEACHER_ENTER_CORRECT_ANSWER,
    TEACHER_ADD_OPTIONS, TEACHER_ADD_COMMENT, TEACHER_FINISH_CREATION,
    TEACHER_EDIT_QUESTIONS, TEACHER_EDIT_QUESTION_PART, TEACHER_EDIT_QUESTION_CONTENT,
    TEACHER_GLOBAL_COMMENT, TEACHER_FINAL_CONFIRM, TEACHER_EDIT_SUBJECT,
    TEACHER_EDIT_CLASSES, TEACHER_EDIT_GLOBAL_COMMENT
)
from database import Database
from utils import create_back_button, validate_class

logger = logging.getLogger(__name__)

# Константы
MAX_MESSAGE_LENGTH = 4096
TEXT_PART_LENGTH = 1000

# Утилиты
def sanitize_input(text: str) -> str:
    """Санитизация входных данных для предотвращения проблем с отображением."""
    if not text:
        return text
    text = re.sub(r'[<>|&]', '', text)
    text = re.sub(r'\s+', ' ', text.strip())
    return text[:200]

def split_message(text: str, max_length: int) -> List[str]:
    """Разбиение длинного сообщения на части."""
    parts = []
    current_part = ""
    for line in text.split("\n"):
        if len(current_part) + len(line) + 1 > max_length:
            parts.append(current_part)
            current_part = line + "\n"
        else:
            current_part += line + "\n"
    if current_part:
        parts.append(current_part)
    return parts

class StateManager:
    """Управление стеком состояний и промежуточными данными."""
    def __init__(self, context: ContextTypes.DEFAULT_TYPE):
        self.context = context
        if "state_stack" not in self.context.user_data:
            self.context.user_data["state_stack"] = []
        if "state_data" not in self.context.user_data:
            self.context.user_data["state_data"] = {}

    def push(self, state: str):
        """Добавление состояния в стек."""
        self.context.user_data["state_stack"].append(state)
        logger.debug(f"Pushed state: {state}")

    def pop(self) -> Optional[str]:
        """Извлечение последнего состояния из стека."""
        return self.context.user_data["state_stack"].pop() if self.context.user_data["state_stack"] else None

    def current(self) -> Optional[str]:
        """Получение текущего состояния."""
        return self.context.user_data["state_stack"][-1] if self.context.user_data["state_stack"] else None

    def set_data(self, state: str, key: str, value: Any):
        """Сохранение данных для указанного состояния."""
        if state not in self.context.user_data["state_data"]:
            self.context.user_data["state_data"][state] = {}
        self.context.user_data["state_data"][state][key] = value
        logger.debug(f"Set data for state {state}, key {key}: {value}")

    def get_data(self, state: str, key: str, default: Any = None) -> Any:
        """Получение данных для указанного состояния."""
        return self.context.user_data["state_data"].get(state, {}).get(key, default)

    def clear_data(self):
        """Очистка всех данных."""
        self.context.user_data["state_data"].clear()
        logger.debug("Cleared all state data")

    def clear_state_data(self, state: str):
        """Очистка данных для конкретного состояния."""
        if state in self.context.user_data["state_data"]:
            del self.context.user_data["state_data"][state]
            logger.debug(f"Cleared data for state: {state}")

class TeacherTestMessageManager:
    """Управление текстовыми сообщениями."""
    MESSAGES = {
        "select_subject": "📚 Выберите предмет для теста:",
        "select_class": "✅ Предмет: {subject}\n🏫 Введите классы через запятую (например, 5,6,7):\n{classes}",
        "invalid_class": "❌ Неверный формат! Пример: 5,6,7\nВведите классы заново:",
        "enter_name": "🏫 Классы: {classes}\n✏️ Введите название теста:\n{name}",
        "invalid_name": "❌ Слишком длинное название! Макс. 100 символов\nВведите название заново:",
        "select_question_type": (
            "✅ Название теста: {name}\n📝 Инструкция по созданию теста:\n\n"
            "1. Выберите тип вопроса\n2. Следуйте подсказкам для каждого типа\n"
            "3. Добавляйте необходимое количество вопросов\n\n❓ Выберите тип вопроса:"
        ),
        "enter_question": "✍️ Введите текст вопроса:\n{current}",
        "enter_correct_answer": "✍️ Вопрос:\n{question}\n{action}:\n{current}",
        "invalid_correct_answer": "❌ Введите правильный ответ перед продолжением!",
        "enter_options": (
            "✅ Правильный ответ:\n{correct_answer}\n📋 Введите дополнительные варианты ответов через запятую (1-6):\n{current}"
        ),
        "invalid_options": "❌ Нужно от 1 до 6 уникальных вариантов, не включая правильный ответ!\nВведите варианты заново:",
        "enter_comment": "✅ Эталонный ответ:\n{correct_answer}\n💡 Введите комментарий для проверки:\n{current}",
        "question_added": "✅ Вопрос успешно добавлен!",
        "no_questions": "❌ Добавьте хотя бы один вопрос!",
        "question_list": (
            "📋 Текущий тест: {name}\nПредмет: {subject}\nКлассы: {classes}\n\nСписок вопросов:"
        ),
        "edit_question": "✏️ Редактирование вопроса {idx}",
        "edit_question_part": "Введите новый {part}:\n{current}",
        "changes_saved": "✅ Изменения сохранены!",
        "global_comment": "💡 Введите глобальный комментарий к тесту (или /skip чтобы пропустить):\n{current}",
        "comment_saved": "💡 Комментарий сохранен",
        "comment_skipped": "💡 Комментарий пропущен",
        "final_confirmation": (
            "📋 Итоговые данные теста:\n\n"
            "Название: {name}\nПредмет: {subject}\nКлассы: {classes}\n"
            "Вопросов: {question_count}\nГлобальный комментарий: {comment}"
        ),
        "test_created": "✅ Тест '{name}' успешно создан!",
        "canceled": "❌ Создание теста отменено.",
        "error": "❌ Произошла ошибка. Пожалуйста, начните заново."
    }

    @staticmethod
    def get_message(key: str, **kwargs) -> str:
        message = TeacherTestMessageManager.MESSAGES[key].format(**kwargs)
        return message[:MAX_MESSAGE_LENGTH] if len(message) > MAX_MESSAGE_LENGTH else message

class TeacherTestKeyboardManager:
    """Управление клавиатурами."""
    @staticmethod
    def create_back_button(action: str = "back") -> InlineKeyboardButton:
        return InlineKeyboardButton("🔙 Назад", callback_data=action)

    @staticmethod
    def create_subject_selection() -> InlineKeyboardMarkup:
        subjects = ["Математика", "Физика", "История", "Информатика"]
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(subj, callback_data=f"subj_{subj}") for subj in subjects[i:i+2]]
            for i in range(0, len(subjects), 2)
        ] + [[TeacherTestKeyboardManager.create_back_button()]])

    @staticmethod
    def create_class_input(classes: List[str]) -> InlineKeyboardMarkup:
        buttons = []
        if classes:
            buttons.append([InlineKeyboardButton("▶ Вперед", callback_data="next")])
        buttons.append([TeacherTestKeyboardManager.create_back_button()])
        return InlineKeyboardMarkup(buttons)

    @staticmethod
    def create_name_input(name: str) -> InlineKeyboardMarkup:
        buttons = []
        if name:
            buttons.append([InlineKeyboardButton("▶ Вперед", callback_data="next")])
        buttons.append([TeacherTestKeyboardManager.create_back_button()])
        return InlineKeyboardMarkup(buttons)

    @staticmethod
    def create_question_type_selection() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("Тестовый вопрос", callback_data="type_test"),
             InlineKeyboardButton("Развернутый ответ", callback_data="type_open")],
            [InlineKeyboardButton("🏁 Завершить создание", callback_data="finish_test")],
            [TeacherTestKeyboardManager.create_back_button()]
        ])

    @staticmethod
    def create_question_input(text: str) -> InlineKeyboardMarkup:
        buttons = []
        if text:
            buttons.append([InlineKeyboardButton("▶ Вперед", callback_data="next")])
        buttons.append([TeacherTestKeyboardManager.create_back_button()])
        return InlineKeyboardMarkup(buttons)

    @staticmethod
    def create_correct_answer_input(correct_answer: str) -> InlineKeyboardMarkup:
        buttons = []
        if correct_answer:
            buttons.append([InlineKeyboardButton("▶ Вперед", callback_data="next")])
        buttons.append([TeacherTestKeyboardManager.create_back_button()])
        return InlineKeyboardMarkup(buttons)

    @staticmethod
    def create_options_input(options: List[str]) -> InlineKeyboardMarkup:
        buttons = []
        if options:
            buttons.append([InlineKeyboardButton("▶ Вперед", callback_data="next")])
        buttons.append([TeacherTestKeyboardManager.create_back_button()])
        return InlineKeyboardMarkup(buttons)

    @staticmethod
    def create_comment_input(comment: str) -> InlineKeyboardMarkup:
        buttons = []
        if comment:
            buttons.append([InlineKeyboardButton("▶ Вперед", callback_data="next")])
        buttons.append([TeacherTestKeyboardManager.create_back_button()])
        return InlineKeyboardMarkup(buttons)

    @staticmethod
    def create_finalization_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Добавить вопрос", callback_data="add_another"),
             InlineKeyboardButton("✏️ Редактировать вопросы", callback_data="edit_questions")],
            [InlineKeyboardButton("🏁 Завершить создание", callback_data="finish_test")],
            [TeacherTestKeyboardManager.create_back_button()]
        ])

    @staticmethod
    def create_question_list(questions: List[dict]) -> InlineKeyboardMarkup:
        keyboard = [
            [InlineKeyboardButton(f"Вопрос {i+1} ({q['type']})", callback_data=f"edit_{i}")]
            for i, q in enumerate(questions)
        ]
        keyboard += [
            [InlineKeyboardButton("➕ Добавить вопрос", callback_data="add_another"),
             InlineKeyboardButton("🏁 Завершить создание", callback_data="finish_test")],
            [TeacherTestKeyboardManager.create_back_button()]
        ]
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def create_edit_question_menu(q_type: str) -> InlineKeyboardMarkup:
        buttons = [
            [InlineKeyboardButton("📝 Текст вопроса", callback_data="edit_text"),
             InlineKeyboardButton("✅ Правильный ответ", callback_data="edit_correct")],
            [InlineKeyboardButton("📋 Варианты ответов", callback_data="edit_options")
             if q_type == "test" else InlineKeyboardButton("💬 Комментарий", callback_data="edit_comment")]
        ]
        buttons.append([TeacherTestKeyboardManager.create_back_button()])
        return InlineKeyboardMarkup(buttons)

    @staticmethod
    def create_final_confirmation_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Редактировать название", callback_data="edit_name"),
             InlineKeyboardButton("📚 Изменить предмет", callback_data="edit_subject")],
            [InlineKeyboardButton("🏫 Изменить классы", callback_data="edit_classes"),
             InlineKeyboardButton("❓ Редактировать вопросы", callback_data="edit_questions")],
            [InlineKeyboardButton("💬 Изменить комментарий", callback_data="edit_global_comment")],
            [InlineKeyboardButton("✅ Подтвердить создание", callback_data="confirm_test")],
            [TeacherTestKeyboardManager.create_back_button()]
        ])

class TeacherTestValidator:
    """Валидация данных теста."""
    @staticmethod
    def validate_classes(classes: List[str]) -> bool:
        return all(validate_class(c) for c in classes)

    @staticmethod
    def validate_name(name: str) -> bool:
        return len(name) <= 100

    @staticmethod
    def validate_options(options: List[str], correct_answer: str) -> bool:
        all_options = options + [correct_answer]
        if len(all_options) < 2 or len(all_options) > 7:
            return False
        if len(set(all_options)) != len(all_options):
            return False
        return True

    @staticmethod
    def validate_test(test: dict) -> bool:
        return (
            test.get("subject") and
            test.get("classes") and
            test.get("name") and
            test.get("questions") and
            test.get("teacher_id")
        )

class TeacherTestCreator:
    """Класс для создания тестов учителем."""
    def __init__(self, db):
        self.db = db

    @staticmethod
    def network_retry(func):
        @wraps(func)
        @retry(stop=stop_after_attempt(3), wait=wait_fixed(1))
        async def wrapper(self, *args, **kwargs):
            try:
                return await func(self, *args, **kwargs)
            except (NetworkError, TimedOut) as e:
                logger.warning(f"Network error in {func.__name__}: {e}, retrying...")
                raise
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    logger.error(f"BadRequest in {func.__name__}: {e}")
                return
        return wrapper

    def reset_state(self, context: ContextTypes.DEFAULT_TYPE):
        """Сброс состояния теста."""
        context.user_data["current_test"] = {
            "id": None,
            "subject": None,
            "classes": [],
            "name": None,
            "questions": [],
            "global_comment": None,
            "teacher_id": None,
            "created_at": None
        }
        context.user_data["editing_question_idx"] = -1
        state_manager = StateManager(context)
        state_manager.clear_data()

    @network_retry
    async def safe_edit_message(self, query, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None):
        """Безопасное редактирование сообщения."""
        if query.message.text == text and query.message.reply_markup == reply_markup:
            logger.debug("Сообщение не требует изменений")
            return
        await query.edit_message_text(text[:MAX_MESSAGE_LENGTH], reply_markup=reply_markup)

    @network_retry
    async def safe_reply_text(self, message, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None):
        """Безопасная отправка ответа."""
        await message.reply_text(text[:MAX_MESSAGE_LENGTH], reply_markup=reply_markup)

    @network_retry
    async def start_creation(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Начало создания теста."""
        query = update.callback_query
        await query.answer()
        self.reset_state(context)

        context.user_data["current_test"]["teacher_id"] = str(update.effective_user.id)
        state_manager = StateManager(context)
        state_manager.push(TEACHER_SELECT_SUBJECT)

        await self.safe_edit_message(
            query,
            TeacherTestMessageManager.get_message("select_subject"),
            TeacherTestKeyboardManager.create_subject_selection()
        )
        return TEACHER_SELECT_SUBJECT

    @network_retry
    async def process_subject(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Обработка выбора предмета."""
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)
        subject = sanitize_input(query.data.split("_")[1])
        state_manager.set_data(TEACHER_SELECT_SUBJECT, "subject", subject)
        context.user_data["current_test"]["subject"] = subject

        state_manager.push(TEACHER_SELECT_CLASS)
        classes = context.user_data["current_test"]["classes"]
        await self.safe_edit_message(
            query,
            TeacherTestMessageManager.get_message(
                "select_class",
                subject=subject,
                classes=f"Ранее введено: {', '.join(classes)}" if classes else ""
            ),
            TeacherTestKeyboardManager.create_class_input(classes)
        )
        return TEACHER_SELECT_CLASS

    @network_retry
    async def process_class(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Обработка ввода классов."""
        state_manager = StateManager(context)
        class_input = sanitize_input(update.message.text)
        classes = [c.strip() for c in class_input.split(',')]

        if not TeacherTestValidator.validate_classes(classes):
            await self.safe_reply_text(
                update.message,
                TeacherTestMessageManager.get_message("invalid_class"),
                TeacherTestKeyboardManager.create_back_button()
            )
            return TEACHER_SELECT_CLASS

        state_manager.set_data(TEACHER_SELECT_CLASS, "classes", classes)
        context.user_data["current_test"]["classes"] = classes
        state_manager.push(TEACHER_ENTER_NAME)

        await self.safe_reply_text(
            update.message,
            TeacherTestMessageManager.get_message(
                "enter_name",
                classes=', '.join(classes),
                name=""
            ),
            TeacherTestKeyboardManager.create_name_input("")
        )
        return TEACHER_ENTER_NAME

    @network_retry
    async def move_forward_class(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Переход к вводу названия теста."""
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)
        classes = state_manager.get_data(TEACHER_SELECT_CLASS, "classes", [])

        if not classes:
            await self.safe_edit_message(
                query,
                TeacherTestMessageManager.get_message("invalid_class"),
                TeacherTestKeyboardManager.create_back_button()
            )
            return TEACHER_SELECT_CLASS

        state_manager.push(TEACHER_ENTER_NAME)
        name = state_manager.get_data(TEACHER_ENTER_NAME, "name", "")
        await self.safe_edit_message(
            query,
            TeacherTestMessageManager.get_message(
                "enter_name",
                classes=', '.join(classes),
                name=f"Ранее введено: {name}" if name else ""
            ),
            TeacherTestKeyboardManager.create_name_input(name)
        )
        return TEACHER_ENTER_NAME

    @network_retry
    async def back_to_subject_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Возврат к выбору предмета."""
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)
        state_manager.pop()  # Удаляем TEACHER_SELECT_CLASS
        subject = state_manager.get_data(TEACHER_SELECT_SUBJECT, "subject", "")

        await self.safe_edit_message(
            query,
            TeacherTestMessageManager.get_message("select_subject"),
            TeacherTestKeyboardManager.create_subject_selection()
        )
        return TEACHER_SELECT_SUBJECT

    @network_retry
    async def process_test_name(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Обработка ввода названия теста."""
        state_manager = StateManager(context)
        name = sanitize_input(update.message.text)

        if not TeacherTestValidator.validate_name(name):
            await self.safe_reply_text(
                update.message,
                TeacherTestMessageManager.get_message("invalid_name"),
                TeacherTestKeyboardManager.create_back_button()
            )
            return TEACHER_ENTER_NAME

        state_manager.set_data(TEACHER_ENTER_NAME, "name", name)
        context.user_data["current_test"]["name"] = name
        state_manager.push(TEACHER_QUESTION_TYPE)

        await self.safe_reply_text(
            update.message,
            TeacherTestMessageManager.get_message("select_question_type", name=name),
            TeacherTestKeyboardManager.create_question_type_selection()
        )
        return TEACHER_QUESTION_TYPE

    @network_retry
    async def move_forward_name(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Переход к выбору типа вопроса."""
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)
        name = state_manager.get_data(TEACHER_ENTER_NAME, "name", "")

        if not name:
            await self.safe_edit_message(
                query,
                TeacherTestMessageManager.get_message("invalid_name"),
                TeacherTestKeyboardManager.create_back_button()
            )
            return TEACHER_ENTER_NAME

        state_manager.push(TEACHER_QUESTION_TYPE)
        await self.safe_edit_message(
            query,
            TeacherTestMessageManager.get_message("select_question_type", name=name),
            TeacherTestKeyboardManager.create_question_type_selection()
        )
        return TEACHER_QUESTION_TYPE

    @network_retry
    async def back_to_class_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Возврат к вводу классов."""
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)
        state_manager.pop()  # Удаляем TEACHER_ENTER_NAME
        classes = state_manager.get_data(TEACHER_SELECT_CLASS, "classes", [])
        subject = state_manager.get_data(TEACHER_SELECT_SUBJECT, "subject", "")

        await self.safe_edit_message(
            query,
            TeacherTestMessageManager.get_message(
                "select_class",
                subject=subject,
                classes=f"Ранее введено: {', '.join(classes)}" if classes else ""
            ),
            TeacherTestKeyboardManager.create_class_input(classes)
        )
        return TEACHER_SELECT_CLASS

    @network_retry
    async def process_question_type(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Обработка выбора типа вопроса."""
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)
        q_type = query.data.split("_")[1]
        state_manager.set_data(TEACHER_QUESTION_TYPE, "question_type", q_type)
        context.user_data["current_test"]["questions"].append({
            "type": q_type,
            "text": None,
            "correct_answer": None,
            "options": [],
            "check_comment": None
        })

        state_manager.push(TEACHER_ENTER_QUESTION)
        question_text = state_manager.get_data(TEACHER_ENTER_QUESTION, "question_text", "")
        await self.safe_edit_message(
            query,
            TeacherTestMessageManager.get_message(
                "enter_question",
                current=f"Ранее введено: {question_text}" if question_text else ""
            ),
            TeacherTestKeyboardManager.create_question_input(question_text)
        )
        return TEACHER_ENTER_QUESTION

    @network_retry
    async def back_to_test_name(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Возврат к вводу названия теста."""
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)
        state_manager.pop()  # Удаляем TEACHER_QUESTION_TYPE
        name = state_manager.get_data(TEACHER_ENTER_NAME, "name", "")
        classes = state_manager.get_data(TEACHER_SELECT_CLASS, "classes", [])

        await self.safe_edit_message(
            query,
            TeacherTestMessageManager.get_message(
                "enter_name",
                classes=', '.join(classes),
                name=f"Ранее введено: {name}" if name else ""
            ),
            TeacherTestKeyboardManager.create_name_input(name)
        )
        return TEACHER_ENTER_NAME

    @network_retry
    async def process_question_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Обработка ввода текста вопроса."""
        state_manager = StateManager(context)
        question_text = sanitize_input(update.message.text)
        state_manager.set_data(TEACHER_ENTER_QUESTION, "question_text", question_text)
        current_question = context.user_data["current_test"]["questions"][-1]
        current_question["text"] = question_text

        action = "✅ Введите ПРАВИЛЬНЫЙ ответ:" if current_question["type"] == "test" else "📝 Введите эталонный ответ:"
        state_manager.push(TEACHER_ENTER_CORRECT_ANSWER)
        correct_answer = state_manager.get_data(TEACHER_ENTER_CORRECT_ANSWER, "correct_answer", "")

        await self.safe_reply_text(
            update.message,
            TeacherTestMessageManager.get_message(
                "enter_correct_answer",
                question=question_text,
                action=action,
                current=f"Ранее введено: {correct_answer}" if correct_answer else ""
            ),
            TeacherTestKeyboardManager.create_correct_answer_input(correct_answer)
        )
        return TEACHER_ENTER_CORRECT_ANSWER

    @network_retry
    async def move_forward_question(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Переход к вводу правильного ответа."""
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)
        question_text = state_manager.get_data(TEACHER_ENTER_QUESTION, "question_text", "")
        current_question = context.user_data["current_test"]["questions"][-1]

        if not question_text:
            await self.safe_edit_message(
                query,
                TeacherTestMessageManager.get_message("enter_question", current=""),
                TeacherTestKeyboardManager.create_back_button()
            )
            return TEACHER_ENTER_QUESTION

        current_question["text"] = question_text
        action = "✅ Введите ПРАВИЛЬНЫЙ ответ:" if current_question["type"] == "test" else "📝 Введите эталонный ответ:"
        state_manager.push(TEACHER_ENTER_CORRECT_ANSWER)
        correct_answer = state_manager.get_data(TEACHER_ENTER_CORRECT_ANSWER, "correct_answer", "")

        await self.safe_edit_message(
            query,
            TeacherTestMessageManager.get_message(
                "enter_correct_answer",
                question=question_text,
                action=action,
                current=f"Ранее введено: {correct_answer}" if correct_answer else ""
            ),
            TeacherTestKeyboardManager.create_correct_answer_input(correct_answer)
        )
        return TEACHER_ENTER_CORRECT_ANSWER

    @network_retry
    async def back_to_question_type_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Возврат к выбору типа вопроса."""
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)
        state_manager.pop()  # Удаляем TEACHER_ENTER_QUESTION
        if context.user_data["current_test"]["questions"] and not context.user_data["current_test"]["questions"][-1]["text"]:
            context.user_data["current_test"]["questions"].pop()

        name = state_manager.get_data(TEACHER_ENTER_NAME, "name", "")
        await self.safe_edit_message(
            query,
            TeacherTestMessageManager.get_message("select_question_type", name=name),
            TeacherTestKeyboardManager.create_question_type_selection()
        )
        return TEACHER_QUESTION_TYPE

    @network_retry
    async def process_correct_answer(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Обработка ввода правильного ответа."""
        state_manager = StateManager(context)
        correct_answer = sanitize_input(update.message.text)
        state_manager.set_data(TEACHER_ENTER_CORRECT_ANSWER, "correct_answer", correct_answer)
        current_question = context.user_data["current_test"]["questions"][-1]
        current_question["correct_answer"] = correct_answer

        if current_question["type"] == "test":
            state_manager.push(TEACHER_ADD_OPTIONS)
            options = state_manager.get_data(TEACHER_ADD_OPTIONS, "options", [])
            await self.safe_reply_text(
                update.message,
                TeacherTestMessageManager.get_message(
                    "enter_options",
                    correct_answer=correct_answer,
                    current=f"Ранее введено: {', '.join(options)}" if options else ""
                ),
                TeacherTestKeyboardManager.create_options_input(options)
            )
            return TEACHER_ADD_OPTIONS

        state_manager.push(TEACHER_ADD_COMMENT)
        comment = state_manager.get_data(TEACHER_ADD_COMMENT, "comment", "")
        await self.safe_reply_text(
            update.message,
            TeacherTestMessageManager.get_message(
                "enter_comment",
                correct_answer=correct_answer,
                current=f"Ранее введено: {comment}" if comment else ""
            ),
            TeacherTestKeyboardManager.create_comment_input(comment)
        )
        return TEACHER_ADD_COMMENT

    @network_retry
    async def move_forward_correct_answer(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Переход к вводу вариантов или комментария."""
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)
        correct_answer = state_manager.get_data(TEACHER_ENTER_CORRECT_ANSWER, "correct_answer", "")
        current_question = context.user_data["current_test"]["questions"][-1]

        if not correct_answer:
            await self.safe_edit_message(
                query,
                TeacherTestMessageManager.get_message("invalid_correct_answer"),
                TeacherTestKeyboardManager.create_back_button()
            )
            return TEACHER_ENTER_CORRECT_ANSWER

        current_question["correct_answer"] = correct_answer
        if current_question["type"] == "test":
            state_manager.push(TEACHER_ADD_OPTIONS)
            options = state_manager.get_data(TEACHER_ADD_OPTIONS, "options", [])
            await self.safe_edit_message(
                query,
                TeacherTestMessageManager.get_message(
                    "enter_options",
                    correct_answer=correct_answer,
                    current=f"Ранее введено: {', '.join(options)}" if options else ""
                ),
                TeacherTestKeyboardManager.create_options_input(options)
            )
            return TEACHER_ADD_OPTIONS

        state_manager.push(TEACHER_ADD_COMMENT)
        comment = state_manager.get_data(TEACHER_ADD_COMMENT, "comment", "")
        await self.safe_edit_message(
            query,
            TeacherTestMessageManager.get_message(
                "enter_comment",
                correct_answer=correct_answer,
                current=f"Ранее введено: {comment}" if comment else ""
            ),
            TeacherTestKeyboardManager.create_comment_input(comment)
        )
        return TEACHER_ADD_COMMENT

    @network_retry
    async def back_to_question_text_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Возврат к вводу текста вопроса."""
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)
        state_manager.pop()  # Удаляем TEACHER_ENTER_CORRECT_ANSWER
        question_text = state_manager.get_data(TEACHER_ENTER_QUESTION, "question_text", "")

        await self.safe_edit_message(
            query,
            TeacherTestMessageManager.get_message(
                "enter_question",
                current=f"Ранее введено: {question_text}" if question_text else ""
            ),
            TeacherTestKeyboardManager.create_question_input(question_text)
        )
        return TEACHER_ENTER_QUESTION

    @network_retry
    async def process_options(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Обработка ввода дополнительных вариантов ответа."""
        state_manager = StateManager(context)
        options_input = sanitize_input(update.message.text)
        options = [opt.strip() for opt in options_input.split(',') if opt.strip()]
        current_question = context.user_data["current_test"]["questions"][-1]
        correct_answer = current_question["correct_answer"]

        if not TeacherTestValidator.validate_options(options, correct_answer):
            await self.safe_reply_text(
                update.message,
                TeacherTestMessageManager.get_message("invalid_options"),
                TeacherTestKeyboardManager.create_back_button()
            )
            return TEACHER_ADD_OPTIONS

        state_manager.set_data(TEACHER_ADD_OPTIONS, "options", options)
        current_question["options"] = options + [correct_answer]
        return await self._finalize_question(update, context)

    @network_retry
    async def move_forward_options(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Переход к финализации вопроса."""
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)
        options = state_manager.get_data(TEACHER_ADD_OPTIONS, "options", [])
        current_question = context.user_data["current_test"]["questions"][-1]
        correct_answer = current_question["correct_answer"]

        if not TeacherTestValidator.validate_options(options, correct_answer):
            await self.safe_edit_message(
                query,
                TeacherTestMessageManager.get_message("invalid_options"),
                TeacherTestKeyboardManager.create_back_button()
            )
            return TEACHER_ADD_OPTIONS

        current_question["options"] = options + [correct_answer]
        return await self._finalize_question(update, context)

    @network_retry
    async def back_to_correct_answer_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Возврат к вводу правильного ответа."""
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)
        state_manager.pop()  # Удаляем TEACHER_ADD_OPTIONS или TEACHER_ADD_COMMENT
        correct_answer = state_manager.get_data(TEACHER_ENTER_CORRECT_ANSWER, "correct_answer", "")
        current_question = context.user_data["current_test"]["questions"][-1]
        action = "✅ Введите ПРАВИЛЬНЫЙ ответ:" if current_question["type"] == "test" else "📝 Введите эталонный ответ:"

        await self.safe_edit_message(
            query,
            TeacherTestMessageManager.get_message(
                "enter_correct_answer",
                question=current_question["text"],
                action=action,
                current=f"Ранее введено: {correct_answer}" if correct_answer else ""
            ),
            TeacherTestKeyboardManager.create_correct_answer_input(correct_answer)
        )
        return TEACHER_ENTER_CORRECT_ANSWER

    @network_retry
    async def process_comment(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Обработка ввода комментария."""
        state_manager = StateManager(context)
        comment = sanitize_input(update.message.text)
        state_manager.set_data(TEACHER_ADD_COMMENT, "comment", comment)
        current_question = context.user_data["current_test"]["questions"][-1]
        current_question["check_comment"] = comment
        return await self._finalize_question(update, context)

    @network_retry
    async def move_forward_comment(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Переход к финализации вопроса."""
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)
        comment = state_manager.get_data(TEACHER_ADD_COMMENT, "comment", "")
        current_question = context.user_data["current_test"]["questions"][-1]

        if not comment:
            await self.safe_edit_message(
                query,
                TeacherTestMessageManager.get_message(
                    "enter_comment",
                    correct_answer=current_question["correct_answer"],
                    current=""
                ),
                TeacherTestKeyboardManager.create_back_button()
            )
            return TEACHER_ADD_COMMENT

        current_question["check_comment"] = comment
        return await self._finalize_question(update, context)

    @network_retry
    async def _finalize_question(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Финализация вопроса."""
        state_manager = StateManager(context)
        state_manager.push(TEACHER_FINISH_CREATION)
        # Очищаем данные, связанные с созданием вопроса
        for state in [TEACHER_QUESTION_TYPE, TEACHER_ENTER_QUESTION, TEACHER_ENTER_CORRECT_ANSWER, TEACHER_ADD_OPTIONS, TEACHER_ADD_COMMENT]:
            state_manager.clear_state_data(state)

        if update.message:
            await self.safe_reply_text(
                update.message,
                TeacherTestMessageManager.get_message("question_added"),
                TeacherTestKeyboardManager.create_finalization_menu()
            )
        else:
            await self.safe_edit_message(
                update.callback_query,
                TeacherTestMessageManager.get_message("question_added"),
                TeacherTestKeyboardManager.create_finalization_menu()
            )
        return TEACHER_FINISH_CREATION

    @network_retry
    async def show_question_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Отображение списка вопросов."""
        current_test = context.user_data["current_test"]
        if not current_test["questions"]:
            await self.safe_reply_text(
                update.message if update.message else update.callback_query.message,
                TeacherTestMessageManager.get_message("no_questions"),
                TeacherTestKeyboardManager.create_question_type_selection()
            )
            return TEACHER_QUESTION_TYPE

        state_manager = StateManager(context)
        state_manager.push(TEACHER_EDIT_QUESTIONS)

        message = TeacherTestMessageManager.get_message(
            "question_list",
            name=current_test["name"],
            subject=current_test["subject"],
            classes=', '.join(current_test["classes"])
        )
        if update.callback_query:
            await self.safe_edit_message(
                update.callback_query,
                message,
                TeacherTestKeyboardManager.create_question_list(current_test["questions"])
            )
        else:
            await self.safe_reply_text(
                update.message,
                message,
                TeacherTestKeyboardManager.create_question_list(current_test["questions"])
            )
        return TEACHER_EDIT_QUESTIONS

    @network_retry
    async def select_question_to_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Выбор вопроса для редактирования."""
        query = update.callback_query
        await query.answer()
        try:
            idx = int(query.data.split("_")[1])
        except (IndexError, ValueError):
            await self.safe_edit_message(
                query,
                TeacherTestMessageManager.get_message("error"),
                TeacherTestKeyboardManager.create_back_button()
            )
            return TEACHER_EDIT_QUESTIONS

        if not 0 <= idx < len(context.user_data["current_test"]["questions"]):
            await self.safe_edit_message(
                query,
                TeacherTestMessageManager.get_message("error"),
                TeacherTestKeyboardManager.create_back_button()
            )
            return TEACHER_EDIT_QUESTIONS

        context.user_data["editing_question_idx"] = idx
        state_manager = StateManager(context)
        state_manager.push(TEACHER_EDIT_QUESTION_PART)

        q_type = context.user_data["current_test"]["questions"][idx]["type"]
        await self.safe_edit_message(
            query,
            TeacherTestMessageManager.get_message("edit_question", idx=idx + 1),
            TeacherTestKeyboardManager.create_edit_question_menu(q_type)
        )
        return TEACHER_EDIT_QUESTION_PART

    @network_retry
    async def edit_question_part(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Редактирование части вопроса."""
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)
        part = query.data.split("_")[1]
        context.user_data["editing_part"] = part
        idx = context.user_data["editing_question_idx"]
        question = context.user_data["current_test"]["questions"][idx]

        part_labels = {
            "text": "текст вопроса",
            "correct": "правильный ответ",
            "options": "варианты ответов",
            "comment": "комментарий"
        }
        current_values = {
            "text": question.get("text", ""),
            "correct": question.get("correct_answer", ""),
            "options": ", ".join([opt for opt in question.get("options", []) if opt != question.get("correct_answer", "")]) if question.get("options") else "",
            "comment": question.get("check_comment", "")
        }

        state_manager.push(TEACHER_EDIT_QUESTION_CONTENT)
        await self.safe_edit_message(
            query,
            TeacherTestMessageManager.get_message(
                "edit_question_part",
                part=part_labels[part],
                current=f"Текущее значение: {current_values[part]}" if current_values[part] else ""
            ),
            TeacherTestKeyboardManager.create_back_button()
        )
        return TEACHER_EDIT_QUESTION_CONTENT

    @network_retry
    async def save_edited_question(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Сохранение отредактированного вопроса."""
        new_value = sanitize_input(update.message.text)
        idx = context.user_data["editing_question_idx"]
        part = context.user_data["editing_part"]
        question = context.user_data["current_test"]["questions"][idx]

        if part == "options":
            options = [opt.strip() for opt in new_value.split(",") if opt.strip()]
            if not TeacherTestValidator.validate_options(options, question["correct_answer"]):
                await self.safe_reply_text(
                    update.message,
                    TeacherTestMessageManager.get_message("invalid_options"),
                    TeacherTestKeyboardManager.create_back_button()
                )
                return TEACHER_EDIT_QUESTION_CONTENT
            question["options"] = options + [question["correct_answer"]]
        elif part == "text":
            question["text"] = new_value
        elif part == "correct":
            question["correct_answer"] = new_value
            if question["type"] == "test" and question["options"]:
                question["options"] = [opt for opt in question["options"] if opt != question["correct_answer"]] + [new_value]
        elif part == "comment":
            question["check_comment"] = new_value

        await self.safe_reply_text(
            update.message,
            TeacherTestMessageManager.get_message("changes_saved"),
            TeacherTestKeyboardManager.create_back_button()
        )
        return await self.show_question_list(update, context)

    @network_retry
    async def back_to_question_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Возврат к списку вопросов."""
        state_manager = StateManager(context)
        state_manager.pop()  # Удаляем TEACHER_EDIT_QUESTION_PART или TEACHER_EDIT_QUESTION_CONTENT
        return await self.show_question_list(update, context)

    @network_retry
    async def finish_creation(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Завершение создания теста."""
        query = update.callback_query
        await query.answer()
        current_test = context.user_data["current_test"]

        if not current_test["questions"]:
            await self.safe_edit_message(
                query,
                TeacherTestMessageManager.get_message("no_questions"),
                TeacherTestKeyboardManager.create_question_type_selection()
            )
            return TEACHER_QUESTION_TYPE

        state_manager = StateManager(context)
        state_manager.push(TEACHER_GLOBAL_COMMENT)
        global_comment = state_manager.get_data(TEACHER_GLOBAL_COMMENT, "global_comment", "")

        await self.safe_edit_message(
            query,
            TeacherTestMessageManager.get_message(
                "global_comment",
                current=f"Ранее введено: {global_comment}" if global_comment else ""
            ),
            TeacherTestKeyboardManager.create_back_button()
        )
        return TEACHER_GLOBAL_COMMENT

    @network_retry
    async def process_global_comment(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Обработка глобального комментария."""
        state_manager = StateManager(context)
        user_input = sanitize_input(update.message.text)
        current_test = context.user_data["current_test"]

        if user_input.lower() == "/skip":
            current_test["global_comment"] = None
            state_manager.set_data(TEACHER_GLOBAL_COMMENT, "global_comment", None)
        else:
            current_test["global_comment"] = user_input
            state_manager.set_data(TEACHER_GLOBAL_COMMENT, "global_comment", user_input)

        return await self.show_final_confirmation(update, context)

    @network_retry
    async def move_forward_global_comment(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Переход к финальному подтверждению."""
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)
        global_comment = state_manager.get_data(TEACHER_GLOBAL_COMMENT, "global_comment", None)
        current_test = context.user_data["current_test"]
        current_test["global_comment"] = global_comment

        return await self.show_final_confirmation(update, context)

    @network_retry
    async def handle_final_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Обработка редактирования перед финальным подтверждением."""
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)
        action = query.data
        current_test = context.user_data["current_test"]

        if action == "edit_name":
            state_manager.push(TEACHER_ENTER_NAME)
            name = state_manager.get_data(TEACHER_ENTER_NAME, "name", "")
            await self.safe_edit_message(
                query,
                TeacherTestMessageManager.get_message(
                    "enter_name",
                    classes=', '.join(current_test["classes"]),
                    name=f"Ранее введено: {name}" if name else ""
                ),
                TeacherTestKeyboardManager.create_name_input(name)
            )
            return TEACHER_ENTER_NAME
        elif action == "edit_subject":
            state_manager.push(TEACHER_EDIT_SUBJECT)
            await self.safe_edit_message(
                query,
                TeacherTestMessageManager.get_message("select_subject"),
                TeacherTestKeyboardManager.create_subject_selection()
            )
            return TEACHER_EDIT_SUBJECT
        elif action == "edit_classes":
            state_manager.push(TEACHER_EDIT_CLASSES)
            classes = state_manager.get_data(TEACHER_SELECT_CLASS, "classes", [])
            await self.safe_edit_message(
                query,
                TeacherTestMessageManager.get_message(
                    "select_class",
                    subject=current_test["subject"],
                    classes=f"Ранее введено: {', '.join(classes)}" if classes else ""
                ),
                TeacherTestKeyboardManager.create_class_input(classes)
            )
            return TEACHER_EDIT_CLASSES
        elif action == "edit_questions":
            return await self.show_question_list(update, context)
        elif action == "edit_global_comment":
            state_manager.push(TEACHER_EDIT_GLOBAL_COMMENT)
            global_comment = state_manager.get_data(TEACHER_GLOBAL_COMMENT, "global_comment", "")
            await self.safe_edit_message(
                query,
                TeacherTestMessageManager.get_message(
                    "global_comment",
                    current=f"Ранее введено: {global_comment}" if global_comment else ""
                ),
                TeacherTestKeyboardManager.create_back_button()
            )
            return TEACHER_EDIT_GLOBAL_COMMENT
        return TEACHER_FINAL_CONFIRM

    @network_retry
    async def process_edit_global_comment(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Обработка редактирования глобального комментария."""
        state_manager = StateManager(context)
        user_input = sanitize_input(update.message.text)
        current_test = context.user_data["current_test"]

        if user_input.lower() == "/skip":
            current_test["global_comment"] = None
            state_manager.set_data(TEACHER_GLOBAL_COMMENT, "global_comment", None)
        else:
            current_test["global_comment"] = user_input
            state_manager.set_data(TEACHER_GLOBAL_COMMENT, "global_comment", user_input)

        return await self.show_final_confirmation(update, context)


    @network_retry
    async def move_forward_edit_global_comment(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Переход к финальному подтверждению после редактирования комментария."""
        query = update.callback_query
        await query.answer()
        return await self.show_final_confirmation(update, context)

    @network_retry
    async def process_edit_subject(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Обработка редактирования предмета."""
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)
        subject = sanitize_input(query.data.split("_")[1])
        state_manager.set_data(TEACHER_SELECT_SUBJECT, "subject", subject)
        context.user_data["current_test"]["subject"] = subject
        return await self.show_final_confirmation(update, context)

    @network_retry
    async def process_edit_classes(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Обработка редактирования классов."""
        state_manager = StateManager(context)
        class_input = sanitize_input(update.message.text)
        classes = [c.strip() for c in class_input.split(',')]

        if not TeacherTestValidator.validate_classes(classes):
            await self.safe_reply_text(
                update.message,
                TeacherTestMessageManager.get_message("invalid_class"),
                TeacherTestKeyboardManager.create_back_button()
            )
            return TEACHER_EDIT_CLASSES

        state_manager.set_data(TEACHER_SELECT_CLASS, "classes", classes)
        context.user_data["current_test"]["classes"] = classes
        await self.safe_reply_text(
            update.message,
            TeacherTestMessageManager.get_message("select_class", subject="", classes=', '.join(classes)),
            TeacherTestKeyboardManager.create_final_confirmation_menu()
        )
        return await self.show_final_confirmation(update, context)

    @network_retry
    async def move_forward_edit_classes(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Переход к финальному подтверждению после редактирования классов."""
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)
        classes = state_manager.get_data(TEACHER_SELECT_CLASS, "classes", [])

        if not classes:
            await self.safe_edit_message(
                query,
                TeacherTestMessageManager.get_message("invalid_class"),
                TeacherTestKeyboardManager.create_back_button()
            )
            return TEACHER_EDIT_CLASSES

        return await self.show_final_confirmation(update, context)

    @network_retry
    async def show_final_confirmation(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Отображение финального подтверждения."""
        state_manager = StateManager(context)
        current_test = context.user_data["current_test"]
        comment_status = current_test.get("global_comment", "не добавлен")

        # Устанавливаем состояние только если оно еще не активно
        if state_manager.current() != TEACHER_FINAL_CONFIRM:
            state_manager.push(TEACHER_FINAL_CONFIRM)

        message = TeacherTestMessageManager.get_message(
            "final_confirmation",
            name=current_test["name"],
            subject=current_test["subject"],
            classes=', '.join(current_test["classes"]),
            question_count=len(current_test["questions"]),
            comment=comment_status
        )
        reply_markup = TeacherTestKeyboardManager.create_final_confirmation_menu()

        if update.callback_query:
            await self.safe_edit_message(
                update.callback_query,
                message,
                reply_markup
            )
        else:
            await self.safe_reply_text(
                update.message,
                message,
                reply_markup
            )
        return TEACHER_FINAL_CONFIRM

    @network_retry
    async def process_final_confirmation(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Финальное подтверждение и сохранение теста."""
        query = update.callback_query
        await query.answer()
        current_test = context.user_data["current_test"]
        current_test["id"] = str(uuid.uuid4())
        current_test["created_at"] = datetime.now().isoformat()

        if not TeacherTestValidator.validate_test(current_test):
            await self.safe_edit_message(
                query,
                TeacherTestMessageManager.get_message("error"),
                TeacherTestKeyboardManager.create_back_button()
            )
            return TEACHER_FINAL_CONFIRM

        await self.safe_reply_text(query.message, "⏳ Сохраняем тест в базу данных...")
        self.db.save_test(current_test)

        await self.safe_edit_message(
            query,
            TeacherTestMessageManager.get_message("test_created", name=current_test["name"]),
            None
        )
        self.reset_state(context)
        return ConversationHandler.END

    @network_retry
    async def cancel_creation(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Отмена создания теста."""
        message = update.message or update.callback_query.message
        await self.safe_reply_text(
            message,
            TeacherTestMessageManager.get_message("canceled"),
            None
        )
        self.reset_state(context)
        return ConversationHandler.END

    @network_retry
    async def back_to_finalization(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Возврат к финализации вопроса."""
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)
        state_manager.pop()  # Удаляем TEACHER_GLOBAL_COMMENT или TEACHER_FINAL_CONFIRM
        state_manager.push(TEACHER_FINISH_CREATION)

        await self.safe_edit_message(
            query,
            TeacherTestMessageManager.get_message("question_added"),
            TeacherTestKeyboardManager.create_finalization_menu()
        )
        return TEACHER_FINISH_CREATION

    @network_retry
    async def back_to_teacher_main(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Возврат в главное меню учителя."""
        query = update.callback_query
        await query.answer()
        self.reset_state(context)

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 Создать тест", callback_data="create_test")],
            [InlineKeyboardButton("📊 Проверить работы", callback_data="check_results")],
            [InlineKeyboardButton("🔙 Назад", callback_data="back")]
        ])
        await self.safe_edit_message(
            query,
            "🏠 Меню учителя:",
            keyboard
        )
        return TEACHER_MAIN

    def get_conversation_handler(self):
        """Получение обработчика диалога."""
        return ConversationHandler(
            entry_points=[CallbackQueryHandler(self.start_creation, pattern=r"^create_test$")],
            states={
                TEACHER_SELECT_SUBJECT: [
                    CallbackQueryHandler(self.process_subject, pattern=r"^subj_"),
                    CallbackQueryHandler(self.back_to_teacher_main, pattern=r"^back$")
                ],
                TEACHER_SELECT_CLASS: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.process_class),
                    CallbackQueryHandler(self.move_forward_class, pattern=r"^next$"),
                    CallbackQueryHandler(self.back_to_subject_select, pattern=r"^back$")
                ],
                TEACHER_ENTER_NAME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.process_test_name),
                    CallbackQueryHandler(self.move_forward_name, pattern=r"^next$"),
                    CallbackQueryHandler(self.back_to_class_input, pattern=r"^back$")
                ],
                TEACHER_QUESTION_TYPE: [
                    CallbackQueryHandler(self.process_question_type, pattern=r"^type_"),
                    CallbackQueryHandler(self.finish_creation, pattern=r"^finish_test$"),
                    CallbackQueryHandler(self.back_to_test_name, pattern=r"^back$")
                ],
                TEACHER_ENTER_QUESTION: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.process_question_text),
                    CallbackQueryHandler(self.move_forward_question, pattern=r"^next$"),
                    CallbackQueryHandler(self.back_to_question_type_select, pattern=r"^back$")
                ],
                TEACHER_ENTER_CORRECT_ANSWER: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.process_correct_answer),
                    CallbackQueryHandler(self.move_forward_correct_answer, pattern=r"^next$"),
                    CallbackQueryHandler(self.back_to_question_text_input, pattern=r"^back$")
                ],
                TEACHER_ADD_OPTIONS: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.process_options),
                    CallbackQueryHandler(self.move_forward_options, pattern=r"^next$"),
                    CallbackQueryHandler(self.back_to_correct_answer_input, pattern=r"^back$")
                ],
                TEACHER_ADD_COMMENT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.process_comment),
                    CallbackQueryHandler(self.move_forward_comment, pattern=r"^next$"),
                    CallbackQueryHandler(self.back_to_correct_answer_input, pattern=r"^back$")
                ],
                TEACHER_FINISH_CREATION: [
                    CallbackQueryHandler(self.finish_creation, pattern=r"^finish_test$"),
                    CallbackQueryHandler(self.back_to_question_type_select, pattern=r"^add_another$"),
                    CallbackQueryHandler(self.show_question_list, pattern=r"^edit_questions$"),
                    CallbackQueryHandler(self.back_to_finalization, pattern=r"^back$")
                ],
                TEACHER_EDIT_QUESTIONS: [
                    CallbackQueryHandler(self.select_question_to_edit, pattern=r"^edit_"),
                    CallbackQueryHandler(self.back_to_question_type_select, pattern=r"^add_another$"),
                    CallbackQueryHandler(self.finish_creation, pattern=r"^finish_test$"),
                    CallbackQueryHandler(self.back_to_finalization, pattern=r"^back$")
                ],
                TEACHER_EDIT_QUESTION_PART: [
                    CallbackQueryHandler(self.edit_question_part, pattern=r"^edit_"),
                    CallbackQueryHandler(self.back_to_question_list, pattern=r"^back$")
                ],
                TEACHER_EDIT_QUESTION_CONTENT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.save_edited_question),
                    CallbackQueryHandler(self.back_to_question_list, pattern=r"^back$")
                ],
                TEACHER_GLOBAL_COMMENT: [
                    MessageHandler(filters.TEXT | filters.COMMAND, self.process_global_comment),
                    CallbackQueryHandler(self.move_forward_global_comment, pattern=r"^next$"),
                    CallbackQueryHandler(self.back_to_finalization, pattern=r"^back$")
                ],
                TEACHER_EDIT_SUBJECT: [
                    CallbackQueryHandler(self.process_edit_subject, pattern=r"^subj_"),
                    CallbackQueryHandler(self.show_final_confirmation, pattern=r"^back$")
                ],
                TEACHER_EDIT_CLASSES: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.process_edit_classes),
                    CallbackQueryHandler(self.move_forward_edit_classes, pattern=r"^next$"),
                    CallbackQueryHandler(self.show_final_confirmation, pattern=r"^back$")
                ],
                TEACHER_EDIT_GLOBAL_COMMENT: [
                    MessageHandler(filters.TEXT | filters.COMMAND, self.process_edit_global_comment),
                    CallbackQueryHandler(self.move_forward_edit_global_comment, pattern=r"^next$"),
                    CallbackQueryHandler(self.show_final_confirmation, pattern=r"^back$")
                ],
                TEACHER_FINAL_CONFIRM: [
                    CallbackQueryHandler(self.process_final_confirmation, pattern=r"^confirm_test$"),
                    CallbackQueryHandler(self.handle_final_edit, pattern=r"^edit_"),
                    CallbackQueryHandler(self.back_to_finalization, pattern=r"^back$")
                ]
            },
            fallbacks=[
                CommandHandler("cancel", self.cancel_creation)
            ],
            map_to_parent={
                ConversationHandler.END: TEACHER_MAIN,
                TEACHER_MAIN: TEACHER_MAIN
            },
            allow_reentry=True
        )

def teacher_test_creator_conv_handler():
    return TeacherTestCreator().get_conversation_handler()