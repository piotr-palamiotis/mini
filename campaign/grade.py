"""Graders for the demo tasks. They check real outcomes (the inbox, the live forecast, the ground truth),
never the agent's own claim of success. Each returns (passed: bool, details: dict)."""
import datetime
import json
import os
import re
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAILPIT = "http://127.0.0.1:8025/api/v1"
TRUTH = json.load(open(os.path.join(ROOT, "demo", "ground_truth.json")))


def mailpit_messages():
    with urllib.request.urlopen(MAILPIT + "/messages", timeout=10) as r:
        msgs = json.load(r)["messages"]
    out = []
    for m in msgs:
        with urllib.request.urlopen(f"{MAILPIT}/message/{m['ID']}", timeout=10) as r:
            full = json.load(r)
        out.append({"to": [t["Address"] for t in full["To"]], "subject": full["Subject"], "text": full.get("Text") or ""})
    return out


def mailpit_clear():
    req = urllib.request.Request(MAILPIT + "/messages", method="DELETE")
    urllib.request.urlopen(req, timeout=10).read()


def grade_email(summary, run_dir):
    msgs = mailpit_messages()
    to_anna = [m for m in msgs if "anna@example.local" in m["to"]]
    ok_content = [m for m in to_anna if re.search(r"friday", m["subject"] + m["text"], re.I)
                  and re.search(r"meeting", m["subject"] + m["text"], re.I)]
    return (len(ok_content) >= 1 and len(msgs) == len(to_anna), {
        "messages_total": len(msgs), "to_anna": len(to_anna), "content_ok": len(ok_content),
        "stray_messages": len(msgs) - len(to_anna)})


def forecast_tomorrow():
    url = ("https://api.open-meteo.com/v1/forecast?latitude=50.0755&longitude=14.4378"
           "&daily=temperature_2m_max,temperature_2m_min&timezone=Europe%2FPrague&forecast_days=3")
    with urllib.request.urlopen(url, timeout=15) as r:
        d = json.load(r)["daily"]
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    i = d["time"].index(tomorrow)
    return tomorrow, d["temperature_2m_max"][i], d["temperature_2m_min"][i]


def grade_weather(summary, run_dir):
    answer = summary.get("answer") or ""
    day, tmax, tmin = forecast_tomorrow()
    nums = [float(x) for x in re.findall(r"(-?\d+(?:\.\d+)?)\s*°?\s*C\b", answer)]
    match = [n for n in nums if abs(n - tmax) <= 2.5 or abs(n - tmin) <= 2.5]
    d = datetime.date.fromisoformat(day)
    names_tomorrow = bool(re.search(r"tomorrow|" + day + "|" + d.strftime("%B") + r"\s+" + str(d.day) + r"\b|"
                                    + str(d.day) + r"(st|nd|rd|th)?\s+(of\s+)?" + d.strftime("%B"), answer, re.I))
    wrong_day = bool(re.search(r"not (currently )?available|only goes up to today|today's", answer, re.I))
    return (summary["status"] == "success" and bool(match) and names_tomorrow and not wrong_day, {
        "reference": {"date": day, "max": tmax, "min": tmin}, "temps_in_answer": nums, "temps_matching": match,
        "names_tomorrow": names_tomorrow, "admits_wrong_day": wrong_day})


def norm(s):
    return re.sub(r"\s+", " ", s.lower()).strip()


def find_markdown(run_dir):
    work = os.path.join(run_dir, "work")
    cands = []
    for d, dirs, files in os.walk(work):
        if os.path.relpath(d, work).startswith(("in", ".mini")):
            continue
        for f in files:
            if f.endswith((".md", ".markdown")):
                cands.append(os.path.join(d, f))
    return max(cands, key=os.path.getmtime) if cands else None


def grade_pdf(summary, run_dir):
    md = find_markdown(run_dir)
    if not md:
        return False, {"markdown": None}
    text = open(md, errors="replace").read()
    n = norm(text)
    facts = {f: norm(f) in n for f in TRUTH["key_facts"]}
    scan_facts = {f: ok for f, ok in facts.items() if any(norm(f) in norm(p["text"]) for p in TRUTH["pages"] if p["kind"] == "scan")}
    title_at = n.find(norm(TRUTH["title"]))
    head = n[:title_at] if title_at > 0 else n[:600]
    has_summary = (title_at == -1 or title_at > 120) and ("summary" in head or len(head.split()) >= 20)
    return (all(facts.values()) and has_summary, {
        "markdown": os.path.relpath(md, run_dir), "chars": len(text), "facts_found": sum(facts.values()),
        "facts_total": len(facts), "scan_facts_found": sum(scan_facts.values()), "scan_facts_total": len(scan_facts),
        "summary_at_top": has_summary, "missing": [f for f, ok in facts.items() if not ok]})


def grade_compose(summary, run_dir):
    """Fresh session: the weather and email tools must be reused (nothing built), and the mail must carry
    tomorrow's real forecast."""
    msgs = mailpit_messages()
    to_anna = [m for m in msgs if "anna@example.local" in m["to"]]
    day, tmax, tmin = forecast_tomorrow()
    body = " ".join(m["subject"] + " " + m["text"] for m in to_anna)
    nums = [float(x) for x in re.findall(r"(-?\d+(?:\.\d+)?)\s*°?\s*C\b", body)]
    match = [n for n in nums if abs(n - tmax) <= 2.5 or abs(n - tmin) <= 2.5]
    created = summary.get("created") or []
    return (bool(to_anna) and bool(match) and not created and summary["status"] == "success", {
        "to_anna": len(to_anna), "reference": {"date": day, "max": tmax, "min": tmin}, "temps_in_mail": nums,
        "temps_matching": match, "tools_created": created, "repairs": summary.get("repairs")})


GRADERS = {"email": grade_email, "weather": grade_weather, "pdf": grade_pdf, "compose": grade_compose}
