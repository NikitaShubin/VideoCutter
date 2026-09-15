/* Тесты чистой отрисовки статус-бара (общей для редактора и превью списка):
 * фон, фрагменты, затемнение «после текущего», выделение, маркер — в том же
 * порядке и с теми же пикселями, что draw_statusbar из PyVideoCutter.
 *
 * Запуск: node scripts/test_statusbar.mjs   (Node >= 23)
 * Возвращает код 0 при успехе, 1 при провале любого ассерта.
 */

import assert from "node:assert/strict";
import { statusbarOps } from "../frontend/src/model/statusbar.ts";

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

const W = 100;
const H = 20;
const T = 10; // 10 кадров, как в тестовом видео

const base = {
  width: W,
  height: H,
  totalFrames: T,
  fragments: [
    { start: 0, end: 3 },
    { start: 5, end: 9 },
  ],
  position: 7,
};

check("первая операция — зелёный фон на весь canvas", () => {
  const ops = statusbarOps(base);
  assert.equal(ops[0].fill, "#00ff00");
  assert.deepEqual({ x: ops[0].x, y: ops[0].y, w: ops[0].w, h: ops[0].h }, { x: 0, y: 0, w: W, h: H });
});

check("фрагменты — красные, правая граница включена (end-start+1)", () => {
  const reds = statusbarOps(base).filter((o) => o.fill === "#ff0000");
  assert.equal(reds.length, 2);
  // timelinePix(0)=0, timelinePix(3)=33 → ширина 34
  assert.deepEqual({ x: reds[0].x, w: reds[0].w }, { x: 0, w: 34 });
  // timelinePix(5)=55, timelinePix(9)=99 → ширина 45
  assert.deepEqual({ x: reds[1].x, w: reds[1].w }, { x: 55, w: 45 });
});

check("затемнение стартует на колонку правее позиции (overlayStart)", () => {
  const dim = statusbarOps(base).find((o) => o.fill === "rgba(0,0,0,0.5)");
  // timelinePix(7)=77 → 78, w = 100-78 = 22
  assert.deepEqual({ x: dim.x, w: dim.w }, { x: 78, w: 22 });
});

check("маркер — белая линия в 1px на пикселе позиции, последней операцией", () => {
  const ops = statusbarOps(base);
  const cur = ops[ops.length - 1];
  assert.equal(cur.fill, "#ffffff");
  assert.deepEqual({ x: cur.x, w: cur.w, h: cur.h }, { x: 77, w: 1, h: H });
});

check("keyPose не задан — синего выделения нет", () => {
  const extras = statusbarOps({ ...base, keyPose: null }).filter((o) => o.fill === "#0000ff");
  assert.equal(extras.length, 0);
});

check("keyPose → синий диапазон с lighter между ключевой точкой и позицией", () => {
  const sel = statusbarOps({ ...base, keyPose: 2 }).find((o) => o.fill === "#0000ff");
  assert.equal(sel.op, "lighter");
  // timelinePix(2)=22, timelinePix(7)=77 → w=55
  assert.deepEqual({ x: sel.x, w: sel.w }, { x: 22, w: 55 });
});

check("пустые фрагменты → только фон, затемнение и маркер", () => {
  const ops = statusbarOps({ ...base, fragments: [] });
  assert.equal(ops.filter((o) => o.fill === "#ff0000").length, 0);
  assert.equal(ops.length, 3);
});

check("один кадр (total=1) не падает и красит курсор в 0", () => {
  const ops = statusbarOps({ ...base, totalFrames: 1, position: 0, fragments: [{ start: 0, end: 0 }] });
  const cur = ops[ops.length - 1];
  assert.equal(cur.x, 0);
  assert.equal(ops.find((o) => o.fill === "#ff0000").w, 1);
});

console.log(`ok: ${passed} проверок`);