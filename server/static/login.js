import { api, withButton } from "./common.js";

const form = document.getElementById("signin");
const error = document.getElementById("error");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const email = form.email.value.trim();
  const password = form.password.value;
  if (!email || !password) {
    showError("Enter your work email and password.");
    return;
  }
  error.hidden = true;
  try {
    const { home } = await withButton(form.querySelector("button"), () => api("POST", "/api/login", { email, password }));
    location.assign(home);
  } catch (failure) {
    showError(failure.message);
    form.password.select();
  }
});

function showError(message) {
  error.textContent = message;
  error.hidden = false;
  form.email.setAttribute("aria-invalid", String(!form.email.value.trim()));
}
