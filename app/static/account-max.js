"use strict";

const maxConfirm = document.getElementById("confirm-max");
const maxStatus = document.getElementById("max-status");
maxConfirm.addEventListener("click", async () => {
  const initData = window.WebApp?.initData;
  if (!initData) {
    maxStatus.textContent = "Откройте мини-приложение по ссылке входа в MAX.";
    return;
  }
  maxConfirm.disabled = true;
  try {
    const response = await fetch("/account/max/miniapp", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({init_data: initData}), signal: AbortSignal.timeout(10000),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.message);
    maxStatus.textContent = `Код: ${result.code}. Введите его только в браузере, где сами начали вход.`;
    maxConfirm.hidden = true;
  } catch (error) {
    maxStatus.textContent = error.message || "Вход пока не подтверждён.";
  } finally {
    maxConfirm.disabled = false;
  }
});
