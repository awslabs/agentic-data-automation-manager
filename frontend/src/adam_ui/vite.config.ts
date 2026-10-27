//  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
//  SPDX-License-Identifier: Apache-2.0

import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // The local dev server's address is the sign-in callback every stage but prod accepts
  // (cdk_infra/shared/stage.py LOCAL_FRONTEND_URL); the page calls the deployed API itself.
  server: { port: 5173, strictPort: true },
});
