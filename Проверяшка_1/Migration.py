import json
import os
import logging

logger = logging.getLogger(__name__)

def migrate_results_json(results_file: str):
    if not os.path.exists(results_file):
        logger.error(f"Файл {results_file} не найден.")
        return logger.error(f"Файл {results_file} не найден.")
    try:
        with open(results_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            logger.error(f"Некорректный формат {results_file}: ожидался словарь.")
            return

        for user_id, user_data in data.items():
            for result in user_data.get("tests", []):
                if "scores" not in result:
                    result["scores"] = {}
                if "comments" not in result:
                    result["comments"] = {}

        with open(results_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info(f"Миграция {results_file} завершена.")

    except:
        print("Kek")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    migrate_results_json("data/results.json")