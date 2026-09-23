"""保障调度引擎。

核心约束：
* 车辆在任一重叠时间窗内只承诺给一支队伍（容量内同队多人可共享），
  因此同一辆无障碍车不可能同时被两支队伍拿到；
* 志愿者、单件器材、客房同样按重叠窗口互斥；
* 资源有期限（active_until / credential_until），过期不参与排程；
* 医学分级变化、赛程延误、器材故障只重排受影响的未来行程，
  其他段的承诺一律不动；
* 紧急医疗可优先占用普通通勤车，但必须给出理由，并通知原安排负责人；
* 每次改派落 changes 审计，实际影响（释放/新签/空档/抢占）进 impact。
"""

import uuid
from datetime import datetime, timezone

from . import catalog
from .catalog import ValidationError
from .store import Store

RESOURCE_KINDS = ("vehicle", "room", "equipment", "volunteer")
VENUE_SEGMENT_KINDS = ("checkin", "classification", "training", "competition")


def _new_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _parse_minute(iso_text):
    return datetime.strptime(iso_text, "%Y-%m-%dT%H:%M")


def _locked(method):
    """公共调度操作串行化：'查冲突→写承诺' 必须在同一把锁内完成。"""

    def wrapper(self, *args, **kwargs):
        with self.store.lock:
            return method(self, *args, **kwargs)

    wrapper.__name__ = method.__name__
    wrapper.__doc__ = method.__doc__
    return wrapper


class Engine:
    def __init__(self, store=None, now_fn=None):
        self.store = store or Store()
        self.now_fn = now_fn or (
            lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
        )

    # ------------------------------------------------------------------
    # 需求聚合
    # ------------------------------------------------------------------

    @staticmethod
    def _needs(row):
        """从数据库行读取并规范化功能需求列表。"""
        return catalog.normalize_needs(Store._loads(row["needs_json"], []))

    def segment_needs(self, segment):
        """段所需功能码：个人段取个人，团队段聚合全队最严格需求。"""
        if segment["person_id"]:
            return self._needs(self.store.get_person(segment["person_id"]))
        needs = set()
        for person in self.store.list_persons(team_id=segment["team_id"]):
            needs.update(self._needs(person))
        return sorted(needs)

    def team_size_with_companions(self, team_id):
        """座位需求：队员 + 需要陪护同席的人数。"""
        total = 0
        for person in self.store.list_persons(team_id=team_id):
            total += 1
            if "COMPANION_SEAT" in self._needs(person):
                total += 1
        return max(total, 1)

    # ------------------------------------------------------------------
    # 资源可用性
    # ------------------------------------------------------------------

    def _resource_busy(self, kind, resource_id, start, end,
                       seats_required=None, allow_team=None):
        """返回与窗口冲突的他队承诺；同队占用允许拼车（容量内）。"""
        rows = self.store.active_assignments_for(kind, resource_id, start, end)
        blockers = []
        used_seats = 0
        for row in rows:
            if allow_team and row["team_id"] == allow_team:
                used_seats += row["seats_taken"]
                continue
            blockers.append(row)
        if blockers:
            return blockers
        if seats_required is not None and kind == "vehicle":
            vehicle = self.store.get_vehicle(resource_id)
            if used_seats + seats_required > vehicle["seats"]:
                # 同队拼车也超容量，视为冲突，需另派车
                return rows
        return []

    def _not_expired(self, until):
        return until is None or until >= self.now_fn()

    def _find_vehicle(self, city, features, seats, start, end, team_id,
                      allow_kinds=("accessible", "commuter")):
        required = set(features)
        candidates = []
        for vehicle in self.store.list_vehicles(city=city):
            if vehicle["service_status"] != "in_service":
                continue
            if not self._not_expired(vehicle["active_until"]):
                continue
            if vehicle["kind"] not in allow_kinds:
                continue
            have = set(self.store._loads(vehicle["features_json"], []))
            if not required.issubset(have):
                continue
            if vehicle["seats"] < seats:
                continue
            blockers = self._resource_busy(
                "vehicle", vehicle["vehicle_id"], start, end,
                seats_required=seats, allow_team=team_id,
            )
            if blockers:
                continue
            candidates.append(vehicle)
        # 优先普通通勤车，把无障碍专车留给真正需要升降能力的队伍
        candidates.sort(key=lambda v: (0 if v["kind"] == "commuter" else 1, v["seats"]))
        return candidates[0] if candidates else None

    def _find_room(self, hotel_id, features, start, end):
        required = set(features)
        for room in self.store.list_rooms(hotel_id=hotel_id):
            have = set(self.store._loads(room["features_json"], []))
            if not required.issubset(have):
                continue
            if self._resource_busy("room", room["room_id"], start, end):
                continue
            return room
        return None

    def _find_equipment(self, city, eq_type, start, end):
        for item in self.store.list_equipment(city=city, eq_type=eq_type):
            if item["service_status"] != "in_service":
                continue
            if not self._not_expired(item["active_until"]):
                continue
            if self._resource_busy("equipment", item["item_id"], start, end):
                continue
            return item
        return None

    def _find_volunteers(self, city, skills, start, end):
        """为所需技能各找一名窗口空闲、资质有效的志愿者。

        一名志愿者可兼多项技能（如同时会轮椅推行与转移辅助），
        合并为一人、只产生一条分配；技能无法全部满足时返回缺口。
        """
        chosen = {}  # volunteer_id -> (row, 覆盖的技能集合)
        for skill in sorted(skills):
            pick = None
            for volunteer in self.store.list_volunteers(city=city):
                if not self._not_expired(volunteer["credential_until"]):
                    continue
                have = set(self.store._loads(volunteer["skills_json"], []))
                if skill not in have:
                    continue
                existing = chosen.get(volunteer["volunteer_id"])
                if existing is not None:
                    # 已选中的兼项志愿者直接复用，无需再查窗口
                    pick = volunteer
                    break
                if self._resource_busy("volunteer", volunteer["volunteer_id"], start, end):
                    continue
                pick = volunteer
                break
            if pick is None:
                return None, skill
            entry = chosen.setdefault(pick["volunteer_id"], [pick, set()])
            entry[1].add(skill)
        return [(row, covered) for row, covered in chosen.values()], None

    # ------------------------------------------------------------------
    # 覆盖一个行程段
    # ------------------------------------------------------------------

    def _release_segment_assignments(self, segment_id):
        released = []
        for assignment in self.store.active_assignments_for_segment(segment_id):
            self.store.release_assignment(assignment["assignment_id"])
            released.append(dict(assignment))
        return released

    @_locked
    def cover_segment(self, segment_id, _trigger=None):
        """为单个行程段安排全部所需资源；失败则登记保障空档。

        重排语义：先释放该段现有承诺，再按新窗口/新需求重选，
        其他段不受影响。
        """
        segment = self.store.get_segment(segment_id)
        if segment is None:
            raise ValidationError(f"未知行程段: {segment_id}")
        if segment["status"] == "cancelled":
            return {"segment_id": segment_id, "status": "cancelled",
                    "assignments": [], "gaps": []}

        needs = self.segment_needs(segment)
        start, end = segment["planned_start"], segment["planned_end"]
        city = segment["city"]
        released = self._release_segment_assignments(segment_id)

        made = []
        gaps = []

        def gap(kind, reason):
            gap_id = _new_id("gap")
            self.store.open_gap(gap_id, segment_id, kind, reason)
            gaps.append({"gap_id": gap_id, "resource_kind": kind, "reason": reason})

        def assign(kind, resource_id, seats=1, medical_override=0, reason=None):
            aid = _new_id("asg")
            self.store.add_assignment(
                aid, segment_id, kind, resource_id, city, start, end,
                segment["team_id"], seats_taken=seats,
                medical_override=medical_override, reason=reason,
            )
            made.append(aid)
            return aid

        kind = segment["kind"]

        if kind in VENUE_SEGMENT_KINDS:
            # 场馆是固定能力：能力不足直接空档，不占用其他资源
            venue = self.store.get_venue(segment["venue_id"]) if segment["venue_id"] else None
            if venue is None:
                gap("venue", "未指定场馆")
            else:
                venue_features = set(self.store._loads(venue["features_json"], []))
                required_venue = catalog.required_venue_features(needs)
                missing = required_venue - venue_features
                if missing:
                    gap("venue", f"场馆缺少无障碍能力: {sorted(missing)}")
            if not gaps:
                self._cover_equipment_and_volunteers(segment, needs, assign, gap)

        elif kind == "lodging":
            if not segment["hotel_id"]:
                gap("room", "未指定酒店")
            else:
                room_features = catalog.required_room_features(needs)
                room = self._find_room(segment["hotel_id"], room_features, start, end)
                if room is None:
                    gap("room", "酒店无符合房型/无障碍客房空闲")
                else:
                    assign("room", room["room_id"])
            # 住宿段仍可能需要陪护/引导志愿者
            skills = catalog.required_volunteer_skills(needs)
            volunteers, missing_skill = self._find_volunteers(city, skills, start, end)
            if missing_skill:
                gap("volunteer", f"缺少具备资质的志愿者: {missing_skill}")
            else:
                for volunteer, _skills in volunteers:
                    assign("volunteer", volunteer["volunteer_id"])

        elif kind == "transfer":
            seats = self.team_size_with_companions(segment["team_id"])
            vehicle_features = catalog.required_vehicle_features(needs)
            # 需要升降/轮椅固定等能力时只派无障碍车
            allow_kinds = ("accessible",) if vehicle_features else ("accessible", "commuter")
            vehicle = self._find_vehicle(
                city, vehicle_features, seats, start, end,
                segment["team_id"], allow_kinds=allow_kinds,
            )
            if vehicle is None:
                gap("vehicle", "无符合能力且时间空闲的车辆")
            else:
                assign("vehicle", vehicle["vehicle_id"], seats=seats)
            skills = catalog.required_volunteer_skills(needs)
            volunteers, missing_skill = self._find_volunteers(city, skills, start, end)
            if missing_skill:
                gap("volunteer", f"缺少具备资质的志愿者: {missing_skill}")
            else:
                for volunteer, _skills in volunteers:
                    assign("volunteer", volunteer["volunteer_id"])
        else:
            raise ValidationError(f"未知行程段类型: {kind}")

        if gaps:
            self.store.set_segment_status(segment_id, "gap")
        else:
            self.store.resolve_gaps_for(segment_id)
            self.store.set_segment_status(segment_id, "covered")

        return {
            "segment_id": segment_id,
            "status": "gap" if gaps else "covered",
            "released": released,
            "assignments": made,
            "gaps": gaps,
        }

    def _cover_equipment_and_volunteers(self, segment, needs, assign, gap):
        city = segment["city"]
        start, end = segment["planned_start"], segment["planned_end"]
        for eq_type in catalog.required_equipment_types(needs):
            item = self._find_equipment(city, eq_type, start, end)
            if item is None:
                gap("equipment", f"器材 {eq_type} 无空闲/已到期")
            else:
                assign("equipment", item["item_id"])
        skills = catalog.required_volunteer_skills(needs)
        volunteers, missing_skill = self._find_volunteers(city, skills, start, end)
        if missing_skill:
            gap("volunteer", f"缺少具备资质的志愿者: {missing_skill}")
        else:
            for volunteer, _skills in volunteers:
                assign("volunteer", volunteer["volunteer_id"])

    # ------------------------------------------------------------------
    # 增量重排
    # ------------------------------------------------------------------

    def _future_segments_for_team(self, team_id):
        return [s for s in self.store.list_segments(team_id=team_id)
                if s["planned_start"] >= self.now_fn() and s["status"] != "cancelled"]

    def _record_change(self, trigger_type, trigger_ref, reason, actor, impact):
        change_id = _new_id("chg")
        self.store.record_change(change_id, trigger_type, trigger_ref, reason, actor, impact)
        return change_id

    @_locked
    def classification_change(self, person_id, new_needs=None, category=None, actor="医学分级人员"):
        """医学分级变化：更新个人功能需求，仅重排该人及其所在队的未来行程。"""
        person = self.store.get_person(person_id)
        if person is None:
            raise ValidationError(f"未知人员: {person_id}")
        if new_needs is None and category is None:
            raise ValidationError("分级变化须提供新的功能需求或残疾类别")
        if category is not None:
            needs = catalog.needs_for_category(category)
        else:
            needs = catalog.normalize_needs(new_needs)

        self.store.upsert_person(
            person_id, person["name"], person["team_id"], person["city"],
            category or person["category"], needs,
        )
        self.store.set_person_status(person_id, "保障中", classification_status="classified")

        affected = self._future_segments_for_person_or_team(person_id, person["team_id"])
        impact = self._recover(affected)
        impact["person_id"] = person_id
        impact["needs_after"] = needs
        change_id = self._record_change(
            "classification_change", person_id,
            "医学分级结果变化，重排受影响未来行程", actor, impact,
        )
        impact["change_id"] = change_id
        return impact

    def _future_segments_for_person_or_team(self, person_id, team_id):
        ids = set()
        for segment in self.store.list_segments(person_id=person_id):
            if segment["planned_start"] >= self.now_fn() and segment["status"] != "cancelled":
                ids.add(segment["segment_id"])
        for segment in self._future_segments_for_team(team_id):
            ids.add(segment["segment_id"])
        return [self.store.get_segment(sid) for sid in sorted(ids)]

    @_locked
    def schedule_delay(self, event_id, delay_minutes, actor="赛区调度员"):
        """赛程延误：平移该赛程关联段，仅重排这些段。"""
        event = self.store.get_event(event_id)
        if event is None:
            raise ValidationError(f"未知赛程: {event_id}")
        if delay_minutes <= 0:
            raise ValidationError("延误分钟数必须为正")
        new_start = Store.shift_iso(event["scheduled_start"], delay_minutes)
        new_end = Store.shift_iso(event["scheduled_end"], delay_minutes)
        self.store.execute(
            "UPDATE events SET status='delayed',delay_minutes=delay_minutes+?,"
            "scheduled_start=?,scheduled_end=? WHERE event_id=?",
            (delay_minutes, new_start, new_end, event_id),
        )

        affected = [s for s in self.store.list_segments(city=event["city"])
                    if s["event_id"] == event_id and s["status"] != "cancelled"]
        for segment in affected:
            self.store.shift_segment(
                segment["segment_id"],
                Store.shift_iso(segment["planned_start"], delay_minutes),
                Store.shift_iso(segment["planned_end"], delay_minutes),
            )
        refreshed = [self.store.get_segment(s["segment_id"]) for s in affected]
        impact = self._recover(refreshed)
        impact["event_id"] = event_id
        impact["delay_minutes"] = delay_minutes
        change_id = self._record_change(
            "schedule_delay", event_id,
            f"赛程延误 {delay_minutes} 分钟，仅重排关联行程", actor, impact,
        )
        impact["change_id"] = change_id
        return impact

    @_locked
    def equipment_failure(self, item_id, actor="赛区调度员"):
        """器材故障：标记停用，仅重排仍在使用该器材的未来段。"""
        item = self.store.get_equipment(item_id)
        if item is None:
            raise ValidationError(f"未知器材: {item_id}")
        self.store.execute(
            "UPDATE equipment SET service_status='broken' WHERE item_id=?", (item_id,)
        )
        affected_ids = set()
        cutoff = self.now_fn()
        for assignment in self.store.list_assignments(status="scheduled"):
            if (assignment["resource_kind"] == "equipment"
                    and assignment["resource_id"] == item_id
                    and assignment["window_end"] > cutoff):
                affected_ids.add(assignment["segment_id"])
        affected = [self.store.get_segment(sid) for sid in sorted(affected_ids)]
        impact = self._recover(affected)
        impact["item_id"] = item_id
        change_id = self._record_change(
            "equipment_failure", item_id,
            "器材故障停用，仅重排受影响行程", actor, impact,
        )
        impact["change_id"] = change_id
        return impact

    def _recover(self, segments):
        """重排若干段，汇总实际影响。"""
        results = []
        for segment in segments:
            results.append(self.cover_segment(segment["segment_id"]))
        return {
            "affected_segments": [r["segment_id"] for r in results],
            "released": [a for r in results for a in r["released"]],
            "new_assignments": [aid for r in results for aid in r["assignments"]],
            "gaps": [g for r in results for g in r["gaps"]],
        }

    # ------------------------------------------------------------------
    # 紧急医疗：优先占用普通通勤资源
    # ------------------------------------------------------------------

    @_locked
    def emergency_transport(self, team_id, city, start, end, reason,
                            person_id=None, actor="医疗应急组", seats=1,
                            accessible_required=False):
        """紧急医疗派车。

        优先使用空闲车辆；空闲不足时可优先*占用普通通勤车*
        （kind=commuter，不含无障碍专车）。占用必须：
        1. 给出书面理由；2. 通知每支被影响队伍的原安排负责人；
        3. 被抢占的段立即重排（换车或形成空档并显式可见）。
        """
        if not reason or not str(reason).strip():
            raise ValidationError("紧急医疗占用必须说明理由")
        if self.store.get_team(team_id) is None:
            raise ValidationError(f"未知队伍: {team_id}")
        _parse_minute(start)
        _parse_minute(end)
        if end <= start:
            raise ValidationError("结束时间必须晚于开始时间")

        features = catalog.required_vehicle_features(
            self._needs(self.store.get_person(person_id)) if person_id else []
        )
        if accessible_required:
            features |= {"lift"}

        # 1) 空闲车（能力满足即可）
        vehicle = self._find_vehicle(
            city, features, max(seats, 1), start, end, team_id,
            allow_kinds=("accessible", "commuter"),
        )

        preempted = []
        notifications = []
        if vehicle is None:
            # 2) 占用普通通勤资源：找窗口内被其他队占用的 commuter
            vehicle, preempted = self._preempt_commuter(
                city, features, seats, start, end
            )

        if vehicle is None:
            # 医疗也无车可用：记录空档并报警（不伪造承诺）
            segment_id = self._ensure_emergency_segment(
                team_id, city, start, end, person_id)
            gap_id = _new_id("gap")
            self.store.open_gap(gap_id, segment_id, "vehicle", "紧急医疗无可用车辆（含通勤资源）")
            self.store.set_segment_status(segment_id, "gap")
            impact = {"segment_id": segment_id, "gaps": [{"gap_id": gap_id,
                      "resource_kind": "vehicle", "reason": "紧急医疗无可用车辆"}],
                      "preempted": [], "notifications": []}
            change_id = self._record_change("medical_override", None, reason, actor, impact)
            impact["change_id"] = change_id
            return impact

        segment_id = self._ensure_emergency_segment(team_id, city, start, end, person_id)
        aid = _new_id("asg")
        self.store.add_assignment(
            aid, segment_id, "vehicle", vehicle["vehicle_id"], city, start, end,
            team_id, seats_taken=max(seats, 1), medical_override=1, reason=reason,
        )
        self.store.set_segment_status(segment_id, "covered")
        self.store.resolve_gaps_for(segment_id)

        # 被抢占的段在医疗承诺落定后重排（它们拿不回这辆车）
        rerun = []
        for old in preempted:
            self.store.release_assignment(old["assignment_id"])
            result = self.cover_segment(old["segment_id"])
            rerun.append(result)
            notification = self._notify_preempted_team(old, reason, vehicle, start, end, actor)
            notifications.append(notification)

        impact = {
            "segment_id": segment_id,
            "vehicle_id": vehicle["vehicle_id"],
            "assignment_id": aid,
            "reason": reason,
            "preempted": [dict(p) for p in preempted],
            "rerouted": rerun,
            "notifications": notifications,
        }
        change_id = self._record_change(
            "medical_override", aid, reason, actor, impact,
        )
        for note in notifications:
            note["change_id"] = change_id
        impact["change_id"] = change_id
        return impact

    def _preempt_commuter(self, city, features, seats, start, end):
        """找一辆可抢占的普通通勤车及其窗口内的他队承诺。"""
        for vehicle in self.store.list_vehicles(city=city):
            if vehicle["kind"] != "commuter":
                continue  # 紧急占用权限只覆盖普通通勤资源
            if vehicle["service_status"] != "in_service":
                continue
            if not self._not_expired(vehicle["active_until"]):
                continue
            have = set(self.store._loads(vehicle["features_json"], []))
            if not set(features).issubset(have):
                continue
            if vehicle["seats"] < max(seats, 1):
                continue
            blocking = [
                row for row in self.store.active_assignments_for(
                    "vehicle", vehicle["vehicle_id"], start, end)
            ]
            if blocking and all(b["medical_override"] == 0 for b in blocking):
                return vehicle, [dict(b) for b in blocking]
        return None, []

    def _ensure_emergency_segment(self, team_id, city, start, end, person_id):
        segment_id = _new_id("seg")
        self.store.upsert_segment(
            segment_id, team_id, "transfer", city, start, end, person_id=person_id,
        )
        return segment_id

    def _notify_preempted_team(self, old_assignment, reason, vehicle, start, end, actor):
        team = self.store.get_team(old_assignment["team_id"])
        recipient = team["leader"] or f"赛区调度员:{team['city']}"
        subject = f"医疗应急占用车辆 {vehicle['vehicle_id']}，原行程需改派"
        body = (
            f"因紧急医疗（{reason}），{start}–{end} 车辆 {vehicle['vehicle_id']} "
            f"被优先占用，贵队行程段 {old_assignment['segment_id']} 的原车辆承诺已释放，"
            f"系统已尝试重新安排，请看护新安排或空档。通知人：{actor}。"
        )
        note_id = _new_id("ntf")
        self.store.add_notification(note_id, recipient, subject, body)
        return {"notification_id": note_id, "recipient": recipient,
                "segment_id": old_assignment["segment_id"],
                "team_id": old_assignment["team_id"], "subject": subject}

    # ------------------------------------------------------------------
    # 跨城交接：个人支持不能在跨城交接处断档
    # ------------------------------------------------------------------

    @_locked
    def open_handoff(self, person_id, to_city, outgoing_segment_id=None, actor="运动员联络员"):
        person = self.store.get_person(person_id)
        if person is None:
            raise ValidationError(f"未知人员: {person_id}")
        if to_city == person["city"] and outgoing_segment_id is None:
            raise ValidationError("交接目的城市须与当前城市不同")
        handoff_id = _new_id("hof")
        needs = self._needs(person)
        self.store.open_handoff(
            handoff_id, person_id, person["team_id"], person["city"], to_city,
            needs, outgoing_segment_id,
        )
        return {"handoff_id": handoff_id, "person_id": person_id,
                "from_city": person["city"], "to_city": to_city,
                "needs": needs, "status": "pending"}

    @_locked
    def accept_handoff(self, handoff_id, incoming_segment_id, actor="赛区调度员"):
        """接入方只有把 incoming 段的个人支持资源排齐，交接才能成立。"""
        handoff = self.store.get_handoff(handoff_id)
        if handoff is None:
            raise ValidationError(f"未知交接: {handoff_id}")
        if handoff["status"] != "pending":
            raise ValidationError(f"交接已结束: {handoff['status']}")
        segment = self.store.get_segment(incoming_segment_id)
        if segment is None:
            raise ValidationError(f"未知行程段: {incoming_segment_id}")
        if segment["person_id"] != handoff["person_id"]:
            raise ValidationError("接入段必须属于被交接人员")
        if segment["city"] != handoff["to_city"]:
            raise ValidationError("接入段不在目的城市")

        result = self.cover_segment(incoming_segment_id)
        if result["status"] != "covered":
            raise ValidationError(
                f"接入城市支持资源未排齐，交接不能成立: {result['gaps']}"
            )
        self.store.accept_handoff(handoff_id, incoming_segment_id)
        return {"handoff_id": handoff_id, "status": "accepted",
                "incoming_segment_id": incoming_segment_id,
                "assignments": result["assignments"]}

    @_locked
    def handoff_continuity(self):
        """检测跨城断档：pending 交接的接入时间已到仍未 accepted。"""
        now = self.now_fn()
        broken = []
        for handoff in self.store.list_handoffs():
            if handoff["status"] != "pending":
                continue
            incoming = None
            person_segments = self.store.list_segments(person_id=handoff["person_id"])
            incoming = [s for s in person_segments if s["city"] == handoff["to_city"]]
            incoming.sort(key=lambda s: s["planned_start"])
            if incoming and incoming[0]["planned_start"] <= now:
                broken.append({
                    "handoff_id": handoff["handoff_id"],
                    "person_id": handoff["person_id"],
                    "from_city": handoff["from_city"],
                    "to_city": handoff["to_city"],
                    "reason": "目的城市首个行程已开始但交接仍未被接入",
                })
        return broken
