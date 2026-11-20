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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "3000"))
ADDON_TOKEN = os.environ.get("ADDON_TOKEN", "")      # секрет для аддона
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")      # секрет для админ-запросов
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")          # токен бота (только для уведомлений)
# Только эти Telegram ID могут пользоваться ботом (@termuxqy и @monkqa). Остальным бот не отвечает.
ALLOWED_IDS = {int(x) for x in os.environ.get("ALLOWED_IDS", "7869659514,6413320766").split(",") if x.strip().isdigit()}
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "https://site.minitim.fun")
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


def notify(text, order_id=None):
    """Уведомление всем разрешённым админам; к новому заказу добавляется кнопка «Оплачен»."""
    if not BOT_TOKEN:
        return
    markup = ({"inline_keyboard": [[{"text": "✅ Оплачен", "callback_data": f"paid:{order_id}"}]]}
              if order_id else None)

    def run():
        for uid in ALLOWED_IDS:
            try:
                params = {"chat_id": uid, "text": text}
                if markup:
                    params["reply_markup"] = markup
                tg("sendMessage", **params)
            except Exception:
                pass  # админ мог не нажать «Старт» у бота

    threading.Thread(target=run, daemon=True).start()


STATUS = {"pending": "ждёт оплаты", "paid": "оплачен, ждёт выдачи", "delivered": "выдан"}


def _fmt(o):
    return f"#{o['id']} {o['nick']} — {ITEMS[o['item']]['title']}, {o['price']} руб. [{STATUS.get(o['status'], o['status'])}]"


def handle_update(u):
    cb = u.get("callback_query")
    if cb:
        if cb.get("from", {}).get("id") not in ALLOWED_IDS:
            return  # чужим не отвечаем
        data = cb.get("data", "")
        if data.startswith("paid:"):
            o = mark_paid(data[5:])
            tg("answerCallbackQuery", callback_query_id=cb["id"],
               text="Оплачен, аддон выдаст товар" if o else "Заказ не найден или уже оплачен")
            msg = cb.get("message")
            if o and msg:
                try:
                    tg("editMessageReplyMarkup", chat_id=msg["chat"]["id"],
                       message_id=msg["message_id"], reply_markup={"inline_keyboard": []})
                except Exception:
                    pass
        return
    m = u.get("message")
    if not m or m.get("chat", {}).get("type") != "private":
        return
    if m.get("from", {}).get("id") not in ALLOWED_IDS:
        return  # чужим не отвечаем: ни на /start, ни на что другое
    text = (m.get("text") or "").strip()
    parts = text.split()
    cmd = parts[0].split("@")[0].lower() if parts else ""

    def reply(t):
        tg("sendMessage", chat_id=m["chat"]["id"], text=t)

    if cmd == "/start":
        reply("Панель заказов MiniTim SMP\n\n/orders - последние заказы\n/pending - ждут оплаты\n/paid <id> - отметить оплаченным")
    elif cmd in ("/orders", "/pending"):
        with _lock:
            lst = [o for o in orders if cmd == "/orders" or o["status"] == "pending"][-10:]
        reply("\n".join(_fmt(o) for o in lst) or "Заказов нет")
    elif cmd == "/paid":
        o = mark_paid(parts[1]) if len(parts) > 1 else None
        reply(f"Заказ #{o['id']} помечен оплаченным. Аддон выдаст товар." if o else "Заказ не найден или уже оплачен. Формат: /paid <id>")


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


def mark_paid(order_id):
    """Точка для платёжной системы: когда оплата подтверждена, вызови mark_paid(id)."""
    with _lock:
        o = next((x for x in orders if x["id"] == order_id), None)
        if not o or o["status"] != "pending":
            return None
        o["status"] = "paid"
        o["paidAt"] = int(time.time())
        _save()
    notify(f"Оплачен заказ #{o['id']}: {o['nick']} — {ITEMS[o['item']]['title']} ({o['price']} руб.)")
    return o


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
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
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
        return h.startswith("Bearer ") and hmac.compare_digest(h[7:], token)

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
                       for o in orders if o["status"] == "paid"]
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
            notify(f"Новый заказ #{order['id']}: {nick} — {item['title']} ({item['price']} руб.)", order["id"])
            # paymentUrl появится, когда подключишь платёжку.
            return self._send(200, {"id": order["id"], "status": "pending", "paymentUrl": None})
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
                if not o or o["status"] != "paid":
                    return self._send(404, {"error": "not_found_or_not_paid"})
                o["status"] = "delivered"
                o["deliveredAt"] = int(time.time())
                _save()
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
