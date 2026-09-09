import type { VideoPair } from "../types";

interface Props {
  pairs: VideoPair[];
  onSelect: (id: string) => void;
}

export function PairList({ pairs, onSelect }: Props) {
  return (
    <div className="pair-list">
      <h2>Рабочие пространства</h2>
      <ul>
        {pairs.length === 0 && <li className="empty">Пока нет workspace-ов.</li>}
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
