"use client";

import { useState, useEffect } from "react";

const API = process.env.NEXT_PUBLIC_API_URL || "";

type LiveRobot = { ID: number; IP: string; SN: string; ROBOTNAME: string; ONLINE: string; [key: string]: any };
type Pt = [number, number];
type Side = { ip: string; dx: number; dy: number; yaw_deg: number };

type Preview = {
  main: { ip: string; footprint: Pt[]; width: number; length: number; turn_radius: number };
  sides: Record<string, { ip: string; transformed: Pt[] }>;
  combined: Pt[];
  width: number;
  length: number;
  turn_radius: number;
  warnings: string[];
  expand?: { left: number; right: number; front: number; back: number };
  based_on_backup: boolean;
};

type Props = { liveRobots: LiveRobot[]; areaId?: number; variant?: "tow" | "rack" };
type PoiOption = { id: number; name: string; type: string };
type LoopLeg = { lap: number; poi: string; result: string; duration: number; message: string; at: string };
type LoopStatus = {
  state: "idle" | "running" | "done" | "stopped" | "failed";
  message?: string;
  lap?: number;
  laps?: number;
  route?: string[];
  target?: string | null;
  success?: number;
  fail?: number;
  avg_duration?: number | null;
  max_duration?: number | null;
  started_at?: string;
  finished_at?: string | null;
  legs?: LoopLeg[];
};

const LOOP_STATE_LABELS: Record<string, string> = {
  idle: "대기", running: "주행 중", done: "완료", stopped: "중지됨", failed: "실패",
};

// 풋프린트 좌표: +X 오른쪽, +Y 앞쪽 → SVG 는 y 반전
function FootprintSvg({ preview }: { preview: Preview }) {
  const all = [...preview.combined, ...preview.main.footprint];
  const r = Math.max(...all.map(([x, y]) => Math.max(Math.abs(x), Math.abs(y))), preview.turn_radius) + 0.1;
  const toPts = (poly: Pt[]) => poly.map(([x, y]) => `${x},${-y}`).join(" ");
  return (
    <svg viewBox={`${-r} ${-r} ${r * 2} ${r * 2}`} style={{ width: "100%", height: 220, background: "var(--bg-surface-1)", borderRadius: 6 }}>
      <circle cx={0} cy={0} r={preview.turn_radius} fill="none" stroke="var(--text-muted)" strokeWidth={0.005} strokeDasharray="0.03 0.03" />
      <polygon points={toPts(preview.combined)} fill="rgba(239,68,68,0.12)" stroke="#ef4444" strokeWidth={0.01} />
      {Object.entries(preview.sides).map(([name, s]) => (
        <polygon key={name} points={toPts(s.transformed)} fill="rgba(59,130,246,0.25)" stroke="#3b82f6" strokeWidth={0.008} />
      ))}
      <polygon points={toPts(preview.main.footprint)} fill="rgba(54,223,200,0.3)" stroke="#36dfc8" strokeWidth={0.008} />
      {/* 전방 표시 */}
      <line x1={0} y1={0} x2={0} y2={-0.25} stroke="#36dfc8" strokeWidth={0.012} />
      <polygon points="-0.04,-0.21 0.04,-0.21 0,-0.27" fill="#36dfc8" />
      <circle cx={0} cy={0} r={0.015} fill="var(--text-primary)" />
    </svg>
  );
}

const JACK_LABELS: Record<string, string> = {
  up: "올림", down: "내림", moving: "동작 중", unknown: "확인 불가",
};

// variant: "tow" = S600 + S300 견인 / "rack" = 랙 기구 고정 + 잭 업 상태 주행
export function TowTestPanel({ liveRobots, areaId, variant = "tow" }: Props) {
  const isRack = variant === "rack";
  const onlineRobots = liveRobots.filter((r) => r.ONLINE === "Online");

  const [mainIp, setMainIp] = useState("");
  const [left, setLeft] = useState<Side>({ ip: "", dx: -0.6, dy: 0, yaw_deg: 0 });
  const [right, setRight] = useState<Side>({ ip: "", dx: 0.6, dy: 0, yaw_deg: 0 });
  const [margin, setMargin] = useState(0.03);
  // simple: S300 조회 없이 S600 외곽을 좌/우/앞/뒤로 확장 / detail: S300 로봇 풋프린트 + 실측 오프셋
  const [mode, setMode] = useState<"simple" | "detail">("simple");
  const [totalCm, setTotalCm] = useState(isRack ? { width: 77, length: 77 } : { width: 160, length: 100 });
  const [speed, setSpeed] = useState({ max_forward_velocity: 0.3, max_forward_acc: 0.15, max_angular_velocity: 0.3 });
  const [applySpeed, setApplySpeed] = useState(true);

  const [preview, setPreview] = useState<Preview | null>(null);
  const [status, setStatus] = useState<any>(null);
  const [log, setLog] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  // 반복 주행
  const [pois, setPois] = useState<PoiOption[]>([]);
  const [loopPoiIds, setLoopPoiIds] = useState<number[]>([]);
  const [addPoiId, setAddPoiId] = useState(0);
  const [laps, setLaps] = useState(10);
  const [waitSec, setWaitSec] = useState(5);
  const [stopOnFail, setStopOnFail] = useState(true);
  const [loop, setLoop] = useState<LoopStatus>({ state: "idle" });
  const [requireJack, setRequireJack] = useState(true);
  const [jack, setJack] = useState<{ position: string; raw: any } | null>(null);

  // 랙 모드: 잭 상태 폴링 (/jack_state WS 1회 조회라 요청이 겹치지 않게)
  useEffect(() => {
    if (!isRack || !mainIp) {
      setJack(null);
      return;
    }
    let alive = true;
    let inflight = false;
    const poll = async () => {
      if (inflight) return;
      inflight = true;
      try {
        const res = await fetch(`${API}/api/tow-test/jack/${mainIp}`);
        if (res.ok && alive) setJack(await res.json());
      } catch {} finally {
        inflight = false;
      }
    };
    poll();
    const t = setInterval(poll, 3000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [isRack, mainIp]);

  useEffect(() => {
    const url = areaId ? `${API}/api/map/active-pois?area_id=${areaId}` : `${API}/api/map/active-pois`;
    fetch(url)
      .then((r) => r.json())
      .then((data) => setPois(Array.isArray(data) ? data.map((p: any) => ({ id: p.id, name: p.name, type: p.poi_type || p.type })) : []))
      .catch(() => {});
  }, [areaId]);

  // 선택한 S600 의 반복 주행 상태 폴링
  useEffect(() => {
    if (!mainIp) {
      setLoop({ state: "idle" });
      return;
    }
    let alive = true;
    const poll = async () => {
      try {
        const res = await fetch(`${API}/api/tow-test/loop/status/${mainIp}`);
        if (res.ok && alive) setLoop(await res.json());
      } catch {}
    };
    poll();
    const t = setInterval(poll, 2000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [mainIp]);

  const addLog = (...lines: string[]) =>
    setLog((prev) => [...lines.map((l) => `[${new Date().toLocaleTimeString()}] ${l}`), ...prev].slice(0, 50));

  const call = async (label: string, url: string, init?: RequestInit) => {
    setBusy(true);
    try {
      const res = await fetch(`${API}${url}`, init);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      return data;
    } catch (e: unknown) {
      addLog(`${label} 실패: ${e instanceof Error ? e.message : e}`);
      return null;
    } finally {
      setBusy(false);
    }
  };

  const configBody = () =>
    mode === "simple"
      ? {
          main_ip: mainIp,
          margin,
          total_width: totalCm.width / 100,
          total_length: totalCm.length / 100,
        }
      : { main_ip: mainIp, left: left.ip ? left : null, right: right.ip ? right : null, margin };

  const handleStatus = async () => {
    const data = await call("상태 조회", `/api/tow-test/status/${mainIp}`);
    if (!data) return;
    setStatus(data);
    addLog(`상태: 폭 ${data.metrics?.width}m, 길이 ${data.metrics?.length}m, 백업 ${data.backup ? "있음" : "없음"}`);
  };

  const handlePreview = async () => {
    const data = await call("미리보기", "/api/tow-test/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(configBody()),
    });
    if (!data) return;
    setPreview(data);
    addLog(`미리보기: 폭 ${data.width}m, 길이 ${data.length}m, 회전반경 ${data.turn_radius}m`);
  };

  const handleApply = async () => {
    const target = isRack ? "랙 외곽" : "합성 외곽선";
    if (!confirm(`로봇 풋프린트를 ${target}으로 변경합니다.\n원본은 백업되며 '복원'으로 되돌릴 수 있습니다. 진행할까요?`)) return;
    const data = await call("적용", "/api/tow-test/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...configBody(), speed: applySpeed ? speed : null, free_side_wheels: mode === "detail" }),
    });
    if (!data) return;
    setPreview(data.preview);
    addLog(...data.steps.map((s: string) => `적용: ${s}`));
  };

  const handleRestore = async (ip: string) => {
    const data = await call(`복원(${ip})`, `/api/tow-test/restore/${ip}`, { method: "POST" });
    if (data) addLog(...data.steps.map((s: string) => `복원(${ip}): ${s}`));
  };

  const handleRestoreAll = async () => {
    if (!confirm(isRack ? "로봇 풋프린트/속도를 원본으로 복원합니다." : "S600 풋프린트/속도와 S300 auto_hold 를 원본으로 복원합니다.")) return;
    const ips = mode === "detail" ? [mainIp, left.ip, right.ip] : [mainIp];
    for (const ip of ips.filter(Boolean)) await handleRestore(ip);
  };

  const handleSideAction = async (label: string, path: string) => {
    for (const ip of [left.ip, right.ip].filter(Boolean)) {
      const data = await call(`${label}(${ip})`, path.replace("{ip}", ip), { method: "POST" });
      if (data) addLog(`${label}(${ip}) 완료`);
    }
  };

  const robotSelect = (value: string, onChange: (v: string) => void, exclude: string[]) => (
    <select className="jack-test-panel__select" value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">선택 안 함</option>
      {onlineRobots.filter((r) => r.IP === value || !exclude.includes(r.IP)).map((r) => (
        <option key={r.IP} value={r.IP}>{r.ROBOTNAME || r.SN} ({r.IP})</option>
      ))}
    </select>
  );

  const numInput = (value: number, onChange: (v: number) => void, step = 0.01) => (
    <input
      type="number"
      step={step}
      className="jack-test-panel__select"
      value={value}
      onChange={(e) => onChange(Number(e.target.value))}
    />
  );

  const sideFields = (label: string, side: Side, setSide: (s: Side) => void) => (
    <div className="jack-test-panel__route-info" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <label className="jack-test-panel__label">
        {label} S300
        {robotSelect(side.ip, (ip) => setSide({ ...side, ip }), [mainIp, left.ip, right.ip])}
      </label>
      <div className="tow-test-panel__row3">
        <label className="jack-test-panel__label">dx (m){numInput(side.dx, (dx) => setSide({ ...side, dx }))}</label>
        <label className="jack-test-panel__label">dy (m){numInput(side.dy, (dy) => setSide({ ...side, dy }))}</label>
        <label className="jack-test-panel__label">yaw (°){numInput(side.yaw_deg, (yaw_deg) => setSide({ ...side, yaw_deg }), 0.5)}</label>
      </div>
    </div>
  );

  const poiName = (id: number) => pois.find((p) => p.id === id)?.name || `#${id}`;
  const loopRunning = loop.state === "running";

  const handleLoopStart = async () => {
    const data = await call("반복 주행 시작", "/api/tow-test/loop/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        robot_ip: mainIp, poi_ids: loopPoiIds, laps, wait_sec: waitSec, stop_on_fail: stopOnFail,
        require_jack_up: isRack && requireJack,
      }),
    });
    if (data) addLog(`반복 주행 시작: ${loopPoiIds.map(poiName).join(" → ")} × ${laps || "∞"}`);
  };

  const handleJack = async (action: "up" | "down") => {
    if (action === "down" && !confirm("잭을 내리면 랙 다리가 바닥에 닿아 이동 시 끌립니다.\n정지 상태에서만 내리세요. 진행할까요?")) return;
    const data = await call(`잭 ${action === "up" ? "업" : "다운"}`, `/api/tow-test/jack/${mainIp}/${action}`, { method: "POST" });
    if (data) addLog(`잭 ${action === "up" ? "업" : "다운"} 명령 전송 — 약 10초 후 상태 확인`);
  };

  const handleLoopStop = async () => {
    const data = await call("반복 주행 중지", `/api/tow-test/loop/stop/${mainIp}`, { method: "POST" });
    if (data) addLog("반복 주행 중지 요청");
  };

  const hasSideRobot = mode === "detail" && !!(left.ip || right.ip);
  const hasSide = mode === "simple" ? totalCm.width > 0 || totalCm.length > 0 : hasSideRobot;

  return (
    <div className="jack-test-panel">
      <h3 className="jack-test-panel__title">{isRack ? "랙 고정 주행 테스트" : "S600 + S300 견인 테스트"}</h3>

      <div className="jack-test-panel__form">
        <label className="jack-test-panel__label">
          {isRack ? "주행 로봇" : "S600 (주행 로봇)"}
          {robotSelect(mainIp, setMainIp, [left.ip, right.ip])}
        </label>
        {!isRack && (
          <div className="tow-test-panel__row3" style={{ gridTemplateColumns: "1fr 1fr" }}>
            <button className={`btn ${mode === "simple" ? "btn--primary" : "btn--outline"} tow-test-panel__btn-sm`} onClick={() => setMode("simple")}>간단 (폭 확장)</button>
            <button className={`btn ${mode === "detail" ? "btn--primary" : "btn--outline"} tow-test-panel__btn-sm`} onClick={() => setMode("detail")}>상세 (S300 연결)</button>
          </div>
        )}
        {mode === "simple" ? (
          <div className="jack-test-panel__route-info" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <span>
              {isRack
                ? "로봇 + 랙 전체 외곽 실측 크기 (cm) — 다리/캐스터 포함 가장 바깥 기준"
                : "S600 + S300 묶은 전체 실측 크기 (cm) — 브라켓 등 가장 바깥 기준"}
            </span>
            <div className="tow-test-panel__row3" style={{ gridTemplateColumns: "1fr 1fr" }}>
              <label className="jack-test-panel__label">전체 가로 (좌우){numInput(totalCm.width, (v) => setTotalCm({ ...totalCm, width: v }), 1)}</label>
              <label className="jack-test-panel__label">전체 세로 (앞뒤){numInput(totalCm.length, (v) => setTotalCm({ ...totalCm, length: v }), 1)}</label>
            </div>
          </div>
        ) : (
          <>
            {sideFields("좌측", left, setLeft)}
            {sideFields("우측", right, setRight)}
          </>
        )}
        <label className="jack-test-panel__label">
          여유 margin (m)
          {numInput(margin, setMargin)}
        </label>

        <div className="jack-test-panel__route-info" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <label style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <input type="checkbox" checked={applySpeed} onChange={(e) => setApplySpeed(e.target.checked)} />
            적용 시 속도/가속 제한
          </label>
          <div className="tow-test-panel__row3">
            <label className="jack-test-panel__label">속도{numInput(speed.max_forward_velocity, (v) => setSpeed({ ...speed, max_forward_velocity: v }))}</label>
            <label className="jack-test-panel__label">가속{numInput(speed.max_forward_acc, (v) => setSpeed({ ...speed, max_forward_acc: v }))}</label>
            <label className="jack-test-panel__label">각속도{numInput(speed.max_angular_velocity, (v) => setSpeed({ ...speed, max_angular_velocity: v }))}</label>
          </div>
        </div>
      </div>

      <div className="jack-test-panel__buttons">
        <button className="btn btn--outline jack-test-panel__btn" disabled={busy || !mainIp} onClick={handleStatus}>상태 조회</button>
        <button className="btn btn--outline jack-test-panel__btn" disabled={busy || !mainIp || !hasSide} onClick={handlePreview}>미리보기</button>
      </div>
      <div className="jack-test-panel__buttons" style={{ marginTop: 0 }}>
        <button className="btn btn--primary jack-test-panel__btn" disabled={busy || !mainIp || !hasSide} onClick={handleApply}>적용</button>
        <button className="btn btn--danger jack-test-panel__btn" disabled={busy || !mainIp} onClick={handleRestoreAll}>복원</button>
      </div>
      {isRack ? (
        <>
          <div className="jack-test-panel__status" style={{ borderLeftColor: jack?.position === "up" ? "#36dfc8" : jack?.position === "down" ? "#ef4444" : "#f59e0b" }}>
            <span className="jack-test-panel__status-label">
              잭 상태: {mainIp ? JACK_LABELS[jack?.position || "unknown"] : "-"}
              {jack?.position === "down" && " — 이동 시 랙 끌림!"}
            </span>
            {jack?.raw && <span className="jack-test-panel__status-msg">{JSON.stringify(jack.raw)}</span>}
          </div>
          <div className="jack-test-panel__buttons" style={{ marginTop: 0 }}>
            <button className="btn btn--primary jack-test-panel__btn tow-test-panel__btn-sm" disabled={busy || !mainIp} onClick={() => handleJack("up")}>잭 업</button>
            <button className="btn btn--outline jack-test-panel__btn tow-test-panel__btn-sm" disabled={busy || !mainIp || loopRunning} onClick={() => handleJack("down")}>잭 다운</button>
            <button className="btn btn--outline jack-test-panel__btn tow-test-panel__btn-sm" disabled={busy || !mainIp}
              onClick={async () => { if (await call("슬립 해제", `/api/tow-test/clear-slip/${mainIp}`, { method: "POST" })) addLog("슬립 에러 해제"); }}>슬립 해제</button>
          </div>
        </>
      ) : (
        <>
          <div className="jack-test-panel__buttons" style={{ marginTop: 0 }}>
            <button className="btn btn--outline jack-test-panel__btn tow-test-panel__btn-sm" disabled={busy || !hasSideRobot}
              onClick={() => handleSideAction("바퀴 풀기", "/api/tow-test/auto-hold/{ip}?enable=false")}>S300 바퀴 풀기</button>
            <button className="btn btn--outline jack-test-panel__btn tow-test-panel__btn-sm" disabled={busy || !hasSideRobot}
              onClick={() => handleSideAction("바퀴 잠금", "/api/tow-test/auto-hold/{ip}?enable=true")}>S300 바퀴 잠금</button>
          </div>
          <div className="jack-test-panel__buttons" style={{ marginTop: 0 }}>
            <button className="btn btn--outline jack-test-panel__btn tow-test-panel__btn-sm" disabled={busy || !hasSideRobot}
              onClick={() => handleSideAction("E-Stop 해제", "/api/tow-test/confirm-estop/{ip}")}>S300 E-Stop 해제</button>
            <button className="btn btn--outline jack-test-panel__btn tow-test-panel__btn-sm" disabled={busy || !mainIp}
              onClick={async () => { if (await call("슬립 해제", `/api/tow-test/clear-slip/${mainIp}`, { method: "POST" })) addLog("S600 슬립 에러 해제"); }}>S600 슬립 해제</button>
          </div>
        </>
      )}

      <h3 className="jack-test-panel__title" style={{ marginTop: 8 }}>반복 주행</h3>
      <div className="jack-test-panel__form">
        <label className="jack-test-panel__label">
          경로 (순서대로 이동, 마지막 다음엔 처음으로)
          <div style={{ display: "flex", gap: 6 }}>
            <select className="jack-test-panel__select" value={addPoiId} onChange={(e) => setAddPoiId(Number(e.target.value))} disabled={loopRunning}>
              <option value={0}>POI 선택</option>
              {pois.filter((p) => p.type !== "charging").map((p) => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </select>
            <button className="btn btn--outline tow-test-panel__btn-sm" style={{ whiteSpace: "nowrap" }} disabled={!addPoiId || loopRunning}
              onClick={() => { setLoopPoiIds([...loopPoiIds, addPoiId]); setAddPoiId(0); }}>추가</button>
          </div>
        </label>
        {loopPoiIds.length > 0 && (
          <div className="jack-test-panel__route-info" style={{ display: "flex", flexWrap: "wrap", gap: 4, alignItems: "center" }}>
            {loopPoiIds.map((id, i) => (
              <span key={i} className="tow-test-panel__chip">
                {i + 1}. {poiName(id)}
                {!loopRunning && <button onClick={() => setLoopPoiIds(loopPoiIds.filter((_, j) => j !== i))}>×</button>}
              </span>
            ))}
          </div>
        )}
        <div className="tow-test-panel__row3">
          <label className="jack-test-panel__label">반복 횟수 (0=무한){numInput(laps, setLaps, 1)}</label>
          <label className="jack-test-panel__label">도착 후 대기 (초){numInput(waitSec, setWaitSec, 1)}</label>
          <label className="jack-test-panel__label" style={{ justifyContent: "flex-end" }}>
            <span style={{ display: "flex", alignItems: "center", gap: 4 }}>
              <input type="checkbox" checked={stopOnFail} onChange={(e) => setStopOnFail(e.target.checked)} />실패 시 중지
            </span>
          </label>
        </div>
        {isRack && (
          <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12 }}>
            <input type="checkbox" checked={requireJack} onChange={(e) => setRequireJack(e.target.checked)} />
            잭 업 확인 후에만 이동 (매 구간 출발 전 확인)
          </label>
        )}
      </div>
      <div className="jack-test-panel__buttons" style={{ marginTop: 0 }}>
        <button className="btn btn--primary jack-test-panel__btn" disabled={busy || !mainIp || loopPoiIds.length < 2 || loopRunning} onClick={handleLoopStart}>반복 시작</button>
        <button className="btn btn--danger jack-test-panel__btn" disabled={!mainIp || !loopRunning} onClick={handleLoopStop}>중지</button>
      </div>

      {loop.state !== "idle" && (
        <div className="jack-test-panel__status" style={{ borderLeftColor: loop.state === "failed" ? "#ef4444" : loopRunning ? "#36dfc8" : "#94a3b8" }}>
          <span className="jack-test-panel__status-label">
            {LOOP_STATE_LABELS[loop.state]} · {loop.lap}/{loop.laps || "∞"}회차{loop.target ? ` · → ${loop.target}` : ""}
          </span>
          <span className="jack-test-panel__status-msg">
            성공 {loop.success} · 실패 {loop.fail} · 평균 {loop.avg_duration ?? "-"}s · 최대 {loop.max_duration ?? "-"}s
            {" "}· {loop.started_at}{loop.finished_at ? ` ~ ${loop.finished_at}` : ""}
          </span>
          {loop.message && <span className="jack-test-panel__status-msg">{loop.message}</span>}
          {!!loop.legs?.length && (
            <div className="tow-test-panel__log" style={{ fontSize: 11 }}>
              {loop.legs.map((l, i) => (
                <div key={i} style={{ color: l.result === "succeeded" ? undefined : "#ef4444" }}>
                  {l.at} [{l.lap}] {l.poi} {l.result === "succeeded" ? "✓" : "✗"} {l.duration}s{l.message ? ` — ${l.message}` : ""}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {preview && (
        <div className="jack-test-panel__status" style={{ borderLeftColor: preview.warnings.length ? "#f59e0b" : "#36dfc8" }}>
          <span className="jack-test-panel__status-label">
            합성: 폭 {preview.width}m · 길이 {preview.length}m · 회전반경 {preview.turn_radius}m
          </span>
          <span className="jack-test-panel__status-msg">
            S600 원본: 폭 {preview.main.width}m · 길이 {preview.main.length}m{preview.based_on_backup ? " (백업 기준)" : ""}
          </span>
          {preview.expand && mode === "simple" && (
            <span className="jack-test-panel__status-msg">
              확장: 좌/우 {preview.expand.left}m · 앞/뒤 {preview.expand.front}m (여유 margin 별도)
            </span>
          )}
          <FootprintSvg preview={preview} />
          <span className="jack-test-panel__status-msg">
            <span style={{ color: "#36dfc8" }}>■</span> S600 <span style={{ color: "#3b82f6" }}>■</span> {mode === "simple" ? "확장" : "S300"}{" "}
            <span style={{ color: "#ef4444" }}>■</span> 합성 풋프린트 · 점선 = 회전반경 · 위쪽 = 전방
          </span>
          {preview.warnings.map((w) => (
            <span key={w} className="jack-test-panel__status-msg" style={{ color: "#f59e0b" }}>⚠ {w}</span>
          ))}
        </div>
      )}

      {status && (
        <div className="jack-test-panel__route-info">
          <div>현재 effective 폭 {status.metrics?.width}m · 길이 {status.metrics?.length}m · 꼭짓점 {status.effective_footprint?.length}개</div>
          <div>속도 {status.speed?.max_forward_velocity} · 가속 {status.speed?.max_forward_acc} · 각속도 {status.speed?.max_angular_velocity}</div>
          <div>백업: {status.backup ? "있음 (합성 적용 중일 수 있음)" : "없음"}</div>
        </div>
      )}

      {log.length > 0 && (
        <div className="jack-test-panel__route-info tow-test-panel__log">
          {log.map((l, i) => <div key={i}>{l}</div>)}
        </div>
      )}
    </div>
  );
}
