import { useCallback, useEffect, useState } from "react";
import { getCache, setCache, type CacheState } from "../api";

// Ручка управления GOP-кэшем (RAM): заполнение + лимиты без рестарта.
// Лимиты стартовые — из автотюна (доли машины, видны в title);
// ручная смена переживает только до рестарта бэкенда.
export function CacheControl() {
  const [cache, setCacheState] = useState<CacheState | null>(null);
  const [mbDraft, setMbDraft] = useState("");
  const [gopsDraft, setGopsDraft] = useState("");
  const [error, setError] = useState("");
  const [open, setOpen] = useState(false);

  const refresh = useCallback(() => {
    getCache()
      .then((c) => {
        setCacheState(c);
        setError("");
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  useEffect(() => {
    refresh();
    const id = window.setInterval(refresh, 5000);
    return () => window.clearInterval(id);
  }, [refresh]);

  const apply = useCallback(() => {
    const patch: { mb?: number; gops?: number } = {};
    const mb = parseInt(mbDraft, 10);
    const gops = parseInt(gopsDraft, 10);
    if (Number.isFinite(mb) && mb > 0) patch.mb = mb;
    if (Number.isFinite(gops) && gops > 0) patch.gops = gops;
    if (Object.keys(patch).length === 0) return;
    setCache(patch)
      .then((c) => {
        setCacheState(c);
        setError("");
        setMbDraft("");
        setGopsDraft("");
      })
      .catch((e: Error) => setError(e.message));
  }, [mbDraft, gopsDraft]);

  if (!cache) return null;
  const u = cache.usage;
  const pct = Math.min(100, Math.round((100 * u.mb) / Math.max(1, u.mb_cap)));
  const srcMb = cache.tuning.source["VC_GOP_CACHE_MB"] ?? "?";
  const srcGops = cache.tuning.source["VC_GOP_CACHE_GOPS"] ?? "?";
  const perSource = Object.entries(u.per_source ?? {})
    .map(([k, v]) => `${k}: ${v.mb}МБ`)
    .join(", ");

  return (
    <span className="info" style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
      <button
        className="toolbar-help"
        onClick={() => setOpen((v) => !v)}
        title={`GOP-кэш (RAM): ${u.gops}/${u.gops_cap} групп, ${u.mb}/${u.mb_cap}МБ на ${u.sources} источниках${perSource ? ` (${perSource})` : ""}. Лимиты: МБ [${srcMb}], GOP [${srcGops}] (auto — из автотюна). Раскрыть ручку лимитов.`}
      >
        💾 {u.mb}/{u.mb_cap}МБ
      </button>
      <span
        style={{
          display: "inline-block", width: 48, height: 6,
          background: "#333", borderRadius: 3, overflow: "hidden",
        }}
        title={`Заполнение кэша: ${pct}%`}
      >
        <span style={{ display: "block", width: `${pct}%`, height: "100%", background: pct > 90 ? "#c33" : "#6a6" }} />
      </span>
      {open && (
        <span style={{ display: "inline-flex", gap: 4, alignItems: "center" }}>
          <label title="Лимит МБ (1–16384). Уменьшение тут же вытесняет лишнее.">
            МБ
            <input
              type="number" min={16} max={16384} step={64}
              value={mbDraft} placeholder={String(cache.caps.mb)}
              onChange={(e) => setMbDraft(e.target.value)}
              style={{ width: 64 }}
            />
          </label>
          <label title="Лимит групп (1–1024)">
            GOP
            <input
              type="number" min={1} max={1024} step={1}
              value={gopsDraft} placeholder={String(cache.caps.gops)}
              onChange={(e) => setGopsDraft(e.target.value)}
              style={{ width: 48 }}
            />
          </label>
          <button className="toolbar-help" onClick={apply} title="Применить лимиты без рестарта">
            ✓
          </button>
          {error && <span title={error}>⚠</span>}
        </span>
      )}
    </span>
  );
}
