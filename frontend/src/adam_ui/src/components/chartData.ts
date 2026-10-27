//  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
//  SPDX-License-Identifier: Apache-2.0

// Kept out of DataChart.tsx so that file exports only components (React Fast Refresh).

import type { ParsedTable } from "./markdownTables";

/** A cell's number, ignoring $, % and thousands separators; null when it is blank, N/A or not a number. */
function toNumber(val: string): number | null {
  const cleaned = val.replace(/[$,%]/g, "").trim();
  if (cleaned === "" || cleaned === "N/A") return null;
  const n = Number(cleaned);
  return isNaN(n) ? null : n;
}

const isBlank = (val: string) => val.trim() === "" || val.trim() === "N/A";

/** The unit every value in a column is written in: dollars, percent, or a plain number. */
function unitOf(values: string[]): "$" | "%" | "" {
  const written = values.filter((v) => !isBlank(v));
  if (written.every((v) => v.includes("$"))) return "$";
  if (written.every((v) => v.includes("%"))) return "%";
  return "";
}

const SKIP_HEADERS = /^(id|key|year|date|period|form|quarter)$/i;

function isLikelyMetric(header: string, values: string[]): boolean {
  const h = header.replace(/\*\*/g, "").trim();
  if (SKIP_HEADERS.test(h)) return false;
  if (values.every((v) => /^\d{4}$/.test(v.trim()))) return false;
  if (values.every((v) => /^\d+$/.test(v.trim()) && Number(v.trim()) < 100000 && !v.includes("$"))) return false;
  return true;
}

export interface ChartableData {
  labels: string[];
  /** null where the table had no value: a gap, not a zero. */
  series: { name: string; data: (number | null)[] }[];
  /** Every plotted value was written in dollars, so the axis may show them so. */
  currency: boolean;
}

export function detectChartableData(table: ParsedTable): ChartableData | null {
  if (table.rows.length < 2 || table.headers.length < 2) return null;

  const numericCols: number[] = [];
  for (let col = 1; col < table.headers.length; col++) {
    const values = table.rows.map((row) => row[col] || "");
    if (values.every((val) => isBlank(val) || toNumber(val) !== null) && isLikelyMetric(table.headers[col], values)) {
      numericCols.push(col);
    }
  }

  if (numericCols.length === 0) return null;
  // One axis holds one unit: columns in another unit than the first are left out, not drawn to its scale.
  const unit = unitOf(table.rows.map((row) => row[numericCols[0]] || ""));
  const plotted = numericCols.filter((col) => unitOf(table.rows.map((row) => row[col] || "")) === unit);

  const labels = table.rows.map((row) => (row[0] || "").replace(/\*\*/g, ""));
  const series = plotted.map((col) => ({
    name: table.headers[col].replace(/\*\*/g, ""),
    data: table.rows.map((row) => toNumber(row[col] || "")),
  }));
  return { labels, series, currency: unit === "$" };
}

/** An axis tick: large values shortened, with a dollar sign only for amounts the table gave in dollars. */
export function formatAxisValue(val: number, currency: boolean): string {
  const sign = currency ? "$" : "";
  if (Math.abs(val) >= 1e12) return `${sign}${(val / 1e12).toFixed(1)}T`;
  if (Math.abs(val) >= 1e9) return `${sign}${(val / 1e9).toFixed(1)}B`;
  if (Math.abs(val) >= 1e6) return `${sign}${(val / 1e6).toFixed(1)}M`;
  if (Math.abs(val) >= 1e3) return `${sign}${(val / 1e3).toFixed(1)}K`;
  return `${sign}${val}`;
}
