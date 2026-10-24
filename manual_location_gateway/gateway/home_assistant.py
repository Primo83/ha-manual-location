"""Run the isolated prototype against synthetic HA, never the user's HA."""

import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import signal
import time

import aiohttp
from aiohttp import web


def matching_gps(value, point):
    return (
        isinstance(value, (list, tuple)) and len(value) == 2
        and all(isinstance(item, (int, float)) for item in value)
        and abs(value[0] - point["latitude"]) < 1e-8
        and abs(value[1] - point["longitude"]) < 1e-8
    )


class NativeHooks:
    """Actual HA REST operations and fsync of its own persisted checkpoint."""

    def __init__(self, base_url, token, tracker_id, person_id, config_dir, fault_control=None, *, control_token=None):
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": "Bearer " + token}
        self.history_key = control_token
        self.tracker_id = tracker_id
        self.person_id = person_id
        self.restore_path = Path(config_dir) / ".storage" / "core.restore_state"
        self.client = None
        self.fault_control = Path(fault_control) if fault_control else None

    def fault_enabled(self, name):
        if self.fault_control is None or not self.fault_control.exists():
            return False
        return json.loads(self.fault_control.read_text()).get(name) is True

    async def start(self):
        self.client = aiohttp.ClientSession(
            headers=self.headers, timeout=aiohttp.ClientTimeout(total=10),
            trust_env=False,
        )

    async def close(self):
        if self.client:
            await self.client.close()

    async def ready(self):
        for entity_id in (self.tracker_id, self.person_id):
            try:
                async with self.client.get(self.base_url + "/api/states/" + entity_id) as response:
                    if response.status != 200:
                        return False
                    state = await response.json()
                if state.get("entity_id") != entity_id or state.get("state") in {"unavailable", "unknown"}:
                    return False
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                return False
        return True

    async def check_source_session(self, expected):
        return await self._check_session(expected, control=False)

    async def check_control_session(self, expected):
        return await self._check_session(expected, control=True)

    async def check_normal_intent(self, intent_id, source_session, *, opening=False):
        try:
            async with self.client.get(self.base_url + "/api/manual_location/gateway_session?control=1") as response:
                if response.status != 200:
                    return False
                value = await response.json()
            return (isinstance(value, dict) and value.get("session") == source_session
                    and value.get("normal_intent_id") == intent_id and value.get("normal_desired") is True
                    and (not opening or value.get("resume_ready") is True) and value.get("person_id") == self.person_id
                    and value.get("tracker_id") == self.tracker_id)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return False

    async def _check_session(self, expected, *, control):
        try:
            path = "/api/manual_location/gateway_session" + ("?control=1" if control else "")
            async with self.client.get(self.base_url + path) as response:
                if response.status != 200:
                    return False
                value = await response.json()
            return isinstance(value, dict) and value.get("session") == expected
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return False

    async def core_boot(self):
        async with self.client.get(self.base_url + "/api/manual_location/gateway_session?control=1") as response:
            if response.status != 200:
                raise RuntimeError("native epoch unavailable")
            value = await response.json()
        nonce = value.get("core_boot") if isinstance(value, dict) else None
        if not isinstance(nonce, str) or not 16 <= len(nonce) <= 128:
            raise RuntimeError("native epoch unavailable")
        return nonce

    async def verify(self, point):
        for _ in range(60):
            matched = True
            for entity_id in (self.tracker_id, self.person_id):
                try:
                    async with self.client.get(self.base_url + "/api/states/" + entity_id) as response:
                        if response.status != 200:
                            matched = False
                            break
                        state = await response.json()
                    attributes = state.get("attributes", {})
                    gps = [attributes.get("latitude"), attributes.get("longitude")]
                    if not matching_gps(gps, point) or attributes.get("gps_accuracy") != point["gps_accuracy"]:
                        matched = False
                        break
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    matched = False
                    break
            if matched:
                return True
            await asyncio.sleep(0.05)
        return False

    async def phone_snapshot(self, point):
        snapshots = {}
        for entity_id in (self.tracker_id, self.person_id):
            async with self.client.get(self.base_url + "/api/states/" + entity_id) as response:
                if response.status != 200:
                    raise RuntimeError("phone snapshot unavailable")
                state = await response.json()
            attributes = state.get("attributes", {})
            if (state.get("entity_id") != entity_id
                    or not matching_gps([attributes.get("latitude"), attributes.get("longitude")], point)
                    or attributes.get("gps_accuracy") != point["gps_accuracy"]):
                return None
            context = state.get("context")
            snapshots[entity_id] = {"last_updated": state.get("last_updated"),
                                    "context_id": context.get("id") if isinstance(context, dict) else None}
        return snapshots

    async def record_phone(self, payload):
        if self.history_key is None:
            raise RuntimeError("phone history authentication unavailable")
        async with self.client.post(self.base_url + "/api/manual_location/gateway_history",
                                    json={**payload, "gateway_key": self.history_key}) as response:
            if response.status != 200:
                raise RuntimeError("phone history capture rejected")
            return await response.json()

    async def query_phone(self, binding):
        if self.history_key is None:
            raise RuntimeError("phone history authentication unavailable")
        async with self.client.post(self.base_url + "/api/manual_location/gateway_history/query",
                                    json={**binding, "gateway_key": self.history_key}) as response:
            if response.status != 200:
                raise RuntimeError("phone history query rejected")
            return await response.json()

    async def native_target(self):
        """Used only by the explicitly attested stopped-Core repair actor."""
        states = {}
        for entity_id in (self.tracker_id, self.person_id):
            async with self.client.get(self.base_url + "/api/states/" + entity_id) as response:
                if response.status != 200:
                    raise RuntimeError("native target unavailable")
                state = await response.json()
            if state.get("entity_id") != entity_id or state.get("state") in {"unknown", "unavailable"}:
                raise RuntimeError("native target unavailable")
            states[entity_id] = state
        person = states[self.person_id]
        attrs = person.get("attributes", {})
        point = {"person_id": self.person_id, **{key: attrs.get(key) for key in ("latitude", "longitude", "gps_accuracy")}}
        if (any(type(point[key]) not in {int, float} or not math.isfinite(point[key])
                for key in ("latitude", "longitude", "gps_accuracy")) or abs(point["latitude"]) > 90
                or abs(point["longitude"]) > 180 or point["gps_accuracy"] < 0):
            raise RuntimeError("native target unavailable")
        if not await self.verify(point):
            raise RuntimeError("native target mismatch")
        snapshots = await self.phone_snapshot(point)
        if snapshots is None:
            raise RuntimeError("native target mismatch")
        return {"point": point, "snapshots": snapshots,
                "native_timestamp": person.get("last_updated"), "operation_id": None}

    async def correct_history(self, payload):
        if self.history_key is None:
            raise RuntimeError("history correction authentication unavailable")
        async with self.client.post(self.base_url + "/api/manual_location/gateway_history/correct",
                                    json={**payload, "gateway_key": self.history_key}) as response:
            if response.status != 200:
                raise RuntimeError("history correction rejected")
            return await response.json()

    def checkpoint_disk(self, point):
        # Read the file HA created. Never construct or edit its contents here.
        if self.fault_enabled("native_disk_sync"):
            raise OSError("synthetic native disk sync failure")
        def verify_saved_records():
            records = json.loads(self.restore_path.read_text()).get("data", [])
            for entity_id in (self.tracker_id, self.person_id):
                record = next((item for item in records if item.get("state", {}).get("entity_id") == entity_id), None)
                if record is None:
                    raise RuntimeError("native checkpoint missing")
                attributes = record["state"].get("attributes", {})
                gps = [attributes.get("latitude"), attributes.get("longitude")]
                if not matching_gps(gps, point) or attributes.get("gps_accuracy") != point["gps_accuracy"]:
                    raise RuntimeError("native checkpoint state does not match")
                if entity_id == self.tracker_id:
                    extra = record.get("extra_data", {}).get("data", {})
                    if not matching_gps(extra.get("gps"), point) or extra.get("gps_accuracy") != point["gps_accuracy"]:
                        raise RuntimeError("native checkpoint extra data does not match")

        verify_saved_records()
        for target, flags in (
            (self.restore_path, os.O_RDONLY),
            (self.restore_path.parent, os.O_RDONLY | os.O_DIRECTORY),
        ):
            descriptor = os.open(target, flags)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        verify_saved_records()

    async def checkpoint(self, point, *, phase=None):
        if not await self.verify(point):
            raise RuntimeError("native state verification failed")
        if self.fault_enabled("before_native_checkpoint"):
            raise OSError("synthetic failure before native checkpoint")
        if phase is not None:
            await phase("checkpoint_inflight")
        async with self.client.post(
            self.base_url + "/api/services/homeassistant/save_persistent_states", json={},
        ) as response:
            if response.status != 200:
                raise RuntimeError("native checkpoint request failed")
            await response.read()
        if phase is not None:
            await phase("checkpoint_closed")
        await asyncio.to_thread(self.checkpoint_disk, point)


