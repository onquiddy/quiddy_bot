from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import platform
import re
import secrets
import shutil
import socket
import stat
import sys
import time
import zipfile
from collections import deque
from pathlib import Path
from typing import Any

import aiohttp
import yaml

from quiddy.core.logging import done, loading, system

log = logging.getLogger("quiddy.music.runtime")


def _safe_extract_zip(zf: zipfile.ZipFile, destination: Path, *, max_total_bytes: int = 300_000_000) -> None:
    # Архивы здесь приходят из сети, поэтому extractall() без проверки мне не нравится.
    base = destination.resolve()
    total = 0
    for info in zf.infolist():
        total += max(0, int(info.file_size))
        if total > max_total_bytes:
            raise RuntimeError("Archive is larger than the configured extraction limit")
        target = (destination / info.filename).resolve()
        try:
            target.relative_to(base)
        except ValueError as exc:
            raise RuntimeError(f"Unsafe archive member: {info.filename!r}") from exc
    zf.extractall(destination)



class EmbeddedLavalinkError(RuntimeError):
    pass


class EmbeddedCipherError(RuntimeError):
    pass


class EmbeddedCipherRuntime:
    """Self-hosted yt-cipher runtime managed by Quiddy.

    Quiddy downloads a private Deno runtime and yt-cipher source on first start,
    binds it to localhost only, protects it with a generated API token and keeps
    the process supervised. No public endpoint or second manual console is needed.
    """

    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self.ctx = plugin.ctx
        self.cfg = self.ctx.config.get("embedded_cipher", {})
        self.root = (self.ctx.root / str(self.cfg.get("runtime_dir", "runtime/yt-cipher"))).resolve()
        self.source_dir = self.root / "source"
        self.tools_dir = self.root / "tools"
        self.deno_dir = self.tools_dir / "deno"
        self.token_file = self.root / ".token"
        self.marker_file = self.root / ".source-ready"
        self.process: asyncio.subprocess.Process | None = None
        self._stdout_task: asyncio.Task | None = None
        self._log_file = None
        self._watchdog_task: asyncio.Task | None = None
        self._restart_lock = asyncio.Lock()
        self._stopping = False
        self._recent_logs: deque[str] = deque(maxlen=int(self.cfg.get("recent_log_lines", 100)))
        self.host = str(self.cfg.get("host", "127.0.0.1"))
        self.port = int(self.cfg.get("port", 8001))
        self.token = ""
        self.deno_command: str | None = None

    @property
    def uri(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def start(self) -> None:
        if not bool(self.cfg.get("enabled", True)):
            raise EmbeddedCipherError("embedded_cipher.enabled=false")
        self.root.mkdir(parents=True, exist_ok=True)
        self.tools_dir.mkdir(parents=True, exist_ok=True)
        self.token = self._load_or_create_token()
        try:
            self.deno_command = await self._ensure_deno()
            await self._ensure_source()
            await self._spawn()
            await self.wait_ready()
            if bool(self.cfg.get("watchdog", True)):
                self._watchdog_task = asyncio.create_task(self._watchdog(), name="music:yt-cipher-watchdog")
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        self._stopping = True
        if self._watchdog_task:
            self._watchdog_task.cancel()
            await asyncio.gather(self._watchdog_task, return_exceptions=True)
            self._watchdog_task = None
        await self._stop_process()

    async def restart(self) -> None:
        async with self._restart_lock:
            await self._stop_process()
            await self._spawn()
            await self.wait_ready()

    async def refresh_source(self) -> None:
        """Force-refresh yt-cipher/ejs, then restart only the cipher process."""
        async with self._restart_lock:
            await self._stop_process()
            if self.source_dir.exists():
                shutil.rmtree(self.source_dir, ignore_errors=True)
            self.marker_file.unlink(missing_ok=True)
            await self._ensure_source(force=True)
            await self._spawn()
            await self.wait_ready()

    async def _stop_process(self) -> None:
        proc = self.process
        if proc:
            if proc.returncode is None:
                loading(log, "[Cipher] Stopping local yt-cipher…")
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=float(self.cfg.get("shutdown_timeout_seconds", 8)))
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            else:
                await proc.wait()
        if self._stdout_task:
            try:
                await asyncio.wait_for(asyncio.shield(self._stdout_task), timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._stdout_task.cancel()
                await asyncio.gather(self._stdout_task, return_exceptions=True)
            self._stdout_task = None
        self.process = None

    def _load_or_create_token(self) -> str:
        env_name = str(self.cfg.get("token_env", "YOUTUBE_REMOTE_CIPHER_PASSWORD"))
        supplied = os.getenv(env_name)
        if supplied:
            return supplied
        if self.token_file.exists():
            token = self.token_file.read_text("utf-8").strip()
            if token:
                return token
        token = secrets.token_urlsafe(36)
        self.token_file.write_text(token + "\n", "utf-8")
        try:
            os.chmod(self.token_file, 0o600)
        except OSError:
            pass
        done(log, "[Cipher] Generated local API token in %s", self.token_file)
        return token

    def _platform_asset(self) -> str:
        machine = platform.machine().lower()
        is_arm = machine in {"arm64", "aarch64"}
        if sys.platform == "win32":
            return "deno-aarch64-pc-windows-msvc.zip" if is_arm else "deno-x86_64-pc-windows-msvc.zip"
        if sys.platform.startswith("linux"):
            return "deno-aarch64-unknown-linux-gnu.zip" if is_arm else "deno-x86_64-unknown-linux-gnu.zip"
        if sys.platform == "darwin":
            return "deno-aarch64-apple-darwin.zip" if is_arm else "deno-x86_64-apple-darwin.zip"
        raise EmbeddedCipherError(f"Unsupported OS for automatic Deno bootstrap: {sys.platform}/{machine}")

    async def _ensure_deno(self) -> str:
        configured = str(self.cfg.get("deno_command", "")).strip()
        if configured:
            resolved = shutil.which(configured) or (configured if Path(configured).exists() else None)
            if resolved:
                return str(resolved)
        system_deno = shutil.which("deno")
        if system_deno:
            system(log, "[Cipher] Using system Deno: %s", system_deno)
            return system_deno

        exe = self.deno_dir / ("deno.exe" if sys.platform == "win32" else "deno")
        if exe.exists():
            return str(exe)
        if not bool(self.cfg.get("auto_download_deno", True)):
            raise EmbeddedCipherError("Deno not found and embedded_cipher.auto_download_deno=false")

        self.deno_dir.mkdir(parents=True, exist_ok=True)
        asset = self._platform_asset()
        url = str(self.cfg.get("deno_download_url", "https://github.com/denoland/deno/releases/latest/download/{asset}"))
        url = url.replace("{asset}", asset)
        archive = self.root / "deno.zip.part"
        loading(log, "[Cipher] Downloading private Deno runtime…")
        await self._download(url, archive, timeout=180)
        try:
            with zipfile.ZipFile(archive) as zf:
                _safe_extract_zip(zf, self.deno_dir)
        finally:
            archive.unlink(missing_ok=True)
        if not exe.exists():
            candidates = list(self.deno_dir.rglob("deno.exe" if sys.platform == "win32" else "deno"))
            if not candidates:
                raise EmbeddedCipherError("Deno archive did not contain a deno executable")
            candidates[0].replace(exe)
        if sys.platform != "win32":
            exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        proc = await asyncio.create_subprocess_exec(
            str(exe), "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await proc.communicate()
        first = out.decode("utf-8", "replace").splitlines()[0] if out else "Deno"
        done(log, "[Cipher] %s installed locally", first)
        return str(exe)

    async def _download(self, url: str, target: Path, *, timeout: float = 180) -> str:
        http = self.ctx.services.get("http")
        session = getattr(http, "session", None)
        if session is None:
            raise EmbeddedCipherError("Shared HTTP session is not available")
        target.parent.mkdir(parents=True, exist_ok=True)
        sha = hashlib.sha256()
        async with session.get(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                body = (await resp.text())[:300]
                raise EmbeddedCipherError(f"Download failed HTTP {resp.status}: {body}")
            with target.open("wb") as fh:
                async for chunk in resp.content.iter_chunked(1024 * 256):
                    fh.write(chunk)
                    sha.update(chunk)
        return sha.hexdigest()

    def _source_is_fresh(self) -> bool:
        if not self.marker_file.exists() or not (self.source_dir / "server.ts").exists():
            return False
        hours = float(self.cfg.get("source_refresh_hours", 24))
        if hours <= 0:
            return True
        age = time.time() - self.marker_file.stat().st_mtime
        return age < hours * 3600

    async def _ensure_source(self, *, force: bool = False) -> None:
        if not force and self._source_is_fresh():
            return
        source_url = str(self.cfg.get("source_archive_url", "https://github.com/kikkia/yt-cipher/archive/refs/heads/master.zip"))
        fallback_ejs_revision = str(self.cfg.get("ejs_revision", "cd4e87f52e87ab6d8b318fd3a817adda6fafa8dc"))
        ejs_url_template = str(self.cfg.get("ejs_archive_url", "https://github.com/yt-dlp/ejs/archive/{revision}.zip"))
        source_zip = self.root / "yt-cipher.zip.part"
        ejs_zip = self.root / "ejs.zip.part"
        staging = self.root / ".staging"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)

        loading(log, "[Cipher] Fetching yt-cipher source…")
        await self._download(source_url, source_zip, timeout=180)
        try:
            yt_extract = staging / "yt"
            yt_extract.mkdir()
            with zipfile.ZipFile(source_zip) as zf:
                _safe_extract_zip(zf, yt_extract)
            yt_roots = [p for p in yt_extract.iterdir() if p.is_dir()]
            if len(yt_roots) != 1:
                raise EmbeddedCipherError("Unexpected yt-cipher archive layout")

            # Upstream documents the ejs commit it currently expects. Discover it
            # from README so our periodic source refresh does not keep a stale pin.
            readme = yt_roots[0] / "README.md"
            ejs_revision = fallback_ejs_revision
            if readme.exists():
                match = re.search(r"git\s+checkout\s+([0-9a-f]{40})", readme.read_text("utf-8", errors="replace"), re.I)
                if match:
                    ejs_revision = match.group(1)
            log.info("[Cipher] yt-dlp/ejs revision: %s", ejs_revision[:12])
            ejs_url = ejs_url_template.replace("{revision}", ejs_revision)
            await self._download(ejs_url, ejs_zip, timeout=180)

            ejs_extract = staging / "ejs"
            ejs_extract.mkdir()
            with zipfile.ZipFile(ejs_zip) as zf:
                _safe_extract_zip(zf, ejs_extract)
            ejs_roots = [p for p in ejs_extract.iterdir() if p.is_dir()]
            if len(ejs_roots) != 1:
                raise EmbeddedCipherError("Unexpected ejs archive layout")

            shutil.rmtree(self.source_dir, ignore_errors=True)
            shutil.copytree(yt_roots[0], self.source_dir)
            shutil.copytree(ejs_roots[0], self.source_dir / "ejs")
        finally:
            source_zip.unlink(missing_ok=True)
            ejs_zip.unlink(missing_ok=True)
            shutil.rmtree(staging, ignore_errors=True)

        deno = self.deno_command or await self._ensure_deno()
        patch = self.source_dir / "scripts" / "patch-ejs.ts"
        if not patch.exists():
            raise EmbeddedCipherError("yt-cipher patch script was not found")
        loading(log, "[Cipher] Patching yt-dlp/ejs…")
        proc = await asyncio.create_subprocess_exec(
            deno,
            "run",
            "--allow-read",
            "--allow-write",
            str(patch),
            cwd=str(self.source_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            text = out.decode("utf-8", "replace")[-4000:]
            raise EmbeddedCipherError(f"yt-cipher ejs patch failed ({proc.returncode}):\n{text}")
        self.marker_file.write_text(
            f"source=master\nejs={ejs_revision}\nupdated={int(time.time())}\n", "utf-8"
        )
        done(log, "[Cipher] yt-cipher source prepared locally")

    def _choose_port(self) -> int:
        desired = int(self.cfg.get("port", 8001))
        if desired > 0 and self._port_free(desired):
            return desired
        if desired > 0 and not bool(self.cfg.get("auto_port_fallback", True)):
            raise EmbeddedCipherError(f"Cipher port {desired} is already in use")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((self.host, 0))
            return int(sock.getsockname()[1])

    def _port_free(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((self.host, port))
                return True
            except OSError:
                return False

    async def _spawn(self) -> None:
        self._stopping = False
        self.port = self._choose_port() if not (self.process and self.process.returncode is None) else self.port
        deno = self.deno_command or await self._ensure_deno()
        server = self.source_dir / "server.ts"
        if not server.exists():
            raise EmbeddedCipherError("yt-cipher server.ts is missing")
        env = os.environ.copy()
        env.update({
            "HOST": self.host,
            "PORT": str(self.port),
            "API_TOKEN": self.token,
            "OVERRIDE_SCRIPT_VARIANT": str(self.cfg.get("override_script_variant", "IAS")),
            "PREPROCESSED_CACHE_SIZE": str(self.cfg.get("cache_size", 100)),
            "MAX_THREADS": str(self.cfg.get("max_threads", 2)),
        })
        if bool(self.cfg.get("ignore_script_region", False)):
            env["IGNORE_SCRIPT_REGION"] = "true"
        command = [
            deno,
            "run",
            "--allow-net",
            "--allow-read",
            "--allow-write",
            "--allow-env",
            str(server),
        ]
        loading(log, "[Cipher] Starting local yt-cipher on %s:%s…", self.host, self.port)
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(asyncio.subprocess, "CREATE_NO_WINDOW", 0)
        self.process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(self.source_dir),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            creationflags=creationflags,
        )
        self._stdout_task = asyncio.create_task(self._pump_stdout(), name="music:yt-cipher-stdout")

    async def _pump_stdout(self) -> None:
        proc = self.process
        if not proc or not proc.stdout:
            return
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            self._recent_logs.append(line)
            lowered = line.lower()
            if "error" in lowered or "exception" in lowered:
                log.error("[Cipher] %s", line)
            elif "warn" in lowered:
                log.warning("[Cipher] %s", line)
            else:
                log.info("[Cipher] %s", line)

    async def wait_ready(self) -> None:
        timeout = float(self.cfg.get("startup_timeout_seconds", 60))
        deadline = asyncio.get_running_loop().time() + timeout
        last_error = "not started"
        while asyncio.get_running_loop().time() < deadline:
            if self.process and self.process.returncode is not None:
                tail = "\n".join(list(self._recent_logs)[-20:])
                raise EmbeddedCipherError(
                    f"yt-cipher exited with code {self.process.returncode}. Last logs:\n{tail}"
                )
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port), timeout=1.5
                )
                writer.close()
                await writer.wait_closed()
                done(log, "[Cipher] Local yt-cipher ready • %s", self.uri)
                return
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(0.5)
        raise EmbeddedCipherError(f"yt-cipher readiness timeout ({last_error})")

    async def _watchdog(self) -> None:
        base = float(self.cfg.get("restart_delay_seconds", 3))
        attempts = 0
        while not self._stopping:
            proc = self.process
            if not proc:
                await asyncio.sleep(1)
                continue
            code = await proc.wait()
            if self._stopping:
                return
            attempts += 1
            delay = min(30.0, base * attempts)
            log.error("[Cipher] Process exited code=%s; restart in %.1fs", code, delay)
            await asyncio.sleep(delay)
            try:
                await self.restart()
                attempts = 0
            except Exception:
                log.exception("[Cipher] Automatic restart failed")

    def recent_logs(self, count: int = 30) -> list[str]:
        return list(self._recent_logs)[-max(1, min(count, 100)) :]

    async def doctor(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "cipher_process": "up" if self.process and self.process.returncode is None else "down",
            "cipher_pid": self.process.pid if self.process and self.process.returncode is None else None,
            "cipher_uri": self.uri,
            "deno": self.deno_command,
            "cipher_source": (self.source_dir / "server.ts").exists(),
            "cipher_token": bool(self.token),
            "cipher_variant": str(self.cfg.get("override_script_variant", "IAS")),
        }
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), timeout=1.5)
            writer.close(); await writer.wait_closed()
            result["cipher_tcp"] = "ok"
        except Exception as exc:
            result["cipher_tcp"] = f"{type(exc).__name__}: {exc}"
        return result


class EmbeddedLavalinkRuntime:
    """Owns local yt-cipher + Lavalink processes for the music plugin."""

    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self.ctx = plugin.ctx
        self.cfg = self.ctx.config.get("embedded_lavalink", {})
        self.root = (self.ctx.root / str(self.cfg.get("runtime_dir", "runtime/lavalink"))).resolve()
        self.jar = self.root / "Lavalink.jar"
        self.application_yml = self.root / "application.yml"
        self.secret_file = self.root / ".password"
        self.plugins_dir = self.root / "plugins"
        self.process: asyncio.subprocess.Process | None = None
        self._stdout_task: asyncio.Task | None = None
        self._log_file = None
        self._watchdog_task: asyncio.Task | None = None
        self._stopping = False
        self._recent_logs: deque[str] = deque(maxlen=int(self.cfg.get("recent_log_lines", 120)))
        self._restart_lock = asyncio.Lock()
        self.password = ""
        self.port = int(self.cfg.get("port", 2333))
        self.host = str(self.cfg.get("host", "127.0.0.1"))
        self.cipher: EmbeddedCipherRuntime | None = None
        self.youtube_backend = str(self.cfg.get("youtube_backend", "ytdlp")).strip().lower()
        self.ytdlp_path: Path | None = None
        self.ytdlp_deno_path: Path | None = None
        self.ytdlp_auth_mode: str = "none"
        self.ytdlp_auth_source: str | None = None
        self.ytdlp_extra_args: list[str] = []

    @property
    def uri(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _env(self, name: str, default: str = "") -> str:
        value = os.getenv(name)
        return value if value is not None else default

    def _bool_env(self, name: str, default: bool = False) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    async def start(self) -> None:
        if not bool(self.cfg.get("enabled", True)):
            raise EmbeddedLavalinkError("embedded_lavalink.enabled=false")
        self.root.mkdir(parents=True, exist_ok=True)
        self.password = self._load_or_create_password()
        try:
            await self._ensure_java()
            await self._ensure_jar()
            await self._ensure_plugin_jars()

            if self.youtube_backend == "ytdlp":
                self.ytdlp_path = await self._ensure_ytdlp()
                self.ytdlp_deno_path = await self._ensure_ytdlp_deno()
                self.ytdlp_extra_args = self._build_ytdlp_runtime_args()
                done(log, "[Music] YouTube backend ready • yt-dlp %s", self.ytdlp_path)
                system(
                    log,
                    "[Music] YouTube runtime • auth=%s • js=deno • ejs=npm",
                    self.ytdlp_auth_mode,
                )
            elif self.youtube_backend == "youtube_source":
                # youtube-source remains available as a fallback backend. Its remote
                # cipher only solves player-script signatures; it does not solve
                # YouTube's current SABR-only responses on its own.
                if not self._env("YOUTUBE_REMOTE_CIPHER_URL") and bool(
                    self.ctx.config.get("embedded_cipher", {}).get("enabled", True)
                ):
                    self.cipher = EmbeddedCipherRuntime(self.plugin)
                    await self.cipher.start()
            else:
                raise EmbeddedLavalinkError(
                    f"Unknown embedded_lavalink.youtube_backend={self.youtube_backend!r}; "
                    "expected 'ytdlp' or 'youtube_source'"
                )

            self._write_application_yml()
            await self._spawn()
            await self.wait_ready()
            if bool(self.cfg.get("watchdog", True)):
                self._watchdog_task = asyncio.create_task(self._watchdog(), name="music:lavalink-watchdog")
        except BaseException:
            # Startup is transactional: a half-started Java/Deno process must never
            # leak when the plugin fails inside discord.py setup_hook.
            await self.stop()
            raise

    async def stop(self) -> None:
        self._stopping = True
        if self._watchdog_task:
            self._watchdog_task.cancel()
            await asyncio.gather(self._watchdog_task, return_exceptions=True)
            self._watchdog_task = None
        await self._stop_lavalink()
        if self.cipher:
            await self.cipher.stop()
            self.cipher = None

    async def _stop_lavalink(self) -> None:
        proc = self.process
        if proc:
            if proc.returncode is None:
                loading(log, "[Lavalink] Stopping embedded node…")
                proc.terminate()
                # Java на Windows иногда игнорирует мягкое завершение достаточно долго.
                # Даю ему короткое окно, а затем завершаю дерево процесса без огромного traceback.
                graceful_timeout = min(float(self.cfg.get("shutdown_timeout_seconds", 10)), 4.0)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=graceful_timeout)
                except asyncio.TimeoutError:
                    log.warning("[Lavalink] Узел не завершился за %.0f с • принудительно останавливаю процесс", graceful_timeout)
                    if os.name == "nt" and getattr(proc, "pid", None):
                        killer = await asyncio.create_subprocess_exec(
                            "taskkill", "/PID", str(proc.pid), "/T", "/F",
                            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                        )
                        try:
                            await asyncio.wait_for(killer.wait(), timeout=3.0)
                        except asyncio.TimeoutError:
                            killer.kill()
                    if proc.returncode is None:
                        proc.kill()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=3.0)
                    except asyncio.TimeoutError:
                        log.warning("[Lavalink] Процесс уже передан ОС на завершение • продолжаю shutdown Quiddy")
            else:
                await proc.wait()
        if self._stdout_task:
            try:
                await asyncio.wait_for(asyncio.shield(self._stdout_task), timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._stdout_task.cancel()
                await asyncio.gather(self._stdout_task, return_exceptions=True)
            self._stdout_task = None
        if self._log_file is not None:
            try:
                self._log_file.flush()
            finally:
                self._log_file.close()
                self._log_file = None
        self.process = None

    async def restart(self) -> None:
        async with self._restart_lock:
            await self._stop_lavalink()
            self._write_application_yml()
            await self._spawn()
            await self.wait_ready()

    async def restart_cipher(self, *, refresh: bool = False) -> None:
        if not self.cipher:
            raise EmbeddedCipherError("Embedded cipher is not active")
        if refresh:
            await self.cipher.refresh_source()
        else:
            await self.cipher.restart()

    async def _ensure_ytdlp(self) -> Path:
        """Install a private yt-dlp nightly executable used by LavaSrc.

        YouTube changes too quickly for a release-pinned extractor to be a good
        playback primitive. The yt-dlp project itself recommends nightly builds
        for regular users, so Quiddy keeps a private, refreshable copy instead of
        depending on a machine-global install.
        """
        cfg = self.ctx.config.get("embedded_ytdlp", {}) or {}
        if not bool(cfg.get("enabled", True)):
            configured = str(cfg.get("path", "")).strip()
            resolved = shutil.which(configured or "yt-dlp")
            if not resolved:
                raise EmbeddedLavalinkError("yt-dlp backend enabled but no yt-dlp executable is available")
            return Path(resolved).resolve()

        runtime_dir = (self.ctx.root / str(cfg.get("runtime_dir", "runtime/yt-dlp"))).resolve()
        runtime_dir.mkdir(parents=True, exist_ok=True)
        system_name = platform.system().lower()
        machine = platform.machine().lower()
        if system_name == "windows":
            asset = "yt-dlp.exe"
        elif system_name == "darwin":
            asset = "yt-dlp_macos"
        elif system_name == "linux":
            # The platform-independent zipapp works anywhere Quiddy's Python does,
            # including Debian/Ubuntu x86_64 and aarch64.
            asset = "yt-dlp"
        else:
            raise EmbeddedLavalinkError(f"Unsupported platform for embedded yt-dlp: {system_name}/{machine}")

        target = runtime_dir / asset
        marker = runtime_dir / ".downloaded-at"
        refresh_hours = max(1.0, float(cfg.get("refresh_hours", 12)))
        refresh_due = True
        if target.exists() and marker.exists():
            try:
                refresh_due = (time.time() - float(marker.read_text("utf-8").strip())) >= refresh_hours * 3600
            except (ValueError, OSError):
                refresh_due = True

        if refresh_due:
            template = str(cfg.get(
                "download_url",
                "https://github.com/yt-dlp/yt-dlp-nightly-builds/releases/latest/download/{asset}",
            ))
            url = template.replace("{asset}", asset)
            http = self.ctx.services.get("http")
            session = getattr(http, "session", None)
            if session is None:
                raise EmbeddedLavalinkError("Shared HTTP session is not available")
            tmp = target.with_suffix(target.suffix + ".part")
            loading(log, "[yt-dlp] Downloading latest nightly build…")
            try:
                async with session.get(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=180)) as resp:
                    if resp.status != 200:
                        body = (await resp.text())[:300]
                        raise EmbeddedLavalinkError(f"yt-dlp download failed HTTP {resp.status}: {body}")
                    with tmp.open("wb") as fh:
                        async for chunk in resp.content.iter_chunked(1024 * 256):
                            fh.write(chunk)
                if tmp.stat().st_size < 100_000:
                    raise EmbeddedLavalinkError(f"yt-dlp download is unexpectedly small ({tmp.stat().st_size} bytes)")
                tmp.replace(target)
                if system_name != "windows":
                    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                marker.write_text(str(time.time()), "utf-8")
            except Exception:
                tmp.unlink(missing_ok=True)
                if not target.exists():
                    raise
                log.warning("[yt-dlp] Refresh failed; using existing private build", exc_info=True)

        if system_name != "windows":
            target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        proc = await asyncio.create_subprocess_exec(
            str(target), "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await proc.communicate()
        version = out.decode("utf-8", "replace").strip().splitlines()
        if proc.returncode != 0:
            raise EmbeddedLavalinkError(f"yt-dlp executable failed self-check: {out.decode('utf-8', 'replace')[:500]}")
        done(log, "[yt-dlp] %s ready", version[0] if version else "nightly")
        return target

    async def _ensure_ytdlp_deno(self) -> Path:
        """Return a Deno executable for yt-dlp's EJS challenge solver.

        Reuse the exact same private Deno bootstrap as the embedded yt-cipher
        runtime. We intentionally do not start yt-cipher in yt-dlp mode; only
        the Deno executable is needed by yt-dlp.
        """
        cfg = self.ctx.config.get("embedded_ytdlp", {}) or {}
        configured = self._env("YOUTUBE_DENO_PATH", str(cfg.get("deno_path", "")).strip())
        if configured:
            candidate = Path(configured).expanduser()
            resolved = shutil.which(str(candidate)) or (str(candidate.resolve()) if candidate.exists() else None)
            if resolved:
                return Path(resolved)
            raise EmbeddedLavalinkError(f"Configured YOUTUBE_DENO_PATH does not exist: {configured}")

        # Prefer an already bootstrapped Quiddy Deno, then a machine-global one.
        cipher_cfg = self.ctx.config.get("embedded_cipher", {}) or {}
        cipher_root = (self.ctx.root / str(cipher_cfg.get("runtime_dir", "runtime/yt-cipher"))).resolve()
        embedded = cipher_root / "tools" / "deno" / ("deno.exe" if sys.platform == "win32" else "deno")
        if embedded.exists():
            return embedded
        system_deno = shutil.which("deno")
        if system_deno:
            return Path(system_deno).resolve()

        # Bootstrap only Deno; do not fetch/start yt-cipher.
        helper = EmbeddedCipherRuntime(self.plugin)
        helper.root.mkdir(parents=True, exist_ok=True)
        helper.tools_dir.mkdir(parents=True, exist_ok=True)
        deno = await helper._ensure_deno()
        return Path(deno).resolve()

    def _detect_waterfox_profile(self) -> Path | None:
        """Find the most recently used Waterfox profile containing cookies.sqlite."""
        if sys.platform != "win32":
            return None
        appdata = os.getenv("APPDATA")
        if not appdata:
            return None
        profiles = Path(appdata) / "Waterfox" / "Profiles"
        if not profiles.is_dir():
            return None
        candidates: list[tuple[float, Path]] = []
        for profile in profiles.iterdir():
            if not profile.is_dir():
                continue
            cookies = profile / "cookies.sqlite"
            if not cookies.exists():
                continue
            try:
                stamp = max(cookies.stat().st_mtime, profile.stat().st_mtime)
            except OSError:
                stamp = 0.0
            candidates.append((stamp, profile.resolve()))
        return max(candidates, key=lambda item: item[0])[1] if candidates else None

    def _build_ytdlp_runtime_args(self) -> list[str]:
        """Build safe yt-dlp args for YouTube auth + JS challenge solving.

        The generated Lavalink config never contains cookie values, only either
        a local browser profile path or a cookie-file path. No cookie contents
        are logged.
        """
        cfg = self.ctx.config.get("embedded_ytdlp", {}) or {}
        args: list[str] = []

        if self.ytdlp_deno_path:
            args += ["--js-runtimes", f"deno:{self.ytdlp_deno_path}"]
        if bool(cfg.get("remote_ejs", True)):
            args += ["--remote-components", str(cfg.get("remote_ejs_component", "ejs:npm"))]

        requested_mode = self._env("YOUTUBE_AUTH_MODE", str(cfg.get("auth_mode", "auto"))).strip().lower()
        browser = self._env("YOUTUBE_BROWSER", str(cfg.get("browser", "firefox"))).strip() or "firefox"
        browser_profile_raw = self._env(
            "YOUTUBE_BROWSER_PROFILE", str(cfg.get("browser_profile", "")).strip()
        ).strip()
        cookies_file_raw = self._env(
            "YOUTUBE_COOKIES_FILE", str(cfg.get("cookies_file", "runtime/auth/youtube-cookies.txt")).strip()
        ).strip()
        cookies_file = None
        if cookies_file_raw:
            candidate = Path(cookies_file_raw).expanduser()
            cookies_file = candidate if candidate.is_absolute() else (self.ctx.root / candidate).resolve()

        mode = requested_mode
        profile: Path | None = Path(browser_profile_raw).expanduser().resolve() if browser_profile_raw else None
        if mode == "auto":
            # For development on Windows prefer the live Waterfox profile, which
            # mirrors the CLI invocation validated against YouTube. On headless
            # hosts prefer a Netscape-format cookie file when present.
            if profile and (profile / "cookies.sqlite").exists():
                mode = "browser"
            else:
                detected = self._detect_waterfox_profile()
                if detected:
                    profile = detected
                    mode = "browser"
                elif cookies_file and cookies_file.is_file():
                    mode = "cookies_file"
                else:
                    mode = "none"

        if mode == "browser":
            if profile is None:
                profile = self._detect_waterfox_profile()
            if profile is None or not (profile / "cookies.sqlite").exists():
                raise EmbeddedLavalinkError(
                    "YouTube browser auth selected but no browser profile with cookies.sqlite was found. "
                    "Set YOUTUBE_BROWSER_PROFILE or switch YOUTUBE_AUTH_MODE=cookies_file."
                )
            args += ["--cookies-from-browser", f"{browser}:{profile}"]
            self.ytdlp_auth_mode = "browser"
            self.ytdlp_auth_source = str(profile)
        elif mode == "cookies_file":
            if not cookies_file or not cookies_file.is_file():
                raise EmbeddedLavalinkError(
                    "YouTube cookies_file auth selected but the file does not exist. "
                    "Set YOUTUBE_COOKIES_FILE to a Netscape-format cookies.txt file."
                )
            args += ["--cookies", str(cookies_file)]
            self.ytdlp_auth_mode = "cookies_file"
            self.ytdlp_auth_source = str(cookies_file)
        elif mode in {"none", "off", "disabled"}:
            self.ytdlp_auth_mode = "none"
            self.ytdlp_auth_source = None
            if requested_mode != "none":
                log.warning(
                    "[Music] No YouTube authentication source found; YouTube may require sign-in. "
                    "Set YOUTUBE_BROWSER_PROFILE (dev) or YOUTUBE_COOKIES_FILE (server)."
                )
        else:
            raise EmbeddedLavalinkError(
                f"Unknown embedded_ytdlp.auth_mode={requested_mode!r}; expected auto/browser/cookies_file/none"
            )
        return args

    async def _ensure_java(self) -> None:
        java = shutil.which(str(self.cfg.get("java_command", "java")))
        if not java:
            raise EmbeddedLavalinkError("Java not found. Install Java 21+ and make `java` available in PATH.")
        proc = await asyncio.create_subprocess_exec(
            java, "-version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await proc.communicate()
        text = out.decode("utf-8", "replace").strip().splitlines()
        system(log, "[Lavalink] %s", text[0] if text else "java version unknown")

    async def _ensure_jar(self) -> None:
        version = str(self.cfg.get("version", "4.2.2"))
        url = str(self.cfg.get("download_url", f"https://github.com/lavalink-devs/Lavalink/releases/download/{version}/Lavalink.jar"))
        url = url.replace("{version}", version)
        marker = self.root / ".lavalink-version"
        current = marker.read_text("utf-8").strip() if marker.exists() else ""
        if self.jar.exists() and current == version:
            return
        if not bool(self.cfg.get("auto_download", True)):
            raise EmbeddedLavalinkError(f"{self.jar} is missing and auto_download=false")
        loading(log, "[Lavalink] Downloading Lavalink %s…", version)
        http = self.ctx.services.get("http")
        session = getattr(http, "session", None)
        if session is None:
            raise EmbeddedLavalinkError("Shared HTTP session is not available")
        tmp = self.jar.with_suffix(".jar.part")
        sha = hashlib.sha256()
        async with session.get(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=180)) as resp:
            if resp.status != 200:
                body = (await resp.text())[:300]
                raise EmbeddedLavalinkError(f"Lavalink download failed HTTP {resp.status}: {body}")
            with tmp.open("wb") as fh:
                async for chunk in resp.content.iter_chunked(1024 * 256):
                    fh.write(chunk); sha.update(chunk)
        tmp.replace(self.jar)
        marker.write_text(version, "utf-8")
        done(log, "[Lavalink] Lavalink %s downloaded • sha256=%s…", version, sha.hexdigest()[:16])

    async def _download_plugin_jar(self, *, url: str, target: Path, label: str) -> None:
        http = self.ctx.services.get("http")
        session = getattr(http, "session", None)
        if session is None:
            raise EmbeddedLavalinkError("Shared HTTP session is not available")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".part")
        sha = hashlib.sha256()
        loading(log, "[Lavalink] Downloading %s…", label)
        try:
            async with session.get(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=180)) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:300]
                    raise EmbeddedLavalinkError(f"{label} download failed HTTP {resp.status}: {body}")
                with tmp.open("wb") as fh:
                    async for chunk in resp.content.iter_chunked(1024 * 256):
                        fh.write(chunk)
                        sha.update(chunk)
            if tmp.stat().st_size < 10_000:
                raise EmbeddedLavalinkError(f"{label} download is unexpectedly small ({tmp.stat().st_size} bytes)")
            tmp.replace(target)
            done(log, "[Lavalink] %s ready • sha256=%s…", label, sha.hexdigest()[:16])
        finally:
            tmp.unlink(missing_ok=True)

    async def _ensure_plugin_jars(self) -> None:
        """Install exact plugin jars before Java starts.

        Lavalink 4.1+ has an update checker. On Windows, letting Lavalink both load
        and replace a plugin can hit a file-lock race (the loaded JAR cannot be
        deleted). Quiddy therefore owns plugin installation and leaves
        ``lavalink.plugins`` empty. Lavalink only loads the exact jars from
        ``pluginsDir``.
        """
        if not bool(self.cfg.get("manage_plugin_jars", True)):
            return
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        youtube_version = str(self.cfg.get("youtube_source_version", "1.18.2"))
        lavasrc_version = str(self.cfg.get("lavasrc_version", "4.8.3"))
        specs = (
            (
                "youtube-plugin",
                youtube_version,
                str(self.cfg.get(
                    "youtube_plugin_url",
                    "https://maven.lavalink.dev/releases/dev/lavalink/youtube/youtube-plugin/{version}/youtube-plugin-{version}.jar",
                )),
            ),
            (
                "lavasrc-plugin",
                lavasrc_version,
                str(self.cfg.get(
                    "lavasrc_plugin_url",
                    "https://maven.lavalink.dev/releases/com/github/topi314/lavasrc/lavasrc-plugin/{version}/lavasrc-plugin-{version}.jar",
                )),
            ),
        )
        for prefix, version, template in specs:
            wanted = self.plugins_dir / f"{prefix}-{version}.jar"
            # Remove stale versions *before* Java exists, avoiding Windows JAR locks.
            for stale in self.plugins_dir.glob(f"{prefix}-*.jar"):
                if stale.name == wanted.name:
                    continue
                try:
                    stale.unlink()
                    log.info("[Lavalink] Removed stale plugin %s", stale.name)
                except OSError as exc:
                    raise EmbeddedLavalinkError(
                        f"Cannot remove stale plugin {stale}. Close any old Lavalink/Java process and retry: {exc}"
                    ) from exc
            if not wanted.exists() or wanted.stat().st_size < 10_000:
                wanted.unlink(missing_ok=True)
                await self._download_plugin_jar(
                    url=template.replace("{version}", version),
                    target=wanted,
                    label=f"{prefix} {version}",
                )

    def _load_or_create_password(self) -> str:
        env_name = str(self.cfg.get("password_env", "LAVALINK_PASSWORD"))
        supplied = os.getenv(env_name)
        if supplied:
            return supplied
        if self.secret_file.exists():
            value = self.secret_file.read_text("utf-8").strip()
            if value:
                return value
        value = secrets.token_urlsafe(36)
        self.secret_file.write_text(value + "\n", "utf-8")
        try:
            os.chmod(self.secret_file, 0o600)
        except OSError:
            pass
        done(log, "[Lavalink] Generated local node password in %s", self.secret_file)
        return value

    def _choose_port(self) -> int:
        desired = int(self.cfg.get("port", 2333))
        if desired > 0 and self._port_free(desired):
            return desired
        if desired > 0 and not bool(self.cfg.get("auto_port_fallback", True)):
            raise EmbeddedLavalinkError(f"Port {desired} is already in use")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((self.host, 0)); return int(sock.getsockname()[1])

    def _port_free(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((self.host, port)); return True
            except OSError:
                return False

    def _write_application_yml(self) -> None:
        self.port = self._choose_port() if not (self.process and self.process.returncode is None) else self.port
        yt_refresh = self._env("YOUTUBE_REFRESH_TOKEN")
        yt_po = self._env("YOUTUBE_PO_TOKEN")
        yt_visitor = self._env("YOUTUBE_VISITOR_DATA")
        cipher_url = self._env("YOUTUBE_REMOTE_CIPHER_URL") or (self.cipher.uri if self.cipher else "")
        cipher_password = self._env("YOUTUBE_REMOTE_CIPHER_PASSWORD") or (self.cipher.token if self.cipher else "")
        oauth_enabled = self._bool_env("YOUTUBE_OAUTH_ENABLED", bool(self.cfg.get("youtube_oauth", False)))

        youtube_version = str(self.cfg.get("youtube_source_version", "1.18.2"))
        lavasrc_version = str(self.cfg.get("lavasrc_version", "4.8.3"))
        clients = list(self.cfg.get("youtube_clients", [
            "MUSIC", "MWEB", "TVHTML5_SIMPLY", "ANDROID_MUSIC", "IOS",
            "WEB", "ANDROID_VR", "WEBEMBEDDED",
        ]))
        youtube_source_enabled = self.youtube_backend == "youtube_source"
        if youtube_source_enabled and oauth_enabled and "TV" not in clients:
            clients.append("TV")
            log.info("[Lavalink] YouTube OAuth enabled: added TV playback client")

        data: dict[str, Any] = {
            "server": {"port": self.port, "address": self.host},
            "lavalink": {
                # Quiddy manages exact plugin JARs itself. Keeping this list empty
                # disables Lavalink's download/update path and avoids Windows JAR
                # replacement races during startup.
                "plugins": [],
                "pluginsDir": "./plugins",
                "server": {
                    "password": self.password,
                    "sources": {
                        "youtube": False, "bandcamp": True, "soundcloud": True, "twitch": True,
                        "vimeo": True, "http": True, "local": False,
                    },
                    "filters": {
                        "volume": True, "equalizer": True, "karaoke": True, "timescale": True,
                        "tremolo": True, "vibrato": True, "distortion": True, "rotation": True,
                        "channelMix": True, "lowPass": True,
                    },
                    "bufferDurationMs": 400,
                    "frameBufferDurationMs": 5000,
                    "opusEncodingQuality": 10,
                    "resamplingQuality": "LOW",
                    "trackStuckThresholdMs": 10000,
                    "useSeekGhosting": True,
                    "youtubePlaylistLoadLimit": int(self.cfg.get("youtube_playlist_page_limit", 6)),
                    "playerUpdateInterval": 5,
                    "youtubeSearchEnabled": True,
                    "soundcloudSearchEnabled": True,
                    "gc-warnings": True,
                    "timeouts": {
                        "connectTimeoutMs": int(self.cfg.get("connect_timeout_ms", 10000)),
                        "connectionRequestTimeoutMs": int(self.cfg.get("connection_request_timeout_ms", 10000)),
                        "socketTimeoutMs": int(self.cfg.get("socket_timeout_ms", 10000)),
                    },
                },
            },
            "plugins": {
                "youtube": {
                    "enabled": youtube_source_enabled,
                    "allowSearch": True,
                    "allowDirectVideoIds": True,
                    "allowDirectPlaylistIds": True,
                    "clients": clients,
                },
                "lavasrc": {
                    "providers": ["ytsearch:\"%ISRC%\"", "ytsearch:%QUERY%", "scsearch:%QUERY%"],
                    "sources": {
                        "applemusic": False,
                        "deezer": False, "yandexmusic": False, "flowerytts": False, "youtube": False,
                        "vkmusic": False, "tidal": False, "qobuz": False,
                        "ytdlp": self.youtube_backend == "ytdlp", "jiosaavn": False,
                    },
                    "lyrics-sources": {
                        "deezer": False, "youtube": True, "yandexmusic": False,
                        "vkmusic": False, "lrcLib": True,
                    },
                    "ytdlp": {
                        "path": str(self.ytdlp_path) if self.ytdlp_path else "yt-dlp",
                        "searchLimit": 10,
                        "mixPlaylistLoadLimit": 25,
                        "playlistLoadLimit": int(self.cfg.get("ytdlp_playlist_load_limit", 200)),
                        "customLoadArgs": [
                            "-q", "--no-warnings", "--flat-playlist", "--skip-download", "-J",
                            *self.ytdlp_extra_args,
                        ],
                        "customPlaybackArgs": [
                            "-q", "--no-warnings", "-f", "bestaudio/best", "-J",
                            *self.ytdlp_extra_args,
                        ],
                    },
                },
            },
            "logging": {"level": {
                "root": str(self.cfg.get("lavalink_log_level", "INFO")),
                "lavalink": "INFO",
                "dev.lavalink.youtube.http.YoutubeOauth2Handler": "INFO",
            }},
            "metrics": {"prometheus": {"enabled": False, "endpoint": "/metrics"}},
        }
        yt = data["plugins"]["youtube"]
        if youtube_source_enabled and oauth_enabled:
            oauth: dict[str, Any] = {"enabled": True}
            if yt_refresh:
                oauth["refreshToken"] = yt_refresh
                oauth["skipInitialization"] = True
            yt["oauth"] = oauth
        elif youtube_source_enabled and yt_po and yt_visitor:
            yt["pot"] = {"token": yt_po, "visitorData": yt_visitor}
        if youtube_source_enabled and cipher_url:
            yt["remoteCipher"] = {
                "url": cipher_url,
                "password": cipher_password,
                "userAgent": "QuiddyNetwork",
            }
            system(log, "[Lavalink] YouTube remote cipher: %s", cipher_url)
        self.application_yml.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), "utf-8")

    async def _spawn(self) -> None:
        self._stopping = False
        java = shutil.which(str(self.cfg.get("java_command", "java"))) or "java"
        xms = str(self.cfg.get("xms", "128M")); xmx = str(self.cfg.get("xmx", "512M"))
        command = [java, f"-Xms{xms}", f"-Xmx{xmx}", "-XX:+UseG1GC", "-XX:+UseStringDeduplication", "-jar", str(self.jar)]
        loading(log, "[Lavalink] Starting embedded node on %s:%s…", self.host, self.port)
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(asyncio.subprocess, "CREATE_NO_WINDOW", 0)
        # На Windows я не держу stdout Lavalink через asyncio PIPE. Proactor создаёт
        # отдельный pipe transport, который при жёсткой остановке Java может дожить до
        # закрытия event loop и испортить чистый shutdown сообщением из __del__.
        # Обычный файловый дескриптор этого transport не создаёт и заодно сохраняет
        # полный лог движка для диагностики.
        log_path = self.root / "lavalink-runtime.log"
        self._log_file = log_path.open("ab", buffering=0)
        self.process = await asyncio.create_subprocess_exec(
            *command, cwd=str(self.root), stdout=self._log_file,
            stderr=asyncio.subprocess.STDOUT, creationflags=creationflags,
        )

    async def _pump_stdout(self) -> None:
        proc = self.process
        if not proc or not proc.stdout:
            return
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            self._recent_logs.append(line)
            lowered = line.lower()
            # Сырые Spring/Undertow логи полезны в файл, но в консоли от них невозможно что-либо понять.
            if "error" in lowered or "exception" in lowered:
                log.error("Аудио-движок │ %s", line)
            elif "warn" in lowered and "turning off sentry" not in lowered and "buffer pool was not set" not in lowered:
                log.warning("Аудио-движок │ %s", line)
            elif "lavalink is ready to accept connections" in lowered:
                done(log, "Аудио-движок готов принимать подключения")
            elif "loaded 'lavasrc-plugin" in lowered:
                done(log, "LavaSrc загружен")
            elif "loaded 'youtube-plugin" in lowered:
                log.debug("YouTube plugin загружен: %s", line)
            elif "registering ytdlp audio source" in lowered:
                done(log, "yt-dlp зарегистрирован как источник аудио")
            elif "loaded track " in lowered:
                title = line.split("Loaded track ", 1)[-1]
                log.info("Трек подготовлен │ %s", title)
            elif "got request to load" in lowered or "requestloggingfilter" in lowered or "native library" in lowered or "initializ" in lowered or "started launcher" in lowered:
                log.debug("[Lavalink] %s", line)
            else:
                log.debug("[Lavalink] %s", line)

    async def wait_ready(self) -> None:
        timeout = float(self.cfg.get("startup_timeout_seconds", 90))
        deadline = asyncio.get_running_loop().time() + timeout
        http = self.ctx.services.get("http"); session = getattr(http, "session", None)
        if session is None:
            raise EmbeddedLavalinkError("Shared HTTP session is not available")
        headers = {"Authorization": self.password}; last_error = "not started"
        while asyncio.get_running_loop().time() < deadline:
            if self.process and self.process.returncode is not None:
                tail = "\n".join(list(self._recent_logs)[-20:])
                raise EmbeddedLavalinkError(f"Lavalink exited with code {self.process.returncode}. Last logs:\n{tail}")
            try:
                async with session.get(f"{self.uri}/version", headers=headers, timeout=aiohttp.ClientTimeout(total=2)) as resp:
                    if resp.status == 200:
                        version = (await resp.text()).strip()
                        done(log, "[Lavalink] Embedded node ready • %s • %s", version, self.uri)
                        return
                    last_error = f"HTTP {resp.status}"
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(0.75)
        raise EmbeddedLavalinkError(f"Lavalink readiness timeout ({last_error})")

    async def _watchdog(self) -> None:
        base = float(self.cfg.get("restart_delay_seconds", 3)); attempts = 0
        while not self._stopping:
            proc = self.process
            if not proc:
                await asyncio.sleep(1); continue
            code = await proc.wait()
            if self._stopping:
                return
            attempts += 1; delay = min(30.0, base * attempts)
            log.error("[Lavalink] Process exited code=%s; restart in %.1fs", code, delay)
            await asyncio.sleep(delay)
            try:
                await self.restart(); attempts = 0
            except Exception:
                log.exception("[Lavalink] Automatic restart failed")

    def recent_logs(self, count: int = 30) -> list[str]:
        return list(self._recent_logs)[-max(1, min(count, 100)) :]

    def recent_cipher_logs(self, count: int = 30) -> list[str]:
        return self.cipher.recent_logs(count) if self.cipher else []

    async def doctor(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "mode": "embedded",
            "lavalink_process": "down",
            "lavalink_pid": None,
            "lavalink_uri": self.uri,
            "java": shutil.which(str(self.cfg.get("java_command", "java"))),
            "jar_exists": self.jar.exists(),
            "youtube_oauth": self._bool_env("YOUTUBE_OAUTH_ENABLED", bool(self.cfg.get("youtube_oauth", False))),
            "youtube_refresh_token": bool(os.getenv("YOUTUBE_REFRESH_TOKEN")),
            "youtube_pot": bool(os.getenv("YOUTUBE_PO_TOKEN") and os.getenv("YOUTUBE_VISITOR_DATA")),
            "remote_cipher_external": bool(os.getenv("YOUTUBE_REMOTE_CIPHER_URL")),
            
            "youtube_backend": self.youtube_backend,
            "ytdlp_path": str(self.ytdlp_path) if self.ytdlp_path else None,
            "ytdlp_js_runtime": str(self.ytdlp_deno_path) if self.ytdlp_deno_path else None,
            "ytdlp_ejs": "npm" if self.youtube_backend == "ytdlp" else None,
            "ytdlp_auth_mode": self.ytdlp_auth_mode,
            "ytdlp_auth_source": self.ytdlp_auth_source,
        }
        if self.process and self.process.returncode is None:
            result["lavalink_process"] = "up"; result["lavalink_pid"] = self.process.pid
        if self.cipher:
            result.update(await self.cipher.doctor())
        elif os.getenv("YOUTUBE_REMOTE_CIPHER_URL"):
            result["cipher_uri"] = os.getenv("YOUTUBE_REMOTE_CIPHER_URL")
            result["cipher_process"] = "external"
        http = self.ctx.services.get("http"); session = getattr(http, "session", None)
        if session:
            try:
                async with session.get(
                    f"{self.uri}/version", headers={"Authorization": self.password},
                    timeout=aiohttp.ClientTimeout(total=3),
                ) as resp:
                    result["lavalink_http"] = resp.status
                    if resp.status == 200:
                        result["lavalink_version"] = (await resp.text()).strip()
            except Exception as exc:
                result["lavalink_http_error"] = f"{type(exc).__name__}: {exc}"
        return result
