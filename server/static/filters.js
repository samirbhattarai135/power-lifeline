// One filter bar for every list in Lifeline: an optional search box, a dropdown per question
// (each option with its count), Reset, and the chosen filters as removable pills.
//
// A field is { key, label, value(item) -> string, names?: {value: label}, order?: [values] }.
// The state is a plain object { [key]: chosen value or null, search: text }.
import { el } from "./common.js";

const matchesSearch = (item, text, searchText) => !text || searchText(item).toLowerCase().includes(text.toLowerCase());

/** Items that match every chosen filter and the search text. */
export function applyFilters(items, fields, state, searchText = null) {
  return items.filter((item) => matchesSearch(item, state.search, searchText ?? (() => ""))
    && fields.every((f) => !state[f.key] || f.value(item) === state[f.key]));
}

export function isFiltering(fields, state) {
  return Boolean(state.search) || fields.some((f) => state[f.key]);
}

export function resetState(fields, state) {
  for (const f of fields) state[f.key] = null;
  state.search = "";
}

function select(field, items, chosen, onChange, idPrefix) {
  const counts = new Map();
  for (const item of items) counts.set(field.value(item), (counts.get(field.value(item)) ?? 0) + 1);
  const values = field.order ? field.order.filter((v) => counts.has(v))
    : [...counts.keys()].sort((a, b) => counts.get(b) - counts.get(a) || String(a).localeCompare(String(b)));
  const id = `${idPrefix}-${field.key}`;
  return el("div", { class: "filter-field" },
    el("label", { for: id }, field.label),
    el("select", { class: "input", id, onchange: (e) => onChange(field.key, e.target.value || null) },
      el("option", { value: "" }, "All"),
      values.map((v) => el("option", { value: v, selected: v === chosen }, `${field.names?.[v] ?? v} (${counts.get(v)})`))));
}

/**
 * The bar itself. `onChange(key, value)` for a dropdown or pill, `onSearch(text)` for the box
 * (only shown when `searchLabel` is given), `onReset()` for the button.
 */
export function filterBar(items, fields, state, { onChange, onReset, onSearch, searchLabel, searchText, idPrefix = "filter", noun = "rows" }) {
  const searchId = `${idPrefix}-search`;
  const chosen = fields.filter((f) => state[f.key]);
  const showing = applyFilters(items, fields, state, searchText).length;
  const pills = chosen.map((f) => el("button", { class: "filter-pill", type: "button", "aria-label": `Remove ${f.label} filter`,
    onclick: () => onChange(f.key, null) }, f.names?.[state[f.key]] ?? state[f.key], el("span", { "aria-hidden": "true" }, " ×")));
  if (state.search) {
    pills.push(el("button", { class: "filter-pill", type: "button", "aria-label": "Clear search", onclick: () => onSearch("") },
      `“${state.search}”`, el("span", { "aria-hidden": "true" }, " ×")));
  }
  return [
    el("div", { class: `filter-bar${searchLabel ? " has-search" : ""}`, role: "search", "aria-label": `Filter ${noun}` },
      searchLabel ? el("div", { class: "filter-field filter-search" },
        el("label", { for: searchId }, searchLabel),
        el("input", { class: "input", id: searchId, type: "search", value: state.search ?? "", placeholder: "Name or address",
          oninput: (e) => onSearch(e.target.value) })) : null,
      ...fields.map((f) => select(f, items, state[f.key], onChange, idPrefix)),
      el("button", { class: "btn btn-small filter-reset", type: "button", disabled: !isFiltering(fields, state), onclick: onReset }, "Reset filters")),
    isFiltering(fields, state) ? el("div", { class: "filter-chosen", role: "status" },
      el("span", { class: "filter-chosen-label" }, `Showing ${showing} of ${items.length} ${noun}:`), pills) : null,
  ];
}

/** Put a filter bar in its container, keeping focus (and the cursor in the search box) where it was. */
export function mountFilterBar(container, nodes) {
  const active = document.activeElement;
  const focusedId = container.contains(active) ? active.id : null;
  const caret = focusedId && active.selectionStart != null ? active.selectionStart : null;
  container.replaceChildren(...nodes.filter(Boolean));
  const again = focusedId && document.getElementById(focusedId);
  if (again) {
    again.focus();
    if (caret != null) again.setSelectionRange(caret, caret);
  }
}
