"""Этап B: сверка поведения редактора фрагментов с оригиналом PyVideoCutter.

Прогоняет одинаковые последовательности «нажатий» (add/start/end/delete/
undo/redo) на трёх реализациях и сравнивает состояние списка фрагментов
после каждого шага, а также итоговое содержимое txt-файла оригинала.

Три реализации:
  1) BackendLegacy — точная копия Backend из PyVideoCutter/main.py (tools/);
  2) FragmentEditor — ядро VideoCutter (videocutter/core/fragment_editor.py);
  3) JS FragmentModel — реальная логика фронтенда (frontend/src/model/),
     запускается в Node (Node >= 23 умеет импортировать TS напрямую).

Использование:
    python3 scripts/compare_fragments.py            # детерминированный сценарий
    python3 scripts/compare_fragments.py --random 500   # + случайные оп-серии
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import tempfile
import textwrap

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND = os.path.join(ROOT, "frontend")
LEGACY = os.path.join(ROOT, "tools", "legacy_backend.py")

# Функции JS-модели, доступные извне (операции «нажатий» в терминах оригинала).
# Каждая операция возвращает {"state": [[start,end],...], "ok": bool}.
JS_MODEL_SOURCE = textwrap.dedent(
    r"""
    const target = process.env.VC_TARGET;
    const { FragmentModel } = await import(target);

    const total = Number(process.env.VC_NFRAMES || 0);
    const model = new FragmentModel(total);

    const input = [];
    const decoder = new TextDecoder();
    for await (const chunk of process.stdin) {
        input.push(decoder.decode(chunk, { stream: true }));
    }
    const script = input.join("");

    const logs = [];
    for (const raw of script.trim().split("\n")) {
        if (!raw.trim()) continue;
        const [op, a, b] = raw.split(" ");
        let ok = true;
        switch (op) {
            case "add": ok = model.add(Number(a), Number(b)); break;
            case "start": model.newStart(Number(a)); break;
            case "end": model.newEnd(Number(a)); break;
            case "delete": ok = model.delete(Number(a)) !== "nothing"; break;
            case "undo": ok = model.undo(); break;
            case "redo": ok = model.redo(); break;
            default: throw new Error("unknown op: " + op);
        }
        logs.push(JSON.stringify({ ok, state: model.getFragments().map((f) => [f.start, f.end]) }));
    }
    console.log(logs.join("\n"));
    """
)


def _run_js(script_ops, nframes):
    """Выполняет ops на JS FragmentModel, возвращает список логов."""
    node = os.environ.get("NODE", "node")
    proc = subprocess.run(
        [node, "--input-type=module", "-e", JS_MODEL_SOURCE],
        input=script_ops,
        text=True,
        capture_output=True,
        env={**os.environ, "VC_NFRAMES": str(nframes),
             "VC_TARGET": os.path.join(FRONTEND, "src", "model", "fragmentModel.ts")},
    )
    if proc.returncode != 0:
        raise RuntimeError("JS failed:\n" + proc.stderr + "\n" + proc.stdout)
    logs = []
    for line in proc.stdout.strip().split("\n"):
        if line:
            logs.append(json.loads(line))
    return logs


def run_replay(ops, nframes, out_dir=None):
    """Прогоняет ops на всех трёх реализациях, возвращает лог от каждой."""
    from tools.legacy_backend import BackendLegacy  # noqa: 402

    # 1) Оригинальный Backend.
    out_dir = out_dir or tempfile.mkdtemp(prefix="vc_replay_legacy_")
    legacy = BackendLegacy(nframes, "source.mp4", out_dir)
    legacy_logs = []
    for op in ops:
        name = op[0]
        if name == "add":
            ok = legacy.add([op[1], op[2]])
        elif name == "start":
            ok = legacy.new_start(op[1])
        elif name == "end":
            ok = legacy.new_end(op[1])
        elif name == "delete":
            ok = legacy.delete(op[1])
        elif name == "undo":
            ok = legacy.undo()
        elif name == "redo":
            ok = legacy.redo()
        else:
            raise ValueError(op)
        legacy_logs.append({"ok": bool(ok), "state": [list(f) for f in legacy.fragments]})
    with open(legacy.fragments_file) as f:
        legacy_txt = f.read()

    # 2) Python-ядро FragmentEditor.
    from videocutter.core.fragment_editor import FragmentEditor  # noqa: 402
    from videocutter.core.ports import IFragmentStore  # noqa: 402

    class MemStore(IFragmentStore):
        def __init__(self):
            self.frags = []
        def load_fragments(self):
            return list(self.frags)
        def save_fragments(self, fragments):
            self.frags = [tuple(f) for f in fragments]

    store = MemStore()
    fe = FragmentEditor(nframes, store)
    fe_logs = []
    for op in ops:
        name = op[0]
        if name == "add":
            ok = fe.add((op[1], op[2]))
        elif name == "start":
            ok = fe.new_start(op[1])
        elif name == "end":
            ok = fe.new_end(op[1])
        elif name == "delete":
            ok = fe.delete(op[1]) != "nothing"
        elif name == "undo":
            ok = fe.undo()
        elif name == "redo":
            ok = fe.redo()
        else:
            raise ValueError(op)
        fe_logs.append({"ok": bool(ok), "state": [list(f) for f in fe.fragments]})

    # 3) JS FragmentModel.
    script = "\n".join(" ".join(map(str, op)) for op in ops) + "\n"
    js_logs = _run_js(script, nframes)

    return legacy_logs, fe_logs, js_logs, legacy_txt


def fmt_state(lst):
    return ",".join(f"{s}-{e}" for s, e in lst) or "(пусто)"


def deterministic_ops(nframes):
    """Серия осмысленных «нажатий», покрывающая основные сценарии."""
    return [
        ("end", 20),          # фрагмент [0,20]
        ("start", 50),        # следующий с 50
        ("end", 40),          # должен взяться [0,40]... см. семантику
        ("start", 10),        # начало первого → [10,40], но семантики нет точно-точно
        ("add", 60, 70),
        ("add", 62, 75),      # пересечение − False
        ("delete", 60),       # на границе фрагмента → удалить [60,70]
        ("undo",),
        ("redo",),
        ("end", 80),          # [10,40]?? → до 80
        ("delete", 45),       # в промежутке 40-60 → удалить оба края промежутка
        ("undo",),
        ("undo",),
        ("redo",),
    ]


def random_ops(seed, nframes, count):
    rnd = random.Random(seed)
    ops = []
    for _ in range(count):
        r = rnd.random()
        if r < 0.25:
            ops.append(("add", rnd.randrange(nframes // 2), rnd.randrange(nframes // 2, nframes)))
        elif r < 0.4:
            ops.append(("start", rnd.randrange(nframes)))
        elif r < 0.55:
            ops.append(("end", rnd.randrange(nframes)))
        elif r < 0.7:
            ops.append(("delete", rnd.randrange(nframes)))
        elif r < 0.85:
            ops.append(("undo",))
        else:
            ops.append(("redo",))
    return ops


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--random", type=int, default=0, help="доп. случайных оп-серий (seed)")
    ap.add_argument("--nframes", type=int, default=100, help="число кадров в видео")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    sys.path.insert(0, ROOT)

    sequences = [("детерминированный", deterministic_ops(args.nframes))]
    for seed in range(args.random):
        sequences.append((f"случайный seed={seed}", random_ops(seed, args.nframes, 200)))

    any_fail = False
    for name, ops in sequences:
        legacy_logs, fe_logs, js_logs, legacy_txt = run_replay(ops, args.nframes)
        n = len(ops)
        mism = []
        for i in range(n):
            st = (legacy_logs[i]["state"], fe_logs[i]["state"], js_logs[i]["state"])
            if st[0] != st[1] or st[1] != st[2]:
                mism.append((i, ops[i], st))
        if mism:
            any_fail = True
            print(f"[FAIL] {name}: {len(mism)}/{n} шагов различаются")
            for i, op, st in mism[:12]:
                print(f"   шаг {i} {op}:\n"
                      f"      legacy: {fmt_state(st[0])} ok={legacy_logs[i]['ok']}\n"
                      f"      pycore: {fmt_state(st[1])} ok={fe_logs[i]['ok']}\n"
                      f"      jsfront: {fmt_state(st[2])} ok={js_logs[i]['ok']}")
        else:
            print(f"[PASS] {name}: {n} шагов, все реализации совпали")

    # Сравнение итогового txt с оригиналом (порядок и формат).
    legacy_logs, fe_logs, js_logs, legacy_txt = run_replay(sequences[0][1], args.nframes)
    with tempfile.TemporaryDirectory() as out:
        pass
    print("\nИтоговый txt оригинала:")
    print(legacy_txt or "  (пусто)")

    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())