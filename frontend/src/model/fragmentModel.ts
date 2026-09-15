/** Логика списка фрагментов (перенос FragmentEditor из ядра).
 *
 * Чистый TS-модуль без React — чтобы поведение можно было воспроизводить
 * из Node-тестов и сравнивать с оригинальным Backend из PyVideoCutter.
 */

export interface Fragment {
  start: number;
  end: number;
  comment: string;
}

export class FragmentModel {
  totalFrames: number;
  history: Fragment[][] = [];
  histPos = 0;
  fragments: Fragment[] = [];

  constructor(totalFrames: number) {
    this.totalFrames = totalFrames;
    this.history = [[]];
    this.histPos = 0;
    this.fragments = [];
  }

setInitial(fragments: Fragment[]): void {
      this.fragments = fragments.map((f) => ({
        start: f.start,
        end: f.end,
        comment: f.comment ?? "",
      }));
      this.history = [this.fragments.map((f) => ({ ...f }))];
      this.histPos = 0;
    }

  private push() {
    this.history = this.history
      .slice(0, this.histPos + 1)
      .concat([this.fragments.map((f) => ({ ...f }))]);
    this.histPos = this.history.length - 1;
  }

  getFragments() {
    return this.fragments.map((f) => ({ ...f }));
  }

  /** Фрагмент, содержащий position (или undefined). */
  fragmentAt(position: number): Fragment | undefined {
    return this.fragments.find((f) => position >= f.start && position <= f.end);
  }

  /** Ставит комментарий фрагменту, содержащему position; false, если позиция вне фрагментов. */
  setComment(position: number, text: string): boolean {
    const f = this.fragmentAt(position);
    if (!f) return false;
    f.comment = text;
    this.push();
    return true;
  }

  add(start: number, end: number): boolean {
    // Точная логика оригинала: список ведётся отсортированным, вставка по
    // индексу; наложение проверяется только до первой «целиком большей»
    // границы (если список рассинхронизирован — поведение 1-в-1 с Backend).
    let newInd = 0;
    for (let ind = 0; ind < this.fragments.length; ind++) {
      const f = this.fragments[ind];
      if (start > f.end) {
        newInd = ind + 1;
      } else if (end < f.start) {
        newInd = ind;
        break;
      } else {
        return false;
      }
    }
    this.fragments = [
      ...this.fragments.slice(0, newInd),
      { start, end, comment: "" },
      ...this.fragments.slice(newInd),
    ];
    this.push();
    return true;
  }

  newStart(position: number): void {
    for (let i = 0; i < this.fragments.length; i++) {
      if (this.fragments[i].end >= position) {
        this.fragments[i] = { ...this.fragments[i], start: position };
        this.push();
        return;
      }
    }
    this.fragments.push({ start: position, end: this.totalFrames - 1, comment: "" });
    this.push();
  }

  newEnd(position: number): void {
    for (let i = this.fragments.length - 1; i >= 0; i--) {
      if (this.fragments[i].start <= position) {
        this.fragments[i] = { ...this.fragments[i], end: position };
        this.push();
        return;
      }
    }
    // Все начала правее position: новый фрагмент встаёт в начало списка,
    // иначе сортировка ломается и следующие правки границ бьют в неверный.
    this.fragments.unshift({ start: 0, end: position, comment: "" });
    this.push();
  }

  delete(position: number): "ok" | "merged" | "nothing" | "ambiguous" {
    if (this.fragments.length === 0) return "nothing";

    const point = this.fragments.find((f) => f.start === position && f.end === position);
    if (point) {
      this.fragments = this.fragments.filter((f) => f !== point);
      this.push();
      return "ok";
    }

    const onEdge = this.fragments.filter((f) => position === f.start || position === f.end);
    if (onEdge.length === 1) {
      this.fragments = this.fragments.filter((f) => f !== onEdge[0]);
      this.push();
      return "ok";
    }
    if (onEdge.length === 2) return "ambiguous";

    if (position < this.fragments[0].start) {
      this.fragments[0] = { ...this.fragments[0], start: 0 };
      this.push();
      return "ok";
    }
    if (position > this.fragments[this.fragments.length - 1].end) {
      const last = this.fragments[this.fragments.length - 1];
      this.fragments[this.fragments.length - 1] = { ...last, end: this.totalFrames - 1 };
      this.push();
      return "ok";
    }

    // Позиция строго внутри фрагмента или между двумя. Воспроизводит результат
    // оригинала (плоский список границ): если позиция внутри фрагмента — он
    // удаляется целиком; если в промежутке — два соседних сливаются. В отличие
    // от оригинала, комментарии при слиянии склеиваются через перенос строки.
    for (let i = 0; i < this.fragments.length; i++) {
      const f = this.fragments[i];
      if (f.start < position && position < f.end) {
        this.fragments = this.fragments.filter((_, k) => k !== i);
        this.push();
        return "ok";
      }
      if (i + 1 < this.fragments.length) {
        const g = this.fragments[i + 1];
        if (f.end < position && position < g.start) {
          this.fragments = [
            ...this.fragments.slice(0, i),
            { ...f, end: g.end, comment: [f.comment, g.comment].filter(Boolean).join("\n") },
            ...this.fragments.slice(i + 2),
          ];
          this.push();
          return "merged";
        }
      }
    }
    throw new Error("Ошибка логики программы!");
  }

  undo(): boolean {
    if (this.histPos === 0) return false;
    this.histPos -= 1;
    this.fragments = this.history[this.histPos].map((f) => ({ ...f }));
    return true;
  }

  redo(): boolean {
    if (this.histPos === this.history.length - 1) return false;
    this.histPos += 1;
    this.fragments = this.history[this.histPos].map((f) => ({ ...f }));
    return true;
  }

  keyFrames(): number[] {
    const ks = new Set<number>();
    this.fragments.forEach((f) => {
      ks.add(f.start);
      ks.add(f.end);
    });
    return Array.from(ks).sort((a, b) => a - b);
  }

  selectedFrames(): number {
    return this.fragments.reduce((acc, f) => acc + (f.end - f.start), 0);
  }
}