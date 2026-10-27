//  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
//  SPDX-License-Identifier: Apache-2.0

import { useMemo } from "react";
import { useComputedColorScheme } from "@mantine/core";
import ReactEChartsCore from "echarts-for-react/lib/core";
import * as echarts from "echarts/core";
import { BarChart, LineChart } from "echarts/charts";
import { GridComponent, LegendComponent, TooltipComponent } from "echarts/components";
import { SVGRenderer } from "echarts/renderers";
import { detectChartableData, formatAxisValue } from "./chartData";
import type { ParsedTable } from "./markdownTables";

// Only what these charts draw, so the bundle leaves the rest of ECharts out.
echarts.use([BarChart, LineChart, GridComponent, LegendComponent, TooltipComponent, SVGRenderer]);

// Mantine's gray scale in light mode, its dark scale in dark mode.
const COLORS = {
  light: { legend: "#495057", label: "#868e96", axis: "#ced4da", split: "#e9ecef" },
  dark: { legend: "#c1c2c5", label: "#909296", axis: "#373a40", split: "#2c2e33" },
};

function chooseChartType(labels: string[], series: { name: string; data: (number | null)[] }[]): "bar" | "line" {
  if (labels.length > 10) return "line";
  if (series.length > 3) return "line";
  return "bar";
}

export function DataChart({ table }: { table: ParsedTable }) {
  const chartData = useMemo(() => detectChartableData(table), [table]);
  const colors = COLORS[useComputedColorScheme("dark")];

  if (!chartData) return null;

  const chartType = chooseChartType(chartData.labels, chartData.series);

  const option = {
    backgroundColor: "transparent",
    tooltip: {
      trigger: "axis" as const,
      axisPointer: { type: "shadow" as const },
    },
    legend: {
      show: chartData.series.length > 1,
      textStyle: { color: colors.legend },
      top: 0,
    },
    grid: {
      left: "3%",
      right: "4%",
      bottom: "3%",
      top: chartData.series.length > 1 ? 30 : 10,
      containLabel: true,
    },
    xAxis: {
      type: "category" as const,
      data: chartData.labels,
      axisLabel: {
        color: colors.label,
        rotate: chartData.labels.some((l) => l.length > 15) ? 30 : 0,
        fontSize: 11,
      },
      axisLine: { lineStyle: { color: colors.axis } },
    },
    yAxis: {
      type: "value" as const,
      axisLabel: {
        color: colors.label,
        fontSize: 11,
        formatter: (val: number) => formatAxisValue(val, chartData.currency),
      },
      splitLine: { lineStyle: { color: colors.split } },
    },
    series: chartData.series.map((s) => ({
      name: s.name,
      type: chartType,
      data: s.data,
      itemStyle: { borderRadius: chartType === "bar" ? [4, 4, 0, 0] : undefined },
      emphasis: { focus: "series" as const },
    })),
  };

  return (
    <div style={{ marginTop: 8, marginBottom: 8 }}>
      <ReactEChartsCore
        echarts={echarts}
        option={option}
        style={{ height: 280, width: "100%" }}
        opts={{ renderer: "svg" }}
      />
    </div>
  );
}
