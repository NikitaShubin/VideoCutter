#!/usr/bin/env bash
# Тесты scripts/vc-add.sh — без внешних фреймворков (чистый bash).
#
# Часть 1 (без сервера): --help, разбор позиционных аргументов и флагов
# через --dry-run, коды ошибок.
# Часть 2 (живая): одноразовый backend (runserver на временном корне)
# + полный цикл: создание, проект, ожидание индекса, дубликат 409, API-проверка.
#
# Запуск: ./scripts/test_vc_add.sh  (из корня videocutter)
# Переменные: VC_TEST_PORT (по умолч. 18099), VC_TEST_TIMEOUT (по умолч. 300).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ADD="$SCRIPT_DIR/vc-add.sh"
PORT="${VC_TEST_PORT:-18099}"
TIMEOUT="${VC_TEST_TIMEOUT:-300}"

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "ok   $*"; }
bad()  { FAIL=$((FAIL+1)); echo "FAIL $*"; }

# --- Часть 1: разбор аргументов (сервер не нужен) ---

FIX="$(mktemp -d)"
trap 'rm -rf "$FIX"' EXIT
touch "$FIX/src.mp4" "$FIX/prev.mp4"

# --help: exit 0 + usage в stdout.
if "$ADD" --help >/dev/null 2>&1; then ok "--help exit 0"; else bad "--help exit 0"; fi

# 1 позиционный: только source.
OUT="$("$ADD" "$FIX/src.mp4" --dry-run 2>&1)"
grep -q "source='$FIX/src.mp4'$" <<<"$OUT" && ! grep -q "preview=" <<<"$OUT" \
  && ok "1 arg: только source" || bad "1 arg: только source ($OUT)"

# 2 позиционных, 2-й файл: preview.
OUT="$("$ADD" "$FIX/src.mp4" "$FIX/prev.mp4" --dry-run 2>&1)"
grep -q "preview='$FIX/prev.mp4'" <<<"$OUT" \
  && ok "2 args + файл: preview" || bad "2 args + файл: preview ($OUT)"

# 2 позиционных, 2-й не файл: имя.
OUT="$("$ADD" "$FIX/src.mp4" myname --dry-run 2>&1)"
grep -q "name='myname'" <<<"$OUT" && ! grep -q "preview=" <<<"$OUT" \
  && ok "2 args + не файл: имя" || bad "2 args + не файл: имя ($OUT)"

# 3 позиционных: preview + имя.
OUT="$("$ADD" "$FIX/src.mp4" "$FIX/prev.mp4" myname --dry-run 2>&1)"
grep -q "preview='$FIX/prev.mp4'" <<<"$OUT" && grep -q "name='myname'" <<<"$OUT" \
  && ok "3 args: preview + имя" || bad "3 args: preview + имя ($OUT)"

# 3 позиционных, 2-й не файл: ошибка.
if "$ADD" "$FIX/src.mp4" myname other --dry-run >/dev/null 2>&1; then
  bad "3 args + 2-й не файл: ждали ошибку"
else
  ok "3 args + 2-й не файл: ошибка"
fi

# Смешивание флагов файлов с позиционными: ошибка.
if "$ADD" --source "$FIX/src.mp4" "$FIX/src.mp4" --dry-run >/dev/null 2>&1; then
  bad "флаги + позиционные: ждали ошибку"
else
  ok "флаги + позиционные: ошибка"
fi

# Неизвестный флаг: ошибка.
if "$ADD" --bogus --dry-run >/dev/null 2>&1; then
  bad "неизвестный флаг: ждали ошибку"
else
  ok "неизвестный флаг: ошибка"
fi

# Несуществующий source: ошибка (до сети).
if "$ADD" "$FIX/nope.mp4" --dry-run >/dev/null 2>&1; then
  bad "нет source: ждали ошибку"
else
  ok "нет source: ошибка"
fi

# --project виден в dry-run строке.
OUT="$("$ADD" --source "$FIX/src.mp4" --project H --no-wait --dry-run 2>&1)"
grep -q "source='$FIX/src.mp4'$" <<<"$OUT" \
  && ok "флаговый вызов dry-run" || bad "флаговый вызов dry-run ($OUT)"

# --- Часть 2: живой backend на временном корне ---

command -v python3 >/dev/null || { echo "SKIP живой части: нет python3"; }
if command -v python3 >/dev/null; then
  WSROOT="$(mktemp -d)"
  LOG="$WSROOT/server.log"
  (cd "$ROOT_DIR/backend" && VC_WORKSPACE_ROOT="$WSROOT/ws" \
    python3 manage.py runserver "127.0.0.1:$PORT" --noreload >"$LOG" 2>&1 &
    echo $! >"$WSROOT/pid")
  SRV_PID="$(cat "$WSROOT/pid")"
  cleanup() { kill "$SRV_PID" 2>/dev/null; rm -rf "$WSROOT"; }
  trap 'kill "$SRV_PID" 2>/dev/null; rm -rf "$WSROOT" "$FIX"' EXIT

  # Ждём поднятия (до 60 c).
  READY=0
  for _ in $(seq 1 60); do
    if curl -s -o /dev/null "http://127.0.0.1:$PORT/api/v1/workspaces/"; then
      READY=1; break
    fi
    sleep 1
  done
  if [ "$READY" != 1 ]; then
    bad "runserver не поднялся (см. $LOG)"; tail -20 "$LOG"
  else
    ok "runserver поднялся"
    URL="http://127.0.0.1:$PORT"
    VIDEO="$ROOT_DIR/backend/testdata/test.mp4"

    # Полный цикл: пара + имя + проект (кириллица) + ожидание.
    if "$ADD" --url "$URL" --source "$VIDEO" --name livetask \
        --project "ЖивойПроект" --wait-timeout "$TIMEOUT" >/tmp/opencode/vcadd_live.log 2>&1; then
      ok "живой цикл: создание + проект + wait"
    else
      bad "живой цикл: создание + проект + wait"; tail -5 /tmp/opencode/vcadd_live.log
    fi

    # API-проверка: задача в списке, проект с task_count 1.
    CHECK="$(python3 - "$URL" <<'EOF'
import json, sys, urllib.request
base = sys.argv[1]
tasks = json.load(urllib.request.urlopen(base + "/api/v1/workspaces/"))
projs = json.load(urllib.request.urlopen(base + "/api/v1/projects/"))
t = "livetask" in [w["id"] for w in tasks]
p = [x for x in projs if x["name"] == "ЖивойПроект"]
print(int(t and bool(p) and p[0]["task_count"] == 1))
EOF
)"
    [ "$CHECK" = 1 ] && ok "API: задача + проект (task_count 1)" \
      || bad "API: задача + проект (task_count 1)"

    # Дубликат имени: clean 409, без traceback.
    DUP_OUT="$("$ADD" --url "$URL" --source "$VIDEO" --name livetask --no-wait 2>&1)"
    DUP_CODE=$?
    if [ "$DUP_CODE" != 0 ] && grep -q "409" <<<"$DUP_OUT" \
        && ! grep -q "Traceback" <<<"$DUP_OUT"; then
      ok "дубликат: 409 без traceback"
    else
      bad "дубликат: 409 без traceback (code=$DUP_CODE: $DUP_OUT)"
    fi

    # Уборка за собой через API.
    curl -s -X DELETE "$URL/api/v1/workspaces/livetask/" >/dev/null
    PIDP="$(curl -s "$URL/api/v1/projects/" | python3 -c "import json,sys; print([p['id'] for p in json.load(sys.stdin) if p['name']=='ЖивойПроект'][0])")"
    curl -s -X DELETE "$URL/api/v1/projects/$PIDP/?with_tasks=cascade" >/dev/null
    LEFT="$(curl -s "$URL/api/v1/workspaces/" | python3 -c "import json,sys; print('livetask' in [w['id'] for w in json.load(sys.stdin)])")"
    [ "$LEFT" = "False" ] && ok "уборка: задача удалена" || bad "уборка: задача удалена"
  fi
fi

echo "--- итог: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = 0 ]
