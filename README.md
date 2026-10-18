# dsh-valuation-service

A deterministic financial-data and valuation plugin for **DeepSeek Harness**. It reads a workpaper layout of `01.json` / `02.json` / `04.json`, computes six ratio groups, forecast ratios, DCF input slots, comparable PS / PE and IRR, and writes one unified schema per project set.

It registers one agent tool, **`valuation_service`**. No AI inference, no invented numbers: anything missing is left blank and marked `missing` for a human or a downstream research step to fill.

## What it computes

| Block | Content |
|---|---|
| `ratios.historical` | profitability, solvency, efficiency, cash flow, valuation, growth |
| `ratios.forecast` | forecast-period profitability / growth / expense; BS- and CF-dependent ratios marked missing |
| `dcf` | full input slots (WACC, g, tax, EBIT, D&A, capex, ΔWC, net debt), outputs, missing list, hand-off anchors |
| `comparable` | PS and PE in parallel: trimmed mean, all-sample mean, 1-year mean, pre-money multiple, premium |
| `irr` | target / conservative / optimistic, closed-vs-open judgement, deviation |
| `assumption_diff` | material assumptions vs assumptions computed from actuals, field by field |
| `pending` / `gate` | unresolved conflicts and C2–C7 gate status when the workpapers carry them |

## Input layout

```text
<base>/
├── 项目A/
│   └── json/
│       ├── 01.json   # financial.raw (BS/IS/CF) + financial.ratios
│       ├── 02.json   # assumptions.forecast + assumptions.comparables + assumptions.valuation
│       └── 04.json   # validation.irr_recompute
└── 项目B/
    └── json/...
```

`--base` defaults to `$DSH_VALUATION_BASE` or `./数据底表`. Projects are discovered dynamically: every directory with all three JSON files is included. No project list is hard-coded.

`01.json` may use either `raw = {bs, is, cf}` or report-title sections (e.g. `"一、资产负债表 · 资产"`); both are normalized.

## Install

```bash
dsh plugin add https://github.com/zirscard/dsh-valuation-service
```

Or from a local directory:

```bash
dsh plugin add link:/absolute/path/to/dsh-valuation-service
```

Restart DSH; a new session gets the `valuation_service` tool.

## Tool usage

```json
{
  "base": "/path/to/数据底表",
  "projects": "项目A,项目B",
  "write": true
}
```

- `base` (string, optional): workpaper root; relative paths resolve against the session working directory.
- `projects` (string or array, optional): restrict to these project directory names.
- `write` (boolean, optional, default `false`): write `<base>/valuations-service.json`.
- `out` (string, optional): override the output path.

The tool returns a per-project summary (DCF status and missing list, PS/PE status, IRR closed flag, assumption-diff count, gate) plus the output path.

## CLI usage

The Python entry point is usable without DSH (standard library only):

```bash
python3 core/valuation_service.py --base /path/to/数据底表 --dry-run
python3 core/valuation_service.py --base /path/to/数据底表 --projects 项目A
python3 core/valuation_service.py --base /path/to/数据底表 --write
python3 core/valuation_service.py --base /path/to/数据底表 --write --json
```

`--json` prints one machine-readable summary object; `--write` writes the aggregate file; without `--write` it is a dry run.

## Unified schema

```json
{
  "schema": "m1.valuation-service/v2",
  "project_id": "...",
  "project_name": "...",
  "currency": "CNY",
  "method": "...",
  "financial_inputs": {"period": "...", "is": {}, "bs": {}, "cf": {}},
  "ratios": {"historical": {}, "forecast": {}},
  "dcf": {"status": "computed|partial|unavailable", "inputs": {}, "outputs": {}, "missing": [], "m3_handoff": {}},
  "comparable": {"ps": {}, "pe": {}},
  "irr": {"target": null, "conservative": null, "optimistic": null, "closed": false, "judge": "", "dev": null},
  "assumption_diff": [],
  "pending": [],
  "gate": {}
}
```

The aggregate file wraps records as `{"schema": "m1.valuation-service/aggregate/v2", "generated_at": "...", "base": "...", "projects": [...], "errors": [...]}`.

## Formulas

- Ratios: gross / operating / net margin, ROE, ROA; current / quick / cash ratio, debt-to-equity, debt-to-assets, interest coverage; asset / inventory / receivables turnover and days; OCF ratios; PS / PE / EV / EV-EBITDA; revenue and net-income growth.
- DCF: `FCFF = EBIT × (1 − t) + D&A − capex − ΔWC`; `TV = FCFF_last × (1 + g) / (WACC − g)`; `EV = Σ PV(FCFF) + PV(TV)`; `equity = EV − net debt`.
- Comparable: trimmed mean (10% each side when n ≥ 5), all-sample mean, 1-year mean, pre-money multiple, premium.
- IRR: `(exit value / invested capital) ^ (1 / years) − 1`.
- Assumption diff: material assumption vs module-computed actual, per field.

Ratio formulas are adapted from [anthropics/claude-cookbooks · analyzing-financial-statements/calculate_ratios.py](https://github.com/anthropics/claude-cookbooks/blob/main/skills/custom_skills/analyzing-financial-statements/calculate_ratios.py) (MIT), mapped onto Chinese three-statement labels.

## Adaptation layer

Data variants are handled in one place instead of per project:

| Variant | Function | Rule |
|---|---|---|
| raw layout (`{bs,is,cf}` vs report-title sections) | `_normalize_raw` | normalize by 资产负债表 / 利润表 / 现金流量表 keywords |
| units (thousand / ten-thousand / million CNY) | `_unit_for` | field-name suffix first, then `forecast.unit` |
| field-name variants | `_find_param` | keyword match + exclusion words |
| total vs line item (`资产总计` vs `流动资产合计`) | `_find_val` | totals require an exact match, no substring fallback |
| self-comparison in the comp set | `_included` | drop rows whose `treatment` says 自比 / 剔除 |
| no market PE multiple | `comparable.pe.status=assumption` | use the assumption range explicitly, never present it as a market multiple |

If a new variant is structural (layout / unit / naming), add it to the adapter. If it is a measurement convention, add it to the skill formulas. If data is missing, leave it blank and mark `missing`.

## DCF hand-off

Missing DCF inputs stay blank; `dcf.m3_handoff.anchors` lists what each one needs:

| Missing input | Anchor to fill |
|---|---|
| WACC | risk-free rate / beta / equity risk premium / capital structure |
| g | long-run GDP growth / industry terminal growth |
| capex | capex plan / construction in progress / capacity expansion |
| D&A | depreciation and amortization policy |
| ΔWC | receivables / inventory / payables turnover forecast |
| tax rate | effective tax rate |
| net debt | interest-bearing debt − cash |

## Test

```bash
python3 test/run_tests.py
```

The test builds a synthetic three-project workpaper set in a temporary directory (no client data), runs the service, and checks ratio values, DCF missing slots, comparable status, IRR and the written aggregate file.

## Boundaries

- No AI inference, no model calls, no network requests.
- No client data or project names are shipped in this repository.
- Missing inputs are marked, not fabricated.
- It does not replace a valuation model: it standardizes inputs and keeps the slots.

## License

MIT. Ratio formulas adapted from anthropics/claude-cookbooks (MIT); see [THIRD_PARTY_NOTICES.md](./THIRD_PARTY_NOTICES.md).
