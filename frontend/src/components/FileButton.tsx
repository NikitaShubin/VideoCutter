import { useRef } from "react";

interface Props {
  className?: string;
  accept?: string;
  disabled?: boolean;
  title?: string;
  onFile: (file: File) => void;
  children: React.ReactNode;
}

// Кнопка выбора файла: видимый <button> + скрытый input. Label с hidden
// input не фокусируется с клавиатуры, поэтому файловые действия были
// доступны только мышью.
export function FileButton({
  className,
  accept,
  disabled,
  title,
  onFile,
  children,
}: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  return (
    <>
      <button
        type="button"
        className={className}
        disabled={disabled}
        title={title}
        onClick={() => inputRef.current?.click()}
      >
        {children}
      </button>
      <input
        ref={inputRef}
        type="file"
        hidden
        accept={accept}
        onChange={(e) => {
          const file = e.target.files?.[0] ?? null;
          if (file) onFile(file);
          e.target.value = "";
        }}
      />
    </>
  );
}
