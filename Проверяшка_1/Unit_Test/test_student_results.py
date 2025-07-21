import unittest
from unittest.mock import AsyncMock, MagicMock
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from logic.student_show_result import StudentTestResultsViewer, StateManager, STUDENT_VIEW_RESULTS, STUDENT_MAIN, STUDENT_VIEW_TEST_DETAILS

class TestStudentTestResultsViewer(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.viewer = StudentTestResultsViewer(self.db)
        self.context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
        self.context.user_data = {}
        self.update = MagicMock(spec=Update)
        self.update.callback_query = MagicMock()
        self.update.callback_query.message = MagicMock()
        self.update.callback_query.message.text = ""
        self.update.callback_query.message.reply_markup = None
        self.update.effective_user.id = 12345
        self.state_manager = StateManager(self.context)
        self.viewer.safe_edit_message = AsyncMock()

    async def test_safe_edit_message_no_changes(self):
        query = MagicMock()
        query.message.text = "Тест"
        query.message.reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="back")]])
        await self.viewer.safe_edit_message(
            query,
            "Тест",
            InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="back")]])
        )
        self.viewer.safe_edit_message.assert_not_called()

    async def test_start_view_results_no_results(self):
        self.db.load_student_results.return_value = []
        result = await self.viewer.start_view_results(self.update, self.context)
        self.assertEqual(result, STUDENT_MAIN)
        self.viewer.safe_edit_message.assert_called_with(
            self.update.callback_query,
            "📭 Нет доступных работ. Вернитесь в меню:",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📝 Начать проверочную работу", callback_data="start_test")],
                [InlineKeyboardButton("📊 Посмотреть работы", callback_data="view_results")],
                [InlineKeyboardButton("🔙 Назад", callback_data="back")]
            ])
        )

    async def test_start_view_results_with_results(self):
        self.db.load_student_results.return_value = [{"id": "1", "test_id": "test1"}]
        self.db.load_test_by_id.return_value = {"name": "Тест 1"}
        self.update.callback_query.data = "view_results"
        result = await self.viewer.start_view_results(self.update, self.context)
        self.assertEqual(result, STUDENT_VIEW_RESULTS)
        self.viewer.safe_edit_message.assert_called_with(
            self.update.callback_query,
            "📊 Ваши работы:",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("Тест 1", callback_data="view_0")],
                [InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]
            ])
        )

    async def test_start_view_results_pagination(self):
        self.db.load_student_results.return_value = [
            {"id": str(i), "test_id": f"test{i}"} for i in range(7)
        ]
        self.db.load_test_by_id.side_effect = lambda x: {"name": f"Тест {x[-1]}"}
        self.update.callback_query.data = "page_1"
        result = await self.viewer.start_view_results(self.update, self.context)
        self.assertEqual(result, STUDENT_VIEW_RESULTS)
        call_args = self.viewer.safe_edit_message.call_args
        self.assertEqual(call_args[0][1], "📊 Ваши работы:")
        keyboard = call_args[0][2].inline_keyboard
        self.assertEqual(keyboard[0][0].text, "Тест 5")
        self.assertEqual(keyboard[1][0].text, "Тест 6")
        self.assertIn("⬅️ Пред. страница", [btn.text for row in keyboard for btn in row])
        self.assertNotIn("След. страница ➡️", [btn.text for row in keyboard for btn in row])

    async def test_view_test_details_open_question_with_comments(self):
        self.context.user_data["student_tests"] = [{
            "id": "1",
            "test_id": "test1",
            "timestamp": "2023-01-01T00:00:00",
            "answers": {"0": "x = 3"},
            "scores": {"0": 4.0},
            "comments": {"0": "Ответ правильный, но не указан процесс решения."},
            "Comment_LLM": {"0": "Хорошее объяснение, но можно подробнее."}
        }]
        self.db.load_test_by_id.return_value = {
            "name": "Тест 1",
            "subject": "Математика",
            "classes": [10],
            "questions": [{
                "text": "Решите уравнение x + 2 = 5",
                "type": "open"
            }],
            "global_comment": "Общий комментарий учителя"
        }
        self.update.callback_query.data = "view_0"
        result = await self.viewer.view_test_details(self.update, self.context)
        self.assertEqual(result, STUDENT_VIEW_TEST_DETAILS)
        call_args = self.viewer.safe_edit_message.call_args
        expected_text = (
            "📋 Результаты теста: Тест 1\n"
            "Предмет: Математика\n"
            "Классы: 10\n"
            "Дата завершения: 2023-01-01 00:00\n\n"
            "❓ Вопрос 1: Решите уравнение x + 2 = 5\n"
            "Ваш ответ: x = 3\n"
            "Оценка: 4/5\n"
            "Комментарий учителя: Ответ правильный, но не указан процесс решения.\n"
            "Комментарий модели: Хорошее объяснение, но можно подробнее.\n"
        )
        self.assertEqual(call_args[0][1], expected_text)
        self.assertNotIn("Глобальный комментарий", call_args[0][1])

    async def test_view_test_details_test_question_no_model_comment(self):
        self.context.user_data["student_tests"] = [{
            "id": "1",
            "test_id": "test1",
            "timestamp": "2023-01-01T00:00:00",
            "answers": {"0": "Азот"},
            "scores": {"0": 5.0},
            "comments": {},
            "Comment_LLM": {"0": "Этот комментарий не должен отображаться"}
        }]
        self.db.load_test_by_id.return_value = {
            "name": "Тест 1",
            "subject": "Химия",
            "classes": [10],
            "questions": [{
                "text": "Какой газ составляет большую часть атмосферы?",
                "type": "test",
                "options": ["Кислород", "Азот", "Углекислый газ"]
            }],
            "global_comment": "Общий комментарий учителя"
        }
        self.update.callback_query.data = "view_0"
        result = await self.viewer.view_test_details(self.update, self.context)
        self.assertEqual(result, STUDENT_VIEW_TEST_DETAILS)
        call_args = self.viewer.safe_edit_message.call_args
        expected_text = (
            "📋 Результаты теста: Тест 1\n"
            "Предмет: Химия\n"
            "Классы: 10\n"
            "Дата завершения: 2023-01-01 00:00\n\n"
            "❓ Вопрос 1: Какой газ составляет большую часть атмосферы?\n"
            "Варианты ответа:\n1. Кислород\n2. Азот\n3. Углекислый газ\n"
            "Ваш ответ: Азот\n"
            "Оценка: 5/5\n"
            "Учитель не оставил комментарий\n"
        )
        self.assertEqual(call_args[0][1], expected_text)
        self.assertNotIn("Комментарий модели", call_args[0][1])
        self.assertNotIn("Глобальный комментарий", call_args[0][1])

    async def test_view_test_details_open_question_no_comments(self):
        self.context.user_data["student_tests"] = [{
            "id": "1",
            "test_id": "test1",
            "timestamp": "2023-01-01T00:00:00",
            "answers": {"0": "x = 4"},
            "scores": {"0": 3.0},
            "comments": {},
            "Comment_LLM": {}
        }]
        self.db.load_test_by_id.return_value = {
            "name": "Тест 1",
            "subject": "Математика",
            "classes": [10],
            "questions": [{
                "text": "Решите уравнение x + 2 = 5",
                "type": "open"
            }],
            "global_comment": "Общий комментарий учителя"
        }
        self.update.callback_query.data = "view_0"
        result = await self.viewer.view_test_details(self.update, self.context)
        self.assertEqual(result, STUDENT_VIEW_TEST_DETAILS)
        call_args = self.viewer.safe_edit_message.call_args
        expected_text = (
            "📋 Результаты теста: Тест 1\n"
            "Предмет: Математика\n"
            "Классы: 10\n"
            "Дата завершения: 2023-01-01 00:00\n\n"
            "❓ Вопрос 1: Решите уравнение x + 2 = 5\n"
            "Ваш ответ: x = 4\n"
            "Оценка: 3/5\n"
            "Учитель не оставил комментарий\n"
        )
        self.assertEqual(call_args[0][1], expected_text)
        self.assertNotIn("Комментарий модели", call_args[0][1])
        self.assertNotIn("Глобальный комментарий", call_args[0][1])

    async def test_view_test_details_no_score(self):
        self.context.user_data["student_tests"] = [{
            "id": "1",
            "test_id": "test1",
            "timestamp": "2023-01-01T00:00:00",
            "answers": {"0": "x = 4"},
            "scores": {},
            "comments": {"0": "Некорректный ответ"},
            "Comment_LLM": {"0": "Ответ неверный, правильное значение x = 3."}
        }]
        self.db.load_test_by_id.return_value = {
            "name": "Тест 1",
            "subject": "Математика",
            "classes": [10],
            "questions": [{
                "text": "Решите уравнение x + 2 = 5",
                "type": "open"
            }],
            "global_comment": "Общий комментарий учителя"
        }
        self.update.callback_query.data = "view_0"
        result = await self.viewer.view_test_details(self.update, self.context)
        self.assertEqual(result, STUDENT_VIEW_TEST_DETAILS)
        call_args = self.viewer.safe_edit_message.call_args
        expected_text = (
            "📋 Результаты теста: Тест 1\n"
            "Предмет: Математика\n"
            "Классы: 10\n"
            "Дата завершения: 2023-01-01 00:00\n\n"
            "❓ Вопрос 1: Решите уравнение x + 2 = 5\n"
            "Ваш ответ: x = 4\n"
            "Оценка: Оценка отсутствует\n"
            "Комментарий учителя: Некорректный ответ\n"
            "Комментарий модели: Ответ неверный, правильное значение x = 3.\n"
        )
        self.assertEqual(call_args[0][1], expected_text)
        self.assertNotIn("Глобальный комментарий", call_args[0][1])

    async def test_view_test_details_with_appeal(self):
        self.context.user_data["student_tests"] = [{
            "id": "1",
            "test_id": "test1",
            "timestamp": "2023-01-01T00:00:00",
            "answers": {"0": "x = 3"},
            "scores": {"0": 4.0},
            "comments": {"0": "Хороший ответ"},
            "Comment_LLM": {"0": "Отличное решение!"},
            "appeals": [{
                "question_idx": 0,
                "student_comment": "Прошу пересмотреть",
                "status": "responded",
                "timestamp": "2023-01-02T00:00:00",
                "id": "appeal1"
            }]
        }]
        self.db.load_test_by_id.return_value = {
            "name": "Тест 1",
            "subject": "Математика",
            "classes": [10],
            "questions": [{
                "text": "Решите уравнение x + 2 = 5",
                "type": "open"
            }],
            "global_comment": "Общий комментарий учителя"
        }
        self.update.callback_query.data = "view_0"
        result = await self.viewer.view_test_details(self.update, self.context)
        self.assertEqual(result, STUDENT_VIEW_TEST_DETAILS)
        call_args = self.viewer.safe_edit_message.call_args
        expected_text = (
            "📋 Результаты теста: Тест 1\n"
            "Предмет: Математика\n"
            "Классы: 10\n"
            "Дата завершения: 2023-01-01 00:00\n\n"
            "❓ Вопрос 1: Решите уравнение x + 2 = 5\n"
            "Ваш ответ: x = 3\n"
            "Оценка: 4/5\n"
            "Комментарий учителя: Хороший ответ\n"
            "Комментарий модели: Отличное решение!\n"
            "📢 Апелляция (отправлена 2023-01-02 00:00):\n"
            "Комментарий: Прошу пересмотреть\n"
            "Статус: responded\n"
        )
        self.assertEqual(call_args[0][1], expected_text)
        self.assertNotIn("Глобальный комментарий", call_args[0][1])

    async def test_view_test_details_invalid(self):
        self.context.user_data["student_tests"] = []
        self.update.callback_query.data = "view_0"
        result = await self.viewer.view_test_details(self.update, self.context)
        self.assertEqual(result, STUDENT_VIEW_RESULTS)
        self.viewer.safe_edit_message.assert_called_with(
            self.update.callback_query,
            "❌ Результаты теста не найдены.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back")]])
        )

    async def test_navigate_report_parts(self):
        self.context.user_data["report_parts"] = ["Часть 1", "Часть 2"]
        self.context.user_data["report_part_idx"] = 0
        self.update.callback_query.data = "next_report_part"
        result = await self.viewer.navigate_report_parts(self.update, self.context)
        self.assertEqual(result, STUDENT_VIEW_TEST_DETAILS)
        self.assertEqual(self.context.user_data["report_part_idx"], 1)
        self.viewer.safe_edit_message.assert_called_with(
            self.update.callback_query,
            "Часть 2",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Пред. часть", callback_data="prev_report_part")],
                [InlineKeyboardButton("📜 К списку тестов", callback_data="back_to_list")],
                [InlineKeyboardButton("🔙 Назад", callback_data="back")]
            ])
        )

    async def test_back_to_student_main(self):
        result = await self.viewer.back_to_student_main(self.update, self.context)
        self.assertEqual(result, STUDENT_MAIN)
        self.viewer.safe_edit_message.assert_called_with(
            self.update.callback_query,
            "🏠 Меню учащегося:",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📝 Начать проверочную работу", callback_data="start_test")],
                [InlineKeyboardButton("📊 Посмотреть работы", callback_data="view_results")],
                [InlineKeyboardButton("🔙 Назад", callback_data="back")]
            ])
        )
    async def test_view_test_details_with_appeal_teacher_comment(self):
        self.context.user_data["student_tests"] = [{
            "id": "1",
            "test_id": "test1",
            "timestamp": "2023-01-01T00:00:00",
            "answers": {"0": "x = 3"},
            "scores": {"0": 4.0},
            "comments": {"0": "Хороший ответ"},
            "Comment_LLM": {"0": "Отличное решение!"},
            "appeals": [{
                "question_idx": 0,
                "student_comment": "Кудах-тах",
                "status": "responded",
                "timestamp": "2023-01-02T00:00:00",
                "id": "appeal1",
                "teacher_comment": "Rtr"
            }]
        }]
        self.db.load_test_by_id.return_value = {
            "name": "Тест 1",
            "subject": "Математика",
            "classes": [10],
            "questions": [{
                "text": "Решите уравнение x + 2 = 5",
                "type": "open"
            }],
            "global_comment": "Общий комментарий учителя"
        }
        self.update.callback_query.data = "view_0"
        result = await self.viewer.view_test_details(self.update, self.context)
        self.assertEqual(result, STUDENT_VIEW_TEST_DETAILS)
        call_args = self.viewer.safe_edit_message.call_args
        expected_text = (
            "📋 Результаты теста: Тест 1\n"
            "Предмет: Математика\n"
            "Классы: 10\n"
            "Дата завершения: 2023-01-01 00:00\n\n"
            "❓ Вопрос 1: Решите уравнение x + 2 = 5\n"
            "Ваш ответ: x = 3\n"
            "Оценка: 4/5\n"
            "Комментарий учителя: Хороший ответ\n"
            "Комментарий модели: Отличное решение!\n"
            "📢 Апелляция (отправлена 2023-01-02 00:00):\n"
            "Комментарий: Кудах-тах\n"
            "Статус: responded\n"
            "Ответ учителя: Rtr\n"
        )
        self.assertEqual(call_args[0][1], expected_text)
        self.assertNotIn("Глобальный комментарий", call_args[0][1])


def run_all_tests():
    """Запускает все юнит-тесты для модуля student_results."""
    suite = unittest.TestLoader().loadTestsFromTestCase(TestStudentTestResultsViewer)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()

if __name__ == "__main__":
    unittest.main()