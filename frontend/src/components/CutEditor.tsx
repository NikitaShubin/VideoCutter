import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type MutableRefObject,
} from "react";
import {
  annotationsUrl,
  cancelExport,
  deleteExport,
  downloadFile,
  frameUrl,
  getExportStatus,
  getPair,
  importAnnotations,
  replaceFragments,
  savePosition,
  setPairSettings,
  startExport,
  taskBackupUrl,
  withTokenQuery,
  workspaceNonce,
} from "../api";
import { formatNumber, formatPercent } from "../model/format";
import { FragmentModel } from "../model/fragmentModel";
import {
  predictRotateGain,
  type RotatePrediction,
} from "../model/rotateHint";
import { DataMenu } from "./DataMenu";
import { DiagnosticsMenu } from "./DiagnosticsMenu";
import { FragmentIcon } from "./FragmentIcon";
import { HelpModal } from "./HelpModal";
import { PlaybackMenu } from "./PlaybackMenu";
import { Popover } from "./Popover";
import { TimelineLegend } from "./TimelineLegend";
import { useKeyboardMode } from "./useKeyboardMode";
import { ViewMenu } from "./ViewMenu";
import {
  FrameScheduler,
  nextPlayPosition,
  retargetOnDirectionChange,
  segmentBoundaryForward,
  sweepStaleInflight,
  timelineFrame,
} from "../model/frameScheduler";
import { paintStatusbar } from "../model/statusbar";
import { failureReason } from "../model/errors";
import { type ExportItem, type ExportStatus, type VideoPairDetail } from "../types";

// Логический размер таймлайна. CSS задаёт высоту 36px; backing store
// умножается на devicePixelRatio в эффекте ниже.
const STATUS_W = 800;
const STATUS_H = 36;

// Ландшафт телефона: верхняя шапка лежит поверх кадра. Прячем её после
// бездействия, как в видеоплеере: кадр остаётся чистым, а движение/тап
// возвращают управление. Левую рейку разметки не трогаем — она вне кадра.
const LANDSCAPE_QUERY = "(orientation: landscape) and (max-height: 520px)";
const CONTROLS_IDLE_MS = 2500;

// Короткий портрет (320×568): обычный нижний кластер не помещается, и
// транспорт уезжает под сгиб. Компактная раскладка поднимает его над
// панелью разметки и не даёт таймлайну сжаться ниже пальцевого размера.
const COMPACT_PORTRAIT_QUERY =
  "(orientation: portrait) and (max-width: 360px) and (max-height: 640px)";

/** Закрывает верхний оверлей редактора. true — «назад» обработан. */
export type BackGuard = () => boolean;

/** title с клавиатурной подсказкой — только там, где клавиши доступны. */
function keyTitle(label: string, keys: string, keyboard: boolean): string {
  return keyboard ? `${label} (${keys})` : label;
}

/** Состояние последнего сохранения фрагментов: постоянный индикатор
 * в транспорте, а не одноразовый flash. */
type SaveState = "idle" | "saving" | "saved" | "error";

// Полноэкранный режим: стандартный API + WebKit-префиксы. На iPhone
// Element.requestFullscreen отсутствует вовсе — для него есть резервный
// режим (класс fullscreen-fallback), который держит редактор и кадр по
// всему вьюпорту без нативного API.
type FullscreenDocument = Document & {
  webkitFullscreenElement?: Element | null;
  webkitCurrentFullScreenElement?: Element | null;
  webkitExitFullscreen?: () => Promise<void> | void;
  webkitCancelFullScreen?: () => Promise<void> | void;
};

type FullscreenElement = HTMLElement & {
  webkitRequestFullscreen?: () => Promise<void> | void;
  webkitRequestFullScreen?: () => Promise<void> | void;
};

/** Элемент нативного полного экрана (стандарт или WebKit-префикс). */
function nativeFullscreenElement(): Element | null {
  const doc = document as FullscreenDocument;
  return (
    doc.fullscreenElement ??
    doc.webkitFullscreenElement ??
    doc.webkitCurrentFullScreenElement ??
    null
  );
}

/** Выход из нативного полного экрана любым доступным API. */
function exitNativeFullscreen(): void {
  const doc = document as FullscreenDocument;
  const fn =
    doc.exitFullscreen ?? doc.webkitExitFullscreen ?? doc.webkitCancelFullScreen;
  if (!fn) return;
  try {
    const result = fn.call(doc);
    if (result && typeof result.catch === "function") result.catch(() => {});
  } catch {
    // Уже вышли (или браузер запретил) — состояние синхронизирует событие.
  }
}

/**
 * Запрос нативного полного экрана. false — API нет, нужен резервный режим.
 * Отказ промиса (нет жеста, запрет политики) тоже уводит в резерв: кнопка
 * не должна молча ничего не делать.
 */
function requestNativeFullscreen(
  element: HTMLElement,
  onFail: () => void,
): boolean {
  const node = element as FullscreenElement;
  const fn =
    node.requestFullscreen ??
    node.webkitRequestFullscreen ??
    node.webkitRequestFullScreen;
  if (!fn) return false;
  try {
    const result = fn.call(node);
    if (result && typeof result.catch === "function") result.catch(onFail);
  } catch {
    onFail();
  }
  return true;
}

interface Props {
  pairId: string;
  onBack: () => void;
  /** Общий с App обработчик системной кнопки «Назад». */
  backGuardRef?: MutableRefObject<BackGuard | null>;
}

export function CutEditor({ pairId, onBack, backGuardRef }: Props) {
  const [pair, setPair] = useState<VideoPairDetail | null>(null);
  const [position, setPosition] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [direction, setDirection] = useState<1 | -1>(1);
  const [keyPose, setKeyPose] = useState<number | null>(null);
  const [preserveAspect, setPreserveAspect] = useState(true);
  const [isFullscreen, setIsFullscreen] = useState(false);
  // Резервный полный экран: браузер без Element.requestFullscreen (iPhone).
  const [isFallbackFullscreen, setIsFallbackFullscreen] = useState(false);
  // Физическая клавиатура: шорткаты — основной путь. Панель разметки
  // остаётся только на сенсорном экране.
  const keyboard = useKeyboardMode();
  const [exportItems, setExportItems] = useState<ExportItem[] | null>(null);
  const exportItemsRef = useRef<ExportItem[] | null>(null);
  exportItemsRef.current = exportItems;
  const [waitingIndex, setWaitingIndex] = useState(false);
  const [waitPct, setWaitPct] = useState<number | null>(null);
  const [exporting, setExporting] = useState(false);
  const [exportProgress, setExportProgress] = useState<number | null>(null);
  // Импорт разметки: заливка архива с прогрессом (видео не трогается).
  const [annBusy, setAnnBusy] = useState(false);
  const [annProgress, setAnnProgress] = useState<number | null>(null);
  // Скачивание разметки/бэкапа: спиннер на сборке, затем %.
  const [dlState, setDlState] = useState<{
    key: "ann" | "bak";
    frac: number | null;
  } | null>(null);
  const [message, setMessage] = useState<{
    text: string;
    kind: "success" | "error";
    /** Повтор неудавшегося действия прямо из сообщения об ошибке. */
    retry?: () => void;
  } | null>(null);
  const [loadError, setLoadError] = useState("");
  // Настройки просмотра: масштаб (0.05..1.0) и качество JPEG (30..95).
  const [scale, setScale] = useState(0.75);
  const [quality, setQuality] = useState(78);
  // Применяемые параметры просмотра: обновляются с debounce после
  // ползунков. Иначе каждый тик слайдера — новый ключ кэша (GOP, q, s),
  // полный передекод групп и убийство хитрейта: шкала 30–95 даёт до
  // 65 вариантов на GOP. Ползунок двигается мгновенно, кадры — только
  // по применённым (см. эффект ниже).
  const [viewScale, setViewScale] = useState(0.75);
  const [viewQuality, setViewQuality] = useState(78);
  const [editingComment, setEditingComment] = useState(false);
  const editingCommentRef = useRef(false);
  editingCommentRef.current = editingComment;
  // Фрагмент, у которого открыт черновик: позиция может измениться
  // прокруткой/таймлайном, но комментарий обязан остаться у него.
  const commentStartRef = useRef<number | null>(null);
  const [commentDraft, setCommentDraft] = useState("");
  const [commentSavedFor, setCommentSavedFor] = useState<number | null>(null);
  const [saveState, setSaveState] = useState<SaveState>("idle");
  // Поле номера кадра: null — значение привязано к позиции (обновляется
  // при тапах/прыжках/проигрывании); строка — черновик ручного ввода
  // (пишется только под фокусом). Коммит — Enter или клик мимо поля,
  // Esc — откат черновика.
  const [frameDraft, setFrameDraft] = useState<string | null>(null);
  const [helpOpen, setHelpOpen] = useState(false);
  const helpOpenRef = useRef(false);
  helpOpenRef.current = helpOpen;

  // Верх редактора: открыто максимум одно меню. Пока оно открыто,
  // глобальные горячие клавиши не срабатывают — иначе «D» в меню
  // «Вид» удалила бы фрагмент, а Escape увёл бы из редактора.
  type EditorMenu =
    | "view"
    | "exchange"
    | "diagnostics"
    | "more"
    | "playback"
    | null;
  type MobileMenuTab = "view" | "exchange" | "diagnostics";
  const [openMenu, setOpenMenu] = useState<EditorMenu>(null);
  const [mobileMenuTab, setMobileMenuTab] = useState<MobileMenuTab>("view");
  const openMenuRef = useRef<EditorMenu>(null);
  openMenuRef.current = openMenu;

  // Смена устройства меняет доступный набор меню. Меню, которого на новом
  // устройстве нет, закрываем: иначе openMenu навсегда выключит глобальные
  // клавиши, хотя закрывать уже нечего.
  useEffect(() => {
    if (openMenu === null || openMenu === "playback") return;
    const available = keyboard ? openMenu !== "more" : openMenu === "more";
    if (!available) setOpenMenu(null);
  }, [keyboard, openMenu]);

  // Автоскрытие шапки в ландшафте: состояние выводится из бездействия,
  // а не хранится как «режим». Пока открыт оверлей или фокус в шапке,
  // таймер не прячет управление.
  const [landscape, setLandscape] = useState(false);
  const [compactPortrait, setCompactPortrait] = useState(false);
  const [controlsHidden, setControlsHidden] = useState(false);
  const editorTopRef = useRef<HTMLElement | null>(null);
  // Тап, которым пользователь возвращает скрытую шапку, не должен
  // доходить до кадра: иначе «покажи кнопки» заодно переключает видео.
  const controlsHiddenRef = useRef(false);
  controlsHiddenRef.current = controlsHidden;
  const wakeTapRef = useRef(false);
  // Подписи действий нужны и на телефоне в портрете: значок «фрагмент с
  // уголками» не объясняет, что именно произойдёт. В низком ландшафте
  // подписи отнимают ширину у плавающей рейки 44×44 — там остаются значки.
  const showWorkbenchLabels = keyboard || !landscape;

  useEffect(() => {
    if (
      typeof window === "undefined" ||
      typeof window.matchMedia !== "function"
    ) {
      return;
    }
    const landscapeMq = window.matchMedia(LANDSCAPE_QUERY);
    const compactMq = window.matchMedia(COMPACT_PORTRAIT_QUERY);
    const sync = () => {
      setLandscape(landscapeMq.matches);
      setCompactPortrait(compactMq.matches);
    };
    sync();
    landscapeMq.addEventListener?.("change", sync);
    compactMq.addEventListener?.("change", sync);
    return () => {
      landscapeMq.removeEventListener?.("change", sync);
      compactMq.removeEventListener?.("change", sync);
    };
  }, []);

  useEffect(() => {
    const pinned =
      openMenu !== null || helpOpen || editingComment || isFullscreen;
    if (!landscape || pinned) {
      setControlsHidden(false);
      return;
    }
    let timer: number | null = null;
    let wakeTapTimer: number | null = null;
    const schedule = () => {
      if (timer !== null) window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        timer = null;
        const active = document.activeElement;
        if (
          active instanceof HTMLElement &&
          editorTopRef.current?.contains(active)
        ) {
          return;
        }
        setControlsHidden(true);
      }, CONTROLS_IDLE_MS);
    };
    const wake = (event: Event) => {
      if (event.type === "pointerdown" && controlsHiddenRef.current) {
        // Тот же тап, который разбудил шапку, не должен нажать кадр.
        wakeTapRef.current = true;
        if (wakeTapTimer !== null) window.clearTimeout(wakeTapTimer);
        wakeTapTimer = window.setTimeout(() => {
          wakeTapRef.current = false;
          wakeTapTimer = null;
        }, 400);
      }
      setControlsHidden(false);
      schedule();
    };
    const swallowWakeTap = (event: MouseEvent) => {
      if (!wakeTapRef.current) return;
      wakeTapRef.current = false;
      event.preventDefault();
      event.stopPropagation();
    };
    schedule();
    window.addEventListener("click", swallowWakeTap, true);
    window.addEventListener("pointermove", wake, { passive: true });
    window.addEventListener("pointerdown", wake, { passive: true });
    window.addEventListener("keydown", wake, true);
    window.addEventListener("focusin", wake, true);
    window.addEventListener("focusout", wake, true);
    return () => {
      if (timer !== null) window.clearTimeout(timer);
      if (wakeTapTimer !== null) window.clearTimeout(wakeTapTimer);
      window.removeEventListener("click", swallowWakeTap, true);
      window.removeEventListener("pointermove", wake);
      window.removeEventListener("pointerdown", wake);
      window.removeEventListener("keydown", wake, true);
      window.removeEventListener("focusin", wake, true);
      window.removeEventListener("focusout", wake, true);
    };
  }, [landscape, openMenu, helpOpen, editingComment, isFullscreen]);

  // Системная «Назад» (Android/браузер) сначала закрывает верхний оверлей:
  // справку, меню, список экспорта или комментарий. Только потом App
  // возвращает к списку задач.
  useEffect(() => {
    if (!backGuardRef) return;
    backGuardRef.current = () => {
      if (helpOpen) {
        setHelpOpen(false);
        return true;
      }
      if (openMenu) {
        setOpenMenu(null);
        return true;
      }
      if (exportItems) {
        setExportItems(null);
        return true;
      }
      if (editingComment) {
        setEditingComment(false);
        return true;
      }
      if (fullscreenRef.current || nativeFullscreenElement()) {
        if (nativeFullscreenElement()) {
          exitNativeFullscreen();
        } else {
          fallbackFullscreenRef.current = false;
          fullscreenRef.current = false;
          setIsFallbackFullscreen(false);
          setIsFullscreen(false);
        }
        return true;
      }
      return false;
    };
    return () => {
      backGuardRef.current = null;
    };
  }, [backGuardRef, helpOpen, openMenu, exportItems, editingComment]);

  // Состояние экспорта дублируем в ref: keydown-замыкание несвежее
  // (exporting нет в deps эффекта), а повторный запуск/отмена идут через E.
  const exportingRef = useRef(false);
  const pollTimerRef = useRef<number | null>(null);

  const modelRef = useRef<FragmentModel | null>(null);
  if (!modelRef.current) modelRef.current = new FragmentModel(0);
  const [, forceRender] = useState(0);
  const rerender = () => forceRender((n) => n + 1);

  const statusRef = useRef<HTMLCanvasElement | null>(null);
  const imageRef = useRef<HTMLImageElement | null>(null);
  const editorRef = useRef<HTMLDivElement | null>(null);
  const fullscreenExitRef = useRef<HTMLButtonElement | null>(null);
  const wasFullscreenRef = useRef(false);
  // keydown/back-guard замыкания несвежие (deps пустые) — режим читаем
  // из ref, чтобы Esc и «Назад» видели текущее состояние.
  const fullscreenRef = useRef(false);
  const fallbackFullscreenRef = useRef(false);

  // Синхронизация состояния с полноэкранным режимом браузера (вкл/выкл через
  // Esc и системную кнопку). Слушаем и WebKit-префикс: старые Safari не шлют
  // стандартное событие.
  useEffect(() => {
    const onChange = () => {
      const native = nativeFullscreenElement() === editorRef.current;
      if (native) {
        fallbackFullscreenRef.current = false;
        fullscreenRef.current = true;
        setIsFallbackFullscreen(false);
        setIsFullscreen(true);
      } else if (!fallbackFullscreenRef.current) {
        fullscreenRef.current = false;
        setIsFullscreen(false);
      }
    };
    document.addEventListener("fullscreenchange", onChange);
    document.addEventListener("webkitfullscreenchange", onChange);
    return () => {
      document.removeEventListener("fullscreenchange", onChange);
      document.removeEventListener("webkitfullscreenchange", onChange);
    };
  }, []);

  // В полном экране шапка исчезает: фокус ведём на видимую кнопку выхода,
  // а после выхода возвращаем в шапку — иначе он проваливается в body.
  useEffect(() => {
    if (isFullscreen) {
      wasFullscreenRef.current = true;
      fullscreenExitRef.current?.focus();
    } else if (wasFullscreenRef.current) {
      wasFullscreenRef.current = false;
      editorRef.current
        ?.querySelector<HTMLButtonElement>(".editor-back")
        ?.focus();
    }
  }, [isFullscreen]);

  const enterFallbackFullscreen = () => {
    fallbackFullscreenRef.current = true;
    fullscreenRef.current = true;
    setIsFallbackFullscreen(true);
    setIsFullscreen(true);
  };

  const exitFullscreenMode = () => {
    if (nativeFullscreenElement()) {
      exitNativeFullscreen();
      return;
    }
    fallbackFullscreenRef.current = false;
    fullscreenRef.current = false;
    setIsFallbackFullscreen(false);
    setIsFullscreen(false);
  };

  const toggleFullscreen = () => {
    if (fullscreenRef.current || nativeFullscreenElement()) {
      exitFullscreenMode();
      return;
    }
    const el = editorRef.current;
    if (el && requestNativeFullscreen(el, enterFallbackFullscreen)) return;
    enterFallbackFullscreen();
  };

  // Клиентский кэш кадров + предзагрузка: кадр подставляется в <img> только
  // после загрузки, поэтому пустой/чёрный экран при перемотке не моргает.
  //
  // Режимы показа (номер/полоса всегда обновляются мгновенно):
  //  - тапы и прыжки (таймлайн, PageUp/Down, Home/End, одиночный Ctrl+→):
  //    сразу целевой кадр, без промежуточных (серия быстрых тапов — тоже
  //    прыжок, к позиции последнего тапа);
  //  - удержание стрелки (e.repeat, в т.ч. Ctrl+) → chase: позиция бежит,
  //    экран догоняет, дропая кадры, с маркером до догона. Переход на
  //    удержание отменяет незаконченные запросы тапов (bumpEpoch).
  const MAX_CACHE = 60;
  const SEEK_STEP = 10;
  const CHASE_CAP = 6;
  // Тик pump: свип протухших запросов и повтор попытки загрузки — экран
  // не «залипает», даже если ответ завис (медленный сервер, длинный GOP).
  const PUMP_TICK_MS = 1200;
  // Столько неудачных попыток подряд ждём по одному кадру, прежде чем
  // прекратить долбёжку до нового нав-действия (bumpEpoch сбрасывает).
  const FRAME_FAIL_MAX = 3;
  const schedRef = useRef<FrameScheduler | null>(null);
  if (!schedRef.current) schedRef.current = new FrameScheduler(MAX_CACHE);
  const [shownFrame, setShownFrame] = useState(-1);
  // Прогноз «поворот даст крупнее»: подсказка показывается только тогда,
  // когда ландшафт реально шире портретного кадра (см. model/rotateHint).
  const [viewport, setViewport] = useState(() => ({
    w: typeof window === "undefined" ? 0 : window.innerWidth,
    h: typeof window === "undefined" ? 0 : window.innerHeight,
  }));
  const [rotateGain, setRotateGain] = useState<RotatePrediction | null>(null);
  useEffect(() => {
    const sync = () =>
      setViewport({ w: window.innerWidth, h: window.innerHeight });
    window.addEventListener("resize", sync);
    return () => window.removeEventListener("resize", sync);
  }, []);
  const frameVisible = shownFrame >= 0;
  useLayoutEffect(() => {
    // Портрет телефона и не полноэкранный режим — единственная ситуация,
    // где совет «поверните» вообще уместен.
    if (
      keyboard ||
      isFullscreen ||
      viewport.w <= 0 ||
      viewport.h <= 0 ||
      viewport.w >= viewport.h ||
      viewport.w > 640
    ) {
      setRotateGain(null);
      return;
    }
    const img = imageRef.current;
    const rect = img?.getBoundingClientRect();
    const natW = img?.naturalWidth ?? 0;
    const natH = img?.naturalHeight ?? 0;
    // Пропорции — у нарисованного кадра: метаданные пары могут врать
    // (повёрнутая запись), а object-fit считает по натуральным размерам.
    const ratio =
      natW > 0 && natH > 0
        ? natW / natH
        : pair && pair.width > 0 && pair.height > 0
          ? pair.width / pair.height
          : 0;
    if (!rect || rect.width <= 0 || rect.height <= 0 || ratio <= 0) {
      setRotateGain(null);
      return;
    }
    const portraitFrameWidth = preserveAspect
      ? Math.min(rect.width, rect.height * ratio)
      : rect.width;
    setRotateGain(
      predictRotateGain({
        frameRatio: ratio,
        portraitFrameWidth,
        viewportWidth: viewport.w,
        viewportHeight: viewport.h,
      }),
    );
  }, [keyboard, isFullscreen, viewport, frameVisible, preserveAspect, pair]);
  const chaseRef = useRef(false);
  const heldDirRef = useRef<1 | -1>(1);
  const epochRef = useRef(0);
  const inflightRef = useRef<Set<number>>(new Set());
  // Счётчик неудачных попыток загрузки кадра (по индексу кадра).
  const failsRef = useRef<Map<number, number>>(new Map());
  // Истина, когда терпели неудачи до предела: показываем сообщение о
  // застрявшем кадре и не долбим сервер до нового нав-действия.
  const [frameStuck, setFrameStuck] = useState(false);
  const rightHeldRef = useRef(false);
  const leftHeldRef = useRef(false);
  // Замер клиента (диагностика тормозов, поведения не меняет): load — мс
  // от постановки src до onload (сеть+сервер+декод браузера), show — мс от
  // onload до отрисовки (рендер), fps — показанных кадров в секунду.
  // Агрегация раз в секунду в perf (ниже), чтобы не дёргать рендер на кадр.
  const issueT0Ref = useRef(new Map<number, number>());
  const perfAccumRef = useRef({
    winT0: performance.now(), shown: 0, n: 0, loadSum: 0, showSum: 0,
    shownN: 0,
  });
  const [perf, setPerf] = useState({ fps: 0, load: 0, show: 0 });

  // Загрузка пары + фрагментов.
  const loadPairInto = useCallback((p: VideoPairDetail) => {
    if (!p || !Array.isArray(p.fragments) || typeof p.total_frames !== "number") {
      setLoadError("Задача не найдена или ответ сервера неполный.");
      return;
    }
    if (p.broken) {
      setLoadError(p.error || "Задача битая.");
      return;
    }
    if (p.indexing) {
      // Гонка: открыли раньше готовности (кнопка списка обычно блокирует,
      // но создание ждёт опросом) — ждём здесь, не падаем.
      setWaitingIndex(true);
      setWaitPct(p.indexing_progress ?? null);
      return;
    }
    setWaitingIndex(false);
    setLoadError("");
    setPair(p);
    setScale(p.scale ?? 0.75);
    setQuality(p.quality ?? 78);
    setViewScale(p.scale ?? 0.75);
    setViewQuality(p.quality ?? 78);
    // Возвращаемся на кадр, где пользователь завершил редактирование
    // (сохранён в fragments.tsv); при отсутствии/выходе за диапазон — 0.
    const saved = p.total_frames > 0
      ? Math.min(p.total_frames - 1, Math.max(0, p.position ?? 0))
      : 0;
    setPosition(saved);
    schedRef.current = new FrameScheduler(MAX_CACHE);
    setShownFrame(-1);
    chaseRef.current = false;
    bumpEpoch();
    rightHeldRef.current = false;
    leftHeldRef.current = false;
    modelRef.current = new FragmentModel(p.total_frames);
    modelRef.current.setInitial(
      p.fragments.map((f) => ({ start: f.start, end: f.end, comment: f.comment ?? "" })),
    );
    rerender();
  }, []);

  useEffect(() => {
    getPair(pairId)
      .then(loadPairInto)
      .catch((e: Error) => setLoadError(e.message));
  }, [pairId, loadPairInto]);

  // Ожидание готовности индекса (редкая гонка открытия): опрос detail,
  // по готовности — обычная загрузка, битая — текст ошибки.
  useEffect(() => {
    if (!waitingIndex) return;
    let alive = true;
    const id = window.setInterval(() => {
      getPair(pairId)
        .then((p) => {
          if (!alive) return;
          if (p.broken) {
            setWaitingIndex(false);
            setLoadError(p.error || "Задача битая.");
            return;
          }
          setWaitPct(p.indexing_progress ?? null);
          if (!p.indexing) {
            setWaitingIndex(false);
            loadPairInto(p);
          }
        })
        .catch((e: Error) => {
          if (!alive) return;
          setWaitingIndex(false);
          setLoadError(e.message);
        });
    }, 2000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [waitingIndex, pairId, loadPairInto]);

  // Воспроизведение — flat-out: следующий шаг сразу после показа текущего,
  // без искусственных пауз. Темп задаёт только железо (декод/сеть/рендер):
  // скоростное ревью идёт на максимальной скорости, замедления до 1x нет.
  // speed (1/2/4/… с клавиш 0–9) — stride: сколько кадров проходить за шаг
  // (прореживание для скоростного ревью). Показ — все кадры подряд,
  // без пропусков (кроме явного stride). Покадровая точность — везде.
  const totalFrames = pair?.total_frames ?? 0;
  // Цель J-воспроизведения («до границы»): стоп на ней; на краю видео (0/последний
  // кадр) направление разворачивается, как при обычном проигрывании пробелом.
  const playTargetRef = useRef<number | null>(null);
  useEffect(() => {
    if (!playing) return;
    if (shownFrame !== position) return; // ждём, пока текущий кадр встанет в <img>
    const id = window.setTimeout(() => {
      // Симуляция stride шагов от текущей позиции: J-цель и края — точно.
      let pos = position;
      let done = false;
      for (let k = 0; k < Math.max(1, speed) && !done; k++) {
        const target = playTargetRef.current;
        if (target !== null && pos + direction === target) {
          // J: доехали до ближайшей границы — стоп точно на ней. На краю видео
          // разворот направления, как у обычного воспроизведения (пробел): иначе
          // после J до последнего/первого кадра следующий пуск шёл бы «в стену».
          playTargetRef.current = null;
          setPosition(target);
          if (target === 0 || target === totalFrames - 1) {
            setDirection(direction === 1 ? -1 : 1);
          }
          setPlaying(false);
          done = true;
          break;
        }
        const step = nextPlayPosition(pos, direction, totalFrames);
        if (step.stop) {
          // Дошли до начала/конца: разворачиваемся на воспроизведение
          // в обратную сторону, иначе пробел после остановки не запустит видео.
          // J-прогон завершился на краю — устаревшая цель не должна всплывать
          // при последующем воспроизведении.
          playTargetRef.current = null;
          setDirection(step.direction);
          setPlaying(false);
          done = true;
          break;
        }
        pos = step.pos;
      }
      if (!done && pos !== position) setPosition(pos);
    }, 0);
    return () => window.clearTimeout(id);
  }, [playing, position, shownFrame, direction, speed, totalFrames]);

  // Статус-бар (таймлайн). Воспроизводит draw_statusbar из PyVideoCutter:
  // зелёный фон, красные фрагменты, затемнение от позиции до конца,
  // выбранный диапазон добавляет синий канал (globalCompositeOperation).
  // Отрисовка вынесена в чистый model/statusbar.ts (общий с превью списка).
  //
  // Канва рисуется в логических 800×36 и масштабируется под DPR: на
  // Retina/HiDPI полоса остаётся резкой, а не мыльной.
  const drawStatusbar = useCallback(() => {
    const canvas = statusRef.current;
    const p = modelRef.current;
    if (!canvas || !p) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const dpr = Math.max(1, window.devicePixelRatio || 1);
    const width = canvas.width / dpr;
    const height = canvas.height / dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    paintStatusbar(ctx, {
      width,
      height,
      totalFrames: p.totalFrames,
      fragments: p.fragments,
      position,
      keyPose,
    });
  }, [keyPose, position]);

  useEffect(() => {
    const canvas = statusRef.current;
    if (!canvas) return;
    const dpr = Math.max(1, window.devicePixelRatio || 1);
    const width = Math.round(STATUS_W * dpr);
    const height = Math.round(STATUS_H * dpr);
    if (canvas.width !== width) canvas.width = width;
    if (canvas.height !== height) canvas.height = height;
  }, [pair]);

  useEffect(() => {
    drawStatusbar();
  }, [drawStatusbar, position, keyPose, pair]);

  // Показ кадра: ставим src, помечаем кадр как показанный (state — чтобы
  // эффекты и индикатор загрузки реагировали на появление кадра).
  const ver = pair ? `${pair.video_ver}:${workspaceNonce(pairId)}` : undefined;
  const showFrame = useCallback(
    (idx: number) => {
      if (!pair) return;
      const el = imageRef.current;
      if (el) el.src = frameUrl(pair.id, idx, "visualization", ver, viewScale, viewQuality);
      schedRef.current?.show(idx);
      setShownFrame(idx);
    },
    [pair, ver, viewScale, viewQuality],
  );

  // Единая точка загрузки/показа кадра. Вызывается реактивно (смена позиции
  // или показанного кадра) и по тику таймера (ниже). Строгий режим:
  // грузится и показывается только актуальная позиция — серия тапов и
  // прыжки дают целевой кадр без промежуточных; удержание — отдельный
  // стрим (chase).
  const pump = useCallback(() => {
    const sched = schedRef.current;
    if (!pair || !sched) return;
    const chase = chaseRef.current;
    const pos = position;
    const shown = shownFrame;
    if (pos === shown) return;

    // Протухшие загрузки (ответа нет дольше таймаута): снимаем блок,
    // pump перевыпустит запрос. Иначе забитый cap + дедупликация
    // вешают экран до прихода самого медленного ответа (минуты).
    sweepStaleInflight(
      inflightRef.current, issueT0Ref.current, performance.now());

    const target = pos;
    if (target === shown) return;

    // Предельное число неудач по кадру — крест на попытках: не долбим
    // сервер до нового нав-действия (bumpEpoch чистит счётчик).
    if ((failsRef.current.get(target) ?? 0) >= FRAME_FAIL_MAX) return;

    // Дедупликация: кадр уже грузится.
    const inflight = inflightRef.current;
    if (inflight.has(target)) return;
    // Chase: ограничиваем поток одновременных запросов — остальные кадры
    // «пропускаются» (следующее изменение позиции запросит новее). Без
    // колпачка скраб по большому файлу ставит сотни декодов в очередь.
    if (chase && inflight.size >= CHASE_CAP) return;

    const { gen, cached } = sched.begin(target);
    if (cached) {
      showFrame(target);
      return;
    }

    const myEpoch = epochRef.current;
    const myChase = chase;
    inflight.add(target);
    const img = new Image();
    issueT0Ref.current.set(target, performance.now());
    const acc = perfAccumRef.current;
    img.onload = () => {
      const tLoad = performance.now();
      const t0 = issueT0Ref.current.get(target) ?? tLoad;
      issueT0Ref.current.delete(target);
      acc.n++;
      acc.loadSum += tLoad - t0;
      inflight.delete(target);
      // Режим сменился с момента запроса (chase/буфер тапов брошен, прыжок) —
      // ответ устарел, не показываем.
      if (myEpoch !== epochRef.current) return;
      let shownIt = false;
      if (myChase) {
        if (sched.deliverStreaming(target, img.src, heldDirRef.current)) {
          showFrame(target);
          shownIt = true;
        }
      } else if (sched.deliver(target, gen, img.src)) {
        showFrame(target);
        shownIt = true;
      }
      if (shownIt) {
        // Кадр показан: счётчик неудач по нему отработал.
        failsRef.current.delete(target);
        setFrameStuck(false);
        acc.shown++;
        acc.shownN++;
        requestAnimationFrame(() => {
          acc.showSum += performance.now() - tLoad;
        });
      }
    };
    img.onerror = () => {
      inflight.delete(target);
      issueT0Ref.current.delete(target);
      if (myEpoch !== epochRef.current) return;
      // Кадр не загрузился: кольцо остаётся, повтор — по тику pump.
      // Подменять показ (setShownFrame) нельзя: «фейковый» кадр убирает
      // кольцо, пока кадр фактически не загружен.
      const fails = (failsRef.current.get(target) ?? 0) + 1;
      failsRef.current.set(target, fails);
      if (fails >= FRAME_FAIL_MAX) setFrameStuck(true);
    };
    img.src = frameUrl(pair.id, target, "visualization", ver, viewScale, viewQuality);
  }, [pair, position, shownFrame, showFrame, viewScale, viewQuality]);

  const pumpRef = useRef<() => void>(() => {});
  pumpRef.current = pump;

  useEffect(() => {
    pump();
  }, [pump]);

  // Тик pump: свип протухших запросов и повтор попытки. Без него зависший
  // ответ (медленный сервер, сборка индекса) висит на экране навсегда:
  // свип и повтор работают только пока pump кто-то зовёт.
  useEffect(() => {
    const id = window.setInterval(() => {
      pumpRef.current();
    }, PUMP_TICK_MS);
    return () => window.clearInterval(id);
  }, []);

  // Агрегатор клиентского замера: раз в секунду сворачиваем аккумулятор
  // в perf (один рендер в секунду вместо рендера на кадр).
  useEffect(() => {
    const id = window.setInterval(() => {
      const r = perfAccumRef.current;
      const now = performance.now();
      const dt = (now - r.winT0) / 1000;
      setPerf({
        fps: dt > 0 ? Math.round(r.shown / dt) : 0,
        load: r.n ? Math.round(r.loadSum / r.n) : 0,
        show: r.shownN ? Math.round(r.showSum / r.shownN) : 0,
      });
      r.shown = 0;
      r.n = 0;
      r.loadSum = 0;
      r.showSum = 0;
      r.shownN = 0;
      r.winT0 = now;
    }, 1000);
    return () => window.clearInterval(id);
  }, []);

  // Ползунки качества/масштаба: сохраняем на сервер с debounce и только
  // тогда же ПРИМЕНЯЕМ к просмотру (viewScale/viewQuality). Иначе каждый
  // тик слайдера плодил бы ключи кэша (GOP, q, s) с полным передекодом.
  // In-flight запросы старых вариантов убиваем эпохой (их onload — в пустоту),
  // планировщик сбрасываем, текущий кадр перечитываем уже новым эффектом
  // (pump пересоздаётся от view*-зависимостей).
  useEffect(() => {
    if (!pair) return;
    const id = window.setTimeout(() => {
      setPairSettings(pairId, quality, scale).catch(() => {});
      setViewScale(scale);
      setViewQuality(quality);
      bumpEpoch();
      schedRef.current = new FrameScheduler(MAX_CACHE);
      setShownFrame(-1);
    }, 400);
    return () => window.clearTimeout(id);
  }, [pair, pairId, quality, scale]);

  // --- Режимы навигации (удержание vs тапы/прыжки) ---
  // Любое новое нав-действие сбрасывает накопленное: старые ответы — в
  // пустоту, счётчик неудач и сообщение о застрявшем кадре — в ноль.
  const bumpEpoch = useCallback(() => {
    epochRef.current++;
    inflightRef.current.clear();
    failsRef.current.clear();
    setFrameStuck(false);
  }, []);

  // Прыжок/тап: сразу целевой кадр, незаконченные запросы отменяются.
  // pump здесь НЕ зовётся: все вызовы jumpMode идут парой с setPosition,
  // который сам запускает pump с уже новой позицией (вызов с протухшей
  // позицией пере-запрашивал бы тот же кадр — двойной HTTP на каждый тап).
  const jumpMode = () => {
    chaseRef.current = false;
    bumpEpoch();
  };

  // Переход по таймлайну (клик и клавиатура): всегда прыжок, без
  // промежуточных кадров; позиция клампится в границы задачи.
  const seekTo = (frame: number) => {
    const p = modelRef.current;
    // Пока открыт комментарий, кадр заморожен: иначе черновик при
    // сохранении уедет к другому фрагменту.
    if (!p || editingCommentRef.current) return;
    setPlaying(false);
    jumpMode();
    setPosition(Math.min(p.totalFrames - 1, Math.max(0, frame)));
  };

  // Удержание: real-time стрим с дропом, незаконченные запросы тапов
  // отбрасываются сразу (первый repeat-тек по кнопке).
  const startChase = (dir: 1 | -1) => {
    if (!chaseRef.current) {
      chaseRef.current = true;
      bumpEpoch(); // хвост после отпускания — строгая докачка
    }
    heldDirRef.current = dir;
    pumpRef.current();
  };

  // Отпускание: строгая докачка финального кадра до текущей позиции.
  const endChase = () => {
    if (!chaseRef.current) return;
    chaseRef.current = false;
    bumpEpoch();
    pumpRef.current();
  };

  // Отмена незаконченных запросов (пробел — переход к воспроизведению).
  const cancelPending = () => {
    bumpEpoch();
    // Новое обычное воспроизведение не должно упираться в протухшую цель J.
    playTargetRef.current = null;
    pumpRef.current();
  };

  const togglePlayback = () => {
    if (editingCommentRef.current) return;
    cancelPending();
    setPlaying((v) => !v);
  };

  const jump = (dir: 1 | -1) => {
    const p = modelRef.current;
    if (!p || editingCommentRef.current) return;
    jumpMode(); // прыжок без промежуточных кадров
    const ks = [0, ...p.keyFrames(), p.totalFrames - 1];
    if (dir === 1) {
      for (const f of ks) {
        if (f > position) {
          setPosition(f);
          return;
        }
      }
      setPosition(ks[ks.length - 1]);
    } else {
      for (let i = ks.length - 1; i >= 0; i--) {
        if (ks[i] < position) {
          setPosition(ks[i]);
          return;
        }
      }
      setPosition(ks[0]);
    }
  };

  // J: непрерывное воспроизведение от текущей позиции до ближайшей границы
  // сегмента (начало/конец фрагмента, края видео) в текущем направлении.
  const playToBoundary = () => {
    const p = modelRef.current;
    if (!p) return;
    cancelPending();
    playTargetRef.current = segmentBoundaryForward(
      position,
      direction,
      p.keyFrames(),
      p.totalFrames,
    );
    setPlaying(true);
  };

  const flashTimerRef = useRef<number | null>(null);
  const flash = (
    text: string,
    kind: "success" | "error" = "success",
    retry?: () => void,
  ) => {
    if (flashTimerRef.current !== null) {
      window.clearTimeout(flashTimerRef.current);
      flashTimerRef.current = null;
    }
    setMessage({ text, kind, retry });
    // Успех можно не читать — он уходит сам. Ошибка остаётся до закрытия:
    // иначе сбой сохранения исчезает раньше, чем пользователь его заметит.
    if (kind === "success") {
      flashTimerRef.current = window.setTimeout(() => {
        setMessage(null);
        flashTimerRef.current = null;
      }, 2500);
    }
  };
  const dismissFlash = () => {
    if (flashTimerRef.current !== null) {
      window.clearTimeout(flashTimerRef.current);
      flashTimerRef.current = null;
    }
    setMessage(null);
  };

  const saveSeqRef = useRef(0);
  const saveToDb = (
    frags: { start: number; end: number; comment: string }[],
    options: { commentStart?: number; commentDeleted?: boolean } = {},
  ) => {
    const seq = ++saveSeqRef.current;
    setSaveState("saving");
    replaceFragments(pairId, frags, position)
      .then(() => {
        if (seq !== saveSeqRef.current) return;
        // У сохранения комментария одно подтверждение — локальный бейдж.
        // Транспорт показывает только ход операции и ошибки, иначе рядом
        // появлялись две одинаковые надписи «Сохранено».
        if (options.commentStart !== undefined) {
          setSaveState("idle");
          if (options.commentDeleted) flash("Комментарий удалён");
          else showCommentSaved(options.commentStart);
        } else {
          setSaveState("saved");
          flash("Сохранено");
        }
      })
      .catch((e) => {
        if (seq !== saveSeqRef.current) return;
        setSaveState("error");
        flash(`Ошибка сохранения: ${failureReason(e)}`, "error", () =>
          saveToDb(frags, options),
        );
      });
  };

  // Ввод номера кадра (1-based, как на экране): клампинг в 1..N, переход
  // как прыжок (строго целевой кадр, незаконченные запросы отменяются),
  // пауза воспроизведения. Пусто/мусор — откат без прыжка; уже на этом
  // кадре — тоже без лишнего прыжка.
  const commitFrameInput = () => {
    if (frameDraft === null) return;
    setFrameDraft(null);
    if (!pair || editingCommentRef.current) return;
    const n = parseInt(frameDraft, 10);
    if (!Number.isFinite(n)) return;
    const target = Math.min(pair.total_frames, Math.max(1, n)) - 1;
    if (target === position) return;
    setPlaying(false);
    jumpMode();
    setPosition(target);
  };

  const commentRef = useRef<HTMLTextAreaElement | null>(null);
  const commentEditRef = useRef<HTMLButtonElement | null>(null);
  const commentToolRef = useRef<HTMLButtonElement | null>(null);
  const commentTriggerRef = useRef<HTMLElement | null>(null);
  const commentSavedTimerRef = useRef<number | null>(null);
  const committedRef = useRef(false);

  useEffect(() => {
    return () => {
      if (commentSavedTimerRef.current !== null) {
        window.clearTimeout(commentSavedTimerRef.current);
      }
    };
  }, []);

  const showCommentSaved = (fragmentStart: number) => {
    if (commentSavedTimerRef.current !== null) {
      window.clearTimeout(commentSavedTimerRef.current);
    }
    setCommentSavedFor(fragmentStart);
    commentSavedTimerRef.current = window.setTimeout(() => {
      setCommentSavedFor(null);
      commentSavedTimerRef.current = null;
    }, 1800);
  };

  /** Возврат фокуса туда, откуда открыли редактор: иначе после Save/Cancel
   * клавиатурный пользователь оказывается в начале страницы. */
  const restoreCommentFocus = () => {
    const trigger = commentTriggerRef.current;
    commentTriggerRef.current = null;
    window.setTimeout(() => {
      const target = trigger?.isConnected
        ? trigger
        : (commentEditRef.current ?? commentToolRef.current);
      target?.focus();
    }, 0);
  };

  const startCommentEdit = (trigger?: HTMLElement | null) => {
    const p = modelRef.current;
    if (!p || editingCommentRef.current) return;
    const f = p.fragmentAt(position);
    if (!f) {
      flash("Комментарии можно добавлять только к выделенным участкам", "error");
      return;
    }
    // Пока черновик открыт, кадр не двигается: и сохранение, и удаление
    // обязаны вернуться ровно к тому фрагменту, у которого нажали «I».
    commentStartRef.current = f.start;
    setPlaying(false);
    commentTriggerRef.current =
      trigger ?? commentEditRef.current ?? commentToolRef.current;
    // Новое открытие редактора отменяет прежнее подтверждение: иначе
    // «Сохранено» вернулось бы после закрытия нового черновика.
    if (commentSavedTimerRef.current !== null) {
      window.clearTimeout(commentSavedTimerRef.current);
      commentSavedTimerRef.current = null;
    }
    setCommentSavedFor(null);
    setCommentDraft(f.comment);
    setEditingComment(true);
    committedRef.current = false;
    window.setTimeout(() => commentRef.current?.focus(), 0);
  };

  const commentTarget = () => {
    const p = modelRef.current;
    const start = commentStartRef.current;
    if (!p || start === null) return null;
    return p.fragments.find((f) => f.start === start) ?? null;
  };

  const commitComment = () => {
    const p = modelRef.current;
    if (!p || committedRef.current) return;
    // Пустой черновик — не сохранение: удаление требует отдельной кнопки,
    // иначе стёртый для правки текст пропадал бы по привычному Ctrl+Enter.
    if (!commentDraft.trim()) return;
    const f = commentTarget();
    if (!f) return;
    committedRef.current = true;
    setEditingComment(false);
    restoreCommentFocus();
    commentStartRef.current = null;
    if (p.setComment(f.start, commentDraft)) {
      rerender();
      drawStatusbar();
      saveToDb(p.getFragments(), { commentStart: f.start });
    }
  };

  const deleteComment = () => {
    const p = modelRef.current;
    if (!p || committedRef.current) return;
    const f = commentTarget();
    if (!f || !f.comment) return;
    committedRef.current = true;
    setEditingComment(false);
    restoreCommentFocus();
    commentStartRef.current = null;
    if (p.setComment(f.start, "")) {
      rerender();
      drawStatusbar();
      // Удаление не оставляет плашку «Сохранено» без текста.
      setCommentSavedFor(null);
      saveToDb(p.getFragments(), {
        commentStart: f.start,
        commentDeleted: true,
      });
    }
  };

  const cancelComment = () => {
    committedRef.current = true;
    commentStartRef.current = null;
    setEditingComment(false);
    restoreCommentFocus();
  };

  // Действия разметки — один источник для клавиатуры и видимой панели.
  const afterFragmentChange = () => {
    const p = modelRef.current;
    rerender();
    drawStatusbar();
    if (p) saveToDb(p.getFragments());
  };

  const markStart = () => {
    const p = modelRef.current;
    if (!p) return;
    p.newStart(position);
    afterFragmentChange();
  };

  const markEnd = () => {
    const p = modelRef.current;
    if (!p) return;
    p.newEnd(position);
    afterFragmentChange();
  };

  const createOrComplete = () => {
    const p = modelRef.current;
    if (!p) return;
    if (keyPose === null) {
      if (p.fragments.some((f) => position >= f.start && position <= f.end)) {
        flash("Невозможно создать фрагмент внутри другого", "error");
      } else {
        setKeyPose(position);
      }
      return;
    }
    const [lo, hi] = keyPose < position
      ? [keyPose, position]
      : [position, keyPose];
    if (p.add(lo, hi)) {
      setKeyPose(null);
      afterFragmentChange();
    } else {
      flash("Фрагмент пересекается с существующим", "error");
    }
  };

  const deleteAtPosition = () => {
    const p = modelRef.current;
    if (!p) return;
    const res = p.delete(position);
    if (res === "ok" || res === "merged") {
      afterFragmentChange();
      if (res === "merged") startCommentEdit();
    } else if (res === "ambiguous") {
      flash("Текущий кадр — стык двух фрагментов. Переместите курсор.", "error");
    } else {
      flash("Под курсором нет фрагмента", "error");
    }
  };

  const undoEdit = () => {
    const p = modelRef.current;
    if (p?.undo()) afterFragmentChange();
  };

  const redoEdit = () => {
    const p = modelRef.current;
    if (p?.redo()) afterFragmentChange();
  };

  // Клавиатура.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const p = modelRef.current;
      if (!p) return;
      const k = e.key;
      const code = e.code;
      const ctrl = e.ctrlKey || e.metaKey;
      // Физическая клавиша: кнопка не зависит от раскладки (KeyR ~ R на любой раскладке).
      const is = (physical: string) => code === physical || k === physical;

      // Справка и меню — настоящие диалоги: клавиши обрабатывают они,
      // редактор не перехватывает их и не мешает прокрутке панели.
      if (helpOpenRef.current || openMenuRef.current) return;

      const target = e.target as HTMLElement | null;
      const isEditing =
        target?.isContentEditable ||
        ["INPUT", "TEXTAREA", "SELECT"].includes(target?.tagName ?? "");
      if (isEditing) return;

      // Пока открыт черновик комментария, его кнопки — часть локального
      // поля: Escape закрывает поле, остальные глобальные клавиши молчат.
      if (editingCommentRef.current) {
        if (is("Escape")) {
          e.preventDefault();
          cancelComment();
        }
        return;
      }

      // Навигация.
      const rightKey = is("ArrowRight") || is("Period");
      const leftKey = is("ArrowLeft") || is("Comma");
      // Повторяются только кадровая навигация и прыжки по границам.
      const repeatable =
        rightKey || leftKey || is("PageUp") || is("PageDown");
      if (e.repeat && !repeatable) return;

      // Модификаторы: разрешены только Ctrl+Z, Ctrl+Shift+Z, Ctrl+Enter
      // и Ctrl+←/→ (вместе с алиасами «,»/«.»). Остальные комбинации —
      // браузеру: Ctrl+D не удаляет фрагмент, Ctrl+F не включает полный экран.
      if (e.altKey) return;
      if (ctrl && !(rightKey || leftKey || is("KeyZ") || is("Enter"))) {
        return;
      }

      if (rightKey) rightHeldRef.current = true;
      if (leftKey) leftHeldRef.current = true;

      if (ctrl && rightKey) {
        setPlaying(false);
        if (e.repeat) startChase(1);
        else jumpMode(); // одиночный Ctrl+→ — прыжок на 10, без промежуточных
        setPosition((pos) => Math.min(p.totalFrames - 1, pos + SEEK_STEP));
      } else if (ctrl && leftKey) {
        setPlaying(false);
        if (e.repeat) startChase(-1);
        else jumpMode();
        setPosition((pos) => Math.max(0, pos - SEEK_STEP));
      } else if (rightKey) {
        setPlaying(false);
        if (e.repeat) startChase(1);
        else jumpMode(); // тап — сразу целевой кадр (без «анимации» промкадров)
        setPosition((pos) => Math.min(p.totalFrames - 1, pos + 1));
      } else if (leftKey) {
        setPlaying(false);
        if (e.repeat) startChase(-1);
        else jumpMode();
        setPosition((pos) => Math.max(0, pos - 1));
      } else if (is("PageDown")) {
        e.preventDefault();
        setPlaying(false);
        jump(1);
      } else if (is("PageUp")) {
        e.preventDefault();
        setPlaying(false);
        jump(-1);
      } else if (is("Home")) {
        setPlaying(false);
        jumpMode();
        setPosition(0);
      } else if (is("End")) {
        setPlaying(false);
        jumpMode();
        setPosition(p.totalFrames - 1);
      } else if (is(" ") || is("Space") || is("Spacebar")) {
        e.preventDefault();
        togglePlayback();
      } else if (is("KeyR")) {
        const nd = direction === 1 ? -1 : 1;
        setDirection(nd);
        // J-проигрывание в полёте: цель пересчитывается под новое направление,
        // иначе устаревшая цель (в старом направлении) никогда не достигается
        // обратным ходом, и остановки на границе не происходит.
        if (playTargetRef.current !== null) {
          const p = modelRef.current;
          if (p) playTargetRef.current = retargetOnDirectionChange(playTargetRef.current, position, nd, p.keyFrames(), p.totalFrames);
        }
      } else if (is("KeyJ")) {
        if (!e.repeat) playToBoundary();
      } else if (is("KeyH") || k === "?") {
        // «?» — это Shift+7 на русской раскладке: проверяем до цифр,
        // иначе справка превращается в смену скорости.
        setHelpOpen(true);
      } else if (/^Digit[0-9]$/.test(code)) {
        setSpeed(Math.pow(2, parseInt(code.slice(5), 10)));
      } else if (is("KeyF")) {
        toggleFullscreen();
      } else if (is("KeyA")) {
        setPreserveAspect((v) => !v);
      } else if (is("KeyV")) {
        setOpenMenu("view");
      } else if (is("KeyB")) {
        setOpenMenu("exchange");
      } else if (is("KeyG")) {
        setOpenMenu("diagnostics");
      }

      // Отметка фрагментов.
      else if (is("ArrowUp") || is("BracketLeft")) {
        markStart();
      } else if (is("ArrowDown") || is("BracketRight")) {
        markEnd();
      } else if (is("KeyK") || is("Insert")) {
        createOrComplete();
      } else if (is("Delete") || is("KeyD")) {
        deleteAtPosition();
      } else if (is("KeyI")) {
        startCommentEdit();
      } else if (ctrl && is("KeyZ")) {
        // Ctrl+Shift+Z — стандартный redo, иначе undo.
        if (e.shiftKey) redoEdit();
        else undoEdit();
      } else if (is("KeyZ")) {
        redoEdit();
      } else if (is("KeyE")) {
        handleExport();
      } else if (is("Escape") || is("KeyQ")) {
        if (exportItemsRef.current) {
          e.preventDefault();
          setExportItems(null);
        } else if (fallbackFullscreenRef.current) {
          // Резервный полный экран живёт только в приложении: нативного
          // события Esc не будет, выходим сами.
          e.preventDefault();
          fallbackFullscreenRef.current = false;
          fullscreenRef.current = false;
          setIsFallbackFullscreen(false);
          setIsFullscreen(false);
        } else if (!fullscreenRef.current && !nativeFullscreenElement()) {
          handleBack();
        }
      }
    };
    window.addEventListener("keydown", onKey);
    const onKeyUp = (e: KeyboardEvent) => {
      if (e.code === "ArrowRight" || e.code === "Period") rightHeldRef.current = false;
      if (e.code === "ArrowLeft" || e.code === "Comma") leftHeldRef.current = false;
      if (!rightHeldRef.current && !leftHeldRef.current) endChase();
    };
    const onBlur = () => {
      rightHeldRef.current = false;
      leftHeldRef.current = false;
      endChase();
    };
    window.addEventListener("keyup", onKeyUp);
    window.addEventListener("blur", onBlur);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("keyup", onKeyUp);
      window.removeEventListener("blur", onBlur);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [position, keyPose, direction, speed, playing, drawStatusbar]);

  // Завершение редактирования: фиксируем текущий кадр и возвращаемся к списку.
  const handleBack = () => {
    savePosition(pairId, position).catch(() => {});
    onBack();
  };

  // Закрытие/сворачивание вкладки: keepalive-запрос досылает позицию.
  useEffect(() => {
    const flush = () => {
      savePosition(pairId, position, true).catch(() => {});
    };
    window.addEventListener("pagehide", flush);
    return () => window.removeEventListener("pagehide", flush);
  }, [pairId, position]);

  const setExportingUi = (on: boolean) => {
    exportingRef.current = on;
    setExporting(on);
  };

  const stopPolling = () => {
    if (pollTimerRef.current !== null) {
      window.clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  };

  const finishExportUi = () => {
    stopPolling();
    setExportingUi(false);
    setExportProgress(null);
  };

  const startPolling = () => {
    stopPolling();
    pollTimerRef.current = window.setInterval(async () => {
      let st: ExportStatus;
      try {
        st = await getExportStatus(pairId);
      } catch (e) {
        finishExportUi();
        flash(`Ошибка опроса экспорта: ${failureReason(e)}`, "error");
        return;
      }
      if (st.state === "running" || st.state === "cancelling") {
        const total = st.total ?? 1;
        setExportProgress((st.index ?? 0) / Math.max(1, total));
      } else if (st.state === "done") {
        finishExportUi();
        setExportItems(st.files ?? []);
        flash(`Экспортировано фрагментов: ${st.files?.length ?? 0}`);
      } else if (st.state === "cancelled") {
        finishExportUi();
        flash("Экспорт отменён");
      } else if (st.state === "error") {
        finishExportUi();
        flash(`Ошибка экспорта: ${st.error ?? "неизвестно"}`, "error");
      } else {
        // idle: сервер забыл статус (рестарт) — дальше ждать нечего.
        finishExportUi();
        flash("Экспорт прерван (нет статуса на сервере)", "error");
      }
    }, 500);
  };

  const handleExport = async (force = false) => {
    // Повтор во время экспорта — отмена, а не второй запуск (кнопка и E
    // работают как переключатель).
    if (exportingRef.current) {
      try {
        await cancelExport(pairId);
      } catch (e) {
        flash(`Не удалось отменить экспорт: ${failureReason(e)}`, "error");
      }
      return;
    }
    // Нет фрагментов — экспортировать нечего. Кнопка уже заблокирована,
    // но E и повторный запуск из списка файлов идут мимо disabled.
    if ((modelRef.current?.fragments.length ?? 0) === 0) {
      flash("Нет фрагментов для экспорта", "error");
      return;
    }
    setExportingUi(true);
    setExportProgress(0);
    startPolling();
    try {
      // Актуальность решает сервер по хешу (границы+видео): совпал — сразу
      // done с файлами, иначе прогон. Локальных сравнений больше нет.
      const st = await startExport(pairId, force);
      if (st.state === "done") {
        // Нарезки актуальны — сервер отдал готовые без прогона.
        setExportItems(st.files ?? []);
        flash(`Нарезки актуальны, файлов: ${st.files?.length ?? 0}`);
        finishExportUi();
      } else if (st.state !== "running") {
        finishExportUi();
        if (st.state === "error") flash(`Ошибка экспорта: ${st.error ?? "неизвестно"}`, "error");
        else flash("Экспорт не запущен", "error");
      }
    } catch (e) {
      finishExportUi();
      flash(`Ошибка экспорта: ${failureReason(e)}`, "error");
    }
  };

  const handleDeleteExports = async () => {
    const ok = window.confirm(
      "Удалить все экспортированные фрагменты? Оригиналы не пострадают.",
    );
    if (!ok) return;
    try {
      await deleteExport(pairId);
      setExportItems(null);
      flash("Экспортированные фрагменты удалены");
    } catch (e) {
      flash(`Не удалось удалить экспорт: ${failureReason(e)}`, "error");
    }
  };

  // Импорт разметки из архива (только fragments.tsv): после применения
  // пару перечитываем с сервера — модель, позиция и превью списка свежие.
  const handleAnnotationsImport = (file: File) => {
    if (annBusy) return;
    setPlaying(false);
    setAnnBusy(true);
    setAnnProgress(0);
    importAnnotations(pairId, file, { onProgress: setAnnProgress })
      .then((res) => {
        flash(`Разметка применена: фрагментов ${res.fragments}`);
        return getPair(pairId);
      })
      .then((p) => loadPairInto(p))
      .catch((e: Error) => flash(`Ошибка импорта разметки: ${failureReason(e)}`, "error"))
      .finally(() => {
        setAnnBusy(false);
        setAnnProgress(null);
      });
  };

  // Скачивание архива с откликом: сборка на сервере синхронна, голая
  // ссылка висела бы мёртвой всё время подготовки.
  const handleDownload = (
    key: "ann" | "bak",
    url: string,
    filename: string,
  ) => {
    if (dlState !== null || annBusy) return;
    setDlState({ key, frac: null });
    downloadFile(url, filename, (f) => setDlState({ key, frac: f }))
      .catch((e: Error) => flash(`Ошибка скачивания: ${failureReason(e)}`, "error"))
      .finally(() => setDlState(null));
  };

  // Подхват состояния экспорта при входе: Esc во время экспорта сервер не
  // останавливает, прогресс/отмена продолжают работать после возврата.
  // Готовый список файлов НЕ показываем автоматически — только по нажатию
  // «Экспорт» (при актуальных нарезках сервер отдаёт done с файлами сразу,
  // без перегона).
  useEffect(() => {
    let alive = true;
    getExportStatus(pairId)
      .then((st) => {
        if (!alive) return;
        if (st.state === "running" || st.state === "cancelling") {
          setExportingUi(true);
          const total = st.total ?? 1;
          setExportProgress((st.index ?? 0) / Math.max(1, total));
          startPolling();
        } else if (st.state === "error") {
          flash(`Ошибка экспорта: ${st.error ?? "неизвестно"}`, "error");
        }
      })
      .catch(() => {});
    return () => {
      alive = false;
      stopPolling();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pairId]);

  if (loadError) {
    return (
      <div className="loading">
        <h1 className="sr-only">VideoCutter — редактор</h1>
        <div role="alert">⚠ {loadError}</div>
        <button onClick={onBack}>← Назад к списку</button>
      </div>
    );
  }

  if (!pair) {
    return (
      <div className="loading" role="status">
        <h1 className="sr-only">VideoCutter — редактор</h1>
        {waitingIndex
          ? `Индексируется…${waitPct != null ? ` ${formatPercent(Math.round(waitPct * 100))}` : ""}`
          : "Загрузка видео-пары…"}
      </div>
    );
  }

  const sel = modelRef.current?.selectedFrames() ?? 0;
  const fragmentCount = modelRef.current?.fragments.length ?? 0;
  const exportDisabled = fragmentCount === 0 && !exporting;
  const markedPct = pair.total_frames > 0 ? (100 * sel) / pair.total_frames : 0;
  const canUndo = (modelRef.current?.histPos ?? 0) > 0;
  const canRedo = modelRef.current
    ? modelRef.current.histPos < modelRef.current.history.length - 1
    : false;

  // Индикатор загрузки — только при ручной навигации. При воспроизведении
  // задержек не показываем: кадры идут все подряд в темпе железа.
  const frameLoading = !playing && position !== shownFrame;
  const pairTitle = `${pair.name || pairId}: источник ${pair.source_name}${
    pair.preview_name !== pair.source_name
      ? ` · Превью: ${pair.preview_name}`
      : ""
  }`;
  // Пропорции кадра: в портрете область видео сжимается до них, чтобы
  // чёрные поля не съедали половину экрана. Передаём их CSS-переменной,
  // а не инлайновым aspect-ratio: инлайн перебивал бы `auto` в ландшафте
  // и полноэкранном режиме, где высоту задаёт раскладка.
  const frameAspect =
    pair.width > 0 && pair.height > 0
      ? `${pair.width} / ${pair.height}`
      : undefined;
  const currentFragment = modelRef.current?.fragmentAt(position);
  const fragmentList = modelRef.current?.fragments ?? [];
  const fragmentNumber = currentFragment
    ? fragmentList.indexOf(currentFragment) + 1
    : 0;
  const fragmentContext = currentFragment
    ? `фрагмент ${fragmentNumber} из ${fragmentList.length} · кадры ${formatNumber(
        currentFragment.start + 1,
      )}–${formatNumber(currentFragment.end + 1)}`
    : "";
  const commentSavedHere =
    commentSavedFor !== null && currentFragment?.start === commentSavedFor;
  const commentDraftEmpty = commentDraft.trim() === "";
  const canDeleteComment = commentDraftEmpty && !!currentFragment?.comment;

  return (
    <div
      ref={editorRef}
      className={
        isFullscreen
          ? isFallbackFullscreen
            ? "editor fullscreen fullscreen-fallback"
            : "editor fullscreen"
          : compactPortrait
            ? "editor is-compact"
            : "editor"
      }
    >
      <h1 className="sr-only">Редактор: {pair.name || pairId}</h1>
      {isFullscreen && (
        <button
          ref={fullscreenExitRef}
          type="button"
          className="fullscreen-exit"
          onClick={toggleFullscreen}
          title={keyTitle(
            "Выйти из полноэкранного режима",
            "Esc",
            keyboard,
          )}
          aria-label="Выйти из полноэкранного режима"
        >
          <span aria-hidden="true">✕</span>
          <span className="sr-only">{keyboard ? "Выйти (Esc)" : "Выйти"}</span>
        </button>
      )}
      {!isFullscreen && (
        <header
          ref={editorTopRef}
          className={controlsHidden ? "editor-top controls-hidden" : "editor-top"}
        >
          <div className="editor-context">
            <button type="button" className="editor-back" onClick={handleBack}>
              ← Назад
            </button>
            <div className="editor-identity" title={pairTitle}>
              <span className="pair-name" title={pair.name || pairId}>
                {pair.name || pairId}
              </span>
              <span className="pair-source">
                источник {pair.source_name}
                {pair.preview_name !== pair.source_name && (
                  <span> · Превью: {pair.preview_name}</span>
                )}
              </span>
            </div>
            {pair.pair_warning && (
              <span
                className="editor-warning"
                role="status"
                title={pair.pair_warning}
                aria-label={`Предупреждение: ${pair.pair_warning}`}
              >
                <span aria-hidden="true">⚠</span>
                <span className="editor-warning-text">{pair.pair_warning}</span>
              </span>
            )}
            {keyboard && (
              <div
                className="editor-utilities"
                role="group"
                aria-label="Настройки и сервис"
              >
              <Popover
                label="Вид"
                toggleKey="KeyV"
                trigger={
                  <>
                    Вид
                    {keyboard && <kbd>V</kbd>}
                    <span className="popover-caret" aria-hidden="true">
                      ▾
                    </span>
                  </>
                }
                open={openMenu === "view"}
                onOpenChange={(v) => setOpenMenu(v ? "view" : null)}
              >
                <ViewMenu
                  scale={scale}
                  quality={quality}
                  preserveAspect={preserveAspect}
                  showShortcuts={keyboard}
                  onScale={setScale}
                  onQuality={setQuality}
                  onPreserveAspect={setPreserveAspect}
                  onFullscreen={() => {
                    setOpenMenu(null);
                    toggleFullscreen();
                  }}
                />
              </Popover>
              <Popover
                label="Данные"
                toggleKey="KeyB"
                trigger={
                  <>
                    Данные
                    {keyboard && <kbd>B</kbd>}
                    <span className="popover-caret" aria-hidden="true">
                      ▾
                    </span>
                  </>
                }
                open={openMenu === "exchange"}
                onOpenChange={(v) => setOpenMenu(v ? "exchange" : null)}
              >
                <DataMenu
                  downloading={dlState?.key ?? null}
                  downloadFrac={dlState?.frac ?? null}
                  annBusy={annBusy}
                  annProgress={annProgress}
                  onDownload={(key) =>
                    handleDownload(
                      key,
                      key === "ann"
                        ? annotationsUrl(pairId)
                        : taskBackupUrl(pairId),
                      `${pairId}-${key === "ann" ? "annotations" : "backup"}.zip`,
                    )
                  }
                  onImport={handleAnnotationsImport}
                />
              </Popover>
              <Popover
                label="Диагностика"
                toggleKey="KeyG"
                trigger={
                  <>
                    Диагностика
                    {keyboard && <kbd>G</kbd>}
                    <span className="popover-caret" aria-hidden="true">
                      ▾
                    </span>
                  </>
                }
                open={openMenu === "diagnostics"}
                onOpenChange={(v) => setOpenMenu(v ? "diagnostics" : null)}
              >
                <DiagnosticsMenu
                  fps={perf.fps}
                  load={perf.load}
                  show={perf.show}
                />
              </Popover>
              </div>
            )}
            <div className="editor-global-actions">
              {/* На сенсорном экране три меню не помещаются в строку и
               * отжимают имя задачи. Одно «Ещё» с вкладками оставляет
               * экспорт видимым, а имя — крупным. */}
              {!keyboard && (
                <Popover
                  label="Ещё"
                  trigger={
                    <>
                      Ещё
                      <span className="popover-caret" aria-hidden="true">
                        ▾
                      </span>
                    </>
                  }
                  open={openMenu === "more"}
                  onOpenChange={(v) => {
                    setOpenMenu(v ? "more" : null);
                    if (v) setMobileMenuTab("view");
                  }}
                >
                  <div className="menu-stack">
                    <div
                      className="menu-tabs"
                      role="tablist"
                      aria-label="Настройки и сервис"
                    >
                      {(
                        [
                          ["view", "Вид"],
                          ["exchange", "Данные"],
                          ["diagnostics", "Диагностика"],
                        ] as const
                      ).map(([id, label]) => (
                        <button
                          key={id}
                          type="button"
                          role="tab"
                          aria-selected={mobileMenuTab === id}
                          onClick={() => setMobileMenuTab(id)}
                        >
                          {label}
                        </button>
                      ))}
                    </div>
                    <div role="tabpanel" aria-label="Вид">
                      {mobileMenuTab === "view" && (
                        <ViewMenu
                          scale={scale}
                          quality={quality}
                          preserveAspect={preserveAspect}
                          showShortcuts={keyboard}
                          onScale={setScale}
                          onQuality={setQuality}
                          onPreserveAspect={setPreserveAspect}
                          onFullscreen={() => {
                            setOpenMenu(null);
                            toggleFullscreen();
                          }}
                        />
                      )}
                    </div>
                    <div role="tabpanel" aria-label="Данные">
                      {mobileMenuTab === "exchange" && (
                        <DataMenu
                          downloading={dlState?.key ?? null}
                          downloadFrac={dlState?.frac ?? null}
                          annBusy={annBusy}
                          annProgress={annProgress}
                          onDownload={(key) =>
                            handleDownload(
                              key,
                              key === "ann"
                                ? annotationsUrl(pairId)
                                : taskBackupUrl(pairId),
                              `${pairId}-${key === "ann" ? "annotations" : "backup"}.zip`,
                            )
                          }
                          onImport={handleAnnotationsImport}
                        />
                      )}
                    </div>
                    <div role="tabpanel" aria-label="Диагностика">
                      {mobileMenuTab === "diagnostics" && (
                        <DiagnosticsMenu
                          fps={perf.fps}
                          load={perf.load}
                          show={perf.show}
                        />
                      )}
                    </div>
                  </div>
                </Popover>
              )}
              <button
                type="button"
                className="toolbar-export"
                onClick={() => handleExport()}
                disabled={exportDisabled}
                title={
                  exporting
                    ? keyTitle("Отменить экспорт", "E", keyboard)
                    : exportDisabled
                      ? "Нет фрагментов для экспорта"
                      : keyTitle("Экспорт", "E", keyboard)
                }
              >
                {exportProgress !== null && (
                  <span
                    className="toolbar-export-progress"
                    style={{ width: `${Math.round(exportProgress * 100)}%` }}
                  />
                )}
                <span className="toolbar-export-label">
                  {exportProgress !== null ? (
                    `${formatPercent(Math.round(exportProgress * 100))} · Отмена`
                  ) : (
                    <>
                      Экспорт {keyboard && <kbd>E</kbd>}
                    </>
                  )}
                </span>
              </button>
              <button
                type="button"
                className="toolbar-help"
                onClick={() => setHelpOpen(true)}
                title={keyTitle("Справка", "H", keyboard)}
                aria-label="Справка"
              >
                <span aria-hidden="true">?</span>
              </button>
            </div>
          </div>

          <div className="editor-workbench" hidden={keyboard}>
            <div
              id="editor-markup-tools"
              className="fragment-actions"
              role="toolbar"
              aria-label="Разметка фрагментов"
              hidden={keyboard}
            >
              <button
                type="button"
                onClick={markStart}
                title={keyTitle("Начало фрагмента", "[ или ↑", keyboard)}
                aria-label="Начало фрагмента"
              >
                <FragmentIcon name="start" />
                {showWorkbenchLabels && (
                  <span className="fragment-action-text">Начало</span>
                )}
                {keyboard && <kbd>[</kbd>}
              </button>
              <button
                type="button"
                onClick={markEnd}
                title={keyTitle("Конец фрагмента", "] или ↓", keyboard)}
                aria-label="Конец фрагмента"
              >
                <FragmentIcon name="end" />
                {showWorkbenchLabels && (
                  <span className="fragment-action-text">Конец</span>
                )}
                {keyboard && <kbd>]</kbd>}
              </button>
              <button
                type="button"
                className="fragment-create"
                onClick={createOrComplete}
                aria-pressed={keyPose !== null}
                aria-label={
                  keyPose === null ? "Создать фрагмент" : "Завершить фрагмент"
                }
                title={
                  keyPose === null
                    ? keyTitle(
                        "Создать фрагмент: первое нажатие — начало, второе — конец",
                        "K",
                        keyboard,
                      )
                    : keyTitle(
                        "Завершить фрагмент в текущем кадре",
                        "K",
                        keyboard,
                      )
                }
              >
                <FragmentIcon name={keyPose === null ? "create" : "complete"} />
                {showWorkbenchLabels && (
                  <span className="fragment-action-text">
                    {keyPose === null ? "Создать" : "Завершить"}
                  </span>
                )}
                {keyboard && <kbd>K</kbd>}
              </button>
              <button
                type="button"
                onClick={deleteAtPosition}
                title={keyTitle(
                  "Удалить фрагмент под курсором",
                  "D или Del",
                  keyboard,
                )}
                aria-label="Удалить фрагмент"
              >
                <FragmentIcon name="delete" />
                {showWorkbenchLabels && (
                  <span className="fragment-action-text">Удалить</span>
                )}
                {keyboard && <kbd>D</kbd>}
              </button>
              <button
                type="button"
                ref={commentToolRef}
                onClick={(e) => startCommentEdit(e.currentTarget)}
                title={keyTitle("Комментарий к фрагменту", "I", keyboard)}
                aria-label="Комментарий к фрагменту"
              >
                <FragmentIcon name="comment" />
                {showWorkbenchLabels && (
                  <span className="fragment-action-text">
                    {keyboard ? "Комментарий" : "Коммент."}
                  </span>
                )}
                {keyboard && <kbd>I</kbd>}
              </button>
              <div
                className="editor-history"
                role="group"
                aria-label="История изменений"
              >
                <button
                  type="button"
                  onClick={undoEdit}
                  disabled={!canUndo}
                  title={keyTitle(
                    "Отменить последнее изменение",
                    "Ctrl+Z",
                    keyboard,
                  )}
                  aria-label="Отменить последнее изменение"
                >
                  <FragmentIcon name="undo" />
                  {showWorkbenchLabels && (
                    <span className="fragment-action-text">Отменить</span>
                  )}
                  {keyboard && <kbd>Ctrl+Z</kbd>}
                </button>
                <button
                  type="button"
                  onClick={redoEdit}
                  disabled={!canRedo}
                  title={keyTitle(
                    "Повторить отменённое",
                    "Ctrl+Shift+Z или Z",
                    keyboard,
                  )}
                  aria-label="Повторить отменённое"
                >
                  <FragmentIcon name="redo" />
                  {showWorkbenchLabels && (
                    <span className="fragment-action-text">Повторить</span>
                  )}
                  {keyboard && <kbd>Ctrl+Shift+Z</kbd>}
                </button>
              </div>
            </div>
          </div>
        </header>
      )}

      <div
        className="video-stage"
        style={{ "--frame-aspect": frameAspect } as CSSProperties}
      >
        <div className="video-area">
          <img
            ref={imageRef}
            alt={`Кадр ${formatNumber(position + 1)} из ${formatNumber(pair.total_frames)}`}
            className={frameLoading ? "frame-loading" : undefined}
            onClick={togglePlayback}
            title="Клик — воспроизведение / пауза"
            style={{
              objectFit: preserveAspect ? "contain" : "fill",
              width: "100%",
              height: "100%",
              // До первого кадра пустой <img> не рисуем (иначе вместо кадра
              // виден «сломанный» значок с alt-текстом).
              visibility: shownFrame < 0 ? "hidden" : undefined,
            }}
          />
          {shownFrame < 0 && <div className="frame-empty">Кадр загружается…</div>}
          {frameLoading && (
            <div className="frame-wait">
              <span className="frame-ring" aria-hidden="true" />
              {frameStuck && (
                <span className="frame-wait-text" role="alert">
                  Кадр не загрузился — перейдите на соседний кадр и вернитесь
                </span>
              )}
            </div>
          )}
        </div>
        {(!isFullscreen || editingComment) && (
          editingComment ? (
            <div className="editor-comment is-editing">
              <span className="comment-context">{fragmentContext}</span>
              <textarea
                ref={commentRef}
                name="fragmentComment"
                aria-label="Комментарий к фрагменту"
                aria-describedby="comment-hint"
                rows={3}
                value={commentDraft}
                placeholder="Комментарий к сегменту…"
                onChange={(e) => setCommentDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Escape") {
                    e.preventDefault();
                    cancelComment();
                  } else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                    e.preventDefault();
                    commitComment();
                  }
                }}
              />
              <p className="comment-hint" id="comment-hint">
                Enter — новая строка · Ctrl+Enter — сохранить
              </p>
              <div className="comment-actions">
                {canDeleteComment ? (
                  <button
                    type="button"
                    className="comment-delete"
                    onClick={deleteComment}
                  >
                    Удалить комментарий
                  </button>
                ) : (
                  <button
                    type="button"
                    className="comment-save"
                    onClick={commitComment}
                    disabled={commentDraftEmpty}
                  >
                    Сохранить
                  </button>
                )}
                <button
                  type="button"
                  className="comment-cancel"
                  onClick={cancelComment}
                >
                  Отмена
                </button>
              </div>
            </div>
          ) : currentFragment?.comment || commentSavedHere ? (
            <div
              className={`editor-comment${commentSavedHere ? " is-saved" : ""}${
                currentFragment?.comment ? "" : " is-empty-saved"
              }`}
            >
              <span className="comment-context">{fragmentContext}</span>
              {currentFragment?.comment ? (
                <button
                  type="button"
                  ref={commentEditRef}
                  className="comment-edit"
                  onClick={(e) => startCommentEdit(e.currentTarget)}
                  title="Изменить комментарий"
                >
                  <span className="sr-only">Редактировать комментарий: </span>
                  <span className="comment-text">{currentFragment.comment}</span>
                  <FragmentIcon name="edit" />
                </button>
              ) : null}
              {commentSavedHere && (
                <span className="comment-saved" role="status" aria-atomic="true">
                  <span className="save-dot" aria-hidden="true" />
                  Сохранено
                </span>
              )}
            </div>
          ) : null
        )}
      </div>

      {!isFullscreen && rotateGain?.worthRotating && (
        <p className="rotate-hint">
          <b>Поверните телефон горизонтально</b> — кадр станет крупнее.
        </p>
      )}

      {!isFullscreen && (
        <div className="editor-transport">
          <span className="frame-readout">
            кадр{" "}
            <input
              className="frame-input"
              type="text"
              inputMode="numeric"
              aria-label="Номер кадра"
              title={keyTitle(
                "Номер кадра: введите число",
                "Enter (Esc — отмена)",
                keyboard,
              )}
              style={{
                // Адаптивная ширина: запас по max длине total, черновика и
                // позиции, +1 разряд под курсор — ввод не обрезается.
                width: `${Math.max(
                  (frameDraft ?? String(position + 1)).length,
                  String(pair.total_frames).length,
                ) + 1}ch`,
              }}
              value={frameDraft ?? String(position + 1)}
              onFocus={(e) => {
                setFrameDraft(String(position + 1));
                e.currentTarget.select();
              }}
              onChange={(e) => setFrameDraft(e.target.value.replace(/\D/g, ""))}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  e.currentTarget.select(); // повторный ввод заменит, а не допишет
                  commitFrameInput();
                } else if (e.key === "Escape") {
                  e.preventDefault();
                  setFrameDraft(null); // откат; фокус остаётся
                }
              }}
              onBlur={commitFrameInput}
            />{" "}
            / {formatNumber(pair.total_frames)}
            <span className="save-state" data-state={saveState}>
              {saveState !== "idle" && (
                <span className="save-dot" aria-hidden="true" />
              )}
              {saveState === "saving"
                ? "Сохраняем…"
                : saveState === "saved"
                  ? "Сохранено"
                  : saveState === "error"
                    ? "Ошибка сохранения"
                    : ""}
            </span>
          </span>
          <span className="selection-readout">
            выделено {formatNumber(sel)} ({formatPercent(markedPct, 1)})
          </span>
          <div className="transport-playback">
            <button
              type="button"
              className="transport-play"
              onClick={togglePlayback}
              aria-pressed={playing}
              aria-label={
                playing
                  ? `Пауза, ${direction === -1 ? "назад" : "вперёд"}, скорость ${speed}`
                  : `Воспроизвести ${direction === -1 ? "назад" : "вперёд"}, скорость ${speed}`
              }
              title={
                playing
                  ? keyTitle(
                      "Пауза",
                      "пробел; R — сменить направление",
                      keyboard,
                    )
                  : keyTitle(
                      "Воспроизвести",
                      "пробел; R — сменить направление",
                      keyboard,
                    )
              }
            >
              <span aria-hidden="true">
                {playing
                  ? direction === -1
                    ? "◀"
                    : "▶"
                  : direction === -1
                    ? "◁"
                    : "▷"}
              </span>
            </button>
            <Popover
              label="Направление и скорость"
              triggerLabel="Направление и скорость"
              triggerTitle={keyTitle(
                "Направление и скорость",
                "R, 0–9",
                keyboard,
              )}
              trigger={
                <>
                  <span className="transport-speed">x{speed}</span>
                  <span className="popover-caret" aria-hidden="true">
                    ▾
                  </span>
                </>
              }
              open={openMenu === "playback"}
              onOpenChange={(v) => setOpenMenu(v ? "playback" : null)}
            >
              <PlaybackMenu
                direction={direction}
                speed={speed}
                showShortcuts={keyboard}
                onDirection={setDirection}
                onSpeed={setSpeed}
              />
            </Popover>
            <span className="sr-only" role="status">
              {playing
                ? direction === -1
                  ? "Воспроизведение назад"
                  : "Воспроизведение вперёд"
                : "Пауза"}
            </span>
          </div>
        </div>
      )}

      <canvas
        ref={statusRef}
        className="statusbar"
        width={STATUS_W}
        height={STATUS_H}
        role="slider"
        tabIndex={0}
        aria-label="Позиция на таймлайне"
        aria-orientation="horizontal"
        aria-valuemin={1}
        aria-valuemax={pair.total_frames}
        aria-valuenow={Math.min(pair.total_frames, position + 1)}
        aria-valuetext={`Кадр ${Math.min(pair.total_frames, position + 1)} из ${pair.total_frames}`}
        title={keyTitle(
          "Клик — переход по кадрам",
          "←/→ — кадры; Home/End — края",
          keyboard,
        )}
        onKeyDown={(e) => {
          const p = modelRef.current;
          if (!p) return;
          // Alt — не наш модификатор; Ctrl разрешён только для стрелок.
          if (e.altKey) return;
          const ctrl = e.ctrlKey || e.metaKey;
          if (ctrl && e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
          // Повторяются стрелки и PageUp/PageDown; Home/End — однократно.
          if (
            e.repeat &&
            e.key !== "ArrowLeft" &&
            e.key !== "ArrowRight" &&
            e.key !== "PageUp" &&
            e.key !== "PageDown"
          ) {
            return;
          }
          // PageUp/PageDown — те же «прыжки по границам фрагментов», что
          // и в остальном редакторе (README/справка), а не ±10 кадров.
          if (e.key === "PageUp" || e.key === "PageDown") {
            e.preventDefault();
            e.stopPropagation();
            setPlaying(false);
            jump(e.key === "PageDown" ? 1 : -1);
            return;
          }
          const step = ctrl ? SEEK_STEP : 1;
          let target: number;
          switch (e.key) {
            // ↑/↓ — не навигация слайдера, а глобальные отметки границ
            // (README/справка): не перехватываем, пусть событие дойдёт до window.
            case "ArrowLeft":
              target = position - step;
              break;
            case "ArrowRight":
              target = position + step;
              break;
            case "Home":
              target = 0;
              break;
            case "End":
              target = p.totalFrames - 1;
              break;
            default:
              return;
          }
          e.preventDefault();
          e.stopPropagation();
          seekTo(target);
        }}
        onClick={(e) => {
          const p = modelRef.current;
          const canvas = e.currentTarget;
          if (!p) return;
          const rect = canvas.getBoundingClientRect();
          const x = (e.clientX - rect.left) * (canvas.width / rect.width);
          const pixel = Math.min(canvas.width - 1, Math.max(0, x));
          seekTo(timelineFrame(pixel, canvas.width, p.totalFrames));
        }}
      />

      {!isFullscreen && <TimelineLegend className="editor-legend" />}

      {message && (
        <div
          className={`flash ${message.kind}`}
          role={message.kind === "error" ? "alert" : "status"}
          aria-atomic="true"
        >
          <span>{message.text}</span>
          {message.retry && (
            <button
              className="flash-retry"
              onClick={() => {
                const retry = message.retry!;
                dismissFlash();
                retry();
              }}
            >
              Повторить
            </button>
          )}
          <button
            className="flash-close"
            onClick={dismissFlash}
            aria-label="Скрыть сообщение"
          >
            <span aria-hidden="true">✕</span>
          </button>
        </div>
      )}

      <HelpModal open={helpOpen} onClose={() => setHelpOpen(false)} />

      {exportItems && (
        <div className="export-list">
          <h3>
            Фрагменты экспортированы (оригиналы):
            <button
              className="export-list-close export-list-trash"
              onClick={handleDeleteExports}
              title="Удалить все экспортированные фрагменты с сервера"
              aria-label="Удалить все экспортированные фрагменты"
            >🗑</button>
            <button
              className="export-list-close"
              onClick={() => handleExport(true)}
              title="Перегнать заново (игнорировать готовые файлы)"
              aria-label="Перегнать фрагменты заново"
            >⟳</button>
            <button
              className="export-list-close"
              onClick={() => setExportItems(null)}
              title="Скрыть список (появится снова после следующего экспорта)"
              aria-label="Скрыть список экспортированных фрагментов"
            >✕</button>
          </h3>
          <ul>
            {exportItems.map((f) => (
              <li key={f.url}>
                <a href={withTokenQuery(f.url)} download>{f.filename}</a>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}