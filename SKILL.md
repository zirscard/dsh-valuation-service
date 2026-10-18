---
name: valuation-service
description: Deterministic financial-data and valuation service for DSH. Turns 01/02/04 workpaper JSON into six ratio groups, forecast ratios, DCF input slots, comparable PS/PE and IRR, and writes one unified schema; missing DCF inputs stay blank and are marked missing for a downstream research step to fill.
---

# Valuation service (valuation-service)

> Trigger: the user asks for ratios, forecast ratios, DCF, comparable PS/PE, IRR recomputation, or standardized financial-data processing.

Two pieces:

- `core/valuation_skill.py` — pure formula library (no file I/O, no project-specific code)
- `core/valuation_service.py` — adapter + orchestrator (discover projects, read 01/02/04, call the skill, write one aggregate schema)

The DSH plugin `dsh-valuation-service` registers the agent tool `valuation_service` and ships both files.

## Inputs / outputs

| Item | Content |
|---|---|
| Input | `<base>/<project>/json/01.json` (raw statements + historical ratios), `02.json` (forecast parameters + comps + valuation parameters), `04.json` (IRR recomputation, conflicts) |
| Output | `<base>/valuations-service.json` — one aggregate file, schema `m1.valuation-service/v2` |

`<base>` defaults to `$DSH_VALUATION_BASE` or `./数据底表`; pass `--base` (CLI) or `base` (tool) to override.

## Workflow

1. Confirm the workpaper root and the project directories.
2. Run the tool with a dry run first (`write: false`), inspect per-project DCF / comparable / IRR status.
3. If the statuses are expected, run with `write: true`.
4. Report missing DCF inputs and `assumption_diff` items to the user; do not fill them by guessing.

Never invent a WACC, terminal growth rate, capex, D&A or working-capital change. Leave them blank and mark them `missing`.

## Six ratio groups

Profitability (gross / operating / net margin, ROE, ROA), solvency (current / quick / cash ratio, debt ratios, interest coverage), efficiency (turnover and days), cash flow (OCF ratios, FCF), valuation (PS, PE, EV, EV/EBITDA), growth (revenue, net income).

## DCF formulas

`FCFF = EBIT × (1 − t) + D&A − capex − ΔWC`

`TV = FCFF_last × (1 + g) / (WACC − g)`

`EV = Σ PV(FCFF) + PV(TV)`

`equity = EV − net debt`

## Hand-off anchors

| Missing input | Anchor to fill |
|---|---|
| WACC | risk-free rate / beta / equity risk premium / capital structure |
| g | long-run GDP growth / industry terminal growth |
| capex | capex plan / construction in progress / capacity expansion |
| D&A | depreciation and amortization policy |
| ΔWC | receivables / inventory / payables turnover forecast |
| tax rate | effective tax rate |
| net debt | interest-bearing debt − cash |

## Adaptation rules

| Variant | Function | Rule |
|---|---|---|
| raw layout (`{bs,is,cf}` vs report-title sections) | `_normalize_raw` | normalize by 资产负债表 / 利润表 / 现金流量表 keywords |
| units | `_unit_for` | field suffix first, then `forecast.unit` |
| field names | `_find_param` | keyword match + exclusion words |
| total vs line item | `_find_val` | totals exact-match only |
| self-comparison | `_included` | drop 自比 / 剔除 rows |
| no market PE | `pe.status=assumption` | explicit assumption, not a market multiple |

Structural / unit / naming issue → add to the adapter. Measurement convention → add to the skill formulas. Missing data → leave blank and mark `missing`.

## Known limitations

1. DCF inputs are often missing in early workpapers; the service keeps slots and marks them.
2. PE may be an assumption range (no market multiples in the comp set); it is marked `assumption`.
3. Project self-multiples are removed via `treatment`.
4. Material assumptions and module actuals can cover different periods; `assumption_diff.note` flags that instead of silently aligning them.

## Test

```bash
python3 test/run_tests.py
```
