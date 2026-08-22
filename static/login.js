"use strict";

const form = document.getElementById("login-form");
const password = document.getElementById("password");
const submit = document.getElementById("submit");
const message = document.getElementById("login-message");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  submit.disabled = true;
  message.textContent = "";
  try {
    const response = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: password.value }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      message.textContent = payload.error || `登入失敗（HTTP ${response.status}）`;
      password.value = "";
      password.focus();
      return;
    }
    window.location.replace("/");
  } catch (error) {
    message.textContent = `無法連線伺服器：${error.message}`;
  } finally {
    submit.disabled = false;
  }
});

password.focus();
