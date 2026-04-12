# threadgoon

`threadgoon` is a Python downloader for scanning multiple 4chan boards through the 4cdn JSON API, finding threads that match your file filters, and downloading media from the threads you choose.

The repository now includes `threadgoon-v5.1.py`, an extended version focused on:

- scanning multiple boards in one run
- filtering by file extension, keywords, and file size
- concurrent thread processing
- paginated interactive thread selection
- safe file naming and structured output folders
- retry handling, progress bars, and session logging

## Features

- Multi-board scan using the catalog and thread JSON endpoints.
- Configurable file filters for `.webm`, `.mp4`, `.gif`, `.jpg`, `.jpeg`, and `.png`.
- Optional keyword filtering based on post subject, comment text, and filename.
- Optional minimum and maximum file size limits.
- Concurrent thread fetches with retry/backoff handling for unstable requests.
- Interactive pagination so you can browse matched threads before downloading.
- Download summary with processed threads, matched threads, downloaded files, skipped files, errors, and total size.
- `rich` terminal UI support when available, with plain terminal fallback otherwise.

## Requirements

- Python 3.10+
- `requests`
- `tqdm`
- `rich` (optional, for nicer terminal output)

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install requests tqdm rich
```

## Configuration

The script is configured through constants near the top of `threadgoon-v5.1.py`.

Important options:

- `ALL_ADULT_BOARDS`: boards to scan
- `ALLOWED_EXTENSIONS`: file types to match
- `KEYWORDS`: keyword list used for matching post text
- `MIN_MATCHED_FILES`: minimum number of matching files required for a thread
- `MIN_FILE_SIZE` / `MAX_FILE_SIZE`: optional size filters in bytes
- `MAX_WORKERS`: number of concurrent thread fetch workers
- `OUTPUT_DIR`: base download directory
- `PAGE_SIZE`: number of matched threads shown per page

## Usage

```bash
python3 threadgoon-v5.1.py
```

Typical flow:

1. The script scans the configured boards.
2. It builds a summary of matching threads and files.
3. It shows paginated results in the terminal.
4. You choose thread indexes or ranges to download.
5. Matching files are saved into structured folders under `output/`.

## Output Layout

Downloaded files are stored like this:

```text
output/
  <board>/
    <thread_id>_<thread_title>/
      <tim>_<original_filename>.<ext>
```

## Logging

The script writes runtime information to:

```text
download-v5.log
```

## Repository Contents

- `threadgoon-v5.1.py`: latest extended downloader
- `threadgoon-V4.1.py`: previous version
- older legacy versions kept for reference

## Notes

- The script relies on public 4cdn JSON endpoints and may be affected by API changes, rate limits, or unavailable threads.
- Use it responsibly and only in ways that comply with the target site's rules and the laws applicable in your location.

## License

See [LICENSE](LICENSE).
