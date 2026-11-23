# MiniTim SMP: сервис заказов и автовыдачи.
# Только стандартная библиотека Python 3.8+, requirements.txt не меняется.
# Запуск отдельным проектом: python pay_service.py (назови файл main.py)
# Запуск внутри бота: import main; main.start()
import hmac
import json
import os
import re
import secrets
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "3000"))
ADDON_TOKEN = os.environ.get("ADDON_TOKEN", "")      # секрет для аддона
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")      # секрет для админ-запросов
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")          # токен бота (только для уведомлений)
# Только эти Telegram ID могут пользоваться ботом (@termuxqy и @monkqa). Остальным бот не отвечает.
ALLOWED_IDS = {int(x) for x in os.environ.get("ALLOWED_IDS", "7869659514,6413320766").split(",") if x.strip().isdigit()}
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "https://site.minitim.fun")
# ТЕСТ: TEST_PAY=1 - заказ с сайта сразу считается оплаченным (без денег). После теста уберите переменную!
TEST_PAY = os.environ.get("TEST_PAY", "") == "1"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.environ.get("DATA_FILE") or os.path.join(BASE_DIR, "data", "orders.json")

# Цены только здесь: сайту доверять нельзя.
ITEMS = {
    "priv_titan": {"title": "Привилегия Titan", "price": 49},
    "priv_vip": {"title": "Привилегия VIP", "price": 99},
    "priv_god": {"title": "Привилегия God", "price": 199},
    "kit_titan": {"title": "Кит Titan", "price": 49},
    "kit_vip": {"title": "Кит VIP", "price": 79},
    "kit_god": {"title": "Кит God", "price": 119},
}

_lock = threading.Lock()
_hits = {}  # ограничение: 5 заказов в минуту с одного IP


def _load():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


orders = _load()


def _save():
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DATA_FILE)


def tg(method, **params):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read())


# Статусы заказа: создан -> оплачен -> в очереди на выдачу -> выдан (или отклонён).
STATUS = {
    "pending": "⏳ ждёт оплаты",
    "paid": "💰 оплачен, ждёт выдачи",
    "queued": "🚚 в очереди, выдаст аддон",
    "delivered": "✅ выдан",
    "rejected": "❌ отклонён",
}
MSK = timezone(timedelta(hours=3))


def _when(ts):
    return datetime.fromtimestamp(ts, MSK).strftime("%d.%m %H:%M")


def render_order(o):
    """Текст заказа и кнопки под ним: набор кнопок зависит от статуса."""
    text = (f"Заказ #{o['id']}\nИгрок: {o['nick']}\nТовар: {ITEMS[o['item']]['title']}\n"
            f"Цена: {o['price']} руб.\nСтатус: {STATUS.get(o['status'], o['status'])}\n"
            f"Создан: {_when(o['created'])} (МСК)")
    st = o["status"]
    if st == "pending":
        row = [{"text": "✅ Оплата пришла", "callback_data": f"paid:{o['id']}"},
               {"text": "❌ Отклонить", "callback_data": f"rej:{o['id']}"}]
    elif st == "paid":
        row = [{"text": "🎁 Выдать", "callback_data": f"give:{o['id']}"},
               {"text": "❌ Отклонить", "callback_data": f"rej:{o['id']}"}]
    else:
        row = []
    return text, {"inline_keyboard": [row] if row else []}


def notify(text, markup=None):
    """Сообщение всем разрешённым админам (каждый должен один раз нажать «Старт» у бота)."""
    if not BOT_TOKEN:
        return

    def run():
        for uid in ALLOWED_IDS:
            try:
                params = {"chat_id": uid, "text": text}
                if markup and markup.get("inline_keyboard"):
                    params["reply_markup"] = markup
                tg("sendMessage", **params)
            except Exception:
                pass

    threading.Thread(target=run, daemon=True).start()


def notify_order(prefix, o):
    text, markup = render_order(o)
    notify(f"{prefix}\n\n{text}", markup)


def set_status(order_id, allowed_from, new_status, stamp):
    """Переводит заказ в новый статус, если сейчас он в одном из allowed_from."""
    with _lock:
        o = next((x for x in orders if x["id"] == order_id), None)
        if not o or o["status"] not in allowed_from:
            return None
        o["status"] = new_status
        o[stamp] = int(time.time())
        _save()
        return dict(o)


def mark_paid(order_id, prefix="💰 Пришла оплата!"):
    """Оплата подтверждена. Сюда же вызывает платёжная система (вебхук) или админ кнопкой.
    Админам приходит сообщение «Пришла оплата» с кнопкой «Выдать»."""
    o = set_status(order_id, ("pending",), "paid", "paidAt")
    if o:
        notify_order(prefix, o)
    return o


def queue_for_delivery(order_id):
    """Админ нажал «Выдать»: заказ уходит в очередь, аддон заберёт его и выдаст товар в игре."""
    return set_status(order_id, ("paid",), "queued", "queuedAt")


def reject_order(order_id):
    return set_status(order_id, ("pending", "paid"), "rejected", "rejectedAt")


MENU = [["📦 Новые заказы", "💰 К выдаче"], ["🚚 В очереди", "✅ Выданные"], ["📋 Все заказы", "ℹ️ Помощь"]]
MENU_MARKUP = {"keyboard": MENU, "resize_keyboard": True, "is_persistent": True}
HELP = ("Панель заказов MiniTim SMP\n\n"
        "📦 Новые заказы - созданы на сайте, оплата не подтверждена\n"
        "💰 К выдаче - оплачены, нажми «Выдать»\n"
        "🚚 В очереди - ждут, пока аддон выдаст в игре\n"
        "✅ Выданные - последние выданные\n"
        "📋 Все заказы - последние 10\n\n"
        "Команды: /orders, /paid <id>, /give <id>")
LISTS = {
    "📦 Новые заказы": ("pending", "Новых заказов нет"),
    "💰 К выдаче": ("paid", "Нет оплаченных заказов, ждущих выдачи"),
    "🚚 В очереди": ("queued", "В очереди пусто"),
    "✅ Выданные": ("delivered", "Выданных заказов ещё нет"),
    "📋 Все заказы": (None, "Заказов пока нет"),
}


def send_orders(chat_id, status, empty_text):
    with _lock:
        lst = [dict(o) for o in orders if status is None or o["status"] == status][-10:]
    if not lst:
        tg("sendMessage", chat_id=chat_id, text=empty_text)
        return
    for o in lst:
        text, markup = render_order(o)
        params = {"chat_id": chat_id, "text": text}
        if markup["inline_keyboard"]:
            params["reply_markup"] = markup
        tg("sendMessage", **params)


def _refresh(cb, o):
    """Обновить сообщение с заказом после нажатия кнопки."""
    msg = cb.get("message")
    if not msg:
        return
    text, markup = render_order(o)
    try:
        tg("editMessageText", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
           text=text, reply_markup=markup)
    except Exception:
        pass


def handle_update(u):
    cb = u.get("callback_query")
    if cb:
        if cb.get("from", {}).get("id") not in ALLOWED_IDS:
            return  # чужим не отвечаем
        action, _, oid = cb.get("data", "").partition(":")
        fn, ok_text = {
            "paid": (mark_paid, "Оплата подтверждена. Теперь можно выдать"),
            "give": (queue_for_delivery, "Отправлено на выдачу: аддон выдаст товар в игре"),
            "rej": (reject_order, "Заказ отклонён"),
        }.get(action, (None, ""))
        if not fn:
            return
        o = fn(oid)
        tg("answerCallbackQuery", callback_query_id=cb["id"],
           text=ok_text if o else "Заказ не найден или уже обработан")
        if o:
            _refresh(cb, o)
        return
    m = u.get("message")
    if not m or m.get("chat", {}).get("type") != "private":
        return
    if m.get("from", {}).get("id") not in ALLOWED_IDS:
        return  # чужим не отвечаем: ни на /start, ни на что другое
    chat_id = m["chat"]["id"]
    text = (m.get("text") or "").strip()
    parts = text.split()
    cmd = parts[0].split("@")[0].lower() if parts else ""

    if cmd == "/start" or text == "ℹ️ Помощь":
        tg("sendMessage", chat_id=chat_id, text=HELP, reply_markup=MENU_MARKUP)
    elif text in LISTS:
        send_orders(chat_id, *LISTS[text])
    elif cmd == "/orders":
        send_orders(chat_id, *LISTS["📋 Все заказы"])
    elif cmd in ("/paid", "/give"):
        fn = mark_paid if cmd == "/paid" else queue_for_delivery
        o = fn(parts[1]) if len(parts) > 1 else None
        tg("sendMessage", chat_id=chat_id,
           text=(f"Заказ #{o['id']}: {STATUS[o['status']]}" if o
                 else f"Заказ не найден или не в нужном статусе. Формат: {cmd} <id>"))


def bot_loop():
    if not BOT_TOKEN:
        print("BOT_TOKEN не задан: Telegram-бот выключен", flush=True)
        return
    try:
        tg("deleteWebhook")  # для getUpdates не должно быть активного webhook
    except Exception:
        pass
    offset = 0
    while True:
        try:
            res = tg("getUpdates", offset=offset, timeout=25, allowed_updates=["message", "callback_query"])
            for u in res.get("result", []):
                offset = u["update_id"] + 1
                try:
                    handle_update(u)
                except Exception:
                    pass
        except Exception:
            time.sleep(5)


def _limited(ip):
    now = time.time()
    lst = [t for t in _hits.get(ip, []) if now - t < 60]
    lst.append(now)
    _hits[ip] = lst
    return len(lst) > 5


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, obj):
        body = b"" if code == 204 else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        if code != 204:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Auth-Token")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 4096:
                return None
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return None

    def _authed(self, token):
        if not token:
            return False  # токен не задан: доступ закрыт
        h = self.headers.get("Authorization", "")
        if h.startswith("Bearer ") and hmac.compare_digest(h[7:], token):
            return True
        x = self.headers.get("X-Auth-Token", "")
        return bool(x) and hmac.compare_digest(x, token)

    def do_OPTIONS(self):
        self._send(204, {})

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            return self._send(200, {"ok": True})
        if path == "/queue":  # аддон: оплаченные, но ещё не выданные
            if not self._authed(ADDON_TOKEN):
                return self._send(401, {"error": "unauthorized"})
            with _lock:
                lst = [{k: o[k] for k in ("id", "nick", "item", "price")}
                       for o in orders if o["status"] == "queued"]
            return self._send(200, lst)
        if path == "/admin/orders":
            if not self._authed(ADMIN_TOKEN):
                return self._send(401, {"error": "unauthorized"})
            with _lock:
                return self._send(200, orders[-50:])
        self._send(404, {"error": "not_found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/order":  # сайт создаёт заказ
            ip = (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()
            if _limited(ip):
                return self._send(429, {"error": "too_many_requests"})
            b = self._json()
            if not isinstance(b, dict):
                return self._send(400, {"error": "bad_json"})
            nick = str(b.get("nick", "")).strip()
            if not re.fullmatch(r"[A-Za-z0-9_ ]{3,20}", nick):
                return self._send(400, {"error": "bad_nick"})
            item = ITEMS.get(b.get("item"))
            if not item:
                return self._send(400, {"error": "bad_item"})
            order = {"id": secrets.token_hex(4), "nick": nick, "item": b["item"],
                     "price": item["price"], "status": "pending", "created": int(time.time())}
            with _lock:
                orders.append(order)
                _save()
            if TEST_PAY:
                # Тестовый режим: оплата засчитывается сразу, админам приходит «Пришла оплата» + кнопка «Выдать».
                mark_paid(order["id"], "🧪 ТЕСТ: оплата засчитана автоматически")
            else:
                notify_order("🆕 Новый заказ с сайта", order)
            # paymentUrl появится, когда подключишь платёжку.
            return self._send(200, {"id": order["id"], "status": "paid" if TEST_PAY else "pending",
                                    "paymentUrl": None, "test": TEST_PAY})
        if path == "/admin/paid":  # вручную пометить оплаченным (тест, ручная оплата)
            if not self._authed(ADMIN_TOKEN):
                return self._send(401, {"error": "unauthorized"})
            b = self._json()
            o = isinstance(b, dict) and mark_paid(str(b.get("id", "")))
            return self._send(200, {"ok": True}) if o else self._send(404, {"error": "not_found_or_not_pending"})
        if path == "/done":  # аддон: заказ выдан
            if not self._authed(ADDON_TOKEN):
                return self._send(401, {"error": "unauthorized"})
            b = self._json()
            with _lock:
                o = next((x for x in orders if isinstance(b, dict) and x["id"] == str(b.get("id", ""))), None)
                if not o or o["status"] != "queued":
                    return self._send(404, {"error": "not_found_or_not_queued"})
                o["status"] = "delivered"
                o["deliveredAt"] = int(time.time())
                _save()
                done = dict(o)
            notify(f"✅ Выдано в игре: {done['nick']} — {ITEMS[done['item']]['title']} (заказ #{done['id']})")
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not_found"})


def start():
    """Запуск в фоновом потоке (для встраивания в бота)."""
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=bot_loop, daemon=True).start()
    print(f"Сервис заказов запущен на порту {PORT}", flush=True)
    return srv


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=bot_loop, daemon=True).start()
    print(f"Сервис заказов запущен на порту {PORT}", flush=True)
    srv.serve_forever()
