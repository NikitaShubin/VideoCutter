import type { VideoPair } from "../types";

interface Props {
  pairs: VideoPair[];
  onSelect: (id: number) => void;
  onUpload: () => void;
}

export function PairList({ pairs, onSelect, onUpload }: Props) {
  return (
    <div className="pair-list">
      <h2>Видео-пары</h2>
      <button onClick={onUpload}>Загрузить пару</button>
      <ul>
        {pairs.length === 0 && <li className="empty">Пока нет пар.</li>}
        {pairs.map((p) => (
          <li key={p.id}>
            <button onClick={() => onSelect(p.id)}>
              {p.original_name} · {p.visualization_name || ""} · {p.total_frames} кадров · {p.width}×{p.height}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}