// Small helpers shared by the platform, family and sign-in pages.

export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key in node && key !== "list") node[key] = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  node.append(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
  return node;
}

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export async function api(method, path, body) {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(response.status, data.error || "Something went wrong. Try again.");
  return data;
}

// Runs an async action while showing the button's loading, error and success states.
export async function withButton(button, action, { successLabel } = {}) {
  const label = button.textContent;
  button.setAttribute("aria-busy", "true");
  button.disabled = true;
  try {
    const result = await action();
    if (successLabel) {
      button.dataset.state = "success";
      button.textContent = successLabel;
      setTimeout(() => { delete button.dataset.state; button.textContent = label; }, 1600);
    }
    return result;
  } catch (error) {
    button.dataset.state = "error";
    setTimeout(() => { delete button.dataset.state; }, 1600);
    throw error;
  } finally {
    button.removeAttribute("aria-busy");
    button.disabled = false;
  }
}

export function minutesSince(iso) {
  return Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 60000));
}

export function plural(count, word) {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

export function clockTime(iso) {
  return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

export function listNames(names) {
  if (names.length <= 1) return names.join("");
  return `${names.slice(0, -1).join(", ")} and ${names.at(-1)}`;
}

export function secondsAgo(iso) {
  const seconds = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (seconds < 60) return plural(seconds, "second") + " ago";
  return plural(Math.floor(seconds / 60), "minute") + " ago";
}

// The device's view of the world, phrased the same way on every page.
export function stateOf(unit) {
  if (unit.status === "outage") return unit.acknowledged_by ? "acked" : "out";
  if (unit.status === "ok") return "on";
  return "quiet";
}

// Storm Watch hazards, phrased the same way on the board and the family page.
export function hazardText(hazard) {
  return `${hazard.headline} until ${new Date(hazard.ends).toLocaleTimeString([], { weekday: "short", hour: "numeric", minute: "2-digit" })}`;
}

export function dateTime(iso) {
  return new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

export function durationText(minutes) {
  if (minutes < 1) return "<1 min";
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return minutes % 60 ? `${hours} h ${minutes % 60} min` : `${hours} h`;
}

export function uptimeText(seconds) {
  if (seconds < 3600) return durationText(Math.floor(seconds / 60));
  const days = Math.floor(seconds / 86400);
  return days ? `${plural(days, "day")} ${Math.floor((seconds % 86400) / 3600)} h` : durationText(Math.floor(seconds / 60));
}

// ---- one shape and one word per state, everywhere (board, map, county, family) ------------

export const STATE_WORD = {
  critical: "Under 15 min", out: "Power out", unreached: "No answer", responding: "Responding", silent: "Unit silent", on: "Power on",
};

/** The display state of a home. Board rows carry acknowledged_by, county rows carry responder.
    A claimed home shows "Responding" even under 15 min: the shape says someone is coming. */
export function homeState(row) {
  if (row.status === "outage") {
    if (row.acknowledged_by || row.responder) return "responding";
    if (row.critical) return "critical";
    return row.unreached ? "unreached" : "out";
  }
  return row.status === "ok" ? "on" : "silent";  // offline or never seen: unknown, never assumed fine
}

/** The state's shape, drawn in CSS (see .glyph in app.css). Decorative: always paired with a word. */
export function glyph(state) {
  return el("span", { class: `glyph is-${state}`, "aria-hidden": "true" });
}

export function chip(state, word = STATE_WORD[state]) {
  return el("span", { class: `chip-state is-${state}` }, glyph(state), word);
}

/** "Ruth A. (synthetic)" -> the name, and the honesty marker set small beside it. */
export function nameParts(name) {
  const match = /^(.*?)\s*(\(synthetic\))$/.exec(name);
  return match ? [match[1], match[2]] : [name, ""];
}

export function nameNode(name, tag = "strong") {
  const [plain, marker] = nameParts(name);
  return [el(tag, { class: "who-name" }, plain), marker ? el("span", { class: "synthetic" }, ` ${marker}`) : null];
}

// Backup colour follows time left and is never green: ink over 2 h, amber to 15 min, red under.
const TWO_HOURS_MS = 2 * 3600000;
const FIFTEEN_MIN_MS = 15 * 60000;
export function backupTone(msLeft) {
  if (msLeft > TWO_HOURS_MS) return "ink";
  return msLeft > FIFTEEN_MIN_MS ? "amber" : "red";
}

export function runsOutAt(row) {
  return row.outage_started && row.backup_minutes ? new Date(Date.parse(row.outage_started) + row.backup_minutes * 60000).toISOString() : null;
}
