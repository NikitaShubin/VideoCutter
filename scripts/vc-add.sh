#!/usr/bin/env bash
# Добавляет видеофайл в workspace (папку, видимую автономным VC).
#
# Использование:
#   ./scripts/vc-add.sh <путь_к_видео> [имя_workspace]
#
# Если имя не указано, берётся базовое имя файла без расширения.
# Workspace-папка создаётся в services/videocutter/workspaces/,
# видео копируется туда; для отображения в списке нажмите F5.
#
# Примеры:
#   ./scripts/vc-add.sh /path/to/video.mp4
#   ./scripts/vc-add.sh /path/to/video.mp4 my_project
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKSPACES_DIR="$SCRIPT_DIR/../workspaces"
WORKSPACES_DIR="$(cd "$WORKSPACES_DIR" 2>/dev/null && pwd || { echo "Ошибка: папка workspaces не найдена" >&2; exit 1; })"

if [ $# -lt 1 ]; then
  echo "Использование: $0 <путь_к_видео> [имя_workspace]"
  echo "  video.mp4          — скопирует видео и создаст workspace с именем 'video'."
  echo "  video.mp4 my_ws   — создаст workspace 'my_ws'."
  exit 1
fi

VIDEO_PATH="$1"
WS_NAME="${2:-$(basename "${VIDEO_PATH%.*}")}"

if [ ! -f "$VIDEO_PATH" ]; then
  echo "Ошибка: файл '$VIDEO_PATH' не найден." >&2
  exit 1
fi

WS_DIR="$WORKSPACES_DIR/$WS_NAME"
mkdir -p "$WS_DIR"

cp "$VIDEO_PATH" "$WS_DIR/"
VIDEO_FILENAME="$(basename "$VIDEO_PATH")"
echo "Готово: $VIDEO_FILENAME скопирован в workspace '$WS_NAME' ($WS_DIR)."
echo "Откройте http://localhost:3001/ и нажмите F5."
