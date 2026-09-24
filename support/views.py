"""只读视图：公众赛程（脱敏）与内部运营视图。"""

from .store import Store


def _loads(row, key, default):
    return Store._loads(row[key], default) if row is not None else default


class Views:
    def __init__(self, store):
        self.store = store

    # ------------------------------------------------------------------
    # 公众赛程：只含公众本来就该知道的信息
    # ------------------------------------------------------------------

    def public_schedule(self, city=None):
        """公众赛程。

        明确不含：人员姓名/残疾类别/功能需求、车辆与房间安排、
        志愿者身份、改派理由、紧急占用理由——任何一条都能反向
        暴露敏感需求。延误只呈现"已延时及新时间"，不呈现原因。
        """
        events = self.store.list_events(city=city)
        result = []
        for event in events:
            if event["status"] == "cancelled":
                continue
            venue = self.store.get_venue(event["venue_id"])
            item = {
                "event_id": event["event_id"],
                "title": event["title"],
                "sport": event["sport"],
                "stage": event["stage"],
                "city": event["city"],
                "venue_name": venue["name"] if venue else None,
                "start": event["scheduled_start"],
                "end": event["scheduled_end"],
                "status": event["status"],
            }
            result.append(item)
        return {"schedule": result}

    # ------------------------------------------------------------------
    # 内部视图：每次改派及其实际影响都可回看
    # ------------------------------------------------------------------

    def segment_detail(self, segment_id):
        segment = self.store.get_segment(segment_id)
        if segment is None:
            return None
        assignments = []
        for row in self.store.list_assignments(segment_id=segment_id):
            assignments.append({
                "assignment_id": row["assignment_id"],
                "resource_kind": row["resource_kind"],
                "resource_id": row["resource_id"],
                "window_start": row["window_start"],
                "window_end": row["window_end"],
                "team_id": row["team_id"],
                "seats_taken": row["seats_taken"],
                "status": row["status"],
                "medical_override": bool(row["medical_override"]),
                "override_reason": row["override_reason"],
            })
        return {
            "segment_id": segment["segment_id"],
            "person_id": segment["person_id"],
            "team_id": segment["team_id"],
            "event_id": segment["event_id"],
            "kind": segment["kind"],
            "city": segment["city"],
            "planned_start": segment["planned_start"],
            "planned_end": segment["planned_end"],
            "status": segment["status"],
            "version": segment["version"],
            "assignments": assignments,
        }

    def change_log(self, limit=100):
        """改派审计流：触发类型、理由、动作人、实际影响。"""
        return {"changes": self.store.list_changes(limit=limit)}

    def notifications(self):
        rows = self.store.list_notifications()
        return {"notifications": [dict(r) for r in rows]}

    def command_conflicts(self, message_id=None):
        """消息编号复用冲突：首次与冲突双方的摘要，供值守交接追踪。"""
        return {"conflicts": self.store.list_command_conflicts(message_id=message_id)}

    def open_gaps(self, city=None):
        rows = self.store.list_open_gaps(city=city)
        return {"gaps": [dict(r) for r in rows]}

    def handoffs(self, status=None):
        rows = self.store.list_handoffs(status=status)
        result = []
        for row in rows:
            item = dict(row)
            item["needs"] = _loads(row, "needs_json", [])
            item.pop("needs_json", None)
            result.append(item)
        return {"handoffs": result}

    def resource_utilization(self, city=None):
        """资源承诺视图：同一资源的时间窗承诺，便于核对无双占。"""
        vehicles = []
        for vehicle in self.store.list_vehicles(city=city):
            commits = [
                {
                    "segment_id": a["segment_id"],
                    "team_id": a["team_id"],
                    "window_start": a["window_start"],
                    "window_end": a["window_end"],
                    "status": a["status"],
                    "medical_override": bool(a["medical_override"]),
                }
                for a in self.store.query(
                    "SELECT * FROM assignments WHERE resource_kind='vehicle' "
                    "AND resource_id=? ORDER BY window_start",
                    (vehicle["vehicle_id"],),
                )
            ]
            vehicles.append({
                "vehicle_id": vehicle["vehicle_id"],
                "city": vehicle["city"],
                "kind": vehicle["kind"],
                "seats": vehicle["seats"],
                "features": _loads(vehicle, "features_json", []),
                "active_until": vehicle["active_until"],
                "service_status": vehicle["service_status"],
                "commits": commits,
            })
        return {"vehicles": vehicles}
