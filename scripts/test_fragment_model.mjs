/* Тесты JS-модели фрагментов: границы, удаление, комментарии, история.
 *
 * Запуск: node scripts/test_fragment_model.mjs   (Node >= 23 умеет импортировать TS)
 * Возвращает код 0 при успехе, 1 при провале любого ассерта.
 *
 * Покрывает «механику»: add/newStart/newEnd/delete, setComment, undo/redo
 * (включая восстановление комментариев), setInitial и отдачу через getFragments.
 */

import assert from "node:assert/strict";
import { FragmentModel } from "../frontend/src/model/fragmentModel.ts";

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

function model() {
  return new FragmentModel(100);
}

const fr = (m) => m.getFragments();
const range = (m) => fr(m).map((f) => [f.start, f.end]);

// --- add: вставка по порядку и отказ при пересечении ---
check("add вставляет отсортированно", () => {
  const m = model();
  assert.equal(m.add(40, 60), true);
  assert.equal(m.add(10, 20), true);
  assert.equal(m.add(70, 90), true);
  assert.deepEqual(range(m), [[10, 20], [40, 60], [70, 90]]);
});

check("add отклоняет пересечения и точки-фрагменты", () => {
  const m = model();
  assert.equal(m.add(10, 20), true);
  assert.equal(m.add(15, 25), false); // пересечение слева
  assert.equal(m.add(5, 15), false);  // пересечение справа
  assert.equal(m.add(21, 30), true);
  assert.equal(m.add(30, 30), false); // стык/точка пересекается
});

// --- newStart / newEnd ---
check("newStart/newEnd правят границы и создают с нуля", () => {
  const m2 = model();
  m2.newEnd(20); // [0,20]
  m2.newStart(50); // [0,20],[50,99]
  m2.newEnd(30); // по возрастанию start? reversed: последний start<=30 это [0,20] → [0,30]
  m2.newStart(10); // первый end>=10 — [0,30] → [10,30]
  assert.deepEqual(range(m2), [[10, 30], [50, 99]]);
});

check("регресс: newEnd в начало списка не ломает сортировку и границы", () => {
  // Было: [ :=60, ], :=55 создавал [0,55] в КОНЦЕ списка → [[60,99],[0,55]];
  // следующий [ :=45 правил не тот фрагмент → [[45,99],[0,55]] (перекрытие 45..55).
  const m = model();
  m.newStart(60); // [60,99]
  m.newEnd(55);   // [0,55] должен встать В НАЧАЛО
  m.newStart(45); // первый конец>=45 — [0,55] → [45,55]
  m.newEnd(90);   // правый сегмент → [60,90]
  assert.deepEqual(range(m), [[45, 55], [60, 90]]);
});

check("регресс: newEnd левее всех вставляет в начало даже при двух фрагментах", () => {
  const m = model();
  m.newStart(60); // [60,99]
  m.newEnd(55);   // [0,55] в начало
  m.newStart(45); // [45,55]
  m.newEnd(30);   // нет start <= 30 → [0,30] в начало (не в конец!)
  assert.deepEqual(range(m), [[0, 30], [45, 55], [60, 99]]);
});

// --- delete ---
check("delete удаляет фрагмент по позиции внутри", () => {
  const m = model();
  m.add(10, 20);
  m.add(30, 40);
  assert.equal(m.delete(15), "ok");
  assert.deepEqual(range(m), [[30, 40]]);
});

check("delete на стыке даёт ambiguous", () => {
  // Смежные фрагменты add не создаст (пересечение на границе), но они могут
  // прийти из файла — setInitial повторяет чтение без валидации, как Backend.load.
  const m = model();
  m.setInitial([
    { start: 10, end: 20, comment: "" },
    { start: 20, end: 30, comment: "" },
  ]);
  assert.equal(m.delete(20), "ambiguous");
  assert.deepEqual(range(m), [[10, 20], [20, 30]]);
});

check("delete в промежутке сливает соседние фрагменты", () => {
  const m = model();
  m.add(10, 20);
  m.add(30, 40);
  assert.equal(m.delete(25), "merged");
  assert.deepEqual(range(m), [[10, 40]]);
});

check("delete с границы удаляет весь фрагмент", () => {
  const m = model();
  m.add(10, 20);
  m.add(30, 40);
  assert.equal(m.delete(20), "ok");
  assert.deepEqual(range(m), [[30, 40]]);
});

check("delete левее всех расширяет первый, правее — последний", () => {
  const m = model();
  m.add(10, 20);
  m.add(30, 40);
  assert.equal(m.delete(5), "ok");
  assert.deepEqual(range(m), [[0, 20], [30, 40]]);
  assert.equal(m.delete(50), "ok");
  assert.deepEqual(range(m), [[0, 20], [30, 99]]);
});

check("delete пустого списка — nothing", () => {
  assert.equal(model().delete(5), "nothing");
});

// --- комментарии ---
check("setComment ставит комментарий фрагменту по позиции", () => {
  const m = model();
  m.add(10, 20);
  m.add(30, 40);
  assert.equal(m.setComment(15, "первый кусок"), true);
  assert.equal(m.setComment(35, "второй кусок"), true);
  assert.deepEqual(
    fr(m).map((f) => [f.start, f.end, f.comment]),
    [[10, 20, "первый кусок"], [30, 40, "второй кусок"]],
  );
});

check("setComment вне фрагментов — false", () => {
  const m = model();
  m.add(10, 20);
  assert.equal(m.setComment(25, "в пустоте"), false);
  assert.equal(m.setComment(9, "левее"), false);
});

check("изменение границ новым фрагментом не трогает комментарии других", () => {
  const m = model();
  m.add(10, 20);
  m.add(30, 40);
  m.setComment(15, "первый");
  m.newStart(12); // [12,20]
  m.newEnd(18); // [12,18]
  assert.deepEqual(
    fr(m).map((f) => [f.start, f.end, f.comment]),
    [[12, 18, "первый"], [30, 40, ""]],
  );
});

check("создание нового фрагмента по newStart/newEnd — пустой комментарий", () => {
  const m = model();
  m.newEnd(20);
  assert.deepEqual(fr(m).map((f) => f.comment), [""]);
});

check("delete в промежутке склеивает комментарии обоих фрагментов", () => {
  const m = model();
  m.add(10, 20);
  m.add(30, 40);
  m.setComment(15, "первый");
  m.setComment(35, "второй");
  m.delete(25);
  assert.deepEqual(
    fr(m).map((f) => [f.start, f.end, f.comment]),
    [[10, 40, "первый\nвторой"]],
  );
});

check("delete в промежутке берёт непустой комментарий", () => {
  const m = model();
  m.add(10, 20);
  m.add(30, 40);
  m.setComment(35, "второй");
  m.delete(25);
  assert.deepEqual(
    fr(m).map((f) => [f.start, f.end, f.comment]),
    [[10, 40, "второй"]],
  );
});

check("delete в промежутке при пустых комментариях не создаёт пустых строк", () => {
  const m = model();
  m.add(10, 20);
  m.add(30, 40);
  m.delete(25);
  assert.deepEqual(
    fr(m).map((f) => [f.start, f.end, f.comment]),
    [[10, 40, ""]],
  );
});

// --- история ---
check("undo/redo восстанавливает границы", () => {
  const m = model();
  m.add(10, 20);
  m.add(30, 40);
  m.delete(15);
  assert.deepEqual(range(m), [[30, 40]]);
  assert.equal(m.undo(), true);
  assert.deepEqual(range(m), [[10, 20], [30, 40]]);
  assert.equal(m.redo(), true);
  assert.deepEqual(range(m), [[30, 40]]);
  assert.equal(m.redo(), false);
});

check("undo/redo восстанавливает комментарии", () => {
  const m = model();
  m.add(10, 20);
  m.setComment(15, "важно");
  assert.deepEqual(fr(m).map((f) => f.comment), ["важно"]);
  assert.equal(m.undo(), true);
  assert.deepEqual(fr(m).map((f) => f.comment), [""]);
  assert.equal(m.undo(), true); // вернулись к пустому началу (add тоже в истории)
  assert.equal(m.redo(), true);
  assert.deepEqual(fr(m).map((f) => [f.start, f.end, f.comment]), [[10, 20, ""]]);
  assert.equal(m.redo(), true);
  assert.deepEqual(fr(m).map((f) => [f.start, f.end, f.comment]), [[10, 20, "важно"]]);
});

check("новая операция после undo отбрасывает ветку redo", () => {
  const m = model();
  m.add(10, 20);
  m.add(30, 40);
  m.undo();
  m.add(50, 60); // историю после undo обрезает
  assert.equal(m.redo(), false);
  assert.deepEqual(range(m), [[10, 20], [50, 60]]);
});

check("setComment — отдельная точка истории", () => {
  const m = model();
  m.add(10, 20);
  m.setComment(15, "v1");
  m.setComment(15, "v2");
  assert.equal(m.undo(), true);
  assert.deepEqual(fr(m).map((f) => f.comment), ["v1"]);
  assert.equal(m.undo(), true);
  assert.deepEqual(fr(m).map((f) => f.comment), [""]);
  // delete после комментариев не теряет их по истории
  m.redo();
  m.redo();
  m.delete(15);
  assert.equal(m.undo(), true);
  assert.deepEqual(fr(m).map((f) => [f.start, f.end, f.comment]), [[10, 20, "v2"]]);
});

// --- setInitial ---
check("setInitial подхватывает комментарии и сбрасывает историю", () => {
  const m = model();
  m.add(5, 6);
  m.setInitial([{ start: 1, end: 2, comment: "стартовый" }]);
  assert.deepEqual(fr(m).map((f) => f.comment), ["стартовый"]);
  assert.equal(m.undo(), false);
});

check("setInitial дополняет отсутствующие комментарии пустой строкой", () => {
  const m = model();
  m.setInitial([{ start: 1, end: 2 }, { start: 4, end: 5, comment: "x" }]);
  assert.deepEqual(
    fr(m).map((f) => [f.start, f.end, f.comment]),
    [[1, 2, ""], [4, 5, "x"]],
  );
});

// --- вспомогательные расчёты ---
check("keyFrames и selectedFrames", () => {
  const m = model();
  m.add(10, 20);
  m.add(30, 40);
  assert.deepEqual(m.keyFrames(), [10, 20, 30, 40]);
  assert.equal(m.selectedFrames(), (20 - 10) + (40 - 30));
});

check("fragmentAt находит фрагмент, содержащий позицию", () => {
  const m = model();
  m.add(10, 20);
  assert.equal(m.fragmentAt(10).comment, "");
  assert.equal(m.fragmentAt(15).start, 10);
  assert.equal(m.fragmentAt(25), undefined);
});

console.log(`ok: ${passed} проверок`);