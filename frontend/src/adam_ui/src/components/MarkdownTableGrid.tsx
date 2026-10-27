//  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
//  SPDX-License-Identifier: Apache-2.0

import { useMemo, useRef, useCallback } from "react";
import ReactMarkdown from "react-markdown";
import { AgGridReact } from "ag-grid-react";
import { ActionIcon, Group, useComputedColorScheme } from "@mantine/core";
import { IconCopy } from "@tabler/icons-react";
import {
  ClientSideRowModelModule,
  CsvExportModule,
  ModuleRegistry,
  TextFilterModule,
  themeQuartz,
} from "ag-grid-community";
import { extractMarkdownTables, type ParsedTable } from "./markdownTables";

// Only what these grids use: client-side rows, the text filter (every cell is text) and CSV copy.
ModuleRegistry.registerModules([ClientSideRowModelModule, TextFilterModule, CsvExportModule]);

const lightTheme = themeQuartz;
const darkTheme = themeQuartz.withParams({
  backgroundColor: "#1a1b1e",
  foregroundColor: "#c1c2c5",
  headerBackgroundColor: "#25262b",
  headerTextColor: "#c1c2c5",
  rowHoverColor: "#2c2e33",
  borderColor: "#373a40",
});

function TableGrid({ table }: { table: ParsedTable }) {
  const gridRef = useRef<AgGridReact>(null);
  const dark = useComputedColorScheme("dark") === "dark";

  const handleCopy = useCallback(() => {
    if (gridRef.current?.api) {
      const csv = gridRef.current.api.getDataAsCsv();
      if (csv) void navigator.clipboard.writeText(csv);
    }
  }, []);

  const columnDefs = useMemo(
    () =>
      table.headers.map((h, i) => ({
        headerName: h.replace(/\*\*/g, ""),
        field: `col${i}`,
        flex: 1,
        minWidth: 120,
        sortable: true,
        filter: true,
        resizable: true,
      })),
    [table.headers]
  );

  const rowData = useMemo(
    () =>
      table.rows.map((row) => {
        const obj: Record<string, string> = {};
        table.headers.forEach((_, i) => {
          obj[`col${i}`] = (row[i] || "").replace(/\*\*/g, "");
        });
        return obj;
      }),
    [table.headers, table.rows]
  );

  return (
    <div style={{ width: "100%", marginTop: 8, marginBottom: 8 }}>
      <Group justify="flex-end" mb={2}>
        <ActionIcon size="xs" variant="subtle" onClick={handleCopy} title="Copy as CSV" aria-label="Copy as CSV">
          <IconCopy size={12} />
        </ActionIcon>
      </Group>
      <div
        style={{
          width: "100%",
          height: Math.min(400, 44 + rowData.length * 40),
          borderRadius: 8,
          overflow: "hidden",
          border: "1px solid var(--mantine-color-default-border)",
        }}
      >
        <AgGridReact
          ref={gridRef}
          theme={dark ? darkTheme : lightTheme}
          columnDefs={columnDefs}
          rowData={rowData}
          domLayout="normal"
          suppressMovableColumns={true}
          headerHeight={36}
          rowHeight={36}
        />
      </div>
    </div>
  );
}

export function MarkdownWithTables({ markdown }: { markdown: string }) {
  const segments = useMemo(() => extractMarkdownTables(markdown), [markdown]);

  if (segments.length === 0) {
    return (
      <div style={{ fontSize: 14 }}>
        <ReactMarkdown>{markdown}</ReactMarkdown>
      </div>
    );
  }

  return (
    <div>
      {segments.map((seg, i) => (
        <div key={i}>
          {seg.before && (
            <div style={{ fontSize: 14 }}>
              <ReactMarkdown>{seg.before}</ReactMarkdown>
            </div>
          )}
          <TableGrid table={seg.table} />
          {seg.after && (
            <div style={{ fontSize: 14 }}>
              <ReactMarkdown>{seg.after}</ReactMarkdown>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
