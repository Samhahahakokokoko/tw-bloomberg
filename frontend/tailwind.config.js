/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx,ts,tsx}"],
  theme: {
    extend: {
      colors: {
        terminal: {
          bg: "#0a0e1a",
          surface: "#0f1629",
          border: "#1e3a5f",
          accent: "#00d4ff",
          green: "#00ff88",
          red: "#ff4466",
          yellow: "#ffcc00",
          text: "#c8d8e8",
          muted: "#4a6080",
        },
      },
      fontFamily: {
        mono: ["JetBrains Mono", "Consolas", "monospace"],
      },
    },
  },
  plugins: [],
};
