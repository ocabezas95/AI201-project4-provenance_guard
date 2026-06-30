"""Provenance Guard — Flask application (Milestone 3 boilerplate).

Scaffolds persistence and the submission intake path:
  - SQLite (`provenance.db`) with a single `submissions` table.
  - `POST /submit`  — validation guards + atomic write + contract-shaped response.
  - `GET  /log`     — read-only monitoring of the last few records.

Real detection signals / scoring (Milestone 4) and the appeal/resolve
workflow (Milestone 5) are intentionally stubbed and marked below.
"""

import logging
import os
import re
import sqlite3
import statistics
import uuid
from datetime import datetime, timezone

import groq
from flask import Flask, g, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# --- Constants (tunable, kept in one place per spec) -------------------------
DB_PATH = "provenance.db"
MIN_WORDS = 40          # minimum-length guard (planning.md §5.3)
LOG_LIMIT = 10          # records returned by GET /log
GROQ_MODEL = "llama-3.3-70b-versatile"  # Signal 1: semantic pacing analysis

# Scoring weights and thresholds (planning.md §2 — tunable in one place).
GROQ_WEIGHT = 0.6           # semantic judgment carries the heavier weight
STYLO_WEIGHT = 0.4          # deterministic counterweight, works when degraded
DISAGREEMENT_DELTA = 0.5    # |groq - stylo| above this forces UNCERTAIN
HUMAN_THRESHOLD = 0.40      # final_score < this  -> likely human
AI_THRESHOLD = 0.70         # final_score > this  -> likely AI

# Stylometric tunables.
MATTR_WINDOW = 50           # moving-average TTR window size (tokens)
SENTENCE_CV_SCALE = 0.75    # sentence-length CV that maps to "fully human" (0.0)

# Rate limiting (planning.md §Milestone 5 — defensible /submit ceiling).
SUBMIT_RATE_LIMITS = "10 per minute;100 per day"

logger = logging.getLogger(__name__)

app = Flask(__name__)
#app.json.sort_keys = False

# Track callers by remote IP; in-memory store per modern Flask-Limiter API.
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    storage_uri="memory://",
)


# --- Database layer ----------------------------------------------------------
def get_db():
    """Return a per-request SQLite connection cached on Flask's `g`."""
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row  # dict-like row access
    return g.db


@app.teardown_appcontext
def close_db(exc):
    """Close the request-scoped connection at the end of the context."""
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create the `submissions` table if it does not already exist.

    `degraded` is stored as INTEGER (0/1) since SQLite has no native bool.
    """
    db = get_db()
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS submissions (
            content_id        TEXT PRIMARY KEY,
            text              TEXT NOT NULL,
            final_score       REAL,
            label             TEXT,
            classification    TEXT,
            groq_score        REAL,
            stylometric_score REAL,
            status            TEXT,
            creator_reasoning TEXT,
            degraded          INTEGER,
            created_at        TEXT,
            resolved_at       TEXT
        )
        """
    )
    # Migrate pre-existing databases that predate the resolved_at column.
    columns = {row["name"] for row in db.execute("PRAGMA table_info(submissions)")}
    if "resolved_at" not in columns:
        db.execute("ALTER TABLE submissions ADD COLUMN resolved_at TEXT")
    db.commit()


def insert_submission(record):
    """Persist one submission as a single atomic transaction.

    The `with` block commits on success and rolls back on exception, so the
    audit log can never be left half-written (planning.md §Concurrency).
    """
    db = get_db()
    with db:
        db.execute(
            """
            INSERT INTO submissions (
                content_id, text, final_score, label, classification,
                groq_score, stylometric_score, status, creator_reasoning,
                degraded, created_at
            ) VALUES (
                :content_id, :text, :final_score, :label, :classification,
                :groq_score, :stylometric_score, :status, :creator_reasoning,
                :degraded, :created_at
            )
            """,
            record,
        )


def get_submission(content_id):
    """Fetch a single submission row by id, or None if it does not exist."""
    return (
        get_db()
        .execute("SELECT * FROM submissions WHERE content_id = ?", (content_id,))
        .fetchone()
    )


def update_submission(content_id, fields):
    """Apply a partial update to one submission as a single atomic transaction.

    `fields` maps column names to new values; the `with` block commits on
    success and rolls back on exception, matching `insert_submission`.
    """
    assignments = ", ".join(f"{column} = :{column}" for column in fields)
    db = get_db()
    with db:
        db.execute(
            f"UPDATE submissions SET {assignments} WHERE content_id = :content_id",
            {**fields, "content_id": content_id},
        )


# --- Helpers -----------------------------------------------------------------
def error_response(message, code, http_status):
    """Standard error shape per planning.md (all endpoints)."""
    return jsonify({"error": message, "code": code}), http_status


def validate_submission(payload):
    """Validate a /submit payload.

    Returns (text, None) on success, or (None, (message, code, status))
    describing the failure.
    """
    if not isinstance(payload, dict):
        return None, ("Request body must be a JSON object.", "bad_request", 400)

    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return None, ("Field 'text' is required and must be a string.", "bad_request", 400)

    if len(text.split()) < MIN_WORDS:
        return None, (
            f"Text must contain at least {MIN_WORDS} words to be analyzed.",
            "insufficient_text",
            400,
        )

    return text, None


# Terminal outcomes a reviewer may assign via /resolve.
_RESOLUTION_DECISIONS = ("upheld", "overturned")


def validate_appeal(payload):
    """Validate an /appeal payload.

    Returns ((content_id, creator_reasoning), None) on success, or
    (None, (message, code, status)) describing the failure.
    """
    if not isinstance(payload, dict):
        return None, ("Request body must be a JSON object.", "bad_request", 400)

    content_id = payload.get("content_id")
    if not isinstance(content_id, str) or not content_id.strip():
        return None, ("Field 'content_id' is required and must be a string.", "bad_request", 400)

    creator_reasoning = payload.get("creator_reasoning")
    if not isinstance(creator_reasoning, str) or not creator_reasoning.strip():
        return None, (
            "Field 'creator_reasoning' is required and must be a string.",
            "bad_request",
            400,
        )

    return (content_id, creator_reasoning), None


def validate_resolve(payload):
    """Validate a /resolve payload.

    Returns ((content_id, decision), None) on success, or
    (None, (message, code, status)) describing the failure.
    """
    if not isinstance(payload, dict):
        return None, ("Request body must be a JSON object.", "bad_request", 400)

    content_id = payload.get("content_id")
    if not isinstance(content_id, str) or not content_id.strip():
        return None, ("Field 'content_id' is required and must be a string.", "bad_request", 400)

    decision = payload.get("decision")
    if decision not in _RESOLUTION_DECISIONS:
        return None, (
            "Field 'decision' must be one of 'upheld' or 'overturned'.",
            "bad_request",
            400,
        )

    return (content_id, decision), None


# --- Scoring (Milestone 4 seam) ----------------------------------------------
_PACING_SYSTEM_PROMPT = (
    "You are a forensic text-pacing classifier. Analyze the semantic pacing of "
    "the user's text — the rhythm, burstiness, and variation of how ideas unfold "
    "— and judge whether it was written by a human or generated by an AI.\n"
    "Respond with ONLY a single floating-point number between 0.0 and 1.0, where "
    "0.0 means confidently human and 1.0 means confidently AI. Output nothing "
    "else: no explanation, no labels, no markdown, no units, no surrounding text."
)


def analyze_semantic_pacing(text):
    """Signal 1: score a text's semantic pacing as human-vs-AI via Groq.

    Calls the Llama 3.3 70B model at temperature=0 for deterministic parsing,
    instructing it to return only a bare float in [0.0, 1.0] (0.0 = confident
    human, 1.0 = confident AI).

    Returns:
        (parsed_score: float, degraded: bool)
        - On success: (score clamped to [0.0, 1.0], False).
        - On any Groq error or unexpected failure: (0.0, True). A degraded
          result signals upstream callers to fall back / down-weight Signal 1.
    """
    try:
        client = groq.Groq(api_key=os.environ.get("GROQ_API_KEY"))
        completion = client.chat.completions.create(
            model=GROQ_MODEL,
            temperature=0,
            messages=[
                {"role": "system", "content": _PACING_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
        )
        content = completion.choices[0].message.content
        if content is None:
            raise ValueError("Groq returned an empty message content.")
        score = float(content.strip())
        # Clamp defensively: the model can occasionally drift outside [0, 1].
        score = max(0.0, min(1.0, score))
        return score, False
    except groq.GroqError as exc:
        logger.error("Groq API call failed in analyze_semantic_pacing: %s", exc)
        return 0.0, True
    except Exception as exc:  # noqa: BLE001 — degrade gracefully on any failure
        logger.error(
            "Unexpected failure in analyze_semantic_pacing: %s", exc, exc_info=True
        )
        return 0.0, True


# --- Signal 2: native stylometrics ------------------------------------------
def calculate_mattr(text, window_size=MATTR_WINDOW):
    """Moving-Average Type-Token Ratio in [0.0, 1.0] (lexical diversity).

    Tokenizes `text` into lowercased alphanumeric word runs, slides a window of
    `window_size` tokens across them, and averages the per-window unique-token
    ratio. Unlike raw TTR, this is stable across text length.

    Returns:
        float in [0.0, 1.0] — *higher means more diverse* vocabulary. Returns
        0.0 for empty input. When there are fewer tokens than `window_size`,
        falls back to a single whole-text type-token ratio.
    """
    tokens = re.findall(r"[a-z0-9']+", text.lower())
    if not tokens:
        return 0.0

    if len(tokens) < window_size:
        return len(set(tokens)) / len(tokens)

    ratios = [
        len(set(tokens[i : i + window_size])) / window_size
        for i in range(len(tokens) - window_size + 1)
    ]
    return statistics.fmean(ratios)


def calculate_sentence_cv(text):
    """Sentence-length uniformity score in [0.0 organic/human .. 1.0 uniform/AI].

    Splits `text` on terminal punctuation (`.`, `!`, `?`), counts words per
    sentence, and computes the coefficient of variation (std / mean) of those
    counts. CV is scale-invariant, so the score is comparable whether a
    submission runs short or long. The CV is then inverted and clamped against
    `SENTENCE_CV_SCALE` (the CV that reads as "fully human"):

        - high CV (bursty, human cadence)  -> approaches 0.0
        - low  CV (uniform, AI cadence)    -> approaches 1.0

    Returns:
        float in [0.0, 1.0]. Returns a neutral 0.5 when there is too little
        structure (fewer than two sentences, or a degenerate zero mean).
    """
    parts = re.split(r"[.!?]+", text)
    lengths = [len(s.split()) for s in (p.strip() for p in parts) if s.strip()]
    if len(lengths) < 2 or statistics.fmean(lengths) == 0:
        return 0.5

    cv = statistics.pstdev(lengths) / statistics.fmean(lengths)
    return max(0.0, min(1.0, 1.0 - cv / SENTENCE_CV_SCALE))


def analyze_stylometrics(text):
    """Signal 2: combined stylometric score in [0.0 human .. 1.0 AI].

    Equal blend of sentence-length uniformity and lexical uniformity. Pure
    Python and deterministic, so it remains available when Signal 1 degrades.

    `calculate_mattr` reports diversity (high = human), so it is inverted to a
    uniformity score before blending; `calculate_sentence_cv` is already a
    uniformity score, so both terms point the same way (1.0 = uniform/AI).
    """
    sentence_score = calculate_sentence_cv(text)
    lexical_score = 1.0 - calculate_mattr(text)
    return 0.5 * sentence_score + 0.5 * lexical_score


# --- Confidence scoring engine -----------------------------------------------
_LABELS = {
    "human": (
        "Verified Original: Our analysis indicates this content matches "
        "human writing patterns."
    ),
    "uncertain": (
        "Mixed Signatures: This text contains a blend of structural patterns "
        "that make its origin ambiguous."
    ),
    "ai": (
        "AI-Generated Pattern: This text closely aligns with algorithmic "
        "generation characteristics."
    ),
}


def _classify(final_score, disagreement):
    """Resolve a final score (+ disagreement flag) to a classification key."""
    if disagreement:
        return "uncertain"
    if final_score < HUMAN_THRESHOLD:
        return "human"
    if final_score > AI_THRESHOLD:
        return "ai"
    return "uncertain"


def score_text(text):
    """Run both signals, combine them, and produce a transparency label.

    Flow (planning.md §2): Signal 1 (Groq) + Signal 2 (stylometrics) ->
    weighted average (or Signal-2-only when degraded) -> disagreement override
    -> classification band -> transparency label.
    """
    groq_score, degraded = analyze_semantic_pacing(text)
    stylometric_score = analyze_stylometrics(text)

    if degraded:
        # Signal 1 unavailable: fall back to Signal 2 only. No disagreement
        # check, since there is no trustworthy Groq score to compare against.
        final_score = stylometric_score
        disagreement = False
    else:
        final_score = GROQ_WEIGHT * groq_score + STYLO_WEIGHT * stylometric_score
        disagreement = abs(groq_score - stylometric_score) > DISAGREEMENT_DELTA

    classification = _classify(final_score, disagreement)

    return {
        "final_score": round(final_score, 2),
        "classification": classification,
        "label": _LABELS[classification],
        "groq_score": round(groq_score, 2),
        "stylometric_score": round(stylometric_score, 2),
        "degraded": degraded,
    }


# --- Endpoints ---------------------------------------------------------------
@app.route("/submit", methods=["POST"])
@limiter.limit(SUBMIT_RATE_LIMITS)
def submit():
    """Intake a text submission: validate, score (placeholder), persist."""
    payload = request.get_json(silent=True)

    text, err = validate_submission(payload)
    if err is not None:
        return error_response(*err)

    scored = score_text(text)
    record = {
        "content_id": str(uuid.uuid4()),
        "text": text,
        "final_score": scored["final_score"],
        "label": scored["label"],
        "classification": scored["classification"],
        "groq_score": scored["groq_score"],
        "stylometric_score": scored["stylometric_score"],
        "status": "classified",
        "creator_reasoning": None,
        "degraded": int(scored["degraded"]),
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    try:
        insert_submission(record)
    except sqlite3.Error:
        return error_response("Failed to persist submission.", "server_error", 500)

    return jsonify(
        {
            "content_id": record["content_id"],
            "final_score": record["final_score"],
            "label": record["label"],
            "classification": record["classification"],
            "signals": {
                "groq": record["groq_score"],
                "stylometric": record["stylometric_score"],
            },
            "status": record["status"],
            "degraded": bool(record["degraded"]),
            "created_at": record["created_at"],
        }
    ), 200


@app.route("/log", methods=["GET"])
def log():
    """Read-only monitoring helper: the most recent submissions."""
    try:
        rows = (
            get_db()
            .execute(
                "SELECT * FROM submissions ORDER BY created_at DESC LIMIT ?",
                (LOG_LIMIT,),
            )
            .fetchall()
        )
    except sqlite3.Error:
        return error_response("Failed to read submissions.", "server_error", 500)

    return jsonify([dict(row) for row in rows]), 200


@app.route("/appeal", methods=["POST"])
def appeal():
    """Open an appeal: validate the id, move 'classified' -> 'under_review'."""
    payload = request.get_json(silent=True)

    parsed, err = validate_appeal(payload)
    if err is not None:
        return error_response(*err)
    content_id, creator_reasoning = parsed

    try:
        if get_submission(content_id) is None:
            return error_response("No submission found for that content_id.", "not_found", 404)
        update_submission(
            content_id,
            {"status": "under_review", "creator_reasoning": creator_reasoning},
        )
    except sqlite3.Error:
        return error_response("Failed to update submission.", "server_error", 500)

    return jsonify({"content_id": content_id, "status": "under_review"}), 200


@app.route("/resolve", methods=["POST"])
def resolve():
    """Close an appeal: 'under_review' -> terminal 'upheld' / 'overturned'."""
    payload = request.get_json(silent=True)

    parsed, err = validate_resolve(payload)
    if err is not None:
        return error_response(*err)
    content_id, decision = parsed

    try:
        row = get_submission(content_id)
        if row is None:
            return error_response("No submission found for that content_id.", "not_found", 404)
        # Only an appeal under review may transition to a terminal state
        # (classified -> under_review -> {upheld | overturned}).
        if row["status"] != "under_review":
            return error_response(
                "Submission must be under review before it can be resolved.",
                "invalid_transition",
                409,
            )
        update_submission(
            content_id,
            {
                "status": decision,
                "resolved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
    except sqlite3.Error:
        return error_response("Failed to update submission.", "server_error", 500)

    return jsonify({"content_id": content_id, "status": decision}), 200


@app.errorhandler(429)
def handle_rate_limit(exc):
    """Return JSON (not Flask-Limiter's HTML) when a rate limit is exceeded."""
    return error_response("Rate limit exceeded. Slow down.", "rate_limited", 429)


@app.errorhandler(500)
def handle_internal_error(exc):
    """Return JSON (not an HTML stack trace) for unhandled server errors."""
    return error_response("Internal server error.", "server_error", 500)


# --- Startup -----------------------------------------------------------------
# Ensure the schema exists before the first request is served.
with app.app_context():
    init_db()


if __name__ == "__main__":
    app.run(debug=True)
