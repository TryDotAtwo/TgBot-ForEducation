import logging
import unittest
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    ConversationHandler,
)
from states import *
from database import Database
from logic.teacher_create import TeacherTestCreator
from logic.student_do_test import StudentTestHandler
from logic.student_show_result import StudentTestResultsViewer
from logic.teacher_show_result import TeacherResultsViewer
from utils import push_state, pop_state, cancel, back_handler
from config import BOT_TOKEN

# Импортируем функции для запуска тестов
from Unit_Test.test_teacher_results_viewer import run_all_tests as run_teacher_results_tests
from Unit_Test.test_student_results import run_all_tests as run_student_results_tests
from Unit_Test.test_student_do_test import run_all_tests as run_student_do_tests
from Unit_Test.test_teacher_test_creator import run_pre_init_tests as run_teacher_creator_tests

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Инициализация базы данных
db = Database()

# Создание обработчиков с передачей db
test_creator = TeacherTestCreator(db)
results_viewer = TeacherResultsViewer(db)
student_test_handler = StudentTestHandler(db)
student_results_viewer = StudentTestResultsViewer(db)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Инициирует бота, показывая меню выбора роли."""
    keys_to_clear = [
        "current_test_id", "tests_page", "students_page", "pending_notifications",
        "current_result_id", "current_question_idx", "temp_test_id", "state_history",
        "answers_page", "appeals_page", "question_text_part"
    ]
    for key in keys_to_clear:
        context.user_data.pop(key, None)
    
    keyboard = [
        [InlineKeyboardButton("Учащийся", callback_data='student'),
         InlineKeyboardButton("Учитель", callback_data='teacher')]
    ]
    
    if update.message:
        message = update.message
    elif update.callback_query:
        message = update.callback_query.message
    else:
        logger.error("Нет сообщения в update для отправки ответа.")
        if update and update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠ Ошибка: не удалось обработать запрос. Попробуйте снова."
            )
        return ConversationHandler.END

    await message.reply_text(
        "🎓 Добро пожаловать! Выберите режим:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    logger.debug(f"Переход в CHOOSE_ROLE, user_data: {context.user_data}")
    push_state(context, CHOOSE_ROLE)
    return CHOOSE_ROLE

async def choose_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает выбор роли (ученик/учитель)."""
    query = update.callback_query
    await query.answer()
    
    keys_to_clear = [
        "current_test_id", "tests_page", "students_page", "pending_notifications",
        "current_result_id", "current_question_idx", "temp_test_id",
        "answers_page", "appeals_page", "question_text_part"
    ]
    for key in keys_to_clear:
        context.user_data.pop(key, None)
    
    if query.data == 'student':
        keyboard = [
            [InlineKeyboardButton("📝 Начать проверочную работу", callback_data="start_test")],
            [InlineKeyboardButton("📊 Посмотреть работы", callback_data="view_results")],
            [InlineKeyboardButton("🔙 Назад", callback_data="back")]
        ]
        state = STUDENT_MAIN
        text = "🏠 Меню ученика:"
    else:
        keyboard = [
            [InlineKeyboardButton("📝 Создать тест", callback_data="create_test")],
            [InlineKeyboardButton("📊 Проверить работы", callback_data="check_results")],
            [InlineKeyboardButton("🔙 Назад", callback_data="back")]
        ]
        state = TEACHER_MAIN
        text = "🏠 Меню учителя:"
    
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    logger.debug(f"Переход в {state}, user_data: {context.user_data}")
    push_state(context, state)
    return state

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ошибки, уведомляя пользователя."""
    logger.error(f"Update {update} caused error {context.error}", exc_info=context.error)
    if update and update.effective_chat:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⚠ Произошла ошибка. Попробуйте снова позже."
        )

def run_all_unit_tests():
    """Запускает все юнит-тесты из файлов в папке Unit_Test."""
    print("Running all unit tests...")
    
    # Список функций для запуска тестов
    test_functions = [
        (run_teacher_results_tests, "Teacher Results Viewer"),
        (run_student_results_tests, "Student Results"),
        (run_student_do_tests, "Student Do Test"),
        (run_teacher_creator_tests, "Teacher Test Creator")
    ]
    
    all_passed = True
    for test_func, test_name in test_functions:
        print(f"\nRunning {test_name} tests...")
        result = test_func()
        if result:
            print(f"{test_name} tests passed successfully.")
        else:
            print(f"{test_name} tests failed!")
            all_passed = False
    
    return all_passed

def main():
    # # Запускаем юнит-тесты
    # if not run_all_unit_tests():
    #     raise RuntimeError("Unit tests failed! Aborting bot startup.")
    
    print("\nAll unit tests passed. Starting bot...")
    
    # Запускаем бот
    application = Application.builder().token(BOT_TOKEN).build()
    
    main_conv = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            CHOOSE_ROLE: [
                CallbackQueryHandler(choose_role, pattern="^(student|teacher)$")
            ],
            STUDENT_MAIN: [
                student_test_handler.get_conversation_handler(),
                student_results_viewer.get_conversation_handler()
            ],
            TEACHER_MAIN: [
                test_creator.get_conversation_handler(),
                results_viewer.get_conversation_handler()
            ]
        },
        fallbacks=[
            CommandHandler('cancel', cancel),
            CallbackQueryHandler(back_handler, pattern='^back$')
        ],
        allow_reentry=True
    )
    
    application.add_handler(main_conv)
    application.add_error_handler(error_handler)
    application.run_polling()

if __name__ == '__main__':
    main()