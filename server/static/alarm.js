// Gets a person's attention the moment something new happens: three beeps, a flashing tab
// title and a desktop notification. Only for changes after the page loaded, never on load.
// Browsers only allow sound after a click on the page, so the button "arms" it first.

const SOUND_KEY = "lifeline-sound";
const WHAT = { power: "Power out", machine: "Machine stopped", help: "Help pressed" };

// One key per thing worth an alarm: a new emergency, nobody answering, backup almost out, a unit going silent.
function alarmKeys(unit) {
  if (unit.status === "offline") return [[`${unit.device_id}|silent|${unit.last_seen}`, `Unit silent: ${unit.name}`]];
  if (unit.status !== "outage") return [];
  const base = `${unit.device_id}|${unit.outage_started}`;
  const keys = [[base, `${WHAT[unit.emergency] ?? "Power out"}: ${unit.name}`]];
  if (unit.unreached) keys.push([`${base}|unreached`, `Nobody answering: ${unit.name}`]);
  if (unit.backup_low) keys.push([`${base}|backup`, `Backup almost out: ${unit.name}`]);
  return keys;
}

export function createAlarm(button) {
  const baseTitle = document.title;
  let wanted = localStorage.getItem(SOUND_KEY) !== "off";  // sound is on unless someone turned it off
  let audio = null;
  let seen = null;
  let flashing = null;

  const armed = () => audio?.state === "running";
  function paint() {
    button.setAttribute("aria-pressed", String(wanted && armed()));
    button.textContent = !wanted ? "Sound off" : armed() ? "Sound on" : "Turn on sound";
    button.classList.toggle("is-unarmed", wanted && !armed());
  }

  function beep() {
    if (!wanted || !armed()) return;
    const start = audio.currentTime;
    for (const offset of [0, 0.28, 0.56]) {
      const tone = audio.createOscillator();
      const gain = audio.createGain();
      tone.frequency.value = 880;
      gain.gain.setValueAtTime(0.0001, start + offset);
      gain.gain.exponentialRampToValueAtTime(0.35, start + offset + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + offset + 0.22);
      tone.connect(gain).connect(audio.destination);
      tone.start(start + offset);
      tone.stop(start + offset + 0.24);
    }
  }

  function flashTitle(text) {
    clearInterval(flashing);
    let on = false;
    flashing = setInterval(() => { document.title = (on = !on) ? `⚠ ${text}` : baseTitle; }, 900);
  }
  function stopFlash() {
    clearInterval(flashing);
    flashing = null;
    document.title = baseTitle;
  }
  window.addEventListener("focus", stopFlash);
  document.addEventListener("pointerdown", stopFlash);

  button.addEventListener("click", async () => {
    if (wanted && !armed()) {
      audio ??= new AudioContext();
      await audio.resume();
      if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
      beep();
    } else {
      wanted = !wanted;
      localStorage.setItem(SOUND_KEY, wanted ? "on" : "off");
      if (wanted) beep();
    }
    paint();
  });
  paint();

  return {
    /** Call with every fresh board snapshot; raises the alarm for anything not seen before. */
    notice(units) {
      const current = new Map(units.flatMap(alarmKeys));
      if (seen === null) {
        seen = new Set(current.keys());
        return;
      }
      const fresh = [...current].filter(([key]) => !seen.has(key)).map(([, text]) => text);
      seen = new Set(current.keys());
      if (!fresh.length) return;
      beep();
      if (!document.hasFocus()) flashTitle(fresh[0]);
      if ("Notification" in window && Notification.permission === "granted" && document.visibilityState !== "visible") {
        new Notification("Lifeline", { body: fresh.join("\n"), tag: "lifeline-alarm", requireInteraction: true });
      }
    },
  };
}
