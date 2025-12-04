import json
import os
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

catalog_endpoint = 'https://a.4cdn.org/gif/catalog.json'

r = requests.get(catalog_endpoint)

pages = json.loads(r.text)

threads = [(thread['semantic_url'], thread['no'], thread['images']) for page in pages for thread in page['threads']]
threads.pop(0)

for i, thread in enumerate(threads):
    print('{} | I: {}\t| {}'.format(i, thread[2], thread[0]))

user_choice = input('Please make a selection (space-separated): ')

choice_list = user_choice.split(' ')

print('Fetching threads...')

with ThreadPoolExecutor() as executor:
    futures = [executor.submit(requests.get, f'https://a.4cdn.org/gif/thread/{threads[int(choice)][1]}.json') for choice in choice_list]

print('Processing...')

for choice, future in zip(choice_list, as_completed(futures)):
    thread_title = threads[int(choice)][0]
    print("Installing file from thread: {}".format(thread_title))
    thread = json.loads(future.result().text)
    webms = [post for post in thread['posts'] if post.get('ext', False) == '.webm']
    os.makedirs(thread_title, exist_ok=True)
    for webm in tqdm(webms):
        file_path = os.path.join(thread_title, f"{webm['filename']}.webm")
        if not os.path.exists(file_path):
            r = requests.get(f'https://i.4cdn.org/gif/{webm["tim"]}.webm')
            with open(file_path, "wb") as f:
                f.write(r.content)
                print(f"Downloaded file: {file_path}")
