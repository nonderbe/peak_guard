class PeakGuardPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._data = null;
    this._activeTab = "peak";
    this._savingsChart = "peak";  // welke grafiek zichtbaar in savings-tab
    this._evMode = false;          // houdt bij of de modal in EV-modus staat
    this._editDevice = null;
    this._editCascadeType = "peak";
    this._lastStatusUpdate = 0;

    // De modal leeft als een persistente DOM-node, buiten de render-cyclus
    this._modalEl = null;
    this._modalVisible = false;

    // Debug logger
    this._log = (msg, ...args) => console.log(`[PeakGuard DEBUG] ${msg}`, ...args);
  }

  // ------------------------------------------------------------------ //
  //  HA lifecycle                                                        //
  // ------------------------------------------------------------------ //

  connectedCallback() {
    this._refreshInterval = setInterval(() => {
      // Nooit data herladen als modal open is
      if (this._hass && !this._modalVisible && !this._fetchInProgress) this._fetchData();
    }, 15000);
  }

  disconnectedCallback() {
    clearInterval(this._refreshInterval);
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

    // Update live statusbadges van alle apparaten in beide cascades
    ["peak", "inject"].forEach((cascadeType) => {
      const devices = this._data?.[cascadeType] || [];
      devices.forEach((device, index) => {
        const el = this.shadowRoot.querySelector(`#device-status-${cascadeType}-${index}`);
        if (!el) return;
        const { text, cls } = this._deviceStatus(device);
        el.textContent = text;
        el.className = `device-status ${cls}`;
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
            <button class="btn-icon" id="btn-refresh" title="Verversen">🔄</button>
          </div>
        </header>

        ${this._renderStatusCards()}

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
                  : "Apparaten worden in volgorde ingeschakeld of opgeschroefd wanneer er te veel stroom wordt teruggeleverd aan het net."
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
      const phases = device.ev_phases || 1;
      const maxA   = device.max_value ?? 32;
      const calcW  = Math.round(maxA * 230 * phases);
      powerChip = `<span class="chip">Max ${maxA} A · ${calcW} W · ${phases}F</span>`;
    } else {
      powerChip = device.power_watts ? `<span class="chip">${device.power_watts} W</span>` : "";
    }

    return `
      <div class="device-card">
        <div class="order-col">
          <button class="btn-order" data-action="up" data-index="${index}" data-type="${type}"
            ${index === 0 ? "disabled" : ""}>▲</button>
          <div class="priority">${index + 1}</div>
          <button class="btn-order" data-action="down" data-index="${index}" data-type="${type}"
            ${index === total - 1 ? "disabled" : ""}>▼</button>
        </div>
        <div class="device-info">
          <div class="device-name-row">
            <span class="device-name">${this._esc(device.name)}</span>
            <span id="${statusId}" class="device-status ${statusCls}">${statusText}</span>
          </div>
          <div class="device-entity">${this._esc(device.entity_id)}</div>
          <div class="chips">
            <span class="chip action">${labels[device.action_type] || device.action_type}</span>
            ${powerChip}
            ${device.action_type === "ev_charger" && device.ev_max_soc != null
              ? `<span class="chip">SOC ${device.ev_max_soc}%</span>` : ""}
            ${!device.enabled ? `<span class="chip disabled">Uitgeschakeld</span>` : ""}
          </div>
        </div>
        <div class="device-actions">
          <button class="btn-icon" data-action="edit"
            data-index="${index}" data-type="${type}" title="Bewerken">✏️</button>
          <button class="btn-icon" data-action="delete"
            data-index="${index}" data-type="${type}" title="Verwijderen">🗑️</button>
        </div>
      </div>
    `;
  }

  // Geeft de live statustext en CSS-klasse terug voor een apparaat
  _deviceStatus(device) {
    if (!this._hass) return { text: "—", cls: "status-unknown" };
    const state = this._hass.states[device.entity_id];
    if (!state || state.state === "unavailable" || state.state === "unknown" || state.state === "") {
      return { text: "onbeschikbaar", cls: "status-unknown" };
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

  // ------------------------------------------------------------------ //
  //  Modal — persistente DOM-node, nooit door _render() gewist           //
  // ------------------------------------------------------------------ //

  _openModal(device, cascadeType) {
    this._log("_openModal() aangeroepen. type=" + cascadeType);
    this._editDevice = device || null;
    this._editCascadeType = cascadeType;
    this._modalVisible = true;

    // Maak de backdrop-node éénmalig aan en voeg hem toe aan de shadow root
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
    const isEV       = d.action_type === "ev_charger";
    const isThrottle = d.action_type === "throttle";
    this._evMode = isEV;

    // innerHTML van de modal-node instellen (niet van de shadow root!)
    this._modalEl.innerHTML = `
      <div class="modal" id="modal-box">
        <h3>${d.id ? "Apparaat bewerken" : "Apparaat toevoegen"}</h3>

        <div class="form-group">
          <label>Naam</label>
          <input id="f-name" type="text" value="${this._esc(d.name || "")}"
            placeholder="bijv. Mijn Laadpaal" autocomplete="off" />
        </div>

        <div class="form-group">
          <label>Type apparaat</label>
          <select id="f-action">
            <option value="switch_off"  ${d.action_type === "switch_off"  ? "selected" : ""}>Uitschakelen (switch)</option>
            <option value="switch_on"   ${d.action_type === "switch_on"   ? "selected" : ""}>Inschakelen bij zonne-overschot (switch)</option>
            <option value="ev_charger"  ${d.action_type === "ev_charger"  ? "selected" : ""}>Elektrisch Voertuig (EV Charger)</option>
            <option value="throttle"    ${d.action_type === "throttle"    ? "selected" : ""}>Vermogen verminderen — legacy (number)</option>
          </select>
        </div>

        <!-- Nominaal vermogen: alleen voor switch_off / switch_on / throttle, niet voor EV -->
        <div id="power-field" ${isEV ? 'style="display:none"' : ""}>
          <div class="form-group">
            <label>Nominaal vermogen (W)</label>
            <input id="f-power" type="number" min="0" value="${d.power_watts || 0}"
              placeholder="bijv. 2000" />
            <div class="field-hint">Totaal opgenomen vermogen. Gebruikt voor besparingsberekening.</div>
          </div>
        </div>

        <!-- ======================================================= -->
        <!-- Gewone switch / throttle velden (verborgen bij EV)       -->
        <!-- ======================================================= -->
        <div id="standard-entity-row" ${isEV ? 'style="display:none"' : ""}>
          <div class="form-group">
            <label>Entity ID</label>
            <div class="entity-picker">
              <input id="f-entity" type="text" value="${this._esc(d.entity_id || "")}"
                placeholder="Zoek op naam of entity ID..." autocomplete="off" />
              <div id="entity-dropdown" class="entity-dropdown" style="display:none;"></div>
            </div>
          </div>
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
          </div>
        </div>

        <!-- ======================================================= -->
        <!-- EV Charger velden                                        -->
        <!-- ======================================================= -->
        <div id="ev-fields" ${isEV ? "" : 'style="display:none"'}>

          <div class="ev-section-title">EV Charger instellingen</div>

          <div class="form-group">
            <label>Oplaadschakelaar (switch entity)</label>
            <div class="entity-picker">
              <input id="f-ev-switch" type="text"
                value="${this._esc(d.ev_switch_entity || d.entity_id || "")}"
                placeholder="switch.laadpaal_schakelaar"
                autocomplete="off" />
              <div id="ev-switch-dropdown" class="entity-dropdown" style="display:none;"></div>
            </div>
            <div class="field-hint">Schakelaar die het opladen van de auto start of stopt.</div>
          </div>

          <div class="form-group">
            <label>Laadsnelheid-sensor (number entity)</label>
            <div class="entity-picker">
              <input id="f-ev-current" type="text"
                value="${this._esc(d.ev_current_entity || "")}"
                placeholder="number.laadpaal_stroom"
                autocomplete="off" />
              <div id="ev-current-dropdown" class="entity-dropdown" style="display:none;"></div>
            </div>
            <div class="field-hint">Entiteit waarmee de laadstroom in ampere (A) kan worden aangepast.</div>
          </div>

          <div class="form-group">
            <label>Maximaal batterijpercentage bij zonne-overschot (%)</label>
            <input id="f-ev-soc" type="number" min="0" max="100"
              value="${d.ev_max_soc ?? 100}"
              placeholder="bijv. 100" />
            <div class="field-hint">
              Tijdelijk maximum bij overtollige zonne-energie. Normaal laadt de auto tot een lager percentage
              (bijv. 80%), maar bij zonne-overschot laadt hij tijdelijk tot dit hogere percentage (bijv. 90% of 100%).
            </div>
          </div>

          <div class="form-group">
            <label>Aantal fasen</label>
            <select id="f-ev-phases">
              <option value="1" ${(d.ev_phases ?? 1) == 1 ? "selected" : ""}>1 fase (standaard, 230 V)</option>
              <option value="3" ${(d.ev_phases ?? 1) == 3 ? "selected" : ""}>3 fasen (400 V effectief)</option>
            </select>
            <div class="field-hint">
              1-fase: vermogen = A × 230 V. 3-fasen: vermogen = A × 230 V × 3.
              Voorbeeld: 16 A op 3 fasen = 11 040 W.
            </div>
          </div>

          <div class="form-row">
            <div class="form-group">
              <label>Minimum laadsnelheid (A)</label>
              <input id="f-ev-min-a" type="number" min="0" max="32"
                value="${d.min_value ?? 6}"
                placeholder="6" />
              <div class="field-hint">Minimale stroomsterkte die de lader mag gebruiken.</div>
            </div>
            <div class="form-group">
              <label>Maximum laadsnelheid (A)</label>
              <input id="f-ev-max-a" type="number" min="0" max="125"
                value="${d.max_value ?? 32}"
                placeholder="32" />
              <div class="field-hint">Maximale stroomsterkte die de lader mag gebruiken.</div>
            </div>
          </div>

        </div>
        <!-- /ev-fields -->

        <div class="modal-actions">
          <button class="btn btn-secondary" id="modal-cancel">Annuleren</button>
          <button class="btn btn-primary" id="modal-save">Opslaan</button>
        </div>
      </div>
    `;

    this._attachModalEvents();
  }

  _closeModal() {
    this._log("_closeModal() aangeroepen");
    this._modalVisible = false;
    this._editDevice = null;
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

    // ---- Helper: maak een autocomplete-dropdown aan voor een input ----
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
          });
        });
      };
      inp.addEventListener("input",  () => show(inp.value));
      inp.addEventListener("focus",  () => { if (inp.value) show(inp.value); });
      inp.addEventListener("blur",   () => setTimeout(() => { drop.style.display = "none"; }, 250));
    };

    // Standaard entity picker (switch_off / switch_on / throttle)
    makeEntityPicker("#f-entity",      "#entity-dropdown",      null);
    // EV: schakelaar picker (filter op switch.*)
    makeEntityPicker("#f-ev-switch",   "#ev-switch-dropdown",   (id) => id.startsWith("switch."));
    // EV: stroom-entity picker (filter op number.*)
    makeEntityPicker("#f-ev-current",  "#ev-current-dropdown",  (id) => id.startsWith("number."));

    // ---- Actie-select: toon/verberg de juiste veldgroepen ----
    const actionSelect = root.querySelector("#f-action");
    const updateFieldVisibility = (val) => {
      const evRow       = root.querySelector("#ev-fields");
      const stdRow      = root.querySelector("#standard-entity-row");
      const throttleRow = root.querySelector("#throttle-fields");
      const powerRow    = root.querySelector("#power-field");
      const isEV       = val === "ev_charger";
      const isThrottle = val === "throttle";
      if (evRow)       evRow.style.display       = isEV       ? ""      : "none";
      if (stdRow)      stdRow.style.display      = isEV       ? "none"  : "";
      if (throttleRow) throttleRow.style.display = isThrottle ? ""      : "none";
      if (powerRow)    powerRow.style.display    = isEV       ? "none"  : "";
      this._evMode = isEV;
    };
    if (actionSelect) {
      actionSelect.addEventListener("change", () => updateFieldVisibility(actionSelect.value));
    }

    // ---- Knoppen ----
    root.querySelector("#modal-save")?.addEventListener("click",   () => this._handleSave());
    root.querySelector("#modal-cancel")?.addEventListener("click", () => this._closeModal());
    root.addEventListener("click", (e) => { if (e.target === root) this._closeModal(); });
    root.querySelector("#modal-box")?.addEventListener("click", (e) => e.stopPropagation());
  }

  // ------------------------------------------------------------------ //
  //  Opslaan                                                             //
  // ------------------------------------------------------------------ //

  _handleSave() {
    if (this._saving) return;
    const root = this._modalEl;
    const get  = (id) => root.querySelector(id);
    const val  = (id) => get(id)?.value?.trim() ?? "";

    const name        = val("#f-name");
    const action_type = val("#f-action");
    const isEV        = action_type === "ev_charger";
    const isThrottle  = action_type === "throttle";

    // Validatie: naam altijd verplicht
    if (!name) {
      alert("Naam is verplicht.");
      return;
    }

    let entity_id;
    let device;
    let power_watts;

    if (isEV) {
      // EV: entity_id is de schakelaar (primary key voor snapshots)
      const evSwitch  = val("#f-ev-switch");
      const evCurrent = val("#f-ev-current");
      const evSoc     = parseInt(val("#f-ev-soc")) || 100;
      const evMinA    = parseFloat(val("#f-ev-min-a")) || 6;
      const evMaxA    = parseFloat(val("#f-ev-max-a")) || 32;
      const evPhases  = parseInt(val("#f-ev-phases")) || 1;

      if (!evSwitch) {
        alert("Oplaadschakelaar (switch entity) is verplicht voor een EV Charger.");
        return;
      }
      entity_id = evSwitch;

      // Vermogen dynamisch berekend: max_ampere × 230 V × fasen
      power_watts = Math.round(evMaxA * 230 * evPhases);
      device = {
        id:                 this._editDevice?.id || `dev_${Date.now()}`,
        name,
        entity_id,          // = ev_switch_entity (snapshot-sleutel)
        action_type:        "ev_charger",
        power_watts,        // berekend, niet handmatig ingevoerd
        min_value:          evMinA,
        max_value:          evMaxA,
        power_per_unit:     null,
        enabled:            true,
        priority:           this._editDevice?.priority ?? 999,
        ev_switch_entity:   evSwitch,
        ev_current_entity:  evCurrent || null,
        ev_max_soc:         evSoc,
        ev_phases:          evPhases,
      };
    } else {
      entity_id = val("#f-entity");
      if (!entity_id) {
        alert("Entity ID is verplicht.");
        return;
      }
      power_watts = parseInt(val("#f-power")) || 0;
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
        ev_max_soc:        null,
        ev_phases:         null,
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
    this._saveDevices(this._editCascadeType, devices, true).finally(() => {
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

    this.shadowRoot
      .querySelectorAll("[data-action='edit'], [data-action='delete'], [data-action='up'], [data-action='down']")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const { action, index: idxStr, type } = btn.dataset;
          const idx = parseInt(idxStr);
          const devices = [...(this._data?.[type] || [])];

          if (action === "edit") {
            this._openModal({ ...devices[idx] }, type);
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
        .status-off { background: #fafafa; color: #9e9e9e; border: 1px solid #e0e0e0; }
        .status-throttle { background: #e3f2fd; color: #1565c0; }
        .status-unknown { background: #fafafa; color: #bdbdbd; border: 1px solid #e0e0e0; }
        .device-entity { font-size: .78em; color: var(--secondary-text-color, #9e9e9e); margin: 0 0 6px; word-break: break-all; }
        .chips { display: flex; flex-wrap: wrap; gap: 6px; }
        .chip { font-size: .72em; padding: 2px 9px; border-radius: 10px; background: var(--primary-color, #03a9f4); color: #fff; font-weight: 500; }
        .chip.action { background: #fb8c00; }
        .chip.disabled { background: #9e9e9e; }
        .device-actions { display: flex; gap: 4px; }

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
          width: 100%; max-width: 480px; max-height: 90vh;
          overflow-y: auto;
          box-shadow: 0 12px 40px rgba(0,0,0,.25);
          color: var(--primary-text-color, #212121);
          font-family: var(--paper-font-body1_-_font-family, sans-serif);
        }
        .modal h3 { margin: 0 0 20px; font-size: 1.15em; }
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
        .modal-actions {
          display: flex; justify-content: flex-end;
          gap: 10px; margin-top: 22px; padding-top: 16px;
          border-top: 1px solid var(--divider-color, #e0e0e0);
        }
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
        .ev-empty {
          text-align: center; padding: 24px;
          color: var(--secondary-text-color, #9e9e9e); font-style: italic;
        }
        .mode-badge {
          display: inline-block; padding: 2px 9px; border-radius: 10px;
          font-size: .78em; font-weight: 700; white-space: nowrap;
        }
        .mode-piek  { background: #fff3e0; color: #e65100; }
        .mode-solar { background: #e8f5e9; color: #1b5e20; }


        /* EV Charger modal */
        .ev-section-title {
          font-size: .78em; font-weight: 700; text-transform: uppercase;
          letter-spacing: .07em; color: var(--primary-color, #03a9f4);
          margin: 18px 0 10px; padding-bottom: 6px;
          border-bottom: 2px solid var(--primary-color, #03a9f4);
        }
        .field-hint {
          font-size: .76em; color: var(--secondary-text-color, #9e9e9e);
          margin-top: 5px; line-height: 1.45;
        }
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

    // ---- Gecombineerde events gesorteerd op tijd ---------------- //
    const allEvents = [
      ...peakEvents.map(e => ({
        ts:       e.timestamp_start_uitstel,
        modus:    "Piek",
        icon:     "⚡",
        apparaat: e.apparaat,
        duur:     e.gemeten_duur_min,
        waarde:   `${e.vermeden_piek_kw} kW`,
        eur:      e.besparing_eur,
      })),
      ...solarEvents.map(e => ({
        ts:       e.timestamp_start_inschakeling,
        modus:    "Solar",
        icon:     "☀️",
        apparaat: e.apparaat,
        duur:     e.gemeten_duur_min,
        waarde:   `${e.verschoven_kwh} kWh`,
        eur:      e.besparing_eur,
      })),
    ].sort((a, b) => b.ts.localeCompare(a.ts)).slice(0, 10);

    const fmtTs = (iso) => {
      if (!iso) return "—";
      // "2026-03-21T14:03:00+00:00" → "21 mrt 14:03" (altijd UTC, consistent met server)
      const d = new Date(iso);
      if (isNaN(d.getTime())) return iso.slice(0, 16).replace("T", " ");
      const maanden = ["jan","feb","mrt","apr","mei","jun","jul","aug","sep","okt","nov","dec"];
      return `${d.getUTCDate()} ${maanden[d.getUTCMonth()]} ${String(d.getUTCHours()).padStart(2,"0")}:${String(d.getUTCMinutes()).padStart(2,"0")}`;
    };

    const eventsRows = allEvents.length === 0
      ? `<tr><td colspan="6" class="ev-empty">Nog geen events deze maand</td></tr>`
      : allEvents.map(e => `
        <tr>
          <td>${fmtTs(e.ts)}</td>
          <td><span class="mode-badge mode-${e.modus.toLowerCase()}">${e.icon} ${e.modus}</span></td>
          <td>${this._esc(e.apparaat)}</td>
          <td>${e.duur} min</td>
          <td class="ev-val">${e.waarde}</td>
          <td class="ev-eur">€ ${e.eur}</td>
        </tr>`
      ).join("");

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
        </div>

        <!-- ======================================================= -->
        <!-- Sectie 3: Events-tabel                                   -->
        <!-- ======================================================= -->
        <div class="savings-section-title" style="margin-top:28px;">
          📋 Recente gebeurtenissen (laatste 10)
        </div>

        <div class="events-table-wrap">
          <table class="events-table">
            <thead>
              <tr>
                <th>Datum</th>
                <th>Modus</th>
                <th>Apparaat</th>
                <th>Duur</th>
                <th>Resultaat</th>
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

    const PAD = { top: 20, right: 16, bottom: 48, left: 52 };
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

    // ---- Raster ------------------------------------------------
    ctx.strokeStyle = gridColor;
    ctx.lineWidth   = 1;
    const gridLines = 4;
    for (let i = 0; i <= gridLines; i++) {
      const y = PAD.top + (i / gridLines) * chartH;
      ctx.beginPath();
      ctx.moveTo(PAD.left, y);
      ctx.lineTo(W - PAD.right, y);
      ctx.stroke();

      // Linker y-as labels (kW of kWh)
      const valLbl = ((1 - i / gridLines) * maxA).toFixed(1);
      ctx.fillStyle = textColor;
      ctx.font = `11px sans-serif`;
      ctx.textAlign = "right";
      ctx.fillText(valLbl, PAD.left - 6, y + 4);
    }

    // Rechter y-as label (EUR)
    ctx.save();
    ctx.translate(W - 8, PAD.top + chartH / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.textAlign = "center";
    ctx.fillStyle = accentB;
    ctx.font = "11px sans-serif";
    ctx.fillText("€ besparing", 0, 0);
    ctx.restore();

    // ---- Balken ------------------------------------------------
    labels.forEach((lbl, i) => {
      const cx = PAD.left + (i + 0.5) * groupW;

      // Balk A: hoofdwaarde (kW / kWh)
      const hA = (valuesA[i] / maxA) * chartH;
      const xA = cx - BAR_W - GAP / 2;
      const yA = PAD.top + chartH - hA;
      ctx.fillStyle = accentA;
      ctx.beginPath();
      ctx.roundRect
        ? ctx.roundRect(xA, yA, BAR_W, hA, [3, 3, 0, 0])
        : ctx.rect(xA, yA, BAR_W, hA);
      ctx.fill();

      // Balk B: EUR
      const hB = (valuesB[i] / maxB) * chartH;
      const xB = cx + GAP / 2;
      const yB = PAD.top + chartH - hB;
      ctx.fillStyle = accentB;
      ctx.beginPath();
      ctx.roundRect
        ? ctx.roundRect(xB, yB, BAR_W, hB, [3, 3, 0, 0])
        : ctx.rect(xB, yB, BAR_W, hB);
      ctx.fill();

      // X-as label
      ctx.fillStyle = textColor;
      ctx.font = "11px sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(lbl, cx, H - PAD.bottom + 16);
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
          const stored = localStorage.getItem(`peak_guard_monthly_history_${m}`);
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
      localStorage.setItem(`peak_guard_monthly_history_${mode}`, JSON.stringify(hist));
    } catch (e) {
      console.warn("[PeakGuard] localStorage schrijven mislukt:", e);
    }

    return hist.filter(h => h.valueA > 0 || h.valueB > 0);
  }


}

customElements.define("peak-guard-panel", PeakGuardPanel);