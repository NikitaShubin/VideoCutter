import { useState } from "react";
import { clearAuthToken, listPairs, setAuthToken } from "./api";

// Дверь локального контура (VC_AUTH_TOKEN): один общий токен на всех,
// без логинов и ролей. Проверяется первым же запросом списка.
export function LoginScreen({ onOk }: { onOk: () => void }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const token = value.trim();
    if (!token || busy) return;
    setBusy(true);
    setError("");
    setAuthToken(token);
    listPairs()
      .then(() => onOk())
      .catch(() => {
        clearAuthToken();
        setError("Неверный токен");
        setBusy(false);
      });
  };

  return (
    <div className="login-wrap">
      <form className="login" onSubmit={submit}>
        <h2>VideoCutter</h2>
        <p className="login-hint">Доступ защищён токеном. Введите его для входа.</p>
        <label>
          Токен доступа
          <input
            type="password"
            value={value}
            autoFocus
            autoComplete="current-password"
            onChange={(e) => setValue(e.target.value)}
          />
        </label>
        {error && <div className="login-error">{error}</div>}
        <button type="submit" disabled={!value.trim() || busy}>
          {busy ? "Проверка…" : "Войти"}
        </button>
      </form>
    </div>
  );
}
