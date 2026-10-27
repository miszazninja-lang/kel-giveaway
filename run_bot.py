"""Свой бот честных розыгрышей — тот же код, что работает у @KelLegionBot.

    BOT_TOKEN=123:ABC PUBLIC_URL=https://твой-сервер python run_bot.py

BOT_TOKEN  — токен от @BotFather;
PUBLIC_URL — адрес, по которому снаружи открывается этот сервер (на нём страница проверки /give/<id>);
PORT       — порт страницы (по умолчанию 8080); GIVE_DB — файл базы (по умолчанию give.db).
Нужен только Python 3.8+, без библиотек. Бот работает через getUpdates — вебхук не нужен.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import kel_give as kg

TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", "8080"))
PUBLIC = os.environ.get("PUBLIC_URL", f"http://localhost:{PORT}")
DB = os.environ.get("GIVE_DB", "give.db")


def api(token, method, **params) -> dict:
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                 data=urllib.parse.urlencode(params).encode("utf-8"))
    try:
        with urllib.request.urlopen(req, timeout=40) as r:   # noqa: S310
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {"ok": False, "description": f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "description": type(e).__name__}


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB, timeout=30)
    con.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)")
    return con


def kv_get(con, key):
    r = con.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return r[0] if r else None


def kv_set(con, key, value):
    con.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, str(value)))
    con.commit()


def kv_del(con, key):
    con.execute("DELETE FROM kv WHERE k=?", (key,))
    con.commit()


def kv_keys(con, prefix):
    return [r[0] for r in con.execute("SELECT k FROM kv WHERE k LIKE ?", (prefix + "%",)).fetchall()]


class Page(BaseHTTPRequestHandler):
    def do_GET(self):
        con = db()
        try:
            r = kg.http_get(con, urllib.parse.urlparse(self.path).path)
        finally:
            con.close()
        code, ctype, body = r or (404, "text/plain; charset=utf-8", "Не найдено".encode("utf-8"))
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main() -> None:
    if not TOKEN:
        raise SystemExit("Задай BOT_TOKEN (токен от @BotFather).")
    me = api(TOKEN, "getMe")
    if not me.get("ok"):
        raise SystemExit("Telegram не принял токен: " + str(me.get("description")))
    kg.setup(api=api, kv_get=kv_get, kv_set=kv_set, kv_del=kv_del, kv_keys=kv_keys, base_url=PUBLIC,
             username=me["result"]["username"], source_url=os.environ.get("SOURCE_URL", ""))
    api(TOKEN, "setMyCommands", commands=json.dumps([{"command": "give", "description": "Создать розыгрыш"}]))
    threading.Thread(target=kg.run_loop, args=(db, TOKEN), daemon=True).start()
    threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", PORT), Page).serve_forever, daemon=True).start()
    print(f"@{me['result']['username']} работает, страница проверки: {PUBLIC}/give/<id>")
    offset = 0
    while True:
        r = api(TOKEN, "getUpdates", offset=offset, timeout=30,
                allowed_updates=json.dumps(["message", "callback_query"]))
        if not r.get("ok"):
            time.sleep(3)
            continue
        for u in r.get("result") or []:
            offset = u["update_id"] + 1
            con = db()
            try:
                msg, cb = u.get("message"), u.get("callback_query")
                if msg and not kg.on_message(con, TOKEN, msg) and (msg.get("chat") or {}).get("type") == "private":
                    api(TOKEN, "sendMessage", chat_id=msg["chat"]["id"],
                        text="Привет! Я провожу честные розыгрыши в каналах и группах. Создать — /give")
                elif cb and not kg.on_callback(con, TOKEN, cb):
                    api(TOKEN, "answerCallbackQuery", callback_query_id=cb.get("id"))
            except Exception as e:  # noqa: BLE001
                print("ошибка:", type(e).__name__, e)
            finally:
                con.close()


if __name__ == "__main__":
    main()
