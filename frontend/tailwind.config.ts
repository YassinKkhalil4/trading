import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        profit: "#10b981",
        risk: "#f43f5e",
        system: "#0ea5e9",
      },
    },
  },
  plugins: [],
};

export default config;
