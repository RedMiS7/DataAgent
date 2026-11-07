import { createContext, useEffect, useState } from "react";

export type Theme = "light" | "dark";

export const ThemeContext = createContext<Theme>("light");

const STORAGE_KEY = "dataagent.theme";

function initialTheme(): Theme {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === "light" || saved === "dark") return saved;
  } catch {
    // Fall through to the system preference.
  }
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function useTheme(): { theme: Theme; toggle: () => void } {
  const [theme, setTheme] = useState<Theme>(initialTheme);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      // The theme then applies to this page only.
    }
  }, [theme]);

  return { theme, toggle: () => setTheme((value) => (value === "light" ? "dark" : "light")) };
}
