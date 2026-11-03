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
