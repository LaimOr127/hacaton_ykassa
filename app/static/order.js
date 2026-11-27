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
  const canPay = Boolean(order?.payable_kopecks && !locked && !review);
  const kind = byId("payment-kind");
  if (kind) {
    byId("payment-choice").hidden = !canPay;
    kind.disabled = busy;
  }
  byId("pay").hidden = !canPay;
  byId("pay").disabled = busy;
  byId("pay").textContent = order?.payable_kopecks
    ? `${kind?.value === "invoice" ? "Выставить счёт на" : "Подтвердить и оплатить"} ${money(order.payable_kopecks)}` : "Подтвердить заказ";
  byId("checkout").hidden = !attempt?.confirmation_url;
  byId("checkout").textContent = attempt?.kind === "invoice" ? "Открыть счёт в ЮKassa" : "Перейти к оплате в ЮKassa";
  byId("checkout-hint").hidden = !attempt?.confirmation_url;
  byId("copy-invoice").hidden = !navigator.clipboard || attempt?.kind !== "invoice" || !attempt?.confirmation_url;
  if (byId("copy-invoice").hidden) byId("payment-action-status").textContent = "";
  if (attempt?.confirmation_url) byId("checkout").href = attempt.confirmation_url;
  else byId("checkout").removeAttribute("href");
  byId("check-payment").hidden = !["creating", "pending", "unknown"].includes(attempt?.status) && review?.status !== "pending";
  byId("check-payment").disabled = polling || busy;
  byId("simulate").hidden = !simulated || attempt?.status !== "pending";
  byId("simulate").disabled = busy;
  byId("new-order").hidden = !["paid", "deposit_received"].includes(order?.status);
  byId("new-order").disabled = busy;
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
    if (["succeeded", "canceled", "failed"].includes(attempt.status)) order = await request(`${api}/${order.id}`);
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
    if (["stale_version", "order_locked", "payment_in_progress", "seller_review_required"].includes(error?.code) && order) {
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
  const kind = byId("payment-kind")?.value ?? "payment";
  if (payKey?.version !== order.version || payKey?.kind !== kind) {
    payKey = {version: order.version, id: crypto.randomUUID(), kind};
  }
  attempt = await request(`${api}/${order.id}/payment-attempts`, {
    method: "POST", headers: {"Idempotency-Key": payKey.id}, body: JSON.stringify({order_version: payKey.version, kind}),
  });
  payKey = undefined;
  order = await request(`${api}/${order.id}`);
}));

byId("payment-kind")?.addEventListener("change", render);
byId("copy-invoice").addEventListener("click", async () => {
  const feedback = byId("payment-action-status");
  if (!attempt?.confirmation_url || attempt.kind !== "invoice") return;
  try {
    await navigator.clipboard.writeText(attempt.confirmation_url);
    feedback.textContent = "Ссылка скопирована.";
  } catch {
    feedback.textContent = "Откройте счёт и скопируйте ссылку из адресной строки.";
  }
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
