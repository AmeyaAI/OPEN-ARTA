/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ['./src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        void: '#060811',
        deep: '#0a0f1e',
        surface: '#0e1528',
        raised: '#141d35',
        indigo: '#6366f1',
        violet: '#8b5cf6',
        cyan: '#06b6d4',
        emerald: '#10b981',
        amber: '#f59e0b',
        rose: '#f43f5e',
      },
      fontFamily: {
        sans: ['DM Sans', 'sans-serif'],
        mono: ['JetBrains Mono', 'monospace'],
        display: ['Space Mono', 'monospace'],
      },
      animation: {
        'pulse-ring': 'pulse-ring 3s ease-in-out infinite',
        'orb-pulse': 'orb-pulse 6s ease-in-out infinite',
      },
    },
  },
  plugins: [],
};
