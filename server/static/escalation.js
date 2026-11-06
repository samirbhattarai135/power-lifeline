// What happens after the first alerts: nobody answered, or the backup is almost out.
// Shared by the agency board and the county view; the county wording asks for a welfare check.
import { el } from "./common.js";

const NO_ANSWER = {
  agency: "Everyone on the call list was alerted and nobody has responded. Send someone, or ask county emergency management for a welfare check.",
  county: "Nobody on the call list has responded. A welfare check is needed.",
};

export function escalationNotes(item, audience = "agency") {
  const notes = [];
  if (item.unreached) {
    notes.push(el("p", { class: "escalation-note is-unreached" }, el("span", { class: "tag tag-out" }, "No answer"), ` ${NO_ANSWER[audience]}`));
  }
  if (item.backup_low) {
    notes.push(el("p", { class: "escalation-note is-backup-low" }, el("span", { class: "tag tag-out" }, "Backup almost out"),
      " The whole call list has been told to make sure someone is there now."));
  }
  return notes;
}

const count = (n) => n.toLocaleString("en-US");

// Each county with Lifeline homes, next to how many Medicare patients there rely on powered equipment (HHS emPOWER).
export function countyContext(counties) {
  if (!counties?.length) return null;
  return el("section", { class: "county-context", "aria-label": "Power-dependent residents by county" },
    el("ul", {}, counties.map((c) => el("li", {},
      el("strong", {}, c.name), `: ${count(c.power_dependent)} Medicare beneficiaries use electricity-dependent equipment ` +
      `(${count(c.oxygen)} on oxygen, ${count(c.home_health)} with home health). ` +
      `Lifeline watches ${count(c.lifeline_homes)}` + (c.lifeline_outages ? `; ${count(c.lifeline_outages)} without power now.` : ".")))),
    el("p", { class: "muted" }, "Source: HHS emPOWER, de-identified Medicare claims, updated monthly."));
}
