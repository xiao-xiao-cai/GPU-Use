import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from capacity_fixtures import record_capacity

from gpu_console import storage
from gpu_console.adapters.compshare import GpuInventory
from gpu_console.application import availability, watches
from gpu_console.application.inventory_overview import overview
from gpu_console.application.task_config import SHANGHAI
from gpu_console.storage import availability as observations, availability_rollups
from gpu_console.worker.collection import Collector

PROJECT = "org-availability"
WATCH_ID = "example-watch"
WATCH = {
    "id": WATCH_ID, "project_id": PROJECT, "name": "杭州两卡",
    "spec": json.dumps({
        "region": "cn-hz", "zone": "cn-hz-01", "gpu_model": "4090", "gpu": 2,
        "cpu": 16, "memory_gb": 64, "charge_type": "Postpay",
    }),
}
BASE = datetime(2026, 11, 18, 15, 0, tzinfo=SHANGHAI)
NOW = int(BASE.timestamp())


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "console.sqlite3"
    storage.initialize(path)
    with storage.connect(path) as conn:
        observations.initialize(conn)
        add_watch(conn)
        conn.execute("INSERT INTO snapshots VALUES ('catalog','',?,?,NULL)", (json.dumps([catalog_row()]), NOW))
        watches.sync_bindings(conn)
        yield conn


def add_watch(conn):
    conn.execute("INSERT INTO watches VALUES (:id,:project_id,:name,:spec)", WATCH)


def catalog_row(region="cn-hz", zone="cn-hz-01", status="Normal"):
    return {"region": region, "zone": zone, "gpu_model": "4090", "instance_type": "uhost", "status": status,
            "specs": [{"gpu": 2, "cpu": 16, "memory_gb": 64}]}


def test_catalog_replaces_same_bucket_and_keeps_unknown_separate(db):
    hangzhou, beijing = catalog_row(), catalog_row("cn-bj", "cn-bj-01", "SoldOut")
    observations.record_catalog(db, [hangzhou, beijing], NOW)
    observations.record_catalog(db, [hangzhou, beijing, beijing | {"status": "Normal"}], NOW + 5)
    observations.record_catalog(db, [hangzhou, beijing], NOW + 2)
    result = availability.query(db, source="catalog", now=NOW + 10)
    bj, hz = result["items"]
    assert bj["available"] is None and bj["known_count"] == 0
    assert hz["available"] is True and hz["known_count"] == hz["available_count"] == 1

    observations.record_catalog(db, None, NOW + 20)
    result = availability.query(db, source="catalog", key=hz["key"], now=NOW + 25)
    item = result["detail"]["item"]
    assert item["observed_at"] == NOW + 20 and item["available"] is None
    assert item["known_count"] == item["available_count"] == 0

    observations.record_catalog(db, [hangzhou, beijing], NOW + 300)
    observations.record_catalog(db, [hangzhou], NOW + 600)
    result = availability.query(db, source="catalog", region="cn-bj", now=NOW + 600)["items"][0]
    assert result["available"] is None and result["known_count"] == 1 and result["available_count"] == 0
    assert result["observed_at"] == NOW + 600
    assert availability.query(db, source="catalog", now=NOW + 1201)["items"][1]["available"] is None


def record_stock(conn, available, when):
    observations.record_pool(conn, "cn-hz", when, provider_updated_at=when,
                             counts={("cn-hz-01", "4090", "exclusive"): 2 if available else 0})


def record_spec(conn, available, when):
    record_capacity(conn, PROJECT, ("cn-hz", "cn-hz-01", "4090", "uhost"), {(2, 16, 64): available}, when)


def test_pool_reference_reuses_valid_history_by_pool_and_prefers_verified_total(db, monkeypatch):
    exclusive = ("cn-hz-01", "4090", "exclusive")
    spot = ("cn-hz-01", "4090", "spot")
    sparse = ("cn-hz-01", "5090", "exclusive")
    official = catalog_row()
    directory = [official, official | {"instance_type": "spot"}, official | {"gpu_model": "5090"}]
    db.execute("UPDATE snapshots SET data=? WHERE kind='catalog'", (json.dumps(directory),))
    for hour in range(24, 0, -1):
        ts = NOW - hour * 3600
        observations.record_pool(db, "cn-hz", ts, provider_updated_at=ts, counts={
            exclusive: 100 if hour == 10 else 3,
            spot: 0,
        })
    db.execute("INSERT INTO snapshots VALUES ('locations','',?,?,NULL)", (json.dumps([
        {"region": "cn-hz", "zone": "cn-hz-01", "zone_id": 100},
    ]), NOW))
    path = Path(db.execute("PRAGMA database_list").fetchone()["file"])
    now = [NOW]
    monkeypatch.setattr(storage, "now", lambda: now[0])

    class Cloud:
        totals = {}

        def gpu_inventory(self, region, zone_ids):
            assert region == "cn-hz" and zone_ids == {100: "cn-hz-01"}
            return GpuInventory(now[0], {exclusive: 0, spot: 0, sparse: 2}, frozenset(), self.totals)

    cloud = Cloud()
    collector = Collector(path, cloud)
    collector._pool(now[0], 0)
    peak = {"source": "observed_peak", "cards": 100, "sample_count": None, "peak_at": NOW - 10 * 3600}
    pools = {(row["gpu_model"], row["pool"]): row for row in overview(db, None, now[0], include_spot=True)["data"]}
    assert pools["4090", "exclusive"]["reference"] == peak
    assert pools["4090", "exclusive"]["remaining_cards"] == 0
    assert pools["4090", "spot"]["reference"] is None
    assert pools["5090", "exclusive"]["reference"]["cards"] == 2
    item = availability.query(db, source="watch", project_id=PROJECT, key=WATCH_ID, now=now[0])["detail"]["item"]
    assert item["reference"] == peak and item["required_cards"] == 2 and item["available"] is None

    now[0] += 300
    cloud.totals = {exclusive: 200, spot: 0}
    collector._pool(now[0], 0)
    pools = {row["pool"]: row for row in availability.pool_snapshot(db, now[0])["data"] if row["gpu_model"] == "4090"}
    assert pools["exclusive"]["reference"] == {"source": "platform_total", "cards": 200, "sample_count": None}
    assert pools["spot"]["reference"] is None
    assert all(row["reference"] is None for row in availability.pool_snapshot(db, now[0] + 601)["data"])


def test_historical_peak_backfills_retained_samples_and_survives_trimming(db):
    db.executescript("""
        DROP TRIGGER availability_pool_peak_insert;
        DROP TRIGGER availability_pool_peak_update;
        DROP TABLE availability_pool_peaks;
        DELETE FROM availability_migrations WHERE name='pool-peaks-v1';
    """)
    scope = ("cn-hz-01", "4090", "exclusive")
    old_peak_at = NOW - observations.WINDOW_SECONDS - 3600

    def report(at, cards, *, provider_at=None):
        observations.record_pool(db, "cn-hz", at, provider_updated_at=provider_at or at, counts={scope: cards})

    report(old_peak_at, 100)
    report(NOW, 20)
    observations.initialize(db)
    observations.initialize(db)

    def reference(at):
        [row] = availability_rollups.pool_references(db, at, {})
        return row["reference"]["cards"], row["reference"]["peak_at"]

    assert reference(NOW) == (100, old_peak_at)
    later = NOW + observations.RETENTION_SECONDS + 600
    observations.trim(db, later)
    assert not db.execute("SELECT 1 FROM availability_pool_samples").fetchone()
    assert not db.execute("SELECT 1 FROM availability_pool_items").fetchone()
    report(later, 10)
    assert reference(later) == (100, old_peak_at)
    report(later + 300, 150)
    assert reference(later + 300) == (150, later + 300)
    report(later + 400, 160)  # A newer provider update in the same five-minute bucket.
    assert reference(later + 400) == (160, later + 400)
    report(later + 1200, 1000, provider_at=later + 300)  # An expired snapshot cannot set a record.
    report(later + 1500, 160)
    assert reference(later + 1500) == (160, later + 400)


def test_rolling_bounds_and_independent_retention(db):
    since = NOW - observations.WINDOW_SECONDS
    for stamp, answer in ((since - 1, True), (since, False), (NOW, True)):
        record_stock(db, answer, stamp)
        record_spec(db, answer, stamp)
    record_stock(db, True, NOW + 301)
    result = availability.query(db, source="watch", project_id=PROJECT, key=WATCH_ID, now=NOW)
    history = result["detail"]
    assert len(history["hours"]) == 169
    assert history["item"]["known_count"] == 2 and history["item"]["available_count"] == 1
    assert sum(row["expected_count"] for row in history["hours"]) == history["item"]["expected_count"] == 2017
    assert history["hours"][0]["known_count"] == 1 and history["hours"][0]["available_count"] == 0
    assert next(row for row in history["hours"] if row["start"] == since + 3600)["known_count"] == 0
    observations.trim(db, NOW + 12 * 3600)
    assert db.execute("SELECT COUNT(*) FROM availability_pool_samples").fetchone()[0] == 4
    observations.trim(db, NOW + 25 * 3600)
    assert db.execute("SELECT COUNT(*) FROM availability_pool_samples").fetchone()[0] == 2


def test_hourly_stock_rollups_match_raw_bounds_overwrite_and_legacy_backfill(db):
    since = NOW - observations.WINDOW_SECONDS
    for when, available in (
        (since + 2, False), (since + 302, True), (since + 3602, False),
        (since + 3902, True), (NOW + 2, True), (NOW + 302, False),
    ):
        record_stock(db, available, when)
    record_stock(db, True, since + 3604)
    observed_from, observed_to = since + 3, NOW + 3
    identity = ("cn-hz", "cn-hz-01", "4090", "exclusive")
    raw = observations.pool_history(db, *identity, observed_from, observed_to)
    expected = {}
    for point in raw:
        bucket = (point["provider_updated_at"] // 3600 * 3600, point["remaining_cards"])
        expected[bucket] = expected.get(bucket, 0) + 1

    def observed():
        rows = availability_rollups.pool_distribution(db, *identity, observed_from, observed_to, hourly=True)
        return {(row["start"], row["value"]): row["count"] for row in rows}

    assert observed() == expected
    assert db.execute("SELECT SUM(count) FROM availability_hourly_counts").fetchone()[0] == 6
    db.execute("DELETE FROM availability_migrations WHERE name='hourly-counts-v1'")
    db.execute("DELETE FROM availability_hourly_counts")
    availability_rollups.initialize(db)
    availability_rollups.initialize(db)
    assert observed() == expected
    observations.trim(db, NOW + 9 * 86400)
    assert db.execute("SELECT COUNT(*) FROM availability_hourly_counts").fetchone()[0] == 0


def test_legacy_watch_history_migrates_latest_actual_once(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite3"
    monkeypatch.setattr(storage, "now", lambda: NOW)
    with storage.connect(path) as conn:
        conn.executescript(storage.SCHEMA)
        add_watch(conn)
        conn.executemany(
            "INSERT INTO stock_history VALUES (?,?,?,?)",
            [
                (WATCH_ID, NOW - 86400, 1, None), (WATCH_ID, NOW - 86399, 1, "poll failed"),
                (WATCH_ID, NOW - 300, 0, None), ("removed-watch", NOW - 60, 1, None),
            ],
        )
        observations.initialize(conn)
        observations.record_watch(conn, WATCH, True, NOW - 298)
        observations.initialize(conn)
        rows = conn.execute("SELECT * FROM availability_watch_samples ORDER BY slot").fetchall()
        assert [(row["watch_id"], row["available"]) for row in rows] == [(WATCH_ID, None), (WATCH_ID, 1)]
        assert rows[1]["observed_at"] == NOW - 298


def seed_recommendations(conn):
    for day, (seen, yes) in enumerate(((12, 6), (8, 8), (8, 8)), start=-3):
        for hour, size, positives in ((3, 8, 0), (8, 7, 7), (15, seen, yes)):
            when = int((BASE.replace(hour=hour) + timedelta(days=day)).timestamp())
            for slot in range(size):
                record_spec(conn, slot < positives, when + slot * 300)


def test_recommendation_weights_dates_and_requires_coverage(db):
    initial = availability.query(db, source="watch", key=WATCH_ID, project_id=PROJECT, now=NOW)
    assert initial["detail"]["recommendation_status"] == "insufficient"
    assert initial["detail"]["reminder"] == {"mode": "off", "channels": ["site"]}
    seed_recommendations(db)
    result = availability.query(db, source="watch", key=WATCH_ID, project_id=PROJECT, now=NOW)
    detail = result["detail"]
    assert detail["recommendation_status"] == "ready"
    assert detail["recommended"] == [{"hour": 15, "rate": pytest.approx(5 / 6), "days": 3}]
    assert detail["tight"] == [{"hour": 3, "rate": 0, "days": 3}]
    with pytest.raises(KeyError):
        availability.query(db, source="watch", key=WATCH_ID, project_id="another-project", now=NOW)
    with pytest.raises(KeyError):
        availability.set_reminder(db, watch_id=WATCH_ID, project_id="another-project", mode="advance", channels=["site"])


def test_pool_recommendations_compare_exact_card_counts_with_equal_day_weights(db):
    row = catalog_row()
    row["specs"] += [{"gpu": n, "cpu": 16, "memory_gb": 64} for n in (4, 8)]
    db.execute("UPDATE snapshots SET data=? WHERE kind='catalog'", (json.dumps([row]),))
    scope = ("cn-hz", "cn-hz-01", "4090", "uhost")
    identity = ("cn-hz-01", "4090", "exclusive")
    for day, (seen, high) in enumerate(((12, 6), (8, 8), (8, 8)), start=-3):
        for hour, slots, cards in ((3, 8, 0), (8, 7, 7), (15, seen, high)):
            start = int((BASE.replace(hour=hour) + timedelta(days=day)).timestamp())
            for slot in range(slots):
                timestamp = start + slot * observations.SLOT_SECONDS
                observations.record_pool(
                    db, "cn-hz", timestamp, provider_updated_at=timestamp,
                    counts={identity: 6 if slot < cards else 2},
                )
                record_capacity(db, PROJECT, scope, {(4, 16, 64): slot < cards, (8, 16, 64): False}, timestamp)
    key = availability.query(db, source="pool", card_threshold=4, now=NOW)["items"][0]["key"]
    detail = availability.query(db, source="pool", project_id=PROJECT, key=key, card_threshold=4, now=NOW)["detail"]
    assert detail["recommended"] == [{"hour": 15, "rate": pytest.approx(5 / 6), "days": 3}]
    assert detail["tight"] == [{"hour": 3, "rate": 0, "days": 3}]
    assert detail["reminder"] is None
    detail = availability.query(db, source="pool", project_id=PROJECT, key=key, card_threshold=8, now=NOW)["detail"]
    assert detail["recommendation_status"] == "stable"
    assert detail["recommended"] == detail["tight"] == []


def test_read_keeps_one_snapshot_and_preserves_caller_transaction(db, monkeypatch):
    path = Path(db.execute("PRAGMA database_list").fetchone()["file"])
    record_spec(db, False, NOW - 1)
    list_watches = observations.list_watches

    def record_concurrently(*args, **kwargs):
        rows = list_watches(*args, **kwargs)
        with storage.connect(path) as writer:
            record_spec(writer, True, NOW)
        return rows

    monkeypatch.setattr(observations, "list_watches", record_concurrently)
    filters = {"source": "watch", "key": WATCH_ID, "project_id": PROJECT}
    detail = availability.query(db, **filters, now=NOW)["detail"]
    assert detail["item"]["available"] is False and detail["item"]["known_count"] == 1
    assert not db.in_transaction

    monkeypatch.setattr(observations, "list_watches", list_watches)
    with pytest.raises(RuntimeError), storage.transaction(db):
        record_spec(db, False, NOW + 1)
        assert availability.query(db, **filters, now=NOW + 1)["items"][0]["available"] is False
        assert db.in_transaction
        raise RuntimeError("rollback uncommitted observation")
    assert availability.query(db, **filters, now=NOW + 1)["items"][0]["available"] is True


def test_equally_available_hours_do_not_generate_recommendations(db):
    for day in (3, 2, 1):
        for hour in (3, 15):
            start = int((BASE.replace(hour=hour) - timedelta(days=day)).timestamp())
            for slot in range(8):
                record_spec(db, True, start + slot * 300)
    detail = availability.query(db, source="watch", key=WATCH_ID, project_id=PROJECT, now=NOW)["detail"]
    assert detail["recommendation_status"] == "stable"
    assert detail["recommended"] == detail["tight"] == []


@pytest.mark.parametrize("mode", ["advance", "when_available"])
def test_reminder_modes_dedupe_and_cancel(db, monkeypatch, mode):
    seed_recommendations(db)
    clock = [NOW - 1801 if mode == "advance" else NOW + 15]
    monkeypatch.setattr(storage, "now", lambda: clock[0])
    availability.set_reminder(db, watch_id=WATCH_ID, project_id=PROJECT, mode=mode, channels=["site", "feishu"])

    record_spec(db, True, NOW - 2000 if mode == "advance" else NOW - 15)
    availability.reminder_tick(db, clock[0])
    assert db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0
    clock[0] += 2
    if mode == "when_available":
        record_spec(db, True, clock[0])
    with storage.transaction(db):
        availability.reminder_tick(db, clock[0])
    observations.initialize(db)
    availability.reminder_tick(db, clock[0])

    rows = db.execute("SELECT * FROM notifications ORDER BY channel").fetchall()
    assert [(row["channel"], row["state"]) for row in rows] == [("feishu", "pending"), ("site", "delivered")]
    assert {row["event_id"] for row in rows} == {"rental-window:org-availability:example-watch:2026-11-18"}
    payload = json.loads(rows[0]["payload"])
    assert payload["window_start"] == NOW and payload["mode"] == mode
    assert ("当前规格可租") in payload["message"]
    assert "规格探测" in payload["message"]
    assert availability.delivery_hold(db, rows[0], payload, clock[0]) is None
    expiry = NOW if mode == "advance" else NOW + 3600
    assert availability.delivery_hold(db, rows[0], payload, expiry)[0] == "skipped"
    if mode == "when_available":
        record_spec(db, False, clock[0] + 2)
        assert availability.delivery_hold(db, rows[0], payload, clock[0] + 2)[0] == "pending"
        record_spec(db, True, clock[0] + 60)
        assert availability.delivery_hold(db, rows[0], payload, clock[0] + 60) is None
        assert payload["confirmed_at"] == clock[0]

    availability.set_reminder(db, watch_id=WATCH_ID, project_id=PROJECT, mode="off", channels=[])
    assert db.execute("SELECT state FROM notifications WHERE channel='feishu'").fetchone()[0] == "skipped"
    availability.set_reminder(db, watch_id=WATCH_ID, project_id=PROJECT, mode=mode, channels=["email"])
    availability.reminder_tick(db, clock[0])
    assert db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 2


def test_restore_and_delete_prevent_pending_reminders(db, monkeypatch):
    seed_recommendations(db)
    record_stock(db, True, NOW - 1200)
    now = NOW - 1000
    monkeypatch.setattr(storage, "now", lambda: now)
    availability.set_reminder(db, watch_id=WATCH_ID, project_id=PROJECT, mode="advance", channels=["feishu"])
    storage.set_settings(db, {"restore_gate": True})
    availability.reminder_tick(db, now)
    assert db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0
    storage.set_settings(db, {"restore_gate": False})
    availability.reminder_tick(db, now)
    availability.cancel_restored(db)
    assert db.execute("SELECT state FROM notifications").fetchone()[0] == "skipped"

    db.execute("UPDATE notifications SET state='pending'")
    db.execute("DELETE FROM watches WHERE id=?", (WATCH_ID,))
    observations.remove_watch(db, WATCH_ID)
    assert db.execute("SELECT COUNT(*) FROM availability_pool_samples").fetchone()[0] > 0
    assert db.execute("SELECT state FROM notifications").fetchone()[0] == "skipped"


def test_long_collection_interval_cannot_reuse_stale_positive_for_reminder(db, monkeypatch):
    seed_recommendations(db)
    now = NOW + 900
    monkeypatch.setattr(storage, "now", lambda: now)
    storage.set_settings(db, {"stock_seconds": 900})
    availability.set_reminder(db, watch_id=WATCH_ID, project_id=PROJECT, mode="when_available", channels=["site"])
    record_stock(db, True, now)
    record_spec(db, True, NOW)
    availability.reminder_tick(db, now)
    assert db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0


def test_legacy_bindings_keep_preferred_reminder_and_missing_spec(tmp_path):
    path = tmp_path / "legacy-bindings.db"
    storage.initialize(path)
    spec = json.loads(WATCH["spec"])
    with storage.connect(path) as conn:
        add_watch(conn)
        conn.execute("INSERT INTO watches VALUES ('with-reminder',?,'第二个关注',?)",
                     (PROJECT, json.dumps(spec | {"cpu_platform": "AMD", "image_id": "image-a"})))
        conn.execute("INSERT INTO watches VALUES ('no-catalog',?,'待重新绑定',?)",
                     (PROJECT, json.dumps(spec | {"gpu": 10})))
        observations.save_reminder(conn, "with-reminder", "advance", ["feishu"])
        conn.execute("INSERT INTO snapshots VALUES ('catalog','',?,?,NULL)", (json.dumps([catalog_row()]), NOW))
        observations.record_watch(conn, WATCH, True, NOW)
    storage.initialize(path)
    storage.initialize(path)
    with storage.connect(path) as conn:
        entries = {row["id"]: row for row in watches.list_project(conn, PROJECT, NOW, history=True)}
        assert entries[WATCH_ID]["binding_status"] == "duplicate"
        assert entries[WATCH_ID]["primary_watch_id"] == "with-reminder"
        assert entries["with-reminder"]["binding_status"] == "bound"
        assert entries["with-reminder"]["available"] is None and not entries["with-reminder"]["history"]
        assert observations.reminder(conn, "with-reminder") == {"mode": "advance", "channels": ["feishu"]}
        assert entries["no-catalog"]["binding_status"] == "unlisted"


def test_legacy_inferred_alerts_and_old_samples_do_not_seed_capacity(db):
    from gpu_console.storage import capacity

    record_stock(db, True, NOW)
    observations.record_watch(db, WATCH, True, NOW)
    db.execute("UPDATE catalog_watch_bindings SET baseline_at=?,baseline_available=1", (NOW,))
    db.execute("DELETE FROM availability_migrations WHERE name='scheduling-evidence-v1'")
    storage.emit(db, "watch-stock:example-watch:old", {"watch_id": WATCH_ID}, ["feishu", "site"])
    capacity.initialize(db)
    item = availability.query(db, source="watch", project_id=PROJECT, key=WATCH_ID, now=NOW)["detail"]["item"]
    assert item["remaining_cards"] == 2 and item["available"] is None and item["known_count"] == 0
    baseline = db.execute("SELECT baseline_at,baseline_available FROM catalog_watch_bindings").fetchone()
    assert tuple(baseline) == (None, None)
    assert dict(db.execute("SELECT channel,state FROM notifications ORDER BY channel").fetchall()) == {
        "feishu": "skipped", "site": "delivered",
    }
    record_spec(db, False, NOW)
    capacity.initialize(db)
    assert availability.query(db, source="watch", project_id=PROJECT, now=NOW)["items"][0]["available"] is False


def test_incomplete_matrix_keeps_history_and_does_not_promote_false_to_pool_result(db):
    row = catalog_row()
    row["specs"].append({"gpu": 2, "cpu": 32, "memory_gb": 128})
    db.execute("UPDATE snapshots SET data=? WHERE kind='catalog'", (json.dumps([row]),))
    record_stock(db, True, NOW)
    scope = ("cn-hz", "cn-hz-01", "4090", "uhost")
    record_capacity(db, PROJECT, scope, {(2, 16, 64): False, (2, 32, 128): False}, NOW)
    options = {"source": "pool", "project_id": PROJECT, "card_threshold": 2}
    exact = {"source": "watch", "project_id": PROJECT, "key": WATCH_ID}
    assert availability.query(db, **options, now=NOW)["items"][0]["available"] is False
    record_capacity(db, PROJECT, scope, {(2, 16, 64): False}, NOW + 1)
    item = availability.query(db, **options, now=NOW + 1)["items"][0]
    assert item["available"] is None and item["known_count"] == 1
    assert item["capacity_checked_at"] == NOW + 1
    assert availability.query(db, **exact, now=NOW + 1)["items"][0]["available"] is False
    record_capacity(db, PROJECT, scope, {(2, 32, 128): True}, NOW + 300)
    item = availability.query(db, **options, now=NOW + 300)["items"][0]
    assert item["available"] is True and item["known_count"] == 2 and item["available_count"] == 1
    assert availability.query(db, **exact, now=NOW + 300)["items"][0]["available"] is None
    assert availability.query(db, **options, now=NOW + 901)["items"][0]["available"] is None
