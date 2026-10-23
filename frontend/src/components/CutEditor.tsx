import { useCallback, useEffect, useRef, useState } from "react";
import {
  annotationsUrl,
  cancelExport,
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
import { FragmentModel } from "../model/fragmentModel";
import { CacheControl } from "./CacheControl";
import { HelpModal } from "./HelpModal";
import { Hint } from "./Hint";
import {
  FrameScheduler,
  nextPlayPosition,
  retargetOnDirectionChange,
  segmentBoundaryForward,
  sweepStaleInflight,
  timelineFrame,
} from "../model/frameScheduler";
import { paintStatusbar } from "../model/statusbar";
import { type ExportItem, type ExportStatus, type VideoPairDetail } from "../types";

interface Props {
  pairId: string;
  onBack: () => void;
}

export function CutEditor({ pairId, onBack }: Props) {
  const [pair, setPair] = useState<VideoPairDetail | null>(null);
  const [position, setPosition] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [direction, setDirection] = useState<1 | -1>(1);
  const [keyPose, setKeyPose] = useState<number | null>(null);
  const [preserveAspect, setPreserveAspect] = useState(true);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [exportItems, setExportItems] = useState<ExportItem[] | null>(null);
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
  const [message, setMessage] = useState("");
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
  const [commentDraft, setCommentDraft] = useState("");
  const [helpOpen, setHelpOpen] = useState(false);
  const helpOpenRef = useRef(false);
  helpOpenRef.current = helpOpen;

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

  // Синхронизация состояния с полноэкранным режимом браузера (вкл/выкл через Esc).
  useEffect(() => {
    const onChange = () => setIsFullscreen(document.fullscreenElement === editorRef.current);
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, []);

  const toggleFullscreen = () => {
    if (document.fullscreenElement) {
      document.exitFullscreen().catch(() => {});
    } else {
      editorRef.current?.requestFullscreen().catch(() => {});
    }
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
  const drawStatusbar = useCallback(() => {
    const canvas = statusRef.current;
    const p = modelRef.current;
    if (!canvas || !p) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    paintStatusbar(ctx, {
      width: canvas.width,
      height: canvas.height,
      totalFrames: p.totalFrames,
      fragments: p.fragments,
      position,
      keyPose,
    });
  }, [keyPose, position]);

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

  const jump = (dir: 1 | -1) => {
    const p = modelRef.current;
    if (!p) return;
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

  const flash = (m: string) => {
    setMessage(m);
    window.setTimeout(() => setMessage(""), 2000);
  };

  const commentRef = useRef<HTMLTextAreaElement | null>(null);
  const committedRef = useRef(false);

  const startCommentEdit = () => {
    const p = modelRef.current;
    if (!p) return;
    const f = p.fragmentAt(position);
    if (!f) {
      flash("Комментарии можно добавлять только к выделенным участкам");
      return;
    }
    setCommentDraft(f.comment);
    setEditingComment(true);
    committedRef.current = false;
    window.setTimeout(() => commentRef.current?.focus(), 0);
  };

  const commitComment = () => {
    const p = modelRef.current;
    if (!p || committedRef.current) return;
    committedRef.current = true;
    setEditingComment(false);
    if (p.setComment(position, commentDraft)) {
      rerender();
      drawStatusbar();
      saveToDb(p.getFragments());
    }
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

      // Справка закрывает все остальные горячие клавиши, пока открыта.
      if (helpOpenRef.current) {
        if (is("Escape") || is("KeyQ") || is("KeyH")) setHelpOpen(false);
        else e.preventDefault();
        return;
      }

      const isEditing = (e.target as HTMLElement)?.tagName === "INPUT" || (e.target as HTMLElement)?.tagName === "TEXTAREA";
      if (isEditing) return;

      // Навигация.
      const rightKey = is("ArrowRight") || is("Period");
      const leftKey = is("ArrowLeft") || is("Comma");
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
        cancelPending();
        setPlaying((v) => !v);
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
      } else if (/^Digit[0-9]$/.test(code)) {
        setSpeed(Math.pow(2, parseInt(code.slice(5), 10)));
      } else if (is("KeyF")) {
        toggleFullscreen();
      } else if (is("Tab")) {
        e.preventDefault();
        setPreserveAspect((v) => !v);
      } else if (is("KeyH") || (is("Slash") && e.shiftKey)) {
        setHelpOpen(true);
      }

      // Отметка фрагментов.
      else if (is("ArrowUp") || is("BracketLeft")) {
        p.newStart(position);
        rerender();
        drawStatusbar();
        saveToDb(p.getFragments());
      } else if (is("ArrowDown") || is("BracketRight")) {
        p.newEnd(position);
        rerender();
        drawStatusbar();
        saveToDb(p.getFragments());
      } else if (is("KeyK") || is("Insert") || is("F12")) {
        if (keyPose === null) {
          if (p.fragments.some((f) => position >= f.start && position <= f.end)) {
            flash("Невозможно создать фрагмент внутри другого");
          } else {
            setKeyPose(position);
          }
        } else {
          const [lo, hi] = keyPose < position
            ? [keyPose, position]
            : [position, keyPose];
          const ok = p.add(lo, hi);
          if (ok) {
            setKeyPose(null);
            rerender();
            drawStatusbar();
            saveToDb(p.getFragments());
          } else {
            flash("Фрагмент пересекается с существующим");
          }
        }
      } else if (is("Delete") || is("KeyD")) {
        const res = p.delete(position);
        if (res === "ok" || res === "merged") {
          rerender();
          drawStatusbar();
          saveToDb(p.getFragments());
          if (res === "merged") startCommentEdit();
        } else if (res === "ambiguous") {
          flash("Текущий кадр — стык двух фрагментов. Переместите курсор.");
        }
      } else if (is("KeyI")) {
        startCommentEdit();
      } else if (ctrl && is("KeyZ")) {
        // Ctrl+Shift+Z — стандартный redo, иначе undo.
        if (e.shiftKey) p.redo();
        else p.undo();
        rerender();
        drawStatusbar();
        saveToDb(p.getFragments());
      } else if (is("KeyZ")) {
        p.redo();
        rerender();
        drawStatusbar();
        saveToDb(p.getFragments());
      } else if (is("KeyE")) {
        handleExport();
      } else if (is("Escape") || is("KeyQ")) {
        if (!document.fullscreenElement) handleBack();
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

  const saveToDb = (frags: { start: number; end: number; comment: string }[]) => {
    replaceFragments(pairId, frags, position)
      .then(() => flash("Сохранено"))
      .catch((e) => flash(`Ошибка сохранения: ${e.message}`));
  };

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
        flash(`Ошибка опроса экспорта: ${(e as Error).message}`);
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
        flash(`Ошибка экспорта: ${st.error ?? "неизвестно"}`);
      } else {
        // idle: сервер забыл статус (рестарт) — дальше ждать нечего.
        finishExportUi();
        flash("Экспорт прерван (нет статуса на сервере)");
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
        flash(`Не удалось отменить экспорт: ${(e as Error).message}`);
      }
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
        if (st.state === "error") flash(`Ошибка экспорта: ${st.error ?? "неизвестно"}`);
        else flash("Экспорт не запущен");
      }
    } catch (e) {
      finishExportUi();
      flash(`Ошибка экспорта: ${(e as Error).message}`);
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
      .catch((e: Error) => flash(`Ошибка импорта разметки: ${e.message}`))
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
      .catch((e: Error) => flash(`Ошибка скачивания: ${e.message}`))
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
          flash(`Ошибка экспорта: ${st.error ?? "неизвестно"}`);
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
        <div>⚠ {loadError}</div>
        <button onClick={onBack}>← Назад к списку</button>
      </div>
    );
  }

  if (!pair) {
    return (
      <div className="loading">
        {waitingIndex
          ? `Индексируется…${waitPct != null ? ` ${Math.round(waitPct * 100)}%` : ""}`
          : "Загрузка видео-пары…"}
      </div>
    );
  }

  const sel = modelRef.current?.selectedFrames() ?? 0;

  // Индикатор загрузки — только при ручной навигации. При воспроизведении
  // задержек не показываем: кадры идут все подряд в темпе железа.
  const frameLoading = !playing && position !== shownFrame;

  return (
    <div ref={editorRef} className={isFullscreen ? "editor fullscreen" : "editor"}>
      {!isFullscreen && (
        <div className="editor-toolbar">
          <button onClick={handleBack}>← Назад</button>
          <span className="pair-name">
            {pair.name || pairId}: источник {pair.source_name}
            {pair.preview_name !== pair.source_name && (
              <span> · Превью: {pair.preview_name}</span>
            )}
          </span>
          <span className="view-settings">
            <label className="view-slider">
              Масштаб
              <input
                type="range"
                min={5}
                max={100}
                step={5}
                value={Math.round(scale * 100)}
                onChange={(e) => setScale(parseInt(e.target.value, 10) / 100)}
                title="Масштаб кадров (меньше — быстрее)"
              />
              <b>{Math.round(scale * 100)}%</b>
            </label>
            <label className="view-slider">
              Качество
              <input
                type="range"
                min={30}
                max={95}
                step={1}
                value={quality}
                onChange={(e) => setQuality(parseInt(e.target.value, 10))}
                title="Качество JPEG (меньше — быстрее)"
              />
              <b>{quality}</b>
            </label>
          </span>
          <span className="info">
            кадр {position + 1}/{pair.total_frames} · выделено {sel} ({(100 * sel / pair.total_frames).toFixed(1)}%)
          </span>
          <span
            className="info"
            title={playing ? (direction === -1 ? "идёт назад (R — сменить)" : "идёт вперёд (R — сменить)") : (direction === -1 ? "пауза (пробел), направление: назад" : "пауза (пробел), направление: вперёд")}
            aria-label={playing ? (direction === -1 ? "воспроизведение назад" : "воспроизведение вперёд") : (direction === -1 ? "пауза, направление: назад" : "пауза, направление: вперёд")}
          >{playing ? (direction === -1 ? "◀" : "▶") : (direction === -1 ? "◁" : "▷")}x{speed}</span>
          <span
            className="info"
            title="Замер клиента за секунду: к/с показа · мс загрузки кадра (сеть + сервер + декод браузера) · мс показа (рендер до отрисовки). Сервер тёплое отдаёт за миллисекунды: если ↓ мало, а к/с низкие — упираемся в рендер браузера."
          >⏱ {perf.fps}/с · ↓{perf.load}мс · ⊙{perf.show}мс</span>
          <CacheControl />
          {pair.pair_warning && (
            <span className="info warn" title={pair.pair_warning}>
              ⚠ {pair.pair_warning}
            </span>
          )}
          <button
            className="toolbar-export"
            onClick={() => handleExport()}
            title={exporting ? "Отменить экспорт (E)" : "Экспорт (E)"}
          >
            {exportProgress !== null && (
              <span
                className="toolbar-export-progress"
                style={{ width: `${Math.round(exportProgress * 100)}%` }}
              />
            )}
            <span className="toolbar-export-label">
              {exportProgress !== null
                ? `${Math.round(exportProgress * 100)}% · Отмена`
                : "Экспорт (E)"}
            </span>
          </button>
          <Hint
            className="toolbar-btn"
            text="Экспорт разметки — скачать fragments.tsv + манифест (без видео)"
            pin={false}
            focusable={false}
          >
            <button
              className="toolbar-btn-plain"
              disabled={dlState !== null || annBusy}
              onClick={() =>
                handleDownload(
                  "ann",
                  annotationsUrl(pairId),
                  `${pairId}-annotations.zip`,
                )
              }
            >
              {dlState?.key === "ann" ? (
                <>
                  <span className="btn-spin" />
                  {dlState.frac !== null
                    ? ` ${Math.round(dlState.frac * 100)}%`
                    : ""}
                </>
              ) : (
                "⤓ Разметка"
              )}
            </button>
          </Hint>
          <Hint
            className="toolbar-btn"
            text="Импорт разметки — применить архив разметки к задаче (видео не трогается)"
            pin={false}
            focusable={false}
          >
            <label className="toolbar-btn-plain">
              {annProgress !== null
                ? `Разметка ${Math.round(annProgress * 100)}%`
                : "⤒ Импорт"}
              <input
                type="file"
                hidden
                accept=".zip,application/zip"
                onChange={(e) => {
                  const f = e.target.files?.[0] ?? null;
                  if (f) handleAnnotationsImport(f);
                  e.target.value = "";
                }}
              />
            </label>
          </Hint>
          <Hint
            className="toolbar-btn"
            text="Экспорт бэкапа — скачать задачу целиком (видео + разметка + паспорт)"
            pin={false}
            focusable={false}
          >
            <button
              className="toolbar-btn-plain"
              disabled={dlState !== null || annBusy}
              onClick={() =>
                handleDownload(
                  "bak",
                  taskBackupUrl(pairId),
                  `${pairId}-backup.zip`,
                )
              }
            >
              {dlState?.key === "bak" ? (
                <>
                  <span className="btn-spin" />
                  {dlState.frac !== null
                    ? ` ${Math.round(dlState.frac * 100)}%`
                    : ""}
                </>
              ) : (
                "⤓ Бэкап"
              )}
            </button>
          </Hint>
          <button
            className="toolbar-help"
            onClick={() => setHelpOpen(true)}
            title="Справка (H)"
          >
            ?
          </button>
        </div>
      )}

      <div className="video-area">
        <img
          ref={imageRef}
          alt=""
          className={frameLoading ? "frame-loading" : undefined}
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
          <div className="frame-wait" aria-hidden>
            <span className="frame-ring" />
            {frameStuck && (
              <span className="frame-wait-text">
                Кадр не загрузился — перейдите на соседний кадр и вернитесь
              </span>
            )}
          </div>
        )}
        {(!isFullscreen || editingComment) && (
          editingComment ? (
            <div className="editor-comment">
              <textarea
                ref={commentRef}
                rows={3}
                value={commentDraft}
                placeholder="Комментарий к сегменту…"
                onChange={(e) => setCommentDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Escape") {
                    e.preventDefault();
                    setEditingComment(false);
                  } else if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    commitComment();
                  }
                }}
                onBlur={commitComment}
              />
            </div>
          ) : modelRef.current?.fragmentAt(position)?.comment ? (
            <div className="editor-comment">
              <span className="comment-text">
                {modelRef.current?.fragmentAt(position)?.comment}
              </span>
            </div>
          ) : null
        )}
      </div>

      <canvas
        ref={statusRef}
        className="statusbar"
        width={800}
        height={20}
        onClick={(e) => {
          const p = modelRef.current;
          const canvas = e.currentTarget;
          if (!p) return;
          const rect = canvas.getBoundingClientRect();
          const x = (e.clientX - rect.left) * (canvas.width / rect.width);
          const pixel = Math.min(canvas.width - 1, Math.max(0, x));
          const target = Math.min(
            p.totalFrames - 1,
            timelineFrame(pixel, canvas.width, p.totalFrames),
          );
          setPlaying(false);
          jumpMode();
          setPosition(target);
        }}
      />

      {!isFullscreen && message && <div className="flash">{message}</div>}

      <HelpModal open={helpOpen} onClose={() => setHelpOpen(false)} />

      {!isFullscreen && exportItems && (
        <div className="export-list">
          <h3>
            Фрагменты экспортированы (оригиналы):
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