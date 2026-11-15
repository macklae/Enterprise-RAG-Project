"""Run the proposal's test questions against the running API and print the scores.

    python -m scripts.evaluate                      # http://localhost:8000
    python -m scripts.evaluate http://localhost:8000

Answer accuracy is a text check on expected phrases. "Source hit" means the expected document
and section appear among the sources the answer cited. Use it to tune MIN_DENSE_SCORE,
CHUNK_MAX_TOKENS, the reranker switch and CACHE_SEMANTIC_DISTANCE. Read the answers too.
"""
from __future__ import annotations

import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.request
import http.cookiejar

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"

SESSIONS = {}


def authenticated_session(role):
    if role in SESSIONS:
        return SESSIONS[role]

    username = {
        "employee": "demo_employee",
        "line_manager": "demo_manager",
        "hr_ld": "demo_hr",
        "sre_ops": "demo_sre",
    }[role]

    password = getpass.getpass(f"Password for {username}: ")

    cookies = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookies)
    )

    request = urllib.request.Request(
        BASE + "/api/auth/login",
        data=json.dumps({
            "username": username,
            "password": password,
        }).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with opener.open(request, timeout=30) as response:
        login_data = json.load(response)

    SESSIONS[role] = (opener, login_data["csrf_token"])
    return SESSIONS[role]

from pathlib import Path

DATASET_PATH = (
    Path(__file__).resolve().parent.parent
    / "tests"
    / "golden_dataset.json"
)

FIELDS = (
    "name",
    "role",
    "user",
    "question",
    "expect_phrases",
    "expect_doc",
    "expect_section",
    "expect_found",
)

with DATASET_PATH.open(encoding="utf-8") as f:
    CASES = [
        tuple(item[field] for field in FIELDS)
        for item in json.load(f)
    ]


def post(path: str, body: dict, role: str) -> dict:
    opener, csrf_token = authenticated_session(role)

    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "X-CSRF-Token": csrf_token,
        },
        method="POST",
    )

    try:
        with opener.open(req, timeout=120) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(
            f"API request failed ({error.code}): "
            f"{error.read().decode()}"
        ) from error


def ask(role, user, q):
    t = time.perf_counter()

    result = post(
        "/api/chat",
        {"question": q},
        role,
    )

    result["_ms"] = (time.perf_counter() - t) * 1000
    return result


def main() -> None:
    rows, ok_answer, ok_source, ok_found, n_found, n_nf = [], 0, 0, 0, 0, 0
    for name, role, user, q, phrases, doc, sec, expect_found in CASES:
        r = ask(role, user, q)
        found = bool(r.get("found"))
        
        
        ans = r.get("answer", "")
        a_ok = all(p.lower() in ans.lower() for p in phrases) if expect_found else not found
        s_ok = True
        if expect_found and doc:
            s_ok = any(doc.lower() in (s.get("doc_id") or "").lower() and sec.lower() in (s.get("section") or "").lower() for s in r.get("sources", []))
        f_ok = found == expect_found
        rows.append((name, a_ok, s_ok, f_ok, r["_ms"], r.get("cache", {}).get("status")))
        ok_answer += a_ok
        ok_source += s_ok
        ok_found += f_ok
        print(f"{'PASS' if (a_ok and s_ok and f_ok) else 'FAIL'}  {name:26} answer={a_ok!s:5} source={s_ok!s:5} found={f_ok!s:5} {r['_ms']:7.0f} ms")

    # line manager scope: nobody outside the team appears
    r = ask("line_manager", "Vikram Singh", "What is the training status of Rohan Das and Pooja Shah?")
    leaked = [s for s in r.get("sources", []) if "Risk Engineering" in s.get("text", s.get("excerpt", "")) or "Customer Support" in s.get("excerpt", "")]
    print(f"{'PASS' if not leaked else 'FAIL'}  line manager sees only own team ({len(leaked)} leaked rows)")

    # cache behaviour
    q = "How soon must a P1 incident be acknowledged?"
    ask("employee", None, q)
    t1 = ask("employee", None, q)
    p2 = ask("employee", None, "How soon must a P2 incident be acknowledged?")
    print(f"{'PASS' if t1['cache']['status'] in ('exact', 'semantic') else 'FAIL'}  repeat question served from cache ({t1['_ms']:.0f} ms)")
    p2_answer = p2.get("answer", "").lower()

    p2_ok = (
        p2.get("found") is True
        and "15 minutes" in p2_answer
        and any(
            s.get("doc_id") == "SOP-OPS-001"
            for s in p2.get("sources", [])
        )
    )

    print(
        f"{'PASS' if p2_ok else 'FAIL'}  "
        "P2 answer remains correct after P1 cache test"
    )

    n = len(CASES)
    print(f"\nAnswer accuracy {ok_answer}/{n}   Source hit {ok_source}/{n}   Found/Not found correct {ok_found}/{n}")
    json.dump({"rows": rows}, open("data/eval_report.json", "w"), indent=2)
    print("Saved data/eval_report.json")


if __name__ == "__main__":
    main()
