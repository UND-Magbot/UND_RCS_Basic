# LiDAR 감지 → 랙(선반) 인식 · 이송 Flow

RCS-Basic + AutoXing crawler(S300/S600) 기준.
근거: [AxBot REST Book](https://autoxingtech.github.io/axbot_rest_book/) + `BackEnd/app/services/jack_service.py`

---

## 0. 전제 조건 (이게 안 맞으면 랙을 절대 못 찾음)

| 항목 | 설정 위치 | 값 |
|---|---|---|
| 랙 물리 스펙 | `PATCH /system/settings/user` → `rack.specs` | S300: 0.765×0.765 / S600: 0.83×0.87 (`app/constants/rack_specs.py`) |
| Shelves Point | 맵 overlay feature | `type="34"`, `subtype="rack"`, `hasFixedLegs=false`, `dockViaDirection="front"` |
| overlay 반영 | overlay PATCH 후 | `POST /chassis/current-map` 재선택 (overlays_version 갱신) |

> `rack.specs`는 **사용하는 사이즈 종류만** 등록. 여러 spec이 등록되면 펌웨어가 LiDAR 측정값을 엉뚱한 spec에 매칭해 실패하는 사례 있음.

---

## 1. 전체 흐름 (rack_pickup 모드)

```mermaid
flowchart TD
    A([작업 시작<br/>run_route_job]) --> B[위치 보정<br/>POST /services/start_global_positioning<br/>use_barcode + use_base_map_match]
    B --> C{{"WS /global_positioning_state<br/>succeeded ?"}}
    C -- 실패 --> C1[current-map 재선택 후 1회 재시도] --> D
    C -- 성공 --> D

    D["랙 위치 POI(standby)로<br/>align_with_rack"] --> E{{state}}
    E -- failed --> R[[재시도 루틴 → 3장]]
    R -- 최종 실패 --> X([작업 실패<br/>cancel_move + jack_down])
    R -- 성공 --> F
    E -- succeeded --> F

    F[POST /services/jack_up<br/>+ 10초 대기] --> G[작업 위치로 이송<br/>type=to_unload_point]
    G --> H{{state}}
    H -- failed/timeout --> G2[safe_move 자동 재시도<br/>5초 간격 · 최대 200회] --> G
    H -- succeeded --> I[POST /services/jack_down<br/>+ 10초 대기]

    I --> J{수동 확인 모드?}
    J -- 예 --> J1[출발 버튼 대기<br/>최대 30분] --> K
    J -- 아니오 --> J2[wait_sec 대기] --> K

    K[다시 align_with_rack<br/>→ jack_up] --> L{다음 목적지 있음?}
    L -- 예 --> G
    L -- 아니오 --> M["랙 위치(standby)로 복귀<br/>to_unload_point → jack_down"]
    M --> N["충전소 복귀<br/>standard 접근 → type=charge<br/>charge_retry_count=3"]
    N --> Z(["완료"])
```

---

## 2. align_with_rack 내부 — LiDAR가 랙을 인식하는 구간

```mermaid
flowchart TD
    S([POST /chassis/moves<br/>type=align_with_rack<br/>target_x/y/ori = Shelves Point]) --> A1[Shelves Point 근처까지<br/>일반 경로 주행]
    A1 --> A2[LiDAR 스캔 시작<br/>점군에서 다리 후보 추출]
    A2 --> A3{{"rack.specs 와 매칭<br/>leg_size · foot_radius ·<br/>width × depth"}}
    A3 -- 매칭 실패 --> F1[fail_message<br/>failed to find rack /<br/>AlignFailedInRackArea]
    A3 -- 매칭 성공 --> A4[랙 4다리 중심점 계산<br/>→ 랙 중심 · 방향 산출]
    A4 --> A5[중심선 정렬<br/>alignment=center]
    A5 --> A6[랙 하부로 저속 진입<br/>margin 0.05~0.1m 유지]
    A6 --> A7{{잭 전면 여유<br/>cargo_to_jack_front_edge_<br/>min_distance ≥ 0.05?}}
    A7 -- 부족 --> F1
    A7 -- 충분 --> A8([state=succeeded<br/>WS /detected_rack 갱신])
    F1 --> F2([state=failed])
```

**모니터링 토픽**
- `/detected_rack` — 랙 감지 여부/좌표
- `/jack_state` — `jacking_up` / `jacking_down` / `hold`
- `/robot_model` — 잭 업 시 풋프린트가 랙 크기로 확장됨

---

## 3. 랙 인식 실패 시 재시도 (`align_with_retry`, 최대 4회)

```mermaid
flowchart LR
    T1[1차: 그대로 시도] -- 실패 --> T2[2차: 재로컬화<br/>start_global_positioning]
    T2 -- 실패 --> T3[3차: 0.5m 후진<br/>→ 재로컬화<br/>LiDAR 시야 확보]
    T3 -- 실패 --> T4[4차: 측면 0.3m 우회<br/>→ 재로컬화<br/>접근 각도 변경]
    T4 -- 실패 --> TX([최종 실패 반환])
    T1 & T2 & T3 & T4 -- 성공 --> TS([succeeded])
```

| 실패 코드 | 원인 | 대응 |
|---|---|---|
| `failed to find rack` | POI type이 34가 아님 / 랙 부재 / spec 불일치 | overlay·rack.specs 점검 |
| `AlignFailedInRackArea` | 진입 각도·시야 문제 | 후진·측면 우회 재시도 |
| `MoveTimeout` | 경로 막힘 | safe_move 재시도 |
| `ChargeRetryCountExceeded` | 도킹 실패 | 사전 접근 POI(`<충전소명>-1`) 경유 |

---

## 4. work_mode 별 분기

| 모드 | 랙 인식(align_with_rack) | 이송 move type | 잭 조작 |
|---|---|---|---|
| `rack_pickup` | 사용 (LiDAR 랙 정렬) | `to_unload_point` | pickup에서 up, dropoff에서 down |
| `delivery_no_rack` | 미사용 | `standard` | pickup up / dropoff down |
| `simple_move` | 미사용 | `standard` | 없음 |
