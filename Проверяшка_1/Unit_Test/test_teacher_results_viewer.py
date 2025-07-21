# test_teacher_results_viewer.py

import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from logic.teacher_show_result import TeacherResultsViewer, TeacherResultsValidator
from database import Database
from states import (
    TEACHER_CHECK_RESULTS,
    TEACHER_CHECK_TEST,
    TEACHER_CHECK_STUDENTS,
    TEACHER_VIEW_STUDENT_QUESTIONS,
    TEACHER_CHECK_QUESTIONS,
    TEACHER_CHECK_ANSWERS,
    TEACHER_EDIT_SCORE,
    TEACHER_ADD_COMMENT,
    TEACHER_CHECK_APPEALS,
    TEACHER_RESPOND_APPEAL,
)
from parameterized import parameterized

class TestTeacherResultsViewer(unittest.IsolatedAsyncioTestCase):
    async def setUp(self):
        """Инициализация моков и объекта TeacherResultsViewer перед каждым тестом."""
        self.db_mock = AsyncMock(spec=Database)
        self.viewer = TeacherResultsViewer(self.db_mock)
        self.update_mock = MagicMock(spec=Update)
        self.context_mock = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
        self.context_mock.user_data = {}
        self.update_mock.effective_user.id = 12345  # Учительский ID

    # Тесты для start_check_results
    async def test_start_check_results_no_tests(self):
        """Проверка случая, когда у учителя нет тестов."""
        self.db_mock.load_teacher_tests.return_value = []
        self.update_mock.callback_query = MagicMock()
        result = await self.viewer.start_check_results(self.update_mock, self.context_mock)
        
        self.update_mock.callback_query.answer.assert_called_once()
        self.update_mock.callback_query.edit_message_text.assert_called_once_with(
            "📜 Вы еще не создали ни одного теста.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back")]])
        )
        self.assertEqual(result, TEACHER_CHECK_RESULTS)

    async def test_start_check_results_with_tests(self):
        """Проверка отображения списка тестов."""
        test_data = [
            {"id": "1", "name": "Тест 1", "subject": "Математика", "created_at": "2023-01-01T00:00:00"},
            {"id": "2", "name": "Тест 2", "subject": "Физика", "created_at": "2023-01-02T00:00:00"}
        ]
        self.db_mock.load_teacher_tests.return_value = test_data
        self.db_mock.load_all_results.return_value = []
        self.update_mock.callback_query = MagicMock()
        result = await self.viewer.start_check_results(self.update_mock, self.context_mock)
        
        self.update_mock.callback_query.answer.assert_called_once()
        expected_text = (
            "📜 Ваши тесты:\n\n"
            "📝 Тест 1 (Математика)\nПрохождений: 0\nПоследнее: Никто не проходил\n\n"
            "📝 Тест 2 (Физика)\nПрохождений: 0\nПоследнее: Никто не проходил\n\n"
        )
        expected_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Выбрать: Тест 1", callback_data="select_test_1")],
            [InlineKeyboardButton("Выбрать: Тест 2", callback_data="select_test_2")],
            [InlineKeyboardButton("🔙 Назад", callback_data="back")]
        ])
        self.update_mock.callback_query.edit_message_text.assert_called_once_with(
            expected_text,
            reply_markup=expected_keyboard
        )
        self.assertEqual(result, TEACHER_CHECK_RESULTS)

    # Тесты для select_test
    async def test_select_test_success(self):
        """Проверка выбора теста."""
        test_id = "1"
        test_data = {
            "id": "1", "name": "Тест 1", "subject": "Математика", "classes": ["10А"],
            "created_at": "2023-01-01T00:00:00", "questions": []
        }
        self.db_mock.load_test_by_id.return_value = test_data
        self.db_mock.load_all_results.return_value = []
        self.update_mock.callback_query = MagicMock()
        self.update_mock.callback_query.data = f"select_test_{test_id}"
        result = await self.viewer.select_test(self.update_mock, self.context_mock)
        
        self.update_mock.callback_query.answer.assert_called_once()
        expected_text = (
            "📝 Тест: Тест 1\nПредмет: Математика\nКлассы: 10А\nСоздан: 2023-01-01 00:00\n"
            "Вопросов: 0\nПрохождений: 0\nПоследнее прохождение: Никто не проходил\n\nВыберите действие:"
        )
        expected_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Статистика по ученикам", callback_data="stats_students_1")],
            [InlineKeyboardButton("Статистика по заданиям", callback_data="stats_questions_1")],
            [InlineKeyboardButton("Апелляции", callback_data="view_appeals_1")],
            [InlineKeyboardButton("🔙 Назад", callback_data="back")]
        ])
        self.update_mock.callback_query.edit_message_text.assert_called_once_with(
            expected_text,
            reply_markup=expected_keyboard
        )
        self.assertEqual(result, TEACHER_CHECK_TEST)
        self.assertEqual(self.context_mock.user_data["current_test_id"], test_id)

    async def test_select_test_not_found(self):
        """Проверка случая, когда тест не найден."""
        self.db_mock.load_test_by_id.return_value = None
        self.update_mock.callback_query = MagicMock()
        self.update_mock.callback_query.data = "select_test_invalid"
        result = await self.viewer.select_test(self.update_mock, self.context_mock)
        
        self.update_mock.callback_query.answer.assert_called_once()
        self.update_mock.callback_query.edit_message_text.assert_called_once_with(
            "Ошибка: тест не найден.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back")]])
        )
        self.assertEqual(result, TEACHER_CHECK_RESULTS)

    # Тесты для save_score
    async def test_save_score_success(self):
        """Проверка успешного сохранения оценки."""
        result_id = "result1"
        q_idx = 0
        test_id = "test1"
        self.context_mock.user_data.update({
            "current_result_id": result_id,
            "current_question_idx": q_idx,
            "current_test_id": test_id,
            "return_state": TEACHER_CHECK_ANSWERS
        })
        self.update_mock.message = MagicMock()
        self.update_mock.message.text = "5.0"
        results_data = {
            "user1": {
                "tests": [
                    {"id": "result1", "test_id": "test1", "user_id": "student1", "answers": {"0": "answer"}, "scores": {}}
                ]
            }
        }
        self.db_mock._load_results_file.return_value = results_data
        self.db_mock._save_to_file = AsyncMock()
        self.db_mock.load_test_by_id.return_value = {
            "name": "Тест 1",
            "questions": [{"text": "Вопрос 1"}]
        }
        result = await self.viewer.save_score(self.update_mock, self.context_mock)
        
        self.assertEqual(results_data["user1"]["tests"][0]["scores"]["0"], 5.0)
        self.db_mock._save_to_file.assert_called_once_with(self.db_mock.results_file, results_data)
        self.update_mock.message.reply_text.assert_called_once_with("Оценка сохранена.")
        self.assertEqual(result, TEACHER_CHECK_ANSWERS)

    async def test_save_score_invalid_input(self):
        """Проверка обработки некорректной оценки."""
        self.context_mock.user_data.update({
            "current_result_id": "result1",
            "current_question_idx": 0,
            "current_test_id": "test1",
            "return_state": TEACHER_CHECK_ANSWERS
        })
        self.update_mock.message = MagicMock()
        self.update_mock.message.text = "invalid"
        result = await self.viewer.save_score(self.update_mock, self.context_mock)
        
        self.update_mock.message.reply_text.assert_called_once_with("Ошибка: оценка должна быть числом (например, 5.0).")
        self.assertEqual(result, TEACHER_EDIT_SCORE)

    # Тесты для save_comment
    async def test_save_comment_success(self):
        """Проверка успешного сохранения комментария."""
        result_id = "result1"
        q_idx = 0
        test_id = "test1"
        self.context_mock.user_data.update({
            "current_result_id": result_id,
            "current_question_idx": q_idx,
            "current_test_id": test_id,
            "return_state": TEACHER_CHECK_ANSWERS
        })
        self.update_mock.message = MagicMock()
        self.update_mock.message.text = "Хорошая работа"
        results_data = {
            "user1": {
                "tests": [
                    {"id": "result1", "test_id": "test1", "user_id": "student1", "answers": {"0": "answer"}, "comments": {}}
                ]
            }
        }
        self.db_mock._load_results_file.return_value = results_data
        self.db_mock._save_to_file = AsyncMock()
        self.db_mock.load_test_by_id.return_value = {
            "name": "Тест 1",
            "questions": [{"text": "Вопрос 1"}]
        }
        result = await self.viewer.save_comment(self.update_mock, self.context_mock)
        
        self.assertEqual(results_data["user1"]["tests"][0]["comments"]["0"], "Хорошая работа")
        self.db_mock._save_to_file.assert_called_once_with(self.db_mock.results_file, results_data)
        self.update_mock.message.reply_text.assert_called_once_with("Комментарий сохранён.")
        self.assertEqual(result, TEACHER_CHECK_ANSWERS)

    # Тесты для navigate_tests
    async def test_navigate_tests_prev(self):
        """Проверка навигации на предыдущую страницу тестов."""
        self.context_mock.user_data["tests_page"] = 1
        self.update_mock.callback_query = MagicMock()
        self.update_mock.callback_query.data = "tests_page_prev"
        with patch.object(self.viewer, "start_check_results", new=AsyncMock()) as mock_start:
            result = await self.viewer.navigate_tests(self.update_mock, self.context_mock)
            self.assertEqual(self.context_mock.user_data["tests_page"], 0)
            mock_start.assert_called_once_with(self.update_mock, self.context_mock)
            self.assertEqual(result, TEACHER_CHECK_RESULTS)

    # Тесты для обработки ошибок
    async def test_view_appeals_no_appeals(self):
        """Проверка случая, когда апелляций нет."""
        test_id = "test1"
        self.context_mock.user_data["current_test_id"] = test_id
        self.db_mock.load_test_by_id.return_value = {"name": "Тест 1"}
        self.db_mock.load_all_appeals.return_value = []
        self.update_mock.callback_query = MagicMock()
        result = await self.viewer.view_appeals(self.update_mock, self.context_mock)
        
        self.update_mock.callback_query.edit_message_text.assert_called_once_with(
            "📜 По тесту 'Тест 1' нет апелляций.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back")]])
        )
        self.assertEqual(result, TEACHER_CHECK_TEST)

class TestTeacherResultsValidator(unittest.TestCase):
    @parameterized.expand([
        ("5.0", 5.0),
        ("10", 10.0),
        ("0", 0.0),
        ("invalid", None),
        ("", None),
    ])
    def test_validate_score(self, input_str, expected):
        """Проверка валидации оценки."""
        result = TeacherResultsValidator.validate_score(input_str)
        self.assertEqual(result, expected)

    def test_validate_comment(self):
        """Проверка валидации комментария."""
        self.assertTrue(TeacherResultsValidator.validate_comment("Хорошо"))
        self.assertFalse(TeacherResultsValidator.validate_comment(""))
        self.assertFalse(TeacherResultsValidator.validate_comment("   "))

def run_all_tests():
    """Запуск всех тестов."""
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestTeacherResultsViewer)
    suite.addTests(loader.loadTestsFromTestCase(TestTeacherResultsValidator))
    runner = unittest.TextTestRunner()
    result = runner.run(suite)
    return result.wasSuccessful()

if __name__ == "__main__":
    run_all_tests()