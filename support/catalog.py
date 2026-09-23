"""领域目录：功能需求码与资源能力的统一词表。

保障中心只登记"功能支持所需的最少信息"，不登记诊断。任何携带
诊断类字段（diagnosis / 诊断 / 残疾类别诊断 等）的登记请求都会
被拒绝；残疾类别只用于决定需要哪一类车辆、器具、志愿者技能与
应急支持，类别本身不是诊断。
"""

# 受支持的功能需求码（minimal functional-needs codes）。
# 这些码描述"需要什么支持"，不描述"为什么需要"。
FUNCTIONAL_NEEDS = {
    "WHEELCHAIR": "全程轮椅通行（坡道、宽门、无障碍客房、升降车）",
    "WHEELCHAIR_SPORT": "竞赛轮椅（竞赛轮椅由器材库保障，非代步轮椅）",
    "TRANSFER_ASSIST": "转移协助（座面转移、上下车需要志愿者辅助）",
    "VISION_GUIDE": "视障引导（引导员、触觉标识、引导音频）",
    "HEARING_SUPPORT": "听障支持（手语志愿者、文字/视觉提示）",
    "INTELLECTUAL_SUPPORT": "智力支持（熟悉特奥流程的志愿者、简化指引、陪伴引导）",
    "SERVICE_ANIMAL": "导盲犬/辅助犬空间与排泄区域",
    "COMPANION_SEAT": "陪护人员同车同席",
    "MEDICAL_OXYGEN": "随车/随房氧气接口与存放空间",
    "LIFT_VEHICLE": "带升降平台的无障碍车辆",
    "GROUND_FLOOR": "低层/可电梯直达客房",
    "BARIATRIC": "加宽床、加宽车门与承重辅具",
}

# 诊断类字段名：出现在登记载荷里一律拒绝（不公开诊断）。
FORBIDDEN_FIELDS = frozenset(
    [
        "diagnosis",
        "diagnoses",
        "medical_diagnosis",
        "disability_diagnosis",
        "clinical_note",
        "patient_history",
        "诊断",
        "医学诊断",
        "临床诊断",
        "诊断名称",
        "病历",
        "病史",
        "病因",
        "病理",
    ]
)

# 残疾类别 -> 功能需求码默认集合（仅决定资源类型，不触及诊断）。
CATEGORY_PROFILES = {
    "肢体": ["WHEELCHAIR", "TRANSFER_ASSIST", "LIFT_VEHICLE", "GROUND_FLOOR"],
    "视力": ["VISION_GUIDE", "COMPANION_SEAT"],
    "听力": ["HEARING_SUPPORT"],
    "智力": ["INTELLECTUAL_SUPPORT", "COMPANION_SEAT"],
}

# 功能需求 -> 需要车辆具备的能力
NEED_VEHICLE_FEATURES = {
    "WHEELCHAIR": ["lift", "wheelchair_lock"],
    "LIFT_VEHICLE": ["lift", "wheelchair_lock"],
    "TRANSFER_ASSIST": ["lift"],
    "BARIATRIC": ["wide_door", "heavy_duty"],
    "MEDICAL_OXYGEN": ["oxygen_storage"],
    # COMPANION_SEAT 不要求特殊装置：陪护人的座位由座位计数（+1）体现，
    # 避免普通无障碍车因缺少 companion_seat 装置而派不出去。
    "VISION_GUIDE": [],
    "HEARING_SUPPORT": [],
    "INTELLECTUAL_SUPPORT": [],
    "SERVICE_ANIMAL": ["animal_space"],
    "GROUND_FLOOR": [],
}

# 功能需求 -> 场馆无障碍能力（场馆直接按功能需求码声明能力）
NEED_VENUE_FEATURES = {
    "WHEELCHAIR": ["WHEELCHAIR"],
    "TRANSFER_ASSIST": ["WHEELCHAIR"],
    "VISION_GUIDE": ["VISION_GUIDE"],
    "HEARING_SUPPORT": ["HEARING_SUPPORT"],
    "INTELLECTUAL_SUPPORT": ["INTELLECTUAL_SUPPORT"],
    "SERVICE_ANIMAL": ["SERVICE_ANIMAL"],
    "MEDICAL_OXYGEN": ["MEDICAL_OXYGEN"],
    "BARIATRIC": ["BARIATRIC"],
}

# 功能需求 -> 客房需要具备的属性
NEED_ROOM_FEATURES = {
    "WHEELCHAIR": ["accessible_room", "roll_in_shower"],
    "GROUND_FLOOR": ["low_level_access"],
    "VISION_GUIDE": ["braille_signage"],
    "HEARING_SUPPORT": ["visual_alert"],
    "SERVICE_ANIMAL": ["animal_friendly"],
    "MEDICAL_OXYGEN": ["oxygen_outlet"],
    "BARIATRIC": ["wide_bed"],
    "COMPANION_SEAT": ["companion_bed"],
}

# 功能需求 -> 志愿者技能
NEED_VOLUNTEER_SKILLS = {
    "WHEELCHAIR": ["wheelchair_handling"],
    "TRANSFER_ASSIST": ["transfer_assist"],
    "VISION_GUIDE": ["sighted_guide"],
    "HEARING_SUPPORT": ["sign_language"],
    "INTELLECTUAL_SUPPORT": ["special_olympics_care"],
    "SERVICE_ANIMAL": ["animal_etiquette"],
    "MEDICAL_OXYGEN": ["oxygen_handling"],
    "BARIATRIC": ["bariatric_assist"],
}

# 器材类型（功能需求 -> 器材类型）
# 日常轮椅属运动员个人辅具，不进赛会器材库；
# 赛会只保障竞赛轮椅、氧气、承重辅具等可调度器材。
NEED_EQUIPMENT_TYPES = {
    "WHEELCHAIR_SPORT": "sport_wheelchair",
    "MEDICAL_OXYGEN": "oxygen_cylinder",
    "BARIATRIC": "bariatric_aid",
}

# 场馆必须按其承办项目声明无障碍能力码，同 FUNCTIONAL_NEEDS 的键。
# 行程段类型
SEGMENT_KINDS = ("checkin", "classification", "training", "competition", "lodging", "transfer")

# 三个联合承办城市
HOST_CITIES = ("城市A", "城市B", "城市C")

# 分配状态
ASSIGNMENT_STATUS = ("scheduled", "fulfilled", "released")

# 行程段状态
SEGMENT_STATUS = ("planned", "covered", "gap", "cancelled")

# 个人支持状态（参考 fixtures/domain.json）
PERSON_STATUS = ("待报到", "已分级", "保障中", "需改派", "已离赛")


class ValidationError(ValueError):
    """登记/命令载荷不合法。"""


def reject_diagnosis(payload):
    """载荷中出现诊断类字段直接拒绝，绝不落库。"""
    if not isinstance(payload, dict):
        return
    for key in payload:
        if str(key).lower() in FORBIDDEN_FIELDS or key in FORBIDDEN_FIELDS:
            raise ValidationError(f"保障中心不登记诊断信息，字段 {key!r} 不被接受")


def normalize_needs(needs):
    """把功能需求列表规范化为去重后的合法码列表。"""
    if needs is None:
        return []
    if isinstance(needs, str):
        needs = [needs]
    result = []
    for code in needs:
        code = str(code).strip().upper()
        if code not in FUNCTIONAL_NEEDS:
            raise ValidationError(f"未知功能需求码: {code}")
        if code not in result:
            result.append(code)
    return result


def needs_for_category(category):
    """残疾类别 -> 默认功能需求码列表（类别不是诊断）。"""
    if category not in CATEGORY_PROFILES:
        raise ValidationError(f"未知残疾类别: {category}")
    return list(CATEGORY_PROFILES[category])


def required_vehicle_features(needs):
    features = set()
    for need in needs:
        features.update(NEED_VEHICLE_FEATURES.get(need, ()))
    return features


def required_room_features(needs):
    features = set()
    for need in needs:
        features.update(NEED_ROOM_FEATURES.get(need, ()))
    return features


def required_venue_features(needs):
    """场馆按功能需求码声明可承接的能力（与客房/车辆的具体属性码不同）。"""
    features = set()
    for need in needs:
        features.update(NEED_VENUE_FEATURES.get(need, ()))
    return features


def required_volunteer_skills(needs):
    skills = set()
    for need in needs:
        skills.update(NEED_VOLUNTEER_SKILLS.get(need, ()))
    return skills


def required_equipment_types(needs):
    return sorted({eq for need in needs for eq in [NEED_EQUIPMENT_TYPES.get(need)] if eq})
