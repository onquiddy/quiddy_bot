from __future__ import annotations

import asyncio
import os
import platform
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

import psutil


@dataclass(slots=True)
class RuntimeSample:
    ts: float
    cpu: float
    rss: int
    system_memory: float
    loop_lag_ms: float
    threads: int
    tasks: int


class RuntimeMonitor:
    """Cheap in-process telemetry. It samples; it never blocks command handling."""

    def __init__(self, app, cfg: dict[str, Any] | None = None) -> None:
        self.app = app
        self.cfg = cfg or {}
        self.started_at = time.time()
        self.process = psutil.Process(os.getpid())
        self.samples: deque[RuntimeSample] = deque(maxlen=max(30, int(self.cfg.get("history_samples", 300))))
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_expected = 0.0
        self._last_lag_ms = 0.0
        self._peak_rss = 0
        self._cpu_count = psutil.cpu_count(logical=True) or 1
        self.process.cpu_percent(None)

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="core:runtime-monitor")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        interval = max(1.0, float(self.cfg.get("sample_interval_seconds", 2.0)))
        loop = asyncio.get_running_loop()
        expected = loop.time() + interval
        while True:
            await asyncio.sleep(max(0.0, expected - loop.time()))
            now_loop = loop.time()
            self._last_lag_ms = max(0.0, (now_loop - expected) * 1000.0)
            expected = now_loop + interval
            try:
                mem = self.process.memory_info().rss
                self._peak_rss = max(self._peak_rss, mem)
                self.samples.append(RuntimeSample(
                    ts=time.time(), cpu=self.process.cpu_percent(None), rss=mem,
                    system_memory=psutil.virtual_memory().percent,
                    loop_lag_ms=self._last_lag_ms, threads=self.process.num_threads(),
                    tasks=len(asyncio.all_tasks()),
                ))
            except (psutil.Error, RuntimeError):
                pass

    @staticmethod
    def _mb(value: int) -> float:
        return value / 1024 / 1024

    def snapshot(self) -> dict[str, Any]:
        latest = self.samples[-1] if self.samples else None
        children_rss = 0
        child_count = 0
        try:
            children = self.process.children(recursive=True)
            child_count = len(children)
            for child in children:
                try:
                    children_rss += child.memory_info().rss
                except psutil.Error:
                    pass
        except psutil.Error:
            pass
        vm = psutil.virtual_memory()
        cpu = latest.cpu if latest else 0.0
        rss = latest.rss if latest else self.process.memory_info().rss
        return {
            "status": "up" if self._running else "down",
            "uptime_seconds": max(0, int(time.time() - self.started_at)),
            "process_cpu_percent": round(cpu, 1),
            "process_ram_mb": round(self._mb(rss), 1),
            "children_ram_mb": round(self._mb(children_rss), 1),
            "total_tree_ram_mb": round(self._mb(rss + children_rss), 1),
            "peak_process_ram_mb": round(self._mb(self._peak_rss or rss), 1),
            "system_ram_percent": round(vm.percent, 1),
            "system_ram_free_mb": round(self._mb(vm.available), 0),
            "loop_lag_ms": round(self._last_lag_ms, 2),
            "threads": latest.threads if latest else self.process.num_threads(),
            "tasks": latest.tasks if latest else len(asyncio.all_tasks()),
            "children": child_count,
            "logical_cpus": self._cpu_count,
            "python": platform.python_version(),
            "os": f"{platform.system()} {platform.release()}",
        }

    async def health(self) -> dict[str, Any]:
        snap = self.snapshot()
        lag_warn = float(self.cfg.get("loop_lag_warning_ms", 150))
        ram_warn = float(self.cfg.get("system_ram_warning_percent", 90))
        snap["status"] = "degraded" if snap["loop_lag_ms"] >= lag_warn or snap["system_ram_percent"] >= ram_warn else "up"
        return snap
