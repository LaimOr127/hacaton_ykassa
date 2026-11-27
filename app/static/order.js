"use strict";

const byId = (id) => document.getElementById(id);
const csrf = document.querySelector('meta[name="csrf-token"]').content;
const simulated = document.body.dataset.mode === "simulated";
const client = new Intl.NumberFormat("ru-RU", {style: "currency", currency: "RUB"});
const money = (kopecks) => client.format(kopecks / 100);
let order = null;
let attempt = null;
let busy = false;
let polling = false;
let timer;
let createKey;
let lastMessage;
let payKey;
let lastDelta = "";
const api = "/api/v1/orders";
const settled = new Set(["paid", "deposit_received"]);
const finishedAttempt = new Set(["succeeded", "canceled", "failed"]);

async function request(path, options = {}) {
  const headers = {"X-CSRF-Token": csrf, "X-Shop": document.body.dataset.shop, ...options.headers};
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {...options, headers, signal: AbortSignal.timeout(45000)});
  const body = await response.json();
  if (!response.ok) throw body;
  return body;
}

function showNotice(message = "") {
  byId("notice").hidden = !message;
  byId("notice").textContent = message;
}

function describePayment() {
  if (!attempt) return lastDelta || (order?.payable_kopecks
    ? "Проверьте состав и сумму перед подтверждением."
    : "Добавьте позиции, чтобы перейти к оплате.");
  const invoice = attempt.kind === "invoice";
  if (attempt.status === "succeeded" && order.checkout?.paid_parts < order.checkout?.parts) {
    return "Доля оплачена. Можно создать ссылку для следующего плательщика.";
  }
  if (invoice && attempt.status === "pending" && !attempt.confirmation_url && Date.parse(attempt.expires_at) <= Date.now()) {
    return "Срок счёта истёк. Проверяем его состояние в ЮKassa.";
  }
  const messages = {
    creating: invoice ? "Выставляем счёт." : "Создаём платёж.",
    pending: simulated ? "Готово к симуляции оплаты."
      : invoice ? "Счёт выставлен. Оплатите его на странице ЮKassa." : "Платёж создан. Завершите оплату на странице ЮKassa.",
    succeeded: order.fulfilled ? "Заказ исполнен." : simulated ? "Симуляция оплаты завершена." : (
      order.status === "deposit_received" ? "Предоплата получена." : "Оплата подтверждена."
    ),
    canceled: "Оплата отменена. Можно создать новую попытку.",
    failed: "Платёж не создан. Можно повторить.",
    unknown: "Платёж на сверке. Новая оплата пока недоступна.",
  };
  return messages[attempt.status];
}

function paymentSelection() {
  const plan = order?.checkout;
  const kind = plan ? "payment" : byId("payment-kind")?.value ?? "payment";
  return {
    kind,
    parts: plan?.parts ?? (kind === "payment" ? Number(byId("payment-parts")?.value ?? 1) : 1),
    tip_percent: plan?.tip_percent ?? (kind === "payment" ? Number(byId("payment-tip")?.value ?? 0) : 0),
    part_number: plan ? Math.min(plan.paid_parts + 1, plan.parts) : 1,
  };
}

function render() {
  const locked = ["payment_pending", "paid", "deposit_received"].includes(order?.status);
  const review = order?.seller_review;
  const items = byId("items");
  items.replaceChildren(...(order?.items ?? []).map((item) => {
    const row = document.createElement("li");
    const label = document.createElement("span");
    const price = document.createElement("span");
    label.textContent = `${item.title} × ${item.quantity}`;
    price.className = "item-price";
    price.textContent = money(item.amount_kopecks);
    row.append(label, price);
    return row;
  }));
  byId("empty").hidden = items.childElementCount > 0;
  byId("total").textContent = money(order?.total_kopecks ?? 0);
  byId("order-reference").hidden = !order?.id;
  byId("order-reference").textContent = order?.id ? `#${order.id.slice(0, 8)}` : "";
  byId("deposit").hidden = !(order?.balance_kopecks > 0);
  byId("deposit").textContent = order?.balance_kopecks > 0
    ? `Сейчас ${money(order.payable_kopecks)}, остаток ${money(order.balance_kopecks)}` : "";
  const status = describePayment();
  if (byId("payment-status").textContent !== status) byId("payment-status").textContent = status;
  const stage = order?.status === "paid" || order?.status === "deposit_received" || attempt?.status === "unknown"
    ? 2 : locked ? 1 : 0;
  ["payment-review", "payment-provider", "payment-verified"].forEach((id, index) => {
    const item = byId(id);
    if (index === stage) item.setAttribute("aria-current", "step");
    else item.removeAttribute("aria-current");
    item.classList.toggle("is-complete", index < stage);
  });
  byId("invoice-expiry").hidden = !attempt?.expires_at;
  byId("invoice-expiry").textContent = attempt?.expires_at
    ? `Счёт действует до ${new Date(attempt.expires_at).toLocaleString("ru-RU", {dateStyle:"short", timeStyle:"short"})}.` : "";
  const reviewMessage = review?.status === "pending" ? "Заказ у продавца. Ответ появится здесь."
    : review?.status === "answered" ? `Ответ продавца\n${review.reply}\nОтправьте уточнённый заказ в чат.` : "";
  const reviewPanel = byId("seller-review");
  reviewPanel.hidden = !reviewMessage;
  if (reviewPanel.textContent !== reviewMessage) reviewPanel.textContent = reviewMessage;
  byId("text").disabled = locked || busy || review?.status === "pending";
  byId("send").disabled = locked || busy || review?.status === "pending";
  const plan = order?.checkout;
  const selection = paymentSelection();
  const continuation = plan && plan.paid_parts < plan.parts && finishedAttempt.has(attempt?.status);
  const canPay = Boolean(order?.payable_kopecks && (!locked || continuation) && !review);
  const kind = byId("payment-kind");
  if (kind) {
    byId("payment-choice").hidden = !canPay || Boolean(plan);
    kind.disabled = busy;
  }
  const controls = byId("checkout-options");
  if (controls) controls.hidden = !canPay || Boolean(plan) || selection.kind !== "payment";
  const base = order?.payable_kopecks ?? 0;
  const tip = plan?.tip_kopecks ?? Math.round(base * selection.tip_percent / 100);
  const nextAmount = plan?.next_kopecks ?? Math.ceil((base + tip) / selection.parts);
  const progress = byId("split-progress");
  progress.hidden = !plan || plan.parts === 1;
  const progressText = plan ? `Оплачено долей: ${plan.paid_parts} из ${plan.parts}. Получено ${money(plan.paid_kopecks)}.` : "";
  if (progress.textContent !== progressText) progress.textContent = progressText;
  const tipLine = byId("tip-total");
  tipLine.hidden = !tip;
  tipLine.textContent = tip ? `Чаевые ${money(tip)} · К оплате с чаевыми ${money(plan?.total_kopecks ?? base + tip)}` : "";
  byId("pay").hidden = !canPay;
  byId("pay").disabled = busy;
  let payLabel = selection.kind === "invoice" ? "Выставить счёт на" : "Подтвердить и оплатить";
  if (selection.parts > 1) payLabel = `Создать ссылку на долю ${selection.part_number} из ${selection.parts} ·`;
  byId("pay").textContent = base ? `${payLabel} ${money(nextAmount)}` : "Подтвердить заказ";
  byId("checkout").hidden = !attempt?.confirmation_url;
  byId("checkout").textContent = attempt?.kind === "invoice" ? "Открыть счёт в ЮKassa" : "Перейти к оплате в ЮKassa";
  byId("checkout-hint").hidden = !attempt?.confirmation_url;
  byId("copy-invoice").hidden = !navigator.clipboard || !attempt?.confirmation_url || (attempt.kind !== "invoice" && !(plan?.parts > 1));
  byId("copy-invoice").textContent = attempt?.kind === "invoice" ? "Скопировать ссылку на счёт" : "Скопировать ссылку на долю";
  if (byId("copy-invoice").hidden) byId("payment-action-status").textContent = "";
  if (attempt?.confirmation_url) byId("checkout").href = attempt.confirmation_url;
  else byId("checkout").removeAttribute("href");
  byId("check-payment").hidden = !["creating", "pending", "unknown"].includes(attempt?.status) && review?.status !== "pending";
  byId("check-payment").disabled = polling || busy;
  byId("simulate").hidden = !simulated || attempt?.status !== "pending";
  byId("simulate").disabled = busy;
  byId("new-order").hidden = !settled.has(order?.status);
  byId("new-order").disabled = busy;
  const documents = byId("payment-documents");
  if (documents) documents.hidden = !settled.has(order?.status) && !plan?.paid_parts;
}

function addMessage(role, text) {
  const element = document.createElement("p");
  element.className = "message " + role;
  element.textContent = text;
  byId("messages").append(element);
  byId("messages").scrollTop = byId("messages").scrollHeight;
}

async function refreshOrder() {
  order = await request(`${api}/${order.id}`);
  if (order.payment_attempt_id) {
    attempt = await request(`${api}/${order.id}/payment-attempts/${order.payment_attempt_id}`);
    if (finishedAttempt.has(attempt.status)) order = await request(`${api}/${order.id}`);
  } else attempt = null;
}

function schedule() {
  clearTimeout(timer);
  if (["creating", "pending"].includes(attempt?.status) || order?.seller_review?.status === "pending") {
    timer = setTimeout(refreshPayment, 7000);
  }
}

async function run(action) {
  if (busy || polling) return;
  busy = true;
  showNotice();
  render();
  try {
    await action();
  } catch (error) {
    if (["stale_version", "order_locked", "payment_in_progress", "seller_review_required", "checkout_changed"].includes(error?.code) && order) {
      try { await refreshOrder(); } catch { /* Сохранённый заказ можно открыть повторно. */ }
    }
    showNotice(error?.code ? error.message : "Связь прервалась. Повторите действие.");
  } finally {
    busy = false;
    render();
    schedule();
  }
}

async function refreshPayment() {
  if (busy || polling || !order) return;
  polling = true;
  render();
  try {
    await refreshOrder();
    showNotice();
  } catch {
    showNotice("Не удалось обновить платёж. Попробуйте ещё раз.");
  } finally {
    polling = false;
    render();
    schedule();
  }
}

async function newOrder() {
  createKey ??= crypto.randomUUID();
  order = await request(api, {method: "POST", headers: {"Idempotency-Key": createKey}, body: "{}"});
  createKey = undefined;
}

byId("chat").addEventListener("submit", (event) => {
  event.preventDefault();
  const text = byId("text").value.trim();
  if (!text) return;
  run(async () => {
    if (!order) await newOrder();
    const previousTotal = order.total_kopecks;
    if (lastMessage?.text !== text || lastMessage?.order_version !== order.version) {
      lastMessage = {text, order_version: order.version, client_message_id: crypto.randomUUID()};
    }
    const result = await request(`${api}/${order.id}/messages`, {method: "POST", body: JSON.stringify(lastMessage)});
    if (order.seller_review?.status === "answered" && result.order.seller_review?.status !== "answered") {
      addMessage("agent", `Продавец\n${order.seller_review.reply}`);
    }
    order = result.order;
    lastMessage = undefined;
    const difference = order.total_kopecks - previousTotal;
    lastDelta = previousTotal && difference ? `Итог изменился на ${difference > 0 ? "+" : "−"}${money(Math.abs(difference))}.` : "";
    addMessage("customer", text);
    addMessage("agent", result.reply);
    byId("text").value = "";
    if (byId("sample")) byId("sample").hidden = true;
  });
});

byId("sample")?.addEventListener("click", () => {
  byId("text").value = byId("sample").dataset.message;
  byId("text").focus();
});

byId("pay").addEventListener("click", () => run(async () => {
  const body = JSON.stringify({order_version: order.version, ...paymentSelection()});
  if (payKey?.body !== body) payKey = {body, id: crypto.randomUUID()};
  attempt = await request(`${api}/${order.id}/payment-attempts`, {
    method: "POST", headers: {"Idempotency-Key": payKey.id}, body,
  });
  payKey = undefined;
  order = await request(`${api}/${order.id}`);
}));

for (const id of ["payment-kind", "payment-parts", "payment-tip"]) byId(id)?.addEventListener("change", render);
byId("copy-invoice").addEventListener("click", async () => {
  const feedback = byId("payment-action-status");
  if (!attempt?.confirmation_url || byId("copy-invoice").hidden) return;
  try {
    await navigator.clipboard.writeText(attempt.confirmation_url);
    feedback.textContent = "Ссылка скопирована.";
  } catch {
    feedback.textContent = "Откройте страницу ЮKassa и скопируйте ссылку из адресной строки.";
  }
});

function documentRow(payment, access, orderId) {
  const row = document.createElement("article");
  row.className = "payment-document";
  const title = document.createElement("h4");
  title.textContent = `${payment.part_number ? "Доля " + payment.part_number : "Платёж"} · ${money(payment.amount_kopecks)}`;
  row.append(title);
  const kinds = payment.has_receipt ? ["receipt", "confirmation"] : ["confirmation"];
  const labels = {receipt: "Кассовый чек", confirmation: "Подтверждение оплаты"};
  if (access.download) {
    const links = document.createElement("div");
    links.className = "document-links";
    for (const kind of kinds) {
      const link = document.createElement("a");
      link.href = `/s/${encodeURIComponent(document.body.dataset.shop)}/orders/${orderId}/documents/${payment.attempt_id}/${kind}`;
      link.textContent = `Скачать: ${labels[kind]}`;
      links.append(link);
    }
    row.append(links);
  }
  if (access.email) {
    const form = document.createElement("form");
    const select = document.createElement("select");
    const documentLabel = document.createElement("label");
    documentLabel.textContent = "Документ для отправки";
    documentLabel.append(select);
    for (const kind of kinds) select.add(new Option(labels[kind], kind));
    const label = document.createElement("label");
    const email = document.createElement("input");
    label.textContent = "Электронная почта";
    email.type = "email";
    email.required = true;
    email.maxLength = 254;
    email.autocomplete = "email";
    label.append(email);
    const send = document.createElement("button");
    send.textContent = "Отправить на почту";
    const feedback = document.createElement("p");
    feedback.setAttribute("role", "status");
    feedback.className = "payment-hint";
    let delivery;
    form.append(documentLabel, label, send, feedback);
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const recipient = email.value.trim();
      const kind = select.value;
      if (delivery?.recipient !== recipient || delivery?.kind !== kind) delivery = {recipient, kind, key: crypto.randomUUID()};
      send.disabled = true;
      feedback.textContent = "Отправляем документ.";
      try {
        const response = await request(`${api}/${orderId}/documents/${payment.attempt_id}/${kind}/email`, {
          method: "POST", headers: {"Idempotency-Key": delivery.key}, body: JSON.stringify({recipient}),
        });
        feedback.textContent = {
          accepted: "Документ принят почтовым сервером.",
          unknown: "Почтовый сервер не подтвердил отправку. Проверьте входящие или скачайте документ.",
          sending: "Отправка ещё обрабатывается.",
          failed: "Письмо не отправлено. Проверьте адрес или повторите через минуту.",
        }[response.status] || "Статус отправки недоступен.";
        if (response.status === "failed") delivery = undefined;
      } catch (error) {
        feedback.textContent = error?.message || "Не удалось проверить отправку. Повторите запрос.";
      } finally {
        send.disabled = false;
      }
    });
    row.append(form);
  }
  return row;
}

async function loadDocuments() {
  const orderId = order?.id;
  const status = byId("document-status");
  const refresh = byId("refresh-documents");
  if (!orderId || refresh.disabled) return;
  refresh.disabled = true;
  status.textContent = "Загружаем документы.";
  try {
    const access = await request(`${api}/${orderId}/documents`);
    if (order?.id !== orderId) return;
    byId("document-list").replaceChildren(...access.payments.map(payment => documentRow(payment, access, orderId)));
    status.textContent = access.payments.length ? "" : "Документы пока недоступны.";
  } catch (error) {
    if (order?.id === orderId) status.textContent = error?.message || "Не удалось загрузить документы.";
  } finally {
    refresh.disabled = false;
  }
}

byId("refresh-documents")?.addEventListener("click", loadDocuments);
byId("payment-documents")?.addEventListener("toggle", event => {
  if (event.currentTarget.open) loadDocuments();
});

byId("check-payment").addEventListener("click", refreshPayment);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && ["creating", "pending", "unknown"].includes(attempt?.status)) refreshPayment();
});
byId("simulate").addEventListener("click", () => run(async () => {
  await request(`/demo/orders/${order.id}/payment-attempts/${attempt.id}/approve`, {
    method: "POST", body: "{}",
  });
  await refreshOrder();
}));
byId("new-order").addEventListener("click", () => run(async () => {
  await newOrder();
  attempt = null;
  lastDelta = "";
  lastMessage = undefined;
  for (const id of ["payment-parts", "payment-tip"]) {
    const select = byId(id);
    if (select) select.selectedIndex = 0;
  }
  if (byId("payment-documents")) {
    byId("payment-documents").open = false;
    byId("document-list").replaceChildren();
    byId("document-status").textContent = "";
  }
  byId("messages").replaceChildren();
  addMessage("agent", "Опишите, что хотите заказать.");
}));

const orderId = document.body.dataset.order;
if (orderId) {
  const load = () => run(async () => {
    order = {id: orderId};
    await refreshOrder();
  });
  load();
} else render();
