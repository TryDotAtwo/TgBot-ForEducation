import gevent.monkey
gevent.monkey.patch_all()  # Monkey-патчинг для устранения предупреждения

import uuid
import random
import logging
import socket
import requests
from locust import HttpUser, task, between
from locust.env import Environment
from locust.runners import LocalRunner
import time

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("locust.log")
    ]
)
logger = logging.getLogger(__name__)

### Тесты Locust ###
class DatabaseUser(HttpUser):
    wait_time = between(1, 5)  # Задержка между запросами от 1 до 5 секунд
    teacher_id = None
    test_id = None
    user_id = None
    result_id = None

    def on_start(self):
        """Инициализация пользователя: создание теста и результата."""
        self.teacher_id = str(uuid.uuid4())
        self.user_id = str(uuid.uuid4())
        
        # Создаём тест
        test_data = {"teacher_id": self.teacher_id, "title": f"Sample Test {self.teacher_id}"}
        response = self.client.post("/tests/", json=test_data)
        if response.status_code == 200:
            self.test_id = response.json().get("test_id")
            logger.info(f"Создан тест для teacher_id: {self.teacher_id}, test_id: {self.test_id}")
            
            # Сохраняем результат для теста
            result_data = {"test_id": self.test_id, "score": random.randint(0, 100)}
            response = self.client.post(f"/results/{self.user_id}", json=result_data)
            if response.status_code == 200:
                self.result_id = response.json().get("result_id")
                logger.info(f"Создан результат для user_id: {self.user_id}, result_id: {self.result_id}")

    @task(3)
    def create_test(self):
        """Создание нового теста."""
        test_data = {"teacher_id": self.teacher_id, "title": f"Sample Test {random.randint(1, 1000)}"}
        response = self.client.post("/tests/", json=test_data)
        if response.status_code == 200:
            new_test_id = response.json().get("test_id")
            logger.info(f"Создан новый тест с test_id: {new_test_id}")

    @task(2)
    def get_teacher_tests(self):
        """Получение тестов преподавателя."""
        self.client.get(f"/tests/{self.teacher_id}")

    @task(1)
    def get_test_by_id(self):
        """Получение теста по ID."""
        if self.test_id:
            self.client.get(f"/tests/id/{self.test_id}")

    @task(2)
    def save_result(self):
        """Сохранение результата теста."""
        if self.test_id:
            result_data = {"test_id": self.test_id, "score": random.randint(0, 100)}
            response = self.client.post(f"/results/{self.user_id}", json=result_data)
            if response.status_code == 200:
                self.result_id = response.json().get("result_id")
                logger.info(f"Создан результат для user_id: {self.user_id}, result_id: {self.result_id}")

    @task(1)
    def save_appeal(self):
        """Сохранение апелляции."""
        if self.result_id and self.user_id:
            appeal_data = {"question_idx": random.randint(0, 10), "text": "Appeal text"}
            self.client.post(f"/appeals/{self.user_id}/{self.result_id}", json=appeal_data)

    @task(1)
    def get_all_appeals(self):
        """Получение всех апелляций."""
        self.client.get("/appeals/")

    @task(1)
    def get_student_results(self):
        """Получение результатов студента."""
        if self.user_id:
            self.client.get(f"/results/{self.user_id}")

    @task(1)
    def get_all_results(self):
        """Получение всех результатов."""
        self.client.get("/results/")

### Проверка доступности порта ###
def check_port(port):
    """Проверяет, свободен ли порт."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        return s.connect_ex(('127.0.0.1', port)) != 0

### Проверка доступности FastAPI ###
def check_fastapi():
    """Проверяет, доступен ли FastAPI-сервер."""
    for _ in range(10):  # Пробуем 10 раз с интервалом 2 секунды
        try:
            response = requests.get("http://localhost:8000", timeout=5)
            if response.status_code == 200:
                logger.info("FastAPI сервер доступен")
                return True
            else:
                logger.warning(f"FastAPI сервер вернул код: {response.status_code}")
        except requests.ConnectionError:
            logger.warning("FastAPI сервер недоступен, ждём...")
        time.sleep(2)
    logger.error("FastAPI сервер не запустился за отведённое время")
    return False

### Программный запуск Locust ###
def run_locust():
    """Программно запускает Locust-тесты."""
    logger.info("Инициализация Locust...")

    # Проверка доступности FastAPI
    if not check_fastapi():
        logger.error("Прерывание: FastAPI сервер недоступен")
        return

    # Проверка порта для Locust
    if not check_port(8089):
        logger.error("Порт 8089 занят. Попробуйте другой порт.")
        return

    try:
        # Создаём окружение Locust
        env = Environment(user_classes=[DatabaseUser], host="http://localhost:8000")
        # Явно создаём LocalRunner
        env.runner = LocalRunner(env)
        logger.info("Runner инициализирован")

        # Создаём веб-интерфейс
        logger.info("Создание веб-интерфейса Locust на http://localhost:8089")
        env.create_web_ui(host="0.0.0.0", port=8089)

        # Проверяем, что runner существует
        if env.runner is None:
            logger.error("Ошибка: runner не инициализирован")
            return

        # Запускаем тесты
        logger.info("Запуск тестов с 10 пользователями, 2 в секунду")
        env.runner.start(user_count=10, spawn_rate=2)

        # Проверяем, работает ли веб-интерфейс
        time.sleep(2)
        try:
            response = requests.get("http://localhost:8089", timeout=5)
            if response.status_code == 200:
                logger.info("Locust веб-интерфейс доступен: http://localhost:8089")
            else:
                logger.error(f"Locust веб-интерфейс вернул код: {response.status_code}")
        except requests.ConnectionError:
            logger.error("Locust веб-интерфейс недоступен на http://localhost:8089")

        try:
            env.runner.greenlet.join()  # Ждём завершения тестов
        except KeyboardInterrupt:
            logger.info("Остановка Locust по запросу пользователя")
            env.runner.quit()
            env.web_ui.stop()
    except Exception as e:
        logger.error(f"Ошибка при запуске Locust: {e}")
        raise

### Основной запуск ###
if __name__ == "__main__":
    run_locust()