"""Gravitrax Web Service: A RESTful API for GraviTrax Connect.

This web service provides HTTP endpoints to interact with GraviTrax Connect stones.
It allows for connecting to a bridge, sending signals, and receiving notifications
through a web interface.
"""

import asyncio
import json
import threading
import time
import uuid
from threading import Event, Lock
import logging

from flask import Flask, jsonify, request
from flask_cors import CORS

from pathlib import Path
import re

import yaml
from jsonschema import Draft7Validator, ValidationError

from gravitraxconnect import gravitrax_bridge as gb
from gravitraxconnect import gravitrax_constants as gv

# Create Flask app
app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# Ensure INFO-level logging for Flask and Werkzeug by default
try:
    app.logger.setLevel(logging.INFO)
except Exception:
    pass
try:
    logging.getLogger("werkzeug").setLevel(logging.INFO)
except Exception:
    pass

# Multi-connection registry
# Each connection has: {
#   'bridge': gb.Bridge(), 'connected': bool, 'notifications': list,
#   'notification_lock': Lock(),
#   'connection_event': Event(), 'disconnect_event': Event(),
#   'notifications_enabled': bool, 'notif_version': int
# }
connections = {}
connections_lock = Lock()

# Configure logging

def _configure_gravitrax_logging(flask_app: Flask | None = None):
    """Reconfigure library logging to integrate with Flask.

    The gravitraxconnect.gravitrax_bridge module installs its own StreamHandler
    and disables logging by default. Here we remove its handler, enable the
    logger, and route logs to Flask/werkzeug handlers so logs appear nicely in
    the web service output and any WSGI server (e.g., gunicorn).
    """
    # Use the logger object from the library module
    lib_logger = gb.logger  # logging.getLogger("gravitraxconnect.gravitrax_bridge")

    # Ensure it is enabled
    lib_logger.disabled = False

    # Remove any custom handlers attached by the library (avoid duplicate output)
    for h in list(lib_logger.handlers):
        try:
            lib_logger.removeHandler(h)
        except Exception:
            pass

    # Align the log level with the Flask app's logger if available
    try:
        target_level = flask_app.logger.level if flask_app else logging.INFO
    except Exception:
        target_level = logging.INFO
    lib_logger.setLevel(target_level)

    # Route to Flask's handlers when available; otherwise, propagate to root
    attached = False
    if flask_app:
        try:
            for h in flask_app.logger.handlers:
                lib_logger.addHandler(h)
                attached = True
        except Exception:
            pass
    if not attached:
        # Fallback: propagate to root. Ensure root has at least one handler.
        lib_logger.propagate = True
        root = logging.getLogger()
        if not root.handlers:
            try:
                handler = logging.StreamHandler()
                handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
                root.addHandler(handler)
            except Exception:
                pass
    else:
        # We directly attached Flask handlers; avoid duplicate propagation
        lib_logger.propagate = False


# Apply configuration now that the app exists
_configure_gravitrax_logging(app)
# Emit a startup log through the library to verify visibility
try:
    gb.log_print("GraviTrax Web Service: library logging configured", level="INFO")
except Exception:
    pass

# ----- OpenAPI-based request validation -----
_OAS_SPEC = None
_OAS_OPERATIONS = None


def _load_openapi_spec():
    global _OAS_SPEC, _OAS_OPERATIONS
    if _OAS_SPEC is not None:
        return _OAS_SPEC
    try:
        spec_path = Path(__file__).with_name('openapi.yaml')
        with spec_path.open('r', encoding='utf-8') as f:
            _OAS_SPEC = yaml.safe_load(f)
    except Exception as e:
        app.logger.warning(f"OpenAPI spec could not be loaded: {e}")
        _OAS_SPEC = {}
    # Index operations by (method, path)
    _OAS_OPERATIONS = {}
    try:
        paths = _OAS_SPEC.get('paths', {})
        for oas_path, path_item in paths.items():
            for method in ['get', 'post', 'put', 'delete', 'patch']:
                if method in path_item:
                    _OAS_OPERATIONS[(method.upper(), oas_path)] = path_item[method]
    except Exception:
        pass
    return _OAS_SPEC


def _normalize_path_segments(p: str):
    # Split path into segments; parameters (either <x> or {x}) become a generic placeholder '{}'
    segs = [s for s in p.split('/') if s != '']
    norm = []
    for s in segs:
        if s.startswith('<') and s.endswith('>'):
            norm.append('{}')
        elif s.startswith('{') and s.endswith('}'):
            norm.append('{}')
        else:
            norm.append(s)
    return norm


def _match_oas_operation(flask_rule, method: str):
    # Return the OpenAPI operation dict for this rule+method, or None
    _load_openapi_spec()
    if not _OAS_OPERATIONS:
        return None
    fr = flask_rule or ''
    fsegs = _normalize_path_segments(fr)
    for (m, oas_path), op in _OAS_OPERATIONS.items():
        if m != method.upper():
            continue
        osegs = _normalize_path_segments(oas_path)
        if len(fsegs) != len(osegs):
            continue
        ok = True
        for fs, os in zip(fsegs, osegs):
            if fs == '{}' or os == '{}':
                continue
            if fs != os:
                ok = False
                break
        if ok:
            return op
    return None


def _resolve_refs(schema, spec):
    # Very simple in-file $ref resolver for '#/components/schemas/...'
    if isinstance(schema, dict):
        if '$ref' in schema and isinstance(schema['$ref'], str):
            ref = schema['$ref']
            if ref.startswith('#/components/schemas/'):
                name = ref.split('/')[-1]
                resolved = spec.get('components', {}).get('schemas', {}).get(name)
                if resolved is not None:
                    return _resolve_refs(resolved, spec)
        # Recurse
        return {k: _resolve_refs(v, spec) for k, v in schema.items()}
    elif isinstance(schema, list):
        return [_resolve_refs(v, spec) for v in schema]
    else:
        return schema


def _validate_request_against_openapi():
    # No rule in blueprint/static cases
    if not request.url_rule:
        return None
    try:
        operation = _match_oas_operation(request.url_rule.rule, request.method)
        if not operation:
            return None  # Not in spec or unmatched; do nothing
        # Validate requestBody if defined
        req_body = operation.get('requestBody') if isinstance(operation, dict) else None
        if req_body:
            required = bool(req_body.get('required', False))
            content = req_body.get('content', {})
            app_json = content.get('application/json') if isinstance(content, dict) else None
            schema = app_json.get('schema') if isinstance(app_json, dict) else None
            if schema is not None:
                # Must be JSON
                if not request.is_json:
                    if required:
                        return jsonify({"error": "bad_request", "details": ["Expected application/json body"]}), 400
                    else:
                        return None
                try:
                    data = request.get_json(silent=True)
                except Exception:
                    data = None
                if data is None:
                    return jsonify({"error": "bad_request", "details": ["Invalid JSON body"]}), 400
                spec = _load_openapi_spec()
                resolved_schema = _resolve_refs(schema, spec)
                try:
                    Draft7Validator(resolved_schema).validate(data)
                except ValidationError as ve:
                    # Build human-friendly path message
                    loc = "".join([f"/{p}" for p in ve.absolute_path])
                    msg = f"{ve.message}{' at ' + loc if loc else ''}"
                    return jsonify({"error": "bad_request", "details": [msg]}), 400
        # Minimal validation for GET /api/v1/bridges query 'timeout'
        # (because schema is in parameters, we keep it simple)
        if request.method.upper() == 'GET' and request.url_rule.rule == '/api/v1/bridges':
            timeout = request.args.get('timeout')
            if timeout is not None:
                try:
                    tv = float(timeout)
                    if tv < 0.1:
                        return jsonify({"error": "bad_request", "details": ["timeout must be >= 0.1"]}), 400
                except ValueError:
                    return jsonify({"error": "bad_request", "details": ["timeout must be a number"]}), 400
        return None
    except Exception as e:
        # Fail-open: if validator crashes, don't block the request, but log
        try:
            app.logger.warning(f"OpenAPI validation skipped due to error: {e}")
        except Exception:
            pass
        return None


@app.before_request
def _before_request_validation():
    res = _validate_request_against_openapi()
    return res


class Connection:
    def __init__(self, notifications_enabled: bool = False, bridge_mode_enabled: bool = False):
        # Dedicated asyncio loop running in a background thread for this connection
        self._loop = asyncio.new_event_loop()
        self._loop_started = threading.Event()
        self._thread = threading.Thread(target=self._loop_worker, name=f"conn-loop-{id(self)}", daemon=True)
        self._thread.start()
        self._loop_started.wait(timeout=5)
        # Create the Bridge inside the dedicated loop so its asyncio primitives bind to that loop
        self.bridge = None
        def _init_bridge_sync():
            async def _init():
                self.bridge = gb.Bridge()
                return True
            fut = asyncio.run_coroutine_threadsafe(_init(), self._loop)
            return fut.result(timeout=5)
        try:
            _init_bridge_sync()
        except Exception:
            # Leave as None; operations will fail later with a clear message
            pass
        self.connected = False
        self.notifications = []
        self.notification_lock = Lock()
        self.connection_event = Event()
        self.disconnect_event = Event()
        self.notifications_enabled = notifications_enabled
        self.ble_notifications = False
        self.notif_version = 0
        # Bridge mode state
        self.bridge_mode_enabled = bridge_mode_enabled
        self.bridge_mode_active = False
        # Reported MAC address of the connected bridge (if known)
        self.mac_address = None

    def _loop_worker(self):
        asyncio.set_event_loop(self._loop)
        # Signal started once the loop begins processing callbacks
        self._loop.call_soon_threadsafe(self._loop_started.set)
        self._loop.run_forever()

    def _submit(self, coro):
        if self._loop.is_closed():
            raise RuntimeError("connection loop is closed")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def _await_concurrent(self, cfut):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: cfut.result())

    async def _run(self, coro):
        cfut = self._submit(coro)
        return await self._await_concurrent(cfut)

    def add_notification(self, notif: dict):
        if not self.notifications_enabled:
            return
        now = time.time()
        with self.notification_lock:
            cutoff = now - 30.0
            if self.notifications:
                self.notifications = [n for n in self.notifications if n.get('timestamp', 0) >= cutoff]
            # Increment version and stamp the notification with it
            self.notif_version = self.notif_version + 1
            notif = dict(notif)
            notif['v'] = self.notif_version
            self.notifications.append(notif)

    def clear_notifications(self):
        with self.notification_lock:
            self.notifications = []
            self.notif_version = 0

    def get_notifications_snapshot(self):
        with self.notification_lock:
            now = time.time()
            cutoff = now - 30.0
            if self.notifications:
                self.notifications = [n for n in self.notifications if n.get('timestamp', 0) >= cutoff]
            payload = self.notifications[:]
            etag = str(self.notif_version)
        return payload, etag

    def get_notifications_delta(self, since_version: int):
        with self.notification_lock:
            now = time.time()
            cutoff = now - 30.0
            if self.notifications:
                self.notifications = [n for n in self.notifications if n.get('timestamp', 0) >= cutoff]
            # Only notifications with version greater than since_version
            payload = [n for n in self.notifications if int(n.get('v', 0)) > int(since_version)]
            etag = str(self.notif_version)
        return payload, etag

    def current_etag(self) -> str:
        with self.notification_lock:
            return str(self.notif_version)

    async def apply_notifications_flag(self, new_flag: bool, callback_factory, conn_id: str):
        old_flag = self.notifications_enabled
        self.notifications_enabled = new_flag
        if new_flag and not old_flag:
            if self.connected:
                try:
                    await self._run(self.bridge.notification_enable(callback_factory(conn_id)))
                    self.ble_notifications = True
                except Exception as e:
                    gb.log_print(f"Failed to enable notifications: {e}")
                    self.ble_notifications = False
        elif not new_flag and old_flag:
            self.clear_notifications()
            if self.connected and self.ble_notifications:
                try:
                    await self._run(self.bridge.notification_disable())
                except Exception as e:
                    gb.log_print(f"Failed to disable notifications: {e}")
                self.ble_notifications = False

    async def apply_bridge_mode_flag(self, new_flag: bool):
        old_flag = self.bridge_mode_enabled
        self.bridge_mode_enabled = new_flag
        if new_flag and not old_flag:
            if self.connected and not self.bridge_mode_active:
                try:
                    await self._run(self.bridge.start_bridge_mode())
                    self.bridge_mode_active = True
                except Exception as e:
                    gb.log_print(f"Failed to start bridge mode: {e}")
                    self.bridge_mode_active = False
        elif not new_flag and old_flag:
            if self.connected and self.bridge_mode_active:
                try:
                    await self._run(self.bridge.stop_bridge_mode())
                except Exception as e:
                    gb.log_print(f"Failed to stop bridge mode: {e}")
                self.bridge_mode_active = False

    async def send(self, status, color, stone, count, resends, resend_gap, gap):
        if count > 1:
            await self._run(self.bridge.send_periodic(status, color, count=count, stone=stone, gap=gap, resend_gap=resend_gap, resends=resends))
        else:
            await self._run(self.bridge.send_signal(status, color, stone=stone, resends=resends, resend_gap=resend_gap))

    async def get_battery(self) -> str:
        return await self._run(self.bridge.request_battery_string())

    async def get_info(self):
        return await self._run(self.bridge.request_bridge_info())

    def long_poll_notifications(self, client_etag: str | None, timeout: float):
        start = time.time()
        with self.notification_lock:
            cutoff = start - 30.0
            if self.notifications:
                self.notifications = [n for n in self.notifications if n.get('timestamp', 0) >= cutoff]
            current_version = self.notif_version
            latest_etag = str(current_version)
        # If client provided an ETag, compute delta relative to it
        if client_etag is not None:
            try:
                client_version = int(client_etag)
            except (TypeError, ValueError):
                client_version = -1
            if client_version != current_version:
                payload, etag = self.get_notifications_delta(client_version)
                return ("modified", payload, etag)
        # No ETag provided, or no change yet: wait for updates
        initial_version = current_version
        while time.time() - start < timeout:
            time.sleep(0.1)
            with self.notification_lock:
                v = self.notif_version
            if v != initial_version:
                if client_etag is not None:
                    # Return only new notifications since client's version
                    try:
                        client_version = int(client_etag)
                    except (TypeError, ValueError):
                        client_version = -1
                    payload, etag = self.get_notifications_delta(client_version)
                else:
                    # No client etag: return a snapshot of everything we have
                    payload, etag = self.get_notifications_snapshot()
                return ("modified", payload, etag)
        # Timeout reached
        return ("timeout" if client_etag is None else "not_modified", None, str(initial_version))


# --------- Multi-connection helpers ---------

def _make_notification_callback(conn_id: str):
    async def _cb(bridge: gb.Bridge, **kwargs):
        # Prepare notification payload
        def lookup(value, table, prefix=""):
            try:
                return table[value]
            except (KeyError, IndexError):
                if prefix:
                    return f"{prefix}{value}"
                return value

        header = kwargs.get('Header')
        stone = kwargs.get('Stone')
        status = kwargs.get('Status')
        color = kwargs.get('Color')
        data = kwargs.get('Data')

        if header is not None:
            notif = {
                "timestamp": time.time(),
                "type": "signal",
                "status": status,
                "status_name": lookup(status, gv.DICT_VAL_STATUS),
                "stone": stone,
                "stone_name": lookup(stone, gv.DICT_VAL_STONE),
                "color": color,
                "color_name": lookup(color, gv.LOOKUP_COLOR, "Color"),
            }
            gb.log_print(
                f"{notif['color_name']:5} detected from Stone",
                f" {notif['stone_name']} with Status {notif['status_name']}",
                bridge=bridge,
            )
        else:
            notif = {
                "timestamp": time.time(),
                "type": "data",
                "data": data,
            }
            gb.log_print(f"New Notification: {data}", bridge=bridge)

        with connections_lock:
            conn = connections.get(conn_id)
        if conn is None:
            return
        # Store using Connection helper (handles enable flag and cleanup/versioning)
        conn.add_notification(notif)
    return _cb


def _make_disconnect_callback(conn_id: str):
    def _cb(bridge: gb.Bridge, **kwargs):
        with connections_lock:
            conn = connections.get(conn_id)
        if conn is None:
            return
        if kwargs.get("by_timeout"):
            gb.log_print("Disconnect timed out", bridge=bridge)
        elif kwargs.get("user_disconnected"):
            gb.log_print("Successfully Disconnected", bridge=bridge)
        else:
            gb.log_print("Connection to Bridge was interrupted", bridge=bridge)
        conn.connected = False
        conn.mac_address = None
        conn.disconnect_event.set()
    return _cb


async def connection_connect(conn_id: str, mac_address=None):
    with connections_lock:
        conn = connections.get(conn_id)
        if conn is None:
            # init structure
            conn = Connection(notifications_enabled=False)
            connections[conn_id] = conn
    try:
        gb.log_print("Searching for Bridge")
        if mac_address:
            success = await conn._run(conn.bridge.connect(name_or_addr=mac_address, by_name=False, try_reconnect=True, dc_callback=_make_disconnect_callback(conn_id)))
        else:
            success = await conn._run(conn.bridge.connect(try_reconnect=True, dc_callback=_make_disconnect_callback(conn_id)))
        if not success:
            gb.log_print("Failed to connect to bridge")
            conn.connected = False
            conn.mac_address = None
            return False
        # Store MAC address if available (prefer explicit argument, else query bridge)
        try:
            conn.mac_address = str(mac_address) if mac_address else (await conn._run(asyncio.to_thread(conn.bridge.get_address)))
        except Exception:
            # Fallback: try direct call (Bridge.get_address is sync)
            try:
                conn.mac_address = conn.bridge.get_address()
            except Exception:
                conn.mac_address = None
        # Enable BLE notifications only if explicitly requested
        if conn.notifications_enabled:
            try:
                await conn._run(conn.bridge.notification_enable(_make_notification_callback(conn_id)))
                conn.ble_notifications = True
            except Exception as _e:
                gb.log_print(f"Failed to enable notifications on connect: {_e}")
                conn.ble_notifications = False
        else:
            conn.ble_notifications = False
        # Mark connected before optional modes
        conn.connected = True
        # If bridge mode was requested, start it now
        if conn.bridge_mode_enabled:
            try:
                await conn._run(conn.bridge.start_bridge_mode())
                conn.bridge_mode_active = True
            except Exception as _e:
                gb.log_print(f"Failed to start bridge mode on connect: {_e}")
                conn.bridge_mode_active = False
        # Do not request battery/info on connect; expose via dedicated routes
        conn.connection_event.set()
        return True
    except Exception as e:
        gb.log_print(f"Error connecting to bridge: {str(e)}")
        conn.connected = False
        conn.mac_address = None
        return False


async def connection_disconnect(conn_id: str):
    with connections_lock:
        conn = connections.get(conn_id)
    if not conn:
        return True
    if not conn.connected:
        return True
    try:
        conn.disconnect_event.clear()
        await conn._run(conn.bridge.disconnect(timeout=15, dc_callback_on_timeout=True))
        await asyncio.sleep(1)
        if conn.disconnect_event.is_set():
            conn.connected = False
            conn.mac_address = None
            return True
        return False
    except Exception as e:
        gb.log_print(f"Error disconnecting from bridge: {str(e)}")
        return False




@app.route('/api/v1/constants', methods=['GET'])
def get_constants():
    """Get the GraviTrax constants (stone types, statuses, colors)."""
    return jsonify({
        "stone_types": {k: v for k, v in gv.DICT_STONE.items()},
        "stone_values": {str(k): v for k, v in gv.DICT_VAL_STONE.items()},
        "statuses": {k: v for k, v in gv.DICT_STATUS.items()},
        "status_values": {str(k): v for k, v in gv.DICT_VAL_STATUS.items()},
        "colors": {
            "red": gv.COLOR_RED,
            "green": gv.COLOR_GREEN,
            "blue": gv.COLOR_BLUE
        }
    })


@app.route('/api/v1/bridges', methods=['GET'])
async def list_bridges():
    """Scan for available GraviTrax bridges and return their MAC addresses.

    Optional query parameters:
    - timeout: float seconds to scan (default 5)
    - name: device name filter (default gv.BRIDGE_NAME; use empty string to scan all)
    """
    # Parse query params with defaults
    try:
        timeout = float(request.args.get('timeout', 5))
        if timeout <= 0:
            timeout = 5
    except (TypeError, ValueError):
        timeout = 5
    name = request.args.get('name', getattr(gv, 'BRIDGE_NAME', None))
    if name is None:
        # Fallback if constant not present
        name = "GraviTrax Bridge"

    try:
        addresses = await gb.scan_bridges(name=name, timeout=timeout, do_print=False)
        return jsonify({"bridges": addresses})
    except Exception as e:
        gb.log_print(f"Error scanning for bridges: {str(e)}")
        return jsonify({"bridges": [], "error": "scan_failed"}), 500


# --------- Connections-based endpoints ---------

# --------- Simple API (default connection) ---------
DEFAULT_CONNECTION_ID = "default"

async def _ensure_default_connection(need_notifications: bool = False, need_bridge_mode: bool = False) -> tuple[bool, str]:
    """
    Ensure the default connection exists and is connected. Optionally ensure that
    notifications and bridge mode are enabled. Returns (ok, message).
    """
    # Create connection object if missing
    with connections_lock:
        conn = connections.get(DEFAULT_CONNECTION_ID)
        if conn is None:
            conn = Connection(notifications_enabled=need_notifications, bridge_mode_enabled=need_bridge_mode)
            connections[DEFAULT_CONNECTION_ID] = conn
    # Connect if not connected
    if not conn.connected:
        ok = await connection_connect(DEFAULT_CONNECTION_ID)
        if not ok:
            return False, "Failed to connect to bridge"
    # Ensure notifications enabled if required
    if need_notifications and not conn.notifications_enabled:
        await conn.apply_notifications_flag(True, _make_notification_callback, DEFAULT_CONNECTION_ID)
    # Ensure bridge mode enabled if required
    if need_bridge_mode and not conn.bridge_mode_enabled:
        await conn.apply_bridge_mode_flag(True)
    return True, "ok"

@app.route('/api/simple/signals', methods=['POST'])
async def simple_send_signal():
    # Ensure default connection with bridge mode active (for reliable sending)
    ok, msg = await _ensure_default_connection(need_notifications=False, need_bridge_mode=True)
    if not ok:
        return jsonify({"success": False, "message": msg}), 500
    with connections_lock:
        conn = connections.get(DEFAULT_CONNECTION_ID)
    if not conn or not conn.connected:
        return jsonify({"success": False, "message": "Not connected"}), 400
    params, err = _parse_signal_params(request.json)
    if err:
        code, emsg = err
        return jsonify({"success": False, "message": emsg}), code
    status, color, stone, count, resends, resend_gap, gap = params
    try:
        await conn.send(status, color, stone, count, resends, resend_gap, gap)
        return jsonify({"success": True})
    except Exception as e:
        gb.log_print(f"Error sending signal on default: {e}")
        return jsonify({"success": False, "message": "Failed to send signal"}), 500

@app.route('/api/simple/notifications', methods=['GET'])
def simple_get_notifications():
    # Ensure default connection with notifications enabled; no need to require bridge mode here
    # Because we are in a sync context for long-poll, keep interface similar to existing connection notifications
    # Enable notifications flag (and underlying BLE notifications if connected)
    loop = asyncio.get_event_loop()
    try:
        ok, msg = loop.run_until_complete(_ensure_default_connection(need_notifications=True, need_bridge_mode=False))
    except RuntimeError:
        # If no running loop in this thread (typical for Flask), create a temporary one to run the coroutine
        ok, msg = asyncio.run(_ensure_default_connection(need_notifications=True, need_bridge_mode=False))
    if not ok:
        return jsonify({"error": "connect_failed", "message": msg}), 500
    with connections_lock:
        conn = connections.get(DEFAULT_CONNECTION_ID)
    if not conn:
        return jsonify({"error": "not_found"}), 404
    if not conn.notifications_enabled:
        return jsonify({"error": "notifications_disabled"}), 409
    timeout = 30.0
    client_etag = request.headers.get('If-None-Match')
    state, payload, etag = conn.long_poll_notifications(client_etag, timeout)
    if state == "modified":
        resp = jsonify(payload)
        resp.headers['ETag'] = etag
        return resp
    from flask import Response
    if state == "timeout":
        r = Response(status=204)
        r.headers['ETag'] = etag
        return r
    else:
        r = Response(status=304)
        r.headers['ETag'] = etag
        return r

def _parse_signal_params(data):
    if not data:
        return None, (400, "No data provided")
    status = data.get('status')
    color = data.get('color')
    stone = data.get('stone', )
    count = data.get('count', 1)
    resends = data.get('resends', 12)
    resend_gap = data.get('resend_gap', 0)
    gap = data.get('gap', 0)
    # Convert string values to integers if needed
    if isinstance(status, str):
        try:
            status = gv.DICT_STATUS.get(status.upper(), gv.STATUS_ALL)
        except (ValueError, TypeError):
            return None, (400, "Invalid status value")
    if isinstance(color, str):
        color_map = {"red": gv.COLOR_RED, "green": gv.COLOR_GREEN, "blue": gv.COLOR_BLUE}
        cm = color_map.get(color.lower()) if isinstance(color, str) else None
        if cm is not None:
            color = cm
        else:
            color = gv.COLOR_RED
    if isinstance(stone, str):
        try:
            stone = gv.DICT_STONE.get(stone.lower(), gv.STONE_BRIDGE)
        except (ValueError, TypeError):
            return None, (400, "Invalid stone value")
    return (status, color, stone, count, resends, resend_gap, gap), None


@app.route('/api/v1/connections', methods=['POST'])
async def create_connection():
    data = request.json or {}
    mac_address = data.get('mac_address')
    # Require client-provided connection id
    conn_id = data.get('connection_id')
    if not isinstance(conn_id, str) or not conn_id:
        return jsonify({"success": False, "message": "connection_id is required"}), 400
    with connections_lock:
        if conn_id in connections:
            return jsonify({"success": False, "message": "Connection ID already exists"}), 409
        connections[conn_id] = Connection(
            notifications_enabled=bool(data.get('notifications', False)),
            bridge_mode_enabled=bool(data.get('bridge_mode', False))
        )
    # Connect and wait (with timeout) in the async context
    try:
        success = await asyncio.wait_for(connection_connect(conn_id, mac_address), timeout=30)
    except asyncio.TimeoutError:
        with connections_lock:
            connections.pop(conn_id, None)
        return jsonify({"success": False, "message": "Connection timed out"}), 504
    if success:
        return jsonify({"success": True, "connection_id": conn_id}), 200
    else:
        with connections_lock:
            connections.pop(conn_id, None)
        return jsonify({"success": False, "message": "Connection failed"}), 500


@app.route('/api/v1/connections', methods=['GET'])
def list_connections():
    with connections_lock:
        items = []
        for cid, c in connections.items():
            items.append({
                'connection_id': cid,
                'connected': c.connected,
                'mac_address': c.mac_address,
            })
    return jsonify({'connections': items})


@app.route('/api/v1/connections/<conn_id>', methods=['GET'])
def get_connection(conn_id):
    with connections_lock:
        conn = connections.get(conn_id)
    if not conn:
        return jsonify({"error": "not_found"}), 404
    return jsonify({
        'connection_id': conn_id,
        'connected': conn.connected,
        'notifications': conn.notifications_enabled,
        'bridge_mode': conn.bridge_mode_enabled,
        'mac_address': conn.mac_address,
    })


@app.route('/api/v1/connections/<conn_id>', methods=['PUT'])
async def update_connection(conn_id):
    with connections_lock:
        conn = connections.get(conn_id)
    if not conn:
        return jsonify({"error": "not_found"}), 404
    data = request.json or {}
    # Accept 'notifications' and 'bridge_mode' fields
    if 'notifications' in data:
        new_notif = bool(data.get('notifications', conn.notifications_enabled))
        await conn.apply_notifications_flag(new_notif, _make_notification_callback, conn_id)
    if 'bridge_mode' in data:
        new_bridge = bool(data.get('bridge_mode', conn.bridge_mode_enabled))
        await conn.apply_bridge_mode_flag(new_bridge)
    return jsonify({
        'connection_id': conn_id,
        'connected': conn.connected,
        'notifications': conn.notifications_enabled,
        'bridge_mode': conn.bridge_mode_enabled,
        'mac_address': conn.mac_address,
    })


@app.route('/api/v1/connections/<conn_id>', methods=['DELETE'])
async def delete_connection(conn_id):
    with connections_lock:
        conn = connections.get(conn_id)
    if not conn:
        return jsonify({"success": True}), 200
    result = await connection_disconnect(conn_id)
    with connections_lock:
        connections.pop(conn_id, None)
    if result:
        return jsonify({"success": True})
    else:
        return jsonify({"success": False, "message": "Disconnect failed"}), 500


@app.route('/api/v1/connections/<conn_id>/signals', methods=['POST'])
async def send_connection_signal(conn_id):
    with connections_lock:
        conn = connections.get(conn_id)
    if not conn:
        return jsonify({"success": False, "message": "Connection not found"}), 404
    if not conn.connected:
        return jsonify({"success": False, "message": "Not connected"}), 400
    params, err = _parse_signal_params(request.json)
    if err:
        code, msg = err
        return jsonify({"success": False, "message": msg}), code
    status, color, stone, count, resends, resend_gap, gap = params
    try:
        await conn.send(status, color, stone, count, resends, resend_gap, gap)
        return jsonify({"success": True})
    except Exception as e:
        gb.log_print(f"Error sending signal on {conn_id}: {e}")
        return jsonify({"success": False, "message": "Failed to send signal"}), 500


@app.route('/api/v1/connections/<conn_id>/notifications', methods=['GET'])
def get_connection_notifications(conn_id):
    with connections_lock:
        conn = connections.get(conn_id)
    if not conn:
        return jsonify({"error": "not_found"}), 404
    if not conn.notifications_enabled:
        return jsonify({"error": "notifications_disabled"}), 409
    timeout = 30.0
    client_etag = request.headers.get('If-None-Match')
    state, payload, etag = conn.long_poll_notifications(client_etag, timeout)
    if state == "modified":
        resp = jsonify(payload)
        resp.headers['ETag'] = etag
        return resp
    from flask import Response
    if state == "timeout":
        r = Response(status=204)
        r.headers['ETag'] = etag
        return r
    else:
        r = Response(status=304)
        r.headers['ETag'] = etag
        return r


@app.route('/api/v1/connections/<conn_id>/battery', methods=['GET'])
async def get_connection_battery(conn_id):
    with connections_lock:
        conn = connections.get(conn_id)
    if not conn:
        return jsonify({"error": "not_found"}), 404
    if not conn.connected:
        return jsonify({"error": "not_connected"}), 400
    try:
        battery = await conn.get_battery()
        return jsonify({"battery": battery})
    except Exception as e:
        gb.log_print(f"Error requesting battery on {conn_id}: {e}")
        return jsonify({"error": "battery_failed"}), 500


@app.route('/api/v1/connections/<conn_id>/info', methods=['GET'])
async def get_connection_info(conn_id):
    with connections_lock:
        conn = connections.get(conn_id)
    if not conn:
        return jsonify({"error": "not_found"}), 404
    if not conn.connected:
        return jsonify({"error": "not_connected"}), 400
    try:
        info = await conn.get_info()
        return jsonify({"info": info})
    except Exception as e:
        gb.log_print(f"Error requesting bridge info on {conn_id}: {e}")
        return jsonify({"error": "info_failed"}), 500


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=True)