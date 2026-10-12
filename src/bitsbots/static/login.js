let registering = false;
const nameField = document.querySelector("#nameField");
const displayName = document.querySelector("#displayName");
const password = document.querySelector("#password");

function setMode(isRegistering) {
  registering = isRegistering;
  nameField.hidden = !registering;
  displayName.required = registering;
  password.autocomplete = registering ? "new-password" : "current-password";
  document.querySelector("#modeLabel").textContent = registering ? "CREATE YOUR WORKSPACE" : "WELCOME BACK";
  document.querySelector("#title").textContent = registering ? "Create account" : "Sign in";
  document.querySelector("#subtitle").textContent = registering
    ? "Your profile and paper portfolio stay isolated on this PC."
    : "Access your private trading workspace.";
  document.querySelector("#submitButton").textContent = registering ? "Create local account" : "Sign in";
  document.querySelector("#switchMode").textContent = registering ? "I already have an account" : "Create a local account";
  document.querySelector("#passwordHint").hidden = !registering;
  document.querySelector("#error").textContent = "";
}

document.querySelector("#switchMode").addEventListener("click", () => setMode(!registering));
document.querySelector("#authForm").addEventListener("submit", async event => {
  event.preventDefault();
  const body = { email: document.querySelector("#email").value, password: password.value };
  if (registering) body.display_name = displayName.value;
  const response = await fetch(registering ? "/api/auth/register" : "/api/auth/login", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
  });
  if (response.ok) {
    window.location.assign("/");
    return;
  }
  const payload = await response.json().catch(() => ({ detail: "Unable to sign in" }));
  document.querySelector("#error").textContent = typeof payload.detail === "string" ? payload.detail : "Check the form and try again.";
});

fetch("/api/auth/setup").then(response => response.json()).then(data => setMode(data.first_run));
