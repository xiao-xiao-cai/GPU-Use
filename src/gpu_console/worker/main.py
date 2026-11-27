"""Run serial cloud decisions and independent background collection and delivery."""

import logging
import signal
from collections.abc import Callable
from contextlib import ExitStack
from ipaddress import ip_address
from threading import Event, Thread
from time import monotonic

from gpu_console import storage
from gpu_console.storage import refresh
from gpu_console.adapters.compshare import CompShare
from gpu_console.adapters.notifications import Channels
from gpu_console.application import availability as rental_windows
from gpu_console.application import inventory_prices
from gpu_console.application.feishu_notices import capacity_wait_tick
from gpu_console.application.ssh_password import serve_ssh_passwords
from gpu_console.application.tasks import TaskStore
from gpu_console.config import Config
from gpu_console.storage import availability, capacity, dashboard_cost, dashboard_cost_rollups, dashboard_usage, storage_cost
from gpu_console.storage import dashboard_billed_cost_rollups
from gpu_console.storage.maintenance import process_lock
from gpu_console.worker.billing import BillingCollector
from gpu_console.worker.instance_assets import AssetCollector
from gpu_console.worker.instance_resize import quote_tick as resize_quote_tick
from gpu_console.worker.capacity import CapacityCollector
from gpu_console.worker.catalog import ImageCollector
from gpu_console.worker.collection import Collector
from gpu_console.worker.instance_quotes import InventoryPriceWorker, QuoteWorker
from gpu_console.worker.outbox import Outbox
from gpu_console.worker.ssh_sessions import serve_ssh_sessions
from gpu_console.worker.tasks import TaskRunner

LOGGER = logging.getLogger(__name__)


def _console_origin(config: Config) -> str | None:
    try:
        address = ip_address(config.mcp_bind_ip)
    except ValueError:
        return None
    if address.is_unspecified:
        return None
    host = str(address) if address.version == 4 else f"[{address}]"
    scheme = "https" if config.cookie_secure else "http"
    return f"{scheme}://{host}:{config.console_bind_port}"


def check_health(config: Config) -> None:
    if not config.db.is_file():
        raise ValueError("等待后台初始化")
    with storage.connect(config.db) as conn:
        row = conn.execute("SELECT success_at FROM system_status WHERE component='worker'").fetchone()
    if row is None or row["success_at"] is None or storage.now() - row["success_at"] > 180:
        raise ValueError("后台心跳超时")


def run_worker(config: Config) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # HTTPX's INFO request message includes the full Feishu webhook URL.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    stopping = Event()

    def stop(_signum, _frame) -> None:
        stopping.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    with (
        process_lock(config.data_dir, ".maintenance.lock", shared=True),
        process_lock(config.data_dir, ".worker.lock"),
        ExitStack() as services,
    ):
        storage.initialize(config.db)
        try:
            cloud = CompShare.from_directory(
                config.secrets_dir, scheduler_omits_empty=config.scheduler_omits_empty,
            )
        except (OSError, ValueError):
            cloud = None
            LOGGER.warning("CompShare 凭据不可用，请检查 worker 挂载并重启")
        try:
            services.enter_context(serve_ssh_passwords(config.data_dir, cloud.ssh_password if cloud else None))
        except OSError:
            LOGGER.warning("SSH 密码查询服务暂不可用")
        try:
            services.enter_context(serve_ssh_sessions(config))
        except OSError:
            LOGGER.warning("SSH 会话服务暂不可用")
        channels = Channels(config.secrets_dir, console_origin=_console_origin(config))
        with storage.connect(config.db) as conn:
            storage.record_status(conn, "cloud", cloud is not None, None if cloud else "优云智算凭据未配置")
            for channel in ("feishu", "email"):
                configured = channels.configured(channel, legacy_only=True)
                storage.record_status(conn, f"channel_{channel}", configured, "configured" if configured else None)

        store = TaskStore(config.db, feishu_configured=lambda _conn: channels.configured("feishu"))
        runner = TaskRunner(store, cloud, read_only=config.read_only, release_charge_types=config.mcp_release_charge_types)
        outbox = Outbox(config.db, channels)

        def loop(component: str, action: Callable, interval: float) -> None:
            last_status = float("-inf")
            previous_error = None
            while not stopping.is_set():
                diagnostic = None
                try:
                    action()
                except Exception as error:
                    diagnostic = f"后台执行异常（{type(error).__name__}）"
                changed = diagnostic != previous_error
                if changed or monotonic() - last_status >= 10:
                    if diagnostic:
                        LOGGER.error("%s %s", component, diagnostic)
                    with storage.connect(config.db) as conn:
                        storage.record_status(conn, component, diagnostic is None, diagnostic)
                    previous_error = diagnostic
                    last_status = monotonic()
                stopping.wait(interval)

        images = ImageCollector(config.db, cloud)
        quotes = QuoteWorker(config.db, cloud)
        inventory_quotes = InventoryPriceWorker(config.db, cloud)

        next_capacity_check = 0

        def deliver_notifications() -> None:
            nonlocal next_capacity_check
            outbox.tick()
            now = storage.now()
            if now >= next_capacity_check:
                configured = channels.configured("feishu")
                with storage.connect(config.db) as conn, storage.transaction(conn):
                    capacity_wait_tick(conn, now, feishu_configured=configured)
                next_capacity_check = now + 60

        def collect_catalog_inputs() -> None:
            resize_quote_tick(config.db, cloud)
            quotes.tick()
            inventory_quotes.tick()
            images.tick()

        next_dashboard_trim = 0

        def update_dashboard() -> None:
            nonlocal next_dashboard_trim
            now = storage.now()
            with storage.connect(config.db) as conn, storage.transaction(conn):
                inventory_prices.sync_targets(conn, now)
                if now >= next_dashboard_trim:
                    conn.execute("DELETE FROM instance_resize_quotes WHERE created_at<?", (now - 86400,))
                    availability.trim(conn, now)
                    capacity.trim(conn, now)
                    dashboard_usage.trim(conn, now)
                    dashboard_cost.trim(conn, now)
                    dashboard_cost_rollups.trim(conn, now)
                    dashboard_billed_cost_rollups.trim(conn, now)
                    storage_cost.trim(conn, now)
                    inventory_prices.trim(conn, now)
                rental_windows.reminder_tick(conn, now)
            if now >= next_dashboard_trim:
                next_dashboard_trim = now + 3600

        def maintain_local_data() -> None:
            runner.store.purge_expired_tasks()
            update_dashboard()

        jobs = [
            Thread(target=loop, args=("notifications", deliver_notifications, 2), daemon=True),
            Thread(target=loop, args=("catalog_inputs", collect_catalog_inputs, 2), daemon=True),
            Thread(target=loop, args=("dashboard", maintain_local_data, 60), daemon=True),
            Thread(target=loop, args=("dashboard_cost_rollups",
                                      lambda: dashboard_cost_rollups.refresh(config.db, storage.now(), estimate_costs=False),
                                      10), daemon=True),
            Thread(target=loop, args=("dashboard_billed_cost_rollups",
                                      lambda: dashboard_billed_cost_rollups.refresh(config.db, storage.now()),
                                      10), daemon=True),
        ]
        if cloud is not None:
            billing = BillingCollector(config.db, cloud)
            jobs.append(Thread(target=loop, args=("billing", billing.tick, 10), daemon=True))
            capacity_collector = CapacityCollector(config.db, cloud)
            jobs.append(Thread(target=loop, args=("capacity", capacity_collector.tick, 2), daemon=True))
            assets = AssetCollector(config.db, cloud)
            jobs.append(Thread(target=loop, args=("instance_assets", assets.tick, 10), daemon=True))
            collector = Collector(config.db, cloud, read_only=config.read_only)
            jobs.append(Thread(target=loop, args=("collection", collector.tick, 2), daemon=True))
        else:
            def unavailable_collection() -> None:
                with storage.connect(config.db) as conn:
                    if refresh.pending(conn):
                        refresh.finish_unavailable(conn)

            jobs.append(Thread(target=loop, args=("collection", unavailable_collection, 2), daemon=True))
        for job in jobs:
            job.start()
        LOGGER.info("后台调度已启动")
        try:
            loop("worker", lambda: runner.tick(limit=1), 1)
        finally:
            stopping.set()
            for job in jobs:
                job.join(timeout=5)
