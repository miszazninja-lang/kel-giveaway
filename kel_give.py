"""Честный розыгрыш в Telegram — бот (26.10.2026, просьба владельца: «открыты для всех, без заявок, максимально
прозрачно, как известные боты розыгрышей, только честно»).

Кто угодно пишет боту /give → выбирает свой канал или группу (бот там админ) → приз, число победителей, срок,
условие «подписка» → бот публикует пост с кнопкой «Участвовать». Кнопка ведёт в личку бота: там проверка подписки
и билет (номер + случайный код — по коду участник находит себя в открытом списке). Итог — сам по сроку, по формуле
из give_core.py: случайное число drand, которое выходит ПОСЛЕ того, как список зафиксирован отпечатком.
Страница /give/<id> показывает список и пересчитывает итог в браузере, беря число у drand напрямую.

Модуль не знает, в каком боте живёт: хозяин передаёт в setup() вызов Telegram API и хранилище ключ-значение.
"""

from __future__ import annotations

import html
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

import give_core as core

KEY = "gv:"                  # + id → розыгрыш (JSON)
DRAFT = "gvd:"               # + tg id → черновик создателя
DRAFT_TTL = 3600
MSK = 3 * 3600               # время в постах — по Москве (аудитория senkuro)
DUR_MIN, DUR_MAX = 5 * 60, 30 * 86400
WIN_MAX = 100
PRIZE_MAX = 300
# 02.11.2026, владелец прислал примеры чужих розыгрышей (свой текст с местами и ссылками, «итоги, когда будет 130
# участников»): свой текст поста, итоги по числу участников, «перешли пост — сделаю такой же».
DESC_MAX = 700               # свой текст поста (с картинкой подпись ≤1024 вместе с нашим блоком — иначе текстом)
TITLE_MAX = 120              # название в итогах и личках — первая строка своего текста
GOAL_MAX = 5_000             # «итоги, когда наберётся N участников»
GOAL_DUR = 7 * 86400         # …но не позже, чем через столько
GIVE_HINT_RE = re.compile(r"розыгр|конкурс|giveaway|\bприз(?:ы|а|у|е|ом|ами|ах|ов\w*)?\b", re.I)  # не «призрак/призыв»
SUBS_MAX = 5                 # каналов-условий
ACTIVE_MAX = 20              # идущих розыгрышей у одного создателя (защита сервера от спама)
SHOWN_EVERY = 30             # счётчик на кнопке поста — не чаще (лимит правок Telegram)
COMPACT_AFTER = 30 * 86400   # старые итоги: убрать tg id и имена, список и проверка остаются
TICK_SEC = 15
TEST_UID_MIN = 9_000_000_000_000   # тестовые id хозяина (у Кель — «режим новичка»): билета не даём
_LOCK = threading.RLock()
_HERE = Path(__file__).resolve().parent


class _Host:
    api = None               # api(token, method, **params) -> dict (ответ Telegram)
    kv_get = None            # (con, key) -> str | None
    kv_set = None            # (con, key, value) -> None (с commit)
    kv_del = None            # (con, key) -> None
    kv_keys = None           # (con, prefix) -> list[str]
    get_json = None          # (url) -> dict; None — urllib из give_core
    mark_backup = None       # (con) -> None: попросить хозяина сохранить данные поскорее
    base_url = ""            # https://<сервер> — страница проверки /give/<id>
    source_url = ""          # где лежит открытый код
    username = ""            # имя бота без @
    promo = ""               # строка «кто мы» после записи в розыгрыш
    now = staticmethod(time.time)


H = _Host()


def setup(**kw) -> None:
    for k, v in kw.items():
        if not hasattr(_Host, k):
            raise AttributeError(k)
        setattr(H, k, v)


# ---------- хранилище ----------
def _load(con, gid: str):
    try:
        raw = H.kv_get(con, KEY + gid)
        return json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001
        return None


def _save(con, g: dict, urgent: bool = True) -> None:
    H.kv_set(con, KEY + g["id"], json.dumps(g, ensure_ascii=False, separators=(",", ":")))
    if urgent and H.mark_backup:
        try:
            H.mark_backup(con)
        except Exception:  # noqa: BLE001
            pass


def _draft(con, uid):
    try:
        d = json.loads(H.kv_get(con, DRAFT + str(uid)) or "null")
    except Exception:  # noqa: BLE001
        d = None
    if d and H.now() - float(d.get("ts") or 0) > DRAFT_TTL:
        H.kv_del(con, DRAFT + str(uid))
        return None
    return d


def _draft_put(con, uid, d: dict) -> None:
    d["ts"] = int(H.now())
    H.kv_set(con, DRAFT + str(uid), json.dumps(d, ensure_ascii=False))


def _all(con) -> list:
    out = []
    for key in H.kv_keys(con, KEY):
        g = _load(con, key[len(KEY):])
        if g:
            out.append(g)
    return out


def open_now(con) -> list:
    """Идущие розыгрыши (приём открыт), ближайшие итоги — первыми. Хозяин сам решает, какие показать у себя."""
    now = H.now()
    return sorted((g for g in _all(con) if g.get("state") == "open" and now < float(g.get("end") or 0)),
                  key=lambda g: g["end"])


# ---------- Telegram ----------
def _api(token, method, **p) -> dict:
    try:
        return H.api(token, method, **p) or {}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "description": type(e).__name__}


def _send(token, chat, text, kb=None, html_mode=False, reply_to=None, preview=False) -> dict:
    p = {"chat_id": chat, "text": text[:4096]}
    if kb is not None:
        p["reply_markup"] = json.dumps(kb, ensure_ascii=False)
    if html_mode:
        p["parse_mode"] = "HTML"
    if reply_to:
        p["reply_parameters"] = json.dumps({"message_id": reply_to, "allow_sending_without_reply": True})
    if not preview:
        p["link_preview_options"] = json.dumps({"is_disabled": True})
    return _api(token, "sendMessage", **p)


_MEDIA_API = {"photo": ("sendPhoto", "photo"), "animation": ("sendAnimation", "animation"),
              "video": ("sendVideo", "video")}
CAPTION_MAX = 1024           # подпись к фото/видео у Telegram


def _media_of(msg: dict):
    """Фото (самое крупное), гифка или видео из сообщения организатора — {t, id} или None."""
    if msg.get("photo"):
        return {"t": "photo", "id": msg["photo"][-1]["file_id"]}
    for t in ("animation", "video"):              # у гифки есть и document — она проверяется первой
        if isinstance(msg.get(t), dict) and msg[t].get("file_id"):
            return {"t": t, "id": msg[t]["file_id"]}
    return None


def _send_post(token, chat, text, media, kb=None) -> dict:
    """Пост розыгрыша (02.11.2026, владелец: «приз — фоткой и т. д., как в чатах выглядят розыгрыши»): с картинкой
    организатора — подписью к ней, без картинки, с длинным текстом (подпись — до 1024 знаков, а номер раунда drand
    резать нельзя) или если Telegram её не принял — обычным текстом. У поста с картинкой в ответе r["_media"]."""
    how = _MEDIA_API.get((media or {}).get("t")) if isinstance(media, dict) else None
    if how and media.get("id") and len(text) <= CAPTION_MAX:
        p = {"chat_id": chat, how[1]: media["id"], "caption": text}
        if kb:
            p["reply_markup"] = json.dumps(kb, ensure_ascii=False)
        r = _api(token, how[0], **p)
        if r.get("ok"):
            return {**r, "_media": True}
    return _send(token, chat, text, kb)


def _answer(token, cb, text="", alert=False) -> None:
    p = {"callback_query_id": cb.get("id")}
    if text:
        p["text"] = text[:200]
    if alert:
        p["show_alert"] = "true"
    _api(token, "answerCallbackQuery", **p)


def _bot_id(token) -> int:
    return int(str(token).split(":", 1)[0])


def _status(token, chat, uid):
    """(статус, может ли публиковать): creator/administrator/member/left/kicked; None — Telegram не ответил
    (у бота нет прав в чате или сбой сети)."""
    r = _api(token, "getChatMember", chat_id=chat, user_id=uid)
    if not r.get("ok"):
        desc = str(r.get("description") or "").lower()
        if "user not found" in desc or "participant_id_invalid" in desc or "member not found" in desc:
            return "left", None
        return None
    m = r.get("result") or {}
    st = m.get("status")
    if st == "restricted":
        st = "member" if m.get("is_member") else "left"
    return st, m.get("can_post_messages")


def _st(x):
    return x[0] if x else None


def _is_in(st) -> bool:
    return _st(st) in ("creator", "administrator", "member")


def _btn(text, data):
    return {"text": text, "callback_data": f"k:g:{data}"[:64]}


def _url(text, url):
    return {"text": text, "url": url}


def _verify_url(gid) -> str:
    return f"{(H.base_url or '').rstrip('/')}/give/{gid}"


def _join_url(gid) -> str:
    return f"https://t.me/{H.username}?start=g{gid}"


# ---------- тексты ----------
def _when(ts) -> str:
    t = time.gmtime(int(ts) + MSK)
    return time.strftime("%d.%m в %H:%M", t) + " МСК"


def _left(sec) -> str:
    sec = max(0, int(sec))
    d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
    if d:
        return f"{d} дн" + (f" {h} ч" if h else "")
    if h:
        return f"{h} ч" + (f" {m} мин" if m else "")
    return f"{max(1, m)} мин"


def _chat_name(c: dict) -> str:
    return f"@{c['username']}" if c.get("username") else f"«{c.get('title') or 'чат'}»"


def _cond_text(subs) -> str:
    return ("подписка на " + ", ".join(_chat_name(s) for s in subs)) if subs else "нет — участвуют все"


def _mask(name: str) -> str:
    name = re.sub(r"\s+", " ", str(name or "")).strip() or "?"
    if len(name) <= 2:
        return name[0] + "*"
    return name[0] + "*" * min(5, len(name) - 2) + name[-1]


def _who(u: dict) -> str:
    name = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x).strip()
    return (name or u.get("username") or "Участник")[:64]


def _end_text(g: dict) -> str:
    if g.get("goal_hit"):
        return "скоро — набралось нужное число участников"
    if g.get("goal"):
        return f"когда наберётся {g['goal']} участников (не позже {_when(g['end'])})"
    return _when(g["end"])


def post_text(g: dict) -> str:
    if g.get("goal"):          # раунд назначится при закрытии приёма (_close переносит его, как при досрочном)
        honest = ("🔒 Честно: победителей выберет публичное случайное число drand — раунд назначу в момент закрытия "
                  "приёма. Заранее его не знает никто, даже я. Пересчитать итог может любой — кнопка «Проверить».")
    else:
        honest = (f"🔒 Честно: победителей выберет публичное случайное число drand (раунд №{g['round']}). "
                  "Заранее его не знает никто, даже я. Пересчитать итог может любой — кнопка «Проверить».")
    tail = [f"✅ Условие: {_cond_text(g.get('subs') or [])}", "", honest, f"Розыгрыши без подкрутки — @{H.username}"]
    if g.get("desc"):          # свой текст организатора, как у ботов розыгрышей, и под ним короткий наш блок
        return "\n".join([g["desc"], "", f"🏆 Победителей: {g['k']} · ⏰ Итоги: {_end_text(g)}"] + tail)
    lines = [f"🎁 Розыгрыш: {g['prize']}", "",
             f"🏆 Победителей: {g['k']}",
             f"⏰ Итоги: {_end_text(g)}"] + tail
    return "\n".join(lines)


def _post_kb(g: dict, n=None) -> dict:
    n = len(g.get("entries") or []) if n is None else n
    st = g.get("state")
    if st == "open":
        return {"inline_keyboard": [[_url(f"🎁 Участвовать ({n})", _join_url(g["id"]))],
                                    [_url("🔍 Проверить честность", _verify_url(g["id"]))]]}
    if st in ("closing", "closed"):
        return {"inline_keyboard": [[_url(f"⏳ Приём закрыт · участников {n}", _verify_url(g["id"]))]]}
    if st == "done":
        return {"inline_keyboard": [[_url("🏆 Итоги и проверка", _verify_url(g["id"]))]]}
    return {"inline_keyboard": [[_url("❌ Отменён · подробности", _verify_url(g["id"]))]]}


def _edit_post(token, g, n=None) -> None:
    if not g.get("msg"):
        return
    _api(token, "editMessageReplyMarkup", chat_id=g["chat"]["id"], message_id=g["msg"],
         reply_markup=json.dumps(_post_kb(g, n), ensure_ascii=False))


# ---------- разбор ввода ----------
START_RE = re.compile(r"(?:/give(?:away)?(?:@\w+)?|/start\s+give|🎁\s*розыгрыш|(?:кель\s+)?(?:(?:создать|новый|"
                      r"честный|мой|мои)\s+)?розыгрыш[иа]?)[!.?]*", re.I)
JOIN_RE = re.compile(r"/start\s+g([a-z0-9]{6,16})", re.I)
_DUR_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(мин\w*|м\b|час\w*|ч\b|сут\w*|д(?:н\w*|ень|ня|ней)?\b|нед\w*)", re.I)
_TOKENISH = re.compile(r"v\d\.(?:local|public)\.", re.I)    # токен senkuro — не наше, отдать хозяину


def parse_duration(text: str):
    total = 0.0
    for num, unit in _DUR_RE.findall(text or ""):
        x = float(num.replace(",", "."))
        u = unit.lower()
        total += x * (60 if u.startswith("м") else 3600 if u.startswith(("ч", "час")) else
                      7 * 86400 if u.startswith("нед") else 86400)
    if not total:
        return None
    return int(max(DUR_MIN, min(DUR_MAX, total)))


def _chat_ref(msg: dict):
    """Канал/группа из сообщения: кнопка выбора чата, пересланный пост, @имя или ссылка t.me."""
    sh = msg.get("chat_shared")
    if sh and sh.get("chat_id"):
        return sh["chat_id"]
    fo = msg.get("forward_origin") or {}
    if fo.get("chat"):
        return fo["chat"].get("id")
    if msg.get("forward_from_chat"):
        return msg["forward_from_chat"].get("id")
    m = re.search(r"(?:t\.me/|@)([A-Za-z][A-Za-z0-9_]{3,31})\b", msg.get("text") or "")
    return f"@{m.group(1)}" if m else None


def _rights(post: bool) -> dict:
    r = {k: False for k in ("is_anonymous", "can_manage_chat", "can_delete_messages", "can_manage_video_chats",
                            "can_restrict_members", "can_promote_members", "can_change_info", "can_invite_users",
                            "can_post_stories", "can_edit_stories", "can_delete_stories")}
    if post:
        r["can_post_messages"] = True
    return r


def _pick_kb(req_id: int, channel_only=False) -> dict:
    ch = {"text": "📢 Выбрать канал", "request_chat": {
        "request_id": req_id, "chat_is_channel": True, "user_administrator_rights": _rights(True),
        "bot_administrator_rights": _rights(True), "request_title": True, "request_username": True}}
    gr = {"text": "👥 Выбрать группу", "request_chat": {
        "request_id": req_id + 1, "chat_is_channel": False, "user_administrator_rights": _rights(False),
        "bot_is_member": True, "request_title": True, "request_username": True}}
    rows = [[ch]] if channel_only else [[ch, gr]]
    rows.append([{"text": "✖️ Отмена"}])
    return {"keyboard": rows, "resize_keyboard": True, "one_time_keyboard": True}


_NO_KB = {"remove_keyboard": True}


# ---------- создание ----------
def _menu(con, token, uid, chat) -> None:
    mine = [g for g in _all(con) if g.get("owner") == uid and g.get("state") in ("open", "closed")]
    if not mine:
        _new(con, token, uid, chat)
        return
    rows, lines = [], ["🎁 Твои розыгрыши:"]
    for g in mine[:10]:
        n = len(g.get("entries") or [])
        lines.append(f"• {g['prize'][:60]} — {_chat_name(g['chat'])}, участников {n}, "
                     + (f"итоги {_end_text(g)}" if g["state"] == "open" else "считаю итог"))
        if g["state"] == "open":
            rows.append([_btn("⏹ Закрыть приём сейчас", f"close:{g['id']}"), _btn("❌ Отменить", f"cancel:{g['id']}")])
    rows.append([_btn("➕ Новый розыгрыш", "new")])
    _send(token, chat, "\n".join(lines), {"inline_keyboard": rows})


def _new(con, token, uid, chat, pre=None) -> None:
    active = sum(1 for g in _all(con) if g.get("owner") == uid and g.get("state") in ("open", "closed"))
    if active >= ACTIVE_MAX:
        _send(token, chat, f"У тебя уже {active} идущих розыгрышей — дождись итогов одного из них 🙂")
        return
    _draft_put(con, uid, {"step": "chat", **({"pre": pre} if isinstance(pre, dict) else {})})
    _send(token, chat,
          "🎁 Честный розыгрыш — бесплатно и для всех.\n\n"
          "1. Добавь меня админом в свой канал (право «Публикация сообщений») или в группу.\n"
          "2. Нажми кнопку ниже и выбери этот чат — или пришли его @адрес, или перешли сюда пост из канала.",
          _pick_kb(1))


def _set_chat(con, token, uid, chat, d, ref, extra=False) -> None:
    r = _api(token, "getChat", chat_id=ref)
    if not r.get("ok"):
        _send(token, chat, "Не вижу такой чат 🤔 Проверь, что я там есть, и выбери снова.",
              _pick_kb(3, True) if extra else _pick_kb(1))
        return
    c = r["result"]
    info = {"id": c["id"], "title": (c.get("title") or "")[:80], "username": c.get("username") or "",
            "type": c.get("type")}
    if extra:
        st = _status(token, info["id"], uid)
        if st is None:
            _send(token, chat, f"Не могу проверять подписчиков {_chat_name(info)}: добавь меня туда админом "
                               "(хватит любых прав) и выбери снова.", _pick_kb(3, True))
            return
        subs = [s for s in d.get("subs") or [] if s["id"] != info["id"]] + [info]
        d["subs"] = subs[:SUBS_MAX + 1]
        d["step"] = "subs"
        _draft_put(con, uid, d)
        _send(token, chat, f"Добавила условие: подписка на {_chat_name(info)}.", _NO_KB)
        _subs_screen(token, chat, d)
        return
    me = _status(token, info["id"], uid)
    if _st(me) not in ("creator", "administrator"):
        _send(token, chat, f"Ты не админ в {_chat_name(info)} — там розыгрыш может запустить только админ.",
              _pick_kb(1))
        return
    bot = _status(token, info["id"], _bot_id(token))
    can_post = (_st(bot) == "administrator" and (info["type"] != "channel" or bool(bot[1]))) or \
        (_st(bot) == "member" and info["type"] != "channel")
    if not can_post:
        _send(token, chat, f"Меня нет в {_chat_name(info)} или нет права публиковать. Добавь меня админом "
                           "(«Публикация сообщений») и выбери снова.", _pick_kb(1))
        return
    pre = d.pop("pre", None)
    d.update(chat=info, step="prize", subs=[info])
    if isinstance(pre, dict):
        if isinstance(pre.get("media"), dict):
            d["media"] = pre["media"]
        _send(token, chat, f"Розыгрыш будет в {_chat_name(info)}. Картинку и текст беру из пересланного поста.",
              _NO_KB)
        _take_prize(con, token, uid, chat, d, pre.get("text") or "")
        return
    _draft_put(con, uid, d)
    _send(token, chat, f"Розыгрыш будет в {_chat_name(info)}.\n\nЧто разыгрываем? Напиши приз одним сообщением "
                       "(например: «Карта ранга S» или «3 пака»).\n"
                       "Можно фото, гифкой или видео с подписью — пост будет с картинкой, как у ботов розыгрышей. "
                       "Можно и свой текст на несколько строк — места, ссылки, условия своими словами: я добавлю "
                       "под ним кнопку участия и строчку про честный итог.",
          _NO_KB)


def _ask_k(token, chat) -> None:
    _send(token, chat, "Сколько победителей? Нажми или напиши число до 100.",
          {"inline_keyboard": [[_btn(str(n), f"k:{n}") for n in (1, 2, 3, 5, 10)]]})


def _ask_time(token, chat) -> None:
    opts = [("1 ч", 3600), ("3 ч", 3 * 3600), ("12 ч", 12 * 3600), ("1 день", 86400), ("3 дня", 3 * 86400),
            ("7 дней", 7 * 86400)]
    _send(token, chat, "Когда подвести итоги? Нажми или напиши: «45 мин», «2 ч», «5 дней» — или «130 участников»: "
                       "тогда итог будет, как только столько наберётся.",
          {"inline_keyboard": [[_btn(t, f"t:{s}") for t, s in opts[:3]], [_btn(t, f"t:{s}") for t, s in opts[3:]],
                               [_btn("👥 Когда наберётся N участников", "t:goal")]]})


def _ask_goal(token, chat) -> None:
    _send(token, chat, f"Сколько участников нужно? Напиши число — например, 130. Приём закроется, как только столько "
                       f"наберётся, но не позже чем через {GOAL_DUR // 86400} дней.")


def _set_goal(con, token, uid, chat, d, n: int) -> None:
    d.update(goal=n, dur=GOAL_DUR, step="subs")
    _draft_put(con, uid, d)
    _subs_screen(token, chat, d)


def _take_prize(con, token, uid, chat, d, text: str) -> None:
    """Приз одной строкой — как раньше; текст на несколько строк (или длиннее PRIZE_MAX) — свой текст поста (desc),
    название для итогов и личек — его первая строка."""
    raw = str(text or "").replace("\r", "")
    rows = [re.sub(r"[ \t]+", " ", x).strip() for x in raw.split("\n")]
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(rows)).strip()
    if not body:
        _draft_put(con, uid, d)
        _send(token, chat, ("Картинка есть 👍 " if d.get("media") else "") + "Теперь напиши, что разыгрываем, — "
                                                                             "одним сообщением.")
        return
    if "\n" in body or len(body) > PRIZE_MAX:
        if len(body) > DESC_MAX:
            _draft_put(con, uid, d)            # картинка, если была, остаётся
            _send(token, chat, f"Текст длинноват: {len(body)} знаков, в пост влезает до {DESC_MAX} (ещё место нужно "
                               "под кнопку участия и строчку про честность). Сократи и пришли снова.")
            return
        d.update(desc=body, prize=next(x for x in rows if x)[:TITLE_MAX], step="k")
    else:
        d.pop("desc", None)
        d.update(prize=re.sub(r"\s+", " ", body)[:PRIZE_MAX], step="k")
    _draft_put(con, uid, d)
    _ask_k(token, chat)


def _subs_screen(token, chat, d) -> None:
    subs = d.get("subs") or []
    main = d["chat"]
    has_main = any(s["id"] == main["id"] for s in subs)
    rows = [[_btn(("✅ " if has_main else "⬜ ") + f"Подписка на {_chat_name(main)}", "s:main")]]
    for i, s in enumerate(subs):
        if s["id"] != main["id"]:
            rows.append([_btn(f"✖️ Убрать {_chat_name(s)}", f"s:rm:{i}")])
    if len(subs) <= SUBS_MAX:
        rows.append([_btn("➕ Ещё канал", "s:add")])
    rows.append([_btn("Без условий", "s:none"), _btn("Дальше →", "s:ok")])
    _send(token, chat, f"Условие участия: {_cond_text(subs)}.\nПодписку проверю при записи и ещё раз на итогах.",
          {"inline_keyboard": rows})


def _confirm(con, token, uid, chat, d) -> None:
    d["step"] = "confirm"
    _draft_put(con, uid, d)
    end = H.now() + d["dur"]
    fake = {"prize": d["prize"], "k": d["k"], "end": end, "subs": d.get("subs") or [],
            "round": core.round_at(end + core.ROUND_DELAY), "desc": d.get("desc"), "goal": d.get("goal")}
    kb = {"inline_keyboard": [[_btn("🚀 Опубликовать", "pub"), _btn("✖️ Отмена", "x")]]}
    if d.get("media"):
        long_ = len(post_text(fake)) > CAPTION_MAX
        _send(token, chat, "Так будет выглядеть пост 👇" + (
            f"\n⚠️ С картинкой Telegram пускает до {CAPTION_MAX} знаков подписи, а тут {len(post_text(fake))} — "
            "пост выйдет текстом без картинки. Хочешь с картинкой — «✖️ Отмена» и текст покороче." if long_ else ""))
        _send_post(token, chat, post_text(fake), d["media"], kb)
    else:
        _send(token, chat, "Так будет выглядеть пост:\n\n" + post_text(fake), kb)


def _publish(con, token, uid, chat, d, frm) -> None:
    with _LOCK:                                   # два нажатия подряд — один пост
        d = _draft(con, uid)
        if (d or {}).get("step") != "confirm":
            if (d or {}).get("step") != "publishing":
                _send(token, chat, "Черновик устарел — начни заново: /give")
            return
        d["step"] = "publishing"
        _draft_put(con, uid, d)
    now = int(H.now())
    end = now + int(d["dur"])
    gid = "".join(secrets.choice("abcdefghijkmnpqrstuvwxyz23456789") for _ in range(8))
    g = {"id": gid, "v": 1, "owner": uid, "owner_name": _who(frm), "owner_user": frm.get("username") or "",
         "chat": d["chat"], "prize": d["prize"], "k": int(d["k"]), "subs": d.get("subs") or [],
         "created": now, "end": end, "round": core.round_at(end + core.ROUND_DELAY),
         "state": "open", "entries": [], "shown": 0, "shown_ts": 0}
    if d.get("desc"):
        g["desc"] = str(d["desc"])[:DESC_MAX]
    if d.get("goal"):
        g["goal"] = max(2, min(GOAL_MAX, int(d["goal"])))
    if isinstance(d.get("media"), dict):
        g["media"] = d["media"]
    r = _send_post(token, g["chat"]["id"], post_text(g), g.get("media"), _post_kb(g))
    if not r.get("ok"):
        d["step"] = "confirm"
        _draft_put(con, uid, d)
        _send(token, chat, "Не получилось опубликовать: " + str(r.get("description") or "Telegram не ответил")[:150]
              + ". Проверь мои права в чате и нажми «Опубликовать» ещё раз.")
        return
    g["msg"] = (r.get("result") or {}).get("message_id")
    if r.get("_media"):
        g["media_post"] = 1
    with _LOCK:
        _save(con, g)
    H.kv_del(con, DRAFT + str(uid))
    _send(token, chat, f"Готово ✅ Розыгрыш опубликован в {_chat_name(g['chat'])}.\n"
                       + (f"Итоги подведу сама, {_end_text(g)}." if g.get("goal") else
                          f"Итоги подведу сама: {_when(end)} (через {_left(end - now)}).") + " Список и проверка: "
                       f"{_verify_url(gid)}\nУправлять: /give", preview=False)


def _on_text(con, token, uid, chat, d, msg) -> bool:
    text = (msg.get("text") or "").strip()
    step = d.get("step")
    if (text or (msg.get("caption") or "").strip()).lower() in ("✖️ отмена", "отмена", "/cancel"):
        H.kv_del(con, DRAFT + str(uid))
        _send(token, chat, "Отменила черновик. Новый — /give", _NO_KB)
        return True
    if step in ("chat", "sub_add"):
        ref = _chat_ref(msg)
        if ref is None:
            if text.startswith("/") or text.lower().startswith("кель "):
                return False
            _send(token, chat, "Нажми кнопку выбора чата, пришли @адрес или перешли пост из канала.",
                  _pick_kb(3, True) if step == "sub_add" else _pick_kb(1))
            return True
        _set_chat(con, token, uid, chat, d, ref, extra=step == "sub_add")
        return True
    media = _media_of(msg) if step == "prize" else None
    if media:
        d["media"] = media
        text = (msg.get("caption") or "").strip()
        if not text:
            _draft_put(con, uid, d)
            _send(token, chat, "Картинка есть 👍 Теперь напиши, что разыгрываем, — одним сообщением.")
            return True
    if not text or text.startswith("/") or text.lower().startswith("кель "):
        return False
    if step == "prize":
        _take_prize(con, token, uid, chat, d, text)
        return True
    if step == "goal":
        m = re.fullmatch(r"(\d{1,6})(?:\s*(?:участ\w*|чел\w*))?", text, re.I)
        if not m or not 2 <= int(m.group(1)) <= GOAL_MAX:
            _send(token, chat, f"Напиши число участников — от 2 до {GOAL_MAX}.")
            return True
        _set_goal(con, token, uid, chat, d, int(m.group(1)))
        return True
    if step == "k":
        m = re.fullmatch(r"\d{1,3}", text)
        if not m or not 1 <= int(text) <= WIN_MAX:
            _send(token, chat, f"Напиши число от 1 до {WIN_MAX}.")
            return True
        d.update(k=int(text), step="time")
        _draft_put(con, uid, d)
        _ask_time(token, chat)
        return True
    if step == "time":
        m = re.fullmatch(r"(\d{1,6})\s*(?:участ\w*|чел\w*)", text, re.I)
        if m and 2 <= int(m.group(1)) <= GOAL_MAX:
            _set_goal(con, token, uid, chat, d, int(m.group(1)))
            return True
        d.pop("goal", None)
        dur = parse_duration(text)
        if not dur:
            _send(token, chat, "Не поняла срок. Например: «45 мин», «2 ч», «3 дня».")
            return True
        d.update(dur=dur, step="subs")
        _draft_put(con, uid, d)
        _subs_screen(token, chat, d)
        return True
    return False


# ---------- участие ----------
def _join(con, token, frm, chat, gid) -> None:
    uid = frm.get("id")
    if int(uid or 0) >= TEST_UID_MIN:
        _send(token, chat, "Это тестовый вход — в розыгрышах участвуют только настоящие аккаунты 🙂")
        return
    g = _load(con, gid)
    if not g:
        _send(token, chat, "Такого розыгрыша нет 🤔")
        return
    now = H.now()
    mine = next((e for e in g.get("entries") or [] if e[2] == uid), None)
    kb = {"inline_keyboard": [[_url("🔍 Список и проверка", _verify_url(gid))]]}
    if mine:
        _send(token, chat, f"Ты уже участвуешь в «{g['prize']}»: билет №{mine[0]}, код {mine[1]}.\n"
                           f"Итоги: {_end_text(g)}.", kb)
        return
    if g.get("state") != "open" or now >= g["end"]:
        _send(token, chat, f"Приём в «{g['prize']}» уже закрыт.", kb)
        return
    miss, unknown = [], []
    for s in g.get("subs") or []:
        st = _status(token, s["id"], uid)
        if st is None:
            unknown.append(s)
        elif not _is_in(st):
            miss.append(s)
    if miss or unknown:
        rows = [[_url(f"Подписаться: {_chat_name(s)}", f"https://t.me/{s['username']}")]
                for s in miss if s.get("username")]
        rows.append([_btn("🔄 Проверить снова", f"j:{gid}")])
        why = ("Чтобы участвовать, подпишись: " + ", ".join(_chat_name(s) for s in miss)) if miss else \
            ("Не получается проверить подписку на " + ", ".join(_chat_name(s) for s in unknown)
             + " — попробуй через минуту.")
        _send(token, chat, why, {"inline_keyboard": rows})
        return
    with _LOCK:
        g = _load(con, gid)
        if not g or g.get("state") != "open" or H.now() >= g["end"]:
            _send(token, chat, "Приём уже закрыт.", kb)
            return
        mine = next((e for e in g["entries"] if e[2] == uid), None)
        if not mine:
            mine = [len(g["entries"]) + 1, secrets.token_hex(3), uid, _who(frm), int(H.now()),
                    frm.get("username") or ""]
            g["entries"].append(mine)
            if g.get("goal") and len(g["entries"]) >= g["goal"]:
                g["end"] = int(H.now())          # набралось — тик закроет приём и назначит раунд drand
                g["closed_early"] = g["goal_hit"] = 1
            _save(con, g)
    text = (f"✅ Ты участвуешь в розыгрыше «{g['prize']}»!\n"
            f"🎟 Билет №{mine[0]} · код {mine[1]} — по коду найдёшь себя в открытом списке.\n"
            f"⏰ Итоги: {_end_text(g)}. "
            "Если выиграешь — напишу сюда.")
    if H.promo:
        text += "\n\n" + H.promo
    _send(token, chat, text, kb)


# ---------- вход из хозяина ----------
def on_message(con, token, msg) -> bool:
    """Личное сообщение боту. True — это было про розыгрыш, дальше не передавать."""
    if (msg.get("chat") or {}).get("type") != "private":
        return False
    frm = msg.get("from") or {}
    uid = frm.get("id")
    if not uid or frm.get("is_bot"):
        return False
    chat = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    if _TOKENISH.search(text) or _TOKENISH.search(msg.get("caption") or ""):   # токен подписью к фото — тоже не приз
        return False
    m = JOIN_RE.fullmatch(text)
    if m:
        _join(con, token, frm, chat, m.group(1).lower())
        return True
    if START_RE.fullmatch(text):
        _menu(con, token, uid, chat)
        return True
    d = _draft(con, uid)
    if not d or d.get("step") == "offer":
        pre = _fwd_giveaway(msg)
        if pre:
            _draft_put(con, uid, {"step": "offer", "pre": pre})
            _send(token, chat, "Похоже на пост розыгрыша 🙂 Сделать такой же у себя? Возьму картинку и текст, добавлю "
                               "кнопку участия и честный итог по числу drand — подкрутить нельзя.",
                  {"inline_keyboard": [[_btn("🎁 Сделать такой", "fw"), _btn("Не надо", "x")]]})
            return True
    if not d:
        return False
    return _on_text(con, token, uid, chat, d, msg)


def _fwd_giveaway(msg: dict):
    """Пересланный пост розыгрыша (чужого бота или свой) → заготовка {media, text} или None."""
    if not (msg.get("forward_origin") or msg.get("forward_from_chat") or msg.get("forward_from")
            or msg.get("forward_sender_name")):
        return None
    text = str(msg.get("caption") or msg.get("text") or "")
    if not GIVE_HINT_RE.search(text) or (H.username and f"@{H.username}".lower() in text.lower()):
        return None                              # не розыгрыш — или наш же пост
    return {"media": _media_of(msg), "text": text[:4096]}


def on_callback(con, token, cb) -> bool:
    """Кнопка «k:g:…». True — обработано (ответ на нажатие уже отправлен)."""
    data = cb.get("data") or ""
    if not data.startswith("k:g:"):
        return False
    parts = data[4:].split(":")
    act, arg = parts[0], parts[1:]
    frm = cb.get("from") or {}
    uid = frm.get("id")
    chat = ((cb.get("message") or {}).get("chat") or {}).get("id") or uid
    if act == "j" and arg:
        _answer(token, cb)
        _join(con, token, frm, chat, arg[0])
        return True
    if act in ("close", "cancel", "cx") and arg:
        _owner_action(con, token, cb, uid, chat, act, arg[0])
        return True
    if act == "new":
        _answer(token, cb)
        _new(con, token, uid, chat)
        return True
    d = _draft(con, uid)
    if not d:
        _answer(token, cb, "Черновик устарел — начни заново: /give", alert=True)
        return True
    _answer(token, cb)
    if act == "x":
        H.kv_del(con, DRAFT + str(uid))
        _send(token, chat, "Отменила черновик. Новый — /give", _NO_KB)
    elif act == "k" and arg and d.get("step") == "k":
        d.update(k=max(1, min(WIN_MAX, int(arg[0]))), step="time")
        _draft_put(con, uid, d)
        _ask_time(token, chat)
    elif act == "t" and arg and arg[0] == "goal" and d.get("step") == "time":
        d["step"] = "goal"
        _draft_put(con, uid, d)
        _ask_goal(token, chat)
    elif act == "t" and arg and arg[0].isdigit() and d.get("step") == "time":
        d.pop("goal", None)
        d.update(dur=max(DUR_MIN, min(DUR_MAX, int(arg[0]))), step="subs")
        _draft_put(con, uid, d)
        _subs_screen(token, chat, d)
    elif act == "s" and arg and d.get("step") in ("subs", "sub_add") and d.get("chat"):
        subs = d.get("subs") or []
        main = d["chat"]
        if arg[0] == "main":
            subs = [s for s in subs if s["id"] != main["id"]] if any(s["id"] == main["id"] for s in subs) \
                else [main] + subs
        elif arg[0] == "rm" and len(arg) > 1 and arg[1].isdigit() and int(arg[1]) < len(subs):
            subs = subs[:int(arg[1])] + subs[int(arg[1]) + 1:]
        elif arg[0] == "none":
            subs = []
        elif arg[0] == "add":
            d["step"] = "sub_add"
            _draft_put(con, uid, d)
            _send(token, chat, "Выбери канал-условие (я должна быть там админом, чтобы видеть подписчиков) "
                               "или пришли его @адрес.", _pick_kb(3, True))
            return True
        elif arg[0] == "ok":
            d["step"] = "subs"
            _confirm(con, token, uid, chat, d)
            return True
        d.update(subs=subs, step="subs")
        _draft_put(con, uid, d)
        _subs_screen(token, chat, d)
    elif act == "pub":
        _publish(con, token, uid, chat, d, frm)
    elif act == "fw" and d.get("step") == "offer":
        _new(con, token, uid, chat, pre=d.get("pre"))
    return True


def _owner_action(con, token, cb, uid, chat, act, gid) -> None:
    with _LOCK:
        g = _load(con, gid)
        if not g or g.get("owner") != uid:
            _answer(token, cb, "Это не твой розыгрыш.", alert=True)
            return
        if g.get("state") != "open":
            _answer(token, cb, "Приём уже закрыт — итог считается сам.", alert=True)
            return
        if act == "cancel":
            _answer(token, cb)
            _send(token, chat, f"Точно отменить «{g['prize']}»? Участники увидят, что розыгрыш отменён.",
                  {"inline_keyboard": [[_btn("Да, отменить", f"cx:{gid}")]]})
            return
        if act == "close":
            g["end"] = int(H.now())
            g["closed_early"] = 1
            _save(con, g)
            _answer(token, cb, "Закрою приём в ближайшие секунды и подведу итог.", alert=True)
            return
        g["state"] = "cancelled"
        g["done_ts"] = int(H.now())
        _save(con, g)
    _answer(token, cb, "Отменила.")
    note = "\n\n❌ Розыгрыш отменён организатором."
    if g.get("media_post"):                   # пост с картинкой — правится подпись, а не текст
        _api(token, "editMessageCaption", chat_id=g["chat"]["id"], message_id=g.get("msg"),
             caption=(post_text(g)[:CAPTION_MAX - len(note)] + note),
             reply_markup=json.dumps(_post_kb(g), ensure_ascii=False))
        return
    _api(token, "editMessageText", chat_id=g["chat"]["id"], message_id=g.get("msg"),
         text=(post_text(g) + note)[:4096],
         reply_markup=json.dumps(_post_kb(g), ensure_ascii=False))


# ---------- по сроку: закрыть приём, вытянуть победителей ----------
# Порядок (ревью 26.10): «closing» — запись закрыта → проверка условий у ВСЕХ (до числа drand, поэтому выгнать
# неугодного победителя потом уже нельзя) → отпечаток списка подходящих → пост с отпечатком в чате → «closed»
# только когда пост ушёл раньше выхода раунда. Не ушёл — повтор с отсрочкой, раунд переносится вперёд.
def _backoff(tries: int) -> int:
    return min(600, 15 * 2 ** max(0, tries - 1))


def _close(con, token, gid) -> None:
    with _LOCK:
        g = _load(con, gid)
        if not g or g.get("state") not in ("open", "closing"):
            return
        if g["state"] == "open":
            g["state"] = "closing"
            g["closing_ts"] = int(H.now())
            _save(con, g)
    if "list" not in g:
        excluded = []
        for e in g.get("entries") or []:
            for s in g.get("subs") or []:
                st = _status(token, s["id"], e[2])
                if st is not None and not _is_in(st):    # не смогли проверить — не наказываем участника
                    excluded.append([e[0], f"не подписан на {_chat_name(s)}"])
                    break
        out = {n for n, _w in excluded}
        with _LOCK:
            g = _load(con, gid)
            if not g or g.get("state") != "closing":
                return
            g["excluded"] = excluded
            g["list"] = core.list_text((e[0], e[1]) for e in g["entries"] if e[0] not in out)
            g["digest"] = core.sha256(g["list"])
            g["eligible"] = len(g["entries"]) - len(excluded)
            if not g["eligible"]:
                g.update(state="done", done_ts=int(H.now()), winners=[], ann=1)
            _save(con, g)
        if g["state"] == "done":
            why = "участников нет" if not g["entries"] else "никто не выполнил условие"
            _send(token, g["chat"]["id"], f"Приём в «{g['prize']}» закрыт: {why} — розыгрыш не состоялся.",
                  reply_to=g.get("msg"))
            _edit_post(token, g, len(g["entries"]))
            _send(token, g["owner"], f"«{g['prize']}»: {why}, победителей нет.")
            return
    now = int(H.now())
    if now < g.get("next_try", 0):
        return
    rt = core.round_time(g["round"])
    if not now + 60 <= rt <= now + core.ROUND_DELAY + 90:      # закрыли раньше срока, с опозданием или повтор
        with _LOCK:
            g = _load(con, gid)
            g.setdefault("round_moved_from", g["round"])
            g["round"] = core.round_at(now + core.ROUND_DELAY)
            _save(con, g)
    moved = ""
    if g.get("round_moved_from"):
        moved = (f"\nРаунд перенесён с №{g['round_moved_from']}: приём закрыт "
                 + ("раньше срока." if g.get("closed_early") else "позже срока, старый раунд мог уже выйти."))
        if g.get("goal_hit"):
            moved = f"\nНабралось {g['goal']} участников — приём закрыт, раунд назначен сейчас."
    ex = f" Не выполнили условие: {len(g['excluded'])} — они в списке не участвуют." if g.get("excluded") else ""
    r = _send(token, g["chat"]["id"],
              f"⏳ Приём закрыт: «{g['prize']}». Участвуют: {g['eligible']}.{ex}\n"
              f"🔒 Список зафиксирован, отпечаток SHA-256:\n{g['digest']}\n"
              f"Победителей выберет число drand раунда №{g['round']} — оно выйдет "
              f"{time.strftime('%H:%M:%S', time.gmtime(core.round_time(g['round']) + MSK))} МСК, примерно через 2 "
              f"минуты. Заранее его не знает никто.{moved}",
              {"inline_keyboard": [[_url("🔍 Список участников", _verify_url(g["id"]))]]}, reply_to=g.get("msg"))
    res = r.get("result") or {}
    with _LOCK:
        g = _load(con, gid)
        if not g or g.get("state") != "closing":
            return
        if r.get("ok") and int(res.get("date") or 0) < core.round_time(g["round"]):
            g.update(state="closed", closed_ts=int(H.now()), close_msg=res.get("message_id"),
                     close_msg_date=int(res.get("date") or 0))
            g.pop("next_try", None)
        else:                                            # не ушло или ушло после выхода раунда — повтор позже
            g["close_tries"] = g.get("close_tries", 0) + 1
            g["next_try"] = int(H.now()) + _backoff(g["close_tries"])
            g["round_moved_from"] = g.get("round_moved_from") or g["round"]
            g["round"] = 0                               # следующий заход выберет новый раунд
        _save(con, g)
    if g["state"] == "closed":
        _edit_post(token, g, len(g["entries"]))
    elif g["close_tries"] == 3:
        _send(token, g["owner"], f"Не могу опубликовать в {_chat_name(g['chat'])} закрытие «{g['prize']}» — "
                                 "проверь мои права (публикация сообщений). Как только смогу, продолжу сама.")


def _draw(con, token, gid) -> None:
    g = _load(con, gid)
    if not g or g.get("state") != "closed":
        return
    if not g.get("close_msg_date") or g["close_msg_date"] >= core.round_time(g["round"]):
        return                                           # отпечаток не опубликован до раунда — не тянем
    b = core.fetch_beacon(g["round"], H.get_json)
    if not b:
        return                                           # раунд не вышел или drand недоступен — следующий тик
    seed = core.seed_of(g["id"], g["digest"], b["randomness"])
    winners = core.draw_order(seed, [n for n, _c in core.parse_list(g["list"])], g["k"])
    with _LOCK:
        g = _load(con, gid)
        if not g or g.get("state") != "closed":
            return
        g.update(state="done", done_ts=int(H.now()), randomness=b["randomness"], signature=b["signature"],
                 seed=seed, winners=winners, ann=0)
        _save(con, g)
    _announce(con, token, gid)


def _mention(e) -> str:
    return f'<a href="tg://user?id={int(e[2])}">{html.escape(e[3])}</a>'


def _send_lines(token, chat, lines, kb=None, reply_to=None) -> bool:
    """HTML по строкам: режем на куски до 3900 знаков, не разрывая тегов; кнопки — у последнего куска."""
    parts, cur = [], ""
    for line in lines:
        if cur and len(cur) + len(line) + 1 > 3900:
            parts.append(cur)
            cur = ""
        cur = (cur + "\n" + line) if cur else line
    parts.append(cur)
    ok = True
    for i, p in enumerate(parts):
        last = i == len(parts) - 1
        r = _send(token, chat, p, kb if last else None, html_mode=True, reply_to=reply_to if i == 0 else None)
        ok = ok and bool(r.get("ok"))
        if not ok:
            break
    return ok


def _announce(con, token, gid) -> None:
    """Итоги в чат; не ушли — повтор с отсрочкой из tick. Победителям и организатору — один раз после успеха."""
    g = _load(con, gid)
    if not g or g.get("state") != "done" or g.get("ann"):
        return
    if H.now() < g.get("next_ann", 0):
        return
    by_n = {e[0]: e for e in g["entries"]}
    esc = html.escape
    lines = [f"🏆 Итоги розыгрыша «{esc(g['prize'])}»", ""]
    lines += [f"{i}. {_mention(by_n[n])} — билет №{n}" for i, n in enumerate(g["winners"], 1)]
    lines += ["", f"Участвовали: {g.get('eligible', len(g['entries']))}."]
    lines.append(f"🔒 Число drand раунда №{g['round']}: {g['randomness'][:16]}… — пересчитать итог может любой.")
    ok = _send_lines(token, g["chat"]["id"], lines,
                     {"inline_keyboard": [[_url("🔍 Проверить итог", _verify_url(g["id"]))]]}, reply_to=g.get("msg"))
    with _LOCK:
        g = _load(con, gid)
        if ok:
            g["ann"] = 1
            g.pop("next_ann", None)
        else:
            g["ann_tries"] = g.get("ann_tries", 0) + 1
            g["next_ann"] = int(H.now()) + _backoff(g["ann_tries"])
            if g["ann_tries"] > 200:                     # дальше не долбим Telegram: итог есть на странице
                g["ann"] = -1
        _save(con, g)
    who = [f"{i}. {_mention(by_n[n])}" + (f" (@{esc(by_n[n][5])})" if len(by_n[n]) > 5 and by_n[n][5] else "")
           + f" — билет №{n}" for i, n in enumerate(g["winners"], 1)]
    if not ok:
        if g.get("ann_tries") == 3:
            _send_lines(token, g["owner"], [f"Не могу опубликовать итоги «{esc(g['prize'])}» в "
                                            f"{esc(_chat_name(g['chat']))} — проверь мои права. Победители:"] + who)
        return
    _edit_post(token, g)
    org = f" Организатор: @{g['owner_user']}." if g.get("owner_user") else " Организатор свяжется с тобой."
    for n in g["winners"]:
        _send(token, by_n[n][2], f"🎉 Ты выиграл в розыгрыше «{g['prize']}» ({_chat_name(g['chat'])})! "
                                 f"Билет №{n}.{org}",
              {"inline_keyboard": [[_url("🔍 Итог и проверка", _verify_url(g["id"]))]]})
    _send_lines(token, g["owner"], [f"🏆 Итоги «{esc(g['prize'])}»:"] + (who or ["победителей нет"])
                + ["", "Напиши победителям и передай приз. Имя — ссылка на профиль."])


_STAGE = {"open": 0, "closing": 1, "closed": 2, "done": 3, "cancelled": 3}


def merge(cur_raw, inc_raw):
    """Слияние при возврате бэкапа (import_state хозяина): дальше продвинутая стадия побеждает; у двух открытых —
    объединение участников по tg id (код билета не меняется; занятый номер — следующий свободный)."""
    try:
        cur = json.loads(cur_raw) if cur_raw else None
        inc = json.loads(inc_raw) if inc_raw else None
    except Exception:  # noqa: BLE001
        return cur_raw or inc_raw
    if not cur or not inc:
        return cur_raw or inc_raw
    sc, si = _STAGE.get(cur.get("state"), 0), _STAGE.get(inc.get("state"), 0)
    if si != sc:
        return inc_raw if si > sc else cur_raw
    if cur.get("state") != "open":
        return inc_raw if (inc.get("close_msg") and not cur.get("close_msg")) or \
            (inc.get("ann") and not cur.get("ann")) else cur_raw
    have = {e[2] for e in cur["entries"]}
    used = {e[0] for e in cur["entries"]}
    for e in sorted(inc["entries"], key=lambda x: x[0]):
        if e[2] in have:
            continue
        e = list(e)
        if e[0] in used:
            e[0] = max(used, default=0) + 1
        used.add(e[0])
        have.add(e[2])
        cur["entries"].append(e)
    cur["entries"].sort(key=lambda x: x[0])
    cur["end"] = min(int(cur["end"]), int(inc["end"]))
    if inc.get("closed_early"):
        cur["closed_early"] = 1
    if inc.get("goal_hit"):
        cur["goal_hit"] = 1
    return json.dumps(cur, ensure_ascii=False, separators=(",", ":"))


def _compact(con, g) -> None:
    """Старый итог: tg id и имена больше не нужны — список номеров и кодов и проверка остаются."""
    with _LOCK:
        g = _load(con, g["id"])
        if not g or g.get("compact"):
            return
        names = {e[0]: _mask(e[3]) for e in g.get("entries") or []}
        g["entries"] = [[e[0], e[1], 0, names[e[0]], e[4], ""] for e in g.get("entries") or []]
        g["compact"] = 1
        _save(con, g, urgent=False)


def tick(con, token) -> None:
    now = H.now()
    for g in _all(con):
        try:
            st = g.get("state")
            if st == "closing" or (st == "open" and now >= g["end"]):
                _close(con, token, g["id"])
            elif st == "open":
                if len(g["entries"]) != g.get("shown") and now - g.get("shown_ts", 0) >= SHOWN_EVERY:
                    n = len(g["entries"])
                    _edit_post(token, g, n)
                    with _LOCK:
                        g2 = _load(con, g["id"])
                        if g2:
                            g2["shown"], g2["shown_ts"] = n, int(now)
                            _save(con, g2, urgent=False)
            elif st == "closed" and now >= core.round_time(g["round"]) + 2:
                _draw(con, token, g["id"])
            elif st == "done" and g.get("ann") == 0:
                _announce(con, token, g["id"])
            elif st in ("done", "cancelled") and not g.get("compact") and now - g.get("done_ts", now) > COMPACT_AFTER:
                _compact(con, g)
        except Exception as e:  # noqa: BLE001 — один сломанный розыгрыш не держит остальные
            print("give tick:", g.get("id"), type(e).__name__, e)


STOP = threading.Event()        # хозяин ставит при остановке (SIGTERM): старый экземпляр больше не закрывает


def run_loop(get_con, token, stop=None, first_delay=0) -> None:
    """Фоновый поток хозяина: тик раз в TICK_SEC секунд со своим соединением к базе. first_delay — пауза перед
    первым тиком: при деплое старый экземпляр ещё жив, и два тика на двух базах закрыли бы розыгрыш дважды."""
    stop = stop or STOP
    if stop.wait(first_delay):
        return
    while not stop.is_set():
        try:
            con = get_con()
            try:
                tick(con, token)
            finally:
                con.close()
        except Exception as e:  # noqa: BLE001
            print("give loop:", type(e).__name__, e)
        stop.wait(TICK_SEC)


# ---------- страница проверки ----------
def public(g: dict) -> dict:
    """Что видит кто угодно: без tg id; имена участников — замаскированы, победителей — как в посте."""
    entries = g.get("entries") or []
    by_n = {e[0]: e for e in entries}
    out = {k: g.get(k) for k in ("id", "prize", "k", "goal", "goal_hit", "created", "end", "state", "round", "round_moved_from",
                                 "closed_ts", "close_msg_date", "digest", "randomness", "signature", "seed",
                                 "excluded", "eligible", "done_ts")}
    out.update(scheme=core.SCHEME, chain=core.DRAND_CHAIN, round_time=core.round_time(g["round"]),
               chat={"title": g["chat"].get("title"), "username": g["chat"].get("username")},
               subs=[{"title": s.get("title"), "username": s.get("username")} for s in g.get("subs") or []],
               n=len(entries), entries=[[e[0], e[1], _mask(e[3])] for e in entries],
               list=g.get("list"), source=H.source_url,
               winners=[[n, by_n[n][1], by_n[n][3] if not g.get("compact") else _mask(by_n[n][3])]
                        for n in g.get("winners") or [] if n in by_n])
    return out


_FILES = {"give_core.py": "text/plain; charset=utf-8", "give_verify.js": "text/javascript; charset=utf-8",
          "kel_give.py": "text/plain; charset=utf-8"}


def http_get(con, path: str):
    """Маршруты /give/…: (код, тип, байты) или None. path — без ведущего «/»."""
    p = path.strip("/")
    if p in ("give/verify.js", "give/code/give_verify.js"):
        return 200, _FILES["give_verify.js"], (_HERE / "give_verify.js").read_bytes()
    m = re.fullmatch(r"give/code/([\w.]+)", p)
    if m and m.group(1) in _FILES:
        return 200, _FILES[m.group(1)], (_HERE / m.group(1)).read_bytes()
    if p in ("give", "give/code"):
        body = ("Честный розыгрыш — открытый код, который сейчас работает на этом сервере:\n"
                + "".join(f"{(H.base_url or '').rstrip('/')}/give/code/{f}\n" for f in _FILES)
                + (f"Репозиторий: {H.source_url}\n" if H.source_url else ""))
        return 200, "text/plain; charset=utf-8", body.encode("utf-8")
    m = re.fullmatch(r"give/([a-z0-9]{6,16})(\.json|\.txt)?", p)
    if not m:
        return None
    g = _load(con, m.group(1))
    if not g:
        return 404, "text/plain; charset=utf-8", "Такого розыгрыша нет".encode("utf-8")
    if m.group(2) == ".json":
        return 200, "application/json; charset=utf-8", json.dumps(public(g), ensure_ascii=False).encode("utf-8")
    if m.group(2) == ".txt":
        text = g.get("list") or core.list_text((e[0], e[1]) for e in g.get("entries") or [])
        return 200, "text/plain; charset=utf-8", text.encode("utf-8")
    page = (_HERE / "give_page.html").read_text(encoding="utf-8").replace("__GID__", g["id"])
    return 200, "text/html; charset=utf-8", page.encode("utf-8")


def kel_setup(k) -> None:
    """Подключение к боту Кель: Telegram — k.api, хранилище — таблица meta (уходит в бэкап)."""
    def kv_keys(con, prefix):
        return [r[0] for r in con.execute("SELECT k FROM meta WHERE k LIKE ?", (prefix + "%",)).fetchall()]

    def kv_del(con, key):
        con.execute("DELETE FROM meta WHERE k=?", (key,))
        con.commit()

    def mark_backup(con):
        k.meta_set(con, "owner_fix_backup_pending", "1")

    # позднее связывание: подмена k.api в тестах хозяина доходит и сюда
    setup(api=lambda *a, **kw: k.api(*a, **kw), kv_get=lambda con, key: k.meta_get(con, key),
          kv_set=lambda con, key, v: k.meta_set(con, key, v), kv_del=kv_del, kv_keys=kv_keys, mark_backup=mark_backup,
          base_url=os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("KEL_PUBLIC_URL")
          or "https://kel-bot.onrender.com",
          source_url=os.environ.get("KEL_GIVE_SOURCE", "https://github.com/miszazninja-lang/kel-giveaway"),
          username=os.environ.get("KEL_BOT_USERNAME", "KelLegionBot"),
          promo="Я Кель — помогаю игрокам senkuro.me: задания, карты, цены банка. Что умею — /help")
