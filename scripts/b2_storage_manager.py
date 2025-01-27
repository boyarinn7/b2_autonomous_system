import os
import json
import logging
from botocore.exceptions import ClientError
from modules.api_clients import get_b2_client
from modules.logger import get_logger
from modules.error_handler import handle_error
from modules.utils import ensure_directory_exists
from modules.config_manager import ConfigManager
import subprocess  # Для запуска внешнего скрипта

# === Инициализация конфигурации и логирования ===
config = ConfigManager()
logger = get_logger("b2_storage_manager")

# === Константы из конфигурации ===
B2_BUCKET_NAME = config.get('API_KEYS.b2.bucket_name')
CONFIG_PUBLIC_PATH = config.get('FILE_PATHS.config_public')
FILE_EXTENSIONS = ['.json', '.png', '.mp4']
FOLDERS = [
    config.get('FILE_PATHS.folder_444'),
    config.get('FILE_PATHS.folder_555'),
    config.get('FILE_PATHS.folder_666')
]
ARCHIVE_FOLDER = config.get('FILE_PATHS.archive_folder')

# Регулярное выражение для проверки формата имени файла
import re
FILE_NAME_PATTERN = re.compile(r"^\d{8}-\d{4}\.\w+$")

def log_folders_state(s3, folders, stage):
    logger.info(f"\n📂 Состояние папок ({stage}):")
    for folder in folders:
        files = list_files_in_folder(s3, folder)
        logger.info(f"- {folder}: {files}")

def load_config_public(s3):
    try:
        local_path = os.path.basename(CONFIG_PUBLIC_PATH)
        s3.download_file(B2_BUCKET_NAME, CONFIG_PUBLIC_PATH, local_path)
        with open(local_path, 'r', encoding='utf-8') as file:
            config_data = json.load(file)
            logger.info(f"✅ Содержимое config_public.json: {config_data}")
            return config_data
    except FileNotFoundError:
        return {}
    except ClientError as e:
        logger.error(f"Error loading config_public.json: {e.response['Error']['Message']}")
        return {}

def save_config_public(s3, data):
    try:
        local_path = os.path.basename(CONFIG_PUBLIC_PATH)
        with open(local_path, 'w', encoding='utf-8') as file:
            json.dump(data, file, ensure_ascii=False, indent=4)
        s3.upload_file(local_path, B2_BUCKET_NAME, CONFIG_PUBLIC_PATH)
    except Exception as e:
        logger.error(f"Error saving config_public.json: {e}")

def list_files_in_folder(s3, folder_prefix):
    try:
        response = s3.list_objects_v2(Bucket=B2_BUCKET_NAME, Prefix=folder_prefix)
        return [
            obj['Key'] for obj in response.get('Contents', [])
            if obj['Key'] != folder_prefix and not obj['Key'].endswith('.bzEmpty') and FILE_NAME_PATTERN.match(os.path.basename(obj['Key']))
        ]
    except ClientError as e:
        logger.error(f"Error listing files in {folder_prefix}: {e.response['Error']['Message']}")
        return []

def get_ready_groups(files):
    groups = {}
    for file_key in files:
        base_name = os.path.basename(file_key)
        if FILE_NAME_PATTERN.match(base_name):
            group_id = base_name.rsplit('.', 1)[0]
            groups.setdefault(group_id, []).append(base_name)

    ready_groups = []
    for group_id, file_list in groups.items():
        expected_files = [group_id + ext for ext in FILE_EXTENSIONS]
        if all(file in file_list for file in expected_files):
            ready_groups.append(group_id)

    return ready_groups


def handle_publish(s3, config_data):
    """Перемещает все файлы с указанными generation_id в архив B2, пока список не станет пустым."""

    while True:
        generation_ids = config_data.get("generation_id", [])

        if not generation_ids:
            logger.info("📂 Нет generation_id в config_public.json, публикация завершена.")
            return  # ❌ Если generation_id пуст – процесс завершается

        if isinstance(generation_ids, str):
            generation_ids = [generation_ids]  # Приводим к списку, если это строка

        logger.info(f"📂 Найдены generation_id: {generation_ids}, перемещаем файлы в архив...")

        # Папки, где ищем файлы с этими generation_id
        source_folders = ["444/", "555/", "666/"]

        archived_ids = []  # 🔹 Список ID, которые отправлены в архив

        for generation_id in generation_ids:
            for folder in source_folders:
                files_to_move = list_files_in_folder(s3, folder)  # Получаем список файлов

                for file_key in files_to_move:
                    if generation_id in file_key:  # 🏷 Фильтруем файлы по generation_id
                        archive_path = f"data/archive/{os.path.basename(file_key)}"

                        try:
                            # 📤 Перемещаем файл в архив
                            s3.copy_object(Bucket=B2_BUCKET_NAME, CopySource={"Bucket": B2_BUCKET_NAME, "Key": file_key},
                                           Key=archive_path)
                            s3.delete_object(Bucket=B2_BUCKET_NAME, Key=file_key)
                            logger.info(f"✅ Файл {file_key} перемещён в архив: {archive_path}")

                            if generation_id not in archived_ids:
                                archived_ids.append(generation_id)  # Запоминаем, что этот ID заархивирован

                        except ClientError as e:
                            logger.error(f"❌ Ошибка при архивировании {file_key}: {e.response['Error']['Message']}")

        # 🏷 Удаляем только заархивированные generation_id из списка
        config_data["generation_id"] = [gid for gid in generation_ids if gid not in archived_ids]

        # ✅ Если список generation_id пуст – удаляем ключ
        if not config_data["generation_id"]:
            del config_data["generation_id"]

        # 📤 Загружаем обновлённый config_public.json в B2
        save_config_public(s3, config_data)
        logger.info(f"✅ Архивация завершена для: {archived_ids}")

        # 🔄 Проверяем, остались ли generation_id, если нет – выходим из цикла
        if not config_data.get("generation_id"):
            logger.info("🎉 Все опубликованные группы заархивированы, завершаем процесс.")
            break


def move_group(s3, src_folder, dst_folder, group_id):
    for ext in FILE_EXTENSIONS:
        src_key = f"{src_folder}{group_id}{ext}"
        dst_key = f"{dst_folder}{group_id}{ext}"
        try:
            s3.head_object(Bucket=B2_BUCKET_NAME, Key=src_key)
            s3.copy_object(Bucket=B2_BUCKET_NAME, CopySource={"Bucket": B2_BUCKET_NAME, "Key": src_key}, Key=dst_key)
            s3.delete_object(Bucket=B2_BUCKET_NAME, Key=src_key)
        except ClientError as e:
            if e.response['Error']['Code'] != "NoSuchKey":
                logger.error(f"Error moving {src_key}: {e.response['Error']['Message']}")

def process_folders(s3, folders):
    empty_folders = set()
    changes_made = True

    while changes_made:
        changes_made = False
        for i in range(len(folders) - 1, 0, -1):
            src_folder = folders[i]
            dst_folder = folders[i - 1]

            if src_folder in empty_folders:
                continue

            src_files = list_files_in_folder(s3, src_folder)
            dst_files = list_files_in_folder(s3, dst_folder)

            src_ready = get_ready_groups(src_files)
            dst_ready = get_ready_groups(dst_files)

            for group_id in src_ready:
                if len(dst_ready) < 1:
                    move_group(s3, src_folder, dst_folder, group_id)
                    changes_made = True

            if not src_ready:
                empty_folders.add(src_folder)

    return list(empty_folders)

def main():
    try:
        s3 = get_b2_client()

        # Лог начального состояния папок
        log_folders_state(s3, FOLDERS, "Начало процесса")

        config_data = load_config_public(s3)

        handle_publish(s3, config_data)

        empty_folders = process_folders(s3, FOLDERS)

        if empty_folders:
            config_data['empty'] = empty_folders
        else:
            config_data.pop('empty', None)

        save_config_public(s3, config_data)

        # Лог конечного состояния папок
        log_folders_state(s3, FOLDERS, "Конец процесса")

        # Лог содержимого config_public.json в конце процесса
        logger.info(f"✅ Финальное содержимое config_public.json: {config_data}")

        # Запуск generate_content.py при наличии пустых папок
        if empty_folders:
            logger.info("⚠️ Найдены пустые папки. Запуск generate_content.py...")
            try:
                subprocess.run(["python", os.path.join(config.get('FILE_PATHS.scripts_folder'), "generate_content.py")], check=True)
                logger.info("✅ Скрипт generate_content.py выполнен успешно.")
            except subprocess.CalledProcessError as e:
                logger.error(f"❌ Ошибка при выполнении generate_content.py: {e}")

    except Exception as e:
        handle_error(logger, e, "Error in main process")

if __name__ == "__main__":
    main()
