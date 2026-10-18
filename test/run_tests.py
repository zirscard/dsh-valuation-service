#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Self-test for dsh-valuation-service.

Builds a synthetic workpaper tree in a temporary directory, runs the service,
and checks the deterministic outputs. No client data is used.

Usage:
    python3 test/run_tests.py
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "core"))

import valuation_service as svc  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s %s" % ("OK" if cond else "FAIL", name, ("- " + str(detail)) if detail else ""))


def write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def build_fixture(base):
    write_json(os.path.join(base, "项目A", "json", "01.json"), {
        "project": {"project_id": "P-A", "data_cutoff": "2025-12-31"},
        "financial": {
            "raw": {
                "bs": {
                    "货币资金": {"2025A": 500},
                    "应收账款": {"2025A": 200},
                    "存货": {"2025A": 100},
                    "流动资产合计": {"2025A": 800},
                    "资产总计": {"2025A": 1500},
                    "流动负债合计": {"2025A": 300},
                    "负债合计": {"2025A": 600},
                    "所有者权益合计": {"2025A": 900},
                    "短期借款": {"2025A": 100},
                },
                "is": {
                    "营业收入": {"2025A": 1000},
                    "营业成本": {"2025A": 600},
                    "毛利": {"2025A": 400},
                    "净利润": {"2025A": 100},
                    "营业利润": {"2025A": 120},
                    "财务费用": {"2025A": 10},
                },
                "cf": {
                    "经营活动产生的现金流量净额": {"2025A": 150},
                    "购建固定资产、无形资产和其他长期资产支付的现金": {"2025A": -50},
                },
            },
            "ratios": {
                "营业收入增速": {"values": {"2024A": 0.10, "2025A": 0.20}},
                "净利润增速": {"values": {"2025A": 0.15}},
                "综合毛利率": {"values": {"2025A": 0.40}},
                "净利率": {"values": {"2025A": 0.10}},
            },
        },
    })
    write_json(os.path.join(base, "项目A", "json", "02.json"), {
        "assumptions": {
            "forecast": {
                "unit": "千元",
                "periods": ["2025A", "2026E"],
                "parameters": {
                    "营业收入（千元）": {"2026E": 1200},
                    "毛利率": {"2026E": 0.42},
                    "净利润（千元）": {"2026E": 130},
                    "营业利润（千元）": {"2026E": 150},
                },
                "missing": ["资本开支", "营运资本"],
            },
            "comparables": {"companies": [
                {"name": "A", "ps_ttm": 5, "pe_ttm": 20, "treatment": ""},
                {"name": "B", "ps_ttm": 7, "pe_ttm": 25, "treatment": ""},
                {"name": "C", "ps_ttm": 6, "pe_ttm": 22, "treatment": "自比"},
            ]},
            "valuation": {"估值方法": "PS", "投前估值": 6000, "WACC": None, "永续增长率 g": 0.03},
        }
    })
    write_json(os.path.join(base, "项目A", "json", "04.json"), {
        "validation": {"irr_recompute": {"cons": 0.12, "opt": 0.20, "judge": "闭合", "dev": 0.01}}
    })


def main():
    with tempfile.TemporaryDirectory(prefix="dsh-valuation-") as base:
        build_fixture(base)
        svc.BASE = base
        rec = svc.build_unified("项目A")

        print("# Ratios")
        prof = rec["ratios"]["historical"]["profitability"]
        check("gross margin 0.40", abs(prof["gross_margin"] - 0.40) < 1e-9, prof["gross_margin"])
        check("net margin 0.10", abs(prof["net_margin"] - 0.10) < 1e-9, prof["net_margin"])
        check("ROE 0.1111", abs(prof["roe"] - 100 / 900) < 1e-9, prof["roe"])

        print("\n# DCF slots")
        dcf = rec["dcf"]
        check("status partial", dcf["status"] == "partial", dcf["status"])
        check("WACC missing", "wacc" in dcf["missing"], dcf["missing"])
        check("capex missing", "capex" in dcf["missing"], dcf["missing"])
        check("EBIT computed", dcf["inputs"]["ebit"]["status"] == "computed", dcf["inputs"]["ebit"])

        print("\n# Comparable")
        check("PS computed", rec["comparable"]["ps"]["status"] == "computed", rec["comparable"]["ps"])
        check("PE computed", rec["comparable"]["pe"]["status"] == "computed", rec["comparable"]["pe"])
        check("self-comparison removed", rec["comparable"]["ps"]["all_mean"] == 6.0,
              rec["comparable"]["ps"]["all_mean"])

        print("\n# IRR & gate")
        check("IRR closed", rec["irr"]["closed"] is True, rec["irr"])
        check("assumption diff flagged", len(rec["assumption_diff"]) >= 1, rec["assumption_diff"])

        print("\n# CLI --write --json")
        out = os.path.join(base, "valuations-service.json")
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "core", "valuation_service.py"),
             "--base", base, "--projects", "项目A", "--write", "--out", out, "--json"],
            capture_output=True, text=True)
        check("exit code 0", proc.returncode == 0, proc.stderr[-300:])
        payload = json.loads(proc.stdout)
        check("json ok", payload.get("ok") is True, payload)
        check("output written", os.path.exists(out), out)
        with open(out, encoding="utf-8") as f:
            agg = json.load(f)
        check("aggregate has one project", len(agg.get("projects", [])) == 1, len(agg.get("projects", [])))
        check("aggregate schema", agg.get("schema") == "m1.valuation-service/aggregate/v2", agg.get("schema"))

    print("\n# Result: %d passed / %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("Failed:", FAIL)
        sys.exit(1)
    print("All passed")


if __name__ == "__main__":
    main()
