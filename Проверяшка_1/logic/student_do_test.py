from typing import Any
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
    CommandHandler,
)
from telegram.error import BadRequest, NetworkError, TimedOut
from datetime import datetime
import logging
from functools import wraps
from tenacity import retry, stop_after_attempt, wait_fixed
from states import *
from utils import create_back_button
import uuid
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

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

    def pop(self) -> str | None:
        """Извлечение последнего состояния из стека."""
        return self.context.user_data["state_stack"].pop() if self.context.user_data["state_stack"] else None

    def current(self) -> str | None:
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

class StudentTestHandler:
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
                logger.warning(f"Network error: {e}")
                raise
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
                logger.info("Игнорируем ошибку 'Message is not modified'")
        return wrapper

    def reset_state(self, context: ContextTypes.DEFAULT_TYPE):
        context.user_data["current_test"] = {}
        context.user_data["user_answers"] = {}
        context.user_data["current_test_id"] = None
        context.user_data["current_question_idx"] = 0
        state_manager = StateManager(context)
        state_manager.clear_data()

    @network_retry
    async def safe_edit_message(self, query, text: str, reply_markup: InlineKeyboardMarkup | None = None):
        """Безопасное редактирование сообщения."""
        if query.message.text == text and query.message.reply_markup == reply_markup:
            logger.debug("Сообщение не требует изменений")
            return
        await query.edit_message_text(text[:4096], reply_markup=reply_markup)

    @network_retry
    async def safe_reply_text(self, message, text: str, reply_markup: InlineKeyboardMarkup | None = None):
        """Безопасная отправка ответа."""
        try:
            result = await message.reply_text(text[:4096], reply_markup=reply_markup)
            if result is None:
                logger.error(f"Failed to send message: reply_text returned None for text='{text[:50]}...'")
                raise ValueError("Telegram API returned None for reply_text")
            return result
        except Exception as e:
            logger.error(f"Error in safe_reply_text: {str(e)}", exc_info=True)
            raise

    def get_conversation_handler(self):
        return ConversationHandler(
            entry_points=[CallbackQueryHandler(self.start_test_selection, pattern=r"^start_test$")],
            states={
                STUDENT_SELECT_SUBJECT: [
                    CallbackQueryHandler(self.process_subject, pattern=r"^subj_"),
                    CallbackQueryHandler(self.back_to_role_selection, pattern=r"^(back|back_role)$")
                ],
                STUDENT_SELECT_CLASS: [
                    CallbackQueryHandler(self.process_class, pattern=r"^cls_"),
                    CallbackQueryHandler(self.back_to_subject_selection, pattern=r"^(back|back_subj)$")
                ],
                STUDENT_ENTER_TEST_NAME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.process_test_name),
                    CallbackQueryHandler(self.back_to_class_selection, pattern=r"^back_cls$"),
                    CallbackQueryHandler(self.confirm_test_name, pattern=r"^confirm_test_name$"),
                    CallbackQueryHandler(self.back_to_subject_selection, pattern=r"^back_subj$")
                ],
                STUDENT_SELECT_TEST: [
                    CallbackQueryHandler(self.select_test, pattern=r"^test_"),
                    CallbackQueryHandler(self.back_to_test_name_input, pattern=r"^(back|back_testname)$")
                ],
                STUDENT_ENTER_INFO: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.process_student_info),
                    CallbackQueryHandler(self.back_to_test_selection, pattern=r"^back_testname$"),
                    CallbackQueryHandler(self.confirm_student_info, pattern=r"^confirm_student_info$")
                ],
                STUDENT_TEST_INSTRUCTIONS: [
                    CallbackQueryHandler(self.start_test, pattern=r"^start$"),
                    CallbackQueryHandler(self.back_to_student_info_input, pattern=r"^(back_instructions)$")
                ],
                STUDENT_ANSWER_QUESTIONS: [
                    CallbackQueryHandler(self.navigate_questions, pattern=r"^(prev|next|review)$"),
                    CallbackQueryHandler(self.process_choice, pattern=r"^ans_\d+$"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.process_answer)
                ],
                STUDENT_REVIEW_ANSWERS: [
                    CallbackQueryHandler(self.edit_answer, pattern=r"^edit_"),
                    CallbackQueryHandler(self.finish_test, pattern=r"^finish"),
                    CallbackQueryHandler(self.back_to_questions, pattern=r"^(back|back_review)$"),
                    CallbackQueryHandler(self.start_appeal, pattern=r"^start_appeal$"),
                    CallbackQueryHandler(self.cancel_test, pattern=r"^(cancel)$")
                ],
                STUDENT_APPEAL_SELECT: [
                    CallbackQueryHandler(self.start_appeal, pattern=r"^start_appeal$"),
                    CallbackQueryHandler(self.select_appeal_question, pattern=r"^appeal_"),
                    CallbackQueryHandler(self.back_to_final_results, pattern=r"^(back|back_final)$"),
                    CallbackQueryHandler(self.cancel_test, pattern=r"^(cancel)$")
                ],
                STUDENT_APPEAL_COMMENT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.process_appeal_comment),
                    CallbackQueryHandler(self.back_to_appeal_selection, pattern=r"^(back|back_appeal)$"),
                    CallbackQueryHandler(self.confirm_appeal_comment, pattern=r"^confirm_appeal$")
                ]
            },
            fallbacks=[CommandHandler("cancel", self.cancel_test)],
            map_to_parent={ConversationHandler.END: STUDENT_MAIN},
            allow_reentry=True
        )

    @network_retry
    async def start_test_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.reset_state(context)
        context.user_data["user_id"] = str(update.effective_user.id)
        query = update.callback_query
        await query.answer()

        subjects = ["Математика", "Физика", "История", "Информатика"]
        keyboard = [[InlineKeyboardButton(subj, callback_data=f"subj_{subj}")] for subj in subjects]
        keyboard.append([create_back_button()])

        await self.safe_edit_message(
            query,
            "📚 Выберите предмет:",
            InlineKeyboardMarkup(keyboard)
        )
        state_manager = StateManager(context)
        state_manager.push(STUDENT_SELECT_SUBJECT)
        return STUDENT_SELECT_SUBJECT

    @network_retry
    async def process_subject(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)
        subject = query.data.split("_", 1)[1]
        state_manager.set_data(STUDENT_SELECT_SUBJECT, "subject", subject)
        context.user_data["current_test"]["subject"] = subject

        keyboard = [
            [InlineKeyboardButton(str(cls), callback_data=f"cls_{cls}") for cls in range(5, 12)]
        ]
        keyboard.append([create_back_button()])

        await self.safe_edit_message(
            query,
            "🏫 Выберите ваш класс:",
            InlineKeyboardMarkup(keyboard)
        )
        state_manager.push(STUDENT_SELECT_CLASS)
        return STUDENT_SELECT_CLASS

    @network_retry
    async def process_class(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not query:
            logger.error("CallbackQuery не найден!")
            await self.safe_reply_text(update.effective_message, "❌ Ошибка системы. Начните заново.")
            return await self.cancel_test(update, context)

        await query.answer()
        state_manager = StateManager(context)
        selected_class = query.data.split("_", 1)[1]
        state_manager.set_data(STUDENT_SELECT_CLASS, "class", selected_class)
        context.user_data["current_test"]["class"] = selected_class

        test_name = state_manager.get_data(STUDENT_ENTER_TEST_NAME, "test_name", "")
        keyboard = [
            [InlineKeyboardButton("◀ Назад", callback_data="back_cls")]
        ]
        if test_name:
            keyboard.insert(0, [InlineKeyboardButton("▶ Вперед", callback_data="confirm_test_name")])

        await self.safe_edit_message(
            query,
            f"✏️ Введите название теста для поиска:\n{'' if not test_name else f'Ранее введено: {test_name}'}",
            InlineKeyboardMarkup(keyboard)
        )
        state_manager.push(STUDENT_ENTER_TEST_NAME)
        return STUDENT_ENTER_TEST_NAME

    @network_retry
    async def process_test_name(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        state_manager = StateManager(context)
        if update.message and update.message.text:
            test_name = update.message.text.strip()
            if not test_name:
                await self.safe_reply_text(
                    update.message,
                    "❌ Название теста не может быть пустым. Попробуйте ещё раз.",
                    InlineKeyboardMarkup([[create_back_button()]])
                )
                return STUDENT_ENTER_TEST_NAME
            state_manager.set_data(STUDENT_ENTER_TEST_NAME, "test_name", test_name)
            context.user_data["test_name"] = test_name
            return await self._search_tests(update, context)
        await self.safe_reply_text(
            update.effective_message,
            "❌ Текст не получен, попробуйте ещё раз.",
            InlineKeyboardMarkup([[create_back_button()]])
        )
        return STUDENT_ENTER_TEST_NAME

    async def _search_tests(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        state_manager = StateManager(context)
        test_name = state_manager.get_data(STUDENT_ENTER_TEST_NAME, "test_name", "")
        selected_class = state_manager.get_data(STUDENT_SELECT_CLASS, "class", "")
        subject = state_manager.get_data(STUDENT_SELECT_SUBJECT, "subject", "")
        tests_data = self.db.load_all_tests()
        filtered_tests = []

        for teacher_data in tests_data.values():
            for t in teacher_data.get("tests", []):
                if (
                    t.get("name")
                    and test_name.lower() in t["name"].lower()
                    and t.get("subject") == subject
                    and str(selected_class) in map(str, t.get("classes", []))
                ):
                    filtered_tests.append(t)

        if not filtered_tests:
            logger.info(f"Тесты не найдены: subject={subject}, class={selected_class}, name={test_name}")
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("◀ Назад", callback_data="back_cls")]
            ])
            await self.safe_reply_text(
                update.effective_message,
                f"❌ Тесты не найдены по параметрам:\n• Предмет: {subject}\n• Класс: {selected_class}\n• Название: {test_name}\n\nПожалуйста, введите другое название:",
                keyboard
            )
            return STUDENT_ENTER_TEST_NAME

        keyboard = [
            [InlineKeyboardButton(t["name"], callback_data=f"test_{t['id']}")]
            for t in filtered_tests
        ]
        keyboard.append([InlineKeyboardButton("◀ Назад", callback_data="back_testname")])

        if update.message:
            await self.safe_reply_text(update.message, "🔍 Найденные тесты:", InlineKeyboardMarkup(keyboard))
        elif update.callback_query:
            await self.safe_edit_message(update.callback_query, "🔍 Найденные тесты:", InlineKeyboardMarkup(keyboard))

        state_manager.push(STUDENT_SELECT_TEST)
        return STUDENT_SELECT_TEST

    @network_retry
    async def confirm_test_name(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)

        test_name = state_manager.get_data(STUDENT_ENTER_TEST_NAME, "test_name", "")
        if not test_name:
            await self.safe_edit_message(
                query,
                "❌ Вы не ввели название теста. Пожалуйста, введите его перед продолжением.",
                InlineKeyboardMarkup([[create_back_button()]])
            )
            return STUDENT_ENTER_TEST_NAME

        await self.safe_edit_message(query, "🔍 Поиск тестов...")
        return await self._search_tests(update, context)

    @network_retry
    async def select_test(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            query = update.callback_query
            await query.answer()
            state_manager = StateManager(context)

            if "_" not in query.data or len(query.data.split("_")) < 2:
                logger.error(f"Некорректный формат callback_data: {query.data}")
                await self.safe_edit_message(query, "❌ Ошибка выбора теста. Попробуйте снова.")
                return await self.cancel_test(update, context)

            test_id = query.data.split("_", 1)[1]
            test = self.db.load_test_by_id(test_id)
            if not test:
                await self.safe_edit_message(query, "❌ Тест не найден!")
                return await self.cancel_test(update, context)

            context.user_data["current_test_id"] = test_id
            student_info = state_manager.get_data(STUDENT_ENTER_INFO, "student_info", "")

            keyboard = [[InlineKeyboardButton("◀ Назад", callback_data="back_testname")]]
            if student_info:
                keyboard.insert(0, [InlineKeyboardButton("▶ Подтвердить", callback_data="confirm_student_info")])

            await self.safe_edit_message(
                query,
                f"📝 Введите ваше ФИО и класс:\n{'' if not student_info else f'Ранее введено: {student_info}'}",
                InlineKeyboardMarkup(keyboard)
            )
            state_manager.push(STUDENT_ENTER_INFO)
            return STUDENT_ENTER_INFO

        except Exception as e:
            logger.error(f"Ошибка в select_test: {str(e)}", exc_info=True)
            await self.safe_reply_text(update.effective_message, "⚠ Произошла ошибка. Начните заново.")
            return await self.cancel_test(update, context)

    @network_retry
    async def process_student_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            state_manager = StateManager(context)
            student_info = update.message.text.strip()
            if not student_info:
                await self.safe_reply_text(
                    update.message,
                    "❌ ФИО и класс не могут быть пустыми. Попробуйте ещё раз.",
                    InlineKeyboardMarkup([[create_back_button()]])
                )
                return STUDENT_ENTER_INFO
            state_manager.set_data(STUDENT_ENTER_INFO, "student_info", student_info)
            context.user_data["student_info"] = student_info

            keyboard = [
                [InlineKeyboardButton("◀ Назад", callback_data="back_instructions")],
                [InlineKeyboardButton("Начать тест ▶", callback_data="start")]
            ]

            msg = await self.safe_reply_text(
                update.message,
                "📋 Инструкции:\n1. Тест ограничен по времени\n2. Используйте кнопки навигации",
                InlineKeyboardMarkup(keyboard)
            )

            if msg is None:
                logger.error("Failed to retrieve message_id: safe_reply_text returned None")
                await self.safe_reply_text(
                    update.message,
                    "⚠ Произошла ошибка при отправке инструкций. Начните заново."
                )
                return await self.cancel_test(update, context)

            context.user_data["instructions_msg_id"] = msg.message_id
            state_manager.push(STUDENT_TEST_INSTRUCTIONS)
            return STUDENT_TEST_INSTRUCTIONS

        except Exception as e:
            logger.error(f"Ошибка в process_student_info: {str(e)}", exc_info=True)
            await self.safe_reply_text(
                update.message,
                "⚠ Произошла ошибка. Начните заново."
            )
            return await self.cancel_test(update, context)

    @network_retry
    async def start_test(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)

        if not context.user_data.get("current_test_id"):
            await self.safe_edit_message(query, "❌ Ошибка: тест не выбран!")
            return await self.cancel_test(update, context)

        test = self.db.load_test_by_id(context.user_data["current_test_id"])
        if not test or "questions" not in test:
            await self.safe_edit_message(query, "❌ Тест поврежден или не найден!")
            return await self.cancel_test(update, context)

        context.user_data.update({
            "questions": test["questions"],
            "current_question_idx": 0,
            "user_answers": {},
            "test_started": True
        })

        instructions_msg_id = context.user_data.get("instructions_msg_id")
        if instructions_msg_id:
            try:
                await context.bot.delete_message(
                    chat_id=query.message.chat_id,
                    message_id=instructions_msg_id
                )
            except BadRequest as e:
                logger.warning(f"Не удалось удалить сообщение инструкций: {e}")
        else:
            logger.warning("instructions_msg_id не найден, пропускаем удаление сообщения")

        context.user_data.pop("instructions_msg_id", None)
        return await self.show_question(update, context)

    @network_retry
    async def show_question(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        state_manager = StateManager(context)
        idx = context.user_data.get("current_question_idx", 0)
        questions = context.user_data.get("questions", [])
        if idx >= len(questions):
            return await self.show_review(update, context)

        question = questions[idx]
        user_answer = context.user_data["user_answers"].get(idx)
        text = f"❓ Вопрос {idx+1}/{len(questions)}:\n{question['text']}"
        if user_answer:
            text += f"\n\nВаш ответ: {user_answer}"

        markup = self._generate_question_markup(context)

        if context.user_data.get("test_started"):
            await self.safe_reply_text(update.effective_message, text, markup)
            context.user_data["test_started"] = False
        else:
            if update.callback_query:
                await self.safe_edit_message(update.callback_query, text, markup)
            else:
                await self.safe_reply_text(update.effective_message, text, markup)

        state_manager.push(STUDENT_ANSWER_QUESTIONS)
        return STUDENT_ANSWER_QUESTIONS

    def _generate_question_markup(self, context):
        idx = context.user_data.get("current_question_idx", 0)
        question = context.user_data["questions"][idx]
        buttons = []
        if question["type"] == "test":
            for opt_idx, opt in enumerate(question["options"]):
                buttons.append([InlineKeyboardButton(opt, callback_data=f"ans_{opt_idx}")])

        nav_buttons = []
        if idx > 0:
            nav_buttons.append(InlineKeyboardButton("◀️ Назад", callback_data="prev"))
        if idx < len(context.user_data["questions"]) - 1:
            nav_buttons.append(InlineKeyboardButton("Вперед ▶️", callback_data="next"))
        nav_buttons.append(InlineKeyboardButton("📝 Завершить", callback_data="review"))
        buttons.append(nav_buttons)
        return InlineKeyboardMarkup(buttons)

    @network_retry
    async def process_answer(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        state_manager = StateManager(context)
        answer = update.message.text.strip()
        if not answer:
            await self.safe_reply_text(
                update.message,
                "❌ Ответ не может быть пустым. Попробуйте ещё раз.",
                InlineKeyboardMarkup([[create_back_button()]])
            )
            return STUDENT_ANSWER_QUESTIONS
        idx = context.user_data.get("current_question_idx", 0)
        context.user_data["user_answers"][idx] = answer
        state_manager.set_data(STUDENT_ANSWER_QUESTIONS, f"answer_{idx}", answer)

        if idx < len(context.user_data["questions"]) - 1:
            context.user_data["current_question_idx"] = idx + 1
            return await self.show_question(update, context)

        return await self.show_review(update, context)

    @network_retry
    async def process_choice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)
        parts = query.data.split("_")
        if len(parts) < 2:
            await self.safe_edit_message(query, "❌ Ошибка выбора ответа.")
            return STUDENT_ANSWER_QUESTIONS

        idx = context.user_data.get("current_question_idx", 0)
        question = context.user_data["questions"][idx]
        try:
            option_idx = int(parts[1])
            if option_idx >= len(question["options"]):
                await self.safe_edit_message(query, "❌ Неверный вариант ответа!")
                return STUDENT_ANSWER_QUESTIONS
            chosen_option = question["options"][option_idx]
        except ValueError:
            await self.safe_edit_message(query, "❌ Неверный формат ответа!")
            return STUDENT_ANSWER_QUESTIONS

        context.user_data["user_answers"][idx] = chosen_option
        state_manager.set_data(STUDENT_ANSWER_QUESTIONS, f"answer_{idx}", chosen_option)

        if idx < len(context.user_data["questions"]) - 1:
            context.user_data["current_question_idx"] = idx + 1
            return await self.show_question(update, context)
        else:
            return await self.show_review(update, context)

    @network_retry
    async def navigate_questions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)

        questions = context.user_data.get("questions", [])
        if not questions:
            await self.safe_edit_message(query, "❌ Нет вопросов для навигации!")
            return ConversationHandler.END

        current_idx = context.user_data.get("current_question_idx", 0)
        action = query.data

        if action == "prev" and current_idx > 0:
            current_idx -= 1
        elif action == "next" and current_idx < len(questions) - 1:
            current_idx += 1
        elif action == "review":
            return await self.show_review(update, context)

        context.user_data["current_question_idx"] = current_idx
        return await self.show_question(update, context)

    @network_retry
    async def show_review(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        state_manager = StateManager(context)
        if update.callback_query:
            query = update.callback_query
            await query.answer()
            message_target = query
        else:
            message_target = update.effective_message

        timestamp = datetime.now().strftime("%H:%M:%S")
        review_text = f"📝 Проверьте ваши ответы (время: {timestamp}):"
        keyboard = [
            [InlineKeyboardButton(f"Вопрос {i+1}", callback_data=f"edit_{i}_{uuid.uuid4()}")]
            for i in range(len(context.user_data["questions"]))
        ]
        keyboard.append([InlineKeyboardButton("✅ Завершить тест", callback_data=f"finish_{uuid.uuid4()}")])

        try:
            if update.callback_query:
                await self.safe_edit_message(query, review_text, InlineKeyboardMarkup(keyboard))
            else:
                await self.safe_reply_text(message_target, review_text, InlineKeyboardMarkup(keyboard))
        except BadRequest as e:
            if "Message is not modified" in str(e):
                logger.info("Игнорируем ошибку 'Message is not modified'")
            else:
                raise

        state_manager.push(STUDENT_REVIEW_ANSWERS)
        return STUDENT_REVIEW_ANSWERS

    @network_retry
    async def edit_answer(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)
        try:
            parts = query.data.split("_")
            question_idx = int(parts[1])
            context.user_data["current_question_idx"] = question_idx
            answer = context.user_data["user_answers"].get(question_idx, "Не отвечен")
            await self.safe_edit_message(query, f"✏️ Редактирование вопроса {question_idx+1}\nТекущий ответ: {answer}")
        except Exception as e:
            logger.error(f"Ошибка при разборе callback_data в edit_answer: {str(e)}", exc_info=True)
            return await self.cancel_test(update, context)
        return await self.show_question(update, context)

    @network_retry
    async def finish_test(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)

        test = self.db.load_test_by_id(context.user_data["current_test_id"])
        score_data = self._generate_score_report(test, context.user_data["user_answers"])
        score_report = score_data["report_text"]

        MAX_MESSAGE_LENGTH = 4000
        if len(score_report) > MAX_MESSAGE_LENGTH:
            score_report = score_report[:MAX_MESSAGE_LENGTH - 50] + "\n... (отчет урезан)"

        keyboard = [
            [InlineKeyboardButton("📢 Подать апелляцию", callback_data="start_appeal")],
            [InlineKeyboardButton("🏠 В главное меню", callback_data="cancel")]
        ]

        context.user_data["test_completed_at"] = datetime.now().isoformat()

        await self.safe_edit_message(
            query,
            f"{score_report}\n\n⚠ Вы можете подать апелляцию в течение 24 часов",
            InlineKeyboardMarkup(keyboard)
        )
        self._save_result(context)

        context.user_data.pop("instructions_msg_id", None)
        state_manager.push(STUDENT_APPEAL_SELECT)
        return STUDENT_APPEAL_SELECT


    def _generate_score_report(self, test, user_answers):
        report = ["📊 Результаты теста:"]
        total_score = 0
        max_score = 0
        scores = {}  # Хранит оценки для каждого вопроса
        Comment_LLM = {}  # Хранит комментарии модели для развёрнутых вопросов

        for idx, question in enumerate(test["questions"]):
            max_score += 10
            user_answer = user_answers.get(idx, "Не отвечен")
            if question["type"] == "test":
                if user_answer == question["correct_answer"]:
                    status = "✅ Верно (+10 баллов)"
                    question_score = 10
                    total_score += 10
                else:
                    status = f"❌ Неверно\nПравильный ответ: {question['correct_answer']}\nВаш ответ: {user_answer}"
                    question_score = 0
                comment = ""  # Для тестовых вопросов комментарий модели не нужен
            else:
                if user_answer == "Не отвечен":
                    status = "❌ Не отвечен"
                    question_score = 0
                    comment = ""
                else:
                    question_score, comment = self._check_open_answer(user_answer, question["correct_answer"])
                    status = f"📝 Оценка: {question_score}/10\nВаш ответ: {user_answer}\n{comment}"
                    total_score += question_score
                    Comment_LLM[str(idx)] = comment  # Сохраняем комментарий модели для развёрнутого вопроса
                # Удаляем префикс "Комментарий модели-проверяющего (Grok-3): " из комментария для хранения
                if comment:
                    comment = comment.replace("Комментарий модели-проверяющего (Grok-3): ", "")
                    Comment_LLM[str(idx)] = comment

            scores[str(idx)] = question_score  # Сохраняем оценку для вопроса
            report.append(f"**Вопрос {idx+1}:**\n{status}")

        percentage = (total_score / max_score) * 100 if max_score > 0 else 0
        report.append(f"\n💡 Итоговый балл: {total_score}/{max_score} ({percentage:.1f}%)")
        report.append("⚠ Это предварительная оценка. Итоговую оценку сообщит учитель.")

        return {
            "report_text": "\n".join(report),
            "scores": scores,
            "Comment_LLM": Comment_LLM
    }


    def _check_open_answer(self, user_answer, correct_answer):
        similarity = SequenceMatcher(None, user_answer.lower(), correct_answer.lower()).ratio()
        score = int(similarity * 10)
        score = max(0, min(10, score))

        # Генерация комментария на основе оценки
        if score >= 8:
            comment = "Ответ близок к правильному, отличная работа!"
        elif score >= 5:
            comment = "Ответ частично правильный, но требует уточнений."
        elif score >= 2:
            comment = "Ответ имеет некоторое сходство, но нуждается в доработке."
        else:
            comment = "Ответ не соответствует правильному, требуется более точное объяснение."

        return score, f"Комментарий модели-проверяющего (Grok-3): {comment}"

    def _save_result(self, context):
        user_id = context.user_data.get("user_id", "unknown")
        test = self.db.load_test_by_id(context.user_data["current_test_id"])
        score_data = self._generate_score_report(test, context.user_data["user_answers"])

        result_data = {
            "test_id": context.user_data["current_test_id"],
            "student_info": context.user_data["student_info"],
            "answers": context.user_data["user_answers"],
            "scores": score_data["scores"],
            "Comment_LLM": score_data["Comment_LLM"],
            "timestamp": datetime.now().isoformat(),
            "appeals": []
        }
        result_id = self.db.save_result(user_id, result_data)
        context.user_data["current_result_id"] = result_id  # Сохраняем result_id для апелляций

    @network_retry
    async def back_to_role_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)
        keyboard = [
            [InlineKeyboardButton("📝 Начать проверочную работу", callback_data="start_test")],
            [InlineKeyboardButton("📊 Посмотреть работы", callback_data="view_results")],
            [InlineKeyboardButton("🔙 Назад", callback_data="back")]
        ]
        await self.safe_edit_message(
            query,
            "🏠 Меню учащегося:",
            InlineKeyboardMarkup(keyboard)
        )
        state_manager.push(STUDENT_MAIN)
        return STUDENT_MAIN

    @network_retry
    async def back_to_subject_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        state_manager = StateManager(context)
        state_manager.pop()
        return await self.start_test_selection(update, context)

    @network_retry
    async def back_to_class_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)
        subject = state_manager.get_data(STUDENT_SELECT_SUBJECT, "subject")
        if not subject:
            await self.safe_edit_message(query, "❌ Ошибка: предмет не выбран. Начните заново.")
            return await self.cancel_test(update, context)
        keyboard = [
            [InlineKeyboardButton(str(cls), callback_data=f"cls_{cls}") for cls in range(5, 12)]
        ]
        keyboard.append([create_back_button()])
        await self.safe_edit_message(
            query,
            "🏫 Выберите ваш класс:",
            InlineKeyboardMarkup(keyboard)
        )
        state_manager.pop()
        state_manager.push(STUDENT_SELECT_CLASS)
        return STUDENT_SELECT_CLASS

    @network_retry
    async def back_to_test_name_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            state_manager = StateManager(context)
            test_name = state_manager.get_data(STUDENT_ENTER_TEST_NAME, "test_name", "")
            text = "✏️ Введите название теста для поиска:\n"
            if test_name:
                text += f"Ранее введено: {test_name}"
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("◀ Назад", callback_data="back_cls"),
                    InlineKeyboardButton("▶ Вперед", callback_data="confirm_test_name") if test_name else InlineKeyboardButton("◀ Назад", callback_data="back_cls")
                ]
            ])
            if update.callback_query:
                await self.safe_edit_message(update.callback_query, text, keyboard)
            else:
                await self.safe_reply_text(update.effective_message, text, keyboard)
            state_manager.pop()
            state_manager.push(STUDENT_ENTER_TEST_NAME)
            return STUDENT_ENTER_TEST_NAME
        except Exception as e:
            logger.error(f"Ошибка в back_to_test_name_input: {str(e)}", exc_info=True)
            return await self.cancel_test(update, context)

    @network_retry
    async def back_to_test_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)

        test_name = state_manager.get_data(STUDENT_ENTER_TEST_NAME, "test_name", "")
        selected_class = state_manager.get_data(STUDENT_SELECT_CLASS, "class", "")
        subject = state_manager.get_data(STUDENT_SELECT_SUBJECT, "subject", "")
        tests_data = self.db.load_all_tests()
        filtered_tests = []

        for teacher_data in tests_data.values():
            for t in teacher_data.get("tests", []):
                if (
                    t.get("name")
                    and test_name.lower() in t["name"].lower()
                    and t.get("subject") == subject
                    and str(selected_class) in map(str, t.get("classes", []))
                ):
                    filtered_tests.append(t)

        if not filtered_tests:
            await self.safe_edit_message(
                query,
                f"❌ Тесты не найдены по параметрам:\n• Предмет: {subject}\n• Класс: {selected_class}\n• Название: {test_name}\n\nПожалуйста, введите другое название:",
                InlineKeyboardMarkup([[create_back_button()]])
            )
            state_manager.push(STUDENT_ENTER_TEST_NAME)
            return STUDENT_ENTER_TEST_NAME

        keyboard = [
            [InlineKeyboardButton(t["name"], callback_data=f"test_{t['id']}")]
            for t in filtered_tests
        ]
        keyboard.append([InlineKeyboardButton("◀ Назад", callback_data="back_testname")])
        await self.safe_edit_message(
            query,
            "🔍 Найденные тесты:",
            InlineKeyboardMarkup(keyboard)
        )
        state_manager.pop()
        state_manager.push(STUDENT_SELECT_TEST)
        return STUDENT_SELECT_TEST

    @network_retry
    async def back_to_student_info_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)

        student_info = state_manager.get_data(STUDENT_ENTER_INFO, "student_info", "")
        text = "📝 Введите ваше ФИО и класс:\n"
        if student_info:
            text += f"Ранее введено: {student_info}"

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("◀ Назад", callback_data="back_testname"),
                InlineKeyboardButton("▶ Подтвердить", callback_data="confirm_student_info") if student_info else InlineKeyboardButton("◀ Назад", callback_data="back_testname")
            ]
        ])

        await self.safe_edit_message(query, text, keyboard)
        state_manager.pop()
        state_manager.push(STUDENT_ENTER_INFO)
        return STUDENT_ENTER_INFO

    @network_retry
    async def confirm_student_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)

        student_info = state_manager.get_data(STUDENT_ENTER_INFO, "student_info", "")
        if not student_info:
            await self.safe_edit_message(
                query,
                "❌ ФИО и класс не введены. Пожалуйста, введите их перед продолжением.",
                InlineKeyboardMarkup([[create_back_button()]])
            )
            return STUDENT_ENTER_INFO

        instructions = (
            "📜 Инструкция:\n"
            "1. Нажмите 'Начать' для старта теста\n"
            "2. Используйте кнопки навигации\n"
            "3. Для завершения нажмите 'Завершить'"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("◀ Назад", callback_data="back_instructions"),
                InlineKeyboardButton("Начать", callback_data="start")
            ]
        ])

        await self.safe_edit_message(query, instructions, keyboard)
        state_manager.push(STUDENT_TEST_INSTRUCTIONS)
        return STUDENT_TEST_INSTRUCTIONS

    @network_retry
    async def back_to_questions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        state_manager = StateManager(context)
        context.user_data["current_question_idx"] = 0
        state_manager.pop()
        return await self.show_question(update, context)

    @network_retry
    async def back_to_final_results(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)

        test = self.db.load_test_by_id(context.user_data["current_test_id"])
        score_data = self._generate_score_report(test, context.user_data["user_answers"])
        score_report = score_data["report_text"]

        keyboard = [
            [InlineKeyboardButton("📢 Подать апелляцию", callback_data="start_appeal")],
            [InlineKeyboardButton("🏠 В главное меню", callback_data="cancel")]
        ]

        await self.safe_edit_message(
            query,
            f"{score_report}\n\n⚠ Вы можете подать апелляцию в течение 24 часов",
            InlineKeyboardMarkup(keyboard)
        )
        state_manager.pop()
        state_manager.push(STUDENT_APPEAL_SELECT)
        return STUDENT_APPEAL_SELECT

    @network_retry
    async def back_to_appeal_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if query:
            await query.answer()
            state_manager = StateManager(context)
            keyboard = [
                [InlineKeyboardButton(f"Вопрос {i+1}", callback_data=f"appeal_{i}")]
                for i in range(len(context.user_data["questions"]))
            ]
            keyboard.append([create_back_button()])

            timestamp = datetime.now().strftime("%H:%M:%S")
            appeal_text = f"🔍 Выберите вопрос для апелляции (время: {timestamp}):"

            await self.safe_edit_message(query, appeal_text, InlineKeyboardMarkup(keyboard))
            state_manager.pop()
            state_manager.push(STUDENT_APPEAL_SELECT)
            return STUDENT_APPEAL_SELECT
        else:
            await self.safe_reply_text(update.effective_message, "❌ Ошибка возврата. Начните заново.")
            return await self.cancel_test(update, context)

    @network_retry
    async def cancel_test(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        state_manager = StateManager(context)
        self.reset_state(context)

        keyboard = [
            [InlineKeyboardButton("📝 Начать проверочную работу", callback_data="start_test")],
            [InlineKeyboardButton("📊 Посмотреть работы", callback_data="view_results")],
            [InlineKeyboardButton("🔙 Назад", callback_data="back")]
        ]
    
        message_text = "🏠 Меню учащегося:"
    
        if update.callback_query:
            await update.callback_query.answer()
            await self.safe_edit_message(
                update.callback_query,
                message_text,
                InlineKeyboardMarkup(keyboard)
            )
        else:
            await self.safe_reply_text(
                update.effective_message,
                message_text,
                InlineKeyboardMarkup(keyboard)
            )

        state_manager.push(STUDENT_MAIN)
        return STUDENT_MAIN

    @network_retry
    async def start_appeal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)

        completed_at = context.user_data.get("test_completed_at")
        if completed_at:
            completed_time = datetime.fromisoformat(completed_at)
            if (datetime.now() - completed_time).total_seconds() > 24 * 3600:
                await self.safe_edit_message(
                    query,
                    "❌ Срок подачи апелляции (24 часа) истёк.",
                    InlineKeyboardMarkup([[create_back_button()]])
                )
                return STUDENT_APPEAL_SELECT

        keyboard = [
            [InlineKeyboardButton(f"Вопрос {i+1}", callback_data=f"appeal_{i}")]
            for i in range(len(context.user_data["questions"]))
        ]
        keyboard.append([create_back_button()])

        timestamp = datetime.now().strftime("%H:%M:%S")
        appeal_text = f"🔍 Выберите вопрос для апелляции (время: {timestamp}):"

        await self.safe_edit_message(query, appeal_text, InlineKeyboardMarkup(keyboard))
        state_manager.push(STUDENT_APPEAL_SELECT)
        return STUDENT_APPEAL_SELECT

    @network_retry
    async def select_appeal_question(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)
        context.user_data["appeal_question_idx"] = int(query.data.split("_", 1)[1])
        appeal_comment = state_manager.get_data(STUDENT_APPEAL_COMMENT, f"comment_{context.user_data['appeal_question_idx']}", "")

        keyboard = [[InlineKeyboardButton("◀ Назад", callback_data="back_appeal")]]
        if appeal_comment:
            keyboard.insert(0, [InlineKeyboardButton("▶ Подтвердить", callback_data="confirm_appeal")])

        await self.safe_edit_message(
            query,
            f"📝 Напишите комментарий к апелляции (макс. 500 символов):\n{'' if not appeal_comment else f'Ранее введено: {appeal_comment}'}",
            InlineKeyboardMarkup(keyboard)
        )
        state_manager.push(STUDENT_APPEAL_COMMENT)
        return STUDENT_APPEAL_COMMENT



    @network_retry
    async def process_appeal_comment(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        state_manager = StateManager(context)
        comment = update.message.text[:500].strip()
        if not comment:
            await self.safe_reply_text(
                update.message,
                "❌ Комментарий не может быть пустым. Попробуйте ещё раз.",
                InlineKeyboardMarkup([[create_back_button()]])
            )
            return STUDENT_APPEAL_COMMENT

        user_id = context.user_data.get("user_id", "unknown")
        result_id = context.user_data["current_result_id"]  # Используем result_id
        question_idx = context.user_data["appeal_question_idx"]
        state_manager.set_data(STUDENT_APPEAL_COMMENT, f"comment_{question_idx}", comment)

        appeal_data = {
            "question_idx": question_idx,
            "student_comment": comment,
            "status": "pending",
            "timestamp": datetime.now().isoformat()
        }
        logger.debug(f"Saving appeal: user_id={user_id}, result_id={result_id}, appeal_data={appeal_data}")
        try:
            self.db.save_appeal(user_id, result_id, appeal_data)
            logger.info(f"Appeal saved successfully for user_id={user_id}, result_id={result_id}, question_idx={question_idx}")
        except Exception as e:
            logger.error(f"Failed to save appeal: {str(e)}", exc_info=True)
            await self.safe_reply_text(
                update.message,
                "❌ Ошибка при сохранении апелляции. Попробуйте снова.",
                InlineKeyboardMarkup([[create_back_button()]])
            )
            return STUDENT_APPEAL_COMMENT

        keyboard = [
            [InlineKeyboardButton(f"Вопрос {i+1}", callback_data=f"appeal_{i}")]
            for i in range(len(context.user_data["questions"]))
        ]
        keyboard.append([create_back_button()])

        timestamp = datetime.now().strftime("%H:%M:%S")
        appeal_text = f"✅ Апелляция по вопросу {question_idx+1} отправлена (время: {timestamp}):\n🔍 Выберите другой вопрос для апелляции:"

        await self.safe_reply_text(update.message, appeal_text, InlineKeyboardMarkup(keyboard))
        state_manager.push(STUDENT_APPEAL_SELECT)
        return STUDENT_APPEAL_SELECT

    @network_retry
    async def confirm_appeal_comment(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        state_manager = StateManager(context)

        question_idx = context.user_data.get("appeal_question_idx")
        comment = state_manager.get_data(STUDENT_APPEAL_COMMENT, f"comment_{question_idx}", "")

        if not comment:
            await self.safe_edit_message(
                query,
                "❌ Комментарий отсутствует. Пожалуйста, введите комментарий.",
                InlineKeyboardMarkup([[create_back_button()]])
            )
            return STUDENT_APPEAL_COMMENT

        user_id = context.user_data.get("user_id", "unknown")
        result_id = context.user_data["current_result_id"]  # Используем result_id
        appeal_data = {
            "question_idx": question_idx,
            "student_comment": comment,
            "status": "pending",
            "timestamp": datetime.now().isoformat()
        }
        logger.debug(f"Saving appeal: user_id={user_id}, result_id={result_id}, appeal_data={appeal_data}")
        try:
            self.db.save_appeal(user_id, result_id, appeal_data)
            logger.info(f"Appeal saved successfully for user_id={user_id}, result_id={result_id}, question_idx={question_idx}")
        except Exception as e:
            logger.error(f"Failed to save appeal: {str(e)}", exc_info=True)
            await self.safe_edit_message(
                query,
                "❌ Ошибка при сохранении апелляции. Попробуйте снова.",
                InlineKeyboardMarkup([[create_back_button()]])
            )
            return STUDENT_APPEAL_COMMENT

        keyboard = [
            [InlineKeyboardButton(f"Вопрос {i+1}", callback_data=f"appeal_{i}")]
            for i in range(len(context.user_data["questions"]))
        ]
        keyboard.append([create_back_button()])

        timestamp = datetime.now().strftime("%H:%M:%S")
        appeal_text = f"✅ Апелляция по вопросу {question_idx+1} подтверждена (время: {timestamp}):\n🔍 Выберите другой вопрос для апелляции:"

        await self.safe_edit_message(query, appeal_text, InlineKeyboardMarkup(keyboard))
        state_manager.push(STUDENT_APPEAL_SELECT)
        return STUDENT_APPEAL_SELECT
