"""
S600 + S300 결합(견인) 주행 테스트 API
- S600 풋프린트를 S600 + 좌/우 S300 합성 외곽선으로 교체
- 원본 풋프린트/속도/auto_hold 는 파일로 백업 → restore 로 복구
"""
import json
import logging
import math
import threading
import time
from datetime import datetime
from pathlib import Path

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.database import SessionLocal
from app.models.map import MapPOI
from app.services.jack_service import (
    robot_get, robot_post, robot_patch,
    create_move, wait_move, cancel_current_move,
    stop_robot_job, update_job_status, clear_job_status, get_job_status,
    _interruptible_sleep, _stop_flags,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tow-test", tags=["견인 테스트"])

BACKUP_DIR = Path(__file__).resolve().parents[2] / "tow_backups"

FOOTPRINT_KEY = "robot.footprint"
SPEED_PARAM_KEYS = {
    "max_forward_velocity": "/wheel_control/max_forward_velocity",
    "max_forward_acc": "/wheel_control/max_forward_acc",
    "max_angular_velocity": "/wheel_control/max_angular_velocity",
}
AUTO_HOLD_KEY = "/planning/auto_hold"
DY_WARN_M = 0.02
FOOTPRINT_VERIFY_SEC = 5.0


# ── 스키마 ──

class SideRobot(BaseModel):
    ip: str = ""
    dx: float = 0.0           # S600 회전중심 기준 좌우 (+오른쪽, 좌측 음수)
    dy: float = 0.0           # 앞뒤 (+앞)
    yaw_deg: float = 0.0
    footprint: list[list[float]] | None = None  # 지정 시 로봇 조회 대신 사용


class TowConfig(BaseModel):
    main_ip: str
    left: SideRobot | None = None
    right: SideRobot | None = None
    margin: float = 0.03
    # 간단 모드: S300 조회 없이 S600 외곽 기준으로 확장 (m)
    expand_left: float = 0.0
    expand_right: float = 0.0
    expand_front: float = 0.0
    expand_back: float = 0.0
    # 결합체 전체 실측 크기 (m). 지정 시 S600 외곽 기준으로 좌/우·앞/뒤 균등 확장값을 자동 계산
    total_width: float = 0.0
    total_length: float = 0.0


class SpeedLimits(BaseModel):
    max_forward_velocity: float | None = None
    max_forward_acc: float | None = None
    max_angular_velocity: float | None = None


class ApplyRequest(TowConfig):
    speed: SpeedLimits | None = None
    free_side_wheels: bool = True  # S300 auto_hold=false


# ── 기하 계산 ──

def transform(points, dx, dy, yaw_deg=0.0):
    c, s = math.cos(math.radians(yaw_deg)), math.sin(math.radians(yaw_deg))
    return [(c * x - s * y + dx, s * x + c * y + dy) for x, y in points]


def convex_hull(points):
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def inflate(hull, margin):
    # 모든 변을 바깥 법선 방향으로 margin 만큼 평행이동 (hull 은 반시계)
    n = len(hull)
    if n < 3 or margin == 0:
        return list(hull)

    def normal(a, b):
        ex, ey = b[0] - a[0], b[1] - a[1]
        d = math.hypot(ex, ey)
        return ey / d, -ex / d

    out = []
    for i in range(n):
        prev, cur, nxt = hull[i - 1], hull[i], hull[(i + 1) % n]
        n1, n2 = normal(prev, cur), normal(cur, nxt)
        bx, by = n1[0] + n2[0], n1[1] + n2[1]
        k = margin / max(bx * n1[0] + by * n1[1], 1e-6)
        out.append((cur[0] + bx * k, cur[1] + by * k))
    return out


def _metrics(poly):
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return {
        "width": round(max(xs) - min(xs), 3),
        "length": round(max(ys) - min(ys), 3),
        "turn_radius": round(max(math.hypot(x, y) for x, y in poly), 3),
    }


def _round_poly(poly):
    return [[round(x, 4), round(y, 4)] for x, y in poly]


# ── 로봇 조회 / 백업 ──

def _extract_footprint(settings: dict):
    fp = settings.get(FOOTPRINT_KEY)
    if fp is None and isinstance(settings.get("robot"), dict):
        fp = settings["robot"].get("footprint")
    return fp


def _get_effective_footprint(ip: str):
    return _extract_footprint(robot_get(ip, "/system/settings/effective"))


def _backup_path(ip: str) -> Path:
    return BACKUP_DIR / f"{ip.replace(':', '_')}.json"


def _load_backup(ip: str) -> dict | None:
    p = _backup_path(ip)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _save_backup(ip: str, data: dict):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    _backup_path(ip).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _robot_error(ip: str, e: Exception):
    return HTTPException(502, f"로봇 통신 실패 ({ip}): {e}")


def _build_preview(cfg: TowConfig) -> dict:
    # 백업이 있으면 백업 원본 기준 (이미 합성 풋프린트가 적용된 상태일 수 있으므로)
    backup = _load_backup(cfg.main_ip)
    if backup and backup.get("footprint"):
        main_fp = backup["footprint"]
    else:
        try:
            main_fp = _get_effective_footprint(cfg.main_ip)
        except requests.RequestException as e:
            raise _robot_error(cfg.main_ip, e)
    if not main_fp:
        raise HTTPException(400, f"S600 풋프린트를 읽을 수 없습니다 ({cfg.main_ip})")

    all_points = [tuple(p) for p in main_fp]
    sides = {}
    warnings = []
    for name, side in (("left", cfg.left), ("right", cfg.right)):
        if side is None or (not side.ip and not side.footprint):
            continue
        fp = side.footprint
        if not fp:
            side_backup = _load_backup(side.ip)
            try:
                fp = (side_backup or {}).get("footprint") or _get_effective_footprint(side.ip)
            except requests.RequestException as e:
                raise _robot_error(side.ip, e)
        if not fp:
            raise HTTPException(400, f"S300 풋프린트를 읽을 수 없습니다 ({side.ip})")
        moved = transform([tuple(p) for p in fp], side.dx, side.dy, side.yaw_deg)
        all_points += moved
        sides[name] = {"ip": side.ip, "original": fp, "transformed": _round_poly(moved)}

        label = "좌" if name == "left" else "우"
        if abs(side.dy) > DY_WARN_M:
            warnings.append(f"{label} S300 dy={side.dy:.3f}m — 바퀴축 불일치, 회전 시 바퀴 옆 끌림/슬립 가능")
        if abs(side.yaw_deg) > 0.5:
            warnings.append(f"{label} S300 yaw={side.yaw_deg:.1f}° — 바퀴 방향 불일치, 주행 시 옆 끌림 가능")
        if name == "left" and side.dx > 0:
            warnings.append("좌 S300 dx 가 양수입니다 (좌측은 음수)")
        if name == "right" and side.dx < 0:
            warnings.append("우 S300 dx 가 음수입니다 (우측은 양수)")

    # 간단 모드: S600 외곽(bounding box) 좌/우에 확장폭만큼 블록 추가, 앞/뒤는 전체 길이 확장
    xs = [p[0] for p in main_fp]
    ys = [p[1] for p in main_fp]
    if cfg.total_width > 0:
        side = max(0.0, (cfg.total_width - (max(xs) - min(xs))) / 2)
        cfg.expand_left = cfg.expand_right = side
    if cfg.total_length > 0:
        fb = max(0.0, (cfg.total_length - (max(ys) - min(ys))) / 2)
        cfg.expand_front = cfg.expand_back = fb
    if cfg.total_width > 0 and cfg.total_width < max(xs) - min(xs):
        warnings.append(f"전체 가로 {cfg.total_width}m 가 S600 폭 {max(xs) - min(xs):.3f}m 보다 작습니다")
    y0, y1 = min(ys) - cfg.expand_back, max(ys) + cfg.expand_front
    blocks = {
        "left": (min(xs) - cfg.expand_left, min(xs)) if cfg.expand_left > 0 else None,
        "right": (max(xs), max(xs) + cfg.expand_right) if cfg.expand_right > 0 else None,
    }
    for name, xr in blocks.items():
        if xr is None or name in sides:
            continue
        rect = [(xr[0], y0), (xr[1], y0), (xr[1], y1), (xr[0], y1)]
        all_points += rect
        sides[name] = {"ip": "", "original": None, "transformed": _round_poly(rect)}
    if cfg.expand_front > 0 or cfg.expand_back > 0:
        all_points += [(min(xs), y0), (max(xs), y0), (max(xs), y1), (min(xs), y1)]

    if not sides and not (cfg.expand_front > 0 or cfg.expand_back > 0):
        raise HTTPException(400, "S300 을 지정하거나 확장 폭을 입력해야 합니다")

    combined = inflate(convex_hull(all_points), cfg.margin)
    return {
        "main": {"ip": cfg.main_ip, "footprint": main_fp, **_metrics([tuple(p) for p in main_fp])},
        "sides": sides,
        "combined": _round_poly(combined),
        **_metrics(combined),
        "warnings": warnings,
        "expand": {
            "left": round(cfg.expand_left, 3), "right": round(cfg.expand_right, 3),
            "front": round(cfg.expand_front, 3), "back": round(cfg.expand_back, 3),
        },
        "based_on_backup": bool(backup),
    }


def _patch_footprint(ip: str, footprint: list) -> bool:
    """footprint PATCH 후 effective 값으로 검증. 평면 키가 안 먹으면 중첩 형식 재시도.
    effective 반영이 비동기라 즉시 조회하면 이전 값이 나옴 → 최대 FOOTPRINT_VERIFY_SEC 동안 폴링."""
    def same():
        eff = _get_effective_footprint(ip) or []
        return len(eff) == len(footprint) and all(
            abs(a[0] - b[0]) < 1e-3 and abs(a[1] - b[1]) < 1e-3 for a, b in zip(eff, footprint)
        )

    def matches():
        deadline = time.time() + FOOTPRINT_VERIFY_SEC
        while True:
            if same():
                return True
            if time.time() >= deadline:
                return False
            time.sleep(0.5)

    robot_patch(ip, "/system/settings/user", {FOOTPRINT_KEY: footprint})
    if matches():
        return True
    logger.warning(f"[tow] {ip} 평면 키 footprint 미반영 → 중첩 형식 재시도")
    robot_patch(ip, "/system/settings/user", {"robot": {"footprint": footprint}})
    return matches()


# ── API ──

@router.get("/status/{robot_ip}")
def api_tow_status(robot_ip: str):
    try:
        effective = _get_effective_footprint(robot_ip)
        user = _extract_footprint(robot_get(robot_ip, "/system/settings/user"))
        params = robot_get(robot_ip, "/robot-params")
    except requests.RequestException as e:
        raise _robot_error(robot_ip, e)
    return {
        "ip": robot_ip,
        "effective_footprint": effective,
        "user_footprint": user,
        "metrics": _metrics([tuple(p) for p in effective]) if effective else None,
        "speed": {k: params.get(v) for k, v in SPEED_PARAM_KEYS.items()},
        "auto_hold": params.get(AUTO_HOLD_KEY),
        "backup": _load_backup(robot_ip),
    }


@router.post("/preview")
def api_tow_preview(cfg: TowConfig):
    return _build_preview(cfg)


@router.post("/apply")
def api_tow_apply(req: ApplyRequest):
    preview = _build_preview(req)
    result = {"preview": preview, "steps": []}

    # 1) S600 원본 백업 (이미 있으면 덮어쓰지 않음)
    if not _load_backup(req.main_ip):
        try:
            params = robot_get(req.main_ip, "/robot-params")
        except requests.RequestException as e:
            raise _robot_error(req.main_ip, e)
        _save_backup(req.main_ip, {
            "role": "main",
            "footprint": preview["main"]["footprint"],
            "speed": {v: params.get(v) for v in SPEED_PARAM_KEYS.values() if params.get(v) is not None},
        })
        result["steps"].append("S600 원본 백업 저장")
    else:
        result["steps"].append("S600 기존 백업 유지 (덮어쓰지 않음)")

    # 2) 합성 풋프린트 적용
    try:
        verified = _patch_footprint(req.main_ip, preview["combined"])
    except requests.RequestException as e:
        raise _robot_error(req.main_ip, e)
    result["footprint_verified"] = verified
    result["steps"].append("풋프린트 적용 " + ("확인됨" if verified else "— effective 값 불일치, 수동 확인 필요"))

    # 3) 속도/가속 제한
    if req.speed:
        body = {SPEED_PARAM_KEYS[k]: v for k, v in req.speed.model_dump().items() if v is not None}
        if body:
            try:
                robot_post(req.main_ip, "/robot-params", body)
                result["steps"].append(f"속도 제한 적용: {body}")
            except requests.RequestException as e:
                result["steps"].append(f"속도 제한 실패: {e}")

    # 4) S300 바퀴 풀기
    if req.free_side_wheels:
        for side in (req.left, req.right):
            if not side or not side.ip:
                continue
            try:
                if not _load_backup(side.ip):
                    params = robot_get(side.ip, "/robot-params")
                    _save_backup(side.ip, {"role": "side", "auto_hold": params.get(AUTO_HOLD_KEY)})
                robot_post(side.ip, "/robot-params", {AUTO_HOLD_KEY: False})
                result["steps"].append(f"S300 {side.ip} auto_hold=false")
            except requests.RequestException as e:
                result["steps"].append(f"S300 {side.ip} 바퀴 풀기 실패: {e}")

    logger.info(f"[tow] apply {req.main_ip}: {result['steps']}")
    return result


@router.post("/restore/{robot_ip}")
def api_tow_restore(robot_ip: str):
    backup = _load_backup(robot_ip)
    if not backup:
        raise HTTPException(404, f"백업이 없습니다 ({robot_ip})")
    steps = []
    try:
        if backup.get("footprint"):
            ok = _patch_footprint(robot_ip, backup["footprint"])
            steps.append("풋프린트 복원 " + ("확인됨" if ok else "— effective 값 불일치"))
        if backup.get("speed"):
            robot_post(robot_ip, "/robot-params", backup["speed"])
            steps.append(f"속도 복원: {backup['speed']}")
        if "auto_hold" in backup and backup["auto_hold"] is not None:
            robot_post(robot_ip, "/robot-params", {AUTO_HOLD_KEY: backup["auto_hold"]})
            steps.append(f"auto_hold={backup['auto_hold']} 복원")
    except requests.RequestException as e:
        raise _robot_error(robot_ip, e)
    _backup_path(robot_ip).unlink(missing_ok=True)
    steps.append("백업 파일 삭제")
    logger.info(f"[tow] restore {robot_ip}: {steps}")
    return {"steps": steps}


@router.post("/auto-hold/{robot_ip}")
def api_tow_auto_hold(robot_ip: str, enable: bool):
    try:
        return robot_post(robot_ip, "/robot-params", {AUTO_HOLD_KEY: enable})
    except requests.RequestException as e:
        raise _robot_error(robot_ip, e)


@router.post("/confirm-estop/{robot_ip}")
def api_tow_confirm_estop(robot_ip: str):
    try:
        return robot_post(robot_ip, "/services/confirm_estop")
    except requests.RequestException as e:
        raise _robot_error(robot_ip, e)


@router.post("/clear-slip/{robot_ip}")
def api_tow_clear_slip(robot_ip: str):
    try:
        return robot_post(robot_ip, "/services/monitor/clear_slipping_error")
    except requests.RequestException as e:
        raise _robot_error(robot_ip, e)


# ── 반복 주행 ──
# 선택한 POI 를 순서대로 standard 이동, laps 회 반복. 구간별 결과/소요시간 기록.
# 재시도 없이 1회 이동 결과를 그대로 기록 (테스트 통계 목적)

LOOP_MAX_LEGS = 300
_loops: dict[str, dict] = {}


class LoopRequest(BaseModel):
    robot_ip: str
    poi_ids: list[int]
    laps: int = 10              # 0 = 중지할 때까지 무한
    wait_sec: float = 5.0       # 각 포인트 도착 후 대기
    stop_on_fail: bool = True
    move_timeout: int = 180
    require_jack_up: bool = False  # 랙 고정 테스트: 잭 업 상태가 아니면 이동 안 함 (랙 다리 끌림 방지)


def _loop_summary(st: dict) -> dict:
    ok = [l for l in st["legs"] if l["result"] == "succeeded"]
    durations = [l["duration"] for l in ok]
    return {
        **{k: v for k, v in st.items() if k != "legs"},
        "success": st["success"],
        "fail": st["fail"],
        "avg_duration": round(sum(durations) / len(durations), 1) if durations else None,
        "max_duration": max(durations) if durations else None,
        "legs": st["legs"][-50:][::-1],
    }


def _run_loop(req: LoopRequest, points: list[dict]):
    ip = req.robot_ip
    st = _loops[ip]
    try:
        lap = 0
        while req.laps == 0 or lap < req.laps:
            lap += 1
            st["lap"] = lap
            for pt in points:
                st["target"] = pt["name"]
                if req.require_jack_up:
                    pos, _raw = get_jack_position(ip)
                    if pos != "up":
                        st["state"] = "failed"
                        st["message"] = f"{lap}회차 {pt['name']} 출발 전 잭 상태 '{pos}' — 이동 중지"
                        return
                update_job_status(ip, status="moving", message=f"[반복 {lap}/{req.laps or '∞'}] {pt['name']} 이동 중")
                t0 = time.time()
                try:
                    move_id = create_move(ip, "standard", pt["x"], pt["y"], pt["ori"])
                    result = wait_move(ip, move_id, timeout=req.move_timeout)
                except RuntimeError:
                    raise
                except Exception as e:
                    result = {"state": "failed", "fail_message": f"통신 오류: {e}"}
                state = str(result.get("state", "")).lower()
                leg = {
                    "lap": lap,
                    "poi": pt["name"],
                    "result": state,
                    "duration": round(time.time() - t0, 1),
                    "message": "" if state == "succeeded" else (result.get("fail_message") or ""),
                    "at": datetime.now().strftime("%H:%M:%S"),
                }
                st["legs"].append(leg)
                del st["legs"][:-LOOP_MAX_LEGS]
                if state == "succeeded":
                    st["success"] += 1
                else:
                    st["fail"] += 1
                    logger.warning(f"[tow-loop] {ip} {pt['name']} 실패: {leg['message']}")
                    if req.stop_on_fail:
                        st["state"] = "failed"
                        st["message"] = f"{lap}회차 {pt['name']} 실패: {leg['message'] or state}"
                        return
                update_job_status(ip, status="waiting", message=f"[반복 {lap}/{req.laps or '∞'}] {pt['name']} 도착 — 대기")
                _interruptible_sleep(ip, req.wait_sec)
        st["state"] = "done"
        st["message"] = f"{lap}회 완료"
    except RuntimeError:
        st["state"] = "stopped"
        st["message"] = "사용자 중지"
    except Exception as e:
        logger.exception(f"[tow-loop] {ip} 오류")
        st["state"] = "failed"
        st["message"] = str(e)
    finally:
        st["finished_at"] = datetime.now().strftime("%H:%M:%S")
        st["target"] = None
        clear_job_status(ip)
        logger.info(f"[tow-loop] {ip} 종료: {st['state']} ({st['message']}) 성공 {st['success']} / 실패 {st['fail']}")


@router.post("/loop/start")
def api_tow_loop_start(req: LoopRequest):
    ip = req.robot_ip
    if len(req.poi_ids) < 2:
        raise HTTPException(400, "POI 를 2개 이상 선택해야 합니다")
    if (_loops.get(ip) or {}).get("state") == "running":
        raise HTTPException(409, "이미 반복 주행 중입니다")
    job = get_job_status(ip)
    if job and job.get("status") not in (None, "idle", "done"):
        raise HTTPException(409, "로봇이 다른 작업 중입니다")
    if req.require_jack_up:
        pos, raw = get_jack_position(ip)
        if pos != "up":
            raise HTTPException(409, f"잭 업 상태가 아닙니다 ({pos}) — 잭 업 후 시작하세요. raw={raw}")

    db = SessionLocal()
    try:
        rows = {p.id: p for p in db.query(MapPOI).filter(MapPOI.id.in_(req.poi_ids)).all()}
    finally:
        db.close()
    points = []
    for pid in req.poi_ids:
        p = rows.get(pid)
        if p is None or p.world_x is None or p.world_y is None:
            raise HTTPException(400, f"POI {pid} 의 좌표가 없습니다")
        points.append({"name": p.name, "x": p.world_x, "y": p.world_y, "ori": p.angle or 0})

    _loops[ip] = {
        "state": "running", "message": "", "lap": 0, "laps": req.laps,
        "route": [p["name"] for p in points], "target": None,
        "success": 0, "fail": 0, "legs": [],
        "started_at": datetime.now().strftime("%H:%M:%S"), "finished_at": None,
    }
    _stop_flags.pop(ip, None)  # 이전 중지 요청 잔여 플래그 제거
    threading.Thread(target=_run_loop, args=(req, points), daemon=True).start()
    logger.info(f"[tow-loop] {ip} 시작: {[p['name'] for p in points]} x {req.laps or '∞'}")
    return {"message": "반복 주행 시작"}


@router.post("/loop/stop/{robot_ip}")
def api_tow_loop_stop(robot_ip: str):
    stop_robot_job(robot_ip)
    try:
        cancel_current_move(robot_ip)
    except requests.RequestException:
        pass
    return {"message": "중지 요청"}


@router.get("/loop/status/{robot_ip}")
def api_tow_loop_status(robot_ip: str):
    st = _loops.get(robot_ip)
    if not st:
        return {"state": "idle"}
    return _loop_summary(st)


# ── 잭 (랙 고정 주행 테스트) ──

def read_jack_state(ip: str, timeout: float = 3.0) -> dict | None:
    """/jack_state WS 토픽 1회 조회. 실패 시 None."""
    from websocket import create_connection  # type: ignore
    ws = None
    try:
        ws = create_connection(f"ws://{ip}:8090/ws/v2/topics", timeout=timeout)
        ws.send(json.dumps({"enable_topic": "/jack_state"}))
        ws.settimeout(timeout)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = ws.recv()
            except Exception:
                break
            try:
                pkt = json.loads(raw) if raw else {}
            except ValueError:
                continue
            if pkt.get("topic") == "/jack_state":
                return pkt
    except Exception as e:
        logger.warning(f"[tow-jack] {ip} jack_state 조회 실패: {e}")
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
    return None


def get_jack_position(ip: str) -> tuple[str, dict | None]:
    """(up / down / moving / unknown, raw 패킷)
    state: jacking_up / jacking_down / hold, progress: 0(내림) ~ 1(올림) 로 판단."""
    pkt = read_jack_state(ip)
    if not pkt:
        return "unknown", None
    state = str(pkt.get("state", "")).lower()
    if state in ("jacking_up", "jacking_down"):
        return "moving", pkt
    progress = pkt.get("progress")
    if isinstance(progress, (int, float)):
        if progress >= 0.95:
            return "up", pkt
        if progress <= 0.05:
            return "down", pkt
    return "unknown", pkt


@router.get("/jack/{robot_ip}")
def api_tow_jack_state(robot_ip: str):
    pos, raw = get_jack_position(robot_ip)
    return {"position": pos, "raw": raw}


@router.post("/jack/{robot_ip}/{action}")
def api_tow_jack(robot_ip: str, action: str):
    if action not in ("up", "down"):
        raise HTTPException(400, "action 은 up / down")
    if action == "down" and (_loops.get(robot_ip) or {}).get("state") == "running":
        raise HTTPException(409, "반복 주행 중에는 잭 다운 불가 — 먼저 중지하세요")
    try:
        return robot_post(robot_ip, f"/services/jack_{action}")
    except requests.RequestException as e:
        raise _robot_error(robot_ip, e)
