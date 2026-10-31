import { useCallback, useEffect, useState } from "react";
import { getCache, setCache, type CacheState } from "../api";
import { formatNumber, formatPercent } from "../model/format";

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
    .map(([k, v]) => `${k}: ${formatNumber(v.mb)} МБ`)
    .join(", ");

  return (
    <div className="cache-control">
      <button
        type="button"
        className="cache-summary"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-label={`GOP-кэш: ${formatNumber(u.mb)} из ${formatNumber(u.mb_cap)} МБ, ${formatNumber(u.gops)} из ${formatNumber(u.gops_cap)} групп. ${open ? "Свернуть лимиты" : "Открыть лимиты"}`}
        title={`GOP-кэш (RAM): ${formatNumber(u.gops)} / ${formatNumber(u.gops_cap)} групп, ${formatNumber(u.mb)} / ${formatNumber(u.mb_cap)} МБ на ${formatNumber(u.sources)} источниках${perSource ? ` (${perSource})` : ""}. Лимиты: МБ [${srcMb}], GOP [${srcGops}] (auto — из автотюна). Раскрыть ручку лимитов.`}
      >
        <span aria-hidden="true">💾</span>
        <span className="cache-summary-title">GOP-кэш</span>
        <span className="cache-summary-value">
          {formatNumber(u.mb)} / {formatNumber(u.mb_cap)} МБ
        </span>
        <span className="cache-summary-value">
          {formatNumber(u.gops)} / {formatNumber(u.gops_cap)} групп
        </span>
      </button>
      <div
        className="cache-meter"
        title={`Заполнение кэша: ${formatPercent(pct)}`}
        aria-hidden="true"
      >
        <span
          style={{
            width: `${pct}%`,
            background: pct > 90 ? "#c33" : "#6a6",
          }}
        />
      </div>
      {open && (
        <div className="cache-limits">
          <label title="Лимит МБ (1–16384). Уменьшение тут же вытесняет лишнее.">
            МБ
            <input
              type="number"
              min={16}
              max={16384}
              step={64}
              value={mbDraft}
              placeholder={String(cache.caps.mb)}
              onChange={(e) => setMbDraft(e.target.value)}
            />
          </label>
          <label title="Лимит групп (1–1024)">
            GOP
            <input
              type="number"
              min={1}
              max={1024}
              step={1}
              value={gopsDraft}
              placeholder={String(cache.caps.gops)}
              onChange={(e) => setGopsDraft(e.target.value)}
            />
          </label>
          <button
            type="button"
            className="cache-apply"
            onClick={apply}
            title="Применить лимиты без рестарта"
            aria-label="Применить лимиты"
          >
            <span aria-hidden="true">✓</span>
          </button>
          {error && (
            <span
              className="cache-error"
              role="status"
              aria-label={`Ошибка кэша: ${error}`}
              title={error}
            >
              <span aria-hidden="true">⚠</span>
            </span>
          )}
        </div>
      )}
    </div>
  );
}
