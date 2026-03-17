# ⚡ Peak Guard

**Peak Guard** is een Home Assistant integratie die automatisch je maandelijkse elektriciteitspiek bewaakt en stroominjectie beheert door apparaten slim te schakelen of te moduleren.

## Wat doet Peak Guard?

Belgische energiecontracten met capaciteitstarief rekenen af op je **maandelijkse piekvermogen** (het hoogste kwartiergemiddelde). Peak Guard voorkomt dat je een nieuwe maandpiek zet door automatisch verbruikers terug te schakelen zodra je piek dreigt overschreden te worden. Omgekeerd schakelt het apparaten in wanneer je zonnepanelen te veel terugleveren aan het net.

Zodra het verbruik normaliseert, worden de apparaten **automatisch hersteld** naar hun oorspronkelijke staat.

## Vereisten

- Home Assistant 2024.1.0 of nieuwer
- Een digitale meter met sensoren voor:
  - **Huidig verbruik** (W) — positief bij afname, negatief bij injectie
  - **Maandelijkse piek** (W)

## Installatie via HACS (aanbevolen)

1. Zorg dat [HACS](https://hacs.xyz) geïnstalleerd is in je Home Assistant
2. Ga naar **HACS → Integraties → ⋮ → Aangepaste opslagplaatsen**
3. Voeg toe: `https://github.com/JOUW_GITHUB_NAAM/peak_guard` als type **Integratie**
4. Zoek naar **Peak Guard** en klik op **Downloaden**
5. Herstart Home Assistant
6. Ga naar **Instellingen → Apparaten & Diensten → Integratie toevoegen**
7. Zoek op **Peak Guard** en volg de configuratiestappen

## Handmatige installatie

1. Download de [nieuwste release](https://github.com/JOUW_GITHUB_NAAM/peak_guard/releases/latest)
2. Kopieer de map `custom_components/peak_guard/` naar je HA-configuratiemap
3. Herstart Home Assistant
4. Ga naar **Instellingen → Apparaten & Diensten → Integratie toevoegen → Peak Guard**

## Configuratie

Tijdens de installatie stel je in:

| Instelling | Beschrijving | Standaard |
|---|---|---|
| Sensor huidig verbruik | Entity ID van de vermogenssensor (W) | — |
| Sensor maandelijkse piek | Entity ID van de maandpiek-sensor (W) | — |
| Buffer (W) | Marge boven de piek voordat ingreep start | 100 W |
| Controle-interval (s) | Hoe vaak Peak Guard controleert | 5 s |

## Gebruik

Na installatie verschijnt **Peak Guard** in de zijbalk. Hier stel je twee cascades in:

### ⚡ Cascade Piekstroom vermijden
Apparaten die worden **uitgeschakeld of teruggeschroefd** zodra het verbruik de maandpiek dreigt te overschrijden. Volgorde bepaalt prioriteit (1 = eerst aangepast).

### ☀️ Cascade Stroominjectie vermijden
Apparaten die worden **ingeschakeld of opgevoerd** zodra er te veel stroom wordt teruggeleverd aan het net.

### Actietypes per apparaat

| Type | Gebruik voor | Herstelt naar |
|---|---|---|
| **Uitschakelen** | Schakelaar die uit mag | Originele staat (aan) |
| **Inschakelen** | Schakelaar die extra verbruik kan opnemen | Originele staat (uit) |
| **Vermogen verminderen** | Laadpaal, boiler, … met `number` entity | Originele laadstroom |

## Bestandsstructuur

```
custom_components/peak_guard/
├── __init__.py
├── config_flow.py
├── const.py
├── controller.py
├── manifest.json
├── strings.json
└── frontend/
    └── peak_guard_panel.js
```
## Licentie

MIT License — vrij te gebruiken en aan te passen.
