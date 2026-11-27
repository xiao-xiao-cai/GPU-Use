"""Deliver previously committed notifications independently of cloud operations."""

import json
from pathlib import Path

from gpu_console import storage
from gpu_console.adapters.notifications import Channels, DeliveryError
from gpu_console.application import availability, feishu_notices, idle_notices, resource_notices

BACKOFF = (30, 120, 600, 1800)


class Outbox:
    def __init__(self, path: Path, channels: Channels):
        self.path = path
        self.channels = channels

    def tick(self) -> None:
        timestamp = storage.now()
        with storage.connect(self.path) as conn:
            row = conn.execute(
                """SELECT id,event_id,channel,payload,attempts,next_at FROM notifications
                   WHERE state='pending' AND next_at<=? ORDER BY next_at,id LIMIT 1""",
                (timestamp,),
            ).fetchone()
            if row is None:
                return
            payload = json.loads(row["payload"])
            hold = (
                idle_notices.delivery_hold(conn, row, payload, timestamp)
                or resource_notices.delivery_hold(conn, row, payload, timestamp)
                or availability.delivery_hold(conn, row, payload, timestamp)
                or feishu_notices.delivery_hold(conn, row, payload, timestamp)
            )
            if hold:
                state, reason = hold
                conn.execute(
                    """UPDATE notifications SET state=?,next_at=?,error=?
                       WHERE id=? AND state='pending' AND attempts=? AND next_at=?""",
                    (state, timestamp + 30, reason, row["id"], row["attempts"], row["next_at"]),
                )
                return
        attempts = row["attempts"] + 1
        error_message = None
        state, next_at, delivered_at = "delivered", timestamp, timestamp
        try:
            self.channels.send(row["channel"], payload, event_id=row["event_id"])
        except DeliveryError as error:
            error_message = str(error)
            delivered_at = None
            if error.retryable and attempts <= len(BACKOFF):
                state, next_at = "pending", timestamp + BACKOFF[attempts - 1]
            else:
                state = "failed"
        with storage.connect(self.path) as conn:
            conn.execute(
                """UPDATE notifications SET attempts=?,state=?,next_at=?,delivered_at=?,error=?
                   WHERE id=? AND state='pending' AND attempts=? AND next_at=?""",
                (attempts, state, next_at, delivered_at, error_message, row["id"], row["attempts"], row["next_at"]),
            )
