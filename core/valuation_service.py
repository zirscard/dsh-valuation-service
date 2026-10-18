#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Financial-data and valuation service (unified v2).

Reads workpaper cells, calls valuation_skill, and writes one unified schema:
  - one schema for every project (PS / PE / DCF are fields, not separate outputs)
  - DCF keeps every input/output slot and marks what is missing
  - assumption diff: material assumptions vs assumptions computed from actuals
  - comparable PS and PE run side by side (missing -> missing/assumption)
  - inconsistencies are not silently "fixed"; they go into status/note for human review

Usage:
  python3 valuation_service.py [--dry-run|--write] [--base DIR]
                               [--projects 项目A,项目B] [--out FILE] [--json]
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

import valuation_skill as skill

DEFAULT_BASE = os.environ.get("DSH_VALUATION_BASE") or os.path.join(os.getcwd(), "数据底表")
BASE = DEFAULT_BASE
DATE = datetime.date.today().isoformat()


def discover_projects():
    """动态发现项目：数据底表下拥有完整 01/02/04 JSON 的项目目录。

    不写死项目清单——skill 服务所有项目，未来新项目只要按统一底稿结构放好
    json/01.json / 02.json / 04.json 即自动纳入。排除 _ 前缀（_design/_pipeline）与非项目目录。
    """
    out = []
    if not os.path.isdir(BASE):
        return out
    for name in sorted(os.listdir(BASE)):
        if name.startswith("_") or name in ("看板",):
            continue
        p = os.path.join(BASE, name)
        if not os.path.isdir(p):
            continue
        jd = os.path.join(p, "json")
        if os.path.exists(os.path.join(jd, "01.json")) and os.path.exists(os.path.join(jd, "02.json")) and os.path.exists(os.path.join(jd, "04.json")):
            out.append(name)
    return out


DEFAULT_PROJECTS = None  # None → 运行时 discover_projects()

# 中文三表标签 → 技能标准英文字段（用于「连接财报单元格」）
BS_LABELS = {
    "cash_and_equivalents": ("货币资金", "现金及现金等价物"),
    "accounts_receivable": ("应收账款",),
    "inventory": ("存货",),
    "current_assets": ("流动资产合计",),
    "total_assets": ("资产总计", "资产合计", "总资产"),
    "current_liabilities": ("流动负债合计",),
    "total_liabilities": ("负债合计",),
    "shareholders_equity": ("所有者权益合计", "股东权益合计"),
}
IS_LABELS = {
    "revenue": ("营业收入",),
    "cogs": ("营业成本",),
    "gross_profit": ("毛利",),
    "net_income": ("净利润", "净亏损"),
    "operating_income": ("营业利润", "营业亏损"),
    "ebit": ("营业利润", "营业亏损"),
    "interest_expense": ("财务费用",),
}
CF_LABELS = {
    "operating_cash_flow": ("经营活动产生的现金流量净额", "经营活动现金流量净额"),
    "capex": ("购建固定资产、无形资产和其他长期资产支付的现金",),
}
DEBT_LABELS = ("短期借款", "长期借款", "应付债券", "一年内到期的非流动负债")


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _find_val(section, labels, period):
    """在 raw 三表某 section（{label:{period:val}}）中按标签找某期值。

    总计类（资产总计/负债合计）必须精确匹配，避免「流动资产合计」被当成「资产合计」。
    """
    if not isinstance(section, dict):
        return None
    totals = {"资产总计", "资产合计", "总资产", "负债合计", "负债总计"}
    # 1) 精确匹配优先
    for label, vals in section.items():
        if str(label).strip() in labels:
            if isinstance(vals, dict) and period in vals:
                return vals[period]
    # 2) 子串回退（总计类不再回退，必须精确）
    for label, vals in section.items():
        l = str(label).strip()
        if l in totals:
            continue
        if any(w in l for w in labels):
            if isinstance(vals, dict) and period in vals:
                return vals[period]
    return None


def _periods(section):
    """取 raw 三表所有期间（合并所有行的键）。"""
    ps = set()
    if isinstance(section, dict):
        for vals in section.values():
            if isinstance(vals, dict):
                ps.update(vals.keys())
    return sorted(ps)


def _normalize_raw(d01):
    """把 01.json financial.raw 统一成 {bs,is,cf}。

    支持两种常见来源结构：
      - 按表分节：raw = {bs: {}, is: {}, cf: {}}
      - 按报表标题分节：raw = {'一、资产负债表 · 资产': {...}, '四、利润表': {...}, ...}
    """
    raw = d01.get("financial", {}).get("raw", {})
    if not isinstance(raw, dict):
        return {"bs": {}, "is": {}, "cf": {}}
    if any(k in raw for k in ("bs", "is", "cf")):
        return {"bs": raw.get("bs", {}), "is": raw.get("is", {}), "cf": raw.get("cf", {})}
    bs, is_, cf = {}, {}, {}
    for name, section in raw.items():
        if not isinstance(section, dict):
            continue
        if "资产负债表" in name or "所有者权益" in name:
            bs.update(section)
        elif "利润表" in name or "损益" in name:
            is_.update(section)
        elif "现金流量表" in name or "现金流" in name:
            cf.update(section)
    return {"bs": bs, "is": is_, "cf": cf}


def extract_standard_financial(d01, period):
    """从 01.json financial.raw 抽取标准三表数据（连接财报单元格）。"""
    raw = _normalize_raw(d01)
    bs = raw.get("bs", {}); is_ = raw.get("is", {}); cf = raw.get("cf", {})

    def pick(section, labels):
        return _find_val(section, labels, period)

    bs_out = {k: pick(bs, v) for k, v in BS_LABELS.items()}
    # 有息负债 = 各债务科目之和（近似）
    debt = 0.0; has_debt = False
    for label, vals in bs.items():
        if any(w in str(label) for w in DEBT_LABELS):
            v = vals.get(period) if isinstance(vals, dict) else None
            if isinstance(v, (int, float)):
                debt += v; has_debt = True
    bs_out["total_debt"] = debt if has_debt else None

    is_out = {k: pick(is_, v) for k, v in IS_LABELS.items()}
    cf_out = {k: pick(cf, v) for k, v in CF_LABELS.items()}
    return {"is": is_out, "bs": bs_out, "cf": cf_out, "market_data": {}}


def extract_forecast(d02):
    """从 02.json 抽取预测参数（材料假设）+ 缺失清单。"""
    fc = (d02.get("assumptions", {}) or {}).get("forecast", {})
    params = fc.get("parameters", {}) or {}
    periods = fc.get("periods", []) or []
    e_periods = [p for p in periods if str(p).endswith("E")]
    missing = fc.get("missing", []) or []
    return {"parameters": params, "periods": e_periods, "missing": missing}


def _find_param(params, include, exclude=()):
    for key in params:
        if any(w in key for w in include) and not any(e in key for e in exclude):
            return key
    return None


def comparable_inputs(d02):
    """从 02.json 抽取可比估值所需输入（PS + PE 两套）。"""
    comps = (d02.get("assumptions", {}) or {}).get("comparables", {})
    companies = comps.get("companies", []) or []
    valuation = (d02.get("assumptions", {}) or {}).get("valuation", {}) or {}

    def _included(c):
        # 有 treatment 字段时剔除「自比」「剔除」；没有该字段则全部纳入
        t = str(c.get("treatment", ""))
        return "剔除" not in t and "自比" not in t

    ps_ttm = [_num(c.get("ps_ttm")) for c in companies if c.get("ps_ttm") is not None and _included(c)]
    if not ps_ttm:
        ps_ttm = [_num(c.get("ps")) for c in companies if c.get("ps") is not None and _included(c)]
    ps_1y = [_num(c.get("ps_1y")) for c in companies if c.get("ps_1y") is not None]
    pe_ttm = [_num(c.get("pe_ttm")) for c in companies if c.get("pe_ttm") is not None]
    pe_1y = [_num(c.get("pe_1y")) for c in companies if c.get("pe_1y") is not None]

    pre_money = _num(valuation.get("投前估值"))

    # base revenue / base net（单位感知：字段后缀优先，其次 forecast.unit）
    fc = (d02.get("assumptions", {}) or {}).get("forecast", {})
    params = fc.get("parameters", {}) or {}
    fc_unit = fc.get("unit", "") or ""

    def _unit_for(key):
        if "（万元）" in key:
            return 1e4
        if "（百万元）" in key:
            return 1e6
        if "（千元）" in key:
            return 1e3
        if "千元" in fc_unit:
            return 1e3
        if "万元" in fc_unit:
            return 1e4
        if "百万元" in fc_unit:
            return 1e6
        return 1.0

    rev_key = _find_param(params, ("营业收入",), ("增长率",))
    net_key = _find_param(params, ("净利润",))
    base_revenue = None
    base_net = None
    if rev_key:
        r = params[rev_key]
        b = _num(r.get("2026E")) or _num(r.get("2025E")) or _num(r.get("2025A"))
        if b is not None:
            base_revenue = b * _unit_for(rev_key)
    # PE 方法用退出年/预测净利：估值参数里任意「净利润」字段优先（如「2029E 净利润」），否则用预测净利
    if metric_pe(valuation):
        for key, val in valuation.items():
            if "净利润" in key and _num(val) is not None:
                base_net = _num(val)
                break
    if base_net is None and net_key:
        n = params[net_key]
        b = _num(n.get("2026E")) or _num(n.get("2025E")) or _num(n.get("2025A"))
        if b is not None:
            base_net = b * _unit_for(net_key)

    # PE 为假设时：用退出 PE 区间作为 pe_ttm 的占位
    if not pe_ttm and metric_pe(valuation):
        tiers = valuation.get("退出 PE 区间")
        if isinstance(tiers, str):
            import re
            nums = re.findall(r"\d+(?:\.\d+)?", tiers)
            if len(nums) >= 2:
                lo, hi = float(nums[0]), float(nums[1])
                pe_ttm = [lo, (lo + hi) / 2, hi]

    return {"ps_ttm": ps_ttm, "ps_1y": ps_1y, "pe_ttm": pe_ttm, "pe_1y": pe_1y,
            "pre_money": pre_money, "base_revenue": base_revenue, "base_net": base_net}


def metric_pe(valuation):
    m = valuation.get("估值方法", "") or ""
    return "PE" in m and "PS" not in m


def _num(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return None


def dcf_inputs(d02):
    valuation = (d02.get("assumptions", {}) or {}).get("valuation", {}) or {}
    fc = (d02.get("assumptions", {}) or {}).get("forecast", {})
    missing = fc.get("missing", []) or []
    params = fc.get("parameters", {}) or {}
    fc_unit = fc.get("unit", "") or ""

    def _unit_for(key):
        if "（万元）" in key: return 1e4
        if "（百万元）" in key: return 1e6
        if "（千元）" in key: return 1e3
        if "千元" in fc_unit: return 1e3
        if "万元" in fc_unit: return 1e4
        if "百万元" in fc_unit: return 1e6
        return 1.0

    # EBIT 用营业利润（不是净利润），单位换算到元
    op_key = _find_param(params, ("营业利润", "EBIT", "营业亏损"))
    ebit = None
    if op_key:
        v = _num(params[op_key].get("2026E")) or _num(params[op_key].get("2025E"))
        if v is not None:
            ebit = v * _unit_for(op_key)
    return {
        "wacc": _num(valuation.get("WACC")),
        "g": _num(valuation.get("永续增长率 g")),
        "tax_rate": None,
        "ebit": ebit,
        "da": None,  # 缺折旧摊销
        "capex": None if any("资本开支" in m for m in missing) else None,
        "wc_change": None if any("营运资本" in m for m in missing) else None,
        "fcff_projection": None,
        "net_debt": None,
    }


def assumption_diff_inputs(d01, d02):
    """材料假设（02 预测 2026E） vs 模块实际（01 最新期比率）。"""
    material = {}
    fc = (d02.get("assumptions", {}) or {}).get("forecast", {})
    params = fc.get("parameters", {}) or {}
    e = "2026E"
    m_key = _find_param(params, ("毛利率",))
    g_key = _find_param(params, ("增长率",))
    rev_key = _find_param(params, ("营业收入",), ("增长率",))
    net_key = _find_param(params, ("净利润",))
    if m_key: material["毛利率"] = _num(params[m_key].get(e))
    if g_key: material["营收增长率"] = _num(params[g_key].get(e))
    if rev_key and net_key:
        r = _num(params[rev_key].get(e)); n = _num(params[net_key].get(e))
        if r and n is not None: material["净利率"] = n / r

    module = {}
    ratios = d01.get("financial", {}).get("ratios", {})
    # 最新期（取比率值里的最后一个期间）
    def latest(series):
        if not isinstance(series, dict):
            return None
        keys = sorted(series.get("values", {}).keys()) if isinstance(series.get("values"), dict) else []
        return series["values"][keys[-1]] if keys else None
    if "综合毛利率" in ratios: module["毛利率"] = latest(ratios["综合毛利率"])
    if "营业收入增速" in ratios: module["营收增长率"] = latest(ratios["营业收入增速"])
    if "净利率" in ratios: module["净利率"] = latest(ratios["净利率"])
    return material, module


def build_forecast_ratios(d02):
    """预测比率：从 02 盈利预测参数算预测期六类比率（损益类可算；BS/CF 类标 missing）。"""
    fc = (d02.get("assumptions", {}) or {}).get("forecast", {})
    params = fc.get("parameters", {}) or {}
    periods = [p for p in (fc.get("periods") or []) if str(p).endswith("E")]
    m_key = _find_param(params, ("毛利率",))
    g_key = _find_param(params, ("增长率",))
    net_key = _find_param(params, ("净利润",))
    rev_key = _find_param(params, ("营业收入",), ("增长率",))
    op_key = _find_param(params, ("营业利润", "EBIT", "营业亏损"))

    outp = {"periods": periods, "profitability": {}, "growth": {}, "expense": {}}
    for p in periods:
        prof = {}
        if m_key:
            prof["gross_margin"] = _num(params[m_key].get(p))
        if rev_key and net_key:
            r = _num(params[rev_key].get(p))
            n = _num(params[net_key].get(p))
            if r and n is not None:
                prof["net_margin"] = n / r
        if rev_key and op_key:
            r = _num(params[rev_key].get(p))
            o = _num(params[op_key].get(p))
            if r and o is not None:
                prof["operating_margin"] = o / r
        if prof:
            outp["profitability"][p] = prof
        if g_key:
            outp["growth"][p] = {"revenue_growth": _num(params[g_key].get(p))}
        exp = {}
        for name in ("销售费用率", "管理费用率", "研发费用率", "财务费用率"):
            if name in params:
                v = _num(params[name].get(p))
                if v is not None:
                    exp[name] = v
        if exp:
            outp["expense"][p] = exp
    outp["solvency"] = {"note": "预测期无资产负债表，偿债能力比率无法计算（需补预测 BS）"}
    outp["efficiency"] = {"note": "预测期无资产负债表，营运能力比率无法计算（需补预测 BS）"}
    outp["cash_flow"] = {"note": "预测期无现金流量表，现金流比率无法计算（需补预测 CF）"}
    outp["valuation"] = {"note": "预测期无市值数据，估值比率无法计算"}
    outp["note"] = "预测期仅有盈利预测参数（损益类）；BS/CF/市值类已标 missing"
    return outp


def build_unified(proj):
    jd = os.path.join(BASE, proj, "json")
    d01 = read_json(os.path.join(jd, "01.json"))
    d02 = read_json(os.path.join(jd, "02.json"))
    d04 = read_json(os.path.join(jd, "04.json"))
    valuation = (d02.get("assumptions", {}) or {}).get("valuation", {}) or {}

    # 最新期间（统一 raw 结构后取并集）
    raw = _normalize_raw(d01)
    periods = set()
    for sec in ("bs", "is", "cf"):
        periods |= set(_periods(raw.get(sec, {})))
    latest = sorted(periods)[-1] if periods else None

    fin = extract_standard_financial(d01, latest) if latest else {"is": {}, "bs": {}, "cf": {}}
    ci = comparable_inputs(d02)
    di = dcf_inputs(d02)
    mat, mod = assumption_diff_inputs(d01, d02)

    # 可比（PS + PE 双轨）
    comparable = skill.compute_comparable(
        ps_ttm=ci["ps_ttm"], ps_1y=ci["ps_1y"], pe_ttm=ci["pe_ttm"], pe_1y=ci["pe_1y"],
        pre_money=ci["pre_money"], base_revenue=ci["base_revenue"], base_net=ci["base_net"],
        pe_assumption=metric_pe(valuation))

    # IRR
    rc = (d04.get("validation", {}) or {}).get("irr_recompute", {}) or {}
    irr = {"target": _num(valuation.get("目标 IRR")) or _num(valuation.get("中性 IRR")),
           "conservative": _num(rc.get("cons")), "optimistic": _num(rc.get("opt")),
           "closed": rc.get("judge") == "闭合", "judge": rc.get("judge", ""), "dev": _num(rc.get("dev"))}

    # pending
    m1p = os.path.join(jd, "m1_conflicts.json")
    pending = []
    if os.path.exists(m1p):
        m1 = read_json(m1p)
        for c in m1.get("conflicts", []) or []:
            st = str(c.get("status", ""))
            if any(k in st for k in ("待", "复核", "材料")):
                pending.append({"id": c.get("id"), "item": c.get("item"), "status": c.get("status"), "grade": c.get("grade")})

    # gate C2–C7
    gate = {}
    gp = os.path.join(BASE, "设计层校验报告.json")
    if os.path.exists(gp):
        gd = read_json(gp)
        for x in gd.get("items", []) or []:
            if x.get("project") == proj:
                item = x.get("item", "")
                for g in ("C2", "C3", "C4", "C5", "C6", "C7"):
                    if item.startswith(g):
                        gate[g] = x.get("status")

    # 六大类比率：5 类由三表公式算 + 第 6 类成长能力取自 01.json 比率表
    ratios_hist = skill.compute_ratios(fin)
    rmap = d01.get("financial", {}).get("ratios", {})
    growth = {}
    for field, key in (("revenue_growth", "营业收入增速"), ("net_income_growth", "净利润增速")):
        series = rmap.get(key)
        if isinstance(series, dict) and isinstance(series.get("values"), dict) and series["values"]:
            k = sorted(series["values"])[-1]
            v = series["values"][k]
            growth[field] = v if isinstance(v, (int, float)) else None
    ratios_hist["growth"] = growth

    # 假设差异：材料年度假设 vs 模块最新期实际（期间口径不同，仅标记）
    adiff = skill.assumption_diff(mat, mod)
    for a in adiff:
        if a.get("note"):
            a["note"] = "材料 2026E 年度假设 vs 模块最新期（%s）实际，期间口径不同；差异仅标记，待人工复核" % (latest,)

    return {
        "schema": "m1.valuation-service/v2",
        "project_id": d01.get("project", {}).get("project_id", proj),
        "project_name": proj,
        "currency": "CNY",
        "data_cutoff": d01.get("project", {}).get("data_cutoff"),
        "method": valuation.get("估值方法", ""),
        "financial_inputs": {"period": latest, "is": fin["is"], "bs": fin["bs"], "cf": fin["cf"]},
        "ratios": {
            "historical": ratios_hist,
            "forecast": build_forecast_ratios(d02),
        },
        "dcf": skill.compute_dcf(di),
        "comparable": comparable,
        "irr": irr,
        "assumption_diff": adiff,
        "pending": pending,
        "gate": gate,
    }


def _project_summary(rec):
    return {
        "project_id": rec["project_id"],
        "project_name": rec["project_name"],
        "dcf": {"status": rec["dcf"]["status"], "missing": rec["dcf"]["missing"]},
        "comparable": {
            "ps": rec["comparable"]["ps"].get("status"),
            "pe": rec["comparable"]["pe"].get("status"),
        },
        "irr_closed": rec["irr"]["closed"],
        "assumption_diff": len(rec["assumption_diff"]),
        "gate": rec["gate"],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Turn 01/02/04 workpaper JSON into one unified valuation-service schema.")
    parser.add_argument("--base", default=DEFAULT_BASE,
                        help="Directory containing <project>/json/{01,02,04}.json (default: $DSH_VALUATION_BASE or ./数据底表)")
    parser.add_argument("--projects", help="Comma-separated project directory names (default: auto-discover)")
    parser.add_argument("--write", action="store_true", help="Write the aggregate JSON file")
    parser.add_argument("--out", help="Output path for the aggregate file (default: <base>/valuations-service.json)")
    parser.add_argument("--json", action="store_true", help="Print a machine-readable summary to stdout")
    args = parser.parse_args(argv)

    global BASE, DATE
    BASE = os.path.abspath(os.path.expanduser(args.base))
    DATE = datetime.date.today().isoformat()

    if not os.path.isdir(BASE):
        payload = {"ok": False, "error": "base directory not found: %s" % BASE}
        print(json.dumps(payload, ensure_ascii=False) if args.json else "错误：%s" % payload["error"])
        return 2

    if args.projects:
        projects = [p.strip() for p in args.projects.split(",") if p.strip()]
    else:
        projects = discover_projects()
        if not args.json:
            print("自动发现项目（%d 个）：%s" % (len(projects), "、".join(projects)))

    agg, summaries, errors = [], [], []
    for proj in projects:
        try:
            rec = build_unified(proj)
        except Exception as e:  # one bad workpaper must not hide the rest
            errors.append({"project": proj, "error": str(e)})
            if not args.json:
                print("%s | ERROR: %s" % (proj, e))
            continue
        agg.append(rec)
        summaries.append(_project_summary(rec))
        if not args.json:
            print("%s | dcf=%s missing=%s | comparable ps=%s pe=%s | irr closed=%s | assumption_diff=%d | gate=%s" % (
                proj, rec["dcf"]["status"], len(rec["dcf"]["missing"]),
                rec["comparable"]["ps"].get("status"), rec["comparable"]["pe"].get("status"),
                rec["irr"]["closed"], len(rec["assumption_diff"]), rec["gate"]))

    out_path = args.out or os.path.join(BASE, "valuations-service.json")
    written = False
    if args.write:
        out_dir = os.path.dirname(os.path.abspath(out_path))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump({
                "schema": "m1.valuation-service/aggregate/v2",
                "generated_at": DATE,
                "base": BASE,
                "projects": agg,
                "errors": errors,
            }, fh, ensure_ascii=False, indent=2)
        written = True

    if args.json:
        print(json.dumps({
            "ok": len(errors) == 0,
            "schema": "m1.valuation-service/aggregate/v2",
            "base": BASE,
            "output": os.path.abspath(out_path) if written else None,
            "written": written,
            "projects": summaries,
            "errors": errors,
        }, ensure_ascii=False))
    else:
        if written:
            print("\n已写回聚合文件：%s（不写每项目文件）" % out_path)
        else:
            print("\n(dry-run：未写回。加 --write 落盘)")
        if errors:
            print("有 %d 个项目失败：%s" % (len(errors), errors))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
