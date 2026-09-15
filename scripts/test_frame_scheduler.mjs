/* Тесты планировщика кадров: только самый свежий кадр встаёт на экран,
 * даже при быстрой навигации и неупорядоченной доставке; кэш LRU;
 * последовательное воспроизведение без пропуска кадров.
 *
 * Запуск: node scripts/test_frame_scheduler.mjs   (Node >= 23)
 * Возвращает код 0 при успехе, 1 при провале любого ассерта.
 */

import assert from "node:assert/strict";
import {
  FrameScheduler,
  nextPlayPosition,
  pickLoadTarget,
  retargetOnDirectionChange,
  segmentBoundaryForward,
  timelineFrame,
  timelinePix,
} from "../frontend/src/model/frameScheduler.ts";

let passed = 0;

function check(name, fn) {
  try {
    fn();
    passed++;
  } catch (err) {
    console.error(`[FAIL] ${name}`);
    throw err;
  }
}

// --- быстрая навигация: показывается только свежий кадр ---
check("быстрая навигация вперёд: показывается последний кадр, старые отбрасываются", () => {
  const s = new FrameScheduler();
  const a = s.begin(5);
  const b = s.begin(6); // пользователь нажал ещё раз до ответа
  assert.equal(a.cached, false);
  assert.equal(b.cached, false);
  // Ответы пришли НЕ в порядке запросов: кадр 5 позже кадра 6.
  assert.equal(s.deliver(6, b.gen, "u6"), true);
  s.show(6);
  assert.equal(s.deliver(5, a.gen, "u5"), false); // устарел
  assert.equal(s.shownIndex, 6);
  assert.equal(s.hasCached(6), true);
  assert.equal(s.hasCached(5), false); // устаревший кадр не кэшируется
});

check("доставка в порядке запросов тоже корректна", () => {
  const s = new FrameScheduler();
  const a = s.begin(5);
  assert.equal(s.deliver(5, a.gen, "u5"), true);
  s.show(5);
  const b = s.begin(6);
  assert.equal(s.deliver(6, b.gen, "u6"), true);
  s.show(6);
  assert.equal(s.shownIndex, 6);
});

check("навигация назад-вперёд: побеждает самая свежая цель", () => {
  const s = new FrameScheduler();
  const a = s.begin(10);
  const b = s.begin(9);
  const c = s.begin(10); // снова вперёд до прихода ответа
  assert.equal(s.deliver(9, b.gen, "u9"), false);
  assert.equal(s.deliver(10, a.gen, "u10-a"), false); // старше c.gen
  assert.equal(s.deliver(10, c.gen, "u10-c"), true);
  s.show(10);
  assert.equal(s.shownIndex, 10);
  assert.equal(s.hasCached(10), true);
  assert.equal(s.hasCached(9), false);
});

// --- кэш ---
check("попадание в кэш синхронно и не стартует сетевую загрузку", () => {
  const s = new FrameScheduler();
  const a = s.begin(3);
  assert.equal(s.deliver(3, a.gen, "u3"), true);
  s.show(3);
  const b = s.begin(3);
  assert.equal(b.cached, true);
  const c = s.begin(4);
  assert.equal(c.cached, false);
});

check("показ кадра актуализирует wanted (кэш-хит повторно не считается загрузкой)", () => {
  const s = new FrameScheduler();
  const a = s.begin(1);
  s.deliver(1, a.gen, "u1");
  s.show(1);
  assert.equal(s.begin(1).cached, true);
});

check("LRU кэш вытесняет старые кадры при переполнении", () => {
  const s = new FrameScheduler(3);
  for (let i = 0; i < 4; i++) {
    const r = s.begin(i);
    assert.equal(s.deliver(i, r.gen, `u${i}`), true);
    s.show(i);
  }
  assert.equal(s.hasCached(0), false);
  assert.equal(s.hasCached(1), true);
  assert.equal(s.hasCached(2), true);
  assert.equal(s.hasCached(3), true);
});

// --- воспроизведение: позиция меняется только после показа кадра ---
check("покадровое воспроизведение проходит все кадры без пропуска", () => {
  const s = new FrameScheduler();
  const total = 5;
  let pos = 0;
  let dir = 1;
  const steps = [];
  while (true) {
    const { gen } = s.begin(pos);
    // «Быстрый сервер»: кадр приходит сразу и становится показанным.
    assert.equal(s.deliver(pos, gen, `u${pos}`), true);
    s.show(pos);
    assert.equal(s.shownIndex, pos);
    steps.push(pos);
    const step = nextPlayPosition(pos, dir, total);
    if (step.stop) {
      dir = step.direction;
      break;
    }
    pos = step.pos;
    dir = step.direction;
  }
  assert.deepEqual(steps, [0, 1, 2, 3, 4]); // все кадры, ни один не пропущен
  assert.equal(dir, -1); // на конце — разворот
});

check("отставание сервера: должен показаться крайний показанный кадр, без прыжков вперёд", () => {
  const s = new FrameScheduler();
  const a = s.begin(0);
  assert.equal(s.deliver(0, a.gen, "u0"), true);
  s.show(0);
  // Позиция ушла вперёд (пользователь нажал 3 раза), но кадры ещё качаются.
  const b = s.begin(1);
  assert.equal(s.deliver(1, b.gen, "u1"), true);
  s.show(1);
  assert.equal(s.shownIndex, 1); // показан кадр, который реально пришёл
});

// --- границы воспроизведения ---
check("границы: стоп и разворот на обоих концах", () => {
  assert.deepEqual(nextPlayPosition(0, -1, 10), { pos: 0, direction: 1, stop: true });
  assert.deepEqual(nextPlayPosition(9, 1, 10), { pos: 9, direction: -1, stop: true });
  assert.deepEqual(nextPlayPosition(4, 1, 10), { pos: 5, direction: 1, stop: false });
  assert.deepEqual(nextPlayPosition(4, -1, 10), { pos: 3, direction: -1, stop: false });
});

check("nextPlayPosition: total=0/1 не падает", () => {
  assert.deepEqual(nextPlayPosition(0, 1, 1), { pos: 0, direction: -1, stop: true });
});

// --- streaming (chase — удержание стрелки) ---
check("streaming: кадры, продвигающие экран вперёд, показываются по мере прихода", () => {
  const s = new FrameScheduler();
  // Экран на 4, пользователь удерживает «→», позиция убежала на 9.
  const a = s.begin(4);
  assert.equal(s.deliver(4, a.gen, "u4"), true);
  s.show(4);
  assert.equal(s.deliverStreaming(5, "u5", 1), true); // догоняющий
  assert.equal(s.shownIndex, 5);
  assert.equal(s.deliverStreaming(7, "u7", 1), true); // ещё вперёд
  assert.equal(s.shownIndex, 7);
  assert.equal(s.deliverStreaming(9, "u9", 1), true); // догнал позицию
  assert.equal(s.shownIndex, 9);
});

check("streaming: поздний «обратный» ответ не откатывает экран назад", () => {
  const s = new FrameScheduler();
  s.show(6);
  assert.equal(s.deliverStreaming(5, "u5", 1), false);
  assert.equal(s.shownIndex, 6);
  assert.equal(s.hasCached(5), true); // кадр закэширован, но не показан
});

check("streaming назад: направление удержания учитывается", () => {
  const s = new FrameScheduler();
  s.show(20);
  assert.equal(s.deliverStreaming(17, "u17", -1), true); // догоняет влево
  assert.equal(s.shownIndex, 17);
  assert.equal(s.deliverStreaming(15, "u15", -1), true); // ещё левее
  assert.equal(s.shownIndex, 15);
  assert.equal(s.deliverStreaming(4, "u4", -1), true); // догнал позицию
  assert.equal(s.shownIndex, 4);
  assert.equal(s.deliverStreaming(18, "u18", -1), false); // выше — не в сторону позиции
  assert.equal(s.shownIndex, 4);
});

// --- pickLoadTarget: drain/jump ---
check("pickLoadTarget: drain грузит следующий кадр по пути, не целевой", () => {
  assert.equal(pickLoadTarget({ shown: 4, position: 9, mode: "drain" }), 5);
  assert.equal(pickLoadTarget({ shown: 4, position: 4, mode: "drain" }), 4);
  assert.equal(pickLoadTarget({ shown: 9, position: 4, mode: "drain" }), 8); // влево
});

check("pickLoadTarget: jump грузит сразу целевую позицию", () => {
  assert.equal(pickLoadTarget({ shown: 4, position: 120, mode: "jump" }), 120);
  assert.equal(pickLoadTarget({ shown: 120, position: 4, mode: "jump" }), 4);
});

check("drain: серия тапов показывает ВСЕ промежуточные кадры без пропуска", () => {
  const s = new FrameScheduler();
  s.show(3);
  let pos = 3;
  // Пользователь быстро нажал «→» 3 раза: позиция мгновенно 6.
  pos = 6;
  const shownOrder = [s.shownIndex];
  while (pos !== s.shownIndex) {
    const target = pickLoadTarget({ shown: s.shownIndex, position: pos, mode: "drain" });
    const { gen } = s.begin(target);
    assert.equal(s.deliver(target, gen, `u${target}`), true);
    s.show(target);
    shownOrder.push(s.shownIndex);
  }
  assert.deepEqual(shownOrder, [3, 4, 5, 6]);
});

check("drain: стрим-догоняние после буфера допустимо с дропом (chase не ждёт дренаж)", () => {
  const s = new FrameScheduler();
  s.show(2);
  const pos = 8; // позиция убежала, буфер тапов «брошен» (переход на удержание)
  // Chase: показывается любой продвигающий кадр, даже если 3,4,5 не пришли.
  assert.equal(s.deliverStreaming(6, "u6", 1), true);
  assert.equal(s.shownIndex, 6);
  assert.equal(s.deliverStreaming(8, "u8", 1), true);
  assert.equal(s.shownIndex, 8); // догнал — промежуточные дропнуты
});

// --- segmentBoundaryForward: J — прокрутка до ближайшей границы ---
// Фрагменты: [100,200], [300,350]; total=400. Границы: 0,100,200,300,350,399.
const KF = [100, 200, 300, 350];
const TOTAL = 400;

check("J: внутри фрагмента — вперёд к концу, назад к началу", () => {
  assert.equal(segmentBoundaryForward(150, 1, KF, TOTAL), 200);
  assert.equal(segmentBoundaryForward(150, -1, KF, TOTAL), 100);
});

check("J: в промежутке — вперёд к следующему началу, назад к прошлому концу", () => {
  assert.equal(segmentBoundaryForward(260, 1, KF, TOTAL), 300);
  assert.equal(segmentBoundaryForward(260, -1, KF, TOTAL), 200);
});

check("J: уже на границе — уводит к следующей строгой", () => {
  assert.equal(segmentBoundaryForward(200, 1, KF, TOTAL), 300);
  assert.equal(segmentBoundaryForward(100, -1, KF, TOTAL), 0);
  assert.equal(segmentBoundaryForward(350, 1, KF, TOTAL), 399);
});

check("J: до первого фрагмента назад → 0, после последнего вперёд → последний кадр", () => {
  assert.equal(segmentBoundaryForward(50, -1, KF, TOTAL), 0);
  assert.equal(segmentBoundaryForward(380, 1, KF, TOTAL), 399);
});

check("J: без фрагментов — до краёв видео", () => {
  assert.equal(segmentBoundaryForward(150, 1, [], TOTAL), 399);
  assert.equal(segmentBoundaryForward(150, -1, [], TOTAL), 0);
});

check("J: границы у краёв не дублируются (0/total-1 не ломают фильтрацию)", () => {
  // Фрагмент начинается с 0 и кончается последним кадром.
  assert.equal(segmentBoundaryForward(5, 1, [0, 399], TOTAL), 399);
  assert.equal(segmentBoundaryForward(398, -1, [0, 399], TOTAL), 0);
});

// --- пересчёт цели J при смене направления в полёте (R во время прогона) ---
check("R в полёте: активный J пересчитывает цель под новое направление", () => {
  // Баг: цель считалась один раз при нажатии J; смена направления в процессе
  // воспроизведения её не обновляла, и устаревшая цель обратным ходом не
  // достигалась — стопа на границе не было. Теперь цель = ближайшая граница
  // в НОВОМ направлении (та же функция сегментирования).
  // Вперёд к 300, посреди (270) жмём R → цель 200 (строго ниже, а не 300).
  assert.equal(retargetOnDirectionChange(300, 270, -1, KF, TOTAL), 200);
  // Назад к 200, посреди (250) жмём R → цель 300 (строго выше).
  assert.equal(retargetOnDirectionChange(200, 250, 1, KF, TOTAL), 300);
  // Неактивный J: цель не создаётся — направление просто переворачивается.
  assert.equal(retargetOnDirectionChange(null, 250, -1, KF, TOTAL), null);
  // Смена, стоя на границе 200: строгая следующая (100), а не сам кадр —
  // не мгновенный автостоп на месте.
  assert.equal(retargetOnDirectionChange(300, 200, -1, KF, TOTAL), 100);
});

// Модель эффекта воспроизведения CutEditor.tsx в чистом виде: while играет —
// каждый тик проверяет position+direction===target (J-стоп), на естественном
// крае разворот и остановка (цель сбрасывается), иначе шаг на ±1 кадр.
function simulateJ(startPos, startDir, target, flipAtPos, retargetOnFlip) {
  let pos = startPos;
  let dir = startDir;
  let t = target;
  let reason = "max-ticks";
  for (let i = 0; i < TOTAL * 2 && reason === "max-ticks"; i++) {
    if (flipAtPos !== null && pos === flipAtPos) {
      dir = dir === 1 ? -1 : 1;
      if (retargetOnFlip) t = retargetOnDirectionChange(t, pos, dir, KF, TOTAL);
    }
    if (t !== null && pos + dir === t) {
      pos = t;
      reason = "j-stop";
      break;
    }
    const step = nextPlayPosition(pos, dir, TOTAL);
    if (step.stop) {
      t = null;
      dir = step.direction;
      reason = "edge-stop";
      break;
    }
    pos = step.pos;
  }
  return { pos, dir, target: t, reason };
}

check("J в полёте: смена направления в середине прогона — стоп на пересчитанной границе (регрессия)", () => {
  // Отчёт: J вперёд от 260 к 300, на 270 жмём R — обратного стопа не было.
  // Без пересчёта прогон доезжал до края 0, разворачивался и лишь потом
  // (после повторного пуска) добирал устаревшую цель 300.
  const fixed = simulateJ(260, 1, segmentBoundaryForward(260, 1, KF, TOTAL), 270, true);
  assert.equal(fixed.reason, "j-stop");
  assert.equal(fixed.pos, 200); // ближайшая граница в обратном направлении
  assert.equal(fixed.dir, -1);

  // Контроль: если бы пересчёта не было (target оставался 300), обратный ход
  // НЕ даёт j-stop на границе — прогон оканчивается естественным краем.
  const broken = simulateJ(260, 1, segmentBoundaryForward(260, 1, KF, TOTAL), 270, false);
  assert.notEqual(broken.reason, "j-stop");
  assert.notEqual(broken.pos, 200);

  // Симметрично: J назад от 400 → цель 350, посреди (на самой 350) жмём R →
  // цель становится 399 вперёд, стоп на последней границе.
  const revFix = simulateJ(400, -1, segmentBoundaryForward(400, -1, KF, TOTAL), 350, true);
  assert.equal(revFix.reason, "j-stop");
  assert.equal(revFix.pos, 399);

  // Без смены направления прямое J стопает ровно на границе прошлого хода.
  const straight = simulateJ(260, 1, segmentBoundaryForward(260, 1, KF, TOTAL), null, false);
  assert.equal(straight.reason, "j-stop");
  assert.equal(straight.pos, 300);
});

// --- timelinePix / timelineFrame (полоса) ---
check("timelinePix: края и середины отображаются без выхода за полосу", () => {
  assert.equal(timelinePix(0, 800, 705), 0);
  assert.equal(timelinePix(704, 800, 705), 799); // последний кадр = правый край
  assert.equal(timelinePix(352, 800, 705), Math.round(352 * 799 / 704));
  assert.equal(timelinePix(200, 800, 705), Math.round(200 * 799 / 704));
});

check("timelinePix: монотонно не убывает", () => {
  let prev = -1;
  for (let f = 0; f < 705; f++) {
    const p = timelinePix(f, 200, 705);
    assert.ok(p >= prev, `timelinePix не монотонен на кадре ${f}`);
    prev = p;
  }
});

check("timelineFrame: round-trip через timelinePix", () => {
  const width = 800;
  const total = 705;
  for (let f = 0; f < total; f++) {
    const pix = timelinePix(f, width, total);
    assert.ok(timelineFrame(pix, width, total) >= 0);
    assert.ok(timelineFrame(pix, width, total) <= total - 1);
  }
  assert.equal(timelineFrame(0, width, total), 0);
  assert.equal(timelineFrame(width - 1, width, total), total - 1);
});

check("полоса: маркер позиции — тот же timelinePix, что и границы (без сдвигов и прилипания)", () => {
  // Тёмная зона «от текущего кадра» стартует ровно с timelinePix(position) —
  // тем же способом, что и границы сегментов. Никаких спец-случаев (+1 на
  // конце сегмента): иначе маркер прилипал к границе и +1 кадр не менял полосу.
  const width = 800;
  const total = 705;
  // самый первый кадр: вся полоса затемнена (светлой зоны «до» нет)
  assert.equal(timelinePix(0, width, total), 0);
  // соседние кадры не склеиваются в один пиксель — каждый +1 сдвигает маркер
  let prev = -1;
  for (let f = 0; f < total - 1; f++) {
    assert.notEqual(timelinePix(f, width, total), timelinePix(f + 1, width, total),
      `прилипание: кадры ${f}/${f + 1} в одном пикселе`);
  }
  // позиция на границе сегмента (crowd): маркер в той же колонке, что граница
  for (const b of [120, 200, 310, 500, 704]) {
    assert.equal(timelinePix(b, width, total), timelinePix(b, width, total));
    assert.ok(timelinePix(b, width, total) >= 0 && timelinePix(b, width, total) <= width - 1);
  }
});

console.log(`ok: ${passed} проверок`);