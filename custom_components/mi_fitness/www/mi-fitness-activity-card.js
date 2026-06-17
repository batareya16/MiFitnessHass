class MiFitnessActivityCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._selectedDate = null;
    this._initialized = false;
    this._lastDate = null;
  }

  setConfig(config) {
    if (!config.user_id) throw new Error("mi-fitness-activity-card: user_id required");
    this._uid      = config.user_id;
    this._stepGoal = parseInt(config.step_goal ?? 10000);
    this._name     = config.name ?? "Daily Activity";
  }

  set hass(hass) {
    this._hass = hass;
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

  _dateStr(d) {
    return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
  }

  _getTier(steps) {
    const T = [
      { min: 15000, emoji: "🚀", title: "Intergalactic Falcon",  tagline: "Are you even human? You simply can't be stopped!" },
      { min: 10000, emoji: "🐆", title: "Rocket Cheetah",        tagline: "10K milestone crushed! Your sneakers are on fire!" },
      { min:  7500, emoji: "🐺", title: "Tireless Wolf",         tagline: "Almost there — the WHO target is about to surrender." },
      { min:  5000, emoji: "🦊", title: "Energetic Fox",         tagline: "Solid progress! You've covered a respectable distance." },
      { min:  3000, emoji: "🐈", title: "House Wanderer",        tagline: "Home territory thoroughly explored. Time to go outside?" },
      { min:  1500, emoji: "🐧", title: "Leisurely Penguin",     tagline: "A quick trip to the fridge counts too. Every step matters." },
      { min:     0, emoji: "🦥", title: "Lazy Sloth",            tagline: "Energy conservation mode on. You're a master of efficiency!" },
    ];
    return T.find(t => steps >= t.min) ?? T[T.length - 1];
  }

  _fmtSteps(n) {
    n = parseInt(n) || 0;
    if (n >= 1000) return `${(n / 1000).toFixed(1).replace(/\.0$/, "")}k`;
    return String(n);
  }

  _fmtDist(meters) {
    const m = parseFloat(meters) || 0;
    if (m < 1000) return `${Math.round(m)} m`;
    return `${(m / 1000).toFixed(2).replace(/\.?0+$/, "")} km`;
  }

  _getData() {
    const todayStr = this._dateStr(new Date());
    const selDate  = this._selectedDate || todayStr;
    const isToday  = selDate === todayStr;

    if (isToday) {
      return {
        isToday,
        selDate,
        todayStr,
        steps:    parseInt(this._state("steps_today"))                || 0,
        dist:     parseFloat(this._attr("steps_today", "distance_m")) || 0,
        calories: parseInt(this._state("calories_today"))             || 0,
        standCnt: parseInt(this._state("stand_hours_today"))          || 0,
        hourly:   this._attr("steps_today", "hourly")                 || [],
        standHrs: this._attr("stand_hours_today", "hours_list")       || [],
        noData:   false,
      };
    } else {
      const hist  = this._attr("steps_today", "history_14d") || [];
      const entry = hist.find(h => h.date === selDate);
      if (!entry) return { isToday, selDate, todayStr, noData: true };
      return {
        isToday,
        selDate,
        todayStr,
        steps:    entry.steps    || 0,
        dist:     entry.distance || 0,
        calories: entry.calories || 0,
        standCnt: null,
        hourly:   null,
        standHrs: null,
        noData:   false,
      };
    }
  }

  _buildHourlyBars(hourly) {
    const HOUR_START = 6, HOUR_END = 22;
    const hours = Array.from({ length: HOUR_END - HOUR_START + 1 }, (_, i) => HOUR_START + i);
    const byHour = {};
    for (const h of (hourly || [])) byHour[h.hour] = h.steps;
    const maxSteps = Math.max(1, ...Object.values(byHour));
    const chartH = 55;
    const barW = 100 / hours.length;

    return hours.map((h, i) => {
      const steps  = byHour[h] ?? 0;
      const pct    = steps / maxSteps;
      const height = steps > 0 ? Math.max(3, pct * chartH) : 0;
      const y      = chartH - height;
      const x      = i * barW + barW * 0.12;
      const w      = barW * 0.76;
      const fill   = steps === 0 ? "var(--divider-color)"
                   : pct > 0.7   ? "#ff6b35"
                   : pct > 0.35  ? "#ff9f43"
                                 : "#97C459";
      const opacity = steps === 0 ? "0.25" : "0.85";
      return { x, y, w, height, fill, opacity };
    });
  }

  _buildDOM() {
    const data = this._getData();
    const todayStr  = data.todayStr;
    const selDate   = data.selDate;
    const isToday   = data.isToday;
    const minDate   = this._dateStr(new Date(Date.now() - 13 * 86400000));
    const dateLabel = isToday
      ? "Today"
      : new Date(selDate + "T12:00:00").toLocaleDateString([], { month: "short", day: "numeric" });

    const HOUR_START = 6, HOUR_END = 22;
    const hours = Array.from({ length: HOUR_END - HOUR_START + 1 }, (_, i) => HOUR_START + i);
    const chartH = 55;
    const barW = 100 / hours.length;
    const standHours = Array.from({ length: 15 }, (_, i) => 8 + i);

    const hourlyTicksHTML = hours.filter(h => h % 3 === 0).map(h => {
      const pct = (h - HOUR_START + 0.5) / hours.length * 100;
      const lbl = h < 12 ? `${h}am` : h === 12 ? "12pm" : `${h-12}pm`;
      return `<span style="position:absolute;left:${pct}%;transform:translateX(-50%);font-size:9px;color:var(--secondary-text-color)">${lbl}</span>`;
    }).join("");

    this.shadowRoot.innerHTML = `
<style>
  :host { display: block; }
  ha-card { padding: 16px 16px 14px; position: relative; }
  .date-chip { position:absolute;top:14px;right:16px;display:flex;align-items:center;gap:4px;font-size:11px;color:var(--secondary-text-color);background:var(--primary-background-color);border-radius:12px;padding:3px 8px;cursor:pointer;z-index:1;white-space:nowrap; }
  .date-input { position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%;border:none; }
  .header { display:flex;align-items:center;gap:14px;margin-bottom:16px;padding-right:70px; }
  .emoji  { font-size:52px;line-height:1;flex-shrink:0; }
  .header-right { flex:1;min-width:0; }
  .tier-title { font-size:20px;font-weight:700;color:var(--primary-text-color);margin:0 0 4px; }
  .tagline    { font-size:12px;color:var(--secondary-text-color);margin-bottom:8px;line-height:1.3; }
  .prog-row   { display:flex;align-items:center;gap:8px; }
  .prog-bar   { flex:1;height:6px;border-radius:3px;background:var(--divider-color);overflow:hidden; }
  .prog-fill  { height:100%;border-radius:3px;transition:width .4s; }
  .prog-pct   { font-size:12px;font-weight:600;flex-shrink:0; }
  .section-title { font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--secondary-text-color);margin:0 0 6px; }
  .stats { display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-top:14px; }
  .stat  { background:var(--primary-background-color);border-radius:8px;padding:7px 6px; }
  .stat-l { font-size:9px;color:var(--secondary-text-color);margin-bottom:2px; }
  .stat-v { font-size:14px;font-weight:600;color:var(--primary-text-color);line-height:1.2; }
  .stat-u { font-size:10px;font-weight:400;color:var(--secondary-text-color); }
  .no-data { font-size:13px;color:var(--secondary-text-color);text-align:center;padding-top:8px; }
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
      <div class="emoji"></div>
      <div class="header-right">
        <div class="tier-title"></div>
        <div class="tagline"></div>
        <div class="prog-row">
          <div class="prog-bar"><div class="prog-fill"></div></div>
          <span class="prog-pct"></span>
        </div>
      </div>
    </div>

    <div class="hourly-section">
      <div class="section-title">Hourly Steps</div>
      <div style="margin-bottom:10px">
        <svg class="hourly-svg" width="100%" height="${chartH + 2}" viewBox="0 0 100 ${chartH + 2}"
             preserveAspectRatio="none" style="display:block;overflow:visible">
          ${hours.map((_, i) => {
            const x = i * barW + barW * 0.12;
            const w = barW * 0.76;
            return `<rect class="bar-${i}" x="${x}%" y="${chartH}" width="${w}%" height="0" rx="2" fill="var(--divider-color)" opacity="0.25"/>`;
          }).join("")}
        </svg>
        <div style="position:relative;height:16px;margin-top:2px">${hourlyTicksHTML}</div>
      </div>
      <div class="section-title">Active Hours</div>
      <div style="display:flex;margin-bottom:14px">
        ${standHours.map(h => `
          <div class="stand-dot-wrap-${h}" style="flex:1;display:flex;flex-direction:column;align-items:center;gap:3px">
            <div class="stand-dot-${h}" style="width:10px;height:10px;border-radius:50%;background:var(--divider-color);opacity:0.5"></div>
            ${h % 3 === 0
              ? `<span style="font-size:8px;color:var(--secondary-text-color);opacity:.7">${h < 12 ? h : h === 12 ? 12 : h-12}</span>`
              : `<span style="font-size:8px;opacity:0">·</span>`}
          </div>`).join("")}
      </div>
    </div>

    <div class="historical-note" style="display:none;text-align:center;padding:10px 0 14px;font-size:12px;color:var(--secondary-text-color)">
      Hourly breakdown available for today only
    </div>

    <div class="stats">
      <div class="stat"><div class="stat-l">Steps</div><div class="stat-v steps-val"></div></div>
      <div class="stat"><div class="stat-l">Distance</div><div class="stat-v dist-val"></div></div>
      <div class="stat"><div class="stat-l">Calories</div><div class="stat-v cal-val"></div></div>
      <div class="stat"><div class="stat-l">Stand</div><div class="stat-v stand-val"></div></div>
    </div>
  </div>
</ha-card>`;

    // Cache bar rects
    this._barRects = hours.map((_, i) => this.shadowRoot.querySelector(`.bar-${i}`));
    this._standDots = standHours.map(h => this.shadowRoot.querySelector(`.stand-dot-${h}`));
    this._standHoursList = standHours;

    // Date picker
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

  _update() {
    if (!this._hass) return;
    const data = this._getData();

    const noDataWrap   = this.shadowRoot.querySelector(".no-data-wrap");
    const mainContent  = this.shadowRoot.querySelector(".main-content");

    if (data.noData) {
      noDataWrap.style.display = "";
      mainContent.style.display = "none";
      this.shadowRoot.querySelector(".no-data").textContent = `No activity data for ${data.selDate}`;
      return;
    }

    noDataWrap.style.display = "none";
    mainContent.style.display = "";

    const { steps, dist, calories, standCnt, hourly, standHrs, isToday } = data;
    const tier     = this._getTier(steps);
    const goalPct  = Math.min(100, Math.round(steps / this._stepGoal * 100));
    const goalColor = goalPct >= 100 ? "#97C459" : goalPct >= 70 ? "#ff9f43" : "var(--primary-color, #03a9f4)";

    // Header
    this.shadowRoot.querySelector(".emoji").textContent = tier.emoji;
    this.shadowRoot.querySelector(".tier-title").textContent = tier.title;
    this.shadowRoot.querySelector(".tagline").textContent = tier.tagline;
    const fill = this.shadowRoot.querySelector(".prog-fill");
    fill.style.width = goalPct + "%";
    fill.style.background = goalColor;
    const pct = this.shadowRoot.querySelector(".prog-pct");
    pct.textContent = goalPct + "%";
    pct.style.color = goalColor;

    // Hourly / historical note
    const hourlySection  = this.shadowRoot.querySelector(".hourly-section");
    const historicalNote = this.shadowRoot.querySelector(".historical-note");
    if (isToday) {
      hourlySection.style.display = "";
      historicalNote.style.display = "none";

      // Update bars
      const HOUR_START = 6;
      const bars = this._buildHourlyBars(hourly);
      const chartH = 55;
      bars.forEach((b, i) => {
        const rect = this._barRects[i];
        if (!rect) return;
        rect.setAttribute("y", b.y);
        rect.setAttribute("height", b.height);
        rect.setAttribute("fill", b.fill);
        rect.setAttribute("opacity", b.opacity);
      });

      // Stand dots
      const standSet = new Set(standHrs || []);
      const battColor = "#97C459";
      this._standDots.forEach((dot, i) => {
        const h = this._standHoursList[i];
        const active = standSet.has(h);
        dot.style.background = active ? battColor : "var(--divider-color)";
        dot.style.opacity = active ? "1" : "0.5";
      });
    } else {
      hourlySection.style.display = "none";
      historicalNote.style.display = "";
    }

    // Stats
    this.shadowRoot.querySelector(".steps-val").innerHTML =
      `${this._fmtSteps(steps)}<br><span class="stat-u">of ${this._fmtSteps(this._stepGoal)}</span>`;
    this.shadowRoot.querySelector(".dist-val").textContent = this._fmtDist(dist);
    this.shadowRoot.querySelector(".cal-val").innerHTML =
      `${calories || "—"}<span class="stat-u">${calories ? " kcal" : ""}</span>`;
    this.shadowRoot.querySelector(".stand-val").innerHTML =
      `${standCnt !== null ? standCnt : "—"}<span class="stat-u">${standCnt !== null ? " h" : ""}</span>`;
  }
}

customElements.define("mi-fitness-activity-card", MiFitnessActivityCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type:        "mi-fitness-activity-card",
  name:        "Mi Fitness Activity",
  description: "Daily activity card with hourly steps chart, tier system and date picker",
});