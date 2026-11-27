"use strict";

document.getElementById("catalog-file")?.addEventListener("change", async (event) => {
  const file = event.currentTarget.files[0];
  const status = document.getElementById("import-status");
  if (!file) return;
  if (file.size > 36_000) {
    status.textContent = "Файл слишком большой. Используйте прайс до 120 строк.";
    return;
  }
  try {
    const text = new TextDecoder("utf-8", {fatal: true}).decode(await file.arrayBuffer());
    if (text.length > 12000) throw new Error();
    document.getElementById("catalog-csv").value = text;
    status.textContent = "Файл прочитан. Проверьте содержимое и опубликуйте прайс.";
  } catch {
    status.textContent = "Файл не удалось прочитать. Сохраните CSV в UTF-8 и проверьте его размер.";
  }
});

const loadModels = document.getElementById("load-models");
const modelProvider = document.getElementById("model-provider");
const modelKey = document.getElementById("model-key");
const modelStatus = document.getElementById("model-status");
const modelOptions = document.getElementById("model-options");
function clearModelOptions() {
  modelOptions.replaceChildren();
  modelStatus.textContent = "";
}
modelProvider?.addEventListener("change", clearModelOptions);
modelKey?.addEventListener("input", clearModelOptions);
loadModels?.addEventListener("click", async () => {
  const form = new FormData(document.getElementById("model-form"));
  if (!form.get("api_key").trim()) {
    modelStatus.textContent = "Введите API-ключ.";
    modelKey.focus();
    return;
  }
  loadModels.disabled = true;
  clearModelOptions();
  modelStatus.textContent = "Проверяем доступные модели.";
  const sameConnection = () => modelProvider.value === form.get("provider") && modelKey.value === form.get("api_key");
  try {
    const body = new URLSearchParams();
    for (const field of ["token", "provider", "api_key"]) body.set(field, form.get(field));
    const response = await fetch("/merchant/model/options", {method:"POST", body, signal:AbortSignal.timeout(12000)});
    const result = await response.json();
    if (!sameConnection()) return;
    if (!response.ok) throw new Error(result.message || "Список недоступен.");
    for (const id of result.models) {
      const option = document.createElement("option");
      option.value = id;
      modelOptions.append(option);
    }
    modelStatus.textContent = result.models.length
      ? `Найдено моделей: ${result.models.length}. Выберите модель из подсказок поля.`
      : "Список пуст. Укажите модель вручную.";
  } catch (error) {
    if (sameConnection()) modelStatus.textContent = error.message || "Список недоступен.";
  } finally {
    loadModels.disabled = false;
  }
});
