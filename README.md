# UND RCS Basic — 사내 표준 AMR 제어 시스템

## 프로젝트 개요

**UND RCS Basic**은 AutoXing 로봇 플랫폼 기반의 **유앤디(UND) 사내 표준 AMR 기본 제어 시스템**입니다.
현장 맞춤형 RCS의 베이스 모델로, 3D 모니터링, 다중 로봇 조율, 잭킹 작업 안전망,
실시간 WebSocket 통신을 지원하며 REST API 기반의 로컬 제어를 적용합니다.

**개발 기간**: 2025.07 ~ 진행 중  
**기여도**: 100%

---

## 주요 기능

- **3D 모니터링 & 맵 관리** — Three.js 기반 실시간 로봇 위치/상태 시각화
- **로봇 IP 자동 등록** — IP 입력 시 로봇 정보 자동 조회 후 DB 저장
- **다중 로봇 안전장치** — POI 락, 데드락 자동 감지/양보, Zone 락(좁은 통로)
- **잭킹 작업 완전 자동화** — `align_with_rack → jack_up → to_unload_point → jack_down`
- **작업 일시정지/재개/강제 종료** — 운영 중 작업 제어, 강제 종료 시 잭 보관 → 충전소 복귀
- **수동 배차 & 스케줄링** — 실시간 로봇 할당, APScheduler 기반 일회/일일/주간 반복
- **로그 & 백업 관리** — 활동 로그, DB 백업/복원, 성능 모니터링

---

## 시스템 구조

```
┌──────────────────────────────────────────┐
│  웹 브라우저 / 태블릿 앱                  │
└──────────────────┬───────────────────────┘
                   │ REST + WebSocket
        ┌──────────▼──────────┐
        │ Frontend (Next.js)  │ : 3002
        │  - 3D 모니터링       │
        │  - 맵 / POI 관리     │
        │  - 로봇 / 작업 관리  │
        └──────────┬──────────┘
                   │
   ┌───────────────┼───────────────┐
   │               │               │
┌──▼──────┐  ┌─────▼─────┐  ┌─────▼─────┐
│ Backend │  │   MySQL   │  │  AutoXing │
│(FastAPI)│──│rcs_basic_db│──│ Robot API │
│  :8002  │  └───────────┘  └───────────┘
└─────────┘
   ├─ routers/   (auth, user, robot, map, task)
   ├─ services/  (jack, scheduler, deadlock_monitor)
   ├─ models/    (Robot, Map, Task, POI, Schedule)
   └─ crud/      (DB 레이어)

TabletApp: TabletApp (Full) + TabletAppSimple (수동배차 특화)
```

---

## 기술 스택

| 구분 | 기술 |
|---|---|
| **Backend** | FastAPI, SQLAlchemy, APScheduler, Python 3.10 |
| **Frontend** | Next.js 16, React 19, TypeScript, Tailwind 4, Three.js, Recharts |
| **Database** | MySQL 5.7+ |
| **Deployment** | Docker Compose |
| **Mobile** | Android (Kotlin/Gradle) — Full + Simple 2종 |
| **Robot** | AutoXing REST API (crane_s300_op5, fw 2.12.21) |
| **Comm** | REST + WebSocket |

---

## 핵심 API

### 로봇 제어
- `POST /api/robots` — IP 입력 자동 등록
- `GET  /api/robots/quick-status/{ip}` — 실시간 상태
- `GET  /api/robots/job-status/{ip}` — 작업 진행 (work_mode 포함)
- `POST /api/robots/remote/pause|resume|force-return/{ip}` — 작업 제어
- `POST /api/robots/remote/stop-all/{ip}` — 즉시 정지

### 맵 & POI
- `GET/POST /api/map/` — 맵 관리
- `GET/POST /api/map/pois` — POI 관리 (general, jack, standby, charger)
- `POST /api/map/default-area/{area_id}` — 모니터링 기본 영역 설정

### 작업 & 스케줄
- `POST /api/tasks/routes` — 경로 생성
- `POST /api/tasks/schedules` — 스케줄 등록
- `POST /api/tasks/manual-run` — 수동 배차

### 잭 테스트
- `POST /api/jack-test/{ip}` — 풀 잭킹 사이클

---

## 빌드 & 실행

### 로컬

```bash
# Backend
cd BackEnd
export DB_HOST=192.168.0.21 DB_USER=root DB_PASSWORD=1234
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Frontend
cd frontend && npm install && npm run dev
```

### Docker

```bash
cat > .env << 'EOF'
SERVER_IP=192.168.0.44
DB_HOST=192.168.0.21
DB_USER=root
DB_PASSWORD=1234
DB_NAME=rcs_basic_db
NEXT_PUBLIC_API_URL=http://192.168.0.44:8002
EOF

docker-compose up -d --build
```

---

## 중요 설정

### Shelves Point POI 사양 (잭킹 필수)
```json
{
  "type": "34",
  "subtype": "rack",
  "shelvesState": "0",
  "hasFixedLegs": false,
  "dockViaDirection": "front",
  "mapOverlay": true
}
```

### Rack 사이즈
- **S300** — width 0.73, depth 0.74
- **S600** — width 0.83, depth 0.87

### WebSocket 토픽
- `/detected_rack` — 랙 감지 상태
- `/jack_state` — 잭 상태 (jacking_up/down, hold)
- `/robot_model` — 로봇 풋프린트 (잭 업 시 확장)

---

## 개발 노트

- **CSS 규칙**: `@import` 금지 (Turbopack 호환) → `globals.css` 인라인
- **POI 좌표**: DB는 `world_x`, `world_y` (pixel 좌표와 별도)
- **맵 변경 시**: `POST /api/tasks/routes/remap-to-map/{id}` 호출

### 주요 기여
- FastAPI 백엔드 아키텍처 & AutoXing Robot API 통합
- 3D 모니터링 (Three.js) + 맵 관리 UI
- 데드락 감지/양보 (`deadlock_monitor.py`)
- POI/Zone 락 충돌 방지 (`poi_lock.py`, `zone_guard.py`)
- 잭킹 완전 자동화 & 안전 재시도 (`jack_service.py`)
- APScheduler 스케줄링
- Docker Compose 배포 자동화
- 태블릿 앱 2종 (Full, Simple)

---

*Last Updated: 2026-06-26*
