# Desktop engine protocol v1

The Swift app launches `Contents/Resources/engine/threadgoon-engine --workspace PATH`. For development only, it can launch `.venv/bin/python -m threadgoon.bridge` using `THREADGOON_DEV_ROOT`. There is one controller and one workspace lease per helper process. EOF on stdin or `shutdown` stops workers before closing SQLite and releasing the lease.

Both directions use UTF-8 JSON objects, one per line. stdout is exclusively protocol traffic; diagnostic output goes to stderr and the workspace log. Swift reads available bytes, buffers partial UTF-8/JSON lines, and drains stderr independently. All frames contain `version: 1`.

Request:

```json
{"version":1,"id":"unique-request-id","command":"search","params":{"boards":["po"],"query":"paper","exact":false}}
```

Response:

```json
{"version":1,"type":"response","id":"unique-request-id","ok":true,"data":{"operation_id":"operation-id"}}
```

A failed command uses `ok: false` and `error`. Startup emits `type: ready`, `app_version` and a snapshot; a startup failure emits `type: fatal` and `error` then exits. Operations are asynchronous: the response acknowledges acceptance, while events report progress and completion. Each active group permits one operation; different groups share the same serialized, rate-limited API client, with independent pause/cancellation state.

Events have `type: event`, `event` and `data`:

- `operation`: `id`, `group`, `state`, `current`, `total`, `message`.
- `operation_finished`: the operation fields plus `result` or `error`. States: `completed`, `cancelled`, `failed`.
- `search_result`: `operation_id` and one exact matching `thread`.
- `download_update`: one persistent download job keyed by its integer ID.
- `snapshot`: workspace, config, threads, boards, downloads, watches, operations, `download_paused`, `downloads_running`.

Thread IDs are `board/thread_id`; download IDs are SQLite integers. Sizes are bytes and can be null. Thread records include `thumbnail_url`, `stale` and attachments; metadata from catalog and exact matches remain distinct. A full attachment history never determines a filtered download batch.

## Commands

| Command | Parameters | Result |
| --- | --- | --- |
| `snapshot` | none | Full snapshot |
| `refresh_boards` | none | Async boards operation |
| `boards_save` | `boards` | Persist selected boards |
| `search` | `boards`, `query`, `exact`, optional `filters` | Async search; final `threads` and board availability |
| `detail` | `key`, optional `filters` | Async filtered exact `thread` |
| `import` | `urls`, optional `filters` | Async imported threads |
| `deep_scan` | `keys`, optional `filters` | Async exact scan for the terminal client |
| `pause_operation`, `resume_operation`, `cancel_operation` | `operation_id` | Updated operation state |
| `prepare_download` | `keys`, optional `filters` | Async single-use `token`, counts, total size, warnings |
| `confirm_download`, `discard_prepared` | `token` | Enqueue/start the confirmed batch, or discard it |
| `download_control` | `action`, optional `ids` | Pause/resume/start, retry failures/cancellations, cancel selected, approve selected waiting jobs |
| `watch_add` | `keys`, optional `filters` | Async validated observation setup |
| `watch_update` | `key`, optional `status`, `auto_download` | Update selected watch |
| `watch_remove` | `key` | Remove observation; preserve files |
| `settings_save` | full `config` | Validate and atomically persist config; rejected while foreground operations/downloads run |
| `diagnostics` | none | Tail of workspace log |
| `shutdown` | none | Reply after worker shutdown and database close, then exit |

Filters use the existing Python configuration fields: extensions, keywords (OR), `keyword_scope` (`file_post` / `whole_thread`), min/max file size and min matched files. A null min/max clears that bound. Native search builds filters from the current form and never uses a pasted URL as a keyword.

Changes to the file destination affect new jobs only. Existing jobs retain their stored relative destinations. Download re-enqueue preserves active, paused, completed, failed and cancelled states; retry is explicit. Observations are restored at startup, while queued manual downloads require continuation. A stopped/crashed helper is restarted explicitly by the native UI and reacquires its lease.
