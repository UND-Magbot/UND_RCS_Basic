"""로봇 모델별 charge_contact 오프셋 (도킹 완료 시 로봇 body 중심에서 pile 까지의 거리).

AutoXing `device/info` 응답의 `robot.charge_contact.pose_2d = [x, y, yaw]` 에서
y 성분의 절댓값이 로봇 body-to-pile 거리 (모든 모델이 charge_contact_position=back).

sync 시 이 값을 사용해 도킹 POI 좌표 → 실제 pile 좌표를 정확히 계산한다.
로봇 오프라인 시 폴백으로 사용되는 매핑.

실측 (2026-07-23):
    crawler_heavy     : -0.5033
    crawler_s300_op5  : -0.369
    hawk_longtray     : -0.369
    longjack          : -0.369
"""
from __future__ import annotations

# 모델명 → charge_contact 오프셋 (m, 양수)
MODEL_TO_CHARGE_OFFSET: dict[str, float] = {
    "crawler_heavy": 0.5033,
    "crawler_s300_op5": 0.369,
    "hawk_longtray": 0.369,
    "longjack": 0.369,
}

# 모델 매핑에 없을 때의 기본값 (가장 흔한 값)
DEFAULT_CHARGE_OFFSET: float = 0.369

# ── charge 이동 정밀도 관측 (2026-07-23 실증 결과) ──────────────────
# 실험 결과 target_accuracy 는 charge 접근 궤적 편차를 증폭시켜 **좌우 오차 악화**:
#   원래 로직 (파라미터 없음)     : lateral range ~5mm
#   target_accuracy=0.003 명시    : lateral range ~9.6mm (반복 실험 확인)
# → target_accuracy 는 charge 에 **넣지 않는다**. charge_retry_count 만 유지.
#   반복 정밀도는 하드웨어/펌웨어 한계 (약 5mm) — 관제에서 로깅으로 모니터링.
CHARGE_RETRY_COUNT: int = 3                  # 원래 값 유지 (5는 검증 미완)


def offset_for_model(model: str | None) -> float:
    """모델명 → charge_contact 오프셋 (m). 매핑 없으면 DEFAULT."""
    if not model:
        return DEFAULT_CHARGE_OFFSET
    return MODEL_TO_CHARGE_OFFSET.get(model, DEFAULT_CHARGE_OFFSET)


def fetch_charge_contact_offset(robot_ip: str, fallback_model: str | None = None,
                                 timeout: float = 3.0) -> float:
    """로봇 `device/info` 실시간 조회 → charge_contact.pose_2d.y 절댓값 반환.

    실패 시 fallback_model 기반 매핑값, 그것도 없으면 DEFAULT_CHARGE_OFFSET.
    """
    import requests
    try:
        r = requests.get(f"http://{robot_ip}:8090/device/info", timeout=timeout)
        r.raise_for_status()
        data = r.json()
        robot = data.get("device", {}).get("hardware", {}).get("robot") or data.get("robot", {})
        cc = robot.get("charge_contact") or {}
        pose = cc.get("pose_2d")
        if isinstance(pose, (list, tuple)) and len(pose) >= 2:
            return abs(float(pose[1]))
    except Exception:
        pass
    return offset_for_model(fallback_model)
