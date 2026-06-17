class MiFitnessSleepStreakCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._initialized = false;
  }

  setConfig(config) {
    // user_id is optional — auto-detected from existing entities if omitted.
    this._uid     = config.user_id ? String(config.user_id) : null;
    this._goalMin = (parseInt(config.goal_hours   ?? 8)  * 60)
                  + (parseInt(config.goal_minutes ?? 0));
    this._name    = config.name ?? "Sleep Goal";
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
    if (!this._initialized) {
      this._buildDOM();
      this._initialized = true;
    }
    this._update();
  }

  getCardSize() { return 4; }

  _fmtGoal() {
    const h  = Math.floor(this._goalMin / 60);
    const mm = this._goalMin % 60;
    return mm === 0 ? `${h}h+` : `${h}h ${mm}min+`;
  }

  _fmtDur(min) {
    const m = parseInt(min) || 0;
    const h = Math.floor(m / 60);
    const r = m % 60;
    if (h === 0) return `${r}m`;
    return r === 0 ? `${h}h` : `${h}h ${r}m`;
  }

  _dateStr(d) {
    return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
  }

  _byDate(history) {
    const m = {};
    for (const r of (history || [])) if (r.date) m[r.date] = r.duration || 0;
    return m;
  }

  _calcStreak(byDate) {
    let streak = 0;
    const d = new Date();
    for (let i = 0; i < 14; i++) {
      const key = this._dateStr(d);
      if (!(key in byDate)) {
        if (i === 0) { d.setDate(d.getDate() - 1); continue; }
        break;
      }
      if (byDate[key] >= this._goalMin) { streak++; d.setDate(d.getDate() - 1); }
      else break;
    }
    return streak;
  }

  _calcBest(byDate) {
    const dates = Object.keys(byDate).sort();
    let best = 0, cur = 0;
    for (const key of dates) {
      if (byDate[key] >= this._goalMin) { cur++; best = Math.max(best, cur); }
      else cur = 0;
    }
    return best;
  }

  _thisWeek(byDate) {
    const today = new Date();
    const dow   = (today.getDay() + 6) % 7;
    let count   = 0;
    for (let i = 0; i <= dow; i++) {
      const d = new Date(today);
      d.setDate(today.getDate() - i);
      const dur = byDate[this._dateStr(d)];
      if (dur !== undefined && dur >= this._goalMin) count++;
    }
    return { count, total: dow + 1 };
  }

  _getDays() {
    const today = new Date();
    return Array.from({ length: 7 }, (_, i) => {
      const d = new Date(today);
      d.setDate(today.getDate() - (6 - i));
      return d;
    });
  }

  _buildDOM() {
    const days   = this._getDays();
    const chartH = 50;
    const barW   = 100 / 7;

    this.shadowRoot.innerHTML = `
<style>
  :host { display: block; }
  ha-card { padding: 14px 16px 12px; }
  .header { display:flex; align-items:center; justify-content:space-between; margin-bottom:12px; }
  .title  { font-size:15px; font-weight:500; color:var(--primary-text-color); display:flex; align-items:center; gap:6px; }
  .goal-sub { font-size:11px; color:var(--secondary-text-color); font-weight:400; margin-left:2px; }
  .dots   { display:flex; gap:4px; margin-bottom:14px; }
  .chart-wrap { margin-bottom:12px; }
  .chart-label { font-size:9px; color:var(--secondary-text-color); opacity:.7; margin-bottom:3px; text-align:right; }
  .stats  { display:grid; grid-template-columns:1fr 1fr; gap:7px; }
  .stat   { background:var(--primary-background-color); border-radius:8px; padding:7px 10px; }
  .stat-l { font-size:10px; color:var(--secondary-text-color); margin-bottom:1px; }
  .stat-v { font-size:15px; font-weight:500; color:var(--primary-text-color); }
  .stat-s { font-size:11px; color:var(--secondary-text-color); font-weight:400; }
</style>
<ha-card>
  <div class="header">
    <div class="title">
      <ha-icon icon="mdi:sleep" style="--mdc-icon-size:18px;color:var(--primary-text-color)"></ha-icon>
      <span class="card-name"></span>
      <span class="goal-sub"></span>
    </div>
    <span class="streak-badge"></span>
  </div>

  <div class="dots"></div>

  <div class="chart-wrap">
    <div class="chart-label"></div>
    <div class="chart-labels-row" style="display:flex;margin-bottom:3px"></div>
    <svg class="chart-svg" width="100%" preserveAspectRatio="none" style="display:block;overflow:visible">
      <line class="goal-line" stroke="#97C459" stroke-width="0.6" stroke-dasharray="2,2" opacity="0.7" vector-effect="non-scaling-stroke"/>
    </svg>
  </div>

  <div class="stats">
    <div class="stat">
      <div class="stat-l">this week</div>
      <div class="stat-v week-val"></div>
    </div>
    <div class="stat">
      <div class="stat-l">best streak</div>
      <div class="stat-v best-val"></div>
    </div>
  </div>
</ha-card>`;

    // Static text
    this.shadowRoot.querySelector(".card-name").textContent  = this._name;
    this.shadowRoot.querySelector(".goal-sub").textContent   = this._fmtGoal();
    this.shadowRoot.querySelector(".chart-label").textContent = `${this._fmtGoal()} goal ·····`;

    // Build dot elements once
    const dotsEl = this.shadowRoot.querySelector(".dots");
    this._dotEls = days.map((d, i) => {
      const isToday = i === 6;
      const wrap    = document.createElement("div");
      wrap.style.cssText = "text-align:center;flex:1";

      const circle = document.createElement("div");
      circle.style.cssText = "position:relative;width:30px;height:30px;border-radius:50%;margin:0 auto 4px";

      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.style.cssText = "position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);display:none";
      circle.appendChild(svg);

      const label = document.createElement("div");
      label.style.cssText = `font-size:9px;color:var(--secondary-text-color);${isToday ? "font-weight:600" : ""}`;
      label.textContent = d.toLocaleDateString([], { weekday: "short" });

      wrap.appendChild(circle);
      wrap.appendChild(label);
      dotsEl.appendChild(wrap);
      return { circle, svg, isToday };
    });

    // Build chart bar rects and label divs once
    const chartSvg  = this.shadowRoot.querySelector(".chart-svg");
    const labelsRow = this.shadowRoot.querySelector(".chart-labels-row");
    chartSvg.setAttribute("height", chartH + 2);
    chartSvg.setAttribute("viewBox", `0 0 100 ${chartH + 2}`);

    this._barRects = days.map((_, i) => {
      const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      rect.setAttribute("x", `${i * barW + barW * 0.18}%`);
      rect.setAttribute("width", `${barW * 0.64}%`);
      rect.setAttribute("rx", "2");
      rect.setAttribute("y", chartH);
      rect.setAttribute("height", "0");
      chartSvg.appendChild(rect);

      const lbl = document.createElement("div");
      lbl.style.cssText = "flex:1;text-align:center;font-size:8px;line-height:1.2";
      labelsRow.appendChild(lbl);

      return { rect, lbl };
    });
  }

  _update() {
    const durSensor = this._hass.states[`sensor.mi_fitness_${this._uid}_sleep_duration`];
    const history   = durSensor?.attributes?.history_14d ?? [];
    const byDate    = this._byDate(history);
    const days      = this._getDays();

    const streak           = this._calcStreak(byDate);
    const best             = this._calcBest(byDate);
    const { count, total } = this._thisWeek(byDate);

    // Streak badge
    const badge = this.shadowRoot.querySelector(".streak-badge");
    if (streak > 0) {
      badge.textContent = `${streak}-day streak 🔥`;
      badge.style.cssText = "font-size:12px;font-weight:500;color:#639922";
    } else {
      badge.textContent = "No streak yet";
      badge.style.cssText = "font-size:12px;color:var(--secondary-text-color)";
    }

    // Dots
    this._dotEls.forEach(({ circle, svg, isToday }, i) => {
      const key     = this._dateStr(days[i]);
      const dur     = byDate[key];
      const done    = dur !== undefined && dur >= this._goalMin;
      const hasData = dur !== undefined;

      circle.style.background = done ? "#97C459" : "var(--primary-background-color)";
      circle.style.border = isToday
        ? (done ? "2px solid #639922" : "2px solid var(--secondary-text-color)")
        : (done ? "none" : "0.5px solid var(--divider-color)");

      if (done) {
        svg.style.display = "";
        svg.setAttribute("width", "12");
        svg.setAttribute("height", "12");
        svg.setAttribute("viewBox", "0 0 12 12");
        svg.innerHTML = `<polyline points="2,6 5,9 10,3" fill="none" stroke="#27500A" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>`;
      } else if (hasData) {
        svg.style.display = "";
        svg.setAttribute("width", "10");
        svg.setAttribute("height", "10");
        svg.setAttribute("viewBox", "0 0 10 10");
        svg.innerHTML = `<line x1="2" y1="2" x2="8" y2="8" stroke="var(--secondary-text-color)" stroke-width="1.5" stroke-linecap="round"/><line x1="8" y1="2" x2="2" y2="8" stroke="var(--secondary-text-color)" stroke-width="1.5" stroke-linecap="round"/>`;
      } else {
        svg.style.display = "none";
      }
    });

    // Chart
    const durations = days.map(d => byDate[this._dateStr(d)] ?? 0);
    const maxDur    = Math.max(this._goalMin * 1.4, ...durations, 60);
    const chartH    = 50;
    const goalPct   = (this._goalMin / maxDur * chartH).toFixed(1);
    const goalY     = (chartH - goalPct).toFixed(1);

    const goalLine = this.shadowRoot.querySelector(".goal-line");
    goalLine.setAttribute("x1", "0");
    goalLine.setAttribute("y1", goalY);
    goalLine.setAttribute("x2", "100");
    goalLine.setAttribute("y2", goalY);

    this._barRects.forEach(({ rect, lbl }, i) => {
      const dur     = durations[i];
      const met     = dur >= this._goalMin;
      const h       = dur > 0 ? Math.max(3, dur / maxDur * chartH) : 0;
      const y       = chartH - h;
      const fill    = dur === 0 ? "var(--divider-color)" : met ? "#97C459" : "var(--secondary-text-color)";
      const opacity = dur === 0 ? "0.4" : "0.85";

      rect.setAttribute("y", y);
      rect.setAttribute("height", h);
      rect.setAttribute("fill", fill);
      rect.setAttribute("opacity", opacity);

      lbl.textContent   = dur > 0 ? this._fmtDur(dur) : "";
      lbl.style.color   = dur === 0 ? "transparent" : met ? "#639922" : "var(--secondary-text-color)";
      lbl.style.fontWeight = met ? "600" : "400";
    });

    // Stats
    this.shadowRoot.querySelector(".week-val").innerHTML = `${count} <span class="stat-s">/ ${total}</span>`;
    this.shadowRoot.querySelector(".best-val").innerHTML = `${best} <span class="stat-s">days</span>`;
  }
}

customElements.define("mi-fitness-sleep-streak-card", MiFitnessSleepStreakCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type:        "mi-fitness-sleep-streak-card",
  name:        "Mi Fitness Sleep Streak",
  description: "Weekly sleep goal streak tracker with history chart",
});