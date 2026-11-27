# Plan: één module die de toestand van de lader vaststelt (stap 1)

Status: stap 1 klaar (1a–1e, 2026-11-27, v1.9.14). Aanleiding: v1.9.13 — de piek-cascade
kende de regel voor een verouderde `stopped` al, de solar-cascade niet; de
reviews vonden telkens plekken die elk op hun eigen manier beslisten of de
wagen laadt.

## Doel

Elke vraag over "wat doet de lader nu?" krijgt één antwoord op één plek, met
een naam die zegt waarvoor het dient. De cascades, het laadschema en het
statuspaneel vragen het alleen nog daar. **Geen gedragswijziging**: er gaat
niets anders naar de wagen, wat de tests bewijzen.

## Wat er nu verspreid staat

Leesfuncties in `EVGuard` (al centraal, maar vermengd met de sturing):
`_charge_state`, `is_charging`, `charging_for_control`,
`stale_stopped_after_start`, `peak_is_off`, `charging_draw_w`,
`read_current_a`, `_effective_current_amps`, `cable_connected`,
`charge_complete`, `is_home`, `hw_min_amps`, `voltage`.

Eigen afleidingen in de sturing zelf (hier ontstaan de verschillen):

| Plek | Wat het zelf bepaalt |
|---|---|
| `restore()` solar-tak | `just_started` (3 min), "al gestopt?" |
| `throttle_down_solar` | `recently_started` (3 min), eigen terugval op `guard.state` zonder laadstatus |
| `apply_action` | `sw_on`: schakelaar → status-sensor (`connected`/`complete` tellen hier wél als aan) → laadstatus |
| `_apply_peak` | `just_stopped` (3 min), plus de `turned_on_at`-regel (10 min) |
| `_apply_solar` | herkenning manuele start, weigering met `is_charging` |
| `schedule_decider._sync_guards` | kopieert de geheugenvelden naar kopieën met een ander id |

Er bestaan dus vier tijdvensters naast elkaar (3, 10, 20 min en 2 min marge)
en twee definities van "aan" (`complete` telt in `sw_on` als aan, in
`is_charging` niet).

## Ontwerp

Nieuw bestand `deciders/charger_state.py` met twee delen:

1. **`ChargerMemory`** — wat Peak Guard zelf deed, per schakelaar: laatste
   echte start en stop, en of Peak Guard die stop deed; laatst verstuurde
   stroom en wanneer; bevestigingen (`is_charging`-weigering, verouderde
   stopped). Nu deels in `EVDeviceGuard` (per apparaat-id), deels in
   `_charge_started` / `_charging_confirmed` (per schakelaar).
2. **`ChargerView`** — alleen-lezen beeld per wagen en per cyclus:
   - `observed_charging()` — wat de wagen meldt; voor startbevestigingen (nu `is_charging`)
   - `charging_for_control(now)` — voor het bijsturen van een lopende lading
   - `stopped_by_pg(now)` — nu `peak_is_off`
   - `switch_on_for_apply(now)` — de `sw_on`-ketting, met de bewuste uitzondering voor `complete`
   - `just_started(now)` / `just_stopped(now)` — vensters als benoemde constanten
   - `current_a(now)` (sensor of laatst verstuurde waarde als de sensor achterloopt), `draw_w(now)`
   - `full()`, `cable_connected()`, `home()`, `hw_min_a`, `voltage`
   - `explain()` — één regel tekst voor de log, de EV-API-log en de popup "Waarom deze toestand?"

`EVGuard` houdt eerst dunne doorverwijzende methodes, zodat de aanroepers
niet in één keer moeten veranderen.

## Stappen (elk een eigen commit, na elke stap alle tests groen)

- **1a. Huidig gedrag vastleggen** (`tests/test_charger_state_baseline.py`):
  tabeltests voor elke leesfunctie over alle combinaties (schakelaar
  on/off/unknown/unavailable; laadstatus charging/starting/stopped/complete/
  disconnected/leeg; `last_reported` vóór/na de start; start of stop door
  Peak Guard en het tijdstip t.o.v. de vensters van 2, 3, 10 en 20 min;
  `is_charging`-weigering), plus de eigen afleidingen via de publieke paden
  (`restore`, `throttle_down_solar`, `apply_action`). Review vóór 1b: dekt de
  tabel elke tak?
  *Uitgevoerd*: ~25 000 scenario's, fixture `tests/fixtures/charger_state_baseline.json.gz`
  (gzip, deterministisch). Takdekking gemeten met `coverage` (enkel in de
  venv, geen projectafhankelijkheid): de leesfuncties volledig; in de
  stuurpaden blijven enkel rate-limit-, retry-, update-interval- en
  ontbrekende-entiteit-takken over (stap 2). Acht bewuste fouten (o.a. venster
  3→2 min, grace 120→150 s, `starting` niet als laden, `complete` niet als aan
  in `sw_on`, peak_is_off zonder eigen stop, geen `last_sent`-fallback,
  achterlopende stroomsensor genegeerd) gaven elk 13–402 afwijkende scenario's.
- **1b.** Module aanmaken; de leesfuncties letterlijk verplaatsen, `EVGuard` verwijst door.
- **1c.** Eigen afleidingen vervangen door aanroepen van de view, één plek per
  commit: `throttle_down_solar`, `restore`, `_apply_peak`, `apply_action`,
  `_apply_solar`. Waar twee plekken bewust verschillen, blijft dat zo — met een
  naam i.p.v. een inline formule.
- **1d.** Geheugen per schakelaar i.p.v. per apparaat-id; `_sync_guards`
  verdwijnt. Bij de eigenaar hebben piek-, solar- en schemakopie hetzelfde id
  (`dev_1781848412204`), dus daar verandert niets; wel een gedragswijziging voor
  configuraties met verschillende ids → aparte stap met eigen tests.
  `status_dict()` blijft dezelfde velden geven.
- **1e.** Sectie "Charger state" in `CLAUDE.md` (vensters en definities in één
  tabel); release 1.9.14.
  *Uitgevoerd 1b–1e*: `ChargerMemory` / `ChargerView` in
  `deciders/charger_state.py`; de eigen afleidingen zijn benoemde methodes
  van de view (`just_started`, `just_stopped_by_pg`, `switch_on_for_apply`,
  `peak_not_charging`, …). De baseline bleef in 1b en 1c ongewijzigd. In 1d
  bleek `_sync_guards` bij verschillende ids een piekstop te wissen (schema
  herstartte na 1 min i.p.v. na de min-OFF); nu gedragen beide configuraties
  zich gelijk. `explain()` is nog niet gebouwd (de bestaande `holds` volstaan
  voorlopig). Daarna opgelost (nog in 1.9.14): `release_schedule` vergat een
  piekstop op de gedeelde guard.

## Bewust niet in stap 1

- De vier tijdvensters samenvoegen of `complete` overal hetzelfde laten
  betekenen — gedragswijzigingen, pas na stap 1 en met veldgegevens.
- Commando's versturen (stap 2) en Tesla-eigenaardigheden apart (stap 3).
- De bekende beperking van `peak_is_off` (zie "Known limitations" in
  `CLAUDE.md`); wordt in de view een benoemde keuze.

## Risico's en controle

- `controller.py` en het paneel zijn niet getest; ze gebruiken de
  lader-toestand niet rechtstreeks (paneel enkel via `status_dict`). Na de
  release de API-output vóór en na vergelijken.
- Grootste risico: een verschil tussen verplaatste en oorspronkelijke code dat
  geen test raakt. Daarom 1a eerst, met review op de dekking.
