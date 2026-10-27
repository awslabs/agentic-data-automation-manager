//  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
//  SPDX-License-Identifier: Apache-2.0

import js from "@eslint/js";
import { defineConfig, globalIgnores } from "eslint/config";
import reactHooks from "eslint-plugin-react-hooks";
import { reactRefresh } from "eslint-plugin-react-refresh";
import security from "eslint-plugin-security";
import globals from "globals";
import tseslint from "typescript-eslint";

export default defineConfig(
  // aws-exports.ts is generated from aws-exports.ts.template by build_website_deployment.sh.
  globalIgnores(["dist/", "node_modules/", "src/aws-exports.ts"]),
  {
    files: ["**/*.{ts,tsx}"],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommendedTypeChecked,
      reactHooks.configs.flat["recommended-latest"],
      reactRefresh.configs.vite(),
      security.configs.recommended,
    ],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
      parserOptions: {
        // vite.config.ts and the tests sit outside tsconfig.json's "src" include and run on Node.
        projectService: { allowDefaultProject: ["vite.config.ts", "tests/*.test.ts"], defaultProject: "tsconfig.node.json" },
        tsconfigRootDir: import.meta.dirname,
      },
    },
    rules: {
      // Untyped, so it flags every obj[key]; here keys are numeric indices or typed unions that tsc checks.
      "security/detect-object-injection": "off",
    },
  },
  {
    files: ["tests/*.test.ts"],
    // node:test runs every test() it is given; the promise test() returns needs no await.
    rules: {
      "@typescript-eslint/no-floating-promises": ["error", {
        allowForKnownSafeCalls: [{ from: "package", name: "test", package: "node:test" }],
      }],
    },
  },
  {
    files: ["**/*.js"],
    extends: [js.configs.recommended],
    languageOptions: { globals: globals.node },
  },
  {
    files: ["**/*.cjs"],
    extends: [js.configs.recommended],
    languageOptions: { globals: globals.node, sourceType: "commonjs" },
  },
);
