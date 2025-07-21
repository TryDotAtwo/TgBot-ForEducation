import json
import os
import uuid
from datetime import datetime
import logging
from queue import Queue
from threading import Lock, Thread

logger = logging.getLogger(__name__)

class Database:
    def __init__(self):
        self.data_dir = "data"
        self.tests_file = os.path.join(self.data_dir, "tests.json")
        self.results_file = os.path.join(self.data_dir, "results.json")
        self.users_file = os.path.join(self.data_dir, "users.json")
        self.lock = Lock()  # Единая блокировка для операций с файлами
        self.write_queue = Queue()
        self._init_data_files()
        self._start_orchestrator()

    def _start_orchestrator(self):
        """Запускает оркестратор для обработки задач записи в отдельном потоке."""
        def worker():
            while True:
                func, args, kwargs = self.write_queue.get()
                try:
                    func(*args, **kwargs)
                except Exception as e:
                    logger.error(f"Ошибка в оркестраторе: {e}")
                self.write_queue.task_done()

        self.orchestrator_thread = Thread(target=worker, daemon=True)
        self.orchestrator_thread.start()

    def _init_data_files(self):
        """Инициализирует файлы данных с правильной структурой."""
        os.makedirs(self.data_dir, exist_ok=True)
        
        for file in [self.tests_file, self.results_file, self.users_file]:
            if not os.path.exists(file):
                self._save_to_file(file, {})
                logger.info(f"Создан файл {file}")

    def _load_file(self, filename: str) -> dict:
        """Загружает данные из файла с учетом блокировки."""
        with self.lock:
            try:
                with open(filename, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if not isinstance(data, dict):
                        logger.warning(f"Некорректный формат {filename}, возвращаем пустой словарь")
                        return {}
                    return data
            except json.JSONDecodeError:
                logger.error(f"Файл {filename} повреждён или пуст")
                return {}
            except FileNotFoundError:
                logger.error(f"Файл {filename} не найден, создаём новый")
                self._save_to_file(filename, {})
                return {}
            except Exception as e:
                logger.error(f"Ошибка загрузки {filename}: {e}")
                return {}

    def _save_to_file(self, filename: str, data: dict):
        """Сохраняет данные в файл (вызывается под блокировкой в оркестраторе)."""
        try:
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            logger.debug(f"Файл {filename} успешно сохранён")
        except Exception as e:
            logger.error(f"Ошибка записи в {filename}: {e}")
            raise

    def save_test(self, test_data: dict) -> str:
        """Сохраняет новый тест через оркестратор и возвращает его ID."""
        result = [""]  # Для хранения результата

        def _save_test():
            tests_data = self._load_file(self.tests_file)
            teacher_id = str(test_data["teacher_id"])
            if teacher_id not in tests_data:
                tests_data[teacher_id] = {"tests": []}
            test_data["id"] = str(uuid.uuid4())
            test_data["created_at"] = datetime.now().isoformat()
            test_data["teacher_id"] = teacher_id
            tests_data[teacher_id]["tests"].append(test_data)
            self._save_to_file(self.tests_file, tests_data)
            logger.info(f"Тест сохранён с ID {test_data['id']} для teacher_id {teacher_id}")
            result[0] = test_data["id"]

        self.write_queue.put((_save_test, [], {}))
        self.write_queue.join()  # Ждём завершения записи
        return result[0]

    def load_teacher_tests(self, teacher_id: str) -> list:
        """Загружает все тесты, созданные указанным преподавателем."""
        tests_data = self._load_file(self.tests_file)
        return tests_data.get(str(teacher_id), {}).get("tests", [])

    def load_test_by_id(self, test_id: str) -> dict | None:
        """Загружает тест по его ID."""
        tests_data = self._load_file(self.tests_file)
        for teacher_data in tests_data.values():
            for test in teacher_data.get("tests", []):
                if test.get("id") == test_id:
                    return test
        return None

    def load_all_tests(self) -> dict:
        """Загружает все тесты из файла tests.json."""
        return self._load_file(self.tests_file)

    def save_result(self, user_id: str, result_data: dict) -> str:
        """Сохраняет результат теста через оркестратор и возвращает ID результата."""
        result = [""]  # Для хранения результата

        def _save_result():
            data = self._load_file(self.results_file)
            if user_id not in data:
                data[user_id] = {"tests": []}
            result_data["id"] = str(uuid.uuid4())
            result_data["appeals"] = []
            data[user_id]["tests"].append(result_data)
            self._save_to_file(self.results_file, data)
            logger.info(f"Результат сохранён для пользователя {user_id}, тест {result_data['test_id']}")
            result[0] = result_data["id"]

        self.write_queue.put((_save_result, [], {}))
        self.write_queue.join()  # Ждём завершения записи
        return result[0]

    def save_appeal(self, user_id: str, result_id: str, appeal_data: dict):
        """Сохраняет апелляцию через оркестратор, обновляя существующую."""
        def _save_appeal():
            data = self._load_file(self.results_file)
            if user_id not in data or "tests" not in data[user_id]:
                logger.error(f"Пользователь {user_id} не найден или не имеет тестов")
                raise ValueError(f"Пользователь {user_id} не найден")
            for test in data[user_id]["tests"]:
                if test["id"] == result_id:
                    if "appeals" not in test:
                        test["appeals"] = []
                    question_idx = appeal_data["question_idx"]
                    for i, existing_appeal in enumerate(test["appeals"]):
                        if existing_appeal["question_idx"] == question_idx:
                            appeal_data["id"] = existing_appeal["id"]
                            test["appeals"][i] = appeal_data
                            self._save_to_file(self.results_file, data)
                            logger.info(f"Апелляция обновлена для user_id={user_id}, result_id={result_id}, вопрос {question_idx}")
                            return
                    appeal_data["id"] = str(uuid.uuid4())
                    test["appeals"].append(appeal_data)
                    self._save_to_file(self.results_file, data)
                    logger.info(f"Новая апелляция добавлена для user_id={user_id}, result_id={result_id}, вопрос {question_idx}")
                    return
            logger.error(f"Результат {result_id} не найден для user_id={user_id}")
            raise ValueError(f"Результат {result_id} не найден")

        self.write_queue.put((_save_appeal, [], {}))
        self.write_queue.join()  # Ждём завершения записи

    def load_all_appeals(self) -> list:
        """Загружает все апелляции всех пользователей."""
        try:
            data = self._load_file(self.results_file)
            all_appeals = []
            for user_id, user_data in data.items():
                for test in user_data.get("tests", []):
                    for appeal in test.get("appeals", []):
                        appeal["user_id"] = user_id
                        appeal["test_id"] = test["test_id"]
                        all_appeals.append(appeal)
            return all_appeals
        except Exception as e:
            logger.error(f"Ошибка загрузки апелляций: {e}")
            return []

    def load_student_results(self, user_id: str) -> list:
        """Загружает все результаты тестов для указанного пользователя."""
        try:
            data = self._load_file(self.results_file)
            return data.get(user_id, {}).get("tests", [])
        except Exception as e:
            logger.error(f"Ошибка загрузки результатов для пользователя {user_id}: {e}")
            return []

    def load_all_results(self) -> list:
        """Загружает все результаты всех пользователей."""
        try:
            data = self._load_file(self.results_file)
            all_results = []
            if not isinstance(data, dict):
                logger.error(f"Некорректный формат данных в results.json: ожидался словарь, получен {type(data)}")
                return []
            for user_id, user_data in data.items():
                for test in user_data.get("tests", []):
                    test["user_id"] = user_id
                    all_results.append(test)
            logger.info(f"Loaded {len(all_results)} results")
            return all_results
        except Exception as e:
            logger.error(f"Ошибка загрузки результатов: {e}")
            return []

    def _load_results_file(self) -> dict:
        """Загружает данные из results.json через _load_file."""
        return self._load_file(self.results_file)