class MiFitnessSleepCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._selectedDate = null;
    this._lastDate = null;
  }

  setConfig(config) {
    // user_id is optional — auto-detected from existing entities if omitted.
    this._uid = config.user_id ? String(config.user_id) : null;
  }

  _detectUid(hass) {
    // Find the account id from any mi_fitness entity so users don't need to
    // look up their Xiaomi user_id. Matches sensor.mi_fitness_<uid>_sleep_duration.
    const re = /^sensor\.mi_fitness_(.+)_sleep_duration$/;
    for (const eid in hass.states) {
      const m = eid.match(re);
      if (m) return m[1];
    }
    return null;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._uid) this._uid = this._detectUid(hass);
    const selDate = this._selectedDate || this._dateStr(new Date());
    if (selDate !== this._lastDate) {
      this._buildDOM();
      this._lastDate = selDate;
    }
    this._update();
  }

  getCardSize() { return 5; }

  _eid(name)        { return `sensor.mi_fitness_${this._uid}_${name}`; }
  _state(name)      { return this._hass.states[this._eid(name)]?.state ?? null; }
  _attr(name, attr) { return this._hass.states[this._eid(name)]?.attributes?.[attr] ?? null; }

  _fmtDur(minutes) {
    const m = parseInt(minutes) || 0;
    const h = Math.floor(m / 60);
    const mm = m % 60;
    if (h === 0) return `${mm}min`;
    return mm === 0 ? `${h}h` : `${h}h ${String(mm).padStart(2, "0")}min`;
  }

  _tsToLocal(v) {
    if (!v || v === "unknown" || v === "unavailable") return null;
    const d = new Date(typeof v === "number" ? v * 1000 : v);
    return isNaN(d) ? null : d;
  }

  _fmtTime(d) {
    if (!d) return "—";
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  _dateStr(d) {
    return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
  }

  static get ANIMALS() {
    return {
      koala:      ["🐨", "Koala"],
      brown_bear: ["🐻", "Brown Bear"],
      sheep:      ["🐑", "Sheep"],
      penguin:    ["🐧", "Penguin"],
      night_owl:  ["🦉", "Night Owl"],
      shark:      ["🦈", "Shark"],
    };
  }

  static get STAGES() {
    return [
      { key: "deep",  label: "Deep",  color: "#3949ab" },
      { key: "rem",   label: "REM",   color: "#8e24aa" },
      { key: "light", label: "Light", color: "#29b6f6" },
      { key: "awake", label: "Awake", color: "#ef5350" },
    ];
  }

  _getData() {
    const todayStr = this._dateStr(new Date());
    const selDate  = this._selectedDate || todayStr;
    const isToday  = selDate === todayStr;

    if (isToday) {
      const bedDate  = this._tsToLocal(this._state("bedtime"));
      const wakeDate = this._tsToLocal(this._state("wake_up_time"));
      // Live sleep sensors hold the LAST synced night and are never cleared.
      // If the watch hasn't synced today's sleep yet, they still carry an
      // older night — don't pass that stale record off as "Today". Require
      // the wake-up to have happened today.
      const fresh = wakeDate && this._dateStr(wakeDate) === todayStr;
      if (!fresh) return { isToday, selDate, todayStr, noData: true };
      return {
        isToday, selDate, todayStr, noData: false,
        ct:       this._state("sleep_chronotype"),
        quality:  parseInt(this._state("sleep_quality"))        || 0,
        dur:      parseInt(this._state("sleep_duration"))       || 0,
        deep:     parseInt(this._state("deep_sleep_duration"))  || 0,
        light:    parseInt(this._state("light_sleep_duration")) || 0,
        rem:      parseInt(this._state("rem_sleep_duration"))   || 0,
        awake:    this._state("sleep_awake_count")              ?? "—",
        avgHr:    this._state("sleep_average_heart_rate")       ?? "—",
        bedDate,
        wakeDate,
        stages:   this._attr("sleep_duration", "stages"),
      };
    } else {
      const hist  = this._attr("sleep_duration", "history_14d") || [];
      const entry = hist.find(h => h.date === selDate);
      if (!entry) return { isToday, selDate, todayStr, noData: true };
      return {
        isToday, selDate, todayStr, noData: false,
        ct:       this._attr("sleep_duration", "chronotype"),
        quality:  entry.quality    || 0,
        dur:      entry.duration   || 0,
        deep:     entry.deep       || 0,
        light:    entry.light      || 0,
        rem:      entry.rem        || 0,
        awake:    entry.awake_count ?? "—",
        avgHr:    "—",
        bedDate:  entry.bedtime ? new Date(entry.bedtime * 1000) : null,
        wakeDate: entry.wakeup  ? new Date(entry.wakeup  * 1000) : null,
        stages:   null,
      };
    }
  }

  _buildDOM() {
    const todayStr = this._dateStr(new Date());
    const selDate  = this._selectedDate || todayStr;
    const minDate  = this._dateStr(new Date(Date.now() - 13 * 86400000));
    const isToday  = selDate === todayStr;
    const dateLabel = isToday
      ? "Today"
      : new Date(selDate + "T12:00:00").toLocaleDateString([], { month: "short", day: "numeric" });

    this.shadowRoot.innerHTML = `
<style>
  :host { display: block; }
  ha-card { padding: 20px 16px 16px; font-family: var(--paper-font-body1_-_font-family, sans-serif); position: relative; min-height: 392px; box-sizing: border-box; }
  .date-chip { position:absolute;top:16px;right:16px;display:flex;align-items:center;gap:4px;font-size:11px;color:var(--secondary-text-color);background:var(--primary-background-color);border-radius:12px;padding:3px 8px;cursor:pointer;z-index:1;white-space:nowrap; }
  .date-input { position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%;border:none; }
  .header { display:flex;align-items:center;gap:14px;margin-bottom:20px;padding-right:70px; }
  .animal-emoji { font-size:52px;line-height:1;flex-shrink:0; }
  .header-right { flex:1;min-width:0; }
  .animal-name  { font-size:20px;font-weight:700;color:var(--primary-text-color);margin:0 0 6px; }
  .badges { display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px; }
  .badge  { padding:3px 10px;border-radius:20px;font-size:12px;font-weight:600; }
  .times  { font-size:13px;color:var(--secondary-text-color);display:flex;align-items:center;gap:6px; }
  .times-arrow { opacity:.5; }
  .section-title { font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--secondary-text-color);margin:0 0 10px; }
  .timeline { margin-bottom:4px; }
  .tl-row   { display:flex;align-items:center;margin-bottom:5px;height:20px; }
  .tl-label { width:42px;font-size:11px;font-weight:600;flex-shrink:0; }
  .tl-track { flex:1;height:20px;background:var(--divider-color,rgba(0,0,0,.08));border-radius:4px;position:relative;overflow:hidden; }
  .bar { position:absolute;top:0;height:100%;opacity:.9;transition:opacity .15s; }
  .bar:hover { opacity:1;cursor:default; }
  .tl-axis  { position:relative;height:16px;margin-left:42px;margin-top:2px;margin-bottom:14px; }
  .tick     { position:absolute;font-size:10px;color:var(--secondary-text-color);transform:translateX(-50%);white-space:nowrap; }
  .no-data-wrap { display:flex;align-items:center;justify-content:center;min-height:340px; }
  .no-data  { text-align:center;color:var(--secondary-text-color);font-size:13px;padding:16px 0; }
  .phases { display:flex;gap:8px;margin-bottom:16px; }
  .phase  { flex:1;display:flex;flex-direction:column;align-items:center;gap:4px;padding:10px 4px;border-radius:10px;background:var(--secondary-background-color,rgba(0,0,0,.04)); }
  .phase-icon { width:8px;height:8px;border-radius:50%; }
  .phase-name { font-size:10px;font-weight:600;color:var(--secondary-text-color);text-transform:uppercase;letter-spacing:.05em; }
  .phase-val  { font-size:14px;font-weight:700;color:var(--primary-text-color);text-align:center; }
  .footer { display:flex;justify-content:space-around;padding-top:14px;border-top:1px solid var(--divider-color,rgba(0,0,0,.08)); }
  .footer-item  { display:flex;flex-direction:column;align-items:center;gap:2px; }
  .footer-label { font-size:11px;color:var(--secondary-text-color); }
  .footer-val   { font-size:22px;font-weight:700;color:var(--primary-text-color); }
  .footer-unit  { font-size:12px;font-weight:400;color:var(--secondary-text-color); }
</style>
<ha-card>
  <div class="date-chip">
    <ha-icon icon="mdi:calendar-month" style="--mdc-icon-size:13px;opacity:.7"></ha-icon>
    <span class="date-label">${dateLabel}</span>
    <input class="date-input" type="date" value="${selDate}" min="${minDate}" max="${todayStr}">
  </div>

  <div class="no-data-wrap" style="display:none">
    <div class="no-data"></div>
  </div>

  <div class="main-content">
    <div class="header">
      <div class="animal-emoji"></div>
      <div class="header-right">
        <div class="animal-name"></div>
        <div class="badges">
          <span class="badge badge-quality"></span>
          <span class="badge badge-duration"></span>
        </div>
        <div class="times">
          <span class="time-bed"></span>
          <span class="times-arrow">→</span>
          <span class="time-wake"></span>
        </div>
      </div>
    </div>

    <div class="section-title">Sleep Stages</div>
    <div class="timeline-wrap"></div>

    <div class="section-title">Summary</div>
    <div class="phases">
      <div class="phase">
        <div class="phase-icon" style="background:#3949ab"></div>
        <div class="phase-name">Deep</div>
        <div class="phase-val deep-val"></div>
      </div>
      <div class="phase">
        <div class="phase-icon" style="background:#29b6f6"></div>
        <div class="phase-name">Light</div>
        <div class="phase-val light-val"></div>
      </div>
      <div class="phase">
        <div class="phase-icon" style="background:#8e24aa"></div>
        <div class="phase-name">REM</div>
        <div class="phase-val rem-val"></div>
      </div>
    </div>

    <div class="footer">
      <div class="footer-item">
        <div class="footer-label">Awake times</div>
        <div class="footer-val awake-val"></div>
      </div>
      <div class="footer-item">
        <div class="footer-label">Sleep Avg HR</div>
        <div class="footer-val avghr-val"></div>
      </div>
    </div>
  </div>
</ha-card>`;

    const input = this.shadowRoot.querySelector(".date-input");
    if (input) {
      input.addEventListener("change", e => {
        this._selectedDate = e.target.value === todayStr ? null : e.target.value;
        this._lastDate = this._selectedDate || todayStr;
        this._buildDOM();
        this._update();
      });
    }
  }

  _buildTimeline(stages, bedDate, wakeDate) {
    const wrap = this.shadowRoot.querySelector(".timeline-wrap");
    if (!stages?.length || !bedDate || !wakeDate) {
      wrap.innerHTML = `<div class="no-data">No stage data available</div>`;
      return;
    }

    const bedTs  = bedDate.getTime() / 1000;
    const wakeTs = wakeDate.getTime() / 1000;
    const total  = wakeTs - bedTs;

    const rows = MiFitnessSleepCard.STAGES.map(({ key, label, color }) => {
      const bars = (stages || [])
        .filter(s => s.label === key)
        .map(s => {
          const l = ((s.start - bedTs) / total * 100).toFixed(2);
          const w = ((s.end - s.start) / total * 100).toFixed(2);
          const t1 = new Date(s.start*1000).toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"});
          const t2 = new Date(s.end*1000).toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"});
          return `<div class="bar" style="left:${l}%;width:${w}%;background:${color}" title="${label}: ${t1} – ${t2}"></div>`;
        }).join("");
      return `<div class="tl-row">
        <span class="tl-label" style="color:${color}">${label}</span>
        <div class="tl-track">${bars}</div>
      </div>`;
    }).join("");

    let tickMs = Math.ceil(bedDate.getTime() / 3600000) * 3600000;
    const ticks = [];
    while (tickMs <= wakeDate.getTime()) {
      const pct   = ((tickMs / 1000 - bedTs) / total * 100).toFixed(2);
      const label = new Date(tickMs).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      ticks.push(`<span class="tick" style="left:${pct}%">${label}</span>`);
      tickMs += 3600000;
    }

    wrap.innerHTML = `
      <div class="timeline">
        ${rows}
        <div class="tl-axis">${ticks.join("")}</div>
      </div>`;
  }

  _update() {
    if (!this._hass) return;
    const data = this._getData();

    const noDataWrap  = this.shadowRoot.querySelector(".no-data-wrap");
    const mainContent = this.shadowRoot.querySelector(".main-content");

    if (data.noData) {
      noDataWrap.style.display = "";
      mainContent.style.display = "none";
      this.shadowRoot.querySelector(".no-data").textContent =
        data.isToday ? "No sleep data yet today" : `No sleep data for ${data.selDate}`;
      return;
    }

    noDataWrap.style.display = "none";
    mainContent.style.display = "";

    const { ct, quality, dur, deep, light, rem, awake, avgHr, bedDate, wakeDate, stages } = data;
    const [emoji, animalName] = MiFitnessSleepCard.ANIMALS[ct] ?? ["😴", ct ?? "—"];
    const qColor = quality >= 80 ? "#4caf50" : quality >= 60 ? "#ff9800" : "#e53935";

    // Header
    this.shadowRoot.querySelector(".animal-emoji").textContent = emoji;
    this.shadowRoot.querySelector(".animal-name").textContent  = animalName;

    const bq = this.shadowRoot.querySelector(".badge-quality");
    bq.textContent = `Quality ${quality}%`;
    bq.style.cssText = `padding:3px 10px;border-radius:20px;font-size:12px;font-weight:600;background:${qColor}22;color:${qColor}`;

    const bd = this.shadowRoot.querySelector(".badge-duration");
    bd.textContent = this._fmtDur(dur);
    bd.style.cssText = "padding:3px 10px;border-radius:20px;font-size:12px;font-weight:600;background:color-mix(in srgb, var(--primary-color, #03a9f4) 15%, transparent);color:var(--primary-color, #03a9f4)";

    this.shadowRoot.querySelector(".time-bed").textContent  = this._fmtTime(bedDate);
    this.shadowRoot.querySelector(".time-wake").textContent = this._fmtTime(wakeDate);

    // Timeline — rebuild since stages data changes
    this._buildTimeline(stages, bedDate, wakeDate);

    // Phases
    this.shadowRoot.querySelector(".deep-val").textContent  = this._fmtDur(deep);
    this.shadowRoot.querySelector(".light-val").textContent = this._fmtDur(light);
    this.shadowRoot.querySelector(".rem-val").textContent   = this._fmtDur(rem);

    // Footer
    this.shadowRoot.querySelector(".awake-val").textContent = awake;
    this.shadowRoot.querySelector(".avghr-val").innerHTML   =
      `${avgHr}${avgHr !== "—" ? ' <span class="footer-unit">bpm</span>' : ""}`;
  }
}

if (!customElements.get("mi-fitness-sleep-card"))
  customElements.define("mi-fitness-sleep-card", MiFitnessSleepCard);