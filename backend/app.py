"""TRACE-X Flask backend for demo and uploaded transaction datasets."""

from __future__ import annotations

import csv
import io
import math
import os
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from flask import Flask, jsonify, render_template, request, session
from flask_cors import CORS
from werkzeug.exceptions import RequestEntityTooLarge

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BACKEND_DIR)
FRONTEND_DIR = os.path.join(PROJECT_ROOT, "frontend")
REQUIRED_COLUMNS = {
    "transaction_id",
    "timestamp",
    "sender_account",
    "receiver_account",
    "amount_inr",
}
OPTIONAL_COLUMNS = {"currency", "channel", "transaction_type", "is_fraud"}
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_ROWS = 10_000

app = Flask(__name__, template_folder=FRONTEND_DIR)
app.config.update(
    MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
    SECRET_KEY=os.environ.get("TRACE_X_SECRET_KEY", "trace-x-development-key"),
)
allowed_origins = [
    origin.strip()
    for origin in os.environ.get("FRONTEND_ORIGIN", "").split(",")
    if origin.strip()
]
if allowed_origins:
    CORS(app, origins=allowed_origins, supports_credentials=True)

_datasets: dict[str, dict[str, Any]] = {}


def _session_key() -> str:
    if "trace_x_session" not in session:
        session["trace_x_session"] = uuid.uuid4().hex
    return str(session["trace_x_session"])


def _parse_timestamp(value: str, row_number: int) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"row {row_number}: timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_csv(file_storage: Any) -> list[dict[str, Any]]:
    raw = file_storage.read(MAX_UPLOAD_BYTES + 1)
    if not raw:
        raise ValueError("the uploaded CSV is empty")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError("the uploaded file exceeds the 5 MB limit")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV must be UTF-8 encoded") from exc
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or not any(name and name.strip() for name in reader.fieldnames):
        raise ValueError("CSV must include a header row")
    headers = {name.strip() for name in reader.fieldnames if name}
    missing = sorted(REQUIRED_COLUMNS - headers)
    if missing:
        raise ValueError("missing required columns: " + ", ".join(missing))
    unknown = headers - REQUIRED_COLUMNS - OPTIONAL_COLUMNS
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for row_number, raw_row in enumerate(reader, start=2):
        if row_number > MAX_ROWS + 1:
            raise ValueError(f"CSV exceeds the {MAX_ROWS:,}-row limit")
        if not any((value or "").strip() for value in raw_row.values()):
            raise ValueError(f"row {row_number}: malformed empty row")
        if None in raw_row:
            raise ValueError(f"row {row_number}: malformed row has too many columns")
        cleaned = {str(key).strip(): (value or "").strip() for key, value in raw_row.items()}
        transaction_id = cleaned.get("transaction_id", "")
        if not transaction_id:
            raise ValueError(f"row {row_number}: transaction_id is required")
        if transaction_id in seen_ids:
            raise ValueError(f"row {row_number}: duplicate transaction_id '{transaction_id}'")
        seen_ids.add(transaction_id)
        sender = cleaned.get("sender_account", "")
        receiver = cleaned.get("receiver_account", "")
        if not sender or not receiver:
            raise ValueError(f"row {row_number}: sender_account and receiver_account are required")
        try:
            amount = float(cleaned.get("amount_inr", ""))
        except ValueError as exc:
            raise ValueError(f"row {row_number}: amount_inr must be numeric") from exc
        if not math.isfinite(amount) or amount < 0:
            raise ValueError(f"row {row_number}: amount_inr must be a finite non-negative number")
        timestamp = _parse_timestamp(cleaned.get("timestamp", ""), row_number)
        if not cleaned.get("timestamp", ""):
            raise ValueError(f"row {row_number}: timestamp is required")
        rows.append(
            {
                "transaction_id": transaction_id,
                "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
                "_timestamp": timestamp,
                "sender_account": sender,
                "receiver_account": receiver,
                "amount_inr": amount,
                "currency": cleaned.get("currency") or "INR",
                "channel": cleaned.get("channel") or "Unknown",
                "transaction_type": cleaned.get("transaction_type") or "Unknown",
                "is_fraud": cleaned.get("is_fraud") or None,
            }
        )
    if not rows:
        raise ValueError("the uploaded CSV contains no transaction records")
    return rows


def _risk_for(row: dict[str, Any], incoming: int, outgoing: int) -> tuple[int, list[str]]:
    factors: list[str] = []
    score = 8
    if row["amount_inr"] >= 100_000:
        score += 32
        factors.append("large amount")
    elif row["amount_inr"] >= 25_000:
        score += 18
        factors.append("above-baseline amount")
    if incoming and outgoing:
        score += 18
        factors.append("through-flow connection")
    if outgoing >= 2:
        score += 14
        factors.append("multiple outgoing links")
    if incoming >= 2:
        score += 12
        factors.append("multiple incoming links")
    return min(score, 99), factors or ["limited observable network signals"]


def _build_dataset(rows: list[dict[str, Any]], source: str, filename: str = "") -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: row["_timestamp"])
    edges: list[dict[str, str]] = []
    edge_keys: set[tuple[str, str]] = set()
    for first in ordered:
        for second in ordered:
            if first is second or first["receiver_account"] != second["sender_account"]:
                continue
            key = (first["transaction_id"], second["transaction_id"])
            if key not in edge_keys and second["_timestamp"] >= first["_timestamp"]:
                edge_keys.add(key)
                edges.append({"source": key[0], "target": key[1]})
    incoming = defaultdict(int)
    outgoing = defaultdict(int)
    for edge in edges:
        outgoing[edge["source"]] += 1
        incoming[edge["target"]] += 1
    records: list[dict[str, Any]] = []
    for row in rows:
        risk, factors = _risk_for(row, incoming[row["transaction_id"]], outgoing[row["transaction_id"]])
        record = {key: value for key, value in row.items() if not key.startswith("_")}
        record.update(
            {
                "id": row["transaction_id"],
                "risk": risk,
                "risk_factors": factors,
                "supplied_label": row["is_fraud"],
                "incoming_count": incoming[row["transaction_id"]],
                "outgoing_count": outgoing[row["transaction_id"]],
            }
        )
        records.append(record)
    nodes = [
        {
            "id": record["id"],
            "transaction_id": record["transaction_id"],
            "sender_account": record["sender_account"],
            "receiver_account": record["receiver_account"],
            "timestamp": record["timestamp"],
            "amount_inr": record["amount_inr"],
            "amount": f'₹{record["amount_inr"]:,.2f}',
            "risk": record["risk"],
            "risk_factors": record["risk_factors"],
            "supplied_label": record["supplied_label"],
            "step": index + 1,
        }
        for index, record in enumerate(records)
    ]
    return {
        "source": source,
        "filename": filename,
        "records": records,
        "nodes": nodes,
        "edges": edges,
        "model": "Rule-based risk indicator (demo)",
        "summary": {
            "transactions": len(records),
            "nodes": len(nodes),
            "edges": len(edges),
            "flagged": sum(node["risk"] >= 70 for node in nodes),
        },
    }


def _demo_dataset() -> dict[str, Any]:
    rows = []
    for index in range(24):
        account = f"DEMO-{index % 8:02d}"
        next_account = f"DEMO-{(index + 1) % 8:02d}"
        rows.append(
            {
                "transaction_id": f"TX-{1008 + index * 7}",
                "timestamp": f"2026-04-{8 + index % 18:02d}T{8 + index % 10:02d}:15:00Z",
                "_timestamp": datetime(2026, 4, 8 + index % 18, 8 + index % 10, 15, tzinfo=timezone.utc),
                "sender_account": account,
                "receiver_account": next_account,
                "amount_inr": float(5000 + index * 1800),
                "currency": "INR",
                "channel": "demo",
                "transaction_type": "transfer",
                "is_fraud": None,
            }
        )
    return _build_dataset(rows, "demo", "built-in demo dataset")


def _current_dataset() -> dict[str, Any]:
    key = _session_key()
    if key not in _datasets:
        _datasets[key] = _demo_dataset()
    return _datasets[key]


@app.errorhandler(RequestEntityTooLarge)
def handle_large_upload(_: RequestEntityTooLarge):
    return jsonify(error="uploaded file exceeds the 5 MB limit"), 413


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/datasets")
def upload_dataset():
    file_storage = request.files.get("file")
    if file_storage is None or not file_storage.filename:
        return jsonify(error="select a CSV file to upload"), 400
    if not file_storage.filename.lower().endswith(".csv"):
        return jsonify(error="unsupported format: upload a .csv file"), 415
    try:
        rows = _parse_csv(file_storage)
        dataset = _build_dataset(rows, "uploaded", os.path.basename(file_storage.filename))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    _datasets[_session_key()] = dataset
    return jsonify(dataset)


@app.get("/api/datasets/current")
def current_dataset():
    return jsonify(_current_dataset())


@app.post("/api/datasets/demo")
def reset_demo():
    _datasets[_session_key()] = _demo_dataset()
    return jsonify(_datasets[_session_key()])


@app.get("/api/datasets/current/risk")
def current_risk():
    dataset = _current_dataset()
    return jsonify(
        {
            "model": dataset["model"],
            "results": [
                {
                    "transaction_id": record["transaction_id"],
                    "demo_risk_score": record["risk"],
                    "risk_factors": record["risk_factors"],
                    "supplied_label": record["supplied_label"],
                }
                for record in dataset["records"]
            ],
        }
    )


@app.get("/api/datasets/current/nodes/<path:node_id>")
def node_details(node_id: str):
    dataset = _current_dataset()
    record = next((item for item in dataset["records"] if item["transaction_id"] == node_id), None)
    if record is None:
        return jsonify(error="transaction not found"), 404
    connected_ids = {
        edge["target"] if edge["source"] == node_id else edge["source"]
        for edge in dataset["edges"]
        if edge["source"] == node_id or edge["target"] == node_id
    }
    neighbors = [item for item in dataset["records"] if item["transaction_id"] in connected_ids]
    return jsonify({"record": record, "neighbors": neighbors})


if __name__ == "__main__":
    app.run(debug=True)
