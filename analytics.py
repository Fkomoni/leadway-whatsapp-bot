"""
Analytics module — tracks bot sessions, per-request timing, and member sentiment.
All data is stored in analytics.db (SQLite, auto-created on first import).
"""
import re
import time
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

DB_PATH = Path(__file__).parent / "analytics.db"

# ─── Schema ──────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS conversations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT    NOT NULL UNIQUE,
    agent_name      TEXT    NOT NULL DEFAULT 'Favour',
    member_phone    TEXT,
    member_id       TEXT,
    member_name     TEXT,
    started_at      TEXT    NOT NULL,
    last_active_at  TEXT,
    message_count   INTEGER NOT NULL DEFAULT 0,
    escalated       INTEGER NOT NULL DEFAULT 0,
    resolution      TEXT    NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS responses (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       TEXT    NOT NULL,
    request_text     TEXT,
    response_text    TEXT,
    requested_at     TEXT    NOT NULL,
    responded_at     TEXT    NOT NULL,
    response_time_ms INTEGER,
    tools_used       TEXT,
    sentiment_score  REAL,
    sentiment_label  TEXT
);

CREATE INDEX IF NOT EXISTS idx_conv_session   ON conversations(session_id);
CREATE INDEX IF NOT EXISTS idx_conv_started   ON conversations(started_at);
CREATE INDEX IF NOT EXISTS idx_resp_session   ON responses(session_id);
CREATE INDEX IF NOT EXISTS idx_resp_req_at    ON responses(requested_at);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(_DDL)


# ─── Sentiment ────────────────────────────────────────────────────────────────

_POS = re.compile(
    r"\b(thank|thanks|great|good|excellent|perfect|helpful|wonderful|brilliant|"
    r"happy|pleased|appreciate|amazing|superb|fantastic|love|awesome|nice|fast)\b",
    re.IGNORECASE,
)
_NEG = re.compile(
    r"\b(bad|terrible|awful|disappoint|unhappy|frustrat|angry|useless|rubbish|"
    r"nonsense|slow|wrong|horrible|disgrace|poor|broken|not working|doesn.t work|"
    r"never|waste|pathetic|annoying|ridiculous)\b",
    re.IGNORECASE,
)


def analyse_sentiment(text: str) -> Tuple[float, str]:
    """Return (score, label).  Score is –1 (very negative) … +1 (very positive)."""
    pos = len(_POS.findall(text))
    neg = len(_NEG.findall(text))
    if pos == 0 and neg == 0:
        return 0.0, "neutral"
    total = pos + neg
    score = round((pos - neg) / total, 2)
    if score > 0.2:
        return score, "positive"
    if score < -0.2:
        return score, "negative"
    return score, "neutral"


# ─── Request-type categorisation ─────────────────────────────────────────────

_TYPE_MAP = {
    "lookup_member_for_id":              "Member ID Lookup",
    "lookup_member_by_email":            "Member ID Lookup",
    "check_benefits":                    "Benefit Check",
    "get_dependants":                    "Dependants Enquiry",
    "check_annual_screening_eligibility":"Annual Screening",
    "get_screening_providers":           "Annual Screening",
    "book_annual_screening":             "Annual Screening",
    "get_network_providers":             "Provider Search",
}


def categorise_tools(tools_csv: Optional[str]) -> str:
    if not tools_csv:
        return "General Enquiry"
    for tool in tools_csv.split(","):
        cat = _TYPE_MAP.get(tool.strip())
        if cat:
            return cat
    return "General Enquiry"


# ─── Write helpers ────────────────────────────────────────────────────────────

def start_session(
    session_id: str,
    agent_name: str = "Favour",
    member_phone: Optional[str] = None,
) -> None:
    now = datetime.utcnow().isoformat()
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO conversations
                (session_id, agent_name, member_phone, started_at, last_active_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, agent_name, member_phone, now, now),
        )


def update_session(
    session_id: str,
    member_id: Optional[str] = None,
    member_name: Optional[str] = None,
    escalated: bool = False,
    resolution: Optional[str] = None,
) -> None:
    parts, vals = [], []
    if member_id is not None:
        parts.append("member_id = ?")
        vals.append(member_id)
    if member_name is not None:
        parts.append("member_name = ?")
        vals.append(member_name)
    if escalated:
        parts.append("escalated = 1")
    if resolution is not None:
        parts.append("resolution = ?")
        vals.append(resolution)
    parts.append("last_active_at = ?")
    vals.append(datetime.utcnow().isoformat())
    vals.append(session_id)
    with _connect() as conn:
        conn.execute(
            f"UPDATE conversations SET {', '.join(parts)} WHERE session_id = ?",
            vals,
        )


def log_response(
    session_id: str,
    request_text: str,
    response_text: str,
    requested_at: float,
    tools_used: Optional[List[str]] = None,
) -> None:
    responded_at = time.time()
    rt_ms = int((responded_at - requested_at) * 1000)
    score, label = analyse_sentiment(request_text)
    tools_csv = ",".join(tools_used) if tools_used else None
    req_iso = datetime.utcfromtimestamp(requested_at).isoformat()
    res_iso = datetime.utcfromtimestamp(responded_at).isoformat()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO responses
                (session_id, request_text, response_text,
                 requested_at, responded_at, response_time_ms,
                 tools_used, sentiment_score, sentiment_label)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id, request_text[:600], response_text[:600],
                req_iso, res_iso, rt_ms,
                tools_csv, score, label,
            ),
        )
        conn.execute(
            """
            UPDATE conversations
               SET message_count  = message_count + 1,
                   last_active_at = ?
             WHERE session_id = ?
            """,
            (datetime.utcnow().isoformat(), session_id),
        )


# ─── Read helpers (used by dashboard) ────────────────────────────────────────

def get_stats(start_iso: str, end_iso: str) -> dict:
    """Aggregate stats for the dashboard over [start_iso, end_iso]."""
    with _connect() as conn:
        # Overall KPIs
        kpi = conn.execute(
            """
            SELECT
                COUNT(DISTINCT r.session_id)               AS total_sessions,
                COUNT(*)                                    AS total_requests,
                ROUND(AVG(r.response_time_ms) / 1000.0, 2) AS avg_rt_s,
                ROUND(AVG(r.sentiment_score), 2)            AS avg_sentiment,
                SUM(CASE WHEN r.sentiment_label = 'positive' THEN 1 ELSE 0 END) AS pos_count,
                SUM(CASE WHEN r.sentiment_label = 'neutral'  THEN 1 ELSE 0 END) AS neu_count,
                SUM(CASE WHEN r.sentiment_label = 'negative' THEN 1 ELSE 0 END) AS neg_count
            FROM responses r
            WHERE r.requested_at BETWEEN ? AND ?
            """,
            (start_iso, end_iso),
        ).fetchone()

        # Per-agent breakdown
        agents = conn.execute(
            """
            SELECT
                c.agent_name,
                COUNT(DISTINCT r.session_id)                AS sessions,
                COUNT(r.id)                                  AS requests,
                ROUND(AVG(r.response_time_ms) / 1000.0, 2)  AS avg_rt_s,
                ROUND(AVG(r.sentiment_score), 2)             AS avg_sentiment,
                SUM(c.escalated)                             AS escalations
            FROM responses r
            JOIN conversations c ON c.session_id = r.session_id
            WHERE r.requested_at BETWEEN ? AND ?
            GROUP BY c.agent_name
            ORDER BY requests DESC
            """,
            (start_iso, end_iso),
        ).fetchall()

        # Daily volume (for line chart)
        daily = conn.execute(
            """
            SELECT
                SUBSTR(requested_at, 1, 10) AS day,
                COUNT(*)                    AS requests
            FROM responses
            WHERE requested_at BETWEEN ? AND ?
            GROUP BY day
            ORDER BY day
            """,
            (start_iso, end_iso),
        ).fetchall()

        # Request type breakdown
        req_types = conn.execute(
            """
            SELECT tools_used, COUNT(*) AS cnt
            FROM responses
            WHERE requested_at BETWEEN ? AND ?
            GROUP BY tools_used
            """,
            (start_iso, end_iso),
        ).fetchall()

        # Peak hours
        hours = conn.execute(
            """
            SELECT
                CAST(SUBSTR(requested_at, 12, 2) AS INTEGER) AS hour,
                COUNT(*) AS cnt
            FROM responses
            WHERE requested_at BETWEEN ? AND ?
            GROUP BY hour
            ORDER BY hour
            """,
            (start_iso, end_iso),
        ).fetchall()

        # Individual request log (last 200)
        log = conn.execute(
            """
            SELECT
                r.requested_at, r.responded_at,
                c.agent_name, c.member_name, c.member_phone,
                r.request_text, r.response_time_ms,
                r.sentiment_label, r.sentiment_score,
                r.tools_used
            FROM responses r
            JOIN conversations c ON c.session_id = r.session_id
            WHERE r.requested_at BETWEEN ? AND ?
            ORDER BY r.requested_at DESC
            LIMIT 200
            """,
            (start_iso, end_iso),
        ).fetchall()

    # Aggregate request types
    type_counts: dict = {}
    for row in req_types:
        cat = categorise_tools(row["tools_used"])
        type_counts[cat] = type_counts.get(cat, 0) + row["cnt"]

    return {
        "kpi": dict(kpi),
        "agents": [dict(a) for a in agents],
        "daily": [dict(d) for d in daily],
        "type_counts": type_counts,
        "hours": {row["hour"]: row["cnt"] for row in hours},
        "log": [dict(r) for r in log],
    }


# Initialise on import
init_db()
