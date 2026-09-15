// Планировщик показа кадров: гарантирует, что на экране оказывается только
// актуальный кадр в режиме прерывистой навигации и «догоняющий» поток кадров
// при удержании (chase). Чистая логика без DOM — покрыта
// scripts/test_frame_scheduler.mjs.

export interface BeginResult {
  gen: number;
  cached: boolean;
}

export interface PlayStep {
  pos: number;
  direction: 1 | -1;
  stop: boolean;
}

export class FrameScheduler {
  private currentGen = 0;
  private shown = -1;
  private wanted = -1;
  private cache = new Map<number, string>();
  private readonly cacheSize: number;

  constructor(cacheSize = 60) {
    this.cacheSize = cacheSize;
  }

  // Вызывается при каждом изменении целевой позиции: инвалидирует все
  // прежние загрузки (старые gen перестают быть «свежими»).
  begin(target: number): BeginResult {
    this.wanted = target;
    this.currentGen++;
    return { gen: this.currentGen, cached: this.cache.has(target) };
  }

  // onload (строгий режим — тапы, прыжки, воспроизведение): возвращает true,
  // если кадр — самый свежий и его нужно показать. Устаревшие не показываются.
  deliver(target: number, gen: number, src: string): boolean {
    if (gen !== this.currentGen) return false;
    this.cachePut(target, src);
    return target === this.wanted;
  }

  // onload (chase — удержание): кэширует и показывает кадр, если он
  // продвигает экран в направлении удержания dir. Поздние «обратные» ответы
  // не откатывают экран. Пропуски кадров при этом допускаются.
  deliverStreaming(target: number, src: string, dir: 1 | -1): boolean {
    this.cachePut(target, src);
    const advanced = dir * (target - this.shown) > 0;
    if (advanced) this.show(target);
    return advanced;
  }

  // Фактический показ кадра на экране.
  show(target: number): void {
    this.shown = target;
    this.wanted = target;
  }

  private cachePut(target: number, src: string): void {
    this.cache.set(target, src);
    if (this.cache.size > this.cacheSize) {
      const oldest = this.cache.keys().next().value;
      if (oldest !== undefined) this.cache.delete(oldest);
    }
  }

  get generation(): number {
    return this.currentGen;
  }

  get shownIndex(): number {
    return this.shown;
  }

  get wantedIndex(): number {
    return this.wanted;
  }

  hasCached(index: number): boolean {
    return this.cache.has(index);
  }
}

// Тип прерывистой навигации: «drain» — показ всех промежуточных кадров по пути
// к позиции (накопленные тапы), «jump» — сразу к цели, без промежуточных.
export type NavMode = "drain" | "jump";

// Какой кадр грузить следующим. В режиме drain при отставании экрана грузится
// следующий по пути кадр (shown+sign(position-shown)) — каждый кадр по пути
// будет показан. В режиме jump — сразу целевая позиция.
export function pickLoadTarget(a: {
  shown: number;
  position: number;
  mode: NavMode;
}): number {
  if (a.mode === "drain" && a.shown !== a.position) {
    return a.shown + Math.sign(a.position - a.shown);
  }
  return a.position;
}

// Позиция следующего кадра при воспроизведении. Логика границ сохранена
// как в оригинале: на стыках разворот + остановка.
export function nextPlayPosition(
  pos: number,
  direction: 1 | -1,
  total: number,
): PlayStep {
  const next = pos + direction;
  if (next < 0) return { pos: 0, direction: 1, stop: true };
  if (next >= total) return { pos: total - 1, direction: -1, stop: true };
  return { pos: next, direction, stop: false };
}

// Ближайшая граница сегмента в направлении движения (J — «проиграть до
// границы»). Границами считаются начала и концы фрагментов, а также края
// видео. Строгое сравнение: уже стоя на границе, J уводит к следующей.
export function segmentBoundaryForward(
  position: number,
  direction: 1 | -1,
  keyframes: number[],
  total: number,
): number {
  const ks = [0, ...keyframes.filter((k) => k > 0 && k < total - 1), total - 1];
  if (direction === 1) {
    return ks.find((f) => f > position) ?? total - 1;
  }
  const left = ks.filter((f) => f < position);
  return left.length ? left[left.length - 1] : 0;
}

// --- Маппинг таймлайна: кадр <-> пиксель полосы (используется статус-баром
// и кликом по нему). Единая пара, чтобы отрисовка и ввод не расходились. ---

// Кадр -> пиксель. Билинейная обратимая пара [0,total-1] -> [0,width-1]:
// с "-1" на обоих концах последний кадр доходит до правого края.
export function timelinePix(frame: number, width: number, total: number): number {
  return Math.round(frame * (width - 1) / Math.max(1, total - 1));
}

// Пиксель -> кадр (клик по полосе).
export function timelineFrame(pixel: number, width: number, total: number): number {
  return Math.round(pixel * Math.max(1, total - 1) / (width - 1));
}

// Старт тёмной зоны «после текущего кадра». Тот же timelinePix, что и границы
// сегментов, поэтому при реальном совпадении позиции с границей смена
// света/тени приходится на ту же колонку, что и смена цвета сегмента:
// - позиция == КОНЕЦ сегмента -> зона начинается со следующей колонки,
//   т.е. ровно на правой границе сегмента (последний пиксель сегмента яркий);
// - позиция == НАЧАЛО сегмента -> зона начинается с первого пикселя сегмента,
//   т.е. ровно на его левой границе;
// - внутри сегмента/вне границ -> зона «кадры после текущего», +1.
export function overlayStart(
  position: number,
  width: number,
  total: number,
  fragments: ReadonlyArray<{ start: number }>,
): number {
  const pix = timelinePix(position, width, total);
  const atStart = fragments.some((f) => f.start === position);
  return atStart ? pix : Math.min(width - 1, pix + 1);
}