import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from logic.student_do_test import StudentTestHandler, StateManager
from states import (
    STUDENT_MAIN,
    STUDENT_SELECT_SUBJECT,
    STUDENT_SELECT_CLASS,
    STUDENT_ENTER_TEST_NAME,
    STUDENT_SELECT_TEST,
    STUDENT_ENTER_INFO,
    STUDENT_TEST_INSTRUCTIONS,
    STUDENT_ANSWER_QUESTIONS,
    STUDENT_REVIEW_ANSWERS,
    STUDENT_APPEAL_SELECT,
    STUDENT_APPEAL_COMMENT
)
from datetime import datetime
import uuid

class TestStudentTestHandler(unittest.TestCase):
    def setUp(self):
        # Мокаем базу данных
        self.db = MagicMock()
        self.handler = StudentTestHandler(self.db)
        
        # Мокаем объекты Telegram
        self.context = MagicMock()
        self.context.user_data = {}
        self.update = MagicMock()
        self.update.callback_query = MagicMock()
        self.update.callback_query.message = MagicMock()
        self.update.callback_query.data = None
        self.update.effective_message = MagicMock()
        self.update.message = None
        
        # Мокаем методы отправки сообщений
        self.handler.safe_edit_message = AsyncMock()
        self.handler.safe_reply_text = AsyncMock(return_value=MagicMock(message_id=123))
        
        # Инициализируем StateManager
        self.state_manager = StateManager(self.context)

    async def test_start_test_selection(self):
        # Подготовка
        self.update.callback_query.data = "start_test"
        self.context.user_data["user_id"] = None
        
        # Выполнение
        result = await self.handler.start_test_selection(self.update, self.context)
        
        # Проверки
        self.assertEqual(result, STUDENT_SELECT_SUBJECT)
        self.assertEqual(self.context.user_data["user_id"], str(self.update.effective_user.id))
        self.assertEqual(self.state_manager.current(), STUDENT_SELECT_SUBJECT)
        self.handler.safe_edit_message.assert_called_with(
            self.update.callback_query,
            "📚 Выберите предмет:",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("Математика", callback_data="subj_Математика")],
                [InlineKeyboardButton("Физика", callback_data="subj_Физика")],
                [InlineKeyboardButton("История", callback_data="subj_История")],
                [InlineKeyboardButton("Информатика", callback_data="subj_Информатика")],
                [InlineKeyboardButton("◀ Назад", callback_data="back")]
            ])
        )

    async def test_process_subject(self):
        # Подготовка
        self.update.callback_query.data = "subj_Математика"
        
        # Выполнение
        result = await self.handler.process_subject(self.update, self.context)
        
        # Проверки
        self.assertEqual(result, STUDENT_SELECT_CLASS)
        self.assertEqual(self.context.user_data["current_test"]["subject"], "Математика")
        self.assertEqual(self.state_manager.current(), STUDENT_SELECT_CLASS)
        self.handler.safe_edit_message.assert_called_with(
            self.update.callback_query,
            "🏫 Выберите ваш класс:",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("5", callback_data="cls_5"),
                 InlineKeyboardButton("6", callback_data="cls_6"),
                 InlineKeyboardButton("7", callback_data="cls_7"),
                 InlineKeyboardButton("8", callback_data="cls_8"),
                 InlineKeyboardButton("9", callback_data="cls_9"),
                 InlineKeyboardButton("10", callback_data="cls_10"),
                 InlineKeyboardButton("11", callback_data="cls_11")],
                [InlineKeyboardButton("◀ Назад", callback_data="back")]
            ])
        )

    async def test_process_student_info_valid(self):
        # Подготовка
        self.update.message = MagicMock()
        self.update.message.text = "Иванов Иван 10А"
        self.context.user_data["student_info"] = None
        
        # Выполнение
        result = await self.handler.process_student_info(self.update, self.context)
        
        # Проверки
        self.assertEqual(result, STUDENT_TEST_INSTRUCTIONS)
        self.assertEqual(self.context.user_data["student_info"], "Иванов Иван 10А")
        self.assertEqual(self.context.user_data["instructions_msg_id"], 123)
        self.assertEqual(self.state_manager.current(), STUDENT_TEST_INSTRUCTIONS)
        self.handler.safe_reply_text.assert_called_with(
            self.update.message,
            "📋 Инструкции:\n1. Тест ограничен по времени\n2. Используйте кнопки навигации",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("◀ Назад", callback_data="back_instructions")],
                [InlineKeyboardButton("Начать тест ▶", callback_data="start")]
            ])
        )

    async def test_process_student_info_empty(self):
        # Подготовка
        self.update.message = MagicMock()
        self.update.message.text = ""
        
        # Выполнение
        result = await self.handler.process_student_info(self.update, self.context)
        
        # Проверки
        self.assertEqual(result, STUDENT_ENTER_INFO)
        self.handler.safe_reply_text.assert_called_with(
            self.update.message,
            "❌ ФИО и класс не могут быть пустыми. Попробуйте ещё раз.",
            InlineKeyboardMarkup([[InlineKeyboardButton("◀ Назад", callback_data="back")]])
        )

    async def test_cancel_test(self):
        # Подготовка
        self.context.user_data["current_test_id"] = "test_001"
        self.context.user_data["user_answers"] = {"0": "Ответ 1"}
        
        # Выполнение
        result = await self.handler.cancel_test(self.update, self.context)
        
        # Проверки
        self.assertEqual(result, STUDENT_MAIN)
        self.assertEqual(self.context.user_data, {})  # Проверяем очистку состояния
        self.assertEqual(self.state_manager.current(), STUDENT_MAIN)
        self.handler.safe_edit_message.assert_called_with(
            self.update.callback_query,
            "🏠 Меню учащегося:",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📝 Начать проверочную работу", callback_data="start_test")],
                [InlineKeyboardButton("📊 Посмотреть работы", callback_data="view_results")],
                [InlineKeyboardButton("🔙 Назад", callback_data="back")]
            ])
        )

    async def test_finish_test(self):
        # Подготовка
        self.context.user_data["current_test_id"] = "test_001"
        self.context.user_data["user_answers"] = {0: "Ответ 1"}
        test_data = {
            "questions": [
                {"type": "open", "text": "Вопрос 1", "correct_answer": "Ответ 1"}
            ]
        }
        self.db.load_test_by_id.return_value = test_data
        
        # Выполнение
        result = await self.handler.finish_test(self.update, self.context)
        
        # Проверки
        self.assertEqual(result, STUDENT_APPEAL_SELECT)
        self.assertEqual(self.state_manager.current(), STUDENT_APPEAL_SELECT)
        self.assertIn("test_completed_at", self.context.user_data)
        self.handler.safe_edit_message.assert_called()
        expected_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Подать апелляцию", callback_data="start_appeal")],
            [InlineKeyboardButton("🏠 В главное меню", callback_data="cancel")]
        ])
        call_args = self.handler.safe_edit_message.call_args
        self.assertEqual(call_args[0][1].split("\n")[0], "📊 Результаты теста:")
        self.assertEqual(call_args[0][2], expected_keyboard)
        self.db.save_result.assert_called()

    async def test_process_appeal_comment_valid(self):
        # Подготовка
        self.update.message = MagicMock()
        self.update.message.text = "Комментарий к апелляции"
        self.context.user_data["user_id"] = "123"
        self.context.user_data["current_test_id"] = "test_001"
        self.context.user_data["appeal_question_idx"] = 0
        self.context.user_data["questions"] = [{"text": "Вопрос 1"}]
        
        # Выполнение
        result = await self.handler.process_appeal_comment(self.update, self.context)
        
        # Проверки
        self.assertEqual(result, STUDENT_APPEAL_SELECT)
        self.assertEqual(self.state_manager.current(), STUDENT_APPEAL_SELECT)
        self.db.save_appeal.assert_called_with(
            "123",
            "test_001",
            {
                "question_idx": 0,
                "student_comment": "Комментарий к апелляции",
                "status": "pending",
                "timestamp": MagicMock()
            }
        )
        self.handler.safe_reply_text.assert_called()
        call_args = self.handler.safe_reply_text.call_args
        self.assertTrue(call_args[0][1].startswith("✅ Апелляция по вопросу 1 отправлена"))

    async def test_process_appeal_comment_empty(self):
        # Подготовка
        self.update.message = MagicMock()
        self.update.message.text = ""
        
        # Выполнение
        result = await self.handler.process_appeal_comment(self.update, self.context)
        
        # Проверки
        self.assertEqual(result, STUDENT_APPEAL_COMMENT)
        self.handler.safe_reply_text.assert_called_with(
            self.update.message,
            "❌ Комментарий не может быть пустым. Попробуйте ещё раз.",
            InlineKeyboardMarkup([[InlineKeyboardButton("◀ Назад", callback_data="back")]])
        )

    async def test_process_choice_valid(self):
        # Подготовка
        self.update.callback_query.data = "ans_0"
        self.context.user_data["current_question_idx"] = 0
        self.context.user_data["questions"] = [
            {"type": "test", "options": ["Вариант 1", "Вариант 2"], "correct_answer": "Вариант 1"}
        ]
        
        # Выполнение
        result = await self.handler.process_choice(self.update, self.context)
        
        # Проверки
        self.assertEqual(self.context.user_data["user_answers"][0], "Вариант 1")
        self.assertEqual(self.state_manager.get_data(STUDENT_ANSWER_QUESTIONS, "answer_0"), "Вариант 1")
        self.assertEqual(result, STUDENT_REVIEW_ANSWERS)  # Один вопрос, переходим к обзору

    async def test_process_choice_invalid(self):
        # Подготовка
        self.update.callback_query.data = "ans_5"
        self.context.user_data["current_question_idx"] = 0
        self.context.user_data["questions"] = [
            {"type": "test", "options": ["Вариант 1", "Вариант 2"], "correct_answer": "Вариант 1"}
        ]
        
        # Выполнение
        result = await self.handler.process_choice(self.update, self.context)
        
        # Проверки
        self.assertEqual(result, STUDENT_ANSWER_QUESTIONS)
        self.handler.safe_edit_message.assert_called_with(
            self.update.callback_query,
            "❌ Неверный вариант ответа!"
        )

    async def test_save_result_with_scores_and_comments(self):
        context = AsyncMock()
        context.user_data = {
            "user_id": "123",
            "current_test_id": "test1",
            "student_info": "Кекич 2",
            "user_answers": {0: "Первый", 1: "x = 2"},
            "questions": [
                {"type": "test", "text": "Выберите", "options": ["Первый", "Второй"], "correct_answer": "Первый"},
                {"type": "open", "text": "Решите", "correct_answer": "x = 2"}
            ]
        }
        self.db.load_test_by_id.return_value = {
            "questions": context.user_data["questions"]
        }
        self.db.save_result = MagicMock()
        await self.handler._save_result(context)
        self.db.save_result.assert_called_once()
        result_data = self.db.save_result.call_args[0][1]
        self.assertEqual(result_data["scores"], {"0": 10, "1": 10})
        self.assertEqual(result_data["Comment_LLM"], {"1": "Ответ близок к правильному, отличная работа!"})

def run_all_tests():
    """
    Запускает все юнит-тесты для модуля student_do_test.
    Возвращает True, если все тесты пройдены успешно, иначе False.
    """
    suite = unittest.TestLoader().loadTestsFromTestCase(TestStudentTestHandler)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()

if __name__ == "__main__":
    import asyncio
    loop = asyncio.get_event_loop()
    loop.run_until_complete(asyncio.gather(*[t.__coroutine__() for t in TestStudentTestHandler.__dict__.values() if hasattr(t, '__coroutine__')]))
    run_all_tests()