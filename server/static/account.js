import { ApiError, api, withButton } from "./common.js";

const form = document.getElementById("password-form");
const error = document.getElementById("error");
const done = document.getElementById("done");

try {
  const me = await api("GET", "/api/me");
  document.getElementById("signed-in-as").textContent = `Signed in as ${me.email} (${me.role_label}).`;
  if (!me.permissions.includes("view_board")) document.getElementById("back").href = "/county";
} catch (failure) {
  if (failure instanceof ApiError && (failure.status === 401 || failure.status === 403)) location.assign("/login");
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  error.hidden = true;
  done.hidden = true;
  if (form.new.value !== form.repeat.value) {
    show("The two new passwords don't match.");
    form.repeat.select();
    return;
  }
  try {
    await withButton(form.querySelector("button"), () =>
      api("POST", "/api/password", { current_password: form.current.value, new_password: form.new.value }));
    form.reset();
    done.hidden = false;
  } catch (failure) {
    show(failure.message);
  }
});

function show(message) {
  error.textContent = message;
  error.hidden = false;
}
