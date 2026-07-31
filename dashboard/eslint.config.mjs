import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTypeScript from "eslint-config-next/typescript";

// Next.js 16 removed `next lint` and no longer lints during `next build`, so
// linting runs through the ESLint CLI (`npm run lint`) with this flat config.
// core-web-vitals carries the React + Next rule sets; typescript adds the
// @typescript-eslint rules (without which unused-vars and no-explicit-any go
// unchecked on .ts/.tsx).
const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTypeScript,
  globalIgnores([
    ".next/**",
    "out/**",
    "build/**",
    "node_modules/**",
    "next-env.d.ts",
  ]),
]);

export default eslintConfig;
