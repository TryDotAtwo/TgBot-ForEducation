import logging
import re
from datetime import datetime
from typing import Optional, List, Dict, Any, Callable
from functools import wraps
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.error import NetworkError, TimedOut
from database import Database
from states import (
    TEACHER_CHECK_RESULTS,
    TEACHER_CHECK_TEST,
    TEACHER_CHECK_STUDENTS,
    TEACHER_CHECK_QUESTIONS,
    TEACHER_CHECK_ANSWERS,
    TEACHER_EDIT_SCORE,
    TEACHER_CHECK_APPEALS,
    TEACHER_RESPOND_APPEAL,
    TEACHER_ADD_COMMENT,
    TEACHER_VIEW_STUDENT_QUESTIONS,
)
import uuid

logger = logging.getLogger(__name__)

# Константы
TESTS_PER_PAGE = 5
STUDENTS_PER_PAGE = 5
QUESTIONS_PER_PAGE = 5
ANSWERS_PER_PAGE = 5
APPEALS_PER_PAGE = 5
MAX_MESSAGE_LENGTH = 4096
TEXT_PART_LENGTH = 1000
MAX_INPUT_LENGTH = 1000

# Утилиты
def network_retry(func: Callable) -> Callable:
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((NetworkError, TimedOut)),
        before_sleep=lambda retry_state: logger.debug(
            f"Попытка {retry_state.attempt_number} для {func.__name__} не удалась, повтор через {retry_state.next_action.sleep} сек."
        )
    )
    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Ошибка в {func.__name__}: {e}")
            raise
    return wrapper

class StateManager:
    def __init__(self):
        self.stack_key = "state_stack"

    def initialize(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.stack_key not in context.user_data:
            context.user_data[self.stack_key] = []
            logger.debug("Инициализирован стек состояний")

    def push(self, context: ContextTypes.DEFAULT_TYPE, state: int) -> None:
        context.user_data[self.stack_key].append(state)
        logger.debug(f"Добавлено состояние {state}, стек: {context.user_data[self.stack_key]}")

    def pop(self, context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
        if context.user_data[self.stack_key]:
            state = context.user_data[self.stack_key].pop()
            logger.debug(f"Извлечено состояние {state}, стек: {context.user_data[self.stack_key]}")
            return state
        logger.debug("Стек состояний пуст")
        return None

    def get_current(self, context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
        stack = context.user_data.get(self.stack_key, [])
        return stack[-1] if stack else None

    def clear(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.user_data[self.stack_key] = []
        logger.debug("Стек состояний очищен")

def sanitize_input(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r'[^\w\s.,!?]', '', text)
    cleaned = cleaned[:MAX_INPUT_LENGTH]
    cleaned = ' '.join(cleaned.split())
    return cleaned

def split_message(text: str, max_length: int) -> List[str]:
    if len(text) <= max_length:
        return [text]
    parts = []
    while text:
        if len(text) <= max_length:
            parts.append(text)
            break
        split_idx = text[:max_length].rfind('\n')
        if split_idx == -1:
            split_idx = text[:max_length].rfind(' ')
        if split_idx == -1:
            split_idx = max_length
        parts.append(text[:split_idx])
        text = text[split_idx:].strip()
    return parts

class TeacherResultsMessageManager:
    MESSAGES = {
        "no_tests": "📜 Вы еще не создали ни одного теста.",
        "test_list": "📜 Ваши тесты:\n\n{tests_info}",
        "test_info": (
            "📝 Тест: {name}\n"
            "Предмет: {subject}\n"
            "Классы: {classes}\n"
            "Создан: {created_at}\n"
            "Вопросов: {questions_count}\n"
            "Прохождений: {results_count}\n"
            "Последнее прохождение: {last_date}\n\n"
            "Выберите действие:"
        ),
        "no_results": "📜 Тест '{name}' ещё никто не проходил.",
        "students_stats": "📊 Статистика по ученикам для теста '{name}':\n\n{students_info}",
        "student_question": (
            "❓ Вопрос #{q_idx}/{total_questions}:\n"
            "{question_text}\n"
            "Правильный ответ: {correct_answer}\n"
            "Комментарий модели: {check_comment}\n\n"
            "👤 {student_info}\n"
            "Ответ: {answer}\n"
            "Оценка: {score}\n"
            "Комментарий учителя: {comment}\n{appeal_info}"
        ),
        "questions_stats": "📊 Статистика по вопросам для теста '{name}':\n\n{questions_info}",
        "question_answers": (
            "❓ Вопрос #{q_idx}:\n"
            "{question_text}\n"
            "Правильный ответ: {correct_answer}\n"
            "Комментарий модели: {check_comment}\n\n"
            "Ответы учеников:\n{answers_info}"
        ),
        "edit_score_prompt": (
            "❓ Вопрос #{q_idx}: {question_text}\n"
            "👤 Студент: {student_info}\n"
            "Ответ: {answer}\n"
            "Текущая оценка: {score}\n"
            "Текущий комментарий: {comment}\n\n"
            "Введите новую оценку (например, 5.0):"
        ),
        "add_comment_prompt": (
            "❓ Вопрос #{q_idx}: {question_text}\n"
            "👤 Студент: {student_info}\n"
            "Ответ: {answer}\n"
            "Оценка: {score}\n"
            "Текущий комментарий: {comment}\n\n"
            "Введите новый комментарий:"
        ),
        "appeals_list": "📜 Апелляции по тесту '{name}':\n\n{appeals_info}",
        "no_appeals": "📜 По тесту '{name}' нет апелляций.",
        "appeal_response_prompt": (
            "❓ Вопрос #{q_idx}: {question_text}\n"
            "Правильный ответ: {correct_answer}\n"
            "👤 Студент: {student_info}\n"
            "Ответ: {answer}\n"
            "Оценка: {score}\n"
            "Апелляция: {student_comment}\n{teacher_comment}\n"
            "Введите комментарий к апелляции:"
        ),
        "error_invalid_score": "Ошибка: оценка должна быть числом (например, 5.0).",
        "error_empty_comment": "Ошибка: комментарий не может быть пустым.",
        "error_missing_data": "Ошибка: данные для редактирования не найдены.",
        "error_test_not_found": "Ошибка: тест не найден.",
        "error_result_not_found": "Ошибка: результат не найден.",
        "error_appeal_not_found": "Ошибка: апелляция не найдена.",
        "score_saved": "Оценка сохранена.",
        "comment_saved": "Комментарий сохранён.",
        "appeal_response_saved": "Ответ на апелляцию сохранён.",
    }

    @staticmethod
    def format_message(key: str, **kwargs) -> str:
        return TeacherResultsMessageManager.MESSAGES[key].format(**kwargs)

class TeacherResultsKeyboardManager:
    @staticmethod
    def create_back_button() -> InlineKeyboardButton:
        return InlineKeyboardButton("🔙 Назад", callback_data="back")

    @staticmethod
    def create_pagination_buttons(page: int, total_pages: int, prefix: str) -> List[InlineKeyboardButton]:
        buttons = []
        if page > 0:
            buttons.append(InlineKeyboardButton("⬅️ Предыдущая", callback_data=f"{prefix}_prev"))
        if page < total_pages - 1:
            buttons.append(InlineKeyboardButton("Следующая ➡️", callback_data=f"{prefix}_next"))
        return buttons

    @staticmethod
    def create_text_part_buttons(part_idx: int, total_parts: int) -> List[InlineKeyboardButton]:
        buttons = []
        if part_idx > 0:
            buttons.append(InlineKeyboardButton("⬅️ Пред. часть", callback_data="text_part_prev"))
        if part_idx < total_parts - 1:
            buttons.append(InlineKeyboardButton("След. часть ➡️", callback_data="text_part_next"))
        return buttons

    @staticmethod
    def create_tests_keyboard(tests: List[Dict], page: int, total_pages: int) -> InlineKeyboardMarkup:
        keyboard = [[InlineKeyboardButton(f"Выбрать: {test['name']}", callback_data=f"select_test_{test['id']}")]
                    for test in tests]
        nav_buttons = TeacherResultsKeyboardManager.create_pagination_buttons(page, total_pages, "tests_page")
        if nav_buttons:
            keyboard.append(nav_buttons)
        keyboard.append([TeacherResultsKeyboardManager.create_back_button()])
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def create_test_menu_keyboard(test_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("Статистика по ученикам", callback_data=f"stats_students_{test_id}")],
            [InlineKeyboardButton("Статистика по заданиям", callback_data=f"stats_questions_{test_id}")],
            [InlineKeyboardButton("Апелляции", callback_data=f"view_appeals_{test_id}")],
            [TeacherResultsKeyboardManager.create_back_button()]
        ])

    @staticmethod
    def create_students_keyboard(results: List[Dict], page: int, total_pages: int) -> InlineKeyboardMarkup:
        keyboard = [[InlineKeyboardButton(f"{r['student_info']} ({r['total_score']})", callback_data=f"view_student_questions_{r['id']}")]
                    for r in results]
        nav_buttons = TeacherResultsKeyboardManager.create_pagination_buttons(page, total_pages, "students_page")
        if nav_buttons:
            keyboard.append(nav_buttons)
        keyboard.append([TeacherResultsKeyboardManager.create_back_button()])
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def create_student_questions_keyboard(q_idx: int, total_questions: int, result_id: str, text_parts: int, has_appeal: bool, appeal_id: str) -> InlineKeyboardMarkup:
        keyboard = [[
            InlineKeyboardButton("Изменить оценку", callback_data=f"edit_score_{result_id}_{q_idx}"),
            InlineKeyboardButton("Оставить комментарий", callback_data=f"add_comment_{result_id}_{q_idx}")
        ]]
        if has_appeal and appeal_id:
            keyboard.append([InlineKeyboardButton("Ответить на апелляцию", callback_data=f"respond_appeal_{appeal_id}")])
        nav_buttons = []
        if q_idx > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ Пред. вопрос", callback_data="question_prev"))
        if q_idx < total_questions - 1:
            nav_buttons.append(InlineKeyboardButton("След. вопрос ➡️", callback_data="question_next"))
        if nav_buttons:
            keyboard.append(nav_buttons)
        text_nav = TeacherResultsKeyboardManager.create_text_part_buttons(0, text_parts)
        if text_nav:
            keyboard.append(text_nav)
        keyboard.append([TeacherResultsKeyboardManager.create_back_button()])
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def create_questions_keyboard(indices: List[int], page: int, total_pages: int, text_parts: int) -> InlineKeyboardMarkup:
        keyboard = [[InlineKeyboardButton(f"Просмотреть ответы на #{idx + 1}", callback_data=f"view_answers_{idx}")]
                    for idx in indices]
        nav_buttons = TeacherResultsKeyboardManager.create_pagination_buttons(page, total_pages, "questions_page")
        if nav_buttons:
            keyboard.append(nav_buttons)
        text_nav = TeacherResultsKeyboardManager.create_text_part_buttons(0, text_parts)
        if text_nav:
            keyboard.append(text_nav)
        keyboard.append([TeacherResultsKeyboardManager.create_back_button()])
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def create_answers_keyboard(results: List[Dict], q_idx: int, page: int, total_pages: int, text_parts: int, appeals: List[Dict]) -> InlineKeyboardMarkup:
        keyboard = []
        for r in results:
            appeal = next((a for a in appeals if a.get("user_id") == r.get("user_id") and a["question_idx"] == q_idx and a["status"] in ["pending", "responded"]), None)
            row = [
                InlineKeyboardButton(
                    f"Изменить оценку: {r['student_info']} ({r.get('scores', {}).get(str(q_idx), 0)})",
                    callback_data=f"edit_score_{r['id']}_{q_idx}"
                ),
                InlineKeyboardButton(
                    f"Комментарий: {r['student_info']}",
                    callback_data=f"add_comment_{r['id']}_{q_idx}"
                )
            ]
            if appeal:
                row.append(InlineKeyboardButton(
                    "Ответить на апелляцию",
                    callback_data=f"respond_appeal_{appeal['id']}"
                ))
            keyboard.append(row)
        nav_buttons = TeacherResultsKeyboardManager.create_pagination_buttons(page, total_pages, "answers_page")
        question_nav = []
        total_questions = len(results[0]["test"]["questions"]) if results else 1
        if q_idx > 0:
            question_nav.append(InlineKeyboardButton("⬅️ Пред. вопрос", callback_data="question_prev"))
        if q_idx < total_questions - 1:
            question_nav.append(InlineKeyboardButton("След. вопрос ➡️", callback_data="question_next"))
        if nav_buttons:
            keyboard.append(nav_buttons)
        if question_nav:
            keyboard.append(question_nav)
        text_nav = TeacherResultsKeyboardManager.create_text_part_buttons(0, text_parts)
        if text_nav:
            keyboard.append(text_nav)
        keyboard.append([TeacherResultsKeyboardManager.create_back_button()])
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def create_appeals_keyboard(appeals: List[Dict], results: List[Dict], page: int, total_pages: int) -> InlineKeyboardMarkup:
        keyboard = []
        for appeal in appeals:
            result = next((r for r in results if r["test_id"] == appeal["test_id"] and str(appeal["question_idx"]) in r["answers"]), None)
            result_id = result["id"] if result else ""
            button_text = f"{'Ответить' if appeal['status'] == 'pending' else 'Изменить ответ'}: {appeal['student_comment'][:20]}..."
            keyboard.append([
                InlineKeyboardButton(f"Изменить оценку: {appeal['student_comment'][:20]}...",
                                    callback_data=f"edit_score_{result_id}_{appeal['question_idx']}"),
                InlineKeyboardButton(button_text, callback_data=f"respond_appeal_{appeal['id']}"),
                InlineKeyboardButton(f"Комментарий: {appeal['student_comment'][:20]}...",
                                    callback_data=f"add_comment_{result_id}_{appeal['question_idx']}")
            ])
        nav_buttons = TeacherResultsKeyboardManager.create_pagination_buttons(page, total_pages, "appeals_page")
        if nav_buttons:
            keyboard.append(nav_buttons)
        keyboard.append([TeacherResultsKeyboardManager.create_back_button()])
        return InlineKeyboardMarkup(keyboard)

class TeacherResultsValidator:
    @staticmethod
    def validate_score(score_text: str) -> Optional[float]:
        try:
            return float(score_text)
        except ValueError:
            return None

    @staticmethod
    def validate_comment(comment: str) -> bool:
        return bool(comment.strip())

    @staticmethod
    def validate_test(test: Dict) -> bool:
        return bool(test and "id" in test and "name" in test)

    @staticmethod
    def validate_result(result: Dict) -> bool:
        return bool(result and "id" in result and "test_id" in result)

    @staticmethod
    def validate_appeal(appeal: Dict) -> bool:
        return bool(appeal and "id" in appeal and "test_id" in appeal)

class TeacherResultsViewer:
    def __init__(self, db: Database):
        self.db = db
        self.state_manager = StateManager()
        self.message_manager = TeacherResultsMessageManager()
        self.keyboard_manager = TeacherResultsKeyboardManager()
        self.validator = TeacherResultsValidator()

    def _get_user_id_by_result_id(self, result_id: str) -> Optional[str]:
        """
        Ищет user_id по result_id, используя метод load_all_results из Database.
        """
        results = self.db.load_all_results()
        for result in results:
            if result.get("id") == result_id:
                return result.get("user_id")
        logger.warning(f"Не найден user_id для результата {result_id}")
        return None

    def _add_change(self, context: ContextTypes.DEFAULT_TYPE, change: Dict) -> None:
        test_id = change["test_id"]
        student_id = change.get("student_id")
        if not student_id:
            logger.error(f"Попытка добавить изменение без student_id: {change}")
            return
        changes = context.user_data.setdefault("test_changes", {}).setdefault(test_id, [])
        # Генерируем уникальный change_id с использованием UUID
        change_id = f"{student_id}_{test_id}_{change['question_idx']}_{change['type']}_{uuid.uuid4()}"
        change["change_id"] = change_id
        changes.append(change)
        logger.debug(f"Добавлено новое изменение: {change_id}")

    async def _send_message(self, update: Update, text: str, reply_markup: InlineKeyboardMarkup) -> None:
        if len(text) > MAX_MESSAGE_LENGTH:
            text = text[:MAX_MESSAGE_LENGTH - 3] + "..."
        
        query = update.callback_query
        logger.debug(f"Sending message with callback_query: {bool(query)}")
        try:
            if query:
                await query.edit_message_text(text, reply_markup=reply_markup)
            else:
                await update.message.reply_text(text, reply_markup=reply_markup)
        except Exception as e:
            if "Message is not modified" not in str(e):
                logger.error(f"Ошибка отправки сообщения: {e}")

    async def _return_to_previous(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        return_state = context.user_data.get("return_state", TEACHER_CHECK_ANSWERS)
        logger.debug(f"Возвращаемся в состояние: {return_state}")
        if return_state == TEACHER_VIEW_STUDENT_QUESTIONS:
            context.user_data["text_part_idx"] = 0
            return await self.view_student_questions(update, context)
        elif return_state == TEACHER_CHECK_APPEALS:
            context.user_data["appeals_page"] = 0
            return await self.view_appeals(update, context)
        elif return_state == TEACHER_CHECK_ANSWERS:
            context.user_data["answers_page"] = 0
            context.user_data["text_part_idx"] = 0
            return await self.view_question_answers(update, context)
        else:
            logger.warning(f"Неизвестное состояние возврата: {return_state}, возвращаемся к вопросам")
            return await self.back_to_questions(update, context)

    @network_retry
    async def start_check_results(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if query:
            await query.answer()

        self.state_manager.initialize(context)
        teacher_id = str(update.effective_user.id)
        teacher_tests = self.db.load_teacher_tests(teacher_id)

        if not teacher_tests:
            await self._send_message(update, self.message_manager.format_message("no_tests"),
                                     InlineKeyboardMarkup([[self.keyboard_manager.create_back_button()]]))
            return TEACHER_CHECK_RESULTS

        page = context.user_data.get("tests_page", 0)
        total_pages = (len(teacher_tests) + TESTS_PER_PAGE - 1) // TESTS_PER_PAGE
        start_idx = page * TESTS_PER_PAGE
        tests_on_page = teacher_tests[start_idx:start_idx + TESTS_PER_PAGE]

        all_results = self.db.load_all_results()
        tests_info = ""
        for test in tests_on_page:
            test_results = [r for r in all_results if r["test_id"] == test["id"]]
            count = len(test_results)
            last_date = max((datetime.fromisoformat(r["timestamp"]) for r in test_results), default=None)
            last_date_str = last_date.strftime("%Y-%m-%d %H:%M") if last_date else "Никто не проходил"
            tests_info += (
                f"📝 {test['name']} ({test['subject']})\n"
                f"Прохождений: {count}\n"
                f"Последнее: {last_date_str}\n\n"
            )

        text = self.message_manager.format_message("test_list", tests_info=tests_info)
        keyboard = self.keyboard_manager.create_tests_keyboard(tests_on_page, page, total_pages)
        await self._send_message(update, text, keyboard)
        self.state_manager.push(context, TEACHER_CHECK_RESULTS)
        return TEACHER_CHECK_RESULTS

    @network_retry
    async def navigate_tests(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if query:
            await query.answer()
        page = context.user_data.get("tests_page", 0)
        action = query.data
        if action == "tests_page_prev":
            context.user_data["tests_page"] = max(0, page - 1)
        elif action == "tests_page_next":
            context.user_data["tests_page"] = page + 1
        return await self.start_check_results(update, context)

    @network_retry
    async def select_test(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if query:
            await query.answer()

        test_id = query.data.replace("select_test_", "") if query and query.data.startswith("select_test_") else context.user_data.get("temp_test_id")
        if not test_id:
            await self._send_message(update, self.message_manager.format_message("error_missing_data"),
                                     InlineKeyboardMarkup([[self.keyboard_manager.create_back_button()]]))
            return TEACHER_CHECK_RESULTS

        context.user_data["current_test_id"] = test_id
        context.user_data["pending_notifications"] = []
        context.user_data["test_changes"] = context.user_data.get("test_changes", {})
        test = self.db.load_test_by_id(test_id)

        if not self.validator.validate_test(test):
            await self._send_message(update, self.message_manager.format_message("error_test_not_found"),
                                     InlineKeyboardMarkup([[self.keyboard_manager.create_back_button()]]))
            return TEACHER_CHECK_RESULTS

        all_results = self.db.load_all_results()
        test_results = [r for r in all_results if r["test_id"] == test_id]
        last_date = max((datetime.fromisoformat(r["timestamp"]) for r in test_results), default=None)
        last_date_str = last_date.strftime("%Y-%m-%d %H:%M") if last_date else "Никто не проходил"

        text = self.message_manager.format_message(
            "test_info",
            name=test["name"],
            subject=test["subject"],
            classes=", ".join(test.get("classes", ["Не указаны"])),
            created_at=datetime.fromisoformat(test["created_at"]).strftime("%Y-%m-%d %H:%M"),
            questions_count=len(test["questions"]),
            results_count=len(test_results),
            last_date=last_date_str
        )
        keyboard = self.keyboard_manager.create_test_menu_keyboard(test_id)
        await self._send_message(update, text, keyboard)
        self.state_manager.push(context, TEACHER_CHECK_TEST)
        return TEACHER_CHECK_TEST

    @network_retry
    async def back_to_tests(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if query:
            await query.answer()

        test_id = context.user_data.get("current_test_id")
        logger.debug(f"Выход из теста {test_id}, test_changes: {context.user_data.get('test_changes', {})}")
        if test_id and test_id in context.user_data.get("test_changes", {}):
            changes = context.user_data["test_changes"].pop(test_id, [])
            context.user_data["pending_notifications"].extend(changes)
            logger.debug(f"Добавлены изменения в pending_notifications: {changes}")
            await self.send_pending_notifications(context)

        context.user_data["current_test_id"] = None
        context.user_data["tests_page"] = 0
        return await self.start_check_results(update, context)

    @network_retry
    async def back_to_main(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if query:
            await query.answer()

        test_id = context.user_data.get("current_test_id")
        logger.debug(f"Выход в главное меню, test_changes: {context.user_data.get('test_changes', {})}")
        if test_id and test_id in context.user_data.get("test_changes", {}):
            changes = context.user_data["test_changes"].pop(test_id, [])
            context.user_data["pending_notifications"].extend(changes)
            logger.debug(f"Добавлены изменения в pending_notifications: {changes}")
            await self.send_pending_notifications(context)

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 Создать тест", callback_data="create_test")],
            [InlineKeyboardButton("📊 Проверить работы", callback_data="check_results")],
            [InlineKeyboardButton("🔙 Назад", callback_data="back")]
        ])
        await self._send_message(update, "🏠 Меню учителя:", keyboard)
        self.state_manager.clear(context)
        return ConversationHandler.END

    @network_retry
    async def stats_students(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if query:
            await query.answer()

        test_id = context.user_data.get("current_test_id")
        test = self.db.load_test_by_id(test_id)
        if not self.validator.validate_test(test):
            await self._send_message(update, self.message_manager.format_message("error_test_not_found"),
                                     InlineKeyboardMarkup([[self.keyboard_manager.create_back_button()]]))
            return TEACHER_CHECK_TEST

        all_results = self.db.load_all_results()
        test_results = [r for r in all_results if r["test_id"] == test_id]
        if not test_results:
            await self._send_message(update, self.message_manager.format_message("no_results", name=test["name"]),
                                     InlineKeyboardMarkup([[self.keyboard_manager.create_back_button()]]))
            return TEACHER_CHECK_TEST

        for result in test_results:
            test_score = sum(1 for q_idx, answer in result["answers"].items()
                             if test["questions"][int(q_idx)]["type"] == "test" and
                             answer == test["questions"][int(q_idx)]["correct_answer"])
            open_score = sum(result.get("scores", {}).get(str(q_idx), 0)
                             for q_idx, q in enumerate(test["questions"]) if q["type"] == "open")
            result["test_score"] = test_score
            result["open_score"] = open_score
            result["total_score"] = test_score + open_score

        test_results.sort(key=lambda r: r["total_score"])
        page = context.user_data.get("students_page", 0)
        total_pages = (len(test_results) + STUDENTS_PER_PAGE - 1) // STUDENTS_PER_PAGE
        start_idx = page * STUDENTS_PER_PAGE
        results_on_page = test_results[start_idx:start_idx + STUDENTS_PER_PAGE]

        students_info = ""
        for r in results_on_page:
            appeals_info = ""
            if "appeals" in r and r["appeals"]:
                appeals_info = "\nАпелляции:\n"
                for appeal in r["appeals"]:
                    status = {"pending": "Ожидает", "responded": "Отвечена"}.get(appeal["status"], appeal["status"])
                    appeals_info += (
                        f"- Вопрос #{appeal['question_idx'] + 1}: {appeal['student_comment'][:50]}... "
                        f"(Статус: {status})\n"
                    )
            students_info += (
                f"👤 {r['student_info']}\n"
                f"Тестовые: {r['test_score']}\n"
                f"Развёрнутые: {r['open_score']}\n"
                f"Общая оценка: {r['total_score']}\n"
                f"Дата: {datetime.fromisoformat(r['timestamp']).strftime('%Y-%m-%d %H:%M')}\n"
                f"{appeals_info}\n"
            )

        text = self.message_manager.format_message("students_stats", name=test["name"], students_info=students_info)
        keyboard = self.keyboard_manager.create_students_keyboard(results_on_page, page, total_pages)
        await self._send_message(update, text, keyboard)
        self.state_manager.push(context, TEACHER_CHECK_STUDENTS)
        return TEACHER_CHECK_STUDENTS

    @network_retry
    async def navigate_students(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if query:
            await query.answer()
        page = context.user_data.get("students_page", 0)
        action = query.data
        if action == "students_page_prev":
            context.user_data["students_page"] = max(0, page - 1)
        elif action == "students_page_next":
            context.user_data["students_page"] = page + 1
        return await self.stats_students(update, context)

    @network_retry
    async def view_student_questions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if query:
            await query.answer()

        test_id = context.user_data.get("current_test_id")
        result_id = query.data.replace("view_student_questions_", "") if query and query.data.startswith("view_student_questions_") else context.user_data.get("current_result_id")
        context.user_data["current_result_id"] = result_id
        q_idx = context.user_data.get("student_question_idx", 0)

        test = self.db.load_test_by_id(test_id)
        result = next((r for r in self.db.load_all_results() if r["id"] == result_id), None)
        if not self.validator.validate_test(test) or not self.validator.validate_result(result):
            await self._send_message(update, self.message_manager.format_message("error_test_not_found" if not test else "error_result_not_found"),
                                     InlineKeyboardMarkup([[self.keyboard_manager.create_back_button()]]))
            return TEACHER_CHECK_STUDENTS

        total_questions = len(test["questions"])
        q_idx = max(0, min(q_idx, total_questions - 1))
        context.user_data["student_question_idx"] = q_idx
        question = test["questions"][q_idx]

        text_parts = split_message(question["text"], TEXT_PART_LENGTH)
        part_idx = min(context.user_data.get("text_part_idx", 0), len(text_parts) - 1)
        context.user_data["text_part_idx"] = part_idx

        appeals = [a for a in result.get("appeals", []) if a["question_idx"] == q_idx and a["status"] in ["pending", "responded"]]
        has_appeal = bool(appeals)
        appeal_id = appeals[0]["id"] if has_appeal else ""
        appeal_info = ""
        if has_appeal:
            appeal = appeals[0]
            status = {"pending": "Ожидает", "responded": "Отвечена"}.get(appeal["status"], appeal["status"])
            appeal_info = (
                f"Апелляция: {appeal['student_comment']}\n"
                f"Статус: {status}\n"
                f"Дата: {datetime.fromisoformat(appeal['timestamp']).strftime('%Y-%m-%d %H:%M')}\n"
            )
            if appeal.get("teacher_comment"):
                appeal_info += f"Ответ преподавателя: {appeal['teacher_comment']}\n"

        text = self.message_manager.format_message(
            "student_question",
            q_idx=q_idx + 1,
            total_questions=total_questions,
            question_text=text_parts[part_idx],
            correct_answer=question["correct_answer"],
            check_comment=question.get("check_comment", "Нет"),
            student_info=result["student_info"],
            answer=result["answers"].get(str(q_idx), "Нет ответа"),
            score=result.get("scores", {}).get(str(q_idx), 0),
            comment=result.get("comments", {}).get(str(q_idx), "Нет"),
            appeal_info=appeal_info
        )
        keyboard = self.keyboard_manager.create_student_questions_keyboard(q_idx, total_questions, result_id, len(text_parts), has_appeal, appeal_id)
        await self._send_message(update, text, keyboard)
        self.state_manager.push(context, TEACHER_VIEW_STUDENT_QUESTIONS)
        return TEACHER_VIEW_STUDENT_QUESTIONS

    @network_retry
    async def navigate_student_questions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if query:
            await query.answer()
        q_idx = context.user_data.get("student_question_idx", 0)
        part_idx = context.user_data.get("text_part_idx", 0)
        action = query.data
        if action == "question_prev":
            context.user_data["student_question_idx"] = q_idx - 1
            context.user_data["text_part_idx"] = 0
        elif action == "question_next":
            context.user_data["student_question_idx"] = q_idx + 1
            context.user_data["text_part_idx"] = 0
        elif action == "text_part_prev":
            context.user_data["text_part_idx"] = max(0, part_idx - 1)
        elif action == "text_part_next":
            context.user_data["text_part_idx"] = part_idx + 1
        return await self.view_student_questions(update, context)

    @network_retry
    async def back_to_students(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if query:
            await query.answer()
        context.user_data["current_result_id"] = None
        context.user_data["student_question_idx"] = 0
        context.user_data["text_part_idx"] = 0
        return await self.stats_students(update, context)

    @network_retry
    async def stats_questions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if query:
            await query.answer()

        test_id = context.user_data.get("current_test_id")
        test = self.db.load_test_by_id(test_id)
        if not self.validator.validate_test(test):
            await self._send_message(update, self.message_manager.format_message("error_test_not_found"),
                                     InlineKeyboardMarkup([[self.keyboard_manager.create_back_button()]]))
            return TEACHER_CHECK_TEST

        all_results = self.db.load_all_results()
        test_results = [r for r in all_results if r["test_id"] == test_id]
        page = context.user_data.get("questions_page", 0)
        total_questions = len(test["questions"])
        total_pages = (total_questions + QUESTIONS_PER_PAGE - 1) // QUESTIONS_PER_PAGE
        start_idx = page * QUESTIONS_PER_PAGE
        questions_on_page = test["questions"][start_idx:start_idx + QUESTIONS_PER_PAGE]
        indices = list(range(start_idx, min(start_idx + QUESTIONS_PER_PAGE, total_questions)))

        questions_info = ""
        for idx, question in zip(indices, questions_on_page):
            answered = sum(1 for r in test_results if str(idx) in r["answers"])
            question_text = f"❓ Вопрос #{idx + 1}: {question['text'][:200]}{'...' if len(question['text']) > 200 else ''}\n"
            if question["type"] == "test":
                question_text += "Варианты ответов:\n"
                for option in question["options"]:
                    is_correct = option == question["correct_answer"]
                    students = [r["student_info"] for r in test_results if str(idx) in r["answers"] and r["answers"][str(idx)] == option]
                    question_text += (
                        f"{'✅' if is_correct else '  '} {option[:100]}: "
                        f"{', '.join(students[:5]) if students else 'Никто'}{'...' if len(students) > 5 else ''}\n"
                    )
                question_text += f"Ответили: {answered}\n\n"
            else:
                correct_answer = question["correct_answer"][:100] + ("..." if len(question["correct_answer"]) > 100 else "")
                check_comment = question.get("check_comment", "")[:100] + ("..." if len(question.get("check_comment", "")) > 100 else "")
                scores = [r.get("scores", {}).get(str(idx), 0) for r in test_results if str(idx) in r["answers"]]
                avg_score = (sum(scores) / len(scores)) if scores else 0
                question_text += (
                    f"Правильный ответ: {correct_answer}\n"
                    f"Комментарий: {check_comment or 'Нет'}\n"
                    f"Средняя оценка: {avg_score:.2f}\n"
                    f"Ответили: {answered}\n\n"
                )
            questions_info += question_text

        text_parts = split_message(questions_info, TEXT_PART_LENGTH)
        part_idx = min(context.user_data.get("text_part_idx", 0), len(text_parts) - 1)
        text = self.message_manager.format_message("questions_stats", name=test["name"], questions_info=text_parts[part_idx])
        keyboard = self.keyboard_manager.create_questions_keyboard(indices, page, total_pages, len(text_parts))
        await self._send_message(update, text, keyboard)
        self.state_manager.push(context, TEACHER_CHECK_QUESTIONS)
        return TEACHER_CHECK_QUESTIONS

    @network_retry
    async def navigate_questions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if query:
            await query.answer()
        page = context.user_data.get("questions_page", 0)
        part_idx = context.user_data.get("text_part_idx", 0)
        action = query.data
        if action == "questions_page_prev":
            context.user_data["questions_page"] = max(0, page - 1)
            context.user_data["text_part_idx"] = 0
        elif action == "questions_page_next":
            context.user_data["questions_page"] = page + 1
            context.user_data["text_part_idx"] = 0
        elif action == "text_part_prev":
            context.user_data["text_part_idx"] = max(0, part_idx - 1)
        elif action == "text_part_next":
            context.user_data["text_part_idx"] = part_idx + 1
        return await self.stats_questions(update, context)

    @network_retry
    async def view_question_answers(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if query:
            await query.answer()

        test_id = context.user_data.get("current_test_id")
        q_idx = int(query.data.replace("view_answers_", "")) if query and query.data.startswith("view_answers_") else context.user_data.get("current_question_idx")
        context.user_data["current_question_idx"] = q_idx
        context.user_data["current_result_id"] = None

        test = self.db.load_test_by_id(test_id)
        if not self.validator.validate_test(test):
            await self._send_message(update, self.message_manager.format_message("error_test_not_found"),
                                     InlineKeyboardMarkup([[self.keyboard_manager.create_back_button()]]))
            return TEACHER_CHECK_QUESTIONS

        question = test["questions"][q_idx]
        all_results = self.db.load_all_results()
        test_results = [r for r in all_results if r["test_id"] == test_id and str(q_idx) in r["answers"]]

        for r in test_results:
            if "scores" not in r:
                logger.warning(f"Result {r['id']} is missing 'scores' field, initializing as empty dict")
                r["scores"] = {}
            r["test"] = test  # Добавляем тест для использования в create_answers_keyboard

        page = context.user_data.get("answers_page", 0)
        total_pages = (len(test_results) + ANSWERS_PER_PAGE - 1) // ANSWERS_PER_PAGE
        start_idx = page * ANSWERS_PER_PAGE
        results_on_page = test_results[start_idx:start_idx + ANSWERS_PER_PAGE]

        text_parts = split_message(question["text"], TEXT_PART_LENGTH)
        part_idx = min(context.user_data.get("text_part_idx", 0), len(text_parts) - 1)
        context.user_data["text_part_idx"] = part_idx

        all_appeals = self.db.load_all_appeals()
        answers_info = ""
        for result in results_on_page:
            answer = result["answers"][str(q_idx)]
            score = result["scores"].get(str(q_idx), 0)
            comment = result.get("comments", {}).get(str(q_idx), "Нет")
            appeals = [a for a in result.get("appeals", []) if a["question_idx"] == q_idx and a["status"] in ["pending", "responded"]]
            answers_info += (
                f"👤 {result['student_info']}\n"
                f"Ответ: {answer}\n"
                f"Оценка: {score}\n"
                f"Комментарий учителя: {comment}\n"
            )
            for appeal in appeals:
                status = {"pending": "Ожидает", "responded": "Отвечена"}.get(appeal["status"], appeal["status"])
                answers_info += (
                    f"Апелляция: {appeal['student_comment']}\n"
                    f"Статус: {status}\n"
                    f"Дата: {datetime.fromisoformat(appeal['timestamp']).strftime('%Y-%m-%d %H:%M')}\n"
                )
                if appeal.get("teacher_comment"):
                    answers_info += f"Ответ преподавателя: {appeal['teacher_comment']}\n"
            answers_info += "\n"

        text = self.message_manager.format_message(
            "question_answers",
            q_idx=q_idx + 1,
            question_text=text_parts[part_idx],
            correct_answer=question["correct_answer"],
            check_comment=question.get("check_comment", "Нет"),
            answers_info=answers_info
        )
        keyboard = self.keyboard_manager.create_answers_keyboard(results_on_page, q_idx, page, total_pages, len(text_parts), all_appeals)
        await self._send_message(update, text, keyboard)
        self.state_manager.push(context, TEACHER_CHECK_ANSWERS)
        return TEACHER_CHECK_ANSWERS

    @network_retry
    async def navigate_answers(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if query:
            await query.answer()
        page = context.user_data.get("answers_page", 0)
        q_idx = context.user_data.get("current_question_idx", 0)
        part_idx = context.user_data.get("text_part_idx", 0)
        action = query.data
        if action == "answers_page_prev":
            context.user_data["answers_page"] = max(0, page - 1)
        elif action == "answers_page_next":
            context.user_data["answers_page"] = page + 1
        elif action == "question_prev":
            context.user_data["current_question_idx"] = max(0, q_idx - 1)
            context.user_data["text_part_idx"] = 0
        elif action == "question_next":
            context.user_data["current_question_idx"] = q_idx + 1
            context.user_data["text_part_idx"] = 0
        elif action == "text_part_prev":
            context.user_data["text_part_idx"] = max(0, part_idx - 1)
        elif action == "text_part_next":
            context.user_data["text_part_idx"] = part_idx + 1
        return await self.view_question_answers(update, context)

    @network_retry
    async def back_to_questions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if query:
            await query.answer()
        context.user_data["current_question_idx"] = None
        context.user_data["answers_page"] = 0
        context.user_data["text_part_idx"] = 0
        return await self.stats_questions(update, context)

    @network_retry
    async def edit_score(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if query:
            await query.answer()

        parts = query.data.split("_")
        result_id, q_idx = parts[2], int(parts[3])
        context.user_data["current_result_id"] = result_id
        context.user_data["current_question_idx"] = q_idx
        context.user_data["return_state"] = self.state_manager.get_current(context) or TEACHER_CHECK_ANSWERS

        test_id = context.user_data.get("current_test_id")
        test = self.db.load_test_by_id(test_id)
        result = next((r for r in self.db.load_all_results() if r["id"] == result_id), None)
        if not self.validator.validate_test(test) or not self.validator.validate_result(result):
            await self._send_message(update, self.message_manager.format_message("error_test_not_found" if not test else "error_result_not_found"),
                                     InlineKeyboardMarkup([[self.keyboard_manager.create_back_button()]]))
            return context.user_data["return_state"]

        question = test["questions"][q_idx]
        text = self.message_manager.format_message(
            "edit_score_prompt",
            q_idx=q_idx + 1,
            question_text=question["text"],
            student_info=result["student_info"],
            answer=result["answers"].get(str(q_idx), "Нет ответа"),
            score=result.get("scores", {}).get(str(q_idx), 0),
            comment=result.get("comments", {}).get(str(q_idx), "Нет")
        )
        await self._send_message(update, text, InlineKeyboardMarkup([[self.keyboard_manager.create_back_button()]]))
        self.state_manager.push(context, TEACHER_EDIT_SCORE)
        return TEACHER_EDIT_SCORE

    @network_retry
    async def save_score(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if not update.message:
            logger.error("Ожидалось сообщение")
            return TEACHER_EDIT_SCORE

        result_id = context.user_data.get("current_result_id")
        q_idx = context.user_data.get("current_question_idx")
        test_id = context.user_data.get("current_test_id")
        return_state = context.user_data.get("return_state", TEACHER_CHECK_ANSWERS)

        if not all([result_id, q_idx is not None, test_id]):
            await update.message.reply_text(self.message_manager.format_message("error_missing_data"))
            return return_state

        score = self.validator.validate_score(update.message.text.strip())
        if score is None:
            await update.message.reply_text(self.message_manager.format_message("error_invalid_score"))
            return TEACHER_EDIT_SCORE

        results_data = self.db._load_results_file()
        result = None
        for user_id, user_data in results_data.items():
            for r in user_data.get("tests", []):
                if r["id"] == result_id:
                    result = r
                    break
            if result:
                break

        if not result:
            await update.message.reply_text(self.message_manager.format_message("error_result_not_found"))
            return return_state

        old_score = result.get("scores", {}).get(str(q_idx), 0)
        if old_score == score:
            logger.debug(f"Оценка для результата {result_id}, вопрос {q_idx} не изменилась: {score}")
            await update.message.reply_text(self.message_manager.format_message("score_saved"))
            return await self._return_to_previous(update, context)

        result.setdefault("scores", {})[str(q_idx)] = score
        self.db._save_to_file(self.db.results_file, results_data)
        logger.info(f"Сохранена оценка {score} для результата {result_id}, вопрос {q_idx}")

        test = self.db.load_test_by_id(test_id)
        test_name = test["name"] if test else "Неизвестный тест"
        question_text = test["questions"][q_idx]["text"][:50] + "..." if test else "Неизвестный вопрос"
        comment = result.get("comments", {}).get(str(q_idx), "Нет")
        student_id = result.get("user_id")
        if not student_id:
            student_id = self._get_user_id_by_result_id(result_id)
            if not student_id:
                logger.warning(f"Отсутствует user_id в результате {result_id}. Пропускаем уведомление. Результат: {result}")
            else:
                self._add_change(context, {
                    "type": "score",
                    "student_id": student_id,
                    "test_id": test_id,
                    "test_name": test_name,
                    "question_idx": q_idx + 1,
                    "question_text": question_text,
                    "score": score,
                    "comment": comment
                })

        await update.message.reply_text(self.message_manager.format_message("score_saved"))
        return await self._return_to_previous(update, context)

    @network_retry
    async def add_comment(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if query:
            await query.answer()

        parts = query.data.split("_")
        result_id, q_idx = parts[2], int(parts[3])
        context.user_data["current_result_id"] = result_id
        context.user_data["current_question_idx"] = q_idx
        context.user_data["return_state"] = self.state_manager.get_current(context) or TEACHER_CHECK_ANSWERS

        test_id = context.user_data.get("current_test_id")
        test = self.db.load_test_by_id(test_id)
        result = next((r for r in self.db.load_all_results() if r["id"] == result_id), None)
        if not self.validator.validate_test(test) or not self.validator.validate_result(result):
            await self._send_message(update, self.message_manager.format_message("error_test_not_found" if not test else "error_result_not_found"),
                                     InlineKeyboardMarkup([[self.keyboard_manager.create_back_button()]]))
            return context.user_data["return_state"]

        question = test["questions"][q_idx]
        text = self.message_manager.format_message(
            "add_comment_prompt",
            q_idx=q_idx + 1,
            question_text=question["text"],
            student_info=result["student_info"],
            answer=result["answers"].get(str(q_idx), "Нет ответа"),
            score=result.get("scores", {}).get(str(q_idx), 0),
            comment=result.get("comments", {}).get(str(q_idx), "Нет")
        )
        await self._send_message(update, text, InlineKeyboardMarkup([[self.keyboard_manager.create_back_button()]]))
        self.state_manager.push(context, TEACHER_ADD_COMMENT)
        return TEACHER_ADD_COMMENT

    @network_retry
    async def save_comment(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if not update.message:
            logger.error("Ожидалось сообщение")
            return TEACHER_ADD_COMMENT

        result_id = context.user_data.get("current_result_id")
        q_idx = context.user_data.get("current_question_idx")
        test_id = context.user_data.get("current_test_id")
        return_state = context.user_data.get("return_state", TEACHER_CHECK_ANSWERS)

        if not all([result_id, q_idx is not None, test_id]):
            await update.message.reply_text(self.message_manager.format_message("error_missing_data"))
            return return_state

        comment = sanitize_input(update.message.text)
        if not self.validator.validate_comment(comment):
            await update.message.reply_text(self.message_manager.format_message("error_empty_comment"))
            return TEACHER_ADD_COMMENT

        results_data = self.db._load_results_file()
        result = None
        for user_id, user_data in results_data.items():
            for r in user_data.get("tests", []):
                if r["id"] == result_id:
                    result = r
                    break
            if result:
                break

        if not result:
            logger.error(f"Результат с ID {result_id} не найден")
            await update.message.reply_text(self.message_manager.format_message("error_result_not_found"))
            return return_state

        old_comment = result.get("comments", {}).get(str(q_idx), "")
        if old_comment == comment:
            logger.debug(f"Комментарий для результата {result_id}, вопрос {q_idx} не изменился")
            await update.message.reply_text(self.message_manager.format_message("comment_saved"))
            return await self._return_to_previous(update, context)

        result.setdefault("comments", {})[str(q_idx)] = comment
        self.db._save_to_file(self.db.results_file, results_data)
        logger.info(f"Сохранён комментарий для результата {result_id}, вопрос {q_idx}")

        test = self.db.load_test_by_id(test_id)
        test_name = test["name"] if test else "Неизвестный тест"
        question_text = test["questions"][q_idx]["text"][:50] + "..." if test else "Неизвестный вопрос"
        score = result.get("scores", {}).get(str(q_idx), 0)
        student_id = result.get("user_id")
        if not student_id:
            student_id = self._get_user_id_by_result_id(result_id)
            if not student_id:
                logger.warning(f"Отсутствует user_id в результате {result_id}. Пропускаем уведомление. Результат: {result}")
            else:
                self._add_change(context, {
                    "type": "comment",
                    "student_id": student_id,
                    "test_id": test_id,
                    "test_name": test_name,
                    "question_idx": q_idx + 1,
                    "question_text": question_text,
                    "score": score,
                    "comment": comment
                })

        await update.message.reply_text(self.message_manager.format_message("comment_saved"))
        return await self._return_to_previous(update, context)

    @network_retry
    async def view_appeals(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if query:
            await query.answer()

        test_id = context.user_data.get("current_test_id")
        test = self.db.load_test_by_id(test_id)
        if not self.validator.validate_test(test):
            await self._send_message(update, self.message_manager.format_message("error_test_not_found"),
                                     InlineKeyboardMarkup([[self.keyboard_manager.create_back_button()]]))
            return TEACHER_CHECK_TEST

        all_appeals = self.db.load_all_appeals()
        test_appeals = [a for a in all_appeals if a["test_id"] == test_id]
        if not test_appeals:
            await self._send_message(update, self.message_manager.format_message("no_appeals", name=test["name"]),
                                     InlineKeyboardMarkup([[self.keyboard_manager.create_back_button()]]))
            return TEACHER_CHECK_TEST

        page = context.user_data.get("appeals_page", 0)
        total_pages = (len(test_appeals) + APPEALS_PER_PAGE - 1) // APPEALS_PER_PAGE
        start_idx = page * APPEALS_PER_PAGE
        appeals_on_page = test_appeals[start_idx:start_idx + APPEALS_PER_PAGE]

        all_results = self.db.load_all_results()
        appeals_info = ""
        for appeal in appeals_on_page:
            question = test["questions"][appeal["question_idx"]]
            result = next((r for r in all_results if r["test_id"] == test_id and str(appeal["question_idx"]) in r["answers"]), None)
            student_info = result["student_info"] if result else "Неизвестный студент"
            score = result.get("scores", {}).get(str(appeal["question_idx"]), 0) if result else 0
            status = {"pending": "Ожидает", "responded": "Отвечена"}.get(appeal["status"], appeal["status"])
            appeals_info += (
                f"❓ Вопрос #{appeal['question_idx'] + 1}: {question['text'][:200]}...\n"
                f"Правильный ответ: {question['correct_answer'][:100]}...\n"
                f"👤 {student_info}\n"
                f"Оценка: {score}\n"
                f"Апелляция: {appeal['student_comment']}\n"
                f"Статус: {status}\n"
                f"Дата: {datetime.fromisoformat(appeal['timestamp']).strftime('%Y-%m-%d %H:%M')}\n"
            )
            if appeal.get("teacher_comment"):
                appeals_info += f"Ответ преподавателя: {appeal['teacher_comment']}\n"
            appeals_info += "\n"

        text = self.message_manager.format_message("appeals_list", name=test["name"], appeals_info=appeals_info)
        keyboard = self.keyboard_manager.create_appeals_keyboard(appeals_on_page, all_results, page, total_pages)
        await self._send_message(update, text, keyboard)
        self.state_manager.push(context, TEACHER_CHECK_APPEALS)
        return TEACHER_CHECK_APPEALS

    @network_retry
    async def navigate_appeals(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if query:
            await query.answer()
        page = context.user_data.get("appeals_page", 0)
        action = query.data
        if action == "appeals_page_prev":
            context.user_data["appeals_page"] = max(0, page - 1)
        elif action == "appeals_page_next":
            context.user_data["appeals_page"] = page + 1
        return await self.view_appeals(update, context)

    @network_retry
    async def start_appeal_response(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if query:
            await query.answer()

        appeal_id = query.data.replace("respond_appeal_", "")
        context.user_data["current_appeal_id"] = appeal_id
        context.user_data["return_state"] = self.state_manager.get_current(context) or TEACHER_CHECK_APPEALS

        appeal = next((a for a in self.db.load_all_appeals() if a["id"] == appeal_id), None)
        if not self.validator.validate_appeal(appeal):
            await self._send_message(update, self.message_manager.format_message("error_appeal_not_found"),
                                     InlineKeyboardMarkup([[self.keyboard_manager.create_back_button()]]))
            return context.user_data["return_state"]

        test = self.db.load_test_by_id(appeal["test_id"])
        question = test["questions"][appeal["question_idx"]] if test else {"text": "Неизвестный вопрос", "correct_answer": "Неизвестно"}
        result = next((r for r in self.db.load_all_results() if r["test_id"] == appeal["test_id"] and str(appeal["question_idx"]) in r["answers"]), None)
        student_info = result["student_info"] if result else "Неизвестный"
        score = result.get("scores", {}).get(str(appeal["question_idx"]), 0) if result else 0

        teacher_comment = f"Текущий ответ преподавателя: {appeal['teacher_comment']}\n" if appeal.get("teacher_comment") else ""
        text = self.message_manager.format_message(
            "appeal_response_prompt",
            q_idx=appeal["question_idx"] + 1,
            question_text=question["text"][:200],
            correct_answer=question["correct_answer"][:100],
            student_info=student_info,
            answer=result["answers"].get(str(appeal["question_idx"]), "Нет ответа") if result else "Нет ответа",
            score=score,
            student_comment=appeal["student_comment"],
            teacher_comment=teacher_comment
        )
        await self._send_message(update, text, InlineKeyboardMarkup([[self.keyboard_manager.create_back_button()]]))
        self.state_manager.push(context, TEACHER_RESPOND_APPEAL)
        return TEACHER_RESPOND_APPEAL

    @network_retry
    async def save_appeal_response(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if not update.message:
            logger.error("Ожидалось сообщение")
            return TEACHER_RESPOND_APPEAL

        appeal_id = context.user_data.get("current_appeal_id")
        test_id = context.user_data.get("current_test_id")
        return_state = context.user_data.get("return_state", TEACHER_CHECK_APPEALS)
        if not appeal_id or not test_id:
            await update.message.reply_text(self.message_manager.format_message("error_missing_data"))
            return return_state

        comment = sanitize_input(update.message.text)
        if not self.validator.validate_comment(comment):
            await update.message.reply_text(self.message_manager.format_message("error_empty_comment"))
            return TEACHER_RESPOND_APPEAL

        appeals = self.db.load_all_appeals()
        appeal = next((a for a in appeals if a["id"] == appeal_id), None)
        if not appeal:
            await update.message.reply_text(self.message_manager.format_message("error_appeal_not_found"))
            return return_state

        results_data = self.db._load_results_file()
        result = None
        for user_id, user_data in results_data.items():
            for r in user_data.get("tests", []):
                if r["test_id"] == appeal["test_id"] and str(appeal["question_idx"]) in r["answers"]:
                    result = r
                    for result_appeal in r.get("appeals", []):
                        if result_appeal["id"] == appeal_id:
                            if result_appeal.get("teacher_comment") == comment:
                                logger.debug(f"Ответ на апелляцию {appeal_id} не изменился")
                                await update.message.reply_text(self.message_manager.format_message("appeal_response_saved"))
                                return await self._return_to_previous(update, context)
                            result_appeal["status"] = "responded"
                            result_appeal["teacher_comment"] = comment
                            result_appeal["response_timestamp"] = datetime.now().isoformat()
                            break
                    self.db._save_to_file(self.db.results_file, results_data)
                    break

        test = self.db.load_test_by_id(appeal["test_id"])
        test_name = test["name"] if test else "Неизвестный тест"
        question_idx = appeal["question_idx"]
        question_text = test["questions"][question_idx]["text"][:50] + "..." if test else "Неизвестный вопрос"
        score = result.get("scores", {}).get(str(question_idx), 0) if result else 0
        student_id = result.get("user_id") if result else None
        if not student_id:
            student_id = self._get_user_id_by_result_id(result["id"]) if result else None
            if not student_id:
                logger.warning(f"Отсутствует user_id в результате для апелляции {appeal_id}. Пропускаем уведомление.")
            else:
                self._add_change(context, {
                    "type": "appeal",
                    "student_id": student_id,
                    "test_id": appeal["test_id"],
                    "test_name": test_name,
                    "question_idx": question_idx + 1,
                    "question_text": question_text,
                    "score": score,
                    "comment": comment
                })

        await update.message.reply_text(self.message_manager.format_message("appeal_response_saved"))
        return await self._return_to_previous(update, context)

    @network_retry
    async def send_pending_notifications(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        notifications = context.user_data.get("pending_notifications", [])
        logger.debug(f"Отправка уведомлений: {notifications}")
        for notification in notifications:
            try:
                if notification["type"] == "score":
                    text = (
                        f"Изменена оценка по тесту '{notification['test_name']}', "
                        f"вопрос #{notification['question_idx']}: {notification['question_text']}\n"
                        f"Новая оценка: {notification['score']}\n"
                        f"Комментарий преподавателя: {notification['comment']}"
                    )
                elif notification["type"] == "comment":
                    text = (
                        f"Добавлен/изменён комментарий по тесту '{notification['test_name']}', "
                        f"вопрос #{notification['question_idx']}: {notification['question_text']}\n"
                        f"Оценка: {notification['score']}\n"
                        f"Комментарий преподавателя: {notification['comment']}"
                    )
                else:  # appeal
                    text = (
                        f"Получен ответ на апелляцию по тесту '{notification['test_name']}', "
                        f"вопрос #{notification['question_idx']}: {notification['question_text']}\n"
                        f"Оценка: {notification['score']}\n"
                        f"Комментарий преподавателя: {notification['comment']}"
                    )
                await context.bot.send_message(
                    chat_id=notification["student_id"],
                    text=text
                )
                logger.info(f"Отправлено уведомление студенту {notification['student_id']}, тип: {notification['type']}")
            except Exception as e:
                logger.error(f"Ошибка отправки уведомления студенту {notification['student_id']}: {e}")
        context.user_data["pending_notifications"] = []
        logger.debug("Очередь уведомлений очищена")

    @network_retry
    async def exit_appeals(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if query:
            await query.answer()

        test_id = context.user_data.get("current_test_id")
        logger.debug(f"Выход из апелляций, test_changes: {context.user_data.get('test_changes', {})}")
        if test_id and test_id in context.user_data.get("test_changes", {}):
            changes = context.user_data["test_changes"].pop(test_id, [])
            context.user_data["pending_notifications"].extend(changes)
            logger.debug(f"Добавлены изменения в pending_notifications: {changes}")
            await self.send_pending_notifications(context)

        context.user_data["appeals_page"] = 0
        context.user_data["temp_test_id"] = context.user_data.get("current_test_id")
        return await self.select_test(update, context)

    def get_conversation_handler(self) -> ConversationHandler:
        return ConversationHandler(
            entry_points=[CallbackQueryHandler(self.start_check_results, pattern="^check_results$")],
            states={
                TEACHER_CHECK_RESULTS: [
                    CallbackQueryHandler(self.navigate_tests, pattern="^tests_page_(prev|next)$"),
                    CallbackQueryHandler(self.select_test, pattern="^select_test_"),
                    CallbackQueryHandler(self.back_to_main, pattern="^back$"),
                ],
                TEACHER_CHECK_TEST: [
                    CallbackQueryHandler(self.stats_students, pattern="^stats_students_"),
                    CallbackQueryHandler(self.stats_questions, pattern="^stats_questions_"),
                    CallbackQueryHandler(self.view_appeals, pattern="^view_appeals_"),
                    CallbackQueryHandler(self.back_to_tests, pattern="^back$"),
                ],
                TEACHER_CHECK_STUDENTS: [
                    CallbackQueryHandler(self.navigate_students, pattern="^students_page_(prev|next)$"),
                    CallbackQueryHandler(self.view_student_questions, pattern="^view_student_questions_"),
                    CallbackQueryHandler(self.exit_appeals, pattern="^back$"),
                ],
                TEACHER_VIEW_STUDENT_QUESTIONS: [
                    CallbackQueryHandler(self.navigate_student_questions, pattern="^(question_(prev|next)|text_part_(prev|next))$"),
                    CallbackQueryHandler(self.edit_score, pattern="^edit_score_"),
                    CallbackQueryHandler(self.add_comment, pattern="^add_comment_"),
                    CallbackQueryHandler(self.start_appeal_response, pattern="^respond_appeal_"),
                    CallbackQueryHandler(self.back_to_students, pattern="^back$"),
                ],
                TEACHER_CHECK_QUESTIONS: [
                    CallbackQueryHandler(self.navigate_questions, pattern="^(questions_page_(prev|next)|text_part_(prev|next))$"),
                    CallbackQueryHandler(self.view_question_answers, pattern="^view_answers_"),
                    CallbackQueryHandler(self.exit_appeals, pattern="^back$"),
                ],
                TEACHER_CHECK_ANSWERS: [
                    CallbackQueryHandler(self.navigate_answers, pattern="^(answers_page_(prev|next)|question_(prev|next)|text_part_(prev|next))$"),
                    CallbackQueryHandler(self.edit_score, pattern="^edit_score_"),
                    CallbackQueryHandler(self.add_comment, pattern="^add_comment_"),
                    CallbackQueryHandler(self.start_appeal_response, pattern="^respond_appeal_"),
                    CallbackQueryHandler(self.back_to_questions, pattern="^back$"),
                ],
                TEACHER_EDIT_SCORE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.save_score),
                    CallbackQueryHandler(self._return_to_previous, pattern="^back$"),
                ],
                TEACHER_ADD_COMMENT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.save_comment),
                    CallbackQueryHandler(self._return_to_previous, pattern="^back$"),
                ],
                TEACHER_CHECK_APPEALS: [
                    CallbackQueryHandler(self.navigate_appeals, pattern="^appeals_page_(prev|next)$"),
                    CallbackQueryHandler(self.edit_score, pattern="^edit_score_"),
                    CallbackQueryHandler(self.start_appeal_response, pattern="^respond_appeal_"),
                    CallbackQueryHandler(self.add_comment, pattern="^add_comment_"),
                    CallbackQueryHandler(self.exit_appeals, pattern="^back$"),
                ],
                TEACHER_RESPOND_APPEAL: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.save_appeal_response),
                    CallbackQueryHandler(self._return_to_previous, pattern="^back$"),
                ],
            },
            fallbacks=[],
        )