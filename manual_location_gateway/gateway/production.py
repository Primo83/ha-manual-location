"""HAOS App launcher. Public TLS terminates in NGINX; control stays on loopback."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from dataclasses import dataclass, field
import hmac
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import ssl
from urllib.parse import urlsplit

from aiohttp import ClientError, web

from .home_assistant import NativeHooks
from .server import Gateway, _json, _production_origin

CONTROL_PORT = 18325
LIVE_PORT = 18326
LIVE_HOST = "172.30.32.1"
RUNTIME_DIR = Path("/tmp/manual-location-gateway")
CONFIG_DIR = Path("/homeassistant")
DATA_DIR = Path("/data")
OFFICIAL_PUSH_HOST = "mobile-apps.home-assistant.io"
LOGGER = logging.getLogger(__name__)


class ConfigurationError(ValueError):
    """Static message only: never include supplied options or stored records."""


def identifier(value, pattern):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ConfigurationError("INVALID_IDENTIFIER")
    return value


@dataclass(frozen=True)
class Options:
    public_url: str
    manual_entry_id: str
    protected_person: str
    protected_tracker: str
    protected_webhook: str = field(repr=False)
    control_token: str = field(repr=False)
    passthrough_webhooks: tuple[str, ...] = field(default=(), repr=False)
    denied_webhooks: tuple[str, ...] = field(default=(), repr=False)
    mode: str = "staging"
    certificate: str = "fullchain.pem"
    private_key: str = "privkey.pem"
    client_certificate: str = "manual-location-gateway-client.pem"
    client_key: str = "manual-location-gateway-client.key"

    @classmethod
    def load(cls, path=DATA_DIR / "options.json"):
        try:
            value = _json(Path(path).read_bytes())
            expected = {"public_url", "manual_entry_id", "protected_person", "protected_tracker",
                        "protected_webhook", "control_token", "passthrough_webhooks", "denied_webhooks",
                        "mode", "certificate", "private_key", "client_certificate", "client_key"}
            if not isinstance(value, dict) or set(value) - expected:
                raise ConfigurationError("INVALID_OPTIONS")
            _production_origin(value["public_url"])
            origin = urlsplit(value["public_url"])
            if (origin.scheme != "https" or not origin.hostname or origin.username or origin.password
                    or origin.query or origin.fragment or origin.path not in {"", "/"}
                    or origin.port != 8123):
                raise ConfigurationError("INVALID_PUBLIC_ORIGIN")
            identifier(origin.hostname, r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
            try:
                ipaddress.ip_address(origin.hostname)
            except ValueError:
                pass
            else:
                raise ConfigurationError("PUBLIC_DNS_NAME_REQUIRED")
            token = value["control_token"]
            if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
                raise ConfigurationError("INVALID_CONTROL_TOKEN")
            person = identifier(value["protected_person"], r"person\.[a-z0-9_]+")
            tracker = identifier(value["protected_tracker"], r"device_tracker\.[a-z0-9_]+")
            entry = identifier(value["manual_entry_id"], r"[A-Za-z0-9_-]{1,128}")
            webhook = identifier(value["protected_webhook"], r"[A-Za-z0-9_-]{1,128}")
            groups = []
            for name in ("passthrough_webhooks", "denied_webhooks"):
                supplied = value.get(name, [])
                if not isinstance(supplied, list):
                    raise ConfigurationError("INVALID_REGISTRATION_LIST")
                groups.append(tuple(identifier(item, r"[A-Za-z0-9_-]{1,128}") for item in supplied))
            all_ids = (webhook, *groups[0], *groups[1])
            if len(all_ids) != len(set(all_ids)) or len(all_ids) > 32:
                raise ConfigurationError("DUPLICATE_REGISTRATION")
            mode = value.get("mode", "staging")
            if mode not in {"staging", "production"}:
                raise ConfigurationError("INVALID_MODE")
            files = []
            for name, default in (("certificate", "fullchain.pem"), ("private_key", "privkey.pem"),
                                  ("client_certificate", "manual-location-gateway-client.pem"),
                                  ("client_key", "manual-location-gateway-client.key")):
                filename = identifier(value.get(name, default), r"[A-Za-z0-9_.-]{1,128}")
                if ".." in filename or filename.startswith("."):
                    raise ConfigurationError("INVALID_TLS_FILENAME")
                files.append(filename)
            return cls(value["public_url"].rstrip("/"), entry, person, tracker, webhook, token,
                       groups[0], groups[1], mode, *files)
        except ConfigurationError:
            raise
        except (KeyError, OSError, ValueError, TypeError):
            raise ConfigurationError("INVALID_OPTIONS") from None

    @property
    def backend_url(self):
        return "https://127.0.0.1:8123" if self.mode == "staging" else "https://127.0.0.1:8124"

    @property
    def public_port(self):
        return 18443 if self.mode == "staging" else 8123

    @property
    def control_url(self):
        return "http://127.0.0.1:18325"

    @property
    def public_host(self):
        return urlsplit(self.public_url).hostname


class RegistrationGuard:
    """Read only selected identities/flags; retain no phone payloads or push token."""

    def __init__(self, options, config_dir=CONFIG_DIR):
        self.options = options
        self.config_dir = Path(config_dir)
        self.baseline = self._read()
        self.allowed_push_url = self.baseline[1]

    def _records(self, filename, key):
        document = _json((self.config_dir / ".storage" / filename).read_bytes())
        values = document["data"][key]
        if not isinstance(values, list):
            raise ConfigurationError("INVALID_REGISTRY")
        return values

    def _read(self, *, require_unattached=False):
        try:
            entries = self._records("core.config_entries", "entries")
            manual = [item for item in entries if item.get("entry_id") == self.options.manual_entry_id
                      and item.get("domain") == "manual_location"]
            if len(manual) != 1 or not isinstance(manual[0].get("options", {}), dict):
                raise ConfigurationError("INTEGRATION_IDENTITY_MISMATCH")
            entry_options = manual[0].get("options", {})
            if require_unattached and "gateway" in entry_options:
                raise ConfigurationError("INTEGRATION_ALREADY_ATTACHED")
            if "gateway" in entry_options:
                adapter = entry_options["gateway"]
                if (not isinstance(adapter, dict) or adapter.get("url") != self.options.control_url
                        or adapter.get("person_id") != self.options.protected_person
                        or not isinstance(adapter.get("token"), str)
                        or not hmac.compare_digest(adapter["token"], self.options.control_token)):
                    raise ConfigurationError("ADAPTER_IDENTITY_MISMATCH")
            mobile = [item for item in entries if item.get("domain") == "mobile_app"]
            expected = {self.options.protected_webhook, *self.options.passthrough_webhooks,
                        *self.options.denied_webhooks}
            by_webhook = {}
            identity = []
            for entry in mobile:
                data = entry.get("data", {})
                webhook = data.get("webhook_id")
                if webhook not in expected or webhook in by_webhook:
                    raise ConfigurationError("REGISTRATION_IDENTITY_MISMATCH")
                if (data.get("supports_encryption") is not False
                        or data.get("cloudhook_url") or data.get("remote_ui_url")):
                    raise ConfigurationError("UNSUPPORTED_REGISTRATION")
                by_webhook[webhook] = entry
                identity.append((webhook, entry.get("entry_id")))
            if set(by_webhook) != expected:
                raise ConfigurationError("REGISTRATION_IDENTITY_MISMATCH")
            registry = self._records("core.entity_registry", "entities")
            trackers = [item for item in registry if item.get("entity_id") == self.options.protected_tracker]
            protected = by_webhook[self.options.protected_webhook]
            if (len(trackers) != 1 or trackers[0].get("config_entry_id") != protected.get("entry_id")
                    or trackers[0].get("platform") != "mobile_app"):
                raise ConfigurationError("TRACKER_IDENTITY_MISMATCH")
            persons = self._records("person", "items")
            person_registry = [item for item in registry if item.get("entity_id") == self.options.protected_person]
            person_key = person_registry[0].get("unique_id") if len(person_registry) == 1 else self.options.protected_person.split(".", 1)[1]
            persons = [item for item in persons if item.get("id") == person_key]
            if len(persons) != 1 or persons[0].get("device_trackers") != [self.options.protected_tracker]:
                raise ConfigurationError("PERSON_IDENTITY_MISMATCH")
            push = protected["data"].get("app_data", {}).get("push_url")
            if push is not None:
                parsed = urlsplit(push)
                if (parsed.scheme != "https" or parsed.hostname != OFFICIAL_PUSH_HOST
                        or parsed.port not in {None, 443} or parsed.username or parsed.password
                        or parsed.query or parsed.fragment or not parsed.path.startswith("/api/sendPush")):
                    raise ConfigurationError("UNSUPPORTED_PUSH_PROVIDER")
            return tuple(sorted(identity)), push
        except ConfigurationError:
            raise
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            raise ConfigurationError("REGISTRY_READ_FAILED") from None

    async def check(self):
        try:
            return await asyncio.to_thread(self._read) == self.baseline
        except ConfigurationError:
            return False

    def require_unattached(self):
        if self._read(require_unattached=True) != self.baseline:
            raise ConfigurationError("REGISTRATION_CHANGED")


def render_nginx(options):
    """No user supplied NGINX statements; every interpolated value was validated."""
    routes = []
    for webhook in (options.protected_webhook, *options.passthrough_webhooks, *options.denied_webhooks):
        routes.append(f"""        location = /api/webhook/{webhook} {{
            client_max_body_size 64k;
            client_body_buffer_size 64k;
            client_body_timeout 15s;
            proxy_read_timeout 30s;
            proxy_pass http://127.0.0.1:18325;
        }}""")
    upstream = options.backend_url
    tls = f"""        proxy_ssl_server_name on;
        proxy_ssl_name {options.public_host};
        proxy_ssl_verify on;
        proxy_ssl_verify_depth 3;
        proxy_ssl_trusted_certificate /etc/ssl/cert.pem;
"""
    if options.mode == "production":
        tls += f"""        proxy_ssl_certificate /ssl/{options.client_certificate};
        proxy_ssl_certificate_key /ssl/{options.client_key};
"""
    return f"""error_log /dev/null crit;
pid /tmp/manual-location-gateway/nginx.pid;
worker_processes 1;
events {{ worker_connections 1024; }}
http {{
    access_log off;
    server_tokens off;
    map $http_upgrade $connection_upgrade {{ default upgrade; '' close; }}
    map $http_host $forward_host {{ default $http_host; '' $host; }}
    client_body_temp_path /tmp/manual-location-gateway/client_body;
    proxy_temp_path /tmp/manual-location-gateway/proxy;
    fastcgi_temp_path /tmp/manual-location-gateway/fastcgi;
    uwsgi_temp_path /tmp/manual-location-gateway/uwsgi;
    scgi_temp_path /tmp/manual-location-gateway/scgi;
    server {{
        listen {options.public_port} ssl;
        listen [::]:{options.public_port} ssl ipv6only=on;
        server_name _;
        ssl_certificate /ssl/{options.certificate};
        ssl_certificate_key /ssl/{options.private_key};
        ssl_protocols TLSv1.2 TLSv1.3;
        ssl_session_tickets off;
        merge_slashes on;
        client_max_body_size 0;
        proxy_http_version 1.1;
        proxy_request_buffering off;
        proxy_buffering off;
        proxy_max_temp_file_size 0;
        proxy_ignore_headers X-Accel-Buffering;
        proxy_cache off;
        proxy_next_upstream off;
        proxy_connect_timeout 3s;
        proxy_send_timeout 60s;
        proxy_read_timeout 3600s;
        proxy_set_header Host $forward_host;
        proxy_set_header Origin $http_origin;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host $forward_host;
        proxy_set_header Forwarded '';
{tls}        location = /control {{ return 404; }}
        location ^~ /control/ {{ return 404; }}
        location = /health {{ return 404; }}
        location = /live {{ return 404; }}
        location ^~ /api/mobile_app/registrations {{ return 403; }}
{chr(10).join(routes)}
        location = /api/websocket {{
            proxy_ssl_server_name off;
            proxy_pass http://127.0.0.1:18325;
        }}
        location / {{ proxy_pass {upstream}; }}
    }}
}}
"""


def backend_ssl_context(options, ssl_dir=Path("/ssl")):
    context = ssl.create_default_context()
    if options.mode == "production":
        context.load_cert_chain(ssl_dir / options.client_certificate, ssl_dir / options.client_key)
    return context


def prepare(options, guard, runtime_dir=RUNTIME_DIR, ssl_dir=Path("/ssl")):
    guard._read()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(ssl_dir / options.certificate, ssl_dir / options.private_key)
    backend_ssl_context(options, ssl_dir)  # Production refuses missing/mismatched client key material.
    directory = Path(runtime_dir)
    directory.mkdir(mode=0o755, parents=True, exist_ok=True)
    for name in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi"):
        (directory / name).mkdir(mode=0o700, exist_ok=True)
    target = directory / "nginx.conf"
    target.write_text(render_nginx(options))
    target.chmod(0o600)


def initialize_checkpoint(path, person_id):
    """An exclusive virgin-file bootstrap, never overwrite a prior checkpoint."""
    path = Path(path)
    marker = path.with_suffix(path.suffix + ".initializing")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    marker_fd = os.open(marker, flags, 0o600)
    try:
        os.write(marker_fd, b"initialization incomplete\n")
        os.fsync(marker_fd)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
            state = {"version": 2, "person_id": person_id, "mode": "custom", "last_custom": None,
                     "operation": None, "custom_receipt": None, "pending_custom": None, "sequence": 0,
                     "upstream_uncertain": False, "uncertainty_origin": None}
            fd = os.open(path, flags, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(json.dumps(state, separators=(",", ":")).encode())
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(directory_fd)
            marker.unlink()
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        # Keep the marker as an independent latch even if the checkpoint is valid.
        with contextlib.suppress(OSError):
            marker.touch(mode=0o600, exist_ok=True)
        raise
    finally:
        os.close(marker_fd)


class Runtime:
    def __init__(self, options, supervisor_token, *, config_dir=CONFIG_DIR, data_dir=DATA_DIR,
                 runtime_dir=RUNTIME_DIR, hooks=None, ssl_dir=Path("/ssl"), bootstrap_dir=Path("/bootstrap")):
        if not supervisor_token:
            raise ConfigurationError("MANAGED_API_TOKEN_REQUIRED")
        self.options = options
        self.data_dir = Path(data_dir)
        self.runtime_dir = Path(runtime_dir)
        self.guard = RegistrationGuard(options, config_dir)
        self.hooks = hooks or NativeHooks("http://supervisor/core", supervisor_token,
                                         options.protected_tracker, options.protected_person, config_dir,
                                         control_token=options.control_token)
        self.checkpoint_path = self.data_dir / "gateway-state.json"
        self.initializing_path = self.checkpoint_path.with_suffix(".json.initializing")
        tls_options = {"backend_ssl_context": backend_ssl_context(options, ssl_dir)} if options.mode == "production" else {}
        self.gateway = Gateway(options.backend_url, options.public_url, options.protected_webhook,
            options.control_token, self.checkpoint_path, self.hooks.checkpoint, self.hooks.verify,
            person_id=options.protected_person, ready_native=self.hooks.ready,
            source_session_check=self.hooks.check_source_session,
            source_control_check=self.hooks.check_control_session,
            phone_snapshot=self.hooks.phone_snapshot, record_phone=self.hooks.record_phone,
            passthrough_webhooks=options.passthrough_webhooks, denied_webhooks=options.denied_webhooks,
            production_mode=True, max_ws_message_bytes=8 * 1024 * 1024,
            backend_ssl_server_hostname=options.public_host,
            allowed_push_url=self.guard.allowed_push_url, registration_guard=self.guard.check, **tls_options)
        if self.initializing_path.exists():
            self.gateway.upstream_uncertain = True
            self.gateway.uncertainty_origin = "unknown"
            self.gateway.pending_custom = None
        self.gateway.app.middlewares.append(self.control_middleware)
        self.runners = []
        self.cert_task = None
        self.bootstrap_task = None
        self.bootstrap_dir = Path(bootstrap_dir)
        self.boot_nonce = secrets.token_urlsafe(32)
        self.bootstrap_seen = set()

    @web.middleware
    async def control_middleware(self, request, handler):
        if request.path not in {"/control/initialize", "/control/attach"}:
            return await handler(request)
        if (request.remote != "127.0.0.1" or request.headers.get("X-Forwarded-For")
                or request.method != "POST" or request.query
                or request.raw_path != request.path or not self.gateway._authorized(request)):
            return web.json_response({"error": "operation denied"}, status=403)
        try:
            payload = await self.gateway._read_json(request)
            if request.path == "/control/initialize":
                async with self.gateway.lock:
                    return await self.initialize(payload)
            # OptionsFlow can reload the integration, which calls this gateway.
            # Never retain the native-writer lock across that RPC.
            return await self.attach(payload)
        except (ConfigurationError, ValueError, OSError, ClientError, asyncio.TimeoutError):
            self.gateway.mode, self.gateway.normal_after, self.gateway.source_session = "custom", None, None
            return web.json_response({"error": "operation failed"}, status=409)

    async def core_unavailable(self):
        try:
            async with self.hooks.client.get(self.hooks.base_url + "/api/config") as response:
                await response.read()
                return response.status in {502, 503}
        except (ClientError, asyncio.TimeoutError):
            return False  # An unreachable Supervisor is not a proven stopped Core.

    async def initialize(self, payload):
        if not isinstance(payload, dict) or set(payload) != {"core_stopped"} or payload["core_stopped"] is not True:
            raise ConfigurationError("STOPPED_ACTOR_ATTESTATION_REQUIRED")
        if self.checkpoint_path.exists() or self.initializing_path.exists():
            raise ConfigurationError("CHECKPOINT_ALREADY_EXISTS")
        self.guard.require_unattached()
        if not await self.core_unavailable():
            raise ConfigurationError("CORE_MUST_BE_STOPPED")
        # The authenticated manager actor separately verifies Core STOPPED. This
        # flag records that barrier; absence of a Core API response alone is insufficient.
        try:
            initialize_checkpoint(self.checkpoint_path, self.options.protected_person)
        except BaseException:
            self.gateway.upstream_uncertain = True
            self.gateway.uncertainty_origin = "unknown"
            raise
        self.gateway._load_checkpoint()
        return web.json_response({"initialized": True})

    async def attach(self, payload):
        if payload != {} or self.gateway.upstream_uncertain or not self.checkpoint_path.exists():
            raise ConfigurationError("ATTACH_NOT_READY")
        self.guard.require_unattached()
        async def post(path, data):
            async with self.hooks.client.post(self.hooks.base_url + path, json=data) as response:
                if response.status != 200:
                    raise ConfigurationError("OPTIONS_FLOW_FAILED")
                value = await response.json()
                if not isinstance(value, dict):
                    raise ConfigurationError("OPTIONS_FLOW_FAILED")
                return value
        base = "/api/config/config_entries/options/flow"
        flow = await post(base, {"handler": self.options.manual_entry_id})
        flow_id = identifier(flow.get("flow_id"), r"[A-Za-z0-9_-]{1,128}")
        flow = await post(base + "/" + flow_id, {"configure_gateway": True})
        if flow.get("step_id") != "gateway":
            raise ConfigurationError("OPTIONS_FLOW_FAILED")
        result = await post(base + "/" + flow_id, {"url": self.options.control_url,
                      "person_id": self.options.protected_person, "token": self.options.control_token})
        if result.get("type") != "create_entry":
            raise ConfigurationError("OPTIONS_FLOW_FAILED")
        return web.json_response({"attached": True})

    def write_bootstrap_json(self, name, value):
        target = self.bootstrap_dir / name
        temporary = self.bootstrap_dir / ("." + name + "." + secrets.token_hex(8))
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(json.dumps(value, separators=(",", ":")).encode())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            fd = os.open(self.bootstrap_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()

    async def process_bootstrap_request(self):
        # Claim before parsing or executing. A crash leaves an ignored claimed
        # file, never a request that can be replayed after the next boot.
        claimed = self.bootstrap_dir / (".claimed-" + secrets.token_hex(8))
        try:
            os.rename(self.bootstrap_dir / "request.json", claimed)
        except FileNotFoundError:
            return False
        request_id, success, error = None, False, "INVALID_REQUEST"
        try:
            fd = os.open(claimed, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size > 4096 or info.st_mode & 0o077:
                    raise ConfigurationError("INVALID_BOOTSTRAP_FILE")
                raw = os.read(fd, 4097)
                if len(raw) > 4096:
                    raise ConfigurationError("INVALID_BOOTSTRAP_FILE")
            finally:
                os.close(fd)
        except (OSError, ConfigurationError):
            raw = b""
        finally:
            with contextlib.suppress(FileNotFoundError, IsADirectoryError):
                claimed.unlink()
        try:
            value = _json(raw)
            if not isinstance(value, dict) or set(value) != {"id", "boot_nonce", "action", "payload"}:
                raise ConfigurationError("INVALID_BOOTSTRAP_REQUEST")
            request_id = identifier(value["id"], r"[A-Za-z0-9_-]{1,64}")
            if value["boot_nonce"] != self.boot_nonce:
                error = "STALE_BOOT"
            elif request_id in self.bootstrap_seen:
                error = "DUPLICATE_REQUEST"
            elif not isinstance(value["action"], str) or value["action"] not in {"initialize", "attach"}:
                error = "ACTION_DENIED"
            elif len(self.bootstrap_seen) >= 128:
                error = "REQUEST_LIMIT"
            else:
                self.bootstrap_seen.add(request_id)
                error = "OPERATION_FAILED"
                if value["action"] == "initialize":
                    async with self.gateway.lock:
                        await self.initialize(value["payload"])
                else:
                    await self.attach(value["payload"])
                success, error = True, None
        except (ConfigurationError, ValueError, OSError, ClientError, asyncio.TimeoutError):
            pass
        self.write_bootstrap_json("result.json", {"id": request_id, "success": success, "error_code": error})
        return True

    async def watch_bootstrap(self):
        while True:
            try:
                await self.process_bootstrap_request()
            except (OSError, ConfigurationError):
                LOGGER.warning("BOOTSTRAP_CHANNEL_FAILED")
            await asyncio.sleep(1)

    def nginx_alive(self):
        try:
            pid = int((self.runtime_dir / "nginx.pid").read_text())
            return pid > 1 and (Path("/proc") / str(pid) / "comm").read_text().strip() == "nginx"
        except (OSError, ValueError):
            return False

    async def live(self, _request):
        return web.Response(text="ok" if self.nginx_alive() else "starting", status=200 if self.nginx_alive() else 503)

    async def watch_certificates(self, ssl_dir=Path("/ssl")):
        def stamp():
            return tuple((path.stat().st_ino, path.stat().st_mtime_ns, path.stat().st_size)
                         for path in (ssl_dir / self.options.certificate, ssl_dir / self.options.private_key))
        previous = stamp()
        while True:
            await asyncio.sleep(30)
            try:
                current = stamp()
                if current == previous:
                    continue
                command = ["nginx", "-e", "/dev/null", "-c", str(self.runtime_dir / "nginx.conf")]
                test = await asyncio.create_subprocess_exec(*command, "-t", "-q",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                if await test.wait() != 0:
                    LOGGER.warning("TLS_RELOAD_REJECTED")
                    continue
                reload = await asyncio.create_subprocess_exec(*command, "-s", "reload",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                if await reload.wait() == 0:
                    previous = current
                else:
                    LOGGER.warning("TLS_RELOAD_FAILED")
            except OSError:
                LOGGER.warning("TLS_RELOAD_UNAVAILABLE")

    async def start(self):
        await self.hooks.start()
        runner = web.AppRunner(self.gateway.app, access_log=None)
        self.runners.append(runner)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", CONTROL_PORT).start()
        health = web.Application()
        health.router.add_get("/live", self.live)
        health_runner = web.AppRunner(health, access_log=None)
        self.runners.append(health_runner)
        await health_runner.setup()
        await web.TCPSite(health_runner, LIVE_HOST, LIVE_PORT).start()
        self.bootstrap_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.write_bootstrap_json("status.json", {"boot_nonce": self.boot_nonce, "ready": True})
        self.bootstrap_task = asyncio.create_task(self.watch_bootstrap())
        self.cert_task = asyncio.create_task(self.watch_certificates())

    async def close(self):
        if self.bootstrap_task:
            self.bootstrap_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.bootstrap_task
        if self.cert_task:
            self.cert_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.cert_task
        for runner in reversed(self.runners):
            await runner.cleanup()
        await self.hooks.close()


async def serve(options):
    runtime = Runtime(options, os.environ.get("SUPERVISOR_TOKEN"))
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda _loop, _context: LOGGER.error("GATEWAY_ASYNC_FAILED"))
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stopped.set)
    try:
        await runtime.start()
        await stopped.wait()
    finally:
        await runtime.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "run"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    # Third-party request/server tracebacks may contain a webhook URI. Operational
    # diagnostics are the static codes above; never log an exception object.
    for handler in logging.getLogger().handlers:
        handler.addFilter(lambda record: record.name == LOGGER.name)
    for name in ("aiohttp", "aiohttp.server", "aiohttp.client", "aiohttp.access", "asyncio"):
        logging.getLogger(name).disabled = True
    try:
        options = Options.load()
        if args.action == "prepare":
            prepare(options, RegistrationGuard(options))
        else:
            asyncio.run(serve(options))
    except Exception:
        LOGGER.error("GATEWAY_START_FAILED")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
