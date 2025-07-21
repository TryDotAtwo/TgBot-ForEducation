from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from database import Database  # Импорт класса Database
import uvicorn
import logging
import uuid

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
db = Database()

### Корневой маршрут для проверки сервера ###
@app.get("/")
def read_root():
    """Проверка доступности FastAPI-сервера."""
    return {"message": "FastAPI сервер работает. Используйте /docs для документации."}

### Модели данных для валидации ###
class TestData(BaseModel):
    teacher_id: str
    title: str

class ResultData(BaseModel):
    test_id: str
    score: int

class AppealData(BaseModel):
    question_idx: int
    text: str

### Эндпоинты FastAPI ###
@app.post("/tests/")
def create_test(test_data: TestData):
    """Создание нового теста."""
    test_id = str(uuid.uuid4())
    test_data_dict = test_data.dict()
    test_data_dict["test_id"] = test_id
    db.save_test(test_data_dict)
    return {"status": "Тест сохранен", "test_id": test_id}

@app.get("/tests/{teacher_id}")
def get_teacher_tests(teacher_id: str):
    """Получение тестов преподавателя."""
    tests = db.load_teacher_tests(teacher_id)
    return {"tests": tests}

@app.get("/tests/id/{test_id}")
def get_test_by_id(test_id: str):
    """Получение теста по ID."""
    test = db.load_test_by_id(test_id)
    if not test:
        raise HTTPException(status_code=404, detail="Тест не найден")
    return test

@app.post("/results/{user_id}")
def save_result(user_id: str, result_data: ResultData):
    """Сохранение результата теста."""
    result_id = str(uuid.uuid4())
    result_data_dict = result_data.dict()
    result_data_dict["result_id"] = result_id
    db.save_result(user_id, result_data_dict)
    return {"status": "Результат сохранен", "result_id": result_id}

@app.post("/appeals/{user_id}/{result_id}")
def save_appeal(user_id: str, result_id: str, appeal_data: AppealData):
    """Сохранение апелляции."""
    try:
        # Проверяем, существует ли результат
        results = db.load_student_results(user_id)
        if not any(r["result_id"] == result_id for r in results):
            raise ValueError(f"Результат {result_id} не найден для пользователя {user_id}")
        db.save_appeal(user_id, result_id, appeal_data.dict())
        return {"status": "Апелляция сохранена"}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@app.get("/appeals/")
def get_all_appeals():
    """Получение всех апелляций."""
    appeals = db.load_all_appeals()
    return {"appeals": appeals}

@app.get("/results/{user_id}")
def get_student_results(user_id: str):
    """Получение результатов студента."""
    results = db.load_student_results(user_id)
    return {"results": results}

@app.get("/results/")
def get_all_results():
    """Получение всех результатов."""
    results = db.load_all_results()
    return {"results": results}

### Основной запуск ###
if __name__ == "__main__":
    logger.info("Запуск FastAPI сервера на http://localhost:8000")
    try:
        uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
    except Exception as e:
        logger.error(f"Ошибка при запуске FastAPI: {e}")
        raise