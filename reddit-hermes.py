#!/usr/bin/env python3
"""MISC-REDDITHR: новые треды r/hermesagent → RU-сводка в топик AI (4307).

Крон `reddit-hermes` (0 11 * * *, no-agent). Reddit доступен только через
OAuth app-only (REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET в ~/.hermes/.env):
анонимный .json отдаёт 403 на обоих egress, redlib-зеркала мертвы —
проверено 08.11, см. skill blocked-page-recovery. Токен кэшируется в state.
Дедуп по id постов (30 дней). Новые треды → Halogen отбирает «интересное
инженеру» (плагины/тулы для Hermes от сообщества, истории применения, советы)
и пишет RU-сводку; модель отвечает номерами, скрипт подставляет URL (паттерн
ai-digest 07.11 — защита от порчи ссылок). Пусто/NONE — молчим. --dry:
печать без поста и без правки state.
"""
import base64
import datetime as dt
import html as H
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "hermes:halogen-tools:1.0 (by /u/ProfessionalEgg5008)"
ENV_FILE = pathlib.Path(os.path.expanduser("~/.hermes/.env"))
STATE = pathlib.Path(os.path.expanduser("~/.hermes/state/reddit-hermes"))
TG_CHAT = "-1004380870977"
TG_THREAD = 4307
SUBREDDIT = "hermesagent"
HALOGEN_ENV = pathlib.Path(os.path.expanduser("~/.config/halogen/env"))
ENDPOINT = "http://127.0.0.1:8731/v1/chat/completions"
HEALTH = "http://127.0.0.1:8731/health"
MODEL = "halogen-qwen3.8-flash-next"
MAX_POSTS = 15          # максимум тредов в одной сводке
SELFTEXT_SNIP = 300     # сколько текста поста показывать модели


def log(msg: str) -> None:
    print(f"[reddit-hermes] {msg}", file=sys.stderr)


def env_get(key: str) -> str:
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{key}=") and not line.startswith("#"):
            return line.split("=", 1)[1].strip().strip('"')
    return ""


def http_json(method: str, url: str, headers: dict,
              data: bytes | None = None) -> dict:
    """HTTP к Reddit: перебираем оба egress (socks5 xray :1080 → прямой);
    HTTPError (4xx/5xx) тоже пробуем вторым egress — блоки Reddit бывают
    per-IP (blocked-page-recovery), наружу поднимаем последнюю ошибку."""
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    last_err: Exception | None = None
    for proxy in ("socks5://127.0.0.1:1080", ""):
        try:
            if proxy:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
                return json.loads(opener.open(req, timeout=30).read())
            return json.loads(urllib.request.urlopen(req, timeout=30).read())
        except Exception as e:  # noqa: BLE001 — включая HTTPError: второй egress
            last_err = e
    raise RuntimeError(f"{method} {url}: {last_err}")


def access_token() -> str:
    """App-only OAuth с кэшем в state (тукен живёт ~1 ч, обновляем за 5 мин)."""
    cid, secret = env_get("REDDIT_CLIENT_ID"), env_get("REDDIT_CLIENT_SECRET")
    if not cid or not secret:
        raise RuntimeError("REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET не заданы в ~/.hermes/.env")
    STATE.mkdir(parents=True, exist_ok=True)
    tf = STATE / "token.json"
    try:
        cached = json.loads(tf.read_text())
        if cached.get("expires_at", 0) > time.time() + 300:
            return cached["access_token"]
    except (FileNotFoundError, ValueError, KeyError):
        pass
    basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    d = http_json("POST", "https://www.reddit.com/api/v1/access_token",
                  {"Authorization": f"Basic {basic}",
                   "User-Agent": UA,
                   "Content-Type": "application/x-www-form-urlencoded"},
                  b"grant_type=client_credentials")
    tok = d["access_token"]
    tf.write_text(json.dumps({"access_token": tok,
                              "expires_at": int(time.time()) + int(d.get("expires_in", 3600))}))
    os.chmod(tf, 0o600)  # OAuth-токен — секрет
    return tok


def new_posts(token: str) -> list[dict]:
    """Свежие посты сабреддита (до 3 страниц по 100), без stickied."""
    out: list[dict] = []
    after = ""
    for _ in range(3):
        url = (f"https://oauth.reddit.com/r/{SUBREDDIT}/new"
               f"?limit=100&raw_json=1" + (f"&after={after}" if after else ""))
        d = http_json("GET", url, {"Authorization": f"Bearer {token}",
                                   "User-Agent": UA})
        children = d.get("data", {}).get("children", [])
        out.extend(c["data"] for c in children
                   if not c["data"].get("stickied") and c["data"].get("id"))
        after = d.get("data", {}).get("after") or ""
        if not after:
            break
        time.sleep(1)
    return out


def halogen(prompt: str) -> str:
    key = ""
    for line in HALOGEN_ENV.read_text().splitlines():
        if line.startswith("HALOGEN_API_KEY="):
            key = line.split("=", 1)[1].strip().strip('"')
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "enable_thinking": False,
        "max_tokens": 1024,
    }).encode()
    req = urllib.request.Request(ENDPOINT, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {key}"})
    from halogen_ready import wait_ready  # общий хелпер (07.11)
    wait_ready(HEALTH)
    for attempt in range(1, 5):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                d = json.loads(r.read().decode())
            return d["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError:
            raise
        except OSError as e:
            if attempt == 4:
                raise
            time.sleep(15 * attempt)
    return ""


def tg_send(text: str) -> int:
    token = env_get("TELEGRAM_BOT_TOKEN")
    payload = json.dumps({
        "chat_id": TG_CHAT, "message_thread_id": TG_THREAD, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }).encode()
    last_err: Exception | None = None
    for proxy in ("socks5://127.0.0.1:1080", ""):
        try:
            if proxy:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
                resp = json.loads(opener.open(
                    urllib.request.Request(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        data=payload, headers={"Content-Type": "application/json"}),
                    timeout=30).read())
            else:
                resp = json.loads(urllib.request.urlopen(
                    urllib.request.Request(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        data=payload, headers={"Content-Type": "application/json"}),
                    timeout=30).read())
            if not resp.get("ok"):
                raise RuntimeError(resp.get("description", "unknown"))
            return int(resp["result"]["message_id"])
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"tg_send: {last_err}")


def load_seen() -> dict:
    try:
        return json.loads((STATE / "seen.json").read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_seen(seen: dict) -> None:
    cutoff = (dt.date.today() - dt.timedelta(days=30)).isoformat()
    seen = {k: v for k, v in seen.items() if str(v) >= cutoff}
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "seen.json").write_text(json.dumps(seen, indent=0))


def build_prompt(posts: list[dict]) -> str:
    lines = []
    for i, p in enumerate(posts, 1):
        st = (p.get("selftext") or "").strip()
        if st in ("[removed]", "[deleted]"):
            st = ""
        ext = p.get("url", "") if not p.get("is_self") else ""
        lines.append(f"[{i}] {p['title']} | автор {p.get('author')} | "
                     f"⬆{p.get('score')} 💬{p.get('num_comments')}"
                     + (f" | внешняя ссылка: {ext}" if ext else "")
                     + (f" | текст: {st[:SELFTEXT_SNIP]}" if st else ""))
    return ("Ты редактор Telegram-топика про AI. Вот новые треды сабреддита "
            "r/hermesagent (Hermes Agent от Nous Research):\n\n"
            + "\n".join(lines)
            + "\n\nОставь ТОЛЬКО интересное инженеру-практику: плагины и тулы "
            "для Hermes от сообщества, истории применения, советы, релизы, "
            "важные баги/фичи. Пустые баг-репорты в одну строку и офтоп — "
            "пропусти. Формат ответа: для каждого оставленного треда строка "
            "вида `номер — сводка по-русски (1-2 строки, без markdown)`; "
            "никаких других строк. Если ничего интересного — ответь ровно "
            "NONE.")


def build_message(today: dt.date, posts: list[dict], summary: str) -> str:
    """Собирает HTML: номера из ответа модели заменяются ссылками на посты."""
    by_num = {i + 1: p for i, p in enumerate(posts)}
    lines = [f"📮 <b>Reddit · r/{SUBREDDIT} · {today.strftime('%d.%m')}</b>"]
    for raw in summary.splitlines():
        raw = raw.strip().lstrip("-• ").strip()
        if not raw:
            continue
        n, sep, rest = raw.partition("—")
        n = n.strip().strip("[](). ")  # «1.» / «[1]» / «1)» → «1» (частый формат модели)
        if sep and n.isdigit() and int(n) in by_num:
            p = by_num[int(n)]
            url = "https://www.reddit.com" + p["permalink"]
            lines.append(f"• <a href=\"{url}\">{H.escape(p['title'])}</a> — "
                         f"{H.escape(rest.strip())}")
        else:
            lines.append(f"• {H.escape(raw)}")
    return "\n".join(lines)


def main() -> None:
    dry = "--dry" in sys.argv
    today = dt.date.today()
    token = access_token()
    posts = new_posts(token)
    seen = load_seen()
    fresh = [p for p in posts if p["id"] not in seen][:MAX_POSTS]
    if not fresh:
        log("новых тредов нет — молчим")
        return
    log(f"новых тредов: {len(fresh)} (всего на сабреддите {len(posts)})")
    summary = halogen(build_prompt(fresh))
    if summary.strip().upper().startswith("NONE"):
        log("Halogen: ничего интересного — молчим")
        if not dry:
            for p in fresh:
                seen[p["id"]] = today.isoformat()
            save_seen(seen)
        return
    text = build_message(today, fresh, summary)
    if dry:
        print(text)
        log("dry-run: без поста и без правки state")
        return
    tg_send(text)
    for p in fresh:
        seen[p["id"]] = today.isoformat()
    save_seen(seen)
    print(text)
    log(f"опубликовано {len(fresh)} тредов")


if __name__ == "__main__":
    main()
