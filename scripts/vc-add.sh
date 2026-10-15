#!/usr/bin/env bash
# Создаёт задачу VideoCutter из локальных видеофайлов — через API сервиса.
#
# Использование:
#   vc-add.sh <source> [preview|name] [name] [флаги...]
#
# Позиционные аргументы (порядок):
#   1-й  — source-видео (из него режутся фрагменты), обязательно;
#   2-й  — если существующий файл, то preview-видео (что смотрит оператор),
#          иначе — имя задачи;
#   3-й  — имя задачи (2-й при этом обязан быть файлом).
#
# Флаги (побеждают позиционные; смешивать --source/--preview/--name
# с позиционными аргументами нельзя):
#   --source PATH      source-видео
#   --preview PATH     preview-видео
#   --name NAME        имя задачи (по умолчанию — из имени source-файла)
#   --project PID      привязать задачу к проекту (нет такого — создадим)
#   --url BASE         базовый URL backend (по умолч. http://localhost:$VC_BACKEND_PORT)
#   --wait             ждать готовности индекса (по умолчанию — да)
#   --no-wait          не ждать, только создать
#   --wait-timeout SEC сколько ждать готовности (по умолч. 1800)
#   --dry-run          только показать запросы, ничего не делать
#   -h, --help         эта справка
#
# Окружение:
#   VC_AUTH_TOKEN      токен доступа, если на сервисе включён фильтр
#                      (тот же, что у сервиса; без него — 401)
#   VC_BACKEND_PORT    порт backend на хосте (по умолч. 8001)
#   VC_UI_PORT         порт UI на хосте (по умолч. 3001, только для ссылки)
#
# Примеры:
#   vc-add.sh 6.mp4                        # одно видео, имя «6»
#   vc-add.sh 6.mp4 6_preview.mp4          # пара source+preview
#   vc-add.sh 6.mp4 6_preview.mp4 vyezd    # пара + имя
#   vc-add.sh 6.mp4 vyezd --project H      # имя + проект
#   vc-add.sh --source 6.mp4 --preview 6_preview.mp4 --name vyezd --project H
set -euo pipefail

URL="http://localhost:${VC_BACKEND_PORT:-8001}"
UI_URL="http://localhost:${VC_UI_PORT:-3001}"
TOKEN="${VC_AUTH_TOKEN:-}"
SOURCE=""; PREVIEW=""; NAME=""; PROJECT=""
WAIT=1; WAIT_TIMEOUT=1800; DRY_RUN=0
POS=()

usage() { sed -n '2,/^set -euo/p' "$0" | sed 's/^# \?//'; exit "${1:-0}"; }
die() { echo "Ошибка: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "нужна утилита '$1'"; }

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --source)   SOURCE="${2:?нужен путь}"; shift 2 ;;
    --preview)  PREVIEW="${2:?нужен путь}"; shift 2 ;;
    --name)     NAME="${2:?нужно имя}"; shift 2 ;;
    --project)  PROJECT="${2:?нужен id/имя проекта}"; shift 2 ;;
    --url)      URL="${2:?нужен URL}"; shift 2 ;;
    --wait)     WAIT=1; shift ;;
    --no-wait)  WAIT=0; shift ;;
    --wait-timeout) WAIT_TIMEOUT="${2:?нужны секунды}"; shift 2 ;;
    --dry-run)  DRY_RUN=1; shift ;;
    --*) die "неизвестный флаг '$1' (см. --help)" ;;
    *) POS+=("$1"); shift ;;
  esac
done

# Флаги файлов конфликтуют с позиционными — требуем что-то одно.
if { [ -n "$SOURCE" ] || [ -n "$PREVIEW" ] || [ -n "$NAME" ]; } && [ "${#POS[@]}" -gt 0 ]; then
  die "флаги --source/--preview/--name нельзя смешивать с позиционными аргументами"
fi

if [ -z "$SOURCE" ]; then
  case "${#POS[@]}" in
    0) usage 1 ;;
    1) SOURCE="${POS[0]}" ;;
    2)
      if [ -f "${POS[1]}" ]; then PREVIEW="${POS[1]}"; else NAME="${POS[1]}"; fi
      SOURCE="${POS[0]}" ;;
    3)
      [ -f "${POS[1]}" ] || die "второй аргумент '${POS[1]}' — не файл (preview), а имя уже задано третьим"
      SOURCE="${POS[0]}"; PREVIEW="${POS[1]}"; NAME="${POS[2]}" ;;
    *) die "слишком много позиционных аргументов (см. --help)" ;;
  esac
fi

[ -f "$SOURCE" ] || die "source-файл '$SOURCE' не найден"
if [ -n "$PREVIEW" ] && [ ! -f "$PREVIEW" ]; then
  die "preview-файл '$PREVIEW' не найден"
fi

need curl; need python3

AUTH=()
if [ -n "$TOKEN" ]; then AUTH=(-H "Authorization: Bearer $TOKEN"); fi

# GET -> тело ответа (curl сыпет прогресс в stderr, тело — в stdout).
api_get() {
  curl -sS "${AUTH[@]}" "$URL$1"
}

# POST multipart -> "HTTP_CODE\nBODY".
api_upload() {
  local args=(-sS -X POST "${AUTH[@]}" "$URL/api/v1/workspaces/")
  args+=(-F "source=@$SOURCE")
  [ -n "$PREVIEW" ] && args+=(-F "preview=@$PREVIEW")
  [ -n "$NAME" ] && args+=(-F "name=$NAME")
  if [ "$DRY_RUN" = 1 ]; then
    echo "+ curl -X POST [Authorization...] $URL/api/v1/workspaces/ -F source=@$SOURCE${PREVIEW:+ -F preview=@$PREVIEW}${NAME:+ -F name=$NAME}"
    return 0
  fi
  curl "${args[@]}" -w '\n%{http_code}'
}

# POST JSON -> "HTTP_CODE\nBODY". $1 — путь, $2 — JSON-тело.
api_post_json() {
  if [ "$DRY_RUN" = 1 ]; then
    echo "+ curl -X POST [Authorization...] $URL$1 -d '$2'"
    return 0
  fi
  curl -sS -X POST "${AUTH[@]}" -H "Content-Type: application/json" \
    -d "$2" "$URL$1" -w '\n%{http_code}'
}

# Разобрать "BODY\nCODE" -> вывести BODY, вернуть 0 iff 2xx, иначе текст ошибки.
expect_ok() {
  local body code err
  body="$(sed '$d' <<<"$1")"
  code="$(tail -n1 <<<"$1")"
  if [[ "$code" =~ ^2 ]]; then echo "$body"; return 0; fi
  err="$(python3 -c "import json,sys; print(json.load(sys.stdin).get('error', 'HTTP $code'))" <<<"$body" 2>/dev/null || echo "HTTP $code")"
  die "$err (HTTP $code)"
}

jget() { python3 -c "import json,sys; print(json.load(sys.stdin)$1)"; }  # jget "['id']"
urlenc() { python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=''))" "$1"; }

echo "== загрузка: source='$SOURCE'${PREVIEW:+, preview='$PREVIEW'}${NAME:+, name='$NAME'}"
UPLOAD_RESP="$(api_upload)"
[ "$DRY_RUN" = 1 ] && exit 0
TASK_BODY="$(expect_ok "$UPLOAD_RESP")"
TASK_ID="$(jget "['id']" <<<"$TASK_BODY")"
echo "Задача создана: '$TASK_ID' (индексация — в фоне)"

if [ -n "$PROJECT" ]; then
  # Проект: ищем по id или имени, нет — создаём с этим именем.
  PROJS="$(api_get "/api/v1/projects/")"
  PID="$(PROJECT="$PROJECT" python3 -c "
import json, os, sys
want = os.environ['PROJECT']
for p in json.load(sys.stdin):
    if p.get('id') == want or p.get('name') == want:
        print(p['id']); break
" <<<"$PROJS")"
  if [ -z "$PID" ]; then
    echo "Проекта '$PROJECT' нет — создаём"
    # JSON строим через python во избежание кавычек в имени:
    BODY="$(PROJECT="$PROJECT" python3 -c "import json,os; print(json.dumps({'name': os.environ['PROJECT']}))")"
    CREATED="$(expect_ok "$(api_post_json "/api/v1/projects/" "$BODY")")"
    PID="$(jget "['id']" <<<"$CREATED")"
  fi
  ATTACH_BODY="$(TASK_ID="$TASK_ID" python3 -c "import json,os; print(json.dumps({'task_id': os.environ['TASK_ID']}))")"
  expect_ok "$(api_post_json "/api/v1/projects/$(urlenc "$PID")/tasks/" "$ATTACH_BODY")" >/dev/null
  echo "Задача привязана к проекту '$PID'"
fi

if [ "$WAIT" = 1 ]; then
  echo -n "Жду готовности индекса"
  START="$(date +%s)"
  while true; do
    DETAIL="$(api_get "/api/v1/workspaces/$(urlenc "$TASK_ID")/")"
    IDX="$(python3 -c "import json,sys; d=json.load(sys.stdin); print(int(bool(d.get('indexing'))), int(bool(d.get('broken'))), d.get('error',''))" <<<"$DETAIL")"
    read -r INDEXING BROKEN ERR <<<"$IDX"
    if [ "$BROKEN" = 1 ]; then die "задача битая: $ERR"; fi
    if [ "$INDEXING" = 0 ]; then break; fi
    if [ "$(( $(date +%s) - START ))" -ge "$WAIT_TIMEOUT" ]; then
      die "таймаут ожидания индекса ($WAIT_TIMEOUT c) — задача индексируется, смотрите в UI"
    fi
    echo -n "."; sleep 5
  done
  FRAMES="$(python3 -c "import json,sys; print(json.load(sys.stdin).get('total_frames'))" <<<"$DETAIL")"
  echo " готово (кадров: $FRAMES)"
fi

echo "UI: $UI_URL/"
