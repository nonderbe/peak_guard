class PeakGuardPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._data = null;
    this._activeTab = "peak";
    this._savingsChart = "peak";  // welke grafiek zichtbaar in savings-tab
    this._evMode = false;          // houdt bij of de modal in EV-modus staat
    this._wizardStep = 1;          // huidige stap in de EV-wizard
    this._editDevice = null;
    this._editCascadeType = "peak";
    this._lastStatusUpdate = 0;

    // De modal leeft als een persistente DOM-node, buiten de render-cyclus
    this._modalEl = null;
    this._modalVisible = false;

    // Debug logger
    this._log = (msg, ...args) => console.log(`[PeakGuard DEBUG] ${msg}`, ...args);

    // EV spanning: 1-fase = 230 V, 3-fasen = 400 V
    this._evVoltage = (phases) => phases === 3 ? 400 : 230;

    // Countdown timer state
    this._countdownInterval = null;  // setInterval handle
    this._nextCheckAt = null;        // Date wanneer de volgende check verwacht wordt
  }

  // ------------------------------------------------------------------ //
  //  HA lifecycle                                                        //
  // ------------------------------------------------------------------ //

  connectedCallback() {
    this._refreshInterval = setInterval(() => {
      // Nooit data herladen als modal open is
      if (this._hass && !this._modalVisible && !this._fetchInProgress) this._fetchData();
    }, 15000);
    // Countdown tick elke seconde
    this._countdownInterval = setInterval(() => this._tickCountdown(), 1000);
  }

  disconnectedCallback() {
    clearInterval(this._refreshInterval);
    clearInterval(this._countdownInterval);
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._data) {
      // Voorkom parallelle fetches terwijl de eerste nog bezig is
      if (this._fetchInProgress) {
        this._log('hass setter: fetch al bezig, overgeslagen');
        return;
      }
      this._log('hass setter: _data=null => _fetchData(). modalVisible=' + this._modalVisible);
      this._fetchData();
      return;
    }
    if (this._modalVisible) {
      this._log('hass setter: modal open, geblokkeerd OK');
      return;
    }
    const now = Date.now();
    if (now - this._lastStatusUpdate > 1000) {
      this._lastStatusUpdate = now;
      this._updateLiveStatus();
    }
  }

  // ------------------------------------------------------------------ //
  //  Data laden / opslaan                                                //
  // ------------------------------------------------------------------ //

  async _fetchData() {
    this._log('_fetchData() gestart. modalVisible=' + this._modalVisible);
    this._fetchInProgress = true;
    try {
      const resp = await this._hass.fetchWithAuth("/api/peak_guard/cascade");
      if (resp.ok) {
        this._data = await resp.json();
        this._log('_fetchData() data ontvangen. modalVisible=' + this._modalVisible);
        // Bereken wanneer de volgende controller-check verwacht wordt
        this._recalcNextCheck();
        if (!this._modalVisible) {
          this._log('_fetchData() => _render() aanroepen');
          this._render();
        } else {
          this._log('_fetchData() => render GEBLOKKEERD (modal open) OK');
        }
      } else {
        this._renderError(`API fout: ${resp.status}`);
      }
    } catch (e) {
      this._renderError(`Verbindingsfout: ${e.message}`);
    } finally {
      this._fetchInProgress = false;
    }
  }

  // Berekent wanneer de volgende check verwacht wordt op basis van last_loop_at en interval_s
  _recalcNextCheck() {
    const st = this._data?.status;
    if (!st?.last_loop_at || !st?.interval_s) { this._nextCheckAt = null; return; }
    const lastMs = new Date(st.last_loop_at).getTime();
    if (isNaN(lastMs)) { this._nextCheckAt = null; return; }
    this._nextCheckAt = new Date(lastMs + st.interval_s * 1000);
  }

  // Wordt elke seconde aangeroepen — werkt de countdown in de DOM bij
  _tickCountdown() {
    const el = this.shadowRoot?.querySelector("#countdown-val");
    const bar = this.shadowRoot?.querySelector("#countdown-bar");
    if (!el || !this._nextCheckAt || !this._data?.status?.interval_s) return;
    const secLeft = Math.max(0, Math.round((this._nextCheckAt - Date.now()) / 1000));
    const intervalS = this._data.status.interval_s;
    el.textContent = secLeft > 0 ? `${secLeft}s` : "bezig…";
    if (bar) bar.style.width = `${Math.round((1 - secLeft / intervalS) * 100)}%`;
  }

  // Roept de force_check API aan en reset de countdown
  async _forceCheck() {
    const btn = this.shadowRoot?.querySelector("#btn-force-check");
    if (btn) { btn.disabled = true; btn.textContent = "⏳"; }
    try {
      await this._hass.fetchWithAuth("/api/peak_guard/force_check", { method: "POST" });
      // Reset countdown: volgende check over ~1s
      this._nextCheckAt = new Date(Date.now() + 2000);
      // Herlaal data na 2s zodat de nieuwe status zichtbaar is
      setTimeout(() => this._fetchData(), 2000);
    } catch(e) {
      console.error("Peak Guard force check fout:", e);
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = "▶ Nu controleren"; }
    }
  }

  async _saveDevices(cascadeType, devices, closeModal = false) {
    try {
      const resp = await this._hass.fetchWithAuth("/api/peak_guard/cascade", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ type: cascadeType, devices }),
      });
      if (resp.ok) {
        if (closeModal) this._closeModal();
        await this._fetchData();
      } else {
        alert(`Opslaan mislukt (HTTP ${resp.status}). Probeer opnieuw.`);
      }
    } catch (e) {
      console.error("Peak Guard: opslaan mislukt", e);
      alert("Verbindingsfout bij opslaan. Controleer de integratie.");
    }
  }

  // Stille opslag zonder modal sluiten of data herladen (gebruikt voor EV-sync)
  async _saveDevicesRaw(cascadeType, devices) {
    try {
      const resp = await this._hass.fetchWithAuth("/api/peak_guard/cascade", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ type: cascadeType, devices }),
      });
      if (!resp.ok) {
        console.warn(`Peak Guard: stille sync naar '${cascadeType}' mislukt (HTTP ${resp.status})`);
      }
    } catch (e) {
      console.error("Peak Guard: stille sync mislukt", e);
    }
  }

  // ------------------------------------------------------------------ //
  //  Live status update (zonder volledige re-render)                     //
  // ------------------------------------------------------------------ //

  _updateLiveStatus() {
    if (!this._data?.config) return;
    const { consumption_sensor, peak_sensor } = this._data.config;

    const getVal = (id) => {
      if (!id) return null;
      const s = this._hass.states[id];
      if (!s) return null;
      const v = parseFloat(s.state);
      return isNaN(v) ? null : v;
    };

    const consumption = getVal(consumption_sensor);
    const peak = getVal(peak_sensor);
    const isInjecting = consumption != null && consumption < 0;
    const injectionValue = isInjecting ? Math.abs(consumption) : 0;
    const overPeak = consumption != null && peak != null && consumption >= peak;

    const setTextAndClass = (selector, text, cls) => {
      const el = this.shadowRoot.querySelector(selector);
      if (!el) return;
      el.textContent = text;
      if (cls !== undefined) el.className = `value ${cls}`;
    };
    setTextAndClass(
      "#status-consumption",
      consumption != null ? `${consumption.toFixed(0)} W` : "—",
      overPeak ? "warning" : "ok"
    );
    setTextAndClass("#status-peak", peak != null ? `${peak.toFixed(0)} W` : "—");
    setTextAndClass(
      "#status-injection",
      `${injectionValue.toFixed(0)} W`,
      isInjecting ? "warning" : "ok"
    );

    // Update live statusbadges en EV-details van alle apparaten in beide cascades
    ["peak", "inject"].forEach((cascadeType) => {
      const devices = this._data?.[cascadeType] || [];
      devices.forEach((device, index) => {
        // Status badge (aan/uit/laden/gestopt)
        const el = this.shadowRoot.querySelector(`#device-status-${cascadeType}-${index}`);
        if (el) {
          const { text, cls } = this._deviceStatus(device);
          el.textContent = text;
          el.className = `device-status ${cls}`;
        }
        // EV live-detail (actuele A en W onder de kaart)
        const evEl = this.shadowRoot.querySelector(`#ev-live-${cascadeType}-${index}`);
        if (evEl) {
          evEl.textContent = this._evLiveDetail(device);
        }
        // EV stap-indicator op de tegel
        if (device.action_type === "ev_charger") {
          const wrapEl  = this.shadowRoot.querySelector(`#ev-debounce-${cascadeType}-${index}`);
          const fillEl  = this.shadowRoot.querySelector(`#ev-debounce-fill-${cascadeType}-${index}`);
          const labelEl = this.shadowRoot.querySelector(`#ev-debounce-label-${cascadeType}-${index}`);
          if (wrapEl && labelEl) {
            const guard = this._data?.status?.ev_guards?.[device.id];
            const stepInfo = this._evTileStepLabel(guard, device, cascadeType);
            if (stepInfo) {
              wrapEl.style.display = "";
              labelEl.textContent = stepInfo.label;
              if (fillEl) {
                if (stepInfo.pct != null) {
                  fillEl.style.display = "";
                  fillEl.style.width = `${stepInfo.pct}%`;
                } else {
                  fillEl.style.display = "none";
                }
              }
            } else {
              wrapEl.style.display = "none";
            }
          }
        }
        // EV SoC chips live bijwerken
        if (device.action_type === "ev_charger" && this._hass) {
          // Chip 1: huidig max laadniveau via ev_soc_entity
          const limEl = this.shadowRoot.querySelector(`#ev-soc-lim-${cascadeType}-${index}`);
          if (limEl && device.ev_soc_entity) {
            const s = this._hass.states[device.ev_soc_entity];
            if (s && s.state !== "unavailable" && s.state !== "unknown") {
              const v = parseFloat(s.state);
              if (!isNaN(v)) limEl.textContent = `⬆ ${v}%`;
            }
          }
          // Chip 2: huidig batterijniveau via ev_battery_entity
          const batEl = this.shadowRoot.querySelector(`#ev-soc-bat-${cascadeType}-${index}`);
          if (batEl && device.ev_battery_entity) {
            const s = this._hass.states[device.ev_battery_entity];
            if (s && s.state !== "unavailable" && s.state !== "unknown") {
              const v = parseFloat(s.state);
              if (!isNaN(v)) batEl.textContent = `🔋 ${v}%`;
            }
          }
          // Chip 3: doel bij zon is statisch (ev_max_soc), geen live update nodig
        }
      });
    });
  }

  // ------------------------------------------------------------------ //
  //  Hoofd render  — raakt de modal-node NOOIT aan                       //
  // ------------------------------------------------------------------ //

  _render() {
    this._log("_render() aangeroepen. modalVisible=" + this._modalVisible);
    if (!this._data) return;

    // Bewaar de modal-node zodat innerHTML hem niet vernietigt
    const savedModal = this._modalEl;
    if (savedModal && savedModal.parentNode === this.shadowRoot) {
      this.shadowRoot.removeChild(savedModal);
    }

    this.shadowRoot.innerHTML = `
      ${this._styles()}
      <div class="container">
        <header class="page-header">
          <div class="title-row">
            <span class="logo">⚡</span>
            <h1>Peak Guard</h1>
          </div>
          <div class="header-actions">
            <span class="badge ${this._data.status?.monitoring ? "active" : "inactive"}">
              <span class="dot"></span>
              ${this._data.status?.monitoring ? "Actief" : "Inactief"}
            </span>
            <div class="countdown-wrap" title="Tijd tot de volgende cascade-check">
              <div class="countdown-label">Volgende check: <span id="countdown-val">—</span></div>
              <div class="countdown-track"><div id="countdown-bar" class="countdown-bar" style="width:0%"></div></div>
            </div>
            <button class="btn btn-secondary btn-force" id="btn-force-check"
              title="Voer direct een nieuwe cascade-check uit (reset countdown)">
              ▶ Nu controleren
            </button>
            <button class="btn-icon" id="btn-refresh" title="GUI verversen">🔄</button>
          </div>
        </header>

        ${this._renderStatusCards()}

        ${this._renderWarnings()}

        <nav class="tabs">
          <button class="tab ${this._activeTab === "peak" ? "active" : ""}" data-tab="peak">
            ⚡ Piekstroom vermijden
          </button>
          <button class="tab ${this._activeTab === "inject" ? "active" : ""}" data-tab="inject">
            ☀️ Stroominjectie vermijden
          </button>
          <button class="tab ${this._activeTab === "savings" ? "active" : ""}" data-tab="savings">
            💰 Besparingen & Overzicht
          </button>
        </nav>

        ${this._activeTab === "savings"
          ? this._renderSavingsPanel()
          : this._renderCascadePanel(this._activeTab)}
      </div>
    `;

    // Modal-node terugplaatsen als die zichtbaar is
    if (savedModal && this._modalVisible) {
      this.shadowRoot.appendChild(savedModal);
    }

    this._attachMainEvents();
  }

  // ------------------------------------------------------------------ //
  //  Waarschuwingspaneel                                                 //
  // ------------------------------------------------------------------ //

  _renderWarnings() {
    const warnings = this._data?.status?.warnings || [];
    if (!warnings.length) return '';
    const items = [...warnings].reverse().map(w => {
      const ts = new Date(w.ts);
      const timeStr = ts.toLocaleTimeString('nl-BE', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      return `<div class="warning-item">
        <span class="warning-ts">${timeStr}</span>
        <span class="warning-msg">${w.message.replace(/</g,'&lt;').replace(/>/g,'&gt;')}</span>
      </div>`;
    }).join('');
    return `
      <div class="warning-panel">
        <div class="warning-panel-header">
          ⚠ ${warnings.length} recente waarschuwing${warnings.length !== 1 ? 'en' : ''}
        </div>
        <div class="warning-list">${items}</div>
      </div>`;
  }

  // ------------------------------------------------------------------ //
  //  Status kaarten                                                      //
  // ------------------------------------------------------------------ //

  _renderStatusCards() {
    const cfg = this._data?.config || {};
    const getVal = (id) => {
      if (!id || !this._hass) return null;
      const s = this._hass.states[id];
      if (!s) return null;
      const v = parseFloat(s.state);
      return isNaN(v) ? null : v;
    };

    const consumption = getVal(cfg.consumption_sensor);
    const peak = getVal(cfg.peak_sensor);
    const isInjecting = consumption != null && consumption < 0;
    const injectionValue = isInjecting ? Math.abs(consumption) : 0;
    const overPeak = consumption != null && peak != null && consumption > 0 && consumption >= peak;

    return `
      <div class="status-row">
        <div class="status-card">
          <div class="label">Huidig verbruik</div>
          <div class="value ${overPeak ? "warning" : "ok"}" id="status-consumption">
            ${consumption != null ? `${consumption.toFixed(0)} W` : "—"}
          </div>
        </div>
        <div class="status-card">
          <div class="label">Maandpiek</div>
          <div class="value" id="status-peak">
            ${peak != null ? `${peak.toFixed(0)} W` : "—"}
          </div>
        </div>
        <div class="status-card">
          <div class="label">Teruglevering</div>
          <div class="value ${isInjecting ? "warning" : "ok"}" id="status-injection">
            ${injectionValue.toFixed(0)} W
          </div>
        </div>
      </div>
    `;
  }

  // ------------------------------------------------------------------ //
  //  Cascade paneel                                                      //
  // ------------------------------------------------------------------ //

  _renderCascadePanel(type) {
    const devices = this._data?.[type] || [];
    const isPeak = type === "peak";

    return `
      <div class="panel">
        <div class="panel-header">
          <div>
            <div class="panel-title">
              ${isPeak ? "Cascade Piekstroom" : "Cascade Injectiepreventie"}
            </div>
            <div class="panel-desc">
              ${
                isPeak
                  ? "Apparaten worden in volgorde uitgeschakeld of teruggeschroefd wanneer het verbruik de maandpiek dreigt te overschrijden."
                  : "Apparaten worden in volgorde ingeschakeld of opgeschroefd zodra er stroom wordt teruggeleverd aan het net. Er is geen globale watt-buffer: de cascade start bij elke injectie. EV-laders hebben een eigen start-drempel (standaard 230 W ≈ 1 A) om onnodig starten en stoppen te voorkomen."
              }
            </div>
          </div>
          <button class="btn btn-primary" data-action="add" data-type="${type}">
            + Toevoegen
          </button>
        </div>

        <div class="device-list">
          ${
            devices.length === 0
              ? `<div class="empty-state">
                  <div class="emoji">${isPeak ? "⚡" : "☀️"}</div>
                  <div>Geen apparaten geconfigureerd.</div>
                  <div class="sub">Klik op "+ Toevoegen" om te beginnen.</div>
                </div>`
              : devices.map((d, i) => this._renderDeviceCard(d, i, devices.length, type)).join("")
          }
        </div>
      </div>
    `;
  }

  _renderDeviceCard(device, index, total, type) {
    const labels = {
      switch_off:  "Uitschakelen",
      switch_on:   "Inschakelen",
      throttle:    "Vermogen verminderen",
      ev_charger:  "EV Charger",
    };

    const { text: statusText, cls: statusCls } = this._deviceStatus(device);
    // Unieke ID voor de live status badge zodat _updateLiveStatus hem kan vinden
    const statusId = `device-status-${type}-${index}`;

    // Vermogenschip: voor EV dynamisch berekend, voor andere types opgeslagen power_watts
    let powerChip;
    if (device.action_type === "ev_charger") {
      const phases    = device.ev_phases || 1;
      const maxA      = device.max_value ?? 32;
      const voltage   = this._evVoltage(phases);
      const calcW     = Math.round(maxA * voltage);
      const phaseLabel = phases === 1 ? "1-fase (230 V)" : "3-fasen (400 V)";
      powerChip = `<span class="chip" title="Maximum laadstroom: ${maxA} A — Maximum vermogen: ${calcW} W (${phaseLabel})">Max ${maxA} A · ${calcW} W · ${phases}F</span>`;
    } else {
      powerChip = device.power_watts
        ? `<span class="chip" title="Nominaal opgenomen vermogen van dit apparaat. Gebruikt voor de besparingsberekening.">${device.power_watts} W</span>`
        : "";
    }

    // SoC chips — drie afzonderlijke waarden, elk met eigen live-update ID en tooltip
    let socChips = "";
    if (device.action_type === "ev_charger") {

      // Chip 1: Huidig maximaal laadniveau (live via ev_soc_entity)
      if (device.ev_soc_entity) {
        let limDisplay = "—";
        if (this._hass) {
          const s = this._hass.states[device.ev_soc_entity];
          if (s && s.state !== "unavailable" && s.state !== "unknown") {
            const v = parseFloat(s.state);
            if (!isNaN(v)) limDisplay = `${v}%`;
          }
        }
        socChips += `<span id="ev-soc-lim-${type}-${index}" class="chip chip-soc-lim"
          title="Huidig ingesteld maximaal laadniveau van de batterij. Dit is de actieve limiet op dit moment — Peak Guard past deze waarde tijdelijk aan bij zonne-overschot en herstelt ze nadien automatisch."
          >⬆ ${limDisplay}</span>`;
      }

      // Chip 2: Huidig batterijniveau (live via ev_battery_entity)
      if (device.ev_battery_entity) {
        let batDisplay = "—";
        if (this._hass) {
          const s = this._hass.states[device.ev_battery_entity];
          if (s && s.state !== "unavailable" && s.state !== "unknown") {
            const v = parseFloat(s.state);
            if (!isNaN(v)) batDisplay = `${v}%`;
          }
        }
        socChips += `<span id="ev-soc-bat-${type}-${index}" class="chip chip-soc-bat"
          title="Huidig batterijniveau van het voertuig (actuele laadtoestand). Enkel weergave — Peak Guard schrijft nooit naar deze sensor."
          >🔋 ${batDisplay}</span>`;
      }

      // Chip 3: Gewenste max SoC bij zonne-overschot (statisch, uit configuratie)
      if (device.ev_max_soc != null) {
        const hasEntity = !!device.ev_soc_entity;
        socChips += `<span class="chip chip-soc-target"
          title="Maximaal gewenst laadniveau bij overtollige zonne-energie. Peak Guard stelt de batterijlimiet tijdelijk in op dit percentage wanneer er zonne-overschot is.${hasEntity ? "" : " Koppel een SoC-limiet entiteit om dit automatisch te laten werken."}"
          >☀ ${device.ev_max_soc}%</span>`;
      }
    }

    const actionTitles = {
      switch_off:  "Dit apparaat wordt tijdelijk uitgeschakeld wanneer het verbruik de maandpiek dreigt te overschrijden.",
      switch_on:   "Dit apparaat wordt ingeschakeld bij overtollige zonne-energie om de injectie naar het net te beperken.",
      throttle:    "Het vermogen van dit apparaat wordt verminderd via een instelbare number-entiteit (legacy modus).",
      ev_charger:  "Elektrisch voertuig met variabele laadstroom. Peak Guard past de laadsnelheid aan op basis van beschikbaar vermogen.",
    };

    return `
      <div class="device-card">
        <div class="order-col">
          <button class="btn-order" data-action="up" data-index="${index}" data-type="${type}"
            ${index === 0 ? "disabled" : ""}>▲</button>
          <div class="priority" title="Prioriteit in de cascade: apparaten met lagere nummers worden eerst ingeschakeld of uitgeschakeld.">${index + 1}</div>
          <button class="btn-order" data-action="down" data-index="${index}" data-type="${type}"
            ${index === total - 1 ? "disabled" : ""}>▼</button>
        </div>
        <div class="device-info">
          <div class="device-name-row">
            <span class="device-name">${this._esc(device.name)}</span>
            <span id="${statusId}" class="device-status ${statusCls}" title="Huidige status van dit apparaat in Home Assistant.">${statusText}</span>
          </div>
          <div class="device-entity">${this._esc(device.entity_id)}</div>
          <div class="chips">
            <span class="chip action" title="${actionTitles[device.action_type] || ""}">${labels[device.action_type] || device.action_type}</span>
            ${powerChip}
            ${socChips}
            ${!device.enabled ? `<span class="chip disabled" title="Dit apparaat is uitgeschakeld en wordt door Peak Guard genegeerd.">Uitgeschakeld</span>` : ""}
          </div>
          ${device.action_type === "ev_charger"
            ? `<div id="ev-live-${type}-${index}" class="ev-live-status"></div>
               <div id="ev-debounce-${type}-${index}" class="ev-debounce-bar-wrap" style="display:none;">
                 <div id="ev-debounce-label-${type}-${index}" class="ev-debounce-label"></div>
                 <div class="ev-debounce-track"><div id="ev-debounce-fill-${type}-${index}" class="ev-debounce-fill" style="width:0%"></div></div>
               </div>`
            : ""}
          ${device.action_type === "ev_charger" && type === "inject" && !device.ev_location_tracker
            ? `<div class="ev-location-warning">
                 <span>Geen locatie-tracker ingesteld. Peak Guard kan niet controleren of de wagen thuis is.</span>
                 <button class="btn-inline-warning" data-action="configure-location" data-index="${index}" data-type="${type}">Configureren</button>
               </div>`
            : ""}
          ${this._renderDeviceControls(device, index, type)}
        </div>
        <div class="device-actions">
          <button class="btn-icon" data-action="info"
            data-index="${index}" data-type="${type}" title="Status-info">ℹ️</button>
          <button class="btn-icon" data-action="edit"
            data-index="${index}" data-type="${type}" title="Bewerken">✏️</button>
          <button class="btn-icon" data-action="delete"
            data-index="${index}" data-type="${type}" title="Verwijderen">🗑️</button>
        </div>
      </div>
    `;
  }

  // Toont een info-popup voor een apparaat met originele/aangepaste status en uitleg.
  _showInfoModal(device, cascadeType) {
    const isEV  = device.action_type === "ev_charger";
    const isPeak = cascadeType === "peak";

    // Zoek snapshot voor dit apparaat (entity_id als sleutel)
    const snaps     = this._data?.snapshots || {};
    const peakSnap  = snaps.peak?.[device.entity_id];
    const injectSnap= snaps.inject?.[device.entity_id];
    const snap      = isPeak ? peakSnap : injectSnap;

    // Huidige staat van de primaire entity
    const entityId  = (isEV && device.ev_switch_entity) ? device.ev_switch_entity : device.entity_id;
    const liveState = this._hass?.states[entityId];
    const liveVal   = liveState?.state ?? "—";

    // Labels
    const stateLabel = (s) => {
      if (s === "on")  return "Aan ✅";
      if (s === "off") return "Uit ⭕";
      return s ?? "—";
    };

    // Originele waarden vóór PG-ingreep (uit snapshot)
    const origCurrent = snap?.original_current != null ? `${snap.original_current} A` : null;
    const origSoc     = snap?.original_soc     != null ? `${snap.original_soc}%`      : null;

    // Aangepaste waarde = huidige live staat
    const modState   = stateLabel(liveVal);
    const curEntityId= isEV ? device.ev_current_entity : null;
    const curState   = curEntityId ? this._hass?.states[curEntityId] : null;
    const modCurrent = curState && curState.state !== "unavailable"
      ? `${parseFloat(curState.state)} A` : null;

    // Uitleg wanneer wordt teruggezet
    let restoreDesc = "";
    if (!snap) {
      restoreDesc = "Peak Guard heeft dit apparaat nog niet aangepast. "
        + "Er is geen actieve ingreep.";
    } else if (isPeak) {
      restoreDesc = `Dit apparaat werd uitgeschakeld omdat het verbruik de maandpiek dreigde te overschrijden. `
        + `Het wordt automatisch terug ingeschakeld zodra het verbruik voldoende onder de piekgrens daalt `
        + `(headroom > ${device.power_watts ?? 0} W) en er geen piekrisico meer is.`;
    } else {
      if (isEV) {
        restoreDesc = `De EV-lader werd gestart op zonne-overschot. `
          + `Het laden stopt automatisch wanneer het zonne-overschot wegvalt `
          + `(netto verbruik ≥ 0 W) of wanneer het surplus lager wordt dan het EV-verbruik.`;
      } else {
        restoreDesc = `Dit apparaat werd ingeschakeld op zonne-overschot. `
          + `Het wordt automatisch uitgeschakeld zodra het netto verbruik niet meer negatief is `
          + `(geen export naar het net meer).`;
      }
    }

    // Bouw tabel-rijen
    const row = (label, huidig, origineel) => `
      <tr>
        <td class="info-label">${label}</td>
        <td class="info-orig">${huidig}</td>
        <td class="info-mod">${origineel}</td>
      </tr>`;

    // "Origineel" = waarde vóór PG-ingreep; als er geen snap is, toon de live waarde (ongewijzigd)
    const origStateFmt   = snap ? stateLabel(snap.original_state) : stateLabel(liveVal);
    const origCurrentFmt = origCurrent ?? modCurrent ?? "—";
    const origSocFmt     = origSoc ?? (() => {
      if (!device.ev_soc_entity) return null;
      const s = this._hass?.states[device.ev_soc_entity];
      return (s && s.state !== "unavailable" && s.state !== "unknown")
        ? `${parseFloat(s.state)}%` : "—";
    })();

    let rows = row("Schakelaar", modState, origStateFmt);
    if (isEV && device.ev_current_entity) {
      rows += row("Laadstroom", modCurrent ?? "—", origCurrentFmt);
    }
    if (isEV && device.ev_soc_entity) {
      const socState  = this._hass?.states[device.ev_soc_entity];
      const curSocVal = (socState && socState.state !== "unavailable" && socState.state !== "unknown")
        ? `${parseFloat(socState.state)}%` : "—";
      rows += row("Laadlimiet", curSocVal, origSocFmt ?? "—");
    }

    // EV evaluatie-checklist (enkel solar-cascade, enkel EV)
    const guard = this._data?.status?.ev_guards?.[device.id];
    const evalChecklist = (isEV && !isPeak)
      ? this._evEvalChecklist(device, guard)
      : "";

    const activeLabel = snap
      ? `<span class="info-active-badge">Ingreep actief</span>`
      : `<span class="info-inactive-badge">Geen ingreep</span>`;

    if (!this._modalEl) {
      this._modalEl = document.createElement("div");
      Object.assign(this._modalEl.style, {
        position: "fixed", inset: "0", zIndex: "999",
        background: "rgba(0,0,0,.45)",
        display: "flex", alignItems: "center",
        justifyContent: "center", padding: "16px",
      });
    }
    // Altijd terughangen: na een re-render kan de node uit de DOM zijn gevallen
    if (!this._modalEl.isConnected) {
      this.shadowRoot.appendChild(this._modalEl);
    }
    this._modalVisible = true;
    this._modalEl.style.display = "flex";

    this._modalEl.innerHTML = `
      <div class="modal" id="modal-box" style="max-width:520px;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
          <h3 style="margin:0;">${this._esc(device.name)}</h3>
          ${activeLabel}
        </div>
        <div class="modal-subtitle">${isPeak ? "Piek-cascade" : "Solar-cascade"} · ${device.action_type}</div>
        <table class="info-table">
          <thead>
            <tr>
              <th></th>
              <th>Huidig</th>
              <th>Origineel</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
        ${evalChecklist}
        <div class="info-restore-box" style="margin-top:14px;">
          <div class="info-restore-title">↩ Wanneer wordt hersteld?</div>
          <div class="info-restore-text">${restoreDesc}</div>
        </div>
        <div class="modal-actions" style="justify-content:flex-end;">
          <button class="btn btn-secondary" id="info-close">Sluiten</button>
        </div>
      </div>`;

    this._modalEl.querySelector("#info-close")?.addEventListener("click", () => {
      this._closeModal();
    });
    this._modalEl.addEventListener("click", (e) => {
      if (e.target === this._modalEl) this._closeModal();
    });
  }

  // Rendert inline bedieningselementen voor een apparaat in de kaart.
  _renderDeviceControls(device, index, type) {
    if (!this._hass) return "";

    const isEV = device.action_type === "ev_charger";
    const entityId = (isEV && device.ev_switch_entity)
      ? device.ev_switch_entity : device.entity_id;
    const state = this._hass.states[entityId];
    if (!state || state.state === "unavailable" || state.state === "unknown") return "";

    const isOn = state.state === "on";
    const toggleLabel = isOn
      ? (isEV ? "⏹ Stop laden" : "Uitschakelen")
      : (isEV ? "▶ Start laden" : "Inschakelen");
    const toggleCls = isOn ? "on" : "off";

    let ampereControl = "";
    if (isEV && device.ev_current_entity) {
      const curState = this._hass.states[device.ev_current_entity];
      const curA = curState && curState.state !== "unavailable"
        ? parseFloat(curState.state) : null;
      const minA = device.ev_min_current ?? device.min_value ?? 6;
      const maxA = device.max_value ?? 32;
      const valA = (!isNaN(curA) ? curA : minA);
      ampereControl = `
        <div class="ampere-control" data-ev-current="${this._esc(device.ev_current_entity)}">
          <label>⚡</label>
          <input type="range" class="ampere-slider"
            min="${minA}" max="${maxA}" step="1" value="${valA}"
            data-action="set-ampere"
            data-entity="${this._esc(device.ev_current_entity)}"
            data-index="${index}" data-type="${type}"
            title="Laadstroom instellen (${minA}–${maxA} A)" />
          <span class="ampere-value" id="ampere-val-${type}-${index}">${valA} A</span>
        </div>`;
    }

    return `
      <div class="device-controls">
        <button class="btn-toggle ${toggleCls}"
          data-action="toggle"
          data-entity="${this._esc(entityId)}"
          data-state="${state.state}"
          data-index="${index}" data-type="${type}"
          title="${isOn ? "Klik om uit te schakelen" : "Klik om in te schakelen"}">
          ${toggleLabel}
        </button>
        ${ampereControl}
      </div>`;
  }

  // Geeft de live statustext en CSS-klasse terug voor een apparaat
  _deviceStatus(device) {
    if (!this._hass) return { text: "—", cls: "status-unknown" };

    // Voor EV: gebruik de schakelaar-entity (ev_switch_entity), niet de primaire entity_id
    const entityId = (device.action_type === "ev_charger" && device.ev_switch_entity)
      ? device.ev_switch_entity
      : device.entity_id;

    const state = this._hass.states[entityId];
    if (!state || state.state === "unavailable" || state.state === "unknown" || state.state === "") {
      return { text: "onbeschikbaar", cls: "status-unknown" };
    }

    if (device.action_type === "ev_charger") {
      // Guard-state heeft voorrang: PG kan al laden terwijl de HA-schakelaar nog "off" rapporteert
      // (bijv. Tesla integration lag na SOC-override + turn_on).
      const guardCharging = this._data?.status?.ev_guards?.[device.id]?.state === "charging";
      if (guardCharging) return { text: "laden", cls: "status-on" };

      if (state.state === "off") {
        const cableEntity = device.ev_cable_entity || "sensor.tesla_opladen";
        const cableState  = this._hass?.states[cableEntity];
        const cs = cableState?.state?.toLowerCase().trim();
        const CABLE_ON = new Set(["on", "true", "connected", "charging",
                                  "complete", "fully_charged", "pending", "1"]);
        let cableConnected = true;
        if (cs && cs !== "unavailable" && cs !== "unknown" && cs !== "") {
          const num = parseFloat(cs);
          cableConnected = CABLE_ON.has(cs) || (!isNaN(num) && num > 0);
        }
        if (!cableConnected) return { text: "kabel los", cls: "status-cable-off" };
        return { text: "gestopt", cls: "status-off" };
      }
      return { text: "laden", cls: "status-on" };
    }
    if (device.action_type === "throttle") {
      const val = parseFloat(state.state);
      const unit = state.attributes?.unit_of_measurement || "A";
      const display = isNaN(val) ? state.state : `${val} ${unit}`;
      return { text: display, cls: "status-throttle" };
    }
    // switch_on / switch_off
    if (state.state === "on") return { text: "aan", cls: "status-on" };
    if (state.state === "off") return { text: "uit", cls: "status-off" };
    return { text: state.state, cls: "status-unknown" };
  }

  // Bouwt de live EV-detailregel: actuele A, berekend W, en huidige SoC-limiet
  _evLiveDetail(device) {
    if (!this._hass || device.action_type !== "ev_charger") return "";

    const swEntity = device.ev_switch_entity || device.entity_id;
    const swState  = this._hass.states[swEntity];
    const guardCharging = this._data?.status?.ev_guards?.[device.id]?.state === "charging";
    if (!swState || (swState.state === "off" && !guardCharging)) return "";

    const parts = [];

    // Actuele laadstroom
    const curEntity = device.ev_current_entity;
    if (curEntity) {
      const curState = this._hass.states[curEntity];
      if (curState && curState.state !== "unavailable" && curState.state !== "unknown") {
        const currentA = parseFloat(curState.state);
        if (!isNaN(currentA)) {
          const phases   = device.ev_phases || 1;
          const voltage  = this._evVoltage(phases);
          const currentW = Math.round(currentA * voltage);
          parts.push(`${currentA} A · ${currentW} W`);
        }
      }
    }

    // Huidige SoC-limiet (de ingestelde limietwaarde, niet de actuele SoC)
    const socEntity = device.ev_soc_entity;
    if (socEntity) {
      const socState = this._hass.states[socEntity];
      if (socState && socState.state !== "unavailable" && socState.state !== "unknown") {
        const soc = parseFloat(socState.state);
        if (!isNaN(soc)) parts.push(`limiet: ${soc}%`);
      }
    }

    return parts.join(" · ");
  }

  // Geeft {label, pct} terug voor de stap-indicator op de EV-tegel, of null als er niets te tonen is.
  _evTileStepLabel(guard, device, cascadeType) {
    if (!guard || cascadeType !== "inject") return null;
    const state = guard.state;
    const skip  = guard.skip_reason ?? "";

    if (state === "charging") return null;

    if (state === "sleeping") {
      const elapsed = Math.round(guard.wake_elapsed_s ?? 0);
      const pct = Math.min(100, Math.round(elapsed / 15 * 100));
      return { label: `💤 Tesla aan het wekken… ${elapsed}s`, pct };
    }
    if (state === "waiting_for_stable_surplus") {
      const secs = Math.round(guard.history_secs ?? 0);
      const pct  = Math.min(100, Math.round(secs / 20 * 100));
      const target = guard.pending_amps != null ? ` → ${guard.pending_amps} A` : "";
      return { label: `☀️ Overschot aan het meten… ${secs}/20s${target}`, pct };
    }
    if (state === "cable_disconnected" || skip.includes("kabel")) {
      return { label: "🔌 Laadkabel niet aangesloten — wachten tot die erin gaat", pct: null };
    }
    if (skip.includes("niet thuis") || skip.includes("EV niet thuis")) {
      return { label: "🏠 Wagen is niet thuis", pct: null };
    }
    if (skip.includes("min OFF")) {
      const rem = Math.ceil(guard.min_off_remaining_s ?? 0);
      return { label: `⏱️ Even afkoelen na de vorige stop… nog ${rem}s`, pct: null };
    }
    if (skip.includes("start-drempel") || skip.includes("surplus")) {
      return { label: "⏳ Wachten op genoeg zon… overschot is nog te klein", pct: null };
    }
    if (skip.includes("niet wakker")) {
      return { label: "💤 Tesla reageert niet — volgende cyclus opnieuw proberen", pct: null };
    }
    return null;
  }

  // Bouwt de evaluatie-checklist voor een EV in de solar-cascade.
  _evEvalChecklist(device, guard) {
    if (!guard) {
      return `<div class="ev-checklist">
        <div class="ev-checklist-title">🔍 Evaluatiestappen</div>
        <div style="font-size:.85em;color:var(--secondary-text-color,#888);">
          Nog geen evaluatiedata — Peak Guard heeft dit apparaat nog niet beoordeeld.
        </div>
      </div>`;
    }

    // Haal surpluswaarde op via de consumptiesensor
    const consumptionSensor = this._data?.config?.consumption_sensor;
    const consumptionState  = consumptionSensor ? this._hass?.states[consumptionSensor] : null;
    const consumption       = consumptionState ? parseFloat(consumptionState.state) : 0;
    const surplus_w         = (consumption < 0) ? Math.abs(consumption) : 0;

    const state  = guard.state ?? "idle";
    const skip   = guard.skip_reason ?? "";
    const startThr = parseFloat(device.start_threshold_w ?? 230);

    // Bepaal welke stap momenteel blokkeert
    let blockingStep = null;
    let blockingMode = "active";  // 'active' = wachten/bezig, 'blocked' = mislukt
    if (state === "charging") {
      blockingStep = "done_all";
    } else if (state === "cable_disconnected" || skip.includes("kabel")) {
      blockingStep = "cable"; blockingMode = "blocked";
    } else if (skip.includes("start-drempel") || skip.includes("surplus")) {
      blockingStep = "surplus"; blockingMode = "blocked";
    } else if (skip.includes("niet thuis") || skip.includes("EV niet thuis")) {
      blockingStep = "location"; blockingMode = "blocked";
    } else if (skip.includes("min OFF")) {
      blockingStep = "min_off"; blockingMode = "active";
    } else if (state === "waiting_for_stable_surplus") {
      blockingStep = "debounce"; blockingMode = "active";
    } else if (state === "sleeping") {
      blockingStep = "wake"; blockingMode = "active";
    } else if (skip.includes("niet wakker")) {
      blockingStep = "wake"; blockingMode = "blocked";
    } else if (surplus_w <= 0) {
      blockingStep = "surplus"; blockingMode = "blocked";
    }

    const STEP_ORDER = ["cable", "surplus", "location", "min_off", "debounce", "wake", "start"];

    const blockIdx = blockingStep === "done_all"
      ? STEP_ORDER.length
      : STEP_ORDER.indexOf(blockingStep);

    const stepStatus = (id) => {
      const idx = STEP_ORDER.indexOf(id);
      if (blockingStep === "done_all") return "done";
      if (idx < blockIdx) return "done";
      if (idx === blockIdx) return blockingMode === "blocked" ? "blocked" : "active";
      return "pending";
    };

    const ICONS = {
      done: "✅", active: "⏳", blocked: "❌", pending: "⬜",
    };

    const step = (id, label, detail = "", barPct = null) => {
      const status = stepStatus(id);
      const icon   = ICONS[status];
      const barHtml = (status === "active" && barPct != null)
        ? `<div class="ev-step-bar-wrap"><div class="ev-step-bar-fill" style="width:${barPct}%"></div></div>`
        : "";
      const detailHtml = detail
        ? `<div class="ev-step-detail">${detail}</div>` : "";
      return `
        <div class="ev-checklist-step ${status}">
          <span class="ev-step-icon">${icon}</span>
          <div class="ev-step-body">
            <div class="ev-step-label">${label}</div>
            ${barHtml}${detailHtml}
          </div>
        </div>`;
    };

    let html = `<div class="ev-checklist"><div class="ev-checklist-title">🔍 Evaluatiestappen</div>`;

    // Stap: kabel (alleen als geconfigureerd)
    if (device.ev_cable_entity) {
      const cableState = this._hass?.states[device.ev_cable_entity];
      const cableVal   = cableState?.state ?? "onbekend";
      html += step("cable", "Laadkabel aangesloten", `entity: ${cableVal}`);
    }

    // Stap: surplus
    const surplusDetail = surplus_w > 0
      ? `${surplus_w.toFixed(0)} W ≥ drempel ${startThr.toFixed(0)} W`
      : `geen overschot (drempel: ${startThr.toFixed(0)} W)`;
    html += step("surplus", "Voldoende zonne-overschot", surplusDetail);

    // Stap: locatie (alleen als geconfigureerd)
    if (device.ev_location_tracker) {
      const locState = this._hass?.states[device.ev_location_tracker];
      const locVal   = locState?.state ?? "onbekend";
      html += step("location", "Wagen thuis", `tracker: ${locVal}`);
    }

    // Stap: min OFF (alleen als PG eerder uitschakelde)
    if (guard.turned_off_by_pg || (guard.min_off_remaining_s ?? 0) > 0) {
      const rem  = Math.ceil(guard.min_off_remaining_s ?? 0);
      const det  = rem > 0 ? `nog ${rem}s wachten` : "wachttijd verstreken";
      html += step("min_off", "Wachttijd na vorige stop", det);
    }

    // Stap: debounce
    const histSecs = Math.round(guard.history_secs ?? 0);
    const histPct  = Math.min(100, Math.round(histSecs / 20 * 100));
    const debDet   = state === "waiting_for_stable_surplus"
      ? `${histSecs}/20s opgebouwd`
      : (stepStatus("debounce") === "done" ? "stabiel ✓" : "");
    html += step("debounce", "Overschot stabiel (20s meting)", debDet,
      state === "waiting_for_stable_surplus" ? histPct : null);

    // Stap: wake-up (alleen als geconfigureerd)
    if (device.ev_wake_button) {
      const wakeEl = Math.round(guard.wake_elapsed_s ?? 0);
      const wakeDet = state === "sleeping"
        ? `wekken… ${wakeEl}s / 15s`
        : (stepStatus("wake") === "done" ? "wakker ✓" : "");
      html += step("wake", "Tesla wakker maken", wakeDet,
        state === "sleeping" ? Math.min(100, Math.round(wakeEl / 15 * 100)) : null);
    }

    // Stap: laden starten
    const startDet = state === "charging"
      ? (guard.last_sent_amps != null ? `${guard.last_sent_amps} A` : "")
      : "";
    html += step("start", "Laden starten", startDet);

    html += `</div>`;
    return html;
  }

  // ------------------------------------------------------------------ //
  //  Modal — persistente DOM-node, nooit door _render() gewist           //
  // ------------------------------------------------------------------ //

  _openModal(device, cascadeType, startStep = 1) {
    this._log("_openModal() aangeroepen. type=" + cascadeType);
    this._editDevice = device || null;
    this._editCascadeType = cascadeType;
    this._modalVisible = true;
    this._wizardStep = startStep;

    // Maak de backdrop-node éénmalig aan
    if (!this._modalEl) {
      this._modalEl = document.createElement("div");
      Object.assign(this._modalEl.style, {
        position: "fixed",
        inset: "0",
        zIndex: "999",
        background: "rgba(0,0,0,.45)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: "16px",
      });
      this.shadowRoot.appendChild(this._modalEl);
    } else {
      this._modalEl.style.display = "flex";
    }

    const d = this._editDevice || {};
    const isEV = d.action_type === "ev_charger";
    this._evMode = isEV;

    if (isEV) {
      this._renderEVWizard(d);
    } else {
      this._renderStandardModal(d);
    }

    this._attachModalEvents();
  }

  // ------------------------------------------------------------------ //
  //  Standaard modal (switch_off / switch_on / throttle)                //
  // ------------------------------------------------------------------ //

  _renderStandardModal(d) {
    const isThrottle = d.action_type === "throttle";
    this._modalEl.innerHTML = `
      <div class="modal" id="modal-box">
        <h3>${d.id ? "Apparaat bewerken" : "Apparaat toevoegen"}</h3>
        <div class="modal-subtitle">Vul de gegevens in voor dit apparaat.</div>

        <div class="form-group">
          <label>Naam</label>
          <input id="f-name" type="text" value="${this._esc(d.name || "")}"
            placeholder="bijv. Boiler" autocomplete="off" />
          <div class="field-hint">Een herkenbare naam voor dit apparaat in de cascade-lijst.</div>
        </div>

        <div class="form-group">
          <label>Type apparaat</label>
          <select id="f-action">
            <option value="switch_off"  ${d.action_type === "switch_off"  ? "selected" : ""}>Uitschakelen (switch)</option>
            <option value="switch_on"   ${d.action_type === "switch_on"   ? "selected" : ""}>Inschakelen bij zonne-overschot (switch)</option>
            <option value="ev_charger"  ${d.action_type === "ev_charger"  ? "selected" : ""}>Elektrisch Voertuig (EV Charger)</option>
            <option value="throttle"    ${d.action_type === "throttle"    ? "selected" : ""}>Vermogen verminderen — legacy (number)</option>
          </select>
          <div class="field-hint">
            <b>Uitschakelen</b>: schakelaar die tijdelijk uitgaat bij piekdreiging.<br>
            <b>Inschakelen</b>: schakelaar die aangaat bij zonne-overschot.<br>
            <b>EV Charger</b>: laadpaal met variabele stroomsterkte.
          </div>
        </div>

        <div class="form-group">
          <label>Entity ID</label>
          <div class="entity-picker">
            <input id="f-entity" type="text" value="${this._esc(d.entity_id || "")}"
              placeholder="Zoek op naam of entity ID..." autocomplete="off" />
            <div id="entity-dropdown" class="entity-dropdown" style="display:none;"></div>
          </div>
          <div class="field-hint">De schakelaar (switch.*) of regelbare entiteit (number.*) in Home Assistant.</div>
        </div>

        <div class="form-group">
          <label>Nominaal vermogen (W)</label>
          <input id="f-power" type="number" min="0" value="${d.power_watts || 0}"
            placeholder="bijv. 2000" />
          <div class="field-hint">Gemiddeld opgenomen vermogen van dit apparaat. Gebruikt voor de besparingsberekening.</div>
        </div>

        <!-- Throttle legacy velden -->
        <div id="throttle-fields" ${isThrottle ? "" : 'style="display:none"'}>
          <div class="form-row">
            <div class="form-group">
              <label>Min. waarde</label>
              <input id="f-min" type="number" min="0" value="${d.min_value ?? 6}" />
            </div>
            <div class="form-group">
              <label>Max. waarde</label>
              <input id="f-max" type="number" min="0" value="${d.max_value ?? 32}" />
            </div>
          </div>
          <div class="form-group">
            <label>Watt per eenheid</label>
            <input id="f-ppu" type="number" min="1" value="${d.power_per_unit ?? 690}" />
            <div class="field-hint">Vermogen (W) per stap van de number-entiteit.</div>
          </div>
        </div>

        <div class="modal-actions">
          <div></div>
          <div class="modal-actions-right">
            <button class="btn btn-secondary" id="modal-cancel">Annuleren</button>
            <button class="btn btn-primary" id="modal-save">Opslaan</button>
          </div>
        </div>
      </div>
    `;
  }

  // ------------------------------------------------------------------ //
  //  EV Charger wizard (3 stappen)                                      //
  // ------------------------------------------------------------------ //

  _renderEVWizard(d) {
    const step = this._wizardStep;
    const isNew = !d.id;

    const stepLabel = ["Identificatie", "Laadconfiguratie", "Zonne & Wake-up"];
    const stepDesc  = [
      "Naam en hardware-koppeling",
      "Fasen, stroomsterkte en vermogen",
      "Batterijlimiet, wake-up en status",
    ];

    const stepsHTML = stepLabel.map((lbl, i) => {
      const n = i + 1;
      const cls = n < step ? "done" : n === step ? "active" : "";
      const dot = n < step ? "✓" : n;
      const conn = i < stepLabel.length - 1
        ? `<div class="wizard-connector ${n < step ? "done" : ""}"></div>`
        : "";
      return `
        <div class="wizard-step ${cls}">
          <div class="wizard-step-dot">${dot}</div>
          <span>${lbl}</span>
        </div>${conn}`;
    }).join("");

    let bodyHTML = "";

    if (step === 1) {
      bodyHTML = `
        <div class="form-group">
          <label>Naam</label>
          <input id="f-name" type="text" value="${this._esc(d.name || "")}"
            placeholder="bijv. Mijn Laadpaal" autocomplete="off" />
          <div class="field-hint">Een herkenbare naam voor deze laadpaal in de cascade-lijst.</div>
        </div>

        <div class="form-group">
          <label>Type apparaat</label>
          <select id="f-action">
            <option value="switch_off">Uitschakelen (switch)</option>
            <option value="switch_on">Inschakelen bij zonne-overschot (switch)</option>
            <option value="ev_charger" selected>Elektrisch Voertuig (EV Charger)</option>
            <option value="throttle">Vermogen verminderen — legacy (number)</option>
          </select>
          <div class="field-hint">Kies een ander type als dit geen laadpaal is — de wizard past zich dan aan.</div>
        </div>

        <div class="form-group">
          <label>Oplaadschakelaar</label>
          <div class="entity-picker">
            <input id="f-ev-switch" type="text"
              value="${this._esc(d.ev_switch_entity || d.entity_id || "")}"
              placeholder="switch.laadpaal_schakelaar" autocomplete="off" />
            <div id="ev-switch-dropdown" class="entity-dropdown" style="display:none;"></div>
          </div>
          <div class="field-hint">
            De schakelaar (switch.*) waarmee het opladen gestart of gestopt wordt.
            Peak Guard schakelt deze uit bij piekdreiging, en aan bij zonne-overschot.
          </div>
        </div>

        <div class="form-group">
          <label>Stroomsensor <span style="font-weight:400;text-transform:none;">(optioneel)</span></label>
          <div class="entity-picker">
            <input id="f-ev-current" type="text"
              value="${this._esc(d.ev_current_entity || "")}"
              placeholder="number.laadpaal_stroom" autocomplete="off" />
            <div id="ev-current-dropdown" class="entity-dropdown" style="display:none;"></div>
          </div>
          <div class="field-hint">
            De number-entiteit (number.*) waarmee de laadstroom in ampere instelbaar is.
            Zonder deze entiteit kan Peak Guard enkel volledig in- of uitschakelen.
          </div>
        </div>

        <div class="form-group">
          <label>Kabeldetectiesensor</label>
          <div class="entity-picker">
            <input id="f-ev-cable" type="text"
              value="${this._esc(d.ev_cable_entity != null ? d.ev_cable_entity : 'sensor.tesla_opladen')}"
              placeholder="sensor.tesla_opladen" autocomplete="off" />
            <div id="ev-cable-dropdown" class="entity-dropdown" style="display:none;"></div>
          </div>
          <div class="field-hint">
            Sensor die aangeeft of de laadkabel aangesloten is.
            Peak Guard start het opladen alleen als deze sensor <strong>aan</strong> of <strong>verbonden</strong> rapporteert.
            Standaard: <em>sensor.tesla_opladen</em>.
          </div>
        </div>
      `;
    } else if (step === 2) {
      bodyHTML = `
        <div class="form-group">
          <label>Aantal fasen</label>
          <select id="f-ev-phases">
            <option value="1" ${(d.ev_phases ?? 1) == 1 ? "selected" : ""}>1 fase — 230 V</option>
            <option value="3" ${(d.ev_phases ?? 1) == 3 ? "selected" : ""}>3 fasen — 400 V</option>
          </select>
          <div class="field-hint">
            De meeste thuisladers laden 1-fasig (230 V). Controleer het typeplaatje of de handleiding van uw laadpaal.
            Het maximale vermogen wordt automatisch berekend: 1-fase = A × 230 V, 3-fasen = A × 400 V.
          </div>
        </div>

        <div class="form-row">
          <div class="form-group">
            <label>Minimum laadstroom (A)</label>
            <input id="f-ev-min-a" type="number" min="1" max="32"
              value="${d.min_value ?? 6}" placeholder="6" />
            <div class="field-hint">
              Laagste toegelaten laadstroom. De meeste laders vereisen minimaal 6 A.
              Onder dit niveau schakelt Peak Guard de lader volledig uit.
              Peak Guard start de lader pas wanneer het zonne-overschot minstens
              de minimale laadstroom dekt (standaard 230 W), zodat de lader niet
              voortdurend aan en uit schakelt.
            </div>
          </div>
          <div class="form-group">
            <label>Maximum laadstroom (A)</label>
            <input id="f-ev-max-a" type="number" min="1" max="125"
              value="${d.max_value ?? 32}" placeholder="32" />
            <div class="field-hint">
              Maximale laadstroom die de lader ondersteunt.
              Controleer het typeplaatje van uw laadpaal.
            </div>
          </div>
        </div>

        <div id="ev-power-preview" class="field-hint" style="
          margin-top:-8px; padding:10px 12px;
          background:var(--secondary-background-color,#f5f5f5);
          border-radius:8px; font-size:.84em;
        ">
          Maximaal vermogen: wordt berekend na invullen.
        </div>
      `;
    } else if (step === 3) {
      bodyHTML = `
        <div class="form-group">
          <label>SoC-limiet entiteit <span style="font-weight:400;text-transform:none;">(optioneel)</span></label>
          <div class="entity-picker">
            <input id="f-ev-soc-entity" type="text"
              value="${this._esc(d.ev_soc_entity || "")}"
              placeholder="number.laadpaal_batterijlimiet" autocomplete="off" />
            <div id="ev-soc-entity-dropdown" class="entity-dropdown" style="display:none;"></div>
          </div>
          <div class="field-hint">
            De number-entiteit (number.*) waarmee het maximale laadpercentage van de batterij instelbaar is —
            bijvoorbeeld <em>number.mijn_auto_charge_limit</em>.
            Peak Guard leest en schrijft deze waarde automatisch bij zonne-overschot.
            Zonder deze entiteit wordt de limiet niet automatisch aangepast.
          </div>
        </div>

        <div class="form-group">
          <label>Batterijniveau-sensor <span style="font-weight:400;text-transform:none;">(optioneel)</span></label>
          <div class="entity-picker">
            <input id="f-ev-battery-entity" type="text"
              value="${this._esc(d.ev_battery_entity || "")}"
              placeholder="sensor.mijn_auto_batterij" autocomplete="off" />
            <div id="ev-battery-entity-dropdown" class="entity-dropdown" style="display:none;"></div>
          </div>
          <div class="field-hint">
            De sensor-entiteit (sensor.*) die het huidig laadniveau van de batterij toont (in %).
            Wordt enkel gebruikt voor weergave in het paneel — Peak Guard schrijft hier nooit naar.
            Voorbeeld: <em>sensor.mijn_auto_battery_level</em>.
          </div>
        </div>

        <div class="form-group">
          <label>Gewenst maximum bij zonne-overschot (%)</label>
          <input id="f-ev-soc" type="number" min="1" max="100"
            value="${d.ev_max_soc ?? 100}" placeholder="100" />
          <div class="field-hint">
            Peak Guard stelt de SoC-limiet tijdelijk in op dit percentage wanneer er overtollig zonne-energie
            beschikbaar is — zodat de auto meer opneemt dan normaal.
            Stel dit hoger in dan uw dagelijkse limiet (bijv. 80% normaal → 100% bij zon).
            Na de laadsessie wordt de originele limiet automatisch hersteld.
            Dit veld heeft alleen effect als u hierboven een SoC-limiet entiteit hebt ingevuld.
          </div>
        </div>

        <hr style="border:none;border-top:1px solid var(--divider-color,#e0e0e0);margin:16px 0;" />
        <div style="font-size:.8em;font-weight:700;text-transform:uppercase;letter-spacing:.06em;
                    color:var(--secondary-text-color,#757575);margin-bottom:12px;">
          Locatie (optioneel)
        </div>

        <div class="form-group">
          <label>Locatie-tracker <span style="font-weight:400;text-transform:none;">(optioneel)</span></label>
          <div class="entity-picker">
            <input id="f-ev-location-tracker" type="text"
              value="${this._esc(d.ev_location_tracker || '')}"
              placeholder="device_tracker.tesla" autocomplete="off" />
            <div id="ev-location-tracker-dropdown" class="entity-dropdown" style="display:none;"></div>
          </div>
          <div class="field-hint">
            Tracker die aangeeft of de auto thuis is (bijv. <em>device_tracker.tesla</em>).
            Peak Guard slaat het laden over als de tracker "not_home" toont — de auto heeft
            dan immers geen invloed op het thuisverbruik.
          </div>
        </div>

        <hr style="border:none;border-top:1px solid var(--divider-color,#e0e0e0);margin:16px 0;" />
        <div style="font-size:.8em;font-weight:700;text-transform:uppercase;letter-spacing:.06em;
                    color:var(--secondary-text-color,#757575);margin-bottom:12px;">
          Wake-up (optioneel — voor auto's die in slaapstand gaan)
        </div>

        <div class="form-group">
          <label>Status-sensor <span style="font-weight:400;text-transform:none;">(optioneel)</span></label>
          <div class="entity-picker">
            <input id="f-ev-status-sensor" type="text"
              value="${this._esc(d.ev_status_sensor || '')}"
              placeholder="binary_sensor.tesla_status" autocomplete="off" />
            <div id="ev-status-sensor-dropdown" class="entity-dropdown" style="display:none;"></div>
          </div>
          <div class="field-hint">
            Sensor die aangeeft of de auto verbonden is (bijv. <em>binary_sensor.tesla_status</em>).
            "on" / "connected" / "online" = verbonden; alles anders = slapend.
            Als deze sensor "verbroken" toont, roept Peak Guard de wake-up knop aan voor het laden start.
          </div>
        </div>

        <div class="form-group">
          <label>Wake-up knop <span style="font-weight:400;text-transform:none;">(optioneel)</span></label>
          <div class="entity-picker">
            <input id="f-ev-wake-button" type="text"
              value="${this._esc(d.ev_wake_button || '')}"
              placeholder="button.tesla_wakker" autocomplete="off" />
            <div id="ev-wake-button-dropdown" class="entity-dropdown" style="display:none;"></div>
          </div>
          <div class="field-hint">
            Knop (button.*) om de auto wakker te maken voor het laden.
            Peak Guard roept deze knop aan als de status-sensor "verbroken" toont,
            en wacht daarna tot de auto verbonden is.
          </div>
        </div>
      `;
    }

    this._modalEl.innerHTML = `
      <div class="modal" id="modal-box">
        <h3>${isNew ? "EV Charger toevoegen" : "EV Charger bewerken"}</h3>
        <div class="modal-subtitle">Stap ${step} van 3 — ${stepDesc[step - 1]}</div>

        <div class="wizard-steps">${stepsHTML}</div>

        <div id="wizard-body">${bodyHTML}</div>

        <div class="modal-actions">
          <div>
            ${step > 1
              ? `<button class="btn btn-secondary" id="wizard-prev">← Vorige</button>`
              : `<button class="btn btn-secondary" id="modal-cancel">Annuleren</button>`}
          </div>
          <div class="modal-actions-right">
            ${step < 3
              ? `<button class="btn btn-secondary" id="modal-cancel-right">Annuleren</button>
                 <button class="btn btn-primary" id="wizard-next">Volgende →</button>`
              : `<button class="btn btn-primary" id="modal-save">Opslaan</button>`}
          </div>
        </div>
      </div>
    `;
  }

  _closeModal() {
    this._log("_closeModal() aangeroepen");
    this._modalVisible = false;
    this._editDevice   = null;
    this._wizardStep   = 1;
    this._evMode       = false;
    if (this._modalEl) {
      this._modalEl.style.display = "none";
    }
  }

  _esc(str) {
    const el = document.createElement("span");
    el.textContent = String(str);
    return el.innerHTML;
  }

  // ------------------------------------------------------------------ //
  //  Modal events                                                        //
  // ------------------------------------------------------------------ //

  _attachModalEvents() {
    const root = this._modalEl;
    const allEntities = Object.keys(this._hass.states).sort();

    // ---- Helper: autocomplete-dropdown voor een entity-input ----------
    const makeEntityPicker = (inputId, dropdownId, filterFn) => {
      const inp  = root.querySelector(inputId);
      const drop = root.querySelector(dropdownId);
      if (!inp || !drop) return;
      const show = (filter) => {
        if (!filter) { drop.style.display = "none"; return; }
        const lower = filter.toLowerCase();
        const matches = allEntities
          .filter((id) => {
            if (filterFn && !filterFn(id)) return false;
            const name = this._hass.states[id]?.attributes?.friendly_name || "";
            return id.toLowerCase().includes(lower) || name.toLowerCase().includes(lower);
          })
          .slice(0, 50);
        if (matches.length === 0) { drop.style.display = "none"; return; }
        drop.innerHTML = matches.map((id) => {
          const name = this._hass.states[id]?.attributes?.friendly_name || "";
          return `<div class="entity-option" data-id="${id}">
            <span class="eo-id">${id}</span>
            ${name ? `<span class="eo-name">${name}</span>` : ""}
          </div>`;
        }).join("");
        drop.style.display = "block";
        drop.querySelectorAll(".entity-option").forEach((opt) => {
          opt.addEventListener("mousedown", (e) => {
            e.preventDefault();
            e.stopPropagation();
            inp.value = opt.dataset.id;
            drop.style.display = "none";
            inp.focus();
            inp.dispatchEvent(new Event("change"));
          });
        });
      };
      inp.addEventListener("input",  () => show(inp.value));
      inp.addEventListener("focus",  () => { if (inp.value) show(inp.value); });
      inp.addEventListener("blur",   () => setTimeout(() => { drop.style.display = "none"; }, 250));
    };

    // ---- Sluit bij klik op backdrop, blokkeer propagatie van modal ----
    root.addEventListener("click", (e) => { if (e.target === root) this._closeModal(); });
    root.querySelector("#modal-box")?.addEventListener("click", (e) => e.stopPropagation());

    // ---- Annuleren ----
    root.querySelector("#modal-cancel")?.addEventListener("click", () => this._closeModal());
    root.querySelector("#modal-cancel-right")?.addEventListener("click", () => this._closeModal());

    if (this._evMode) {
      // ================================================================
      //  EV Wizard events
      // ================================================================
      const step = this._wizardStep;

      if (step === 1) {
        makeEntityPicker("#f-ev-switch",  "#ev-switch-dropdown",  (id) => id.startsWith("switch."));
        makeEntityPicker("#f-ev-current", "#ev-current-dropdown", (id) => id.startsWith("number."));
        makeEntityPicker("#f-ev-cable",   "#ev-cable-dropdown",   (id) => id.startsWith("sensor.") || id.startsWith("binary_sensor."));

        // Type-select: als gebruiker naar niet-EV wisselt, heropen als standaard modal
        const actionSelect = root.querySelector("#f-action");
        if (actionSelect) {
          actionSelect.addEventListener("change", () => {
            if (actionSelect.value !== "ev_charger") {
              // Wissel naar standaard modal, bewaar ingevulde naam
              const naam = root.querySelector("#f-name")?.value?.trim() || "";
              this._evMode = false;
              this._wizardStep = 1;
              const fakeDevice = {
                ...( this._editDevice || {} ),
                name: naam,
                action_type: actionSelect.value,
              };
              this._renderStandardModal(fakeDevice);
              this._attachModalEvents();
            }
          });
        }

        root.querySelector("#wizard-next")?.addEventListener("click", () => {
          const naam = root.querySelector("#f-name")?.value?.trim() || "";
          const evSwitch = root.querySelector("#f-ev-switch")?.value?.trim() || "";
          if (!naam) { alert("Naam is verplicht."); return; }
          if (!evSwitch) { alert("Oplaadschakelaar is verplicht."); return; }
          // Bewaar ingevulde waarden in _editDevice zodat volgende stap ze kent
          this._editDevice = {
            ...(this._editDevice || {}),
            name:             naam,
            action_type:      "ev_charger",
            ev_switch_entity: evSwitch,
            entity_id:        evSwitch,
            ev_current_entity: root.querySelector("#f-ev-current")?.value?.trim() || null,
            ev_cable_entity:   root.querySelector("#f-ev-cable")?.value?.trim() || "sensor.tesla_opladen",
          };
          this._wizardStep = 2;
          this._renderEVWizard(this._editDevice);
          this._attachModalEvents();
        });

      } else if (step === 2) {
        // Live preview van het berekende vermogen
        const updatePreview = () => {
          const minA    = parseFloat(root.querySelector("#f-ev-min-a")?.value) || 6;
          const maxA    = parseFloat(root.querySelector("#f-ev-max-a")?.value) || 32;
          const phases  = parseInt(root.querySelector("#f-ev-phases")?.value)  || 1;
          const voltage = this._evVoltage(phases);
          const minW    = Math.round(minA * voltage);
          const maxW    = Math.round(maxA * voltage);
          const prev    = root.querySelector("#ev-power-preview");
          if (prev) prev.textContent =
            `Vermogensbereik: ${minW} W (min) – ${maxW} W (max) · ${voltage} V (${phases === 3 ? "3-fasen" : "1-fase"})`;
        };
        root.querySelector("#f-ev-min-a")?.addEventListener("input", updatePreview);
        root.querySelector("#f-ev-max-a")?.addEventListener("input", updatePreview);
        root.querySelector("#f-ev-phases")?.addEventListener("change", updatePreview);
        updatePreview();

        root.querySelector("#wizard-prev")?.addEventListener("click", () => {
          this._editDevice = { ...(this._editDevice || {}) };
          this._wizardStep = 1;
          this._renderEVWizard(this._editDevice);
          this._attachModalEvents();
        });

        root.querySelector("#wizard-next")?.addEventListener("click", () => {
          const minA   = parseFloat(root.querySelector("#f-ev-min-a")?.value) || 6;
          const maxA   = parseFloat(root.querySelector("#f-ev-max-a")?.value) || 32;
          const phases = parseInt(root.querySelector("#f-ev-phases")?.value)  || 1;
          if (minA >= maxA) { alert("Minimum laadstroom moet lager zijn dan het maximum."); return; }
          this._editDevice = {
            ...(this._editDevice || {}),
            ev_phases: phases,
            min_value: minA,
            max_value: maxA,
          };
          this._wizardStep = 3;
          this._renderEVWizard(this._editDevice);
          this._attachModalEvents();
        });

      } else if (step === 3) {
        makeEntityPicker("#f-ev-soc-entity",        "#ev-soc-entity-dropdown",        (id) => id.startsWith("number."));
        makeEntityPicker("#f-ev-battery-entity",    "#ev-battery-entity-dropdown",    (id) => id.startsWith("sensor."));
        makeEntityPicker("#f-ev-location-tracker",  "#ev-location-tracker-dropdown",  (id) => id.startsWith("device_tracker.") || id.startsWith("binary_sensor."));
        makeEntityPicker("#f-ev-status-sensor",     "#ev-status-sensor-dropdown",     (id) => id.startsWith("binary_sensor.") || id.startsWith("sensor."));
        makeEntityPicker("#f-ev-wake-button",       "#ev-wake-button-dropdown",       (id) => id.startsWith("button."));

        root.querySelector("#wizard-prev")?.addEventListener("click", () => {
          this._editDevice = { ...(this._editDevice || {}) };
          this._wizardStep = 2;
          this._renderEVWizard(this._editDevice);
          this._attachModalEvents();
        });

        root.querySelector("#modal-save")?.addEventListener("click", () => this._handleSave());
      }

    } else {
      // ================================================================
      //  Standaard modal events
      // ================================================================
      makeEntityPicker("#f-entity", "#entity-dropdown", null);

      // Type-select: als gebruiker naar EV wisselt, open wizard
      const actionSelect = root.querySelector("#f-action");
      if (actionSelect) {
        const throttleFields = root.querySelector("#throttle-fields");
        actionSelect.addEventListener("change", () => {
          const v = actionSelect.value;
          if (v === "ev_charger") {
            const naam = root.querySelector("#f-name")?.value?.trim() || "";
            this._evMode = true;
            this._wizardStep = 1;
            const fakeDevice = { ...(this._editDevice || {}), name: naam, action_type: "ev_charger" };
            this._renderEVWizard(fakeDevice);
            this._attachModalEvents();
            return;
          }
          if (throttleFields) throttleFields.style.display = v === "throttle" ? "" : "none";
        });
      }

      root.querySelector("#modal-save")?.addEventListener("click", () => this._handleSave());
    }
  }

  // ------------------------------------------------------------------ //
  //  Opslaan                                                             //
  // ------------------------------------------------------------------ //

  _handleSave() {
    if (this._saving) return;
    const root = this._modalEl;
    const val  = (id) => root.querySelector(id)?.value?.trim() ?? "";

    let device;

    if (this._evMode) {
      // Alle EV-velden werden stap voor stap bewaard in _editDevice.
      // Stap-3 velden (SOC-entity, SOC-percentage) lezen we nu uit het formulier.
      const d        = this._editDevice || {};
      const evSocEntity    = val("#f-ev-soc-entity") || null;
      const evBattEntity   = val("#f-ev-battery-entity") || null;
      const evSoc          = parseInt(val("#f-ev-soc")) || 100;
      const evMaxA   = d.max_value  ?? 32;
      const evPhases = d.ev_phases  ?? 1;
      const evSwitch = d.ev_switch_entity || d.entity_id || "";

      if (!d.name)    { alert("Naam is verplicht."); return; }
      if (!evSwitch)  { alert("Oplaadschakelaar is verplicht."); return; }

      const power_watts = Math.round(evMaxA * this._evVoltage(evPhases));

      const evStatusSensor    = val("#f-ev-status-sensor")    || d.ev_status_sensor    || null;
      const evWakeButton      = val("#f-ev-wake-button")      || d.ev_wake_button      || null;
      const evLocationTracker = val("#f-ev-location-tracker") || d.ev_location_tracker || null;

      device = {
        id:               d.id || `dev_${Date.now()}`,
        name:             d.name,
        entity_id:        evSwitch,
        action_type:      "ev_charger",
        power_watts,
        min_value:        d.min_value  ?? 6,
        max_value:        evMaxA,
        power_per_unit:   null,
        enabled:          true,
        priority:         d.priority ?? 999,
        ev_switch_entity:  evSwitch,
        ev_current_entity: d.ev_current_entity || null,
        ev_cable_entity:   (d.ev_cable_entity != null && d.ev_cable_entity !== "") ? d.ev_cable_entity : "sensor.tesla_opladen",
        ev_soc_entity:     evSocEntity,
        ev_battery_entity: evBattEntity,
        ev_max_soc:        evSoc,
        ev_phases:         evPhases,
        ev_status_sensor:    evStatusSensor,
        ev_wake_button:      evWakeButton,
        ev_location_tracker: evLocationTracker,
      };

    } else {
      // Standaard modal: alles uit het formulier lezen
      const name        = val("#f-name");
      const action_type = val("#f-action");
      const entity_id   = val("#f-entity");
      const isThrottle  = action_type === "throttle";

      if (!name)      { alert("Naam is verplicht."); return; }
      if (!entity_id) { alert("Entity ID is verplicht."); return; }

      const power_watts = parseInt(val("#f-power")) || 0;
      device = {
        id:             this._editDevice?.id || `dev_${Date.now()}`,
        name,
        entity_id,
        action_type,
        power_watts,
        min_value:      isThrottle ? (parseFloat(val("#f-min")) || 0)   : null,
        max_value:      isThrottle ? (parseFloat(val("#f-max")) || 32)  : null,
        power_per_unit: isThrottle ? (parseFloat(val("#f-ppu")) || 690) : null,
        enabled:        true,
        priority:       this._editDevice?.priority ?? 999,
        ev_switch_entity:  null,
        ev_current_entity: null,
        ev_cable_entity:   null,
        ev_soc_entity:     null,
        ev_battery_entity: null,
        ev_max_soc:        null,
        ev_phases:         null,
        ev_status_sensor:    null,
        ev_wake_button:      null,
        ev_location_tracker: null,
      };
    }

    const devices = [...(this._data?.[this._editCascadeType] || [])];
    const existingIdx = devices.findIndex((d) => d.id === device.id);
    if (existingIdx >= 0) {
      devices[existingIdx] = device;
    } else {
      devices.push(device);
    }

    this._reprioritize(devices);
    this._saving = true;
    const saveBtn = root.querySelector("#modal-save");
    if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = "Bezig..."; }

    // EV Charger: ook synchroniseren naar het andere tabblad (als het er nog niet in staat)
    const otherType = this._editCascadeType === "peak" ? "inject" : "peak";
    const isNewEV   = device.action_type === "ev_charger" && existingIdx < 0;
    const syncToOther = isNewEV && !(this._data?.[otherType] || []).some(
      (d) => d.entity_id === device.entity_id
    );

    const saveMain = () => this._saveDevices(this._editCascadeType, devices, true);

    const saveAll = syncToOther
      ? async () => {
          // Sla het andere tabblad eerst op (zonder modal te sluiten)
          const otherDevices = [...(this._data?.[otherType] || [])];
          // Maak een kopie voor het andere tabblad: zelfde data, nieuwe prioriteit achteraan
          const otherDevice = { ...device, priority: otherDevices.length + 1 };
          otherDevices.push(otherDevice);
          this._reprioritize(otherDevices);
          await this._saveDevicesRaw(otherType, otherDevices);
          // Daarna het hoofdtabblad + modal sluiten + data herladen
          await saveMain();
        }
      : saveMain;

    saveAll().finally(() => {
      this._saving = false;
      if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = "Opslaan"; }
    });
  }

  _reprioritize(devices) {
    devices.forEach((d, i) => { d.priority = i + 1; });
  }

  // ------------------------------------------------------------------ //
  //  Hoofd events (achtergrond)                                          //
  // ------------------------------------------------------------------ //

  _attachMainEvents() {
    this.shadowRoot.querySelectorAll(".tab").forEach((t) => {
      t.addEventListener("click", () => {
        this._activeTab = t.dataset.tab;
        this._render();
        if (t.dataset.tab === "savings") {
          // Kleine timeout zodat de DOM al gerenderd is voor chart-init
          setTimeout(() => this._initSavingsCharts(), 80);
        }
      });
    });

    this.shadowRoot.querySelector("#btn-refresh")?.addEventListener("click", () => {
      this._fetchData();
    });
    this.shadowRoot.querySelector("#btn-force-check")?.addEventListener("click", () => {
      this._forceCheck();
    });

    // Chart selector in savings tab
    this.shadowRoot.querySelectorAll(".chart-toggle-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        this._savingsChart = btn.dataset.chart;
        this.shadowRoot.querySelectorAll(".chart-toggle-btn").forEach(b =>
          b.classList.toggle("active", b.dataset.chart === this._savingsChart)
        );
        this._drawBarChart();
      });
    });

    // Savings tab: init charts on first render
    if (this._activeTab === "savings") {
      setTimeout(() => this._initSavingsCharts(), 80);
    }

    this.shadowRoot.querySelectorAll("[data-action='add']").forEach((btn) => {
      btn.addEventListener("click", () => {
        this._openModal(null, btn.dataset.type);
      });
    });

    // Toggle aan/uit
    this.shadowRoot.querySelectorAll("[data-action='toggle']").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const { entity, state } = btn.dataset;
        if (!entity) return;
        const isOn = state === "on";
        btn.disabled = true;
        try {
          await this._hass.callService("switch", isOn ? "turn_off" : "turn_on", { entity_id: entity });
          // Kleine vertraging zodat HA de state kan bijwerken, dan re-render
          setTimeout(() => this._render(), 800);
        } catch (e) {
          console.error("Peak Guard toggle fout:", e);
        } finally {
          btn.disabled = false;
        }
      });
    });

    // Ampère slider: live label update + set_value on release
    this.shadowRoot.querySelectorAll("[data-action='set-ampere']").forEach((slider) => {
      const { type, index: idxStr } = slider.dataset;
      const valEl = this.shadowRoot.querySelector(`#ampere-val-${type}-${idxStr}`);
      slider.addEventListener("input", () => {
        if (valEl) valEl.textContent = `${slider.value} A`;
      });
      slider.addEventListener("change", async () => {
        const { entity } = slider.dataset;
        if (!entity) return;
        try {
          await this._hass.callService("number", "set_value", { entity_id: entity, value: parseFloat(slider.value) });
        } catch (e) {
          console.error("Peak Guard set-ampere fout:", e);
        }
      });
    });

    this.shadowRoot
      .querySelectorAll("[data-action='info'], [data-action='edit'], [data-action='delete'], [data-action='up'], [data-action='down'], [data-action='configure-location']")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const { action, index: idxStr, type } = btn.dataset;
          const idx = parseInt(idxStr);
          const devices = [...(this._data?.[type] || [])];

          if (action === "info") {
            this._showInfoModal(devices[idx], type);
          } else if (action === "edit") {
            this._openModal({ ...devices[idx] }, type);
          } else if (action === "configure-location") {
            this._openModal({ ...devices[idx] }, type, 3);
          } else if (action === "delete") {
            if (confirm(`'${devices[idx].name}' verwijderen?`)) {
              devices.splice(idx, 1);
              this._reprioritize(devices);
              this._saveDevices(type, devices);
            }
          } else if (action === "up" && idx > 0) {
            [devices[idx - 1], devices[idx]] = [devices[idx], devices[idx - 1]];
            this._reprioritize(devices);
            this._saveDevices(type, devices);
          } else if (action === "down" && idx < devices.length - 1) {
            [devices[idx], devices[idx + 1]] = [devices[idx + 1], devices[idx]];
            this._reprioritize(devices);
            this._saveDevices(type, devices);
          }
        });
      });
  }

  // ------------------------------------------------------------------ //
  //  Fout scherm                                                         //
  // ------------------------------------------------------------------ //

  _renderError(msg) {
    this.shadowRoot.innerHTML = `
      <div style="padding:40px;text-align:center;color:var(--error-color,#f44336);font-family:sans-serif;">
        <div style="font-size:2em;margin-bottom:8px;">⚠️</div>
        <div>${msg}</div>
        <div style="margin-top:12px;font-size:.85em;color:#888;">
          Controleer of de Peak Guard integratie correct geladen is.
        </div>
      </div>
    `;
  }

  // ------------------------------------------------------------------ //
  //  Stijlen (achtergrond)                                               //
  // ------------------------------------------------------------------ //

  _styles() {
    return `
      <style>
        *, *::before, *::after { box-sizing: border-box; }
        :host {
          display: block; height: 100%;
          background: var(--primary-background-color, #f0f2f5);
          font-family: var(--paper-font-body1_-_font-family, sans-serif);
          color: var(--primary-text-color, #212121);
        }
        .container { max-width: 860px; margin: 0 auto; padding: 24px 16px; }

        .page-header {
          display: flex; justify-content: space-between; align-items: center;
          margin-bottom: 20px;
        }
        .title-row { display: flex; align-items: center; gap: 10px; }
        .logo { font-size: 1.8em; }
        h1 { margin: 0; font-size: 1.5em; font-weight: 700; }
        .header-actions { display: flex; align-items: center; gap: 10px; }
        .badge {
          display: inline-flex; align-items: center; gap: 6px;
          padding: 4px 12px; border-radius: 20px; font-size: .8em; font-weight: 600;
        }
        .badge.active { background: #e8f5e9; color: #2e7d32; }
        .badge.inactive { background: #ffebee; color: #c62828; }
        .dot { width: 8px; height: 8px; border-radius: 50%; background: currentColor; }
        .badge.active .dot { animation: pulse 1.5s ease-in-out infinite; }
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }

        /* Countdown */
        .countdown-wrap {
          display: flex; flex-direction: column; align-items: flex-end;
          gap: 3px; min-width: 120px;
        }
        .countdown-label {
          font-size: .75em; color: var(--secondary-text-color, #757575);
          white-space: nowrap;
        }
        .countdown-track {
          width: 100%; height: 4px; border-radius: 2px;
          background: var(--divider-color, #e0e0e0); overflow: hidden;
        }
        .countdown-bar {
          height: 100%; border-radius: 2px;
          background: var(--primary-color, #03a9f4);
          transition: width 0.9s linear;
        }
        .btn-force {
          font-size: .8em; padding: 5px 12px;
        }

        /* Info-modal */
        .info-table {
          width: 100%; border-collapse: collapse;
          margin: 14px 0; font-size: .88em;
        }
        .info-table th {
          text-align: left; font-size: .75em; font-weight: 700;
          text-transform: uppercase; letter-spacing: .05em;
          color: var(--secondary-text-color,#757575);
          padding: 0 10px 6px 0; border-bottom: 2px solid var(--divider-color,#e0e0e0);
        }
        .info-table td {
          padding: 8px 10px 8px 0;
          border-bottom: 1px solid var(--divider-color,#f0f0f0);
          vertical-align: middle;
        }
        .info-label {
          font-weight: 600; color: var(--secondary-text-color,#555);
          white-space: nowrap;
        }
        .info-orig  { font-weight: 600; color: var(--primary-text-color,#212121); }
        .info-mod   { color: var(--secondary-text-color,#888); }
        .info-active-badge {
          font-size: .75em; font-weight: 700; padding: 3px 10px;
          border-radius: 12px; background: #fff3e0; color: #e65100;
          border: 1px solid #ffcc80;
        }
        .info-inactive-badge {
          font-size: .75em; font-weight: 700; padding: 3px 10px;
          border-radius: 12px; background: #e8f5e9; color: #2e7d32;
          border: 1px solid #a5d6a7;
        }
        .info-restore-box {
          background: var(--secondary-background-color,#f5f5f5);
          border-radius: 10px; padding: 12px 14px; margin-top: 4px;
        }
        .info-restore-title {
          font-size: .8em; font-weight: 700; text-transform: uppercase;
          letter-spacing: .05em; color: var(--secondary-text-color,#757575);
          margin-bottom: 5px;
        }
        .info-restore-text { font-size: .87em; line-height: 1.5; }

        /* EV locatie-waarschuwing */
        .ev-location-warning {
          display: flex; align-items: center; justify-content: space-between; gap: 10px;
          margin-top: 8px; padding: 7px 10px;
          background: #fff8e1; border: 1px solid #ffe082; border-radius: 6px;
          font-size: .8em; color: #5d4037;
        }
        .btn-inline-warning {
          flex-shrink: 0; padding: 3px 10px; border-radius: 4px; border: none;
          background: #f9a825; color: #fff; font-size: .85em; font-weight: 600;
          cursor: pointer; white-space: nowrap;
        }
        .btn-inline-warning:hover { background: #f57f17; }

        /* EV debounce / step indicator */
        .ev-debounce-bar-wrap {
          margin-top: 6px;
        }
        .ev-debounce-label {
          font-size: .75em; color: #f57c00; font-weight: 600;
          margin-bottom: 3px;
        }
        .ev-debounce-track {
          width: 100%; height: 5px; border-radius: 3px;
          background: #ffe0b2; overflow: hidden;
        }
        .ev-debounce-fill {
          height: 100%; border-radius: 3px;
          background: linear-gradient(90deg, #f57c00, #ffb74d);
          transition: width 0.5s ease;
        }

        /* EV evaluatie-checklist in info-popup */
        .ev-checklist {
          margin-top: 14px;
        }
        .ev-checklist-title {
          font-size: .75em; font-weight: 700; text-transform: uppercase;
          letter-spacing: .05em; color: var(--secondary-text-color,#757575);
          margin-bottom: 8px;
        }
        .ev-checklist-step {
          display: flex; align-items: flex-start; gap: 8px;
          padding: 5px 0; font-size: .86em;
          border-bottom: 1px solid var(--divider-color,#f0f0f0);
        }
        .ev-checklist-step:last-child { border-bottom: none; }
        .ev-step-icon { flex-shrink: 0; width: 18px; text-align: center; }
        .ev-step-body { flex: 1; }
        .ev-step-label { line-height: 1.4; }
        .ev-step-detail {
          font-size: .82em; color: var(--secondary-text-color,#888);
          margin-top: 2px;
        }
        .ev-step-bar-wrap {
          margin-top: 4px; width: 100%; height: 4px; border-radius: 2px;
          background: #ffe0b2; overflow: hidden;
        }
        .ev-step-bar-fill {
          height: 100%; border-radius: 2px;
          background: linear-gradient(90deg, #f57c00, #ffb74d);
        }
        .ev-checklist-step.done  .ev-step-label { color: var(--primary-text-color,#212121); }
        .ev-checklist-step.active .ev-step-label { color: #f57c00; font-weight: 600; }
        .ev-checklist-step.blocked .ev-step-label { color: #c62828; font-weight: 600; }
        .ev-checklist-step.pending .ev-step-label { color: var(--secondary-text-color,#aaa); }

        .warning-panel {
          background: #fff3e0; border: 1px solid #ffb300;
          border-radius: 10px; margin-bottom: 20px; overflow: hidden;
        }
        .warning-panel-header {
          display: flex; align-items: center; gap: 8px;
          padding: 10px 16px; font-weight: 600; font-size: .9em;
          color: #e65100;
        }
        .warning-list {
          border-top: 1px solid #ffe082; max-height: 180px; overflow-y: auto;
        }
        .warning-item {
          display: flex; gap: 12px; padding: 6px 16px; font-size: .82em;
          border-bottom: 1px solid #fff8e1; align-items: baseline;
        }
        .warning-item:last-child { border-bottom: none; }
        .warning-ts {
          color: #999; flex-shrink: 0; font-family: monospace; font-size: .95em;
        }
        .warning-msg { color: #37474f; line-height: 1.4; }

        .status-row {
          display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
          gap: 12px; margin-bottom: 24px;
        }
        .status-card {
          background: var(--card-background-color, #fff);
          border-radius: 12px; padding: 16px 20px;
          box-shadow: 0 1px 3px rgba(0,0,0,.08);
        }
        .label {
          font-size: .75em; text-transform: uppercase; letter-spacing: .06em;
          margin-bottom: 6px; color: var(--secondary-text-color, #757575);
        }
        .value { font-size: 1.9em; font-weight: 700; }
        .value.ok { color: #388e3c; }
        .value.warning { color: #d32f2f; }

        .tabs {
          display: flex; border-bottom: 2px solid var(--divider-color, #e0e0e0);
          margin-bottom: 20px;
        }
        .tab {
          padding: 10px 22px; cursor: pointer; background: none; border: none;
          font-size: .95em; font-weight: 500;
          color: var(--secondary-text-color, #757575);
          border-bottom: 2px solid transparent; margin-bottom: -2px;
          transition: color .2s, border-color .2s;
        }
        .tab:hover { color: var(--primary-color, #03a9f4); }
        .tab.active { color: var(--primary-color, #03a9f4); border-bottom-color: var(--primary-color, #03a9f4); }

        .panel {
          background: var(--card-background-color, #fff);
          border-radius: 12px; padding: 20px;
          box-shadow: 0 1px 3px rgba(0,0,0,.08);
        }
        .panel-header {
          display: flex; justify-content: space-between;
          align-items: flex-start; margin-bottom: 16px; gap: 16px;
        }
        .panel-title { font-size: 1.05em; font-weight: 700; margin-bottom: 4px; }
        .panel-desc { font-size: .85em; color: var(--secondary-text-color, #757575); }
        .device-list { display: flex; flex-direction: column; gap: 10px; }

        .device-card {
          display: flex; align-items: center; gap: 12px;
          background: var(--secondary-background-color, #fafafa);
          border: 1px solid var(--divider-color, #eeeeee);
          border-radius: 10px; padding: 12px 14px; transition: box-shadow .2s;
        }
        .device-card:hover { box-shadow: 0 2px 8px rgba(0,0,0,.08); }
        .order-col { display: flex; flex-direction: column; align-items: center; gap: 2px; }
        .priority {
          width: 28px; height: 28px; border-radius: 50%;
          background: var(--primary-color, #03a9f4); color: #fff;
          display: flex; align-items: center; justify-content: center;
          font-weight: 700; font-size: .85em;
        }
        .btn-order {
          padding: 1px 5px; border: none; border-radius: 4px;
          background: var(--divider-color, #e0e0e0);
          color: var(--secondary-text-color, #757575);
          cursor: pointer; font-size: .75em; line-height: 1.4;
          transition: background .15s, color .15s;
        }
        .btn-order:hover:not(:disabled) { background: var(--primary-color, #03a9f4); color: #fff; }
        .btn-order:disabled { opacity: .3; cursor: default; }
        .device-info { flex: 1; min-width: 0; }
        .device-name-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 2px; }
        .device-name { font-weight: 600; font-size: .95em; }
        .device-status {
          font-size: .72em; font-weight: 700; padding: 2px 8px;
          border-radius: 10px; white-space: nowrap;
          text-transform: uppercase; letter-spacing: .04em;
        }
        .status-on  { background: #e8f5e9; color: #2e7d32; }
        .status-off       { background: #fafafa; color: #9e9e9e; border: 1px solid #e0e0e0; }
        .status-cable-off { background: #fff3e0; color: #e65100; border: 1px solid #ffcc80; }
        .status-throttle { background: #e3f2fd; color: #1565c0; }
        .status-unknown { background: #fafafa; color: #bdbdbd; border: 1px solid #e0e0e0; }
        .device-entity { font-size: .78em; color: var(--secondary-text-color, #9e9e9e); margin: 0 0 6px; word-break: break-all; }
        .ev-live-status {
          font-size: .78em; font-family: monospace;
          color: var(--primary-color, #03a9f4);
          margin-top: 4px; min-height: 1em;
        }
        .chips { display: flex; flex-wrap: wrap; gap: 6px; }
        .chip { font-size: .72em; padding: 2px 9px; border-radius: 10px; background: var(--primary-color, #03a9f4); color: #fff; font-weight: 500; }
        .chip.action { background: #fb8c00; }
        .chip.disabled { background: #9e9e9e; }
        .chip.chip-soc-lim    { background: #5c6bc0; }   /* paars-blauw: huidige limiet */
        .chip.chip-soc-bat    { background: #43a047; }   /* groen: huidig batterijniveau */
        .chip.chip-soc-target { background: #f57c00; }   /* oranje: doel bij zon */
        .device-actions { display: flex; gap: 4px; align-items: center; }

        /* Inline device controls */
        .device-controls {
          display: flex; align-items: center; gap: 8px;
          margin-top: 8px; flex-wrap: wrap;
        }
        .btn-toggle {
          padding: 5px 14px; border: none; border-radius: 20px;
          font-size: .82em; font-weight: 600; cursor: pointer;
          transition: background .15s, opacity .15s;
          white-space: nowrap;
        }
        .btn-toggle.on  { background: #e8f5e9; color: #2e7d32; border: 1px solid #a5d6a7; }
        .btn-toggle.off { background: #fff8f0; color: #e65100; border: 1px solid #ffcc80; }
        .btn-toggle:hover { opacity: .8; }
        .btn-toggle:disabled { opacity: .4; cursor: default; }
        .ampere-control {
          display: flex; align-items: center; gap: 6px;
        }
        .ampere-control label { font-size: .8em; color: var(--secondary-text-color,#757575); white-space: nowrap; }
        .ampere-slider {
          width: 100px; accent-color: var(--primary-color,#03a9f4);
          cursor: pointer;
        }
        .ampere-value {
          font-size: .82em; font-weight: 600; min-width: 38px;
          color: var(--primary-text-color,#212121);
        }

        .btn {
          padding: 8px 16px; border: none; border-radius: 8px;
          cursor: pointer; font-size: .9em; font-weight: 600;
          transition: opacity .15s, box-shadow .15s; white-space: nowrap;
        }
        .btn:hover { opacity: .88; }
        .btn-primary { background: var(--primary-color, #03a9f4); color: #fff; }
        .btn-secondary { background: var(--secondary-background-color, #eeeeee); color: var(--primary-text-color, #212121); }
        .btn-icon {
          padding: 6px 8px; border: none; border-radius: 6px;
          background: transparent; cursor: pointer; font-size: 1em;
          color: var(--secondary-text-color, #9e9e9e); transition: background .15s;
        }
        .btn-icon:hover { background: var(--divider-color, #e0e0e0); color: var(--primary-text-color, #212121); }

        .empty-state { text-align: center; padding: 40px 20px; color: var(--secondary-text-color, #9e9e9e); }
        .empty-state .emoji { font-size: 2.5em; margin-bottom: 8px; }
        .empty-state .sub { font-size: .85em; margin-top: 4px; }

        /* Modal */
        .modal {
          background: var(--card-background-color, #fff);
          border-radius: 14px; padding: 26px;
          width: 100%; max-width: 500px; max-height: 90vh;
          overflow-y: auto;
          box-shadow: 0 12px 40px rgba(0,0,0,.25);
          color: var(--primary-text-color, #212121);
          font-family: var(--paper-font-body1_-_font-family, sans-serif);
        }
        .modal h3 { margin: 0 0 6px; font-size: 1.15em; }
        .modal-subtitle {
          font-size: .82em; color: var(--secondary-text-color, #9e9e9e);
          margin-bottom: 20px;
        }

        /* Wizard voortgangsindicator */
        .wizard-steps {
          display: flex; align-items: center; gap: 0;
          margin-bottom: 24px;
        }
        .wizard-step {
          display: flex; align-items: center; gap: 6px;
          font-size: .78em; font-weight: 600;
          color: var(--secondary-text-color, #bdbdbd);
        }
        .wizard-step.active { color: var(--primary-color, #03a9f4); }
        .wizard-step.done   { color: #43a047; }
        .wizard-step-dot {
          width: 24px; height: 24px; border-radius: 50%;
          display: flex; align-items: center; justify-content: center;
          font-size: .78em; font-weight: 700; flex-shrink: 0;
          background: var(--divider-color, #e0e0e0);
          color: var(--secondary-text-color, #9e9e9e);
          border: 2px solid transparent;
        }
        .wizard-step.active .wizard-step-dot {
          background: var(--primary-color, #03a9f4); color: #fff;
          border-color: var(--primary-color, #03a9f4);
        }
        .wizard-step.done .wizard-step-dot {
          background: #43a047; color: #fff; border-color: #43a047;
        }
        .wizard-connector {
          flex: 1; height: 2px; min-width: 12px;
          background: var(--divider-color, #e0e0e0);
        }
        .wizard-connector.done { background: #43a047; }

        .wizard-panel { display: none; }
        .wizard-panel.active { display: block; }

        .modal-actions {
          display: flex; justify-content: space-between; align-items: center;
          gap: 10px; margin-top: 22px; padding-top: 16px;
          border-top: 1px solid var(--divider-color, #e0e0e0);
        }
        .modal-actions-right { display: flex; gap: 10px; }
        /* Entity picker */
        .entity-picker { position: relative; }
        .entity-dropdown {
          position: absolute; top: 100%; left: 0; right: 0; z-index: 10;
          background: var(--card-background-color, #fff);
          border: 1px solid var(--primary-color, #03a9f4);
          border-top: none; border-radius: 0 0 8px 8px;
          max-height: 220px; overflow-y: auto;
          box-shadow: 0 4px 12px rgba(0,0,0,.15);
        }
        .entity-option {
          padding: 8px 12px; cursor: pointer;
          display: flex; flex-direction: column; gap: 2px;
          border-bottom: 1px solid var(--divider-color, #f0f0f0);
        }
        .entity-option:last-child { border-bottom: none; }
        .entity-option:hover { background: var(--primary-color, #03a9f4); color: #fff; }
        .entity-option:hover .eo-name { color: rgba(255,255,255,.8); }
        .eo-id { font-size: .85em; font-weight: 600; font-family: monospace; }
        .eo-name { font-size: .78em; color: var(--secondary-text-color, #9e9e9e); }

        /* ============================================================ */
        /*  Besparingen tab                                              */
        /* ============================================================ */
        .savings-panel { padding: 20px; }

        .savings-section-title {
          font-size: .82em; font-weight: 700; text-transform: uppercase;
          letter-spacing: .08em; color: var(--secondary-text-color, #757575);
          margin-bottom: 14px;
        }

        /* Big numbers grid */
        .big-numbers-grid {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
          gap: 14px;
          margin-bottom: 4px;
        }
        .big-card {
          border-radius: 14px; padding: 18px 16px;
          display: flex; flex-direction: column; align-items: flex-start;
          box-shadow: 0 2px 8px rgba(0,0,0,.07);
          position: relative; overflow: hidden;
        }
        .big-card::before {
          content: ""; position: absolute; top: 0; left: 0; right: 0;
          height: 3px;
        }
        .peak-card  { background: #fff8f0; }
        .peak-card::before  { background: linear-gradient(90deg,#f57c00,#ffb74d); }
        .solar-card { background: #f1f8e9; }
        .solar-card::before { background: linear-gradient(90deg,#388e3c,#81c784); }
        .total-card { background: linear-gradient(135deg,#e8f5e9 0%,#e3f2fd 100%); }
        .total-card::before { background: linear-gradient(90deg,#1976d2,#43a047); }

        .big-card-icon  { font-size: 1.5em; margin-bottom: 4px; }
        .big-card-label {
          font-size: .72em; font-weight: 700; text-transform: uppercase;
          letter-spacing: .06em; color: var(--secondary-text-color, #757575);
          margin-bottom: 6px;
        }
        .big-number {
          font-size: 2em; font-weight: 800; line-height: 1.1;
          color: var(--primary-text-color, #212121);
        }
        .total-number { color: #1565c0; }
        .big-card-sub {
          font-size: .72em; color: var(--secondary-text-color, #9e9e9e);
          margin-top: 2px; margin-bottom: 10px;
        }
        .big-card-eur {
          font-size: 1.15em; font-weight: 700;
          color: #2e7d32;
        }
        .big-card-eur-label {
          font-size: .7em; color: var(--secondary-text-color, #9e9e9e); margin-top: 2px;
        }
        .no-data { color: var(--secondary-text-color, #bdbdbd); }

        /* Chart toggle */
        .chart-toggle-row {
          display: flex; gap: 8px; margin-bottom: 12px;
        }
        .chart-toggle-btn {
          padding: 6px 16px; border-radius: 20px; border: none;
          background: var(--secondary-background-color, #eeeeee);
          color: var(--secondary-text-color, #757575);
          font-size: .82em; font-weight: 600; cursor: pointer;
          transition: background .15s, color .15s;
        }
        .chart-toggle-btn.active {
          background: var(--primary-color, #03a9f4); color: #fff;
        }
        .chart-toggle-btn:hover:not(.active) {
          background: var(--divider-color, #e0e0e0);
          color: var(--primary-text-color, #212121);
        }

        /* Chart container */
        .chart-container {
          position: relative; width: 100%; height: 200px;
          background: var(--card-background-color, #fff);
          border-radius: 10px; overflow: hidden;
          box-shadow: 0 1px 4px rgba(0,0,0,.06);
        }
        #savings-chart { display: block; width: 100%; height: 100%; }
        .chart-no-data {
          position: absolute; inset: 0; display: flex;
          flex-direction: column; align-items: center; justify-content: center;
          color: var(--secondary-text-color, #9e9e9e);
          font-size: .9em; text-align: center; gap: 6px;
        }
        .chart-no-data-sub { font-size: .8em; opacity: .7; }
        .chart-tooltip {
          position: absolute;
          background: rgba(33,33,33,0.88);
          color: #fff;
          padding: 5px 10px;
          border-radius: 6px;
          font-size: 12px;
          pointer-events: none;
          white-space: nowrap;
          z-index: 10;
          box-shadow: 0 2px 8px rgba(0,0,0,.3);
        }

        /* Events table */
        .events-table-wrap {
          width: 100%; overflow-x: auto;
          border-radius: 10px; box-shadow: 0 1px 4px rgba(0,0,0,.06);
        }
        .events-table {
          width: 100%; border-collapse: collapse;
          background: var(--card-background-color, #fff);
          font-size: .83em;
        }
        .events-table th {
          padding: 10px 12px; text-align: left;
          background: var(--secondary-background-color, #fafafa);
          font-size: .78em; font-weight: 700; text-transform: uppercase;
          letter-spacing: .05em; color: var(--secondary-text-color, #757575);
          border-bottom: 2px solid var(--divider-color, #e0e0e0);
          white-space: nowrap;
        }
        .events-table td {
          padding: 9px 12px;
          border-bottom: 1px solid var(--divider-color, #f0f0f0);
          vertical-align: middle;
        }
        .events-table tr:last-child td { border-bottom: none; }
        .events-table tr:hover td {
          background: var(--secondary-background-color, #fafafa);
        }
        .ev-val  { font-family: monospace; font-weight: 600; }
        .ev-eur  { font-weight: 700; color: #2e7d32; }
        .ev-hypo { font-family: monospace; color: var(--secondary-text-color, #757575); }
        .ev-time { font-family: monospace; font-size: .88em; color: var(--secondary-text-color, #757575); }
        .ev-empty {
          text-align: center; padding: 24px;
          color: var(--secondary-text-color, #9e9e9e); font-style: italic;
        }
        .ev-day-header td {
          padding: 10px 12px 4px;
          font-size: .76em; font-weight: 700; text-transform: uppercase;
          letter-spacing: .06em;
          color: var(--secondary-text-color, #757575);
          background: var(--secondary-background-color, #fafafa);
          border-bottom: 1px solid var(--divider-color, #e0e0e0);
          border-top: 2px solid var(--divider-color, #e0e0e0);
        }
        .ev-day-header:first-child td { border-top: none; }
        .mode-badge {
          display: inline-block; padding: 2px 9px; border-radius: 10px;
          font-size: .78em; font-weight: 700; white-space: nowrap;
        }
        .mode-piek  { background: #fff3e0; color: #e65100; }
        .mode-solar { background: #e8f5e9; color: #1b5e20; }
        .field-hint {
          font-size: .76em; color: var(--secondary-text-color, #9e9e9e);
          margin-top: 5px; line-height: 1.45;
        }
        .form-group { margin-bottom: 16px; }
        .form-group label {
          display: block; font-size: .78em; font-weight: 700;
          text-transform: uppercase; letter-spacing: .05em;
          color: var(--secondary-text-color, #757575); margin-bottom: 6px;
        }
        .form-group input, .form-group select {
          width: 100%; padding: 10px 12px;
          border: 1px solid var(--divider-color, #e0e0e0);
          border-radius: 8px; font-size: .95em;
          background: var(--primary-background-color, #fafafa);
          color: var(--primary-text-color, #212121);
          box-sizing: border-box; transition: border-color .15s;
        }
        .form-group input:focus, .form-group select:focus {
          outline: none; border-color: var(--primary-color, #03a9f4);
        }
        .form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
            </style>
    `;
  }

  // ================================================================ //
  //  Besparingen & Overzicht tab                                      //
  // ================================================================ //

  /**
   * Hulpfunctie: haal sensorwaarde op uit hass.states.
   * Geeft null terug als sensor ontbreekt of unknown is.
   */
  _sensorVal(entityId, decimals = 2) {
    if (!this._hass || !entityId) return null;
    const s = this._hass.states[entityId];
    if (!s || s.state === "unknown" || s.state === "unavailable") return null;
    const v = parseFloat(s.state);
    return isNaN(v) ? null : parseFloat(v.toFixed(decimals));
  }

  _sensorStr(entityId, fallback = "—") {
    const v = this._sensorVal(entityId);
    return v !== null ? String(v) : fallback;
  }

  // ---------------------------------------------------------------- //
  //  HTML-render van de volledige besparingen-tab                     //
  // ---------------------------------------------------------------- //

  _renderSavingsPanel() {
    const s = (id, d = 2) => this._sensorVal(id, d);
    const fmt = (v, unit = "") =>
      v !== null ? `${v}${unit ? " " + unit : ""}` : `<span class="no-data">—</span>`;

    // ---- Big numbers ophalen ----------------------------------- //
    const peakKw   = s("sensor.peak_guard_peak_avoided_kw_this_month", 3);
    const peakEur  = s("sensor.peak_guard_peak_savings_euro_this_month");
    const peakYr   = s("sensor.peak_guard_peak_savings_euro_this_year");
    const solarKwh = s("sensor.peak_guard_solar_verschoven_kwh_this_month", 3);
    const solarEur = s("sensor.peak_guard_solar_savings_euro_this_month");
    const solarYr  = s("sensor.peak_guard_solar_savings_euro_this_year");
    const totalMth = (peakEur !== null || solarEur !== null)
      ? parseFloat(((peakEur ?? 0) + (solarEur ?? 0)).toFixed(2))
      : null;
    const totalYr  = (peakYr !== null || solarYr !== null)
      ? parseFloat(((peakYr ?? 0) + (solarYr ?? 0)).toFixed(2))
      : null;

    // ---- Events-log ophalen ------------------------------------ //
    const peakEvents  = this._hass?.states["sensor.peak_guard_peak_avoided_events"]
                          ?.attributes?.events ?? [];
    const solarEvents = this._hass?.states["sensor.peak_guard_solar_avoided_events"]
                          ?.attributes?.events ?? [];

    // ---- Gecombineerde events gesorteerd op tijd (max 100) ---------- //
    const allEvents = [
      ...peakEvents.map(e => ({
        ts:        e.timestamp_start_uitstel,
        modus:     "Piek",
        icon:      "⚡",
        apparaat:  e.apparaat,
        duur:      e.gemeten_duur_min,
        waarde:    `${e.vermeden_piek_kw} kW`,
        hypo_kw:   e.hypothetische_piek_kw ?? null,
        eur:       e.besparing_eur,
      })),
      ...solarEvents.map(e => ({
        ts:        e.timestamp_start_inschakeling,
        modus:     "Solar",
        icon:      "☀️",
        apparaat:  e.apparaat,
        duur:      e.gemeten_duur_min,
        waarde:    `${e.verschoven_kwh} kWh`,
        hypo_kw:   null,
        eur:       e.besparing_eur,
      })),
    ].sort((a, b) => b.ts.localeCompare(a.ts)).slice(0, 100);

    const fmtTs = (iso) => {
      if (!iso) return "—";
      const d = new Date(iso);
      if (isNaN(d.getTime())) return iso.slice(0, 16).replace("T", " ");
      return `${String(d.getUTCHours()).padStart(2,"0")}:${String(d.getUTCMinutes()).padStart(2,"0")}`;
    };

    const dayKey = (iso) => {
      if (!iso) return "";
      const d = new Date(iso);
      if (isNaN(d.getTime())) return iso.slice(0, 10);
      return `${d.getUTCFullYear()}-${String(d.getUTCMonth()+1).padStart(2,"0")}-${String(d.getUTCDate()).padStart(2,"0")}`;
    };

    const fmtDayLabel = (key) => {
      // key = "2026-03-21"
      const [y, m, day] = key.split("-").map(Number);
      const maanden = ["jan","feb","mrt","apr","mei","jun","jul","aug","sep","okt","nov","dec"];
      const now = new Date();
      const todayKey = dayKey(now.toISOString());
      const yesterdayKey = dayKey(new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - 1)).toISOString());
      if (key === todayKey)     return `Vandaag — ${day} ${maanden[m-1]} ${y}`;
      if (key === yesterdayKey) return `Gisteren — ${day} ${maanden[m-1]} ${y}`;
      return `${day} ${maanden[m-1]} ${y}`;
    };

    // Groepeer events per dag
    let eventsRows = "";
    if (allEvents.length === 0) {
      eventsRows = `<tr><td colspan="6" class="ev-empty">Nog geen events bijgehouden</td></tr>`;
    } else {
      let lastDay = null;
      for (const e of allEvents) {
        const day = dayKey(e.ts);
        if (day !== lastDay) {
          lastDay = day;
          eventsRows += `
            <tr class="ev-day-header">
              <td colspan="6">${fmtDayLabel(day)}</td>
            </tr>`;
        }
        const hypoCell = e.hypo_kw != null
          ? `${Number(e.hypo_kw).toFixed(3)} kW`
          : `—`;
        eventsRows += `
          <tr>
            <td class="ev-time">${fmtTs(e.ts)}</td>
            <td><span class="mode-badge mode-${e.modus.toLowerCase()}">${e.icon} ${e.modus}</span></td>
            <td>${this._esc(e.apparaat)}</td>
            <td>${e.duur} min · ${e.waarde}</td>
            <td class="ev-hypo">${hypoCell}</td>
            <td class="ev-eur">€ ${e.eur}</td>
          </tr>`;
      }
    }

    return `
      <div class="panel savings-panel">

        <!-- ======================================================= -->
        <!-- Sectie 1: Big numbers                                     -->
        <!-- ======================================================= -->
        <div class="savings-section-title">📊 Huidige maand</div>

        <div class="big-numbers-grid">

          <div class="big-card peak-card">
            <div class="big-card-icon">⚡</div>
            <div class="big-card-label">Piekbeperking</div>
            <div class="big-number">${fmt(peakKw, "kW")}</div>
            <div class="big-card-sub">vermeden piekbijdrage</div>
            <div class="big-card-eur">${fmt(peakEur, "€")}</div>
            <div class="big-card-eur-label">bespaard op capaciteitstarief</div>
          </div>

          <div class="big-card solar-card">
            <div class="big-card-icon">☀️</div>
            <div class="big-card-label">Injectiepreventie</div>
            <div class="big-number">${fmt(solarKwh, "kWh")}</div>
            <div class="big-card-sub">verschoven energie</div>
            <div class="big-card-eur">${fmt(solarEur, "€")}</div>
            <div class="big-card-eur-label">bespaard via lokaal verbruik</div>
          </div>

          <div class="big-card total-card">
            <div class="big-card-icon">💰</div>
            <div class="big-card-label">Totaal bespaard</div>
            <div class="big-number total-number">${fmt(totalMth, "€")}</div>
            <div class="big-card-sub">deze maand</div>
            <div class="big-card-eur">${fmt(totalYr, "€")}</div>
            <div class="big-card-eur-label">dit jaar</div>
          </div>

        </div>

        <!-- ======================================================= -->
        <!-- Sectie 2: Grafieken (canvas bar chart)                    -->
        <!-- ======================================================= -->
        <div class="savings-section-title" style="margin-top:28px;">
          📈 Maand-over-maand overzicht
        </div>

        <div class="chart-toggle-row">
          <button class="chart-toggle-btn ${this._savingsChart === "peak" ? "active" : ""}"
                  data-chart="peak">⚡ Piekbeperking</button>
          <button class="chart-toggle-btn ${this._savingsChart === "solar" ? "active" : ""}"
                  data-chart="solar">☀️ Injectiepreventie</button>
        </div>

        <div class="chart-container">
          <canvas id="savings-chart" height="200"></canvas>
          <div id="chart-no-data" class="chart-no-data" style="display:none;">
            Nog niet genoeg historische data.<br>
            <span class="chart-no-data-sub">
              Grafieken vullen automatisch op naarmate de integratie draait.
            </span>
          </div>
          <div id="chart-tooltip" class="chart-tooltip" style="display:none;"></div>
        </div>

        <!-- ======================================================= -->
        <!-- Sectie 3: Events-tabel                                   -->
        <!-- ======================================================= -->
        <div class="savings-section-title" style="margin-top:28px;">
          📋 Recente gebeurtenissen (laatste 100)
        </div>

        <div class="events-table-wrap">
          <table class="events-table">
            <thead>
              <tr>
                <th>Tijd</th>
                <th>Modus</th>
                <th>Apparaat</th>
                <th>Duur · Resultaat</th>
                <th>Hypo. piek</th>
                <th>Besparing</th>
              </tr>
            </thead>
            <tbody>
              ${eventsRows}
            </tbody>
          </table>
        </div>

      </div>
    `;
  }

  // ---------------------------------------------------------------- //
  //  Bar chart via Canvas API (geen externe dependencies)             //
  // ---------------------------------------------------------------- //

  _initSavingsCharts() {
    if (this._activeTab !== "savings") return;
    this._drawBarChart();
  }

  _drawBarChart() {
    const canvas = this.shadowRoot.querySelector("#savings-chart");
    const noData = this.shadowRoot.querySelector("#chart-no-data");
    if (!canvas) return;

    const isPeak = this._savingsChart === "peak";

    // ---- Historische data ophalen uit statistics ----------------
    // We gebruiken de sensor-attributen niet voor historische data;
    // in plaats daarvan slaan we een interne rollende maandhistorie op
    // via _monthlyHistory (gevuld bij elke hass-update).
    const history = this._getMonthlyHistory(isPeak ? "peak" : "solar");

    if (!history || history.length === 0) {
      canvas.style.display = "none";
      if (noData) noData.style.display = "flex";
      return;
    }

    canvas.style.display = "block";
    if (noData) noData.style.display = "none";

    const ctx = canvas.getContext("2d");
    const W = canvas.offsetWidth || 600;
    const H = canvas.offsetHeight || 200;
    canvas.width  = W;
    canvas.height = H;

    // Kleurenpalet
    const accentA = isPeak ? "#f57c00" : "#2e7d32";
    const accentB = isPeak ? "#ffb74d" : "#81c784";
    const textColor = getComputedStyle(this).getPropertyValue("--primary-text-color").trim() || "#212121";
    const gridColor = "rgba(128,128,128,0.15)";
    const bgColor   = getComputedStyle(this).getPropertyValue("--card-background-color").trim() || "#fff";

    ctx.clearRect(0, 0, W, H);

    const PAD = { top: 24, right: 58, bottom: 48, left: 52 };
    const chartW = W - PAD.left - PAD.right;
    const chartH = H - PAD.top - PAD.bottom;

    const labels    = history.map(h => h.label);
    const valuesA   = history.map(h => h.valueA);  // kW of kWh
    const valuesB   = history.map(h => h.valueB);  // EUR

    const maxA = Math.max(...valuesA, 0.01);
    const maxB = Math.max(...valuesB, 0.01);
    const N    = labels.length;
    const groupW = chartW / N;
    const BAR_W  = Math.max(4, Math.min(22, groupW * 0.35));
    const GAP    = 4;
    const chartBottom = PAD.top + chartH;

    // ---- Raster + beide y-assen --------------------------------
    ctx.strokeStyle = gridColor;
    ctx.lineWidth   = 1;
    const gridLines = 4;
    for (let i = 0; i <= gridLines; i++) {
      const y = PAD.top + (i / gridLines) * chartH;
      ctx.beginPath();
      ctx.moveTo(PAD.left, y);
      ctx.lineTo(W - PAD.right, y);
      ctx.stroke();

      // Linker y-as (kW/kWh)
      const valLbl = ((1 - i / gridLines) * maxA).toFixed(1);
      ctx.fillStyle = textColor;
      ctx.font = "11px sans-serif";
      ctx.textAlign = "right";
      ctx.fillText(valLbl, PAD.left - 6, y + 4);

      // Rechter y-as (EUR)
      const eurVal = (1 - i / gridLines) * maxB;
      const eurLbl = eurVal < 10 ? `€${eurVal.toFixed(2)}` : `€${eurVal.toFixed(1)}`;
      ctx.fillStyle = accentB;
      ctx.textAlign = "left";
      ctx.fillText(eurLbl, W - PAD.right + 4, y + 4);
    }

    // ---- Balken + waarde-labels --------------------------------
    const barData = [];
    labels.forEach((lbl, i) => {
      const cx = PAD.left + (i + 0.5) * groupW;

      // Balk A: hoofdwaarde (kW / kWh)
      const hA = (valuesA[i] / maxA) * chartH;
      const xA = cx - BAR_W - GAP / 2;
      const yA = chartBottom - hA;
      ctx.fillStyle = accentA;
      ctx.beginPath();
      ctx.roundRect
        ? ctx.roundRect(xA, yA, BAR_W, hA, [3, 3, 0, 0])
        : ctx.rect(xA, yA, BAR_W, hA);
      ctx.fill();

      // Waarde-label boven balk A
      if (hA > 0) {
        ctx.fillStyle = textColor;
        ctx.font = "bold 10px sans-serif";
        ctx.textAlign = "center";
        ctx.fillText(valuesA[i].toFixed(1), xA + BAR_W / 2, Math.max(yA - 3, PAD.top + 9));
      }

      // Balk B: EUR
      const hB = (valuesB[i] / maxB) * chartH;
      const xB = cx + GAP / 2;
      const yB = chartBottom - hB;
      ctx.fillStyle = accentB;
      ctx.beginPath();
      ctx.roundRect
        ? ctx.roundRect(xB, yB, BAR_W, hB, [3, 3, 0, 0])
        : ctx.rect(xB, yB, BAR_W, hB);
      ctx.fill();

      // Waarde-label boven balk B
      if (hB > 0) {
        ctx.fillStyle = accentB;
        ctx.font = "bold 10px sans-serif";
        ctx.textAlign = "center";
        const eurStr = valuesB[i] < 10 ? `€${valuesB[i].toFixed(2)}` : `€${valuesB[i].toFixed(1)}`;
        ctx.fillText(eurStr, xB + BAR_W / 2, Math.max(yB - 3, PAD.top + 9));
      }

      // X-as label
      ctx.fillStyle = textColor;
      ctx.font = "11px sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(lbl, cx, H - PAD.bottom + 16);

      barData.push({ label: lbl, valueA: valuesA[i], valueB: valuesB[i], xA, xB, BAR_W, yA, yB, hA, hB, chartBottom });
    });

    // ---- Legenda -----------------------------------------------
    const legY = H - 10;
    const unitA = isPeak ? "kW vermeden" : "kWh verschoven";
    ctx.fillStyle = accentA;
    ctx.fillRect(PAD.left, legY - 8, 10, 10);
    ctx.fillStyle = textColor;
    ctx.font = "11px sans-serif";
    ctx.textAlign = "left";
    ctx.fillText(unitA, PAD.left + 14, legY);

    ctx.fillStyle = accentB;
    ctx.fillRect(PAD.left + 130, legY - 8, 10, 10);
    ctx.fillStyle = textColor;
    ctx.fillText("€ bespaard", PAD.left + 144, legY);

    this._setupChartTooltip(canvas, barData, isPeak);
  }

  _setupChartTooltip(canvas, barData, isPeak) {
    const tooltip = this.shadowRoot.querySelector("#chart-tooltip");
    if (!tooltip) return;

    if (this._chartMouseMove)  canvas.removeEventListener("mousemove",  this._chartMouseMove);
    if (this._chartMouseLeave) canvas.removeEventListener("mouseleave", this._chartMouseLeave);

    const unitAShort = isPeak ? "kW" : "kWh";

    this._chartMouseMove = (e) => {
      const rect = canvas.getBoundingClientRect();
      const scaleX = canvas.width  / rect.width;
      const scaleY = canvas.height / rect.height;
      const mx = (e.clientX - rect.left) * scaleX;
      const my = (e.clientY - rect.top)  * scaleY;

      let hit = null;
      for (const bar of barData) {
        if (mx >= bar.xA && mx <= bar.xA + bar.BAR_W && my >= bar.yA && my <= bar.chartBottom) {
          hit = bar; break;
        }
        if (mx >= bar.xB && mx <= bar.xB + bar.BAR_W && my >= bar.yB && my <= bar.chartBottom) {
          hit = bar; break;
        }
      }

      if (hit) {
        const px = e.clientX - rect.left;
        const py = e.clientY - rect.top;
        tooltip.style.left    = `${Math.min(px + 12, rect.width - 160)}px`;
        tooltip.style.top     = `${Math.max(py - 44, 4)}px`;
        tooltip.style.display = "block";
        const eurStr = hit.valueB < 10 ? hit.valueB.toFixed(2) : hit.valueB.toFixed(1);
        tooltip.innerHTML = `<strong>${hit.label}</strong><br>${hit.valueA.toFixed(2)}\u202f${unitAShort}&ensp;·&ensp;€\u202f${eurStr}`;
      } else {
        tooltip.style.display = "none";
      }
    };

    this._chartMouseLeave = () => { tooltip.style.display = "none"; };

    canvas.addEventListener("mousemove",  this._chartMouseMove);
    canvas.addEventListener("mouseleave", this._chartMouseLeave);
  }

  /**
   * Bouw een rollende maandhistorie op vanuit de huidige sensorwaarden.
   * Elke keer dat de maand verandert, wordt de vorige maand opgeslagen.
   * Geeft maximaal 12 maanden terug.
   * History wordt persistent opgeslagen in localStorage zodat een page
   * reload de historiek niet wist.
   */
  _getMonthlyHistory(mode) {
    if (!this._monthlyHistory) {
      this._monthlyHistory = {};
      // Herstel vanuit localStorage bij eerste aanroep — laad beide modes in één keer
      for (const m of ["peak", "solar"]) {
        try {
          const stored = (typeof localStorage !== "undefined")
            ? localStorage.getItem(`peak_guard_monthly_history_${m}`) : null;
          if (stored) this._monthlyHistory[m] = JSON.parse(stored);
        } catch (e) {
          console.warn("[PeakGuard] localStorage lezen mislukt voor mode " + m + ":", e);
        }
      }
    }
    if (!this._monthlyHistory[mode]) this._monthlyHistory[mode] = [];

    const now   = new Date();
    const month = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
    const maanden = ["jan","feb","mrt","apr","mei","jun","jul","aug","sep","okt","nov","dec"];
    const label = `${maanden[now.getMonth()]} '${String(now.getFullYear()).slice(2)}`;

    const currentA = this._sensorVal(
      mode === "peak"
        ? "sensor.peak_guard_peak_avoided_kw_this_month"
        : "sensor.peak_guard_solar_verschoven_kwh_this_month",
      3
    ) ?? 0;
    const currentB = this._sensorVal(
      mode === "peak"
        ? "sensor.peak_guard_peak_savings_euro_this_month"
        : "sensor.peak_guard_solar_savings_euro_this_month",
      2
    ) ?? 0;

    const hist = this._monthlyHistory[mode];

    // Maandwissel detecteren: als de laatste opgeslagen maand anders is,
    // bewaar de vorige maanddata definitief (was al in hist), en log dit.
    if (hist.length > 0) {
      const lastEntry = hist[hist.length - 1];
      if (lastEntry.month !== month) {
        // Vorige maand is al in hist opgeslagen met de eindwaarden van die maand.
        // We hoeven enkel te zorgen dat de nieuwe maand hieronder wordt toegevoegd.
        this._log("Maandwissel gedetecteerd: " + lastEntry.month + " → " + month);
      }
    }

    // Actualiseer of voeg toe voor de huidige maand
    const idx = hist.findIndex(h => h.month === month);
    if (idx >= 0) {
      hist[idx].valueA = currentA;
      hist[idx].valueB = currentB;
      hist[idx].label  = label;
    } else {
      hist.push({ month, label, valueA: currentA, valueB: currentB });
      if (hist.length > 12) hist.shift();
    }

    // Persist naar localStorage
    try {
      if (typeof localStorage !== "undefined") {
        localStorage.setItem(`peak_guard_monthly_history_${mode}`, JSON.stringify(hist));
      }
    } catch (e) {
      console.warn("[PeakGuard] localStorage schrijven mislukt:", e);
    }

    return hist.filter(h => h.valueA > 0 || h.valueB > 0);
  }


}

customElements.define("peak-guard-panel", PeakGuardPanel);
