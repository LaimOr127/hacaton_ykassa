"use strict";

const socialFeedback = document.getElementById("social-status");
const socialForms = document.querySelectorAll("[data-social-login]");
for (const form of socialForms) {
  form.addEventListener("submit", async event => {
    event.preventDefault();
    for (const option of socialForms) option.querySelector("button").disabled = true;
    socialFeedback.textContent = "Готовим переход к провайдеру.";
    try {
      const response = await fetch(form.action, {
        method: "POST", body: new URLSearchParams(new FormData(form)),
        headers: {Accept: "application/json"}, signal: AbortSignal.timeout(10000),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.message);
      window.location.assign(result.url);
    } catch (error) {
      socialFeedback.textContent = error.message || "Вход пока недоступен.";
    } finally {
      for (const option of socialForms) option.querySelector("button").disabled = false;
    }
  });
}
