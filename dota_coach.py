#!/usr/bin/env python3
"""
Dota Coach — автоматический разбор матчей Dota 2 через Claude с отправкой в Telegram.

Режимы:
  python dota_coach.py                 — одна проверка новых матчей
  python dota_coach.py --watch         — следить постоянно (для автозапуска)
  python dota_coach.py --latest        — разобрать последний матч прямо сейчас
  python dota_coach.py --match 1234    — разобрать конкретный матч
  python dota_coach.py --chat-id       — узнать chat_id для Telegram
  --dry-run                            — печатать разбор в консоль, не слать в Telegram

Зависимостей нет: только стандартная библиотека Python 3.9+.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
STATE_PATH = BASE / "state.json"
PROMPT_PATH = BASE / "coach_prompt.md"
HISTORY_DIR = BASE / "analyses"
CACHE_DIR = BASE / "cache"
OPENDOTA = "https://api.opendota.com/api"
STEAM64_OFFSET = 76561197960265728

# Расходники и мелочь, которые не важны для таймингов предметов
SKIP_ITEMS = {
    "tango", "flask", "clarity", "ward_observer", "ward_sentry", "tpscroll",
    "enchanted_mango", "faerie_fire", "smoke_of_deceit", "dust", "ward_dispenser",
    "branches", "blood_grenade", "tome_of_knowledge", "famango", "great_famango",
    "greater_famango", "cheese", "aegis", "refresher_shard",
}
LANE_ROLE = {1: "лёгкая линия (safe)", 2: "мид", 3: "сложная линия (off)", 4: "лес"}

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- HTTP

def http_json(url, method="GET", payload=None, headers=None, timeout=60):
    hdrs = {"User-Agent": "dota-coach/1.0"}
    if headers:
        hdrs.update(headers)
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    elif method == "POST":
        body = b""
    req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"HTTP {e.code} для {url.split('?')[0]}: {detail}") from None
    return json.loads(raw) if raw else {}


class OpenDota:
    def __init__(self, api_key=None):
        self.api_key = api_key or None

    def _url(self, path, **params):
        if self.api_key:
            params["api_key"] = self.api_key
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        return f"{OPENDOTA}{path}{q}"

    def _call(self, url, method="GET"):
        for attempt in range(6):
            try:
                return http_json(url, method=method)
            except RuntimeError as e:
                if "HTTP 429" in str(e) and attempt < 5:
                    log("OpenDota: слишком много запросов, жду 30 сек…")
                    time.sleep(30)
                    continue
                raise

    def get(self, path, **params):
        data = self._call(self._url(path, **params))
        # OpenDota иногда отдаёт match_id строкой — приводим к числу
        if isinstance(data, list):
            for m in data:
                if isinstance(m, dict) and isinstance(m.get("match_id"), str) and m["match_id"].isdigit():
                    m["match_id"] = int(m["match_id"])
        elif isinstance(data, dict) and isinstance(data.get("match_id"), str) and data["match_id"].isdigit():
            data["match_id"] = int(data["match_id"])
        return data

    def post(self, path):
        return self._call(self._url(path), method="POST")

    def constants(self, name, max_age_days=7):
        """Справочники героев/предметов, кешируются на диск."""
        CACHE_DIR.mkdir(exist_ok=True)
        f = CACHE_DIR / f"{name}.json"
        if f.exists() and time.time() - f.stat().st_mtime < max_age_days * 86400:
            return json.loads(f.read_text("utf-8"))
        data = self.get(f"/constants/{name}")
        f.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
        return data


# ---------------------------------------------------------------- config/state

def load_config():
    if not CONFIG_PATH.exists():
        sys.exit("Нет config.json. Скопируй config.example.json в config.json и заполни.")
    cfg = json.loads(CONFIG_PATH.read_text("utf-8"))
    # В облаке (GitHub Actions) секреты приходят через переменные окружения
    for env, key in (("TELEGRAM_BOT_TOKEN", "telegram_bot_token"), ("TELEGRAM_CHAT_ID", "telegram_chat_id"),
                     ("OPENDOTA_API_KEY", "opendota_api_key"), ("ANTHROPIC_API_KEY", "anthropic_api_key")):
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    if not accounts(cfg):
        sys.exit("В config.json не указан ни один steam_id.")
    return cfg


def to_account_id(sid):
    sid = int(str(sid).strip())
    return sid - STEAM64_OFFSET if sid > STEAM64_OFFSET else sid


def accounts(cfg):
    """Список (account_id, имя). Поддерживает steam_ids: [{id, name}] и старый steam_id."""
    out = []
    for a in cfg.get("steam_ids") or []:
        sid = a.get("id") if isinstance(a, dict) else a
        name = a.get("name", "") if isinstance(a, dict) else ""
        if str(sid).strip().isdigit():
            out.append((to_account_id(sid), name or str(sid)))
    if not out and str(cfg.get("steam_id", "")).strip().isdigit():
        out.append((to_account_id(cfg["steam_id"]), "основной"))
    return out


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text("utf-8"))
    return {}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")


# ---------------------------------------------------------------- match summary

def mmss(sec):
    if sec is None:
        return "?"
    sign = "-" if sec < 0 else ""
    sec = abs(int(sec))
    return f"{sign}{sec // 60}:{sec % 60:02d}"


def at_min(arr, minute):
    if isinstance(arr, list) and len(arr) > minute:
        return arr[minute]
    return None


def is_parsed(match):
    return match.get("version") is not None


def build_summary(match, acc, heroes, items, consts=None):
    consts = consts or {}
    hero_by_id = {int(k): v for k, v in heroes.items()}
    item_by_id = {v.get("id"): v for v in items.values() if isinstance(v, dict) and v.get("id") is not None}

    def hname(hid):
        return hero_by_id.get(hid, {}).get("localized_name", f"hero#{hid}")

    def npc(hid):
        return hero_by_id.get(hid, {}).get("name", "")

    def iname_by_key(key):
        return items.get(key, {}).get("dname") or key

    def iname_by_id(iid):
        if not iid:
            return None
        return item_by_id.get(iid, {}).get("dname") or f"item#{iid}"

    players = match.get("players", [])
    me = next((p for p in players if p.get("account_id") == acc), None)
    if me is None:
        raise RuntimeError("Не нашёл тебя в матче — проверь steam_id и что матч публичный.")

    my_radiant = me.get("isRadiant", me.get("player_slot", 0) < 128)
    won = match.get("radiant_win") == my_radiant
    parsed = is_parsed(match)
    duration = match.get("duration", 0)
    out = []
    A = out.append

    A(f"МАТЧ {match.get('match_id')} | {'ПОБЕДА' if won else 'ПОРАЖЕНИЕ'} | "
      f"длительность {mmss(duration)} | сторона {'Radiant' if my_radiant else 'Dire'}")
    A(f"Счёт по убийствам: Radiant {match.get('radiant_score')} — Dire {match.get('dire_score')}")
    if not parsed:
        A("ВНИМАНИЕ: реплей не разобран OpenDota — доступны только базовые цифры, "
          "без таймингов, линии, смертей и драк.")

    # --- Я
    A("")
    A(f"== МОЙ ГЕРОЙ: {hname(me.get('hero_id'))} ==")
    role = LANE_ROLE.get(me.get("lane_role"), "?")
    RANKS = {1: "Рекрут", 2: "Страж", 3: "Рыцарь", 4: "Герой", 5: "Легенда", 6: "Властелин", 7: "Божество", 8: "Титан"}
    rt = me.get("rank_tier")
    if rt:
        A(f"Ранг игрока в этом матче: {RANKS.get(rt // 10, '?')} {rt % 10 or ''}".strip())
    A(f"Роль на линии: {role}{' (роумил)' if me.get('is_roaming') else ''} | уровень {me.get('level')}")
    A(f"K/D/A: {me.get('kills')}/{me.get('deaths')}/{me.get('assists')} | "
      f"LH/DN: {me.get('last_hits')}/{me.get('denies')} | GPM {me.get('gold_per_min')} | "
      f"XPM {me.get('xp_per_min')} | Net worth {me.get('net_worth')}")
    A(f"Урон по героям {me.get('hero_damage')} | по строениям {me.get('tower_damage')} | "
      f"лечение {me.get('hero_healing')} | станы {round(me.get('stuns') or 0, 1)} сек")
    extra = []
    for key, label in [("lane_efficiency_pct", "эффективность линии %"),
                       ("teamfight_participation", "участие в драках"),
                       ("obs_placed", "observer"), ("sen_placed", "sentry"),
                       ("camps_stacked", "стаки"), ("actions_per_min", "APM"),
                       ("buyback_count", "байбеки")]:
        v = me.get(key)
        if v is not None:
            if key == "teamfight_participation":
                v = f"{round(v * 100)}%"
            extra.append(f"{label}: {v}")
    if extra:
        A(" | ".join(extra))

    bm = me.get("benchmarks") or {}
    if bm:
        parts = []
        for k, v in bm.items():
            if isinstance(v, dict) and v.get("pct") is not None:
                parts.append(f"{k}={round(v.get('raw') or 0, 2)} (перцентиль {round(v['pct'] * 100)})")
        if parts:
            A("Сравнение с другими игроками на этом герое: " + "; ".join(parts))

    # --- Линия
    if parsed:
        A("")
        A("== ЛИНИЯ (по минутам) ==")
        lane = me.get("lane")
        rows = [("я", me)]
        for p in players:
            if p is me or p.get("lane") != lane:
                continue
            side = "союзник" if p.get("isRadiant") == my_radiant else "ПРОТИВНИК"
            rows.append((f"{side} {hname(p.get('hero_id'))}", p))
        for label, p in rows:
            cells = []
            for m in (5, 10, 15):
                cells.append(f"{m}мин: LH {at_min(p.get('lh_t'), m)}, DN {at_min(p.get('dn_t'), m)}, "
                             f"gold {at_min(p.get('gold_t'), m)}, xp {at_min(p.get('xp_t'), m)}")
            A(f"{label}: " + " | ".join(cells))

    # --- Предметы
    if parsed and me.get("purchase_log"):
        A("")
        A("== ТАЙМИНГИ ПРЕДМЕТОВ (без расходников) ==")
        seen = set()
        timings = []
        for e in me["purchase_log"]:
            key = e.get("key")
            if not key or key in SKIP_ITEMS or key.startswith("recipe_") or key in seen:
                continue
            cost = items.get(key, {}).get("cost") or 0
            if cost < 800:
                continue
            seen.add(key)
            timings.append(f"{mmss(e.get('time'))} {iname_by_key(key)}")
        A(", ".join(timings) or "нет данных")
    final = [iname_by_id(me.get(f"item_{i}")) for i in range(6)]
    back = [iname_by_id(me.get(f"backpack_{i}")) for i in range(3)]
    A("Финальные предметы: " + ", ".join(x for x in final if x)
      + (" | рюкзак: " + ", ".join(x for x in back if x) if any(back) else "")
      + (f" | нейтралка: {iname_by_id(me.get('item_neutral'))}" if me.get("item_neutral") else ""))

    # --- Микро
    if parsed:
        out.extend(micro_section(me, players, my_radiant, duration, consts, hname, npc))

    # --- Смерти
    if parsed:
        my_npc = npc(me.get("hero_id"))
        deaths = []
        for p in players:
            if p.get("isRadiant") == my_radiant:
                continue
            for k in p.get("kills_log") or []:
                if k.get("key") == my_npc:
                    deaths.append((k.get("time", 0), hname(p.get("hero_id"))))
        deaths.sort()
        A("")
        A("== МОИ СМЕРТИ (время — кто добил) ==")
        A(", ".join(f"{mmss(t)} {who}" for t, who in deaths) or "не умирал / нет данных")
        kb = me.get("killed_by") or {}
        if kb:
            by = sorted(kb.items(), key=lambda x: -x[1])
            A("Чаще всего убивали: " + ", ".join(
                f"{next((v.get('localized_name') for v in hero_by_id.values() if v.get('name') == n), n)} ×{c}"
                for n, c in by[:5]))
        if me.get("obs_log"):
            A("Observer-варды поставлены на: " + ", ".join(mmss(w.get("time")) for w in me["obs_log"][:20]))

    # --- Драки
    tfs = match.get("teamfights") or []
    if parsed and tfs:
        my_idx = players.index(me)
        A("")
        A(f"== ДРАКИ ({len(tfs)}) ==")
        for tf in tfs[:20]:
            tp = tf.get("players") or []
            mine = tp[my_idx] if my_idx < len(tp) else {}
            my_gold = sum((x.get("gold_delta") or 0) for i, x in enumerate(tp)
                          if i < len(players) and players[i].get("isRadiant") == my_radiant)
            en_gold = sum((x.get("gold_delta") or 0) for i, x in enumerate(tp)
                          if i < len(players) and players[i].get("isRadiant") != my_radiant)
            took_part = (mine.get("damage") or 0) > 0 or (mine.get("deaths") or 0) > 0 \
                or (mine.get("healing") or 0) > 0 or bool(mine.get("killed"))
            res = "выиграли" if my_gold > en_gold else "проиграли"
            A(f"{mmss(tf.get('start'))}–{mmss(tf.get('end'))}: смертей {tf.get('deaths')}, {res} по золоту; "
              f"я {'участвовал' if took_part else 'НЕ участвовал'}"
              + (f" (урон {mine.get('damage')}, умер {mine.get('deaths')}, золото {mine.get('gold_delta')})"
                 if took_part else ""))

    # --- Макро
    adv = match.get("radiant_gold_adv") or []
    if adv:
        s = 1 if my_radiant else -1
        pts = []
        for m in (5, 10, 15, 20, 25, 30, 40):
            if m < len(adv):
                pts.append(f"{m}мин {s * adv[m]:+d}")
        pts.append(f"конец {s * adv[-1]:+d}")
        A("")
        A("== ПРЕИМУЩЕСТВО МОЕЙ КОМАНДЫ ПО ЗОЛОТУ ==")
        A(" | ".join(pts))
    objs = match.get("objectives") or []
    if objs:
        ev = []
        for o in objs:
            t = o.get("type", "")
            if t == "CHAT_MESSAGE_FIRSTBLOOD":
                ev.append(f"{mmss(o.get('time'))} первая кровь")
            elif t == "CHAT_MESSAGE_ROSHAN_KILL":
                team = "мы" if (o.get("team") == 2) == my_radiant else "враги"
                ev.append(f"{mmss(o.get('time'))} Рошан ({team})")
            elif t == "building_kill":
                key = o.get("key", "")
                ours = ("goodguys" in key) == my_radiant
                name = key.replace("npc_dota_goodguys_", "").replace("npc_dota_badguys_", "")
                ev.append(f"{mmss(o.get('time'))} {'потеряли' if ours else 'снесли'} {name}")
        if ev:
            A("")
            A("== ОБЪЕКТЫ ==")
            A(", ".join(ev[:35]))

    # --- Все игроки
    A("")
    A("== ВСЕ ИГРОКИ ==")
    for p in sorted(players, key=lambda x: (x.get("isRadiant") != my_radiant, -(x.get("net_worth") or 0))):
        side = "наши " if p.get("isRadiant") == my_radiant else "враги"
        its = ", ".join(x for x in (iname_by_id(p.get(f"item_{i}")) for i in range(6)) if x)
        A(f"{side} | {hname(p.get('hero_id'))}{' (Я)' if p is me else ''} | "
          f"{LANE_ROLE.get(p.get('lane_role'), '?')} | {p.get('kills')}/{p.get('deaths')}/{p.get('assists')} | "
          f"NW {p.get('net_worth')} | GPM {p.get('gold_per_min')} | {its}")

    return "\n".join(out), {"won": won, "hero": hname(me.get("hero_id")), "parsed": parsed}


# ---------------------------------------------------------------- micro

GOLD_REASONS = {"0": "прочее", "1": "смерти (потеряно)", "2": "байбек", "6": "продажа", "11": "строения",
                "12": "герои", "13": "крипы на линии", "14": "лесные крипы", "15": "Рошан", "16": "курьеры",
                "17": "руны", "18": "общее", "19": "строения", "20": "варды", "21": "Томы/прочее"}
RUNES = {"0": "дабл дамаг", "1": "хаст", "2": "иллюзии", "3": "инвиз", "4": "реген", "5": "баунти",
         "6": "арканка", "7": "вода", "8": "щит"}


def _top(d, n, fmt):
    items_ = sorted((d or {}).items(), key=lambda x: -x[1])[:n]
    return ", ".join(fmt(k, v) for k, v in items_)


def micro_section(me, players, my_radiant, duration, extra, hname, npc):
    out = []
    A = out.append
    abil = extra.get("abilities") or {}
    itm = extra.get("items") or {}
    hero_by_npc = {npc(p.get("hero_id")): hname(p.get("hero_id")) for p in players}

    def aname(k):
        return (abil.get(k) or {}).get("dname") or itm.get(k.replace("item_", ""), {}).get("dname") or k

    def uname(k):
        return hero_by_npc.get(k) or k.replace("npc_dota_", "")

    A("")
    A("== МИКРО: ДОБИВАНИЕ ==")
    lh, dn = me.get("lh_t") or [], me.get("dn_t") or []
    rows = []
    prev_l, prev_d = 0, 0
    for a, b in ((0, 5), (5, 10), (10, 15), (15, 20), (20, 30), (30, 40)):
        if b < len(lh):
            l, d = lh[b], (dn[b] if b < len(dn) else 0)
            rows.append(f"{a}–{b} мин: +{l - prev_l} LH, +{d - prev_d} DN (итого {l}/{d})")
            prev_l, prev_d = l, d
    A("; ".join(rows) or "нет данных")
    A("Ориентир: идеальное добивание без денаев ≈ 40 крипов к 5:00 и ≈ 80–85 к 10:00 (без учёта леса).")
    lk, nk = me.get("lane_kills"), me.get("neutral_kills")
    if lk is not None or nk is not None:
        A(f"Убито крипов: линия {lk}, лес {nk}, древние {me.get('ancient_kills')} | "
          f"стаков {me.get('camps_stacked') or me.get('creeps_stacked') or 0}")
    gr = me.get("gold_reasons") or {}
    if gr:
        A("Откуда золото: " + _top(gr, 8, lambda k, v: f"{GOLD_REASONS.get(str(k), k)} {v}"))
    if me.get("life_state_dead") is not None:
        A(f"Время мёртвым: {mmss(me.get('life_state_dead'))} из {mmss(duration)}")

    A("")
    A("== МИКРО: СКИЛЛЫ И ПРЕДМЕТЫ ==")
    if me.get("ability_uses"):
        uses = {k: v for k, v in me["ability_uses"].items() if not k.startswith(("ability_", "seasonal_"))}
        A("Использование способностей: " + _top(uses, 8, lambda k, v: f"{aname(k)} ×{v}"))
    if me.get("item_uses"):
        A("Использование предметов: " + _top(me["item_uses"], 10, lambda k, v: f"{aname(k)} ×{v}"))
    if me.get("ability_targets"):
        parts = []
        for ab, tg in me["ability_targets"].items():
            parts.append(f"{aname(ab)} → " + _top(tg, 3, lambda k, v: f"{uname(k)} ×{v}"))
        A("По кому кастовал: " + "; ".join(parts[:5]))
    if me.get("damage_inflictor"):
        A("Урон по героям по источникам: " + _top(me["damage_inflictor"], 6,
          lambda k, v: f"{'автоатаки' if k == 'null' else aname(k)} {v}"))
    if me.get("damage_targets"):
        tot = {}
        for _, tg in me["damage_targets"].items():
            for k, v in tg.items():
                tot[k] = tot.get(k, 0) + v
        A("Кому наносил урон: " + _top(tot, 5, lambda k, v: f"{uname(k)} {v}"))
    if me.get("damage_taken"):
        heroes_only = {k: v for k, v in me["damage_taken"].items() if "hero" in k}
        A("Получил урона от героев: " + _top(heroes_only, 5, lambda k, v: f"{uname(k)} {v}"))
    if me.get("max_hero_hit"):
        mh = me["max_hero_hit"]
        A(f"Самый сильный удар: {mh.get('value')} ({aname(mh.get('inflictor') or 'автоатака')} по "
          f"{uname(mh.get('key', ''))}, {mmss(mh.get('time'))})")
    if me.get("multi_kills"):
        A("Мульти-киллы: " + ", ".join(f"{k}x ×{v}" for k, v in me["multi_kills"].items()))

    A("")
    A("== МИКРО: КАРТА, ВИЖН, РУНЫ ==")
    pur = me.get("purchase") or {}
    A(f"Куплено: TP {pur.get('tpscroll', 0)} (использовано TP: {(me.get('item_uses') or {}).get('tpscroll', 0)}), observer {pur.get('ward_observer', 0)}, "
      f"sentry {pur.get('ward_sentry', 0)}, смоук {pur.get('smoke_of_deceit', 0)}, dust {pur.get('dust', 0)}")
    obs_in = {w.get("ehandle"): w.get("time") for w in me.get("obs_log") or [] if w.get("ehandle")}
    short = 0
    for w in me.get("obs_left_log") or []:
        t0 = obs_in.get(w.get("ehandle"))
        if t0 is not None and (w.get("time", 0) - t0) < 300:
            short += 1
    if obs_in:
        A(f"Observer-вардов: {len(obs_in)}, из них простояли меньше 5 мин (скорее всего сняли): {short}")
    A(f"Снял вардов врага: observer {me.get('observer_kills') or 0}, sentry {me.get('sentry_kills') or 0}")
    if me.get("runes"):
        A("Руны: " + ", ".join(f"{RUNES.get(str(k), 'другая')} ×{v}" for k, v in me["runes"].items()))
    if me.get("buyback_log"):
        A("Байбеки: " + ", ".join(mmss(b.get("time")) for b in me["buyback_log"]))
    if me.get("actions_per_min"):
        A(f"APM {me.get('actions_per_min')} | пингов {me.get('pings') or 0}")
    return out


# ---------------------------------------------------------------- analysis

def find_saved_analysis(match_id):
    if not HISTORY_DIR.exists():
        return None
    files = sorted(HISTORY_DIR.glob(f"*_{match_id}.txt"))
    return files[-1].read_text("utf-8") if files else None


def recent_history(n=3, max_chars=1500, exclude=None):
    if not HISTORY_DIR.exists():
        return ""
    latest = {}
    for f in sorted(HISTORY_DIR.glob("*.txt")):
        mid = f.stem.split("_")[-1]
        if mid != str(exclude):
            latest[mid] = f  # по одному (последнему) разбору на матч
    files = sorted(latest.values())[-n:]
    chunks = []
    for f in files:
        chunks.append(f"--- матч {f.stem.split('_')[-1]} ---\n{f.read_text('utf-8')[:max_chars]}")
    return "\n\n".join(chunks)


def build_prompt(cfg, summary, match_id=None):
    coach = PROMPT_PATH.read_text("utf-8") if PROMPT_PATH.exists() else "Ты тренер по Dota 2."
    profile = cfg.get("player_profile", "").strip()
    hist = recent_history(cfg.get("history_matches", 3), exclude=match_id)
    parts = [coach]
    if profile:
        parts.append(f"## Об игроке\n{profile}")
    patch = BASE / "patch_context.md"
    if patch.exists():
        parts.append("## Текущий патч (учитывай при советах по сборке и игре)\n" + patch.read_text("utf-8"))
    if hist:
        parts.append("## Твои прошлые разборы ДРУГИХ матчей этого игрока (только чтобы заметить повторяющиеся ошибки; "
                     "разбирай текущий матч, не комментируй сами разборы)\n" + hist)
    parts.append("## Данные матча\n" + summary)
    parts.append("Сделай разбор по формату выше.")
    return "\n\n".join(parts)


def analyze(cfg, prompt):
    mode = cfg.get("analyzer", "claude_code")
    if mode == "api":
        key = cfg.get("anthropic_api_key")
        if not key:
            raise RuntimeError("analyzer=api, но нет anthropic_api_key в config.json")
        resp = http_json(
            "https://api.anthropic.com/v1/messages", method="POST",
            payload={"model": cfg.get("anthropic_model", "claude-sonnet-5-5"),
                     "max_tokens": 3000,
                     "messages": [{"role": "user", "content": prompt}]},
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            timeout=300)
        return "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text").strip()

    exe = cfg.get("claude_cli") or shutil.which("claude") or shutil.which("claude.cmd") or shutil.which("claude.exe")
    if not exe:
        home = Path.home()
        for cand in [home / ".local/bin/claude.exe", home / ".local/bin/claude",
                     home / "AppData/Roaming/npm/claude.cmd", home / ".claude/local/claude.exe"]:
            if cand.exists():
                exe = str(cand)
                break
    if not exe:
        raise RuntimeError("Не найдена команда claude (Claude Code). Укажи путь в claude_cli или используй analyzer=api.")
    cmd = [exe, "-p", "--output-format", "text"]
    if cfg.get("claude_model"):
        cmd += ["--model", cfg["claude_model"]]
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=900, cwd=str(BASE))
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"Claude Code вернул ошибку: {(r.stderr or r.stdout)[:800]}")
    return r.stdout.strip()


# ---------------------------------------------------------------- telegram

def tg_send(cfg, text):
    token, chat = cfg.get("telegram_bot_token"), cfg.get("telegram_chat_id")
    if not token:
        raise RuntimeError("Не заполнен telegram_bot_token в config.json")
    if not chat:
        chat = auto_chat_id(cfg)
    chunks, cur = [], ""
    for para in text.split("\n"):
        if len(cur) + len(para) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += para + "\n"
    if cur.strip():
        chunks.append(cur)
    for c in chunks:
        http_json(f"https://api.telegram.org/bot{token}/sendMessage", method="POST",
                  payload={"chat_id": chat, "text": c, "disable_web_page_preview": True})


def auto_chat_id(cfg):
    """Берёт chat_id из последнего сообщения боту и сохраняет в config.json."""
    upd = http_json(f"https://api.telegram.org/bot{cfg['telegram_bot_token']}/getUpdates")
    for u in reversed(upd.get("result", [])):
        c = (u.get("message") or {}).get("chat") or {}
        if c.get("type") == "private" and c.get("id"):
            cfg["telegram_chat_id"] = c["id"]
            raw = json.loads(CONFIG_PATH.read_text("utf-8"))
            raw["telegram_chat_id"] = c["id"]
            CONFIG_PATH.write_text(json.dumps(raw, ensure_ascii=False, indent=2), "utf-8")
            log(f"chat_id {c['id']} найден и сохранён в config.json")
            return c["id"]
    raise RuntimeError("Не нашёл chat_id: открой своего бота в Telegram, напиши ему «привет» — "
                       "следующая проверка подхватит сама.")


def tg_chat_id(cfg):
    token = cfg.get("telegram_bot_token")
    if not token:
        sys.exit("Сначала впиши telegram_bot_token в config.json")
    upd = http_json(f"https://api.telegram.org/bot{token}/getUpdates")
    chats = {}
    for u in upd.get("result", []):
        msg = u.get("message") or u.get("channel_post") or {}
        c = msg.get("chat") or {}
        if c.get("id"):
            chats[c["id"]] = c.get("username") or c.get("title") or c.get("first_name")
    if not chats:
        print("Сообщений нет. Напиши своему боту что-нибудь в Telegram и запусти снова.")
    for cid, name in chats.items():
        print(f"chat_id = {cid}   ({name})")


# ---------------------------------------------------------------- flow

def process_match(cfg, od, match_id, acc, acc_name="", dry_run=False, fresh=False):
    if not dry_run and not cfg.get("telegram_chat_id"):
        auto_chat_id(cfg)  # проверяем Telegram ДО того, как тратить запрос к Claude
    match = od.get(f"/matches/{match_id}")
    heroes = od.constants("heroes")
    items = od.constants("items")
    extra = {"items": items}
    try:
        extra["abilities"] = od.constants("abilities")
    except Exception as e:
        log(f"Не удалось загрузить справочник способностей: {e}")
    summary, meta = build_summary(match, acc, heroes, items, extra)
    analysis = None if fresh else find_saved_analysis(match_id)
    if analysis:
        log(f"Матч {match_id} [{acc_name}]: разбор уже есть — отправляю сохранённый.")
    else:
        log(f"Матч {match_id} [{acc_name}]: {meta['hero']}, {'победа' if meta['won'] else 'поражение'}, "
            f"{'разобран' if meta['parsed'] else 'НЕ разобран'} — отправляю Claude…")
        analysis = analyze(cfg, build_prompt(cfg, summary, match_id))
        HISTORY_DIR.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        (HISTORY_DIR / f"{stamp}_{match_id}.txt").write_text(analysis, "utf-8")
        (HISTORY_DIR / f"{stamp}_{match_id}.data.md").write_text(summary, "utf-8")
    multi = len(accounts(cfg)) > 1
    header = (f"{'✅' if meta['won'] else '❌'} {meta['hero']} — матч {match_id}"
              f"{f' (аккаунт {acc_name})' if multi else ''}\n"
              f"https://www.opendota.com/matches/{match_id}\n\n")
    text = header + analysis
    if dry_run:
        print("\n" + text + "\n")
    else:
        tg_send(cfg, text)
        log("Разбор отправлен в Telegram.")


def wait_for_parse(cfg, od, match_id):
    match = od.get(f"/matches/{match_id}")
    if is_parsed(match):
        return True
    log(f"Матч {match_id} ещё не разобран — запрашиваю разбор у OpenDota (обычно 2–10 мин)…")
    try:
        od.post(f"/request/{match_id}")
    except Exception as e:
        log(f"Запрос разбора не прошёл: {e}")
    deadline = time.time() + cfg.get("parse_wait_minutes", 20) * 60
    while time.time() < deadline:
        time.sleep(30)
        if is_parsed(od.get(f"/matches/{match_id}")):
            return True
    log("Не дождался разбора — анализирую по базовым данным.")
    return False


_last_refresh = {}


def refresh_history(cfg, od, acc, acc_name):
    """Просим OpenDota подтянуть свежую историю матчей из Steam (иначе новые игры появляются с большой задержкой)."""
    every = max(30, int(cfg.get("refresh_minutes", 60))) * 60
    if time.time() - _last_refresh.get(acc, 0) < every:
        return
    _last_refresh[acc] = time.time()
    try:
        od.post(f"/players/{acc}/refresh")
    except Exception as e:
        log(f"[{acc_name}] Не удалось запросить обновление истории: {e}")


def fetch_recent(od, acc):
    """Список последних матчей. Если recentMatches пуст (так бывает, пока OpenDota обновляет историю) —
    берём из общего списка матчей игрока."""
    recent = od.get(f"/players/{acc}/recentMatches")
    if isinstance(recent, list) and recent:
        return recent
    alt = od.get(f"/players/{acc}/matches", limit=20)
    return alt if isinstance(alt, list) else []


def check_account(cfg, od, state, acc, acc_name, dry_run=False):
    st = state.setdefault(str(acc), {})
    pending = st.setdefault("pending", {})
    first_run = "last_analyzed" not in st
    last = st.get("last_analyzed", 0)

    recent = fetch_recent(od, acc)
    newest = max((m.get("start_time", 0) for m in recent), default=0)
    if int(time.time()) - st.get("_logged_newest", 0) > 3600:
        st["_logged_newest"] = int(time.time())
        log(f"[{acc_name}] Последний матч, который видит OpenDota: "
            + (f"{datetime.fromtimestamp(newest):%d.%m %H:%M}" if newest else "нет данных"))
        try:
            prof = od.get(f"/players/{acc}")
            if isinstance(prof, dict) and (prof.get("profile") or {}).get("fh_unavailable"):
                log(f"[{acc_name}] ВНИМАНИЕ: OpenDota не может получить историю матчей. В Доте: Настройки → "
                    f"Социальное → включи «Открыть общедоступные данные матчей» (Expose Public Match Data).")
        except Exception:
            pass
    # Если давно нет новых матчей — раз в час просим OpenDota подтянуть историю из Steam
    if not newest or time.time() - newest > 2 * 3600:
        refresh_history(cfg, od, acc, acc_name)

    ids = {m["match_id"] for m in recent if m["match_id"] > last}
    ids |= {int(k) for k in pending}  # ожидающие матчи не теряем, даже если список пуст
    new = sorted(ids)
    if first_run:  # первый запуск — только последний матч, без спама старыми
        new = new[-1:]
    wait_sec = cfg.get("parse_wait_minutes", 20) * 60
    for mid in new:
        key = str(mid)
        if not is_parsed(od.get(f"/matches/{mid}")):
            if key not in pending:
                try:
                    od.post(f"/request/{mid}")
                    log(f"[{acc_name}] Новый матч {mid} — запросил разбор, проверю позже.")
                except Exception as e:
                    log(f"[{acc_name}] Запрос разбора {mid} не прошёл: {e}")
                pending[key] = time.time()
                save_state(state)
                return
            if time.time() - pending[key] < wait_sec:
                return  # ждём дальше, по порядку
        process_match(cfg, od, mid, acc, acc_name, dry_run)
        st["last_analyzed"] = max(mid, st.get("last_analyzed", 0))
        pending.pop(key, None)
        save_state(state)


def check_new(cfg, od, dry_run=False):
    state = load_state()
    if "last_analyzed" in state:  # старый формат (один аккаунт)
        old = {"last_analyzed": state.pop("last_analyzed"), "pending": state.pop("pending", {})}
        state[str(accounts(cfg)[0][0])] = old
    for acc, name in accounts(cfg):
        try:
            check_account(cfg, od, state, acc, name, dry_run)
        except Exception as e:
            log(f"[{name}] Ошибка: {e}")


# ---------------------------------------------------------------- period review

def compact_line(match, acc, acc_name, heroes):
    hero_by_id = {int(k): v for k, v in heroes.items()}
    me = next((p for p in match.get("players", []) if p.get("account_id") == acc), None)
    if not me:
        return None
    rad = me.get("isRadiant", me.get("player_slot", 0) < 128)
    won = match.get("radiant_win") == rad
    hero = hero_by_id.get(me.get("hero_id"), {}).get("localized_name", "?")
    parts = [datetime.fromtimestamp(match.get("start_time", 0)).strftime("%d.%m %H:%M"), acc_name, hero,
             LANE_ROLE.get(me.get("lane_role"), "?"), "ПОБЕДА" if won else "поражение", mmss(match.get("duration")),
             f"KDA {me.get('kills')}/{me.get('deaths')}/{me.get('assists')}",
             f"GPM {me.get('gold_per_min')} XPM {me.get('xp_per_min')}", f"LH/DN {me.get('last_hits')}/{me.get('denies')}"]
    if match.get("version") is not None:
        lh10 = at_min(me.get("lh_t"), 10)
        opp = [p for p in match["players"] if p.get("lane") == me.get("lane") and p.get("isRadiant") != rad]
        opp10 = max((at_min(p.get("lh_t"), 10) or 0) for p in opp) if opp else None
        tf = me.get("teamfight_participation")
        parts += [f"LH@10 {lh10} (лучший оппонент на линии {opp10})",
                  f"урон по героям {me.get('hero_damage')}", f"мёртв {mmss(me.get('life_state_dead'))}",
                  f"участие в драках {round(tf * 100) if tf is not None else '?'}%",
                  f"варды obs/sen {me.get('obs_placed')}/{me.get('sen_placed')}", f"стаки {me.get('camps_stacked')}",
                  f"смерти от: " + ", ".join(f"{k.replace('npc_dota_hero_', '')}×{v}" for k, v in
                                            sorted((me.get("killed_by") or {}).items(), key=lambda x: -x[1])[:3])]
    else:
        parts.append(f"урон по героям {me.get('hero_damage')} (реплей не разобран)")
    return " | ".join(str(x) for x in parts)


def period_review(cfg, od, since, dry_run=False):
    heroes = od.constants("heroes")
    start_ts = int(datetime.strptime(since, "%Y-%m-%d").timestamp())
    lines = []
    for acc, name in accounts(cfg):
        ms = od.get(f"/players/{acc}/matches", limit=60) or []
        ms = sorted((m for m in ms if m.get("start_time", 0) >= start_ts), key=lambda m: m["start_time"])
        log(f"[{name}] матчей с {since}: {len(ms)}")
        for m in ms:
            try:
                full = od.get(f"/matches/{m['match_id']}")
                if not is_parsed(full):
                    try:
                        od.post(f"/request/{m['match_id']}")
                    except Exception:
                        pass
                ln = compact_line(full, acc, name, heroes)
                if ln:
                    lines.append(ln)
            except Exception as e:
                log(f"Матч {m['match_id']}: {e}")
            time.sleep(3)
    if not lines:
        raise RuntimeError("За этот период матчей не найдено.")
    profile = cfg.get("player_profile", "")
    patch = BASE / "patch_context.md"
    if patch.exists():
        profile += "\n\nТекущий патч:\n" + patch.read_text("utf-8")
    prompt = f"""Ты — личный тренер по Dota 2. Ниже все матчи игрока за период с {since} (по одной строке на матч).
Сделай ОДИН общий разбор периода: не по каждому матчу, а закономерности, которые повторяются.
Опирайся только на цифры. Пиши по-русски, без markdown (никаких **, #, таблиц), эмодзи как маркеры разделов, до 3500 знаков.

Об игроке: {profile}

Формат:
📊 Итог периода — сколько игр, винрейт, на каких героях и ролях лучше и хуже всего (с цифрами).
🔁 Повторяющиеся ошибки — 3–5 вещей, которые видны в нескольких матчах (смерти, линия, фарм, участие в драках, вижн). Для каждой: в скольких матчах и какие цифры.
✅ Что получается — 2–3 сильные стороны с цифрами.
🦸 Герои и роли — что стоит брать чаще, что реже, исходя из результатов.
🎯 План на следующую неделю — 3 конкретных упражнения или правила на каждую игру.
Отдельной строкой: какие наблюдения встречаются только в одном матче и поэтому не вошли в выводы.

Матчи:
""" + "\n".join(lines)
    log(f"Отправляю Claude общий разбор по {len(lines)} матчам…")
    analysis = analyze(cfg, prompt)
    HISTORY_DIR.mkdir(exist_ok=True)
    (HISTORY_DIR / f"period_{since}_{datetime.now():%Y%m%d-%H%M}.md").write_text(analysis, "utf-8")
    text = f"📅 Общий разбор с {since}: {len(lines)} матчей\n\n" + analysis
    if dry_run:
        print(text)
    else:
        tg_send(cfg, text)
        log("Общий разбор отправлен в Telegram.")


# ---------------------------------------------------------------- chat in Telegram

CHAT_HELP = ("Можешь писать мне вопросы обычным текстом: про последнюю игру, сборки, героев, патч.\n"
             "Команды:\n/last — прислать последний разбор ещё раз\n"
             "/review — общий разбор за 7 дней (/review 14 — за 14 дней)\n/help — эта подсказка")


def tg_updates(cfg, offset, timeout=20):
    url = (f"https://api.telegram.org/bot{cfg['telegram_bot_token']}/getUpdates"
           f"?timeout={timeout}&offset={offset}&allowed_updates=%5B%22message%22%5D")
    return http_json(url, timeout=timeout + 15).get("result", [])


def tg_typing(cfg):
    try:
        http_json(f"https://api.telegram.org/bot{cfg['telegram_bot_token']}/sendChatAction", method="POST",
                  payload={"chat_id": cfg["telegram_chat_id"], "action": "typing"})
    except Exception:
        pass


def chat_context(cfg, n=3):
    """Последние разборы и данные матчей, чтобы отвечать на вопросы по делу."""
    if not HISTORY_DIR.exists():
        return ""
    latest = {}
    for f in sorted(HISTORY_DIR.glob("*.txt")):
        latest[f.stem.split("_")[-1]] = f
    parts = []
    files = sorted(latest.values())[-n:]
    for i, f in enumerate(reversed(files)):
        mid = f.stem.split("_")[-1]
        block = f"--- Матч {mid} (разбор) ---\n{f.read_text('utf-8')[:2500]}"
        data = f.with_name(f.stem + ".data.md")
        if data.exists():
            block += f"\n--- Матч {mid} (данные) ---\n" + data.read_text("utf-8")[:(9000 if i == 0 else 2500)]
        parts.append(block)
    periods = sorted(HISTORY_DIR.glob("period_*.md"))
    if periods:
        parts.append("--- Последний общий разбор периода ---\n" + periods[-1].read_text("utf-8")[:3000])
    return "\n\n".join(parts)


def answer_question(cfg, question, history):
    coach = PROMPT_PATH.read_text("utf-8") if PROMPT_PATH.exists() else ""
    patch = BASE / "patch_context.md"
    dialog = "\n".join(f"{'Игрок' if r == 'user' else 'Тренер'}: {t}" for r, t in history[-8:])
    prompt = f"""Ты — личный тренер игрока по Dota 2 и общаешься с ним в Telegram.
Отвечай на его сообщение коротко и по делу (обычно до 1500 знаков), по-русски, без markdown (никаких **, #, таблиц).
Опирайся на его реальные матчи и разборы ниже; если данных для ответа нет — так и скажи, не выдумывай цифры.
Если вопрос не про Dota — ответь в одну строку, что ты тренер по Доте.

Как ты обычно разбираешь матчи (для контекста):
{coach[:1500]}

Об игроке: {cfg.get('player_profile', '')}

{('Текущий патч:' + chr(10) + patch.read_text('utf-8')) if patch.exists() else ''}

Последние матчи и разборы:
{chat_context(cfg)}

Переписка до этого:
{dialog or '(пусто)'}

Новое сообщение игрока: {question}
"""
    return analyze(cfg, prompt)


def handle_chat(cfg, od, state, poll_timeout=20):
    """Читает новые сообщения боту и отвечает. Возвращает, было ли что-то обработано."""
    chat_id = str(cfg.get("telegram_chat_id") or "")
    if not chat_id:
        return False
    tg = state.setdefault("_telegram", {})
    if "offset" not in tg:  # первый запуск: старые сообщения пропускаем
        ups = tg_updates(cfg, 0, timeout=0)
        tg["offset"] = (ups[-1]["update_id"] + 1) if ups else 0
        save_state(state)
        return False
    ups = tg_updates(cfg, tg["offset"], timeout=poll_timeout)
    for u in ups:
        tg["offset"] = u["update_id"] + 1
        save_state(state)
        msg = u.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != chat_id:
            continue  # чужие сообщения игнорируем
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        log(f"Сообщение в Telegram: {text[:80]}")
        history = tg.setdefault("history", [])
        try:
            if text in ("/start", "/help"):
                tg_send(cfg, CHAT_HELP)
            elif text == "/last":
                files = sorted(HISTORY_DIR.glob("*_*.txt")) if HISTORY_DIR.exists() else []
                tg_send(cfg, files[-1].read_text("utf-8") if files else "Разборов пока нет.")
            elif text.startswith("/review"):
                arg = text.split()[1] if len(text.split()) > 1 else "7"
                days = int(arg) if arg.isdigit() else 7
                since = datetime.fromtimestamp(time.time() - days * 86400).strftime("%Y-%m-%d")
                tg_send(cfg, f"Собираю общий разбор за {days} дн., это займёт несколько минут…")
                period_review(cfg, od, since)
            else:
                tg_typing(cfg)
                reply = answer_question(cfg, text, history)
                tg_send(cfg, reply)
                history += [["user", text], ["coach", reply[:1500]]]
                tg["history"] = history[-12:]
                save_state(state)
        except Exception as e:
            log(f"Ошибка ответа в Telegram: {e}")
            try:
                tg_send(cfg, f"Не получилось ответить: {e}")
            except Exception:
                pass
    return bool(ups)


def main():
    ap = argparse.ArgumentParser(description="Dota Coach")
    ap.add_argument("--once", action="store_true", help="одна проверка матчей + ответы на сообщения (для облака)")
    ap.add_argument("--watch", action="store_true", help="следить за новыми матчами постоянно")
    ap.add_argument("--latest", action="store_true", help="разобрать последний матч сейчас")
    ap.add_argument("--match", type=int, help="разобрать конкретный матч")
    ap.add_argument("--chat-id", action="store_true", help="показать chat_id Telegram")
    ap.add_argument("--review", metavar="YYYY-MM-DD", help="один общий разбор всех матчей с этой даты")
    ap.add_argument("--fresh", action="store_true", help="заново проанализировать, даже если разбор уже есть")
    ap.add_argument("--dry-run", action="store_true", help="не отправлять в Telegram, печатать")
    args = ap.parse_args()

    if args.chat_id:
        cfg = json.loads(CONFIG_PATH.read_text("utf-8")) if CONFIG_PATH.exists() else {}
        tg_chat_id(cfg)
        return

    cfg = load_config()
    od = OpenDota(cfg.get("opendota_api_key"))
    accs = accounts(cfg)

    if args.review:
        if not args.dry_run and not cfg.get("telegram_chat_id"):
            auto_chat_id(cfg)
        period_review(cfg, od, args.review, args.dry_run)
        return

    if args.latest or args.match:
        best = None  # (start_time, match_id, acc, name)
        for acc, name in accs:
            for m in od.get(f"/players/{acc}/recentMatches") or []:
                if args.match and m["match_id"] != args.match:
                    continue
                cand = (m.get("start_time", 0), m["match_id"], acc, name)
                if best is None or cand > best:
                    best = cand
        if not best:
            sys.exit("Матч не найден ни на одном аккаунте — проверь steam_ids и публичность данных.")
        _, mid, acc, name = best
        wait_for_parse(cfg, od, mid)
        process_match(cfg, od, mid, acc, name, args.dry_run, args.fresh)
        return

    if args.once:
        try:
            check_new(cfg, od, args.dry_run)
        except Exception as e:
            log(f"Ошибка: {e}")
        if cfg.get("telegram_chat", True) and not args.dry_run:
            try:
                handle_chat(cfg, od, load_state(), poll_timeout=0)
            except Exception as e:
                log(f"Ошибка чата: {e}")
        return

    if args.watch:
        interval = max(2, int(cfg.get("check_interval_minutes", 5))) * 60
        log(f"Слежу за матчами ({', '.join(n for _, n in accs)}), проверка каждые {interval // 60} мин.")
        if cfg.get("telegram_chat", True):
            log("Чат в Telegram включён: можешь писать боту вопросы.")
        next_check = 0
        while True:
            if time.time() >= next_check:
                try:
                    check_new(cfg, od, args.dry_run)
                except Exception as e:
                    log(f"Ошибка: {e}")
                next_check = time.time() + interval
            if cfg.get("telegram_chat", True) and not args.dry_run:
                try:
                    handle_chat(cfg, od, load_state())
                except Exception as e:
                    log(f"Ошибка чата: {e}")
                    time.sleep(15)
            else:
                time.sleep(max(1, next_check - time.time()))
    else:
        check_new(cfg, od, args.dry_run)


if __name__ == "__main__":
    main()
