// Прогноз выигрыша от поворота телефона в ландшафт.
//
// Подсказка «поверните телефон» честна только тогда, когда кадр в
// ландшафте действительно заметно крупнее. Раскладка ландшафта в
// styles.css (медиазапросы телефона) зеркалится здесь числами:
//  - --landscape-band: 81px — нижняя полоса транспорта и таймлайна;
//  - рейка разметки: 2×44 (108px), в низком ландшафте 4×44 (208px),
//    в совсем низком — плотнее (198px);
//  - отступ рейки 8px и зазор до кадра 12px.
// Меняешь раскладку в CSS — меняй и эти константы (и наоборот).

/** Во сколько раз кадр должен вырасти, чтобы жест поворота был оправдан. */
export const ROTATE_MIN_GAIN = 1.25;

const LANDSCAPE_BAND = 81;
const RAIL_LEFT = 8;
const RAIL_GAP = 12;

/** Ширина левой рейки разметки по высоте ландшафта. */
function landscapeRailWidth(landscapeHeight: number): number {
  if (landscapeHeight <= 300) return 198;
  if (landscapeHeight <= 350) return 208;
  return 108;
}

export interface RotatePrediction {
  /** Ширина нарисованного кадра в портрете, px. */
  portraitWidth: number;
  /** Прогноз ширины кадра после поворота, px. */
  landscapeWidth: number;
  /** landscapeWidth / portraitWidth; 0 — кадр ещё не измерен. */
  gain: number;
  /** Показывать ли подсказку о повороте. */
  worthRotating: boolean;
}

/**
 * Сравнивает нарисованную ширину кадра в портрете с прогнозом для
 * ландшафта той же раскладки. Пропорции берутся у самого кадра, а не
 * из метаданных пары: показывается то, что реально на экране.
 */
export function predictRotateGain(opts: {
  /** Пропорции кадра width / height. */
  frameRatio: number;
  /** Измеренная ширина кадра в портрете, px. */
  portraitFrameWidth: number;
  /** Ширина портретного вьюпорта, px. */
  viewportWidth: number;
  /** Высота портретного вьюпорта, px — она же ширина ландшафта. */
  viewportHeight: number;
}): RotatePrediction {
  const portraitWidth = Math.max(0, opts.portraitFrameWidth);
  const landscapeHeight = opts.viewportWidth;
  const landscapeWidth = opts.viewportHeight;
  const rail = landscapeRailWidth(landscapeHeight);
  const availableWidth = Math.max(
    0,
    landscapeWidth - RAIL_LEFT - rail - RAIL_GAP,
  );
  const availableHeight = Math.max(0, landscapeHeight - LANDSCAPE_BAND);
  const predicted = Math.min(availableWidth, availableHeight * opts.frameRatio);
  const gain =
    portraitWidth > 0 && Number.isFinite(predicted)
      ? predicted / portraitWidth
      : 0;
  return {
    portraitWidth,
    landscapeWidth: predicted,
    gain,
    worthRotating: gain >= ROTATE_MIN_GAIN,
  };
}
