"""Durable one-shot source changes, owned by the existing Store writer."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import asyncio

from .gateway_bridge import GatewayError
from .const import MAX_PERSONS

MAX_RETURNS = 100


def empty_schedule():
    return {"revision": 0, "manual": {}, "journal": None, "imported": False, "manual_cutoff_at": None}


def validate_schedule(value):
    if (not isinstance(value, dict) or set(value) != {"revision", "manual", "journal", "imported", "manual_cutoff_at"}
            or type(value["revision"]) is not int or value["revision"] < 0
            or type(value["imported"]) is not bool or not isinstance(value["manual"], dict)
            or len(value["manual"]) > MAX_RETURNS):
        raise ValueError("invalid return schedule")
    if value["manual_cutoff_at"] is not None and (type(value["manual_cutoff_at"]) is not int or value["manual_cutoff_at"] <= 0):
        raise ValueError("invalid manual cutoff")
    for key, item in value["manual"].items():
        if (not isinstance(key, str) or not 1 <= len(key) <= 120 or not isinstance(item, dict)
                or set(item) != {"at"} or type(item["at"]) is not int or not 0 < item["at"] <= 253402300799):
            raise ValueError("invalid return event")
    journal = value["journal"]
    if journal is not None and (
        not isinstance(journal, dict) or set(journal) != {"key", "expected_id", "at"}
        or not isinstance(journal["key"], str)
        or (journal["expected_id"] is not None and not isinstance(journal["expected_id"], str))
        or (journal["at"] is not None and type(journal["at"]) is not int)
    ):
        raise ValueError("invalid return journal")
    return deepcopy(value)


def switch_state(auto_custom=True, auto_ha=False):
    return {"custom": "pending" if auto_custom else "off",
            "normal": "pending" if auto_ha else "off", "reason": ""}


class SourceSchedule:
    """Use one Gateway cutoff only while GPS can open; keep all plans in Store."""

    def __init__(self, coordinator):
        self.owner = coordinator
        self.custom_ready = False
        self.boot_at = datetime.now(UTC)
        self.normal_session = {}

    def events(self, data=None, *, future=False):
        data = data if data is not None else self.owner.data
        gateway = self.owner.gateway
        if gateway is None:
            return []
        schedule = data.get("return_schedule", empty_schedule())
        events = [{"id": key, "at": item["at"], "origin": "manual",
                   "scenario_id": None, "scenario_name": None, "editable": True}
                  for key, item in schedule["manual"].items()]
        for key, plan in data["scenarios"].items():
            if (plan["entity_id"] != gateway.person_id or plan["status"] != "running"
                    or not plan.get("auto_custom", True)
                    or plan.get("switch_state", {}).get("custom") != "pending"):
                continue
            anchor = plan.get("switch_cursor", plan["cursor"])
            if anchor >= len(plan["points"]):
                continue
            at = int(datetime.fromisoformat(plan["points"][anchor]["timestamp"]).timestamp()) - 300
            events.append({"id": "scenario:" + key + ":" + plan["run_id"], "at": at,
                           "origin": "scenario", "scenario_id": key,
                           "scenario_name": plan["name"], "editable": False})
        now = datetime.now(UTC).timestamp()
        return sorted((item for item in events if not future or item["at"] > now),
                      key=lambda item: (item["at"], item["id"]))

    def status(self, status):
        events = self.events(future=True)
        plan = status.get("scheduled_return")
        if "return_schedule" not in self.owner.data and plan and plan["state"] == "armed":
            events.append({"id": "legacy-" + plan["id"], "at": plan["at"], "origin": "manual",
                           "scenario_id": None, "scenario_name": None, "editable": True})
        return {**status, "return_events": events,
                "multi_return_supported": status.get("scheduled_return_supported") is True,
                "return_schedule_revision": self.owner.data.get("return_schedule", empty_schedule())["revision"]}

    async def persist(self, snapshot):
        """Metadata-only transaction; never publish positions or grant Normal."""
        owner = self.owner
        snapshot["revision"] = owner.data["revision"] + 1
        try:
            await owner.store.async_save(snapshot)
        except Exception:
            if await owner.store.async_load() != snapshot:
                raise
        if await owner.store.async_load() != snapshot:
            from .coordinator import LocationError
            raise LocationError("storage_error")
        owner._require_loaded()
        owner.data = snapshot

    async def initialize(self, status):
        owner = self.owner
        if not status.get("scheduled_return_supported"):
            return
        schedule = owner.data.get("return_schedule")
        if schedule is not None and schedule["imported"]:
            return
        snapshot = deepcopy(owner.data)
        schedule = snapshot.setdefault("return_schedule", empty_schedule())
        plan = status.get("scheduled_return")
        if plan and plan["state"] == "armed":
            schedule["manual"]["legacy-" + plan["id"]] = {"at": plan["at"]}
            schedule["revision"] += 1
        schedule["imported"] = True
        await self.persist(snapshot)

    async def sync(self, status=None, *, opening=False):
        """Persist intent before CAS. A lost reply is recovered by saved target."""
        owner, gate = self.owner, self.owner.gateway
        if gate is None or "return_schedule" not in owner.data:
            return status
        status = status or await gate.status()
        if not status.get("scheduled_return_supported"):
            raise GatewayError("scheduled return unavailable")
        plan = status.get("scheduled_return")
        if plan and plan["state"] in {"due", "applied"}:
            return status
        schedule = owner.data["return_schedule"]
        journal = schedule["journal"]
        if journal:
            # The Gate receipt may have been lost after its durable CAS.
            actual_at = plan["at"] if plan and plan["state"] == "armed" else None
            expired = journal["at"] is not None and journal["at"] <= datetime.now(UTC).timestamp()
            if actual_at != journal["at"] and not (expired and self.confirmed_custom(status)):
                if (plan["id"] if plan else None) != journal["expected_id"]:
                    raise GatewayError("return journal generation changed")
                status = await gate.configure_return(journal["key"], journal["expected_id"], journal["at"])
                plan = status["scheduled_return"]
            snapshot = deepcopy(owner.data)
            snapshot["return_schedule"]["journal"] = None
            await self.persist(snapshot)
        events = self.events(future=True)
        suspended_intent = (status.get("suspended") and owner._normal_intent_valid()
                            and status.get("resume_intent_id") == owner.data["normal_intent"]["id"])
        desired = events[0]["at"] if events and (opening or suspended_intent or status["mode"] in {"normal", "warmup"}) else None
        actual_at = plan["at"] if plan and plan["state"] == "armed" else None
        if desired == actual_at:
            return status
        expected = plan["id"] if plan else None
        snapshot = deepcopy(owner.data)
        journal = {"key": gate.key(snapshot["revision"], "schedule", {"at": desired, "expected_id": expected}),
                   "expected_id": expected, "at": desired}
        snapshot["return_schedule"]["journal"] = journal
        await self.persist(snapshot)
        status = await gate.configure_return(journal["key"], expected, desired)
        snapshot = deepcopy(owner.data)
        snapshot["return_schedule"]["journal"] = None
        await self.persist(snapshot)
        return status

    async def enter_custom(self, status):
        owner, gate = self.owner, self.owner.gateway
        if status.get("handoff_supported"):
            latch = status.get("handoff")
            await gate.handoff(gate.key(owner.data["revision"], "schedule-handoff"),
                               latch["id"] if latch else None)
            if gate.person_id not in owner.data["persons"]:
                if len(owner.data["persons"]) >= MAX_PERSONS:
                    raise GatewayError("scheduled custom person limit")
                frozen = await gate.read_handoff()
                if not frozen or frozen.get("state") != "applied":
                    raise GatewayError("scheduled custom baseline unavailable")
                snapshot = deepcopy(owner.data)
                snapshot["persons"][gate.person_id] = {
                    "enabled": True, "current": deepcopy(frozen["current"]), "history": [deepcopy(frozen["current"])]}
                await self.persist(snapshot)
            status = await owner._async_gateway_handoff_reconcile(await gate.status())
        else:
            await owner._async_gateway_close(owner.data["revision"])
        person = owner.data["persons"].get(gate.person_id)
        if not person or not person.get("current"):
            raise GatewayError("scheduled custom baseline unavailable")
        if not person["enabled"] or owner.data.get("normal_intent") is not None:
            snapshot = deepcopy(owner.data)
            snapshot["persons"][gate.person_id]["enabled"] = True
            snapshot["normal_intent"] = None
            await self.persist(snapshot)
        await owner._async_gateway_sync()
        status = await gate.status()
        if not self.confirmed_custom(status):
            raise GatewayError("scheduled custom unconfirmed")
        return status

    async def adopt_due_handoff(self, status):
        """A due, explicitly saved switch can initialize a missing Own entry."""
        owner, gate = self.owner, self.owner.gateway
        latch = status.get("handoff")
        if (gate.person_id in owner.data["persons"] or not latch or latch["state"] != "applied"
                or not any(item["at"] <= datetime.now(UTC).timestamp() for item in self.events())):
            return
        frozen = await gate.read_handoff()
        if not frozen or frozen.get("id") != latch["id"] or frozen.get("state") != "applied":
            raise GatewayError("scheduled custom baseline unavailable")
        if len(owner.data["persons"]) >= MAX_PERSONS:
            raise GatewayError("scheduled custom person limit")
        snapshot = deepcopy(owner.data)
        snapshot["persons"][gate.person_id] = {
            "enabled": True, "current": deepcopy(frozen["current"]), "history": [deepcopy(frozen["current"])]}
        await self.persist(snapshot)

    @staticmethod
    def confirmed_custom(status):
        return (status.get("mode") == "custom" and status.get("gps_blocked") is True
                and status.get("has_custom_point") is True
                and not any(status.get(key) for key in ("pending", "replay_required", "upstream_uncertain", "recovery_retryable")))

    def conflict(self, data, now, *, exclude=None):
        gate = self.owner.gateway
        until = now.timestamp() + 300
        if any(item["at"] <= until for item in self.events(data)):
            return True
        for key, plan in data["scenarios"].items():
            if key == exclude or plan["entity_id"] != gate.person_id or plan["status"] != "running":
                continue
            # Also protect a route with automatic custom explicitly disabled.
            if datetime.fromisoformat(plan["points"][plan["cursor"]]["timestamp"]).timestamp() <= until:
                return True
        return False

    async def tick(self):
        owner, gate = self.owner, self.owner.gateway
        self.custom_ready = gate is None
        if gate is None:
            return
        status = await gate.status()
        await self.adopt_due_handoff(status)
        # Existing due/applied latches keep their Store-before-ACK recovery.
        status = await owner._async_gateway_return_reconcile(status)
        if "return_schedule" not in owner.data:
            self.custom_ready = self.confirmed_custom(status)
            return
        await self.initialize(status)
        now = datetime.now(UTC)
        due = [item for item in self.events() if item["at"] <= now.timestamp()]
        if due:
            status = await self.enter_custom(status)
            snapshot = deepcopy(owner.data)
            for item in due:
                if item["origin"] == "manual":
                    snapshot["return_schedule"]["manual"].pop(item["id"], None)
                    snapshot["return_schedule"]["manual_cutoff_at"] = max(
                        snapshot["return_schedule"]["manual_cutoff_at"] or 0, item["at"])
                else:
                    snapshot["scenarios"][item["scenario_id"]]["switch_state"]["custom"] = "done"
            snapshot["return_schedule"]["revision"] += 1
            await self.persist(snapshot)
        status = await self.sync(status)
        self.custom_ready = self.confirmed_custom(status)
        await self.finish_routes(status)

    async def finish_routes(self, status):
        owner, gate = self.owner, self.owner.gateway
        now = datetime.now(UTC)
        for key, route in list(owner.data["scenarios"].items()):
            if route["entity_id"] != gate.person_id or route["status"] != "completed":
                continue
            state = route.get("switch_state", {})
            if state.get("normal") not in {"pending", "attempting", "started"}:
                continue
            final_at = datetime.fromisoformat(route["points"][-1]["timestamp"])
            reason = None
            if not owner.data["enabled"]:
                reason = "normal_interrupted"
            elif state["normal"] in {"attempting", "started"}:
                if (status.get("suspended") and owner._normal_intent_valid()
                        and status.get("resume_intent_id") == owner.data["normal_intent"]["id"]):
                    continue
                if self.normal_session.get(key) == gate.session and status["mode"] in {"warmup", "normal"}:
                    if status["mode"] == "normal":
                        snapshot = deepcopy(owner.data)
                        snapshot["scenarios"][key]["switch_state"]["normal"] = "done"
                        await self.persist(snapshot)
                    continue
                reason = "normal_interrupted"
            elif final_at <= self.boot_at:
                reason = "normal_missed_restart"
            elif (now - final_at).total_seconds() > 30:
                reason = "normal_missed_deadline"
            elif not route.get("run_id"):
                reason = "normal_unconfirmed_run"
            elif (owner.data["return_schedule"]["manual_cutoff_at"] or 0) >= final_at.timestamp():
                reason = "normal_custom_conflict"
            elif self.conflict(owner.data, now, exclude=key):
                reason = "normal_custom_conflict"
            elif not self.confirmed_custom(status) or owner._gateway_applied != owner._gateway_current(owner.data):
                continue
            if reason:
                snapshot = deepcopy(owner.data)
                snapshot["scenarios"][key]["switch_state"].update(normal="skipped", reason=reason)
                await self.persist(snapshot)
                if status["mode"] == "warmup" or not owner.data["persons"].get(gate.person_id, {}).get("enabled"):
                    status = await self.enter_custom(status)
                continue
            # The durable attempting marker is consumed on restart, never replayed.
            snapshot = deepcopy(owner.data)
            snapshot["scenarios"][key]["switch_state"]["normal"] = "attempting"
            snapshot["persons"][gate.person_id]["enabled"] = False
            snapshot["normal_intent"] = owner._new_normal_intent() if gate.resume_supported else None
            await self.persist(snapshot)
            operation = gate.key(owner.data["revision"], "route-normal", route["run_id"])
            source_session = gate.session
            try:
                status = await self.sync(status, opening=True)
                if self.conflict(owner.data, datetime.now(UTC), exclude=key):
                    raise GatewayError("normal conflict changed")
                await owner.location_history.async_own_current()
                intent_id = owner.data["normal_intent"]["id"] if owner.data.get("normal_intent") else None
                await owner.location_history.begin_normal(operation, intent_id=intent_id)
                plan = status.get("scheduled_return")
                self.check_normal_consent(key, route["run_id"], source_session, final_at)
                remaining = 30 - (datetime.now(UTC) - final_at).total_seconds()
                if remaining <= 0:
                    raise GatewayError("normal deadline passed")
                await asyncio.wait_for(gate.normal(operation, expected_id=plan["id"] if plan else None, intent_id=intent_id),
                                       timeout=remaining)
                self.check_normal_consent(key, route["run_id"], source_session, final_at)
                self.normal_session[key] = gate.session
                snapshot = deepcopy(owner.data)
                snapshot["persons"][gate.person_id]["enabled"] = False
                snapshot["scenarios"][key]["switch_state"]["normal"] = "started"
                await self.persist(snapshot)
                owner._gateway_applied = None
                status = await gate.status()
            except (Exception, asyncio.CancelledError):
                owner._gateway_failed()
                await owner._async_gateway_close(owner.data["revision"])
                snapshot = deepcopy(owner.data)
                snapshot["scenarios"][key]["switch_state"].update(normal="skipped", reason="normal_unconfirmed")
                snapshot["persons"][gate.person_id]["enabled"] = True
                snapshot["normal_intent"] = None
                await self.persist(snapshot)
                raise

    def check_normal_consent(self, key, run_id, source_session, final_at):
        """Recheck after every preparation await and after the grant response."""
        owner, now = self.owner, datetime.now(UTC)
        route = owner.data["scenarios"].get(key, {})
        if (not owner.data["enabled"] or not route.get("auto_ha")
                or route.get("status") != "completed" or route.get("run_id") != run_id
                or route.get("switch_state", {}).get("normal") != "attempting"
                or owner.gateway.session != source_session
                or (now - final_at).total_seconds() > 30
                or self.conflict(owner.data, now, exclude=key)):
            raise GatewayError("normal consent expired or changed")

    def deadlines(self):
        result = [datetime.fromtimestamp(item["at"], UTC) for item in self.events()]
        if any(plan["status"] == "completed" and plan.get("switch_state", {}).get("normal") in {"pending", "attempting"}
               for plan in self.owner.data["scenarios"].values()):
            result.append(datetime.now(UTC) + timedelta(seconds=5))
        return result
