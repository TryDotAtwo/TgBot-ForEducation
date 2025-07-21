import unittest
from unittest.mock import AsyncMock, MagicMock
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from logic.teacher_create import (
    TeacherTestCreator, StateManager, TeacherTestValidator,
    TEACHER_SELECT_SUBJECT, TEACHER_SELECT_CLASS, TEACHER_ENTER_NAME,
    TEACHER_QUESTION_TYPE, TEACHER_ENTER_QUESTION, TEACHER_ENTER_CORRECT_ANSWER,
    TEACHER_ADD_OPTIONS, TEACHER_ADD_COMMENT
)

class TestTeacherTestCreator(unittest.TestCase):
    def setUp(self):
        self.creator = TeacherTestCreator()
        self.update = MagicMock(spec=Update)
        self.context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
        self.context.user_data = {}
        self.update.callback_query = AsyncMock()
        self.update.message = AsyncMock()
        self.update.effective_user.id = 12345

    def test_state_manager_data_preservation(self):
        state_manager = StateManager(self.context)
        state_manager.set_data(TEACHER_ENTER_QUESTION, "question_text", "What is 2+2?")
        state_manager.set_data(TEACHER_ENTER_CORRECT_ANSWER, "correct_answer", "4")

        self.assertEqual(state_manager.get_data(TEACHER_ENTER_QUESTION, "question_text"), "What is 2+2?")
        self.assertEqual(state_manager.get_data(TEACHER_ENTER_CORRECT_ANSWER, "correct_answer"), "4")

        state_manager.clear_state_data(TEACHER_ENTER_QUESTION)
        self.assertIsNone(state_manager.get_data(TEACHER_ENTER_QUESTION, "question_text"))
        self.assertEqual(state_manager.get_data(TEACHER_ENTER_CORRECT_ANSWER, "correct_answer"), "4")

    def test_reset_state(self):
        self.context.user_data["current_test"] = {"subject": "Math"}
        self.context.user_data["state_data"] = {TEACHER_ENTER_QUESTION: {"question_text": "What is 2+2?"}}
        self.creator.reset_state(self.context)
        self.assertEqual(self.context.user_data["current_test"], {
            "id": None, "subject": None, "classes": [], "name": None,
            "questions": [], "global_comment": None, "teacher_id": None, "created_at": None
        })
        self.assertEqual(self.context.user_data["state_data"], {})

    def test_validate_options(self):
        self.assertTrue(TeacherTestValidator.validate_options(["A", "B"], "C"))
        self.assertFalse(TeacherTestValidator.validate_options(["A"], "A"))  # Less than 2 total
        self.assertFalse(TeacherTestValidator.validate_options(["A", "A"], "B"))  # Duplicates
        self.assertFalse(TeacherTestValidator.validate_options(["A", "B", "C", "D", "E", "F", "G"], "H"))  # More than 7

    async def test_navigation_preservation(self):
        self.creator.reset_state(self.context)
        state_manager = StateManager(self.context)
        state_manager.set_data(TEACHER_ENTER_QUESTION, "question_text", "What is 2+2?")
        state_manager.set_data(TEACHER_ENTER_CORRECT_ANSWER, "correct_answer", "4")
        state_manager.set_data(TEACHER_ADD_OPTIONS, "options", ["2", "3", "5"])
        self.context.user_data["current_test"]["questions"].append({
            "type": "test", "text": None, "correct_answer": None, "options": [], "check_comment": None
        })

        # Нажатие "Назад" из TEACHER_ADD_OPTIONS в TEACHER_ENTER_CORRECT_ANSWER
        self.update.callback_query.data = "back"
        state_manager.push(TEACHER_ADD_OPTIONS)
        await self.creator.back_to_correct_answer_input(self.update, self.context)
        self.assertEqual(state_manager.get_data(TEACHER_ENTER_CORRECT_ANSWER, "correct_answer"), "4")

        # Нажатие "Вперед" обратно в TEACHER_ADD_OPTIONS
        self.update.callback_query.data = "next"
        await self.creator.move_forward_correct_answer(self.update, self.context)
        self.assertEqual(state_manager.get_data(TEACHER_ADD_OPTIONS, "options"), ["2", "3", "5"])

        # Нажатие "Назад" в TEACHER_ENTER_QUESTION и снова "Вперед"
        state_manager.push(TEACHER_ENTER_CORRECT_ANSWER)
        self.update.callback_query.data = "back"
        await self.creator.back_to_question_text_input(self.update, self.context)
        self.assertEqual(state_manager.get_data(TEACHER_ENTER_QUESTION, "question_text"), "What is 2+2?")

        self.update.callback_query.data = "next"
        await self.creator.move_forward_question(self.update, self.context)
        self.assertEqual(state_manager.get_data(TEACHER_ENTER_CORRECT_ANSWER, "correct_answer"), "4")

    async def test_no_duplicate_menu_on_global_comment(self):
        self.creator.reset_state(self.context)
        self.context.user_data["current_test"] = {
        "name": "Кек", "subject": "Математика", "classes": ["5"],
        "questions": [{}, {}, {}], "teacher_id": "12345"
        }
        state_manager = StateManager(self.context)
        state_manager.push(TEACHER_GLOBAL_COMMENT)
        self.update.message.text = "фыфывыфвфыв"

        # Обработка комментария
        await self.creator.process_global_comment(self.update, self.context)

        # Проверяем, что safe_reply_text вызван только один раз (в show_final_confirmation)
        self.creator.safe_reply_text.assert_called_once()
        self.assertEqual(state_manager.current(), TEACHER_FINAL_CONFIRM)
        self.assertEqual(self.context.user_data["current_test"]["global_comment"], "фыфывыфвфыв")

def run_pre_init_tests():
    """Запуск тестов перед инициализацией бота."""
    suite = unittest.TestLoader().loadTestsFromTestCase(TestTeacherTestCreator)
    result = unittest.TextTestRunner().run(suite)
    if not result.wasSuccessful():
        raise RuntimeError("Pre-initialization tests failed!")
    print("All pre-initialization tests passed successfully.")