// Small single-series SVG charts for the History page. One series each, so the title
// names it and there is no legend; values sit in a native tooltip on a wide hit area.
import { el } from "./common.js";

const SVG = "http://www.w3.org/2000/svg";
const WIDTH = 640, HEIGHT = 220;
const PAD = { top: 16, right: 16, bottom: 30, left: 40 };
const PLOT_W = WIDTH - PAD.left - PAD.right, PLOT_H = HEIGHT - PAD.top - PAD.bottom;

function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  node.append(...children.filter(Boolean));
  return node;
}

function tooltip(text) {
  const node = svg("title");
  node.textContent = text;
  return node;
}

function label(x, y, text, anchor = "middle") {
  const node = svg("text", { x, y, "text-anchor": anchor, class: "chart-label" });
  node.textContent = text;
  return node;
}

// A round top for the axis: 0, half and max, so the grid stays three quiet lines.
function niceMax(value, step) {
  return Math.max(step * 2, Math.ceil(value / (step * 2)) * step * 2);
}

function frame(buckets, max, format, title) {
  const y = (v) => PAD.top + PLOT_H - (v / max) * PLOT_H;
  const grid = [0, max / 2, max].map((v) => [
    svg("line", { x1: PAD.left, x2: WIDTH - PAD.right, y1: y(v), y2: y(v), class: v ? "chart-grid" : "chart-axis" }),
    label(PAD.left - 8, y(v) + 4, format(v), "end"),
  ]).flat();
  const slot = PLOT_W / buckets.length;
  const ticks = [0, Math.floor((buckets.length - 1) / 2), buckets.length - 1]
    .filter((i, n, all) => all.indexOf(i) === n)
    .map((i) => label(PAD.left + slot * (i + 0.5), HEIGHT - 8, buckets[i].label));
  const root = svg("svg", { viewBox: `0 0 ${WIDTH} ${HEIGHT}`, role: "img", "aria-label": title, class: "chart" }, ...grid, ...ticks);
  return { root, y, slot };
}

function figure(title, note, chart) {
  return el("figure", { class: "chart-card" },
    el("figcaption", {}, el("h2", {}, title), note ? el("p", { class: "muted" }, note) : null), chart);
}

export function barChart(buckets, { title, note, unit }) {
  const max = niceMax(Math.max(...buckets.map((b) => b.value)), 1);
  const { root, y, slot } = frame(buckets, max, (v) => String(Math.round(v)), title);
  const barWidth = Math.max(2, Math.min(28, slot - 2));  // a 2px gap keeps neighbours apart
  buckets.forEach((bucket, i) => {
    const x = PAD.left + slot * i;
    const text = `${bucket.label}: ${bucket.value} ${unit(bucket.value)}`;
    if (bucket.value) {
      const top = y(bucket.value), r = Math.min(4, barWidth / 2, PAD.top + PLOT_H - top);
      const left = x + (slot - barWidth) / 2, right = left + barWidth, base = PAD.top + PLOT_H;
      root.append(svg("path", {
        class: "chart-bar",
        d: `M${left},${base} V${top + r} Q${left},${top} ${left + r},${top} H${right - r} Q${right},${top} ${right},${top + r} V${base} Z`,
      }));
    }
    root.append(svg("rect", { x, y: PAD.top, width: slot, height: PLOT_H, class: "chart-hit" }, tooltip(text)));
  });
  return figure(title, note, root);
}

export function lineChart(buckets, { title, note, target, targetLabel, format }) {
  const values = buckets.map((b) => b.value).filter((v) => v !== null);
  if (!values.length) return figure(title, "Nobody has responded to an emergency in this period yet.", null);
  const max = niceMax(Math.max(target, ...values), 5);
  const { root, y, slot } = frame(buckets, max, format, title);
  const ty = y(target);
  root.append(svg("line", { x1: PAD.left, x2: WIDTH - PAD.right, y1: ty, y2: ty, class: "chart-target" }),
              label(WIDTH - PAD.right, ty - 6, targetLabel, "end"));
  // Gaps stay gaps: a week with no answered emergency breaks the line rather than inventing a value.
  let path = "", pen = false;
  buckets.forEach((bucket, i) => {
    if (bucket.value === null) { pen = false; return; }
    path += `${pen ? "L" : "M"}${PAD.left + slot * (i + 0.5)},${y(bucket.value)} `;
    pen = true;
  });
  root.append(svg("path", { d: path, class: "chart-line" }));
  buckets.forEach((bucket, i) => {
    if (bucket.value === null) return;
    const cx = PAD.left + slot * (i + 0.5);
    root.append(svg("circle", { cx, cy: y(bucket.value), r: 4, class: "chart-dot" }),
                svg("rect", { x: cx - slot / 2, y: PAD.top, width: slot, height: PLOT_H, class: "chart-hit" },
                    tooltip(`${bucket.label}: ${format(bucket.value)}`)));
  });
  return figure(title, note, root);
}
