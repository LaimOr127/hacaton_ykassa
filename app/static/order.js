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
  if (!attempt) return lastDelta;
  const messages = {
    creating: "Создаём платёж.",
    pending: simulated ? "Готово к симуляции оплаты." : "Ожидаем подтверждения ЮKassa.",
    succeeded: simulated ? "Симуляция оплаты завершена." : (
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
  byId("deposit").hidden = !(order?.balance_kopecks > 0);
  byId("deposit").textContent = order?.balance_kopecks > 0
    ? `Сейчас ${money(order.payable_kopecks)}, остаток ${money(order.balance_kopecks)}` : "";
  byId("payment-status").textContent = describePayment();
  byId("text").disabled = locked || busy;
  byId("send").disabled = locked || busy;
  byId("pay").hidden = !order?.payable_kopecks || locked;
  byId("pay").disabled = busy;
  byId("pay").textContent = order?.payable_kopecks
    ? `Подтвердить и оплатить ${money(order.payable_kopecks)}` : "Подтвердить заказ";
  byId("checkout").hidden = !attempt?.confirmation_url;
  if (attempt?.confirmation_url) byId("checkout").href = attempt.confirmation_url;
  else byId("checkout").removeAttribute("href");
  byId("check-payment").hidden = !["creating", "pending", "unknown"].includes(attempt?.status);
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
  if (["creating", "pending"].includes(attempt?.status)) timer = setTimeout(refreshPayment, 7000);
}

async function run(action) {
  if (busy || polling) return;
  busy = true;
  showNotice();
  render();
  try {
    await action();
  } catch (error) {
    if (["stale_version", "order_locked", "payment_in_progress"].includes(error?.code) && order) {
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
  if (payKey?.version !== order.version) payKey = {version: order.version, id: crypto.randomUUID()};
  attempt = await request(`${api}/${order.id}/payment-attempts`, {
    method: "POST", headers: {"Idempotency-Key": payKey.id}, body: JSON.stringify({order_version: payKey.version}),
  });
  payKey = undefined;
  order = await request(`${api}/${order.id}`);
}));

byId("check-payment").addEventListener("click", refreshPayment);
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
