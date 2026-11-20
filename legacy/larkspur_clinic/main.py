"""Larkspur Clinic: the front desk app of a small physiotherapy clinic.

A second, unrelated existing app: plain FastAPI over SQLite, written to show that Rehearsal
protects any Synapse built app from a config file alone. Rehearsal never edits this code.
"""
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterator

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

import db

app = FastAPI(title="Larkspur Clinic", version="1.4.0",
              description="Patients, appointments and billing for Larkspur Physiotherapy.")


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def money(cents: int) -> str:
    return f"{Decimal(cents) / 100:.2f}"


def one(conn: sqlite3.Connection, sql: str, item_id: int, kind: str) -> sqlite3.Row:
    row = conn.execute(sql, (item_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"{kind} {item_id} not found")
    return row


def invoice_detail(conn: sqlite3.Connection, invoice_id: int) -> dict:
    row = one(conn, "SELECT * FROM invoices WHERE id = ?", invoice_id, "Invoice")
    refunded = conn.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM refunds WHERE invoice_id = ?",
                            (invoice_id,)).fetchone()[0]
    return {"id": row["id"], "patient_id": row["patient_id"], "appointment_id": row["appointment_id"],
            "total": money(row["total_cents"]), "refunded": money(refunded),
            "refundable_remaining": money(row["total_cents"] - refunded), "status": row["status"]}


class CancelRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=300, description="Why the appointment is cancelled")


class RescheduleRequest(BaseModel):
    starts_at: datetime = Field(description="New start time, for example 2026-12-01T09:30:00Z")


class RefundRequest(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=8, decimal_places=2, description="Amount in US dollars")
    reason: str = Field(min_length=3, max_length=300, description="Why the refund is issued")


class MessageRequest(BaseModel):
    body: str = Field(min_length=1, max_length=3000, description="Message sent to the patient")


@app.get("/inbox")
def list_inbox(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
    """The latest messages patients sent to the front desk, newest first."""
    rows = conn.execute("""SELECT m.id, m.patient_id, p.name AS patient, m.body, m.created_at FROM messages m
                           JOIN patients p ON p.id = m.patient_id WHERE m.direction = 'from_patient'
                           ORDER BY m.created_at DESC, m.id DESC LIMIT 20""").fetchall()
    return [dict(r) for r in rows]


@app.get("/patients/{patient_id}")
def get_patient(patient_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """One patient with their appointments and invoices."""
    p = one(conn, "SELECT * FROM patients WHERE id = ?", patient_id, "Patient")
    appts = conn.execute("SELECT id, clinician, starts_at, status FROM appointments WHERE patient_id = ? "
                         "ORDER BY starts_at", (patient_id,)).fetchall()
    invoices = conn.execute("SELECT id FROM invoices WHERE patient_id = ? ORDER BY id", (patient_id,)).fetchall()
    return {"id": p["id"], "name": p["name"], "email": p["email"], "phone": p["phone"],
            "appointments": [dict(a) for a in appts], "invoices": [invoice_detail(conn, i["id"]) for i in invoices]}


@app.get("/patients/{patient_id}/messages")
def get_patient_messages(patient_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
    """The full message thread with one patient, oldest first."""
    one(conn, "SELECT id FROM patients WHERE id = ?", patient_id, "Patient")
    rows = conn.execute("SELECT id, direction, body, created_at FROM messages WHERE patient_id = ? ORDER BY id",
                        (patient_id,)).fetchall()
    return [dict(r) for r in rows]


@app.post("/patients/{patient_id}/messages", status_code=201)
def send_patient_message(patient_id: int, body: MessageRequest, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """Send a message to a patient."""
    one(conn, "SELECT id FROM patients WHERE id = ?", patient_id, "Patient")
    cur = conn.execute("INSERT INTO messages (patient_id, direction, body, created_at) VALUES (?, 'to_patient', ?, ?)",
                       (patient_id, body.body, now()))
    conn.commit()
    return {"id": cur.lastrowid, "patient_id": patient_id, "sent": True}


@app.get("/appointments/{appointment_id}")
def get_appointment(appointment_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """One appointment with its patient and clinician."""
    return dict(one(conn, "SELECT * FROM appointments WHERE id = ?", appointment_id, "Appointment"))


@app.post("/appointments/{appointment_id}/cancel")
def cancel_appointment(appointment_id: int, body: CancelRequest,
                       conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """Cancel a booked appointment."""
    conn.execute("BEGIN IMMEDIATE")
    a = one(conn, "SELECT * FROM appointments WHERE id = ?", appointment_id, "Appointment")
    if a["status"] != "booked":
        raise HTTPException(status_code=409, detail=f"Appointment {appointment_id} is {a['status']}")
    conn.execute("UPDATE appointments SET status = 'cancelled' WHERE id = ?", (appointment_id,))
    conn.commit()
    return {"id": appointment_id, "status": "cancelled", "reason": body.reason}


@app.patch("/appointments/{appointment_id}")
def reschedule_appointment(appointment_id: int, body: RescheduleRequest,
                           conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """Move a booked appointment to a new start time."""
    conn.execute("BEGIN IMMEDIATE")
    a = one(conn, "SELECT * FROM appointments WHERE id = ?", appointment_id, "Appointment")
    if a["status"] != "booked":
        raise HTTPException(status_code=409, detail=f"Appointment {appointment_id} is {a['status']}")
    starts = body.starts_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("UPDATE appointments SET starts_at = ? WHERE id = ?", (starts, appointment_id))
    conn.commit()
    return {"id": appointment_id, "starts_at": starts}


@app.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """One invoice with the amount still refundable."""
    return invoice_detail(conn, invoice_id)


@app.post("/invoices/{invoice_id}/refunds", status_code=201)
def refund_invoice(invoice_id: int, body: RefundRequest, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """Refund part or all of an invoice."""
    conn.execute("BEGIN IMMEDIATE")
    inv = invoice_detail(conn, invoice_id)
    cents = int((body.amount * 100).to_integral_value())
    remaining = int(Decimal(inv["refundable_remaining"]) * 100)
    if cents > remaining:
        raise HTTPException(status_code=409, detail=f"Refund exceeds the {inv['refundable_remaining']} still refundable")
    conn.execute("INSERT INTO refunds (invoice_id, amount_cents, reason, created_at) VALUES (?, ?, ?, ?)",
                 (invoice_id, cents, body.reason, now()))
    conn.execute("UPDATE invoices SET status = ? WHERE id = ?",
                 ("refunded" if cents == remaining else "partially_refunded", invoice_id))
    conn.commit()
    return invoice_detail(conn, invoice_id)


@app.get("/health")
def health() -> dict:
    """Service health check."""
    return {"ok": True}


@app.delete("/admin/patients/{patient_id}", status_code=204)
def delete_patient(patient_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> None:
    """Permanently delete a patient record and everything attached to it."""
    conn.execute("BEGIN IMMEDIATE")
    one(conn, "SELECT id FROM patients WHERE id = ?", patient_id, "Patient")
    for sql in ("DELETE FROM refunds WHERE invoice_id IN (SELECT id FROM invoices WHERE patient_id = :id)",
                "DELETE FROM invoices WHERE patient_id = :id", "DELETE FROM appointments WHERE patient_id = :id",
                "DELETE FROM messages WHERE patient_id = :id", "DELETE FROM patients WHERE id = :id"):
        conn.execute(sql, {"id": patient_id})
    conn.commit()
