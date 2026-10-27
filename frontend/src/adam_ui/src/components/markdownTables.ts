//  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
//  SPDX-License-Identifier: Apache-2.0

// Kept out of MarkdownTableGrid.tsx so that file exports only components (React Fast Refresh).

export interface ParsedTable {
  headers: string[];
  rows: string[][];
}

function parseMarkdownTable(tableText: string): ParsedTable | null {
  const lines = tableText.trim().split("\n").map((l) => l.trim()).filter(Boolean);
  if (lines.length < 3) return null;

  const parseLine = (line: string): string[] =>
    line
      .replace(/^\|/, "")
      .replace(/\|$/, "")
      .split("|")
      .map((cell) => cell.trim());

  const headers = parseLine(lines[0]);

  const separator = lines[1];
  if (!/^[\s|:-]+$/.test(separator)) return null;

  const rows: string[][] = [];
  for (let i = 2; i < lines.length; i++) {
    const cells = parseLine(lines[i]);
    if (cells.length > 0) {
      rows.push(cells);
    }
  }

  if (headers.length === 0 || rows.length === 0) return null;
  return { headers, rows };
}

export function extractMarkdownTables(text: string): { before: string; table: ParsedTable; after: string }[] {
  const segments: { before: string; table: ParsedTable; after: string }[] = [];
  // A row ends at a newline or the end of the text (an answer often ends on its last row); a blank line ends the table.
  // Up to three spaces may indent a row (a table inside a list item); four make it a code block.
  // eslint-disable-next-line security/detect-unsafe-regex -- every repeat must end a line, so backtracking is bounded per line (checked on adversarial input).
  const tableRegex = /(?:^|\n)((?: {0,3}\|[^\n]+\|[^\S\n]*(?:\n|$)){3,})/g;

  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = tableRegex.exec(text)) !== null) {
    const tableText = match[1];
    const parsed = parseMarkdownTable(tableText);
    if (parsed && parsed.rows.length > 0) {
      const before = text.slice(lastIndex, match.index + (match[0].startsWith("\n") ? 1 : 0));
      segments.push({ before, table: parsed, after: "" });
      lastIndex = match.index + match[0].length;
    }
  }

  if (segments.length > 0) {
    segments[segments.length - 1].after = text.slice(lastIndex);
  }

  return segments;
}
