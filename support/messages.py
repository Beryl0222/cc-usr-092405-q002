"""命令消息处理：统一入口 + message_id 幂等。

三地赛程在重复消息和服务重启中运行：同一条消息（相同 message_id）
无论投递多少次、进程是否重启过，都只生效一次，并返回首次结果。
"""

from . import catalog
from .catalog import ValidationError


class CommandHandler:
    def __init__(self, engine):
        self.engine = engine
        self.store = engine.store

    def handle(self, message):
        if not isinstance(message, dict):
            raise ValidationError("消息必须是对象")
        message_id = message.get("message_id")
        cmd_type = message.get("type")
        if not message_id:
            raise ValidationError("消息缺少 message_id")
        if not cmd_type:
            raise ValidationError("消息缺少 type")
        payload = message.get("payload", {})
        if not isinstance(payload, dict):
            raise ValidationError("payload 必须是对象")

        # 与调度共用同一把锁：并发重复消息时"查重→执行→落库"原子完成
        with self.store.lock:
            duplicate = self.store.get_command(message_id)
            if duplicate is not None:
                return self.store._loads(duplicate["response"], {}) | {"deduplicated": True}

            catalog.reject_diagnosis(payload)
            result = self._dispatch(cmd_type, payload)
            result = {"type": cmd_type, "message_id": message_id, "result": result}
            self.store.save_command(message_id, cmd_type, payload, result)
            return result

    # ------------------------------------------------------------------

    def _dispatch(self, cmd_type, payload):
        handler = getattr(self, f"cmd_{cmd_type}", None)
        if handler is None:
            raise ValidationError(f"未知命令类型: {cmd_type}")
        return handler(payload)

    # ---------- 登记：赛事资格与功能支持所需最少信息 ----------

    def cmd_register_team(self, p):
        self._require(p, ("team_id", "name", "city", "sport"))
        self.store.upsert_team(
            p["team_id"], p["name"], p["city"], p["sport"], p.get("leader"),
        )
        return {"team_id": p["team_id"]}

    def cmd_register_person(self, p):
        self._require(p, ("person_id", "name", "city"))
        if p.get("team_id") and self.store.get_team(p["team_id"]) is None:
            raise ValidationError(f"未知队伍: {p['team_id']}")
        needs = p.get("needs")
        if needs is not None:
            needs = catalog.normalize_needs(needs)
        elif p.get("category"):
            needs = catalog.needs_for_category(p["category"])
        else:
            needs = []
        self.store.upsert_person(
            p["person_id"], p["name"], p.get("team_id"), p["city"],
            p.get("category"), needs, status=p.get("status", "待报到"),
        )
        return {"person_id": p["person_id"], "needs": needs}

    def cmd_update_needs(self, p):
        self._require(p, ("person_id",))
        person = self.store.get_person(p["person_id"])
        if person is None:
            raise ValidationError(f"未知人员: {p['person_id']}")
        needs = catalog.normalize_needs(p["needs"])
        self.store.upsert_person(
            p["person_id"], person["name"], person["team_id"], person["city"],
            person["category"], needs,
        )
        return {"person_id": p["person_id"], "needs": needs}

    # ---------- 资源登记（有期限） ----------

    def cmd_register_venue(self, p):
        self._require(p, ("venue_id", "name", "city"))
        features = catalog.normalize_needs(p.get("features", []))
        self.store.upsert_venue(p["venue_id"], p["name"], p["city"], features)
        return {"venue_id": p["venue_id"], "features": features}

    def cmd_register_hotel(self, p):
        self._require(p, ("hotel_id", "name", "city"))
        self.store.upsert_hotel(p["hotel_id"], p["name"], p["city"])
        for room in p.get("rooms", []):
            self.store.add_room(room["room_id"], p["hotel_id"], room.get("features", []))
        return {"hotel_id": p["hotel_id"], "rooms": len(p.get("rooms", []))}

    def cmd_register_vehicle(self, p):
        self._require(p, ("vehicle_id", "city", "kind"))
        if p["kind"] not in ("accessible", "commuter"):
            raise ValidationError("车辆 kind 必须是 accessible 或 commuter")
        self.store.upsert_vehicle(
            p["vehicle_id"], p["city"], p["kind"], p.get("features", []),
            int(p.get("seats", 1)), p.get("active_until"),
            p.get("service_status", "in_service"),
        )
        return {"vehicle_id": p["vehicle_id"]}

    def cmd_vehicle_status(self, p):
        self._require(p, ("vehicle_id", "service_status"))
        vehicle = self.store.get_vehicle(p["vehicle_id"])
        if vehicle is None:
            raise ValidationError(f"未知车辆: {p['vehicle_id']}")
        self.store.upsert_vehicle(
            p["vehicle_id"], vehicle["city"], vehicle["kind"],
            self.store._loads(vehicle["features_json"], []), vehicle["seats"],
            vehicle["active_until"], p["service_status"],
        )
        return {"vehicle_id": p["vehicle_id"], "service_status": p["service_status"]}

    def cmd_register_equipment(self, p):
        self._require(p, ("item_id", "city", "eq_type"))
        self.store.upsert_equipment(
            p["item_id"], p["city"], p["eq_type"],
            p.get("active_until"), p.get("service_status", "in_service"),
        )
        return {"item_id": p["item_id"]}

    def cmd_register_volunteer(self, p):
        self._require(p, ("volunteer_id", "name", "city"))
        self.store.upsert_volunteer(
            p["volunteer_id"], p["name"], p["city"],
            p.get("skills", []), p.get("credential_until"),
        )
        return {"volunteer_id": p["volunteer_id"]}

    # ---------- 赛程与行程 ----------

    def cmd_register_event(self, p):
        self._require(p, ("event_id", "venue_id", "city", "sport", "stage",
                          "title", "start", "end"))
        if self.store.get_venue(p["venue_id"]) is None:
            raise ValidationError(f"未知场馆: {p['venue_id']}")
        self.store.upsert_event(
            p["event_id"], p["venue_id"], p["city"], p["sport"], p["stage"],
            p["title"], p["start"], p["end"],
        )
        for team_id in p.get("team_ids", []):
            self.store.add_team_event(team_id, p["event_id"])
        return {"event_id": p["event_id"]}

    def cmd_register_segment(self, p):
        self._require(p, ("segment_id", "team_id", "kind", "city", "start", "end"))
        if p["kind"] not in catalog.SEGMENT_KINDS:
            raise ValidationError(f"未知行程段类型: {p['kind']}")
        if p["kind"] == "lodging" and not p.get("person_id"):
            raise ValidationError("住宿段必须登记到具体个人（一人一房），不能使用团队段")
        if self.store.get_team(p["team_id"]) is None:
            raise ValidationError(f"未知队伍: {p['team_id']}")
        self.store.upsert_segment(
            p["segment_id"], p["team_id"], p["kind"], p["city"],
            p["start"], p["end"], person_id=p.get("person_id"),
            venue_id=p.get("venue_id"), hotel_id=p.get("hotel_id"),
            event_id=p.get("event_id"),
        )
        return {"segment_id": p["segment_id"]}

    def cmd_cover_segment(self, p):
        self._require(p, ("segment_id",))
        return self.engine.cover_segment(p["segment_id"])

    def cmd_cover_team(self, p):
        self._require(p, ("team_id",))
        results = []
        for segment in self.engine._future_segments_for_team(p["team_id"]):
            results.append(self.engine.cover_segment(segment["segment_id"]))
        return {"results": results}

    # ---------- 变更：只重排受影响行程 ----------

    def cmd_classification_change(self, p):
        self._require(p, ("person_id",))
        return self.engine.classification_change(
            p["person_id"], new_needs=p.get("needs"),
            category=p.get("category"), actor=p.get("actor", "医学分级人员"),
        )

    def cmd_schedule_delay(self, p):
        self._require(p, ("event_id", "delay_minutes"))
        return self.engine.schedule_delay(
            p["event_id"], int(p["delay_minutes"]),
            actor=p.get("actor", "赛区调度员"),
        )

    def cmd_equipment_failure(self, p):
        self._require(p, ("item_id",))
        return self.engine.equipment_failure(
            p["item_id"], actor=p.get("actor", "赛区调度员"),
        )

    # ---------- 紧急医疗 ----------

    def cmd_emergency_transport(self, p):
        self._require(p, ("team_id", "city", "start", "end", "reason"))
        return self.engine.emergency_transport(
            p["team_id"], p["city"], p["start"], p["end"], p["reason"],
            person_id=p.get("person_id"), actor=p.get("actor", "医疗应急组"),
            seats=int(p.get("seats", 1)),
            accessible_required=bool(p.get("accessible_required", False)),
        )

    # ---------- 跨城交接 ----------

    def cmd_open_handoff(self, p):
        self._require(p, ("person_id", "to_city"))
        return self.engine.open_handoff(
            p["person_id"], p["to_city"],
            outgoing_segment_id=p.get("outgoing_segment_id"),
            actor=p.get("actor", "运动员联络员"),
        )

    def cmd_accept_handoff(self, p):
        self._require(p, ("handoff_id", "incoming_segment_id"))
        return self.engine.accept_handoff(
            p["handoff_id"], p["incoming_segment_id"],
            actor=p.get("actor", "赛区调度员"),
        )

    # ---------- 工具 ----------

    @staticmethod
    def _require(payload, keys):
        for key in keys:
            if key not in payload or payload[key] in (None, ""):
                raise ValidationError(f"缺少必填字段: {key}")
