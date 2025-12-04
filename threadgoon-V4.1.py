import json
import os
import re
import sys
import logging
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.exceptions import RequestException, Timeout
from tqdm import tqdm


BOARD = 'gif'
BASE_URL = 'https://a.4cdn.org'
IMAGE_URL = 'https://i.4cdn.org'
CATALOG_ENDPOINT = f'{BASE_URL}/{BOARD}/catalog.json'
REQUEST_TIMEOUT = (10, 30)  # (connect timeout, read timeout)
CHUNK_SIZE = 8192


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('download.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def sanitize_filename(name):

    if not name:
        return "unnamed"
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = name.strip('. ')
    name = re.sub(r'[\x00-\x1f\x7f]', '', name)
    return name[:200] if len(name) > 200 else name or "unnamed"


def get_unique_filepath(directory, base_filename, extension):

    file_path = os.path.join(directory, f"{base_filename}{extension}")
    if os.path.exists(file_path):
        counter = 1
        while os.path.exists(file_path):
            file_path = os.path.join(directory, f"{base_filename}_{counter}{extension}")
            counter += 1
    return file_path


def fetch_catalog():
    try:
        logger.info(f"Fetching catalog from {CATALOG_ENDPOINT}")
        response = requests.get(CATALOG_ENDPOINT, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return json.loads(response.text)
    except Timeout:
        logger.error(f"Timeout while fetching catalog from {CATALOG_ENDPOINT}")
        print(f"Ошибка: Превышено время ожидания при загрузке каталога")
        sys.exit(1)
    except RequestException as e:
        logger.error(f"Error fetching catalog: {e}")
        print(f"Ошибка при загрузке каталога: {e}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing catalog JSON: {e}")
        print(f"Ошибка при парсинге JSON каталога: {e}")
        sys.exit(1)


def get_thread_data(thread_id):
    thread_url = f'{BASE_URL}/{BOARD}/thread/{thread_id}.json'
    try:
        logger.debug(f"Fetching thread data from {thread_url}")
        response = requests.get(thread_url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return json.loads(response.text)
    except Timeout:
        logger.error(f"Timeout while fetching thread {thread_id}")
        raise
    except RequestException as e:
        logger.error(f"Error fetching thread {thread_id}: {e}")
        raise
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing thread {thread_id} JSON: {e}")
        raise


def download_webms(thread_data, thread_title):
    webms = [post for post in thread_data.get('posts', []) 
             if post.get('ext') == '.webm']
    
    if not webms:
        logger.info(f"No .webm files found in thread: {thread_title}")
        print(f"В треде '{thread_title}' не найдено .webm файлов")
        return
    
    safe_title = sanitize_filename(thread_title)
    try:
        os.makedirs(safe_title, exist_ok=True)
    except OSError as e:
        logger.error(f"Error creating directory {safe_title}: {e}")
        print(f"Ошибка при создании папки '{safe_title}': {e}")
        return
    
    logger.info(f"Downloading {len(webms)} .webm files from thread: {thread_title}")
    
    for webm in tqdm(webms, desc=f"Downloading webms for {safe_title}"):
        try:
            base_filename = sanitize_filename(webm.get('filename', 'unnamed'))
            file_path = get_unique_filepath(safe_title, base_filename, '.webm')
            
            if os.path.exists(file_path):
                logger.debug(f"File already exists: {file_path}, skipping...")
                continue
            
            tim = webm.get('tim')
            if not tim:
                logger.warning(f"No 'tim' field in webm post, skipping")
                continue
            
            file_url = f'{IMAGE_URL}/{BOARD}/{tim}.webm'
            
            try:
                response = requests.get(file_url, stream=True, timeout=REQUEST_TIMEOUT)
                response.raise_for_status()
                
                total_size = int(response.headers.get('content-length', 0))
                filename = os.path.basename(file_path)
                
                try:
                    with open(file_path, "xb") as f:
                        if total_size > 0:
                            with tqdm(total=total_size, unit='B', 
                                     unit_scale=True, unit_divisor=1024,
                                     desc=filename, leave=False) as pbar:
                                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                                    if chunk:
                                        f.write(chunk)
                                        pbar.update(len(chunk))
                        else:
                            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                                if chunk:
                                    f.write(chunk)
                    
                    logger.info(f"Downloaded file: {file_path}")
                    print(f"Скачан файл: {file_path}")
                    
                except FileExistsError:
                    logger.debug(f"File was created by another process: {file_path}")
                    continue
                    
            except Timeout:
                logger.error(f"Timeout while downloading {file_url}")
                print(f"Ошибка: Превышено время ожидания при загрузке {filename}")
                    
                if os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except OSError:
                        pass
                continue
                
            except RequestException as e:
                logger.error(f"Error downloading {file_url}: {e}")
                print(f"Ошибка при загрузке {filename}: {e}")
                if os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except OSError:
                        pass
                continue
                
        except Exception as e:
            logger.error(f"Unexpected error processing webm: {e}", exc_info=True)
            print(f"Неожиданная ошибка при обработке файла: {e}")
            continue


def main():
    try:
        pages = fetch_catalog()
        
        threads = [(thread.get('semantic_url', ''), thread.get('no'), 
                   thread.get('images', 0)) 
                  for page in pages 
                  for thread in page.get('threads', [])]
        
        if threads:
            threads = threads[1:]
        
        if not threads:
            logger.warning("No threads found in catalog")
            print("В каталоге не найдено тредов")
            return
        
        print("\nДоступные треды:")
        print("-" * 60)
        for i, thread in enumerate(threads):
            print(f"{i} | Изображений: {thread[2]}\t| {thread[0]}")
        print("-" * 60)
        
        try:
            user_choice = input("\nВведите номера тредов через пробел: ").strip()
            
            if not user_choice:
                logger.info("No selection made by user")
                print("Выбор не сделан. Выход.")
                return
            
            choice_list = []
            for choice in user_choice.split():
                try:
                    num = int(choice)
                    choice_list.append(num)
                except ValueError:
                    logger.warning(f"Invalid choice format: {choice}")
                    print(f"Предупреждение: '{choice}' не является числом, пропускаем")
            
            if not choice_list:
                print("Не выбрано ни одного валидного номера треда")
                return
            
            max_index = len(threads) - 1
            invalid_choices = [c for c in choice_list if c < 0 or c > max_index]
            if invalid_choices:
                logger.error(f"Invalid choices: {invalid_choices}. Valid range: 0-{max_index}")
                print(f"Ошибка: Недопустимые номера: {invalid_choices}")
                print(f"Допустимый диапазон: 0-{max_index}")
                return
            
            choice_list = sorted(set(choice_list))
            selected_threads = [(threads[choice][1], threads[choice][0]) 
                              for choice in choice_list]
            logger.info(f"User selected threads: {choice_list}")
            
        except (KeyboardInterrupt, EOFError):
            print("\n\nПрервано пользователем. Выход.")
            sys.exit(0)
        
        print(f"\nЗагрузка данных тредов ({len(selected_threads)} тредов)...")
        logger.info(f"Fetching thread data in parallel for {len(selected_threads)} threads")
        
        with ThreadPoolExecutor() as executor:
            futures = {
                executor.submit(get_thread_data, thread_id): (thread_id, thread_title)
                for thread_id, thread_title in selected_threads
            }
            
            print("Обработка...")
            successful_downloads = 0
            failed_downloads = 0
            
            for future in as_completed(futures):
                thread_id, thread_title = futures[future]
                try:
                    thread_data = future.result()
                    
                    logger.info(f"Processing thread: {thread_title} (ID: {thread_id})")
                    print(f"\nОбработка треда: {thread_title} (ID: {thread_id})")
                    
                    download_webms(thread_data, thread_title)
                    successful_downloads += 1
                    
                except Exception as e:
                    logger.error(f"Error processing thread {thread_title} (ID: {thread_id}): {e}", exc_info=True)
                    print(f"Ошибка при обработке треда '{thread_title}' (ID: {thread_id}): {e}")
                    failed_downloads += 1
            
            print("\n" + "=" * 60)
            print(f"Завершено успешно: {successful_downloads}")
            if failed_downloads > 0:
                print(f"Ошибок: {failed_downloads}")
            print("=" * 60)
            logger.info(f"Download session completed. Success: {successful_downloads}, Failed: {failed_downloads}")
    
    except KeyboardInterrupt:
        print("\n\nПрервано пользователем. Выход.")
        logger.info("Program interrupted by user")
        sys.exit(0)
    except Exception as e:
        logger.critical(f"Unexpected error in main: {e}", exc_info=True)
        print(f"Критическая ошибка: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
