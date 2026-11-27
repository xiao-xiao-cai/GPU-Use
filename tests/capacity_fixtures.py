"""Seed simulated platform responses without duplicating worker setup."""

from gpu_console.application import watches
from gpu_console.storage import capacity


def record_capacity(conn, project_id, scope, matrix, timestamp):
    members = capacity.groups(watches.current_catalog(conn))[scope]
    encoded = capacity.encode(sorted(members))
    version = capacity.revision(project_id, scope, encoded, None)
    conn.execute(
        """INSERT OR IGNORE INTO capacity_profiles(project_id,scope,members,revision,next_at)
           VALUES (?,?,?,?,?)""",
        (project_id, capacity.encode(scope), encoded, version, timestamp),
    )
    projected = capacity.project_results(members, matrix)
    assert capacity.record(conn, project_id, scope, version, projected, timestamp, timestamp + 300, None)
    return members
