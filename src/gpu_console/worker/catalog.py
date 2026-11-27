"""Refresh subscribed image catalogs one page at a time."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from sqlite3 import Row

from gpu_console import storage
from gpu_console.adapters.compshare import PAGE_SIZE, CloudError, CompShare, ImagePage
from gpu_console.storage import catalog


@dataclass
class ImageScan:
    batch_id: str | None
    images: list[dict] = field(default_factory=list)
    ids: set[str] = field(default_factory=set)
    total: int | None = None

    def append(self, page: ImagePage) -> bool:
        if page.total is not None:
            if self.total is not None and page.total != self.total:
                raise CloudError("incomplete_images", retryable=True)
            self.total = page.total
        if self.ids.intersection(page.ids):
            raise CloudError("incomplete_images", retryable=True)
        self.ids.update(page.ids)
        if self.total is not None and (
            len(self.ids) > self.total or (not page.ids and len(self.ids) < self.total)
        ):
            raise CloudError("incomplete_images", retryable=True)
        self.images.extend(page.images)
        return len(self.ids) == self.total if self.total is not None else len(page.ids) < PAGE_SIZE


class ImageCollector:
    def __init__(self, path: Path, cloud: CompShare | None):
        self.path = path
        self.cloud = cloud
        self.scans: dict[tuple[str, str], ImageScan] = {}

    def tick(self) -> None:
        with storage.connect(self.path) as conn:
            source = catalog.due_image_source(conn, storage.now())
            if source is None:
                return
            project_id, region, zone = json.loads(source["scope"])
            known = catalog.project_known(conn, project_id) and catalog.location_known(conn, region, zone)
        key = (source["scope"], source["kind"])
        if self.cloud is None or not known:
            self._finish(source, None, "优云智算凭据未配置" if self.cloud is None else "项目或可用区不在当前平台目录中")
            self.scans.pop(key, None)
            return
        scan = self.scans.get(key)
        if scan is None or scan.batch_id != source["batch_id"]:
            scan = self.scans[key] = ImageScan(source["batch_id"])
        try:
            page = self.cloud.image_page(project_id, region, zone, source["kind"], len(scan.ids))
            done = scan.append(page)
        except CloudError as failure:
            self.scans.pop(key, None)
            self._finish(source, None, str(failure), retryable=failure.retryable)
            return
        if done:
            self.scans.pop(key, None)
            self._finish(source, scan.images, None)
        else:
            with storage.connect(self.path) as conn:
                catalog.defer_image_page(conn, source, storage.now())

    def _finish(self, source: Row, images: list[dict] | None, error: str | None, *, retryable: bool = False) -> None:
        with storage.connect(self.path) as conn, storage.transaction(conn):
            catalog.finish_image_source(conn, source, storage.now(), images, error, retryable=retryable)
