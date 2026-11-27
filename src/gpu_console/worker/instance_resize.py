"""Resolve resize previews from the cloud without holding a database transaction."""

import json
from pathlib import Path

from gpu_console import storage
from gpu_console.adapters.compshare import CloudError
from gpu_console.application import instance_resize as resize


def quote_tick(path: Path, cloud) -> None:
    now = storage.now()
    with storage.connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM instance_resize_quotes WHERE state='queued' AND created_at>? ORDER BY created_at LIMIT 1",
            (now - resize.TTL,),
        ).fetchone()
    if row is None:
        return
    expected, target = json.loads(row["expected"]), json.loads(row["target"])
    amount, available, error = None, None, None
    try:
        if cloud is None:
            raise ValueError("优云智算凭据未配置")
        with storage.connect(path) as conn:
            current = resize._target(conn, row["project_id"], row["instance_id"], storage.now())
        if resize.baseline(current) != expected:
            raise ValueError("实例配置已变化，请重新询价")
        available = cloud.capacity(expected | target)
        amount = str(cloud.resize_quote(expected, target))
    except (ValueError, KeyError, CloudError) as failure:
        error = str(failure)
    with storage.connect(path) as conn:
        conn.execute("""UPDATE instance_resize_quotes SET state=?,available=?,amount=?,checked_at=?,error=?
                        WHERE id=? AND state='queued'""",
                     ("failed" if error else "ready", available, amount, storage.now(), error, row["id"]))
