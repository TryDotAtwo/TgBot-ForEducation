from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    CommandHandler
)
from telegram.error import BadRequest, NetworkError, TimedOut
import logging
from datetime import datetime
from functools import wraps
from tenacity import retry, stop_after_attempt, wait_fixed
import re
from states import STUDENT_VIEW_RESULTS, STUDENT_VIEW_TEST_DETAILS, STUDENT_MAIN, CHOOSE_ROLE
from utils import create_back_button

logger = logging.getLogger(__name__)

# Константы
MAX_MESSAGE_LENGTH = 4000
TEXT_PART_LENGTH = 1000
TIMESTAMP_FORMAT = '%Y-%m-%d %H:%M'
TESTS_PER_PAGE = 5

# Утилиты
def sanitize_input(text: str) -> str:
    """Санитизация входных данных для предотвращения проблем с отображением."""
    if not text:
        return text
    text = re.sub(r'[<>|&]', '', text)
    text = re.sub(r'\s+', ' ', text.strip())
    return text

def split_message(text: str, max_length: int) -> list:
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

# Классы
class StateManager:
    """Управление стеком состояний."""
    def __init__(self, context):
        self.context = context
        if "state_stack" not in self.context.user_data:
            self.context.user_data["state_stack"] = []

    def push(self, state):
        self.context.user_data["state_stack"].append(state)
        logger.debug(f"Pushed state: {state}")

    def pop(self):
        if self.context.user_data["state_stack"]:
            return self.context.user_data["state_stack"].pop()
        return None

    def current(self):
        return self.context.user_data["state_stack"][-1] if self.context.user_data["state_stack"] else None

class StudentResultMessageManager:
    """Управление текстовыми сообщениями."""
    MESSAGES = {
        "no_results": "📭 Нет доступных работ. Вернитесь в меню:",
        "view_results": "📊 Ваши работы:",
        "test_not_found": "❌ Тест не найден.",
        "result_not_found": "❌ Результаты теста не найдены.",
        "no_data": "📜 Нет данных.",
        "student_main": "🏠 Меню учащегося:",
        "error": "❌ Произошла ошибка. Пожалуйста, начните заново.",
        "test_details": (
            "📋 Результаты теста: {name}\n"
            "Предмет: {subject}\n"
            "Классы: {classes}\n"
            "Дата завершения: {completed_at}\n\n"
        ),
        "question_open": (
            "❓ Вопрос {idx}: {text}\n"
            "Ваш ответ: {student_answer}\n"
            "Оценка: {score}\n"
        ),
        "question_test": (
            "❓ Вопрос {idx}: {text}\n"
            "Варианты ответа:\n{options}\n"
            "Ваш ответ: {student_answer}\n"
            "Оценка: {score}\n"
        ),
        "teacher_comment": "Комментарий учителя: {comment}\n",
        "no_teacher_comment": "Учитель не оставил комментарий\n",
        "model_comment": "Комментарий модели: {comment}\n",
        "appeal": (
            "📢 Апелляция (отправлена {time}):\n"
            "Комментарий: {student_comment}\n"
            "Статус: {status}\n"
            "Ответ учителя: {teacher_comment}\n" if "{teacher_comment}" else ""
        ),
    }

    @staticmethod
    def get_message(key, **kwargs):
        """Получение сообщения с подстановкой параметров и ограничением длины."""
        message = StudentResultMessageManager.MESSAGES[key].format(**kwargs)
        return message[:MAX_MESSAGE_LENGTH] if len(message) > MAX_MESSAGE_LENGTH else message

class StudentResultKeyboardManager:
    """Управление клавиатурами."""
    @staticmethod
    def create_back_button(action="back"):
        return InlineKeyboardButton("🔙 Назад", callback_data=action)

    @staticmethod
    def create_student_main_menu():
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 Начать проверочную работу", callback_data="start_test")],
            [InlineKeyboardButton("📊 Посмотреть работы", callback_data="view_results")],
            [StudentResultKeyboardManager.create_back_button("back")]
        ])

    @staticmethod
    def create_test_list(tests, page=0):
        """Создание списка тестов с пагинацией, включая дату и время прохождения."""
        total_tests = len(tests)
        total_pages = (total_tests + TESTS_PER_PAGE - 1) // TESTS_PER_PAGE
        start_idx = page * TESTS_PER_PAGE
        end_idx = min(start_idx + TESTS_PER_PAGE, total_tests)
        current_tests = tests[start_idx:end_idx]

        keyboard = []
        for i, test in enumerate(current_tests, start=start_idx):
            test_name = sanitize_input(test.get("name", "Без названия"))
            timestamp = test.get("timestamp")
            if timestamp:
                try:
                    completed_at = datetime.fromisoformat(timestamp)
                    formatted_date = completed_at.strftime('%d.%m.%Y %H:%M')
                except ValueError:
                    formatted_date = "Неизвестно"
            else:
                formatted_date = "Неизвестно"
            button_text = f"{test_name} ({formatted_date})"
            callback_data = f"view_{i}"
            button = InlineKeyboardButton(button_text, callback_data=callback_data)
            keyboard.append([button])

        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ Пред. страница", callback_data=f"page_{page-1}"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("След. страница ➡️", callback_data=f"page_{page+1}"))
        if nav_buttons:
            keyboard.append(nav_buttons)

        keyboard.append([StudentResultKeyboardManager.create_back_button("back_to_main")])
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def create_test_details_navigation(part_idx, total_parts):
        keyboard = []
        nav_buttons = []
        if part_idx > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ Пред. часть", callback_data="prev_report_part"))
        if part_idx < total_parts - 1:
            nav_buttons.append(InlineKeyboardButton("След. часть ➡️", callback_data="next_report_part"))
        if nav_buttons:
            keyboard.append(nav_buttons)
        keyboard.append([InlineKeyboardButton("📜 К списку тестов", callback_data="back_to_list")])
        keyboard.append([StudentResultKeyboardManager.create_back_button()])
        return InlineKeyboardMarkup(keyboard)

class StudentResultValidator:
    """Валидация данных."""
    @staticmethod
    def validate_test_result(result):
        return isinstance(result, dict) and "id" in result and "test_id" in result

    @staticmethod
    def validate_test(test):
        return isinstance(test, dict) and "name" in test and "questions" in test

class StudentTestResultsViewer:
    """Класс для отображения результатов тестов студентов."""
    def __init__(self, db):
        self.db = db

    @staticmethod
    def network_retry(func):
        """Декоратор для повторных попыток при сетевых ошибках."""
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

    @network_retry
    async def safe_edit_message(self, query, text, reply_markup=None):
        """Безопасное редактирование сообщения."""
        try:
            current_text = query.message.text or ""
            current_markup = query.message.reply_markup
            new_text = text[:MAX_MESSAGE_LENGTH]
            new_markup = reply_markup

            if new_text == current_text:
                if new_markup is None and current_markup is None:
                    logger.debug("Сообщение и клавиатура не изменились, пропускаем редактирование")
                    return
                if new_markup and current_markup:
                    new_buttons = [[btn.text for btn in row] for row in new_markup.inline_keyboard]
                    current_buttons = [[btn.text for btn in row] for row in current_markup.inline_keyboard]
                    if new_buttons == current_buttons:
                        logger.debug("Сообщение и клавиатура не изменились, пропускаем редактирование")
                        return

            logger.debug(f"Редактируем сообщение: {new_text[:50]}...")
            await query.edit_message_text(new_text, reply_markup=new_markup)
        except Exception as e:
            logger.error(f"Ошибка при редактировании сообщения: {e}")

    @network_retry
    async def safe_reply_text(self, update, text, reply_markup=None):
        """Безопасная отправка ответа."""
        await update.effective_message.reply_text(text[:MAX_MESSAGE_LENGTH], reply_markup=reply_markup)

    @network_retry
    async def start_view_results(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Начало просмотра результатов тестов с пагинацией."""
        logger.info(f"Entering STUDENT_VIEW_RESULTS for user {update.effective_user.id}")
        query = update.callback_query
        await query.answer()

        student_id = str(update.effective_user.id)
        test_results = self.db.load_student_results(student_id) or []
        logger.debug(f"Loaded {len(test_results)} results for user {student_id}")

        tests = []
        for result in test_results:
            if not StudentResultValidator.validate_test_result(result):
                logger.warning(f"Invalid result format: {result}")
                continue
            test = self.db.load_test_by_id(result.get("test_id"))
            result_copy = result.copy()
            result_copy["name"] = sanitize_input(test.get("name", "Без названия")) if test else "Без названия"
            tests.append(result_copy)
            if not test:
                logger.warning(f"Test with test_id={result.get('test_id')} not found for result {result.get('id')}")

        context.user_data["student_tests"] = tests

        state_manager = StateManager(context)
        state_manager.push(STUDENT_VIEW_RESULTS)

        if not tests:
            await self.safe_edit_message(
                query,
                StudentResultMessageManager.get_message("no_results"),
                StudentResultKeyboardManager.create_student_main_menu()
            )
            state_manager.push(STUDENT_MAIN)
            logger.info(f"Exiting STUDENT_VIEW_RESULTS, no results, moving to STUDENT_MAIN for user {student_id}")
            return STUDENT_MAIN

        page = int(query.data.split("_")[1]) if query.data.startswith("page_") else 0
        await self.safe_edit_message(
            query,
            StudentResultMessageManager.get_message("view_results"),
            StudentResultKeyboardManager.create_test_list(tests, page)
        )
        logger.info(f"Exiting STUDENT_VIEW_RESULTS, staying in STUDENT_VIEW_RESULTS for user {student_id}")
        return STUDENT_VIEW_RESULTS

    @network_retry
    async def view_test_details(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Просмотр деталей теста."""
        logger.info(f"Entering view_test_details for user {update.effective_user.id}")
        query = update.callback_query
        await query.answer()

        if not query.data.startswith("view_"):
            logger.error(f"Unexpected callback_data in view_test_details: {query.data}")
            await self.safe_edit_message(
                query,
                StudentResultMessageManager.get_message("error"),
                StudentResultKeyboardManager.create_student_main_menu()
            )
            return STUDENT_MAIN

        try:
            index = int(query.data.split("_")[1])
        except (IndexError, ValueError):
            logger.error(f"Invalid callback_data format: {query.data}")
            await self.safe_edit_message(
                query,
                StudentResultMessageManager.get_message("result_not_found"),
                StudentResultKeyboardManager.create_back_button()
            )
            return STUDENT_VIEW_RESULTS

        student_id = str(update.effective_user.id)
        completed_tests = context.user_data.get("student_tests", [])
        if index < 0 or index >= len(completed_tests):
            await self.safe_edit_message(
                query,
                StudentResultMessageManager.get_message("result_not_found"),
                StudentResultKeyboardManager.create_back_button()
            )
            logger.warning(f"Invalid test index {index} for user {student_id}")
            return STUDENT_VIEW_RESULTS
        test_result = completed_tests[index]

        test = self.db.load_test_by_id(test_result["test_id"])
        if not test or not StudentResultValidator.validate_test(test):
            await self.safe_edit_message(
                query,
                StudentResultMessageManager.get_message("test_not_found"),
                StudentResultKeyboardManager.create_back_button()
            )
            logger.warning(f"Test with test_id={test_result['test_id']} not found")
            return STUDENT_VIEW_RESULTS

        try:
            completed_at = datetime.fromisoformat(test_result["timestamp"])
        except (KeyError, ValueError):
            completed_at = datetime.now()
            logger.warning(f"Invalid timestamp in result {test_result['id']}")

        report = StudentResultMessageManager.get_message(
            "test_details",
            name=sanitize_input(test.get("name", "Без названия")),
            subject=sanitize_input(test.get("subject", "Не указан")),
            classes=", ".join(map(str, test.get("classes", ["Не указаны"]))),
            completed_at=completed_at.strftime(TIMESTAMP_FORMAT)
        )

        answers = test_result.get("answers", {})
        scores = test_result.get("scores", {})
        comments = test_result.get("comments", {})
        model_comments = test_result.get("Comment_LLM", {})
        text_parts = []
        current_part = ""

        for idx, question in enumerate(test.get("questions", [])):
            question_idx = str(idx)
            student_answer = sanitize_input(answers.get(question_idx, "Не отвечено"))
            score = scores.get(question_idx)
            score = f"{int(score)}/5" if score is not None else "Оценка отсутствует"
            teacher_comment = sanitize_input(comments.get(question_idx, "")[:200])
            model_comment = sanitize_input(model_comments.get(question_idx, "")[:200]) if question.get("type") == "open" else ""

            logger.debug(f"Question {idx + 1}: score={score}, teacher_comment={teacher_comment}, model_comment={model_comment}")

            if question.get("type") == "test":
                options = "\n".join([f"{i+1}. {opt}" for i, opt in enumerate(question.get("options", []))])
                question_text = StudentResultMessageManager.get_message(
                    "question_test",
                    idx=idx + 1,
                    text=sanitize_input(question.get("text", "Нет текста")[:200]),
                    options=options,
                    student_answer=student_answer[:200],
                    score=score
                )
            else:
                question_text = StudentResultMessageManager.get_message(
                    "question_open",
                    idx=idx + 1,
                    text=sanitize_input(question.get("text", "Нет текста")[:200]),
                    student_answer=student_answer[:200],
                    score=score
                )

            if teacher_comment:
                question_text += StudentResultMessageManager.get_message(
                    "teacher_comment",
                    comment=teacher_comment
                )
            else:
                question_text += StudentResultMessageManager.get_message("no_teacher_comment")

            if model_comment and question.get("type") == "open":
                question_text += StudentResultMessageManager.get_message(
                    "model_comment",
                    comment=model_comment
                )

            appeals = test_result.get("appeals", [])
            for appeal in appeals:
                if appeal.get("question_idx") == idx:
                    try:
                        appeal_time = datetime.fromisoformat(appeal["timestamp"])
                        teacher_response = sanitize_input(appeal.get("teacher_comment", "")[:200])
                        logger.debug(f"Appeal for question {idx + 1}: student_comment={appeal.get('student_comment')}, teacher_response={teacher_response}")
                        question_text += StudentResultMessageManager.get_message(
                            "appeal",
                            time=appeal_time.strftime(TIMESTAMP_FORMAT),
                            student_comment=sanitize_input(appeal.get("student_comment", "")[:200]),
                            status=appeal.get("status", "Неизвестно"),
                            teacher_comment=teacher_response
                        )
                    except (KeyError, ValueError) as e:
                        logger.warning(f"Error in appeal {appeal.get('id')}: {e}")

            question_text += "\n"
            if len(current_part) + len(question_text) > TEXT_PART_LENGTH:
                text_parts.append(current_part)
                current_part = question_text
            else:
                current_part += question_text

        if current_part:
            text_parts.append(current_part)

        context.user_data["report_parts"] = text_parts
        part_idx = context.user_data.get("report_part_idx", 0)
        part_idx = min(part_idx, len(text_parts) - 1) if text_parts else 0
        current_text = text_parts[part_idx] if text_parts else StudentResultMessageManager.get_message("no_data")

        state_manager = StateManager(context)
        state_manager.push(STUDENT_VIEW_TEST_DETAILS)

        await self.safe_edit_message(
            query,
            current_text,
            StudentResultKeyboardManager.create_test_details_navigation(part_idx, len(text_parts))
        )
        logger.info(f"Exiting view_test_details for user {student_id}")
        return STUDENT_VIEW_TEST_DETAILS

    @network_retry
    async def navigate_report_parts(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Навигация по частям отчета."""
        logger.info(f"Entering navigate_report_parts for user {update.effective_user.id}")
        query = update.callback_query
        await query.answer()

        text_parts = context.user_data.get("report_parts", [])
        if not text_parts:
            await self.safe_edit_message(
                query,
                StudentResultMessageManager.get_message("no_data"),
                StudentResultKeyboardManager.create_back_button()
            )
            logger.warning(f"No report parts found for user {update.effective_user.id}")
            return STUDENT_VIEW_TEST_DETAILS

        current_part_idx = context.user_data.get("report_part_idx", 0)
        action = query.data

        if action == "prev_report_part":
            new_idx = max(0, current_part_idx - 1)
        elif action == "next_report_part":
            new_idx = min(len(text_parts) - 1, current_part_idx + 1)
        else:
            logger.error(f"Unknown navigation action: {action}")
            await self.safe_edit_message(
                query,
                StudentResultMessageManager.get_message("error"),
                StudentResultKeyboardManager.create_back_button()
            )
            return STUDENT_VIEW_TEST_DETAILS

        context.user_data["report_part_idx"] = new_idx
        current_text = text_parts[new_idx]

        await self.safe_edit_message(
            query,
            current_text,
            StudentResultKeyboardManager.create_test_details_navigation(new_idx, len(text_parts))
        )
        logger.info(f"Exiting navigate_report_parts for user {update.effective_user.id}")
        return STUDENT_VIEW_TEST_DETAILS

    @network_retry
    async def back_to_test_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Возврат к списку тестов."""
        logger.info(f"Entering back_to_test_list for user {update.effective_user.id}")
        context.user_data.pop("report_part_idx", None)
        context.user_data.pop("report_parts", None)
        logger.info(f"Exiting back_to_test_list for user {update.effective_user.id}")
        return await self.start_view_results(update, context)



    @network_retry
    async def back_to_student_main(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Возврат в главное меню студента."""
        logger.info(f"Entering back_to_student_main for user {update.effective_user.id}")
        query = update.callback_query
        await query.answer()

        # Добавляем определение student_id для логирования
        student_id = str(update.effective_user.id)

        state_manager = StateManager(context)
        state_manager.push(STUDENT_MAIN)

        await self.safe_edit_message(
            query,
            StudentResultMessageManager.get_message("student_main"),
            StudentResultKeyboardManager.create_student_main_menu()
        )
        context.user_data.pop("report_part_idx", None)
        context.user_data.pop("report_parts", None)
        logger.info(f"Exiting back_to_student_main, moving to STUDENT_MAIN for user {student_id}")
        return STUDENT_MAIN




    @network_retry
    async def cancel_view(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Отмена просмотра результатов."""
        logger.info(f"Entering cancel_view for user {update.effective_user.id}")
        query = update.callback_query
        await query.answer()

        state_manager = StateManager(context)
        state_manager.push(STUDENT_MAIN)

        await self.safe_edit_message(
            query,
            StudentResultMessageManager.get_message("student_main"),
            StudentResultKeyboardManager.create_student_main_menu()
        )
        context.user_data.pop("report_part_idx", None)
        context.user_data.pop("report_parts", None)
        logger.info(f"Exiting cancel_view, moving to STUDENT_MAIN for user {update.effective_user.id}")
        return STUDENT_MAIN

    def get_conversation_handler(self):
        """Получение обработчика диалога."""
        return ConversationHandler(
            entry_points=[CallbackQueryHandler(self.start_view_results, pattern=r"^view_results$")],
            states={
                STUDENT_VIEW_RESULTS: [
                    CallbackQueryHandler(self.view_test_details, pattern=r"^view_\d+$"),
                    CallbackQueryHandler(self.back_to_student_main, pattern=r"^back_to_main$"),
                    CallbackQueryHandler(self.start_view_results, pattern=r"^page_\d+$"),
                ],
                STUDENT_VIEW_TEST_DETAILS: [
                    CallbackQueryHandler(self.back_to_test_list, pattern=r"^back_to_list$"),
                    CallbackQueryHandler(self.back_to_test_list, pattern=r"^back$"),
                    CallbackQueryHandler(self.navigate_report_parts, pattern=r"^(prev_report_part|next_report_part)$"),
                ],
            },
            fallbacks=[
                CallbackQueryHandler(self.cancel_view, pattern=r"^cancel$"),
                CommandHandler("cancel", self.cancel_view),
            ],
            map_to_parent={
                STUDENT_MAIN: STUDENT_MAIN,
                CHOOSE_ROLE: CHOOSE_ROLE,
            },
            allow_reentry=True,
        )

def student_results_conv_handler(db):
    return StudentTestResultsViewer(db).get_conversation_handler()