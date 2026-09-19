# Модель задачи VC: `task.json`

Sidecar с workflow-атрибутами задачи рядом с `fragments.tsv` внутри
workspace. Аналог бекапа задачи CVAT (JSON), но только нужное нам.

## Почему отдельный файл

- `fragments.tsv` остаётся чистыми данными разметки (фрагменты +
  `# position`, `# settings`) — формат не меняется, миграция старых
  задач не нужна: нет `task.json` = дефолты.
- Атрибуты — списки и вложенность (`assignees[]`, будущие `segments[]`),
  плоские `# key`-строки TSV для этого не годятся.
- Без БД (stateless): чтение per-request, записи атомарно (tmp+rename),
  памяти в ядре ноль. Конкуренция — last-write-wins, как у `fragments.tsv`.

## Формат v1

```json
{
  "v": 1,
  "owner": "username | null",
  "assignees": ["username"],
  "status": "new | in_progress | completed | rejected",
  "stage": "annotation | validation | acceptance",
  "created_at": "iso8601 | null",
  "status_changed_at": "iso8601 | null",
  "status_changed_by": "username | null",
  "notes": "string",
  "subset": "train | val | test | null"
}
```

Дефолты: `status=new`, `stage=annotation`, списки/строки пустые, даты null.

## Правила

- Читатель игнорирует неизвестные ключи, писатель сохраняет их
  (расширяемость без миграций; поле `v` — под будущие схемы).
- Битый JSON = дефолты + warning в лог (задачу не роняем).
- `status`/`stage` вне справочника при записи = `ValueError`.
- Выводимое не храним: прогресс, превью, `updated_at` (mtime),
  счётчики кадров — вычисляются.

## Соответствие CVAT (чистая копия)

| CVAT | VC |
|---|---|
| owner / assignee | `owner` / `assignees[]` (списком: джоб нет, деление одноуровневое) |
| job state new/in_progress/rejected/completed | `status` (задача ≈ одна джоба) |
| stage annotation/validation/acceptance | `stage` (сейчас всегда `annotation`) |
| created/updated, statusChangedBy/At | `created_at`, `status_changed_at/by` |
| guide для аннотатора | `notes` |
| subset | `subset` (optional) |
| labels, consensus, storage, webhooks | не копируем |
| segment_size/overlap → jobs | точка расширения: позже `segments: [{start, end, assignee, stage, state}]` вложатся сюда же без смены формата |

Локально: один пользователь сам себе owner+assignee, гейтов нет —
поведение 1-в-1 с сегодняшним. Назначения чужих CVAT-тасков сюда
не относятся: это своя система VC (реестр — позже).
