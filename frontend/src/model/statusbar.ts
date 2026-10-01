// Отрисовка статус-бара (таймлина) в виде чистой функции: «рисуем» массив
// прямоугольников (ops), а paintStatusbar лишь переносит их на canvas.
//
// Воспроизводит draw_statusbar из PyVideoCutter: зелёный фон, красные
// фрагменты, затемнение после текущего кадра, выделенный диапазон добавляет
// синий канал, маркер текущего кадра — белая линия.
//
// Один источник правды и для редактора, и для превью в списке задач
// (перед список показывает статус-бар так, будто файл открыт: фрагменты +
// сохранённая позиция; keyPose в нём всегда null — выделение не сохраняется).

import { overlayStart, timelinePix } from "./frameScheduler.ts";

export type StatusbarScheme =
  | "timeline"
  | "upload"
  | "receipt"
  | "indexing";

/** Слоёный прогресс (наслоение фаз): фон — цвет завершённой предыдущей
 * фазы (или тёмный, если её не было), поверх — выполненная часть текущей
 * фазы своим цветом. Преемственность видна буквально: каждый этап
 * ложится поверх предыдущего, а не стирает его. */
export interface ProgressLayers {
  base: string;
  fill: string;
  frac: number;
}

export interface StatusbarState {
  width: number;
  height: number;
  totalFrames: number;
  fragments: ReadonlyArray<{ start: number; end: number }>;
  position: number;
  keyPose?: number | null;
  /** Палитра: timeline — состояние разметки (зелёный/красный, как в
   * оригинале); upload/receipt/indexing — стадии создания задачи
   * (синий/бирюза/фиолет: сразу видно, что это не разметка).
   * Остальные слои (затемнение, маркер) общие. */
  scheme?: StatusbarScheme;
  /** Режим прогресса: заменяет таймлайн целиком (фрагменты/keyPose
   * игнорируются). Доля clamp'ится в 0..1. */
  progress?: ProgressLayers;
}

// Цвета палитр: различается только фон (остаток затемняется сам,
// маркер белый во всех). Фрагментов вне timeline-режима не бывает.
const SCHEME_BG: Record<StatusbarScheme, string> = {
  timeline: "#00ff00",
  upload: "#0077ff",
  receipt: "#00bebe",
  indexing: "#8b5cf6",
};

// Одна операция отрисовки; op "lighter" включает globalCompositeOperation.
export interface StatusbarOp {
  fill: string;
  x: number;
  y: number;
  w: number;
  h: number;
  op?: "lighter";
}

function seg(endX: number, x: number): number {
  // Правая граница фрагмента включается, чтобы последний кадр доходил
  // до правого края canvas (как в оригинале).
  return Math.max(1, endX - x + 1);
}

export function statusbarOps(s: StatusbarState): StatusbarOp[] {
  const { width, height, totalFrames, fragments, position } = s;
  const ops: StatusbarOp[] = [];

  // Режим прогресса: слои фаз вместо таймлайна.
  if (s.progress) {
    const f = Math.min(1, Math.max(0, s.progress.frac));
    const x = Math.round(f * width);
    ops.push({ fill: s.progress.base, x: 0, y: 0, w: width, h: height });
    if (x > 0) {
      ops.push({ fill: s.progress.fill, x: 0, y: 0, w: x, h: height });
    }
    if (x < width) {
      ops.push({ fill: "rgba(0,0,0,0.5)", x, y: 0, w: width - x, h: height });
    }
    ops.push({
      fill: "#ffffff",
      x: Math.min(Math.max(x, 0), width - 1),
      y: 0, w: 1, h: height,
    });
    return ops;
  }

  // Фон — по палитре (зелёный timeline, стадии создания — свои цвета).
  ops.push({
    fill: SCHEME_BG[s.scheme ?? "timeline"],
    x: 0, y: 0, w: width, h: height,
  });

  // Фрагменты — красные (BGR (255,0,0)).
  ops.push(
    ...fragments.map((f) => {
      const x = timelinePix(f.start, width, totalFrames);
      return {
        fill: "#ff0000",
        x,
        y: 0,
        w: seg(timelinePix(f.end, width, totalFrames), x),
        h: height,
      };
    }),
  );

  // Затемнение «после текущего кадра» (sb[:, current_shift:, :] //= 2):
  // зона стартует на одну колонку правее позиции, поэтому пиксель текущего
  // кадра никогда не затемняется — у конца сегмента последний пиксель
  // остаётся полностью красным (нет «недокраса»).
  const curX = overlayStart(position, width, totalFrames);
  ops.push({ fill: "rgba(0,0,0,0.5)", x: curX, y: 0, w: width - curX, h: height });

  // Выбранный диапазон (key pose → position): добавляем синий канал = 255
  // с "lighter" (зелёный → циан, красный → пурпурный).
  if (s.keyPose !== null && s.keyPose !== undefined) {
    const [a, b] = [Math.min(s.keyPose, position), Math.max(s.keyPose, position)];
    ops.push({
      fill: "#0000ff",
      x: timelinePix(a, width, totalFrames),
      y: 0,
      w: timelinePix(b, width, totalFrames) - timelinePix(a, width, totalFrames),
      h: height,
      op: "lighter",
    });
  }

  // Маркер текущего кадра — тонкая белая линия на пикселе позиции (поверх
  // всех слоёв; тот же timelinePix, что и границы сегментов).
  ops.push({
    fill: "#ffffff",
    x: timelinePix(position, width, totalFrames),
    y: 0,
    w: 1,
    h: height,
  });

  return ops;
}

export function paintStatusbar(ctx: CanvasRenderingContext2D, s: StatusbarState): void {
  for (const op of statusbarOps(s)) {
    ctx.save();
    if (op.op) ctx.globalCompositeOperation = op.op;
    ctx.fillStyle = op.fill;
    ctx.fillRect(op.x, op.y, op.w, op.h);
    ctx.restore();
  }
}