#!/usr/bin/env python3
"""
RASAT Weekly Literature Digest
================================
Queries arXiv for papers published in the last 7 days across the four RASAT
research pillars, scores them by keyword density, and sends an HTML email digest
to the PI via Gmail SMTP.

Environment variables required (set as GitHub Secrets):
  GMAIL_USER          — sender Gmail address (e.g. yourlab@gmail.com)
  GMAIL_APP_PASSWORD  — Gmail App Password (not the account password)
  DIGEST_RECIPIENT    — recipient email (defaults to GMAIL_USER if unset)
"""

import os
import smtplib
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ── RASAT research pillars and their keyword sets ─────────────────────────────
# Each keyword match adds 1 to the pillar score; capped at 5 per pillar.
# Total max = 20.  Thresholds: ≥4 include · ≥8 high relevance · ≥16 must-read.

PILLARS: dict[str, list[str]] = {
    "Orbital Mechanics": [
        "orbit propagation", "trajectory design", "astrodynamics", "low-thrust",
        "formation flying", "relative motion", "station keeping", "perturbation",
        "orbital mechanics", "transfer orbit", "rendezvous", "cislunar",
        "asteroid", "interplanetary", "ephemeris", "two-body", "three-body",
        "gravitational", "debris", "conjunction",
    ],
    "ADCS": [
        "attitude control", "attitude determination", "attitude estimation",
        "reaction wheel", "control moment gyro", "CMG", "quaternion",
        "slew maneuver", "pointing", "magnetorquer", "star tracker",
        "MEKF", "multiplicative extended kalman", "spacecraft attitude",
        "angular velocity", "torque", "gyroscope", "magnetometer",
    ],
    "Numerical Optimization": [
        "convex optimization", "sequential convex", "SCP", "lossless convexification",
        "trajectory optimization", "direct collocation", "pseudospectral",
        "second-order cone", "SOCP", "semidefinite", "SDP", "nonlinear programming",
        "real-time optimization", "embedded optimization", "fuel optimal",
        "powered descent", "soft landing", "successive convex",
    ],
    "Control Theory": [
        "model predictive control", "MPC", "LQR", "LQG", "H-infinity",
        "robust control", "adaptive control", "sliding mode", "Lyapunov",
        "stability proof", "nonlinear control", "optimal control",
        "Hamilton-Jacobi", "HJB", "learning-based control", "backstepping",
        "contraction", "passivity", "input-to-state stability", "ISS",
    ],
}

# arXiv subject categories to search within
ARXIV_CATS = (
    "cat:cs.SY OR cat:math.OC OR cat:eess.SY OR cat:cs.RO "
    "OR cat:astro-ph.EP OR cat:astro-ph.IM OR cat:math.NA"
)

# Query strings — run each independently; results are deduplicated by arXiv ID
QUERIES: list[str] = [
    # Spacecraft & mission
    "spacecraft control",
    "satellite guidance navigation control",
    "autonomous spacecraft",
    "space mission optimization",

    # Trajectory & orbital
    "trajectory optimization",
    "orbital mechanics",
    "orbit propagation",
    "low-thrust transfer",
    "powered descent landing",
    "formation flying",
    "relative motion spacecraft",
    "cislunar trajectory",
    "interplanetary trajectory",

    # Attitude
    "attitude control",
    "attitude estimation",
    "attitude determination",
    "quaternion control",
    "reaction wheel",
    "control moment gyro",

    # Optimization methods
    "sequential convex programming",
    "convex optimization control",
    "trajectory optimization direct",
    "pseudospectral method optimal control",
    "real-time optimization embedded",

    # Control theory
    "Lyapunov stability nonlinear",
    "model predictive control",
    "robust control spacecraft",
    "adaptive control nonlinear",
    "optimal control LQR",
    "sliding mode control",
]

ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom"}
MAX_RESULTS_PER_QUERY = 50
SEARCH_DAYS = 7


# ── arXiv API ─────────────────────────────────────────────────────────────────

def query_arxiv(search_term: str) -> list[dict]:
    """Return papers from the last SEARCH_DAYS days matching search_term."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=SEARCH_DAYS)
    query = f"({search_term}) AND ({ARXIV_CATS})"
    params = urllib.parse.urlencode({
        "search_query": query,
        "start": 0,
        "max_results": MAX_RESULTS_PER_QUERY,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    })
    url = f"http://export.arxiv.org/api/query?{params}"
    papers: list[dict] = []
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            root = ET.fromstring(resp.read())
        for entry in root.findall("atom:entry", ARXIV_NS):
            published_str = entry.findtext("atom:published", "", ARXIV_NS)
            try:
                published = datetime.fromisoformat(published_str.replace("Z", "+00:00"))
            except ValueError:
                continue
            if published < cutoff:
                continue
            arxiv_id = (entry.findtext("atom:id", "", ARXIV_NS) or "").split("/abs/")[-1]
            title = (entry.findtext("atom:title", "", ARXIV_NS) or "").replace("\n", " ").strip()
            summary = (entry.findtext("atom:summary", "", ARXIV_NS) or "").replace("\n", " ").strip()
            authors = [
                a.findtext("atom:name", "", ARXIV_NS)
                for a in entry.findall("atom:author", ARXIV_NS)
            ]
            papers.append({
                "id": arxiv_id,
                "title": title,
                "authors": authors,
                "published": published.strftime("%Y-%m-%d"),
                "summary": summary,
                "url": f"https://arxiv.org/abs/{arxiv_id}",
            })
    except Exception as exc:
        print(f"  [warn] arXiv query failed for '{search_term}': {exc}")
    return papers


def collect_papers() -> list[dict]:
    """Run all queries, deduplicate by arXiv ID, score, and filter."""
    seen: dict[str, dict] = {}
    for i, query in enumerate(QUERIES):
        print(f"  Query {i + 1}/{len(QUERIES)}: {query!r}")
        for paper in query_arxiv(query):
            if paper["id"] not in seen:
                seen[paper["id"]] = paper
        time.sleep(3)  # be polite to the arXiv API

    results: list[dict] = []
    for paper in seen.values():
        scores = _score(paper)
        total = sum(scores.values())
        if total >= 3:
            paper["scores"] = scores
            paper["total"] = total
            results.append(paper)

    results.sort(key=lambda p: p["total"], reverse=True)
    return results


def _score(paper: dict) -> dict[str, int]:
    """Score a paper on each RASAT pillar via keyword matching."""
    text = (paper["title"] + " " + paper["summary"]).lower()
    return {
        pillar: min(sum(1 for kw in kws if kw.lower() in text), 5)
        for pillar, kws in PILLARS.items()
    }


# ── Email builder ─────────────────────────────────────────────────────────────

def _paper_card(p: dict) -> str:
    score_parts = " · ".join(
        f"{k.split()[0]}: {v}" for k, v in p["scores"].items()
    )
    authors = ", ".join(p["authors"][:4])
    if len(p["authors"]) > 4:
        authors += " et al."
    excerpt = p["summary"][:350] + ("…" if len(p["summary"]) > 350 else "")
    border = "#b91c1c" if p["total"] >= 16 else "#1a56db" if p["total"] >= 8 else "#6b7280"
    return f"""
<div style="margin-bottom:18px;padding:14px 16px;border-left:4px solid {border};
            background:#f9fafb;border-radius:0 4px 4px 0;">
  <a href="{p['url']}" style="font-size:15px;font-weight:600;color:{border};
     text-decoration:none;line-height:1.4;">{p['title']}</a><br>
  <span style="font-size:12px;color:#6b7280;">
    {authors} &nbsp;·&nbsp; arXiv:{p['id']} &nbsp;·&nbsp; {p['published']}
  </span><br>
  <span style="font-size:12px;color:#374151;margin-top:4px;display:block;">
    Score: {score_parts} &nbsp;=&nbsp; <strong>{p['total']}/20</strong>
  </span>
  <p style="font-size:13px;color:#374151;margin:8px 0 0;">{excerpt}</p>
</div>"""


def build_html(papers: list[dict], date_str: str) -> str:
    must_reads  = [p for p in papers if p["total"] >= 16]
    high_rel    = [p for p in papers if 8 <= p["total"] < 16]
    worth_noting = [p for p in papers if 4 <= p["total"] < 8]

    def section(title: str, color: str, items: list[dict]) -> str:
        if not items:
            return ""
        cards = "".join(_paper_card(p) for p in items)
        return (
            f'<h2 style="color:{color};margin-top:28px;margin-bottom:12px;">'
            f'{title} ({len(items)})</h2>{cards}'
        )

    body = ""
    body += section("🔴 Must-Read", "#b91c1c", must_reads)
    body += section("🔵 High Relevance", "#1e40af", high_rel)
    body += section("⚪ Worth Noting", "#4b5563", worth_noting)
    if not papers:
        body = "<p style='color:#374151;'>No papers met the relevance threshold this week.</p>"

    return f"""<!DOCTYPE html>
<html lang="en">
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
             max-width:740px;margin:0 auto;padding:24px;color:#111827;">
  <h1 style="border-bottom:2px solid #e5e7eb;padding-bottom:10px;margin-bottom:6px;">
    RASAT Weekly Literature Digest
  </h1>
  <p style="color:#6b7280;margin-top:0;font-size:13px;">Week ending {date_str}</p>
  <p style="font-size:13px;color:#374151;background:#eff6ff;
            padding:10px 14px;border-radius:4px;margin-bottom:4px;">
    <strong>{len(papers)}</strong> papers passed threshold &nbsp;·&nbsp;
    <strong>{len(must_reads)}</strong> must-reads &nbsp;·&nbsp;
    <strong>{len(high_rel)}</strong> high relevance &nbsp;·&nbsp;
    Scored on: Orbital Mechanics · ADCS · Numerical Optimization · Control Theory
    (max 5 per pillar · include ≥3 · high ≥8 · must-read ≥16)
  </p>
  {body}
  <hr style="margin-top:32px;border:none;border-top:1px solid #e5e7eb;">
  <p style="font-size:12px;color:#9ca3af;">
    To triage a paper in full, open Claude Code in your project and run
    <code>/paper-triage [arXiv ID or DOI]</code>.<br>
    Queries: {len(QUERIES)} arXiv searches across cs.SY · math.OC · eess.SY · astro-ph · math.NA.
  </p>
</body>
</html>"""


# ── Gmail sender ──────────────────────────────────────────────────────────────

def send_email(subject: str, html_body: str) -> None:
    sender    = os.environ["GMAIL_USER"].strip()
    password  = os.environ["GMAIL_APP_PASSWORD"].strip()
    recipient = os.environ.get("DIGEST_RECIPIENT", sender)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"RASAT Digest <{sender}>"
    msg["To"]      = recipient
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, password)
        server.sendmail(sender, recipient, msg.as_string())
    print(f"Digest sent → {recipient}")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"RASAT digest — week ending {today}")
    print(f"Running {len(QUERIES)} arXiv queries…")
    papers = collect_papers()
    print(f"Collected {len(papers)} papers above threshold.")
    html = build_html(papers, today)
    subject = f"RASAT Weekly Digest — {today}  ({len(papers)} papers)"
    send_email(subject, html)
