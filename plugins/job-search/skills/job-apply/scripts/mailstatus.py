#!/usr/bin/env python3
"""Письма о статусах с Mail.ru (IMAP) → job_tracker. Аналог hhstatus.py для почты.

  mailstatus.py                 показать, что изменилось бы (лист не трогает)
  mailstatus.py --apply         записать в лист (sheet.py web app) и запомнить прочитанные UID
  mailstatus.py --apply --tg    то же + сводка в Telegram, если есть изменения (для таймера)
  mailstatus.py --backtest N    прогнать последние N писем из каждой папки в сухом режиме
                                (состояние не трогает; проверка матчинга на реальных письмах)

Ящик только читается (SELECT READONLY + BODY.PEEK) — письма не помечаются прочитанными.
Папки: «Отклики» (серверный фильтр рабочих ответов), INBOX и INBOX/Newsletters (туда
иногда падают письма ATS, например Авито). В INBOX/Newsletters обрабатываются только
письма с домена, известного трекеру; в «Откликах» — все, кроме мусорных доменов.

Связь письма со строкой листа: домен отправителя → упоминания адресов в H/I/N строк
или карта известных ATS-доменов (DOMAIN_COMPANY); вакансия из темы/тела («…», «на вакансию …»)
→ сравнение с названием в B. Строка выбирается, только если совпадение уверенное и единственное.

Что пишется (E = Ожидание ответа / В процессе):
  reject   → E=Закрыто, F=-, D += «; отказ (письмо dd.mm)», N += строка; закрытая строка — только строка в N
  action   → E=В процессе, F=«⚠ ДЕЙСТВИЕ НУЖНО: …», N += строка; приглашение по закрытой — только в сводку
  ack      → N += «получено резюме» (один раз)
  info/soft→ N += строка (ответ рекрутера, «резерв») + деталь в сводку
  marketing (курсы/дайджести с «отписаться») и нерелевантные — пропускаются.
Нет уверенного совпадения → строка не трогается; reject/action попадают в сводку «не отнесено».

Состояние (прочитанные UID по папкам): $JOB_APPLY_HOME/mail-status.json.
Первый запуск — baseline: запоминает текущие UID и старые письма не обрабатывает.
Creds: $MAILRU_USER, $MAILRU_APPPASS (для systemd-таймера — в ~/.config/job-apply/worker.env).
"""
import argparse, email, imaplib, json, os, re, subprocess, sys
from datetime import date, datetime, timedelta
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path(os.environ.get("JOB_APPLY_HOME", "~/.local/share/job-apply")).expanduser()
STATE = HOME / "mail-status.json"
OPEN = {"Ожидание ответа", "В процессе"}
MAX_PER_FOLDER = 40          # писем на папку за один запуск (защита от завала)
BODY_CHARS = 8000

FOLDERS = {                  # имя для логов → raw IMAP-имя
    "Отклики": "&BB4EQgQ6BDsEOAQ6BDg-",
    "INBOX": "INBOX",
    "Newsletters": "INBOX/Newsletters",
}
# домен (суффикс) → токены компании для колонки A; None = сопоставлять только по id вакансии hh
DOMAIN_COMPANY = {
    "sberbank.ru": ["сбер", "sber"], "sber.ru": ["сбер", "sber"],
    "ozon.ru": ["ozon"], "avito.ru": ["avito", "авито"], "x5.ru": ["x5"],
    "vk.company": ["vk"], "otpbank.ru": ["отп", "otp"], "mts.ru": ["мтс", "mts"],
    "yadro.com": ["yadro"], "psbank.ru": ["псб", "psb"], "tbank.ru": ["т-банк", "тбанк", "t"],
    "ozon.tech": ["ozon"], "magnit.tech": ["магнит", "magnit"], "mindbox.ru": ["mindbox"],
    "hh.ru": None, "changellenge.com": [],  # дайджести — пропуск
}
SKIP_DOMAINS = [  # бытовой мусор, не работодатели
    "mos.ru", "gosuslugi.ru", "google.com", "russianpost.ru", "pofd.ru", "payonline.ru",
    "rustore.ru", "ingos-m.ru", "russia.ru", "changellenge.com", "mail.ru", "yandex.ru",
]
REJECT = ["отклонен", "не готовы", "предпочтение друг", "определились с другим", "не смогли",
          "не сможем", "отказ", "не прошли", "не соответству",
          "не готовы предложить", "не готовы пригласить"]
SOFT = ["сохранено в базе", "вернемся при появлении", "вернёмся при появлении", "кадровый резерв",
        "резерв", "добавили ваше резюме в базу", "ваше резюме в базе"]
ACTION = ["пройдите", "приглашаем", "приглашение", "интервью", "тест", "тестирование", "задание",
          "подтвердите", "подтвердить", "подтверждение", "оффер", "offer", "собеседование",
          "анкету", "заполните", "встречу", "ссылка действует", "успей", "финальный этап"]
ACK = ["получили ваше резюме", "получили твой отклик", "получили резюме", "спасибо за отклик",
       "заявка принята", "оставлен отклик", "мы получили", "отклик получен", "резюме получено",
       "принята в работу", "ваш отклик"]
MARKETING = ["отписаться", "рассылк", "дайджест", "подборка", "вебинар", "новый курс", "курсы по",
             "лабиринт возможностей", "unsubscribe", "сериал"]
SUBJ_MARKETING = ["курс", "вебинар", "дайджест", "подборка", "новые вакансии", "сериал", "рассылк"]
SKIP_SENDERS = ["profile@sberbank.ru", "noreply@sberbank.ru", "no-reply@sberbank.ru"]
NONJOB = ["сбербанк онлайн", "госуслуг", "подтверждение email в", "выписка", "полис", "заказ ", "доставк",
          "начале обработки персональных данных", "уведомление об обработке персональных"]
STOP = set("в на по и с из за для от до не что как это вы мы ваш наша наше вашa the a an of to for "
           "and or is are was были быть был она они он you your we our".split())


def norm(s):
    return (s or "").lower().replace("ё", "е")


def tokens(s):
    return {t for t in re.split(r"[^a-zа-я0-9]+", norm(s)) if len(t) >= 2 and t not in STOP}


def tok_in(t, toks):
    if t in toks:
        return True
    return len(t) >= 5 and any(len(x) >= 5 and (t[:5] == x[:5]) for x in toks)


def overlap(a, b):
    return sum(1 for t in a if tok_in(t, b))


def hdr(m, h):
    return str(make_header(decode_header(m.get(h, "") or "")))


def body_text(m):
    plain = html_txt = ""
    for p in m.walk():
        ct = p.get_content_type()
        if ct in ("text/plain", "text/html") and not (plain if ct == "text/plain" else html_txt):
            try:
                t = p.get_payload(decode=True).decode(p.get_content_charset() or "utf-8", "replace")
            except Exception:
                continue
            if ct == "text/html":
                import html as _h
                t = _h.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"<(script|style).*?</\1>", "", t, flags=re.S)))
                html_txt = re.sub(r"\s+", " ", t)
            else:
                plain = re.sub(r"\s+", " ", t)
    return (plain or html_txt)[:BODY_CHARS]


def reg_domain(addr):
    d = (addr or "").split("@")[-1].lower()
    parts = d.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else d


def domain_hit(sender_dom, known_dom):
    return sender_dom == known_dom or sender_dom.endswith("." + known_dom) or known_dom.endswith("." + sender_dom)


def msg_date(m):
    try:
        return parsedate_to_datetime(hdr(m, "Date")).astimezone()
    except Exception:
        return datetime.now()


def trim_body(body):
    """Только свежий текст: отрезаем процитированную историю (>Отправитель…) и футер рассылки."""
    b = re.split(r"(?im)^\s*>*\s*(отправитель|from\s*:|-----original|исходное сообщение|>{2})", body)[0]
    for marker in ["вы получили это письмо", "вы получили данное письмо", "отписаться от рассылок",
                   "отписаться от рассылки", "вы подписаны", "unsubscribe", "manage subscription",
                   "информационное сообщение", "вы получаете это письмо"]:
        i = norm(b).find(marker)
        if i > 200:
            b = b[:i]
    return b


def reject_hit(t):
    """Настоящий отказ: шаблон есть, и в его предложении нет «если/значит» до него (условие — не отказ)."""
    for p in REJECT:
        for m in re.finditer(re.escape(p), t):
            s = max(t.rfind(c, 0, m.start()) for c in ".!?\n;") + 1
            pre = t[s:m.start()]
            if "если" not in pre and "значит" not in pre:
                return True
    return False


def classify(subj, body_main):
    t = norm(subj + " " + body_main)
    if reject_hit(t):
        return "reject", ""
    sj = norm(subj)
    if any(p in sj for p in SUBJ_MARKETING) or any(p in t for p in MARKETING):
        return "marketing", ""
    if any(p in t for p in SOFT):
        return "soft", ""
    if any(p in t for p in ACTION):
        return "action", action_hint(t)
    if any(p in t for p in ACK):
        return "ack", ""
    return "info", ""


def action_hint(t):
    if "подтверди" in t and ("почт" in t or "email" in t or "e-mail" in t):
        return "подтвердить почту по ссылке из письма"
    if "ai-интервью" in t or "гигарекрутер" in t:
        return "пройти AI-интервью (ГигаРекрутер: Telegram или Макс)"
    if "интервью" in t:
        return "ответить на приглашение на интервью"
    if "тест" in t:
        return "пройти тест"
    if "задани" in t:
        return "выполнить задание"
    if "оффер" in t or "offer" in t:
        return "оффер — открыть письмо/ЛК и ответить"
    if "собеседован" in t or "встреч" in t:
        return "ответить на приглашение на встречу"
    if "анкет" in t:
        return "заполнить анкету"
    return "отреагировать на письмо работодателя"


def link_ttl(t, dt):
    """Дедлайн одноразовой ссылки подтверждения, если в письме есть TTL."""
    m = re.search(r"действу\w+\s+(\d+)\s*(час|сут|дн|мин)", t)
    if not m:
        return ""
    n, unit = int(m.group(1)), m.group(2)
    delta = timedelta(minutes=n) if unit == "мин" else timedelta(hours=n) if unit == "час" else timedelta(days=n)
    dl = dt + delta
    return f" (ссылка до {dl:%d.%m %H:%M} МСК)"


def titles(subj, body_main):
    """Кандидаты на название вакансии: кавычки в теме/теле, «на вакансию X», вся тема без Re:."""
    src = subj + " " + body_main[:1500]
    out = re.findall(r"[«\"]\s*([^»\"]{4,90}?)\s*[»\"]", src)
    out += re.findall(r"ваканси[июяи]\s*[:«\"]?\s*([А-ЯA-ZЁa-z][^»\n,.!?;]{3,80})", src)
    s = re.sub(r"^(re|fwd|aw)\s*:\s*", "", subj, flags=re.I).strip()
    s = re.sub(r"[:—-]\s*(заявка|отклик|вопрос|feedback|ответ).*$", "", s, flags=re.I).strip()
    if len(s) >= 8:
        out.append(s)
    return [o.strip(" .:-") for o in out if len(o.strip(" .:-")) >= 5]


class Sheet:
    def __init__(self):
        sys.path.insert(0, str(HERE))
        import sheet
        if not sheet.conf():
            sys.exit("ERR: sheet.py не настроен (~/.config/job-apply/sheet.env)")
        self.sheet = sheet
        self.rows = sheet.call("find", query="", limit=1000)["rows"]
        self.by_dom, self.by_hh = {}, {}
        for r in self.rows:
            v = r["values"]
            blob = " ".join([v[0], v[7], v[8], v[13]])
            for d in {reg_domain(a) for a in re.findall(r"[\w.+-]+@([\w.-]+\.\w+)", blob)}:
                self.by_dom.setdefault(d, []).append(r)
            for i in set(re.findall(r"hh\.ru/vacancy/(\d+)", v[8] + " " + v[13])):
                self.by_hh.setdefault(i, []).append(r)

    def candidates(self, sender_dom, from_name):
        cands = {r["row"]: r for dom, rs in self.by_dom.items() if domain_hit(sender_dom, dom) for r in rs}
        for dom, comps in DOMAIN_COMPANY.items():
            if comps and domain_hit(sender_dom, dom):
                for r in self.rows:
                    a = norm(r["values"][0])
                    if any(tok_in(c, tokens(a)) or c in a for c in comps):
                        cands[r["row"]] = r
        if from_name:
            fn = tokens(from_name)
            for r in self.rows:
                if fn & tokens(r["values"][0]):
                    cands[r["row"]] = r
        return list(cands.values())

    def pick(self, cands, title_toks, subj_toks):
        """(строка, совпадение по названию вакансии): сначала по названию, потом по всей теме."""
        def best(key):
            scored = sorted(((overlap(key, tokens(r["values"][1])) / max(len(key), 1), r) for r in cands),
                            key=lambda x: -x[0])
            if not scored or scored[0][0] < 0.5:
                return None
            if len(scored) > 1 and scored[1][0] >= scored[0][0] * 0.9:
                return None
            return scored[0][1]
        for tt in title_toks:
            r = best(tokens(tt))
            if r:
                return r, True
        return (best(subj_toks), False) if subj_toks else (None, False)


def connect():
    M = imaplib.IMAP4_SSL("imap.mail.ru", 993)
    M.login(os.environ["MAILRU_USER"], os.environ["MAILRU_APPPASS"])
    return M


def folder_state(M, raw):
    d = M.status(f'"{raw}"', "(UIDVALIDITY UIDNEXT)")[1][0].decode()
    uv = int(re.search(r"UIDVALIDITY (\d+)", d).group(1))
    nxt = int(re.search(r"UIDNEXT (\d+)", d).group(1))
    return uv, nxt - 1


def fetch_new(M, raw, st, backtest=0):
    """(uid, message) для новых писем; при backtest — последние N независимо от состояния."""
    uv, maxuid = folder_state(M, raw)
    if M.select(f'"{raw}"', readonly=True)[0] != "OK":
        return [], (uv, maxuid)
    if backtest:
        typ, data = M.uid("SEARCH", None, "ALL")
        uids = [int(x) for x in (data[0] or b"").split()][-backtest:]
    else:
        last = st.get("uid", 0) if st.get("uidvalidity") == uv else maxuid   # baseline / сброс UV
        if last >= maxuid:
            return [], (uv, maxuid)
        typ, data = M.uid("SEARCH", None, f"UID {last + 1}:*")
        uids = [int(x) for x in (data[0] or b"").split() if int(x) > last][-MAX_PER_FOLDER:]
    out = []
    for uid in uids:
        try:
            out.append((uid, email.message_from_bytes(M.uid("FETCH", str(uid), "(BODY.PEEK[])")[1][0][1])))
        except Exception:
            continue
    return out, (uv, maxuid)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--tg", action="store_true")
    ap.add_argument("--backtest", type=int, default=0)
    a = ap.parse_args()
    me = os.environ.get("MAILRU_USER", "").lower()
    state = json.loads(STATE.read_text()) if STATE.exists() and not a.backtest else {}

    M = connect()
    fetched = {}
    for name, raw in FOLDERS.items():
        msgs, uvmax = fetch_new(M, raw, state.get(raw, {}), a.backtest)
        fetched[raw] = (msgs, uvmax, name)
    M.logout()

    n_new = sum(len(v[0]) for v in fetched.values())
    if not n_new:
        if not a.backtest:
            for raw, (_, uvmax, _) in fetched.items():     # baseline / продвижение состояния
                state[raw] = {"uidvalidity": uvmax[0], "uid": uvmax[1]}
            if a.apply:
                STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
            print("почта: новых писем нет" + ("" if a.apply or STATE.exists() else " (baseline записан в режиме --apply)"))
        else:
            print("почта: в папках пусто")
        return

    sh = Sheet()
    changes, notes = [], []
    for raw, (msgs, uvmax, name) in fetched.items():
        strict = name != "Отклики"          # в INBOX/Newsletters — только известные домены
        for uid, m in msgs:
            subj = " ".join(hdr(m, "Subject").split())
            full_body = body_text(m)
            body_main = trim_body(full_body)
            from_name, from_addr = parseaddr(hdr(m, "From"))
            dom = reg_domain(from_addr)
            if from_addr.lower() == me or from_addr.lower() in SKIP_SENDERS \
                    or any(domain_hit(dom, s) for s in SKIP_DOMAINS):
                continue
            if any(p in norm(subj + " " + body_main[:600]) for p in NONJOB):
                continue                    # банк/госуслуги/заказы — не работодатели
            hh_id = re.search(r"hh\.ru/vacancy/(\d+)", subj + " " + body_main)
            cands = sh.by_hh.get(hh_id.group(1), []) if hh_id else []
            if not cands:
                cands = sh.candidates(dom, from_name)
            if strict and not cands:
                continue                    # шум INBOX без связи с трекером
            cls, hint = classify(subj, body_main)
            if cls == "marketing":
                continue
            if cls != "reject" and any(p in norm(full_body[-700:])
                                       for p in ["отписаться", "отписки", "unsubscribe", "рассылк"]):
                sjn = norm(subj)
                if not any(w in sjn for w in ["ваканс", "отклик", "интервью", "тест", "подтвер", "оффер",
                                              "собесед", "приглаш", "заявк", "резюме"]):
                    continue                # рассылка с футером отписки: в теме нет рабочих слов
            dt = msg_date(m)
            stamp = f"{dt:%d.%m %H:%M}"
            key = (f"{dt:%d.%m}", subj[:40])
            in_n = lambda text: key[0] in text and key[1] in text
            if any(in_n(c["values"][13]) for c in cands):
                continue                    # письмо уже записано в одну из строк (вручную/прошлым прогоном)
            title_toks = titles(subj, body_main)
            r, via_title = sh.pick(cands, title_toks, tokens(subj)) if cands else (None, False)
            line = f"почта {stamp} {from_addr}: {subj[:90]}" + (f" — {hint}" if hint else "")
            short = f"{name}: {hdr(m, 'From')[:40]} | {subj[:70]} [{cls}]"
            if r is None:
                if cls in ("reject", "action") and cands:
                    notes.append(f"не отнесено к строке ({cls}): {short}")
                elif not strict and cls in ("info", "soft") and cands:
                    notes.append(f"не отнесено (несколько строк {dom}): {short}")
                continue
            v, row = r["values"], r["row"]
            is_open = v[4] in OPEN
            if in_n(v[13]) or any(c[0] == row and in_n(c[2].get("N", "")) for c in changes):
                continue                    # уже записано (вручную/прошлым прогоном)
            nm = f"#{row} {v[0]} — {v[1][:50]}"
            if cls == "reject":
                d_col = v[3] if "отказ" in norm(v[3]) else f"{v[3]}; отказ (письмо {dt:%d.%m})".lstrip("; ")
                if is_open:
                    changes.append((row, {"E": "Закрыто", "F": "-", "D": d_col}, {"N": line}, f"{nm}: ОТКАЗ письмом ({stamp})"))
                else:
                    changes.append((row, {}, {"N": line}, f"{nm}: формальный отказ письмом (строка уже закрыта)"))
            elif cls == "action":
                if is_open:
                    ttl = link_ttl(norm(body_main), dt) if "подтверди" in hint else ""
                    f_txt = f"⚠ ДЕЙСТВИЕ НУЖНО: {hint}{ttl} — письмо {stamp} ({from_addr})"
                    changes.append((row, {"E": "В процессе", "F": f_txt}, {"N": line}, f"{nm}: {hint.upper()}{ttl}"))
                else:
                    notes.append(f"{nm}: {hint} письмом, но строка закрыта ({v[4]}) — посмотри руками")
            elif is_open:                   # ack / info / soft — только строка в N + деталь в сводку
                changes.append((row, {}, {"N": line}, f"{nm}: {cls} ({subj[:60]})"))
                if cls in ("info", "soft"):
                    notes.append(f"{nm} — письмо от {from_addr} ({subj[:60]}): {body_main[:200]}")
            elif cls in ("info", "soft"):   # ответ совпал с закрытой строкой — запись туда + подсказка
                changes.append((row, {}, {"N": line}, f"{nm}: {cls} по закрытой строке ({subj[:60]})"))
                opens = [c for c in cands if c["values"][4] in OPEN]
                if len(opens) == 1:
                    notes.append(f"письмо «{subj[:50]}» совпало с закрытой #{row}, но открыта строка "
                                 f"#{opens[0]['row']} {opens[0]['values'][0]} — возможно, касается её; проверить")

    out = []
    if changes:
        out.append("Изменения из почты:")
        out += [f"- {c[3]}" for c in changes]
    if notes:
        out.append("Посмотреть:")
        out += [f"- {x}" for x in notes[:15]]
    if not out:
        out = [f"почта: {n_new} новых писем, статусы не изменились"]
    print("\n".join(out))

    if not a.apply:
        print("\n(сухой прогон; --apply запишет)")
        return
    for row, set_, add, _ in changes:
        sh.sheet.call("update", row_number=row, set=set_, append=add)
    if not a.backtest:
        for raw, (_, uvmax, _) in fetched.items():
            state[raw] = {"uidvalidity": uvmax[0], "uid": uvmax[1]}
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    (HOME / "sheet-cache.json").unlink(missing_ok=True)   # status.py перечитает лист
    if a.tg and (changes or notes):
        subprocess.run(["tg", "send", "\n".join(out)], check=False, timeout=60)


if __name__ == "__main__":
    main()
