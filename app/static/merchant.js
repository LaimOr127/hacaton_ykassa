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
