#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""估值技能（公式化 · 确定性）——valuation_skill。

单一技能库，包含全部估值方法的确定性公式（不随项目变化，只有「方法」是参数）：
  1. 比率（六大类）：盈利能力 / 成长能力 / 偿债能力 / 营运能力 / 现金流 / 估值
     —— 公式移植自 anthropics/claude-cookbooks `analyzing-financial-statements/calculate_ratios.py`，
        并按中文三表字段（净利润/营业收入/资产总计…）落位。
  2. DCF：FCFF 折现 + 永续终值（WACC / g / capex / D&A / ΔWC → TV → EV → 权益价值）。
     输入缺什么就标什么（status=missing），字段位置始终保留。
  3. 可比估值：PS 与 PE 两套并行（trimmed mean / 1y mean / 投前倍数 / 溢价）。
  4. IRR：给定投资额/退出价值/年限 → IRR，三档（保守/中性/乐观）。
  5. 假设差异：材料假设 vs 模块按实际数据计算的假设，逐字段标记差异。

本文件是「技能」，纯公式、无文件 I/O、无项目硬编码。
"""
from __future__ import annotations

import math


def _num(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return None


def _safe_div(a, b, default=None):
    a, b = _num(a), _num(b)
    if a is None or b is None or b == 0:
        return default
    return a / b


def _trimmed_mean(values, trim_ratio=0.10):
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    vals = sorted(vals)
    n = len(vals)
    k = max(1, int(round(n * trim_ratio))) if n >= 5 else 0
    if k > 0 and n - 2 * k > 0:
        vals = vals[k:-k]
    return sum(vals) / len(vals)


# --------------------------------------------------------------------------- #
# 1. 比率（六大类）
# --------------------------------------------------------------------------- #

def compute_ratios(fin):
    """从标准化三表数据计算六大类比率。fin = {is:{}, bs:{}, cf:{}}，键为英文标准字段。"""
    is_ = fin.get("is", {})
    bs = fin.get("bs", {})
    cf = fin.get("cf", {})
    rev = _num(is_.get("revenue"))
    cogs = _num(is_.get("cogs"))
    ni = _num(is_.get("net_income"))
    gp = _num(is_.get("gross_profit")) if is_.get("gross_profit") is not None else (rev - cogs if rev is not None and cogs is not None else None)
    op_income = _num(is_.get("operating_income"))
    ebit = _num(is_.get("ebit"))
    ebitda = _num(is_.get("ebitda"))
    interest = _num(is_.get("interest_expense"))

    ta = _num(bs.get("total_assets"))
    ca = _num(bs.get("current_assets"))
    cl = _num(bs.get("current_liabilities"))
    inv = _num(bs.get("inventory"))
    ar = _num(bs.get("accounts_receivable"))
    ap = _num(bs.get("accounts_payable"))
    cash = _num(bs.get("cash_and_equivalents"))
    equity = _num(bs.get("shareholders_equity"))
    debt = _num(bs.get("total_debt"))

    ocf = _num(cf.get("operating_cash_flow"))
    capex = _num(cf.get("capex"))

    out = {}
    # 盈利能力
    out["profitability"] = {
        "gross_margin": _safe_div(gp, rev),
        "operating_margin": _safe_div(op_income, rev),
        "net_margin": _safe_div(ni, rev),
        "roe": _safe_div(ni, equity),
        "roa": _safe_div(ni, ta),
    }
    # 偿债能力
    out["solvency"] = {
        "current_ratio": _safe_div(ca, cl),
        "quick_ratio": _safe_div(ca - inv if (ca is not None and inv is not None) else None, cl),
        "cash_ratio": _safe_div(cash, cl),
        "debt_to_equity": _safe_div(debt, equity),
        "debt_to_assets": _safe_div(debt, ta),
        "interest_coverage": _safe_div(ebit, interest),
    }
    # 营运能力
    out["efficiency"] = {
        "asset_turnover": _safe_div(rev, ta),
        "inventory_turnover": _safe_div(cogs, inv),
        "receivables_turnover": _safe_div(rev, ar),
        "days_sales_outstanding": _safe_div(365, _safe_div(rev, ar)),
        "days_inventory_outstanding": _safe_div(365, _safe_div(cogs, inv)),
        "days_payables_outstanding": _safe_div(365, _safe_div(cogs, ap)),
    }
    # 现金流
    out["cash_flow"] = {
        "ocf_to_revenue": _safe_div(ocf, rev),
        "ocf_to_net_income": _safe_div(ocf, ni),
        "free_cash_flow": (ocf + capex) if (ocf is not None and capex is not None) else None,
    }
    # 估值（market_data 缺则留 None，供下游标记）
    mkt = fin.get("market_data", {})
    mcap = _num(mkt.get("market_cap"))
    if mcap is None:
        sp = _num(mkt.get("share_price")); so = _num(mkt.get("shares_outstanding"))
        mcap = sp * so if (sp is not None and so is not None) else None
    out["valuation"] = {
        "ps": _safe_div(mcap, rev),
        "pe": _safe_div(mcap, ni),
        "ev": (mcap + debt - cash) if (mcap is not None and debt is not None and cash is not None) else None,
        "ev_to_ebitda": _safe_div((mcap + debt - cash) if (mcap is not None and debt is not None and cash is not None) else None, ebitda),
    }
    return out


def compute_growth(hist_series):
    """成长能力：给定某指标的年度序列 {period: value}，计算逐年增速。"""
    keys = sorted(hist_series.keys())
    out = {}
    for i in range(1, len(keys)):
        prev, cur = _num(hist_series.get(keys[i - 1])), _num(hist_series.get(keys[i]))
        if prev and cur is not None:
            out[keys[i]] = (cur / prev) - 1
    return out


# --------------------------------------------------------------------------- #
# 2. DCF（预留全部字段位置；缺什么标 missing）
# --------------------------------------------------------------------------- #

def compute_dcf(d):
    """DCF 全链路公式；输入缺失即 status=missing，字段位置保留。

    d = {wacc, g, tax_rate, ebit, da, capex, wc_change, fcff_projection, net_debt}
    """
    wacc = _num(d.get("wacc")); g = _num(d.get("g"))
    tax = _num(d.get("tax_rate"))
    ebit = _num(d.get("ebit")); da = _num(d.get("da"))
    capex = _num(d.get("capex")); wc = _num(d.get("wc_change"))
    fcff_proj = d.get("fcff_projection")  # list[float]，预测期逐年 FCFF
    net_debt = _num(d.get("net_debt"))

    def slot(value, status="computed", note=None):
        return {"value": value, "status": status, "note": note}

    inputs = {
        "wacc": slot(wacc, "computed" if wacc is not None else "missing", "缺 WACC（02 估值参数为空）" if wacc is None else None),
        "g": slot(g, "computed" if g is not None else "missing", "缺永续增长率 g" if g is None else None),
        "tax_rate": slot(tax, "computed" if tax is not None else "missing"),
        "ebit": slot(ebit, "computed" if ebit is not None else "missing", "缺 EBIT（预测期未给出）" if ebit is None else None),
        "depreciation_amortization": slot(da, "computed" if da is not None else "missing", "缺折旧摊销" if da is None else None),
        "capex": slot(capex, "computed" if capex is not None else "missing", "缺资本开支（02 forecast.missing 已列）" if capex is None else None),
        "working_capital_change": slot(wc, "computed" if wc is not None else "missing", "缺营运资本变动" if wc is None else None),
    }
    fcff0 = None
    if ebit is not None and da is not None and capex is not None and wc is not None:
        t = tax if tax is not None else 0.0
        fcff0 = ebit * (1 - t) + da - capex - wc
    inputs["fcff"] = slot(fcff0, "computed" if fcff0 is not None else "missing",
                          "FCFF = EBIT×(1-t) + D&A − capex − ΔWC；缺项无法计算" if fcff0 is None else None)

    # 终值 + 企业价值 + 权益价值
    tv = None; ev = None; eq = None; tv_ratio = None
    if wacc is not None and g is not None and wacc != g and (fcff0 is not None or fcff_proj):
        f_last = fcff_proj[-1] if fcff_proj else fcff0
        if f_last is not None:
            tv = f_last * (1 + g) / (wacc - g)
            years = len(fcff_proj) if fcff_proj else 0
            pv_sum = 0.0
            for i, f in enumerate(fcff_proj or [fcff0], 1):
                pv_sum += f / (1 + wacc) ** i
            ev = pv_sum + tv / (1 + wacc) ** (years if years else 1)
    outputs = {
        "terminal_value": slot(tv, "computed" if tv is not None else "missing", "TV = FCFF_last×(1+g)/(WACC−g)；缺 WACC/g/FCFF" if tv is None else None),
        "terminal_value_ratio": slot(tv_ratio, "missing", "终值占退出年收入/EBITDA 比例，需退出年收入"),
        "enterprise_value": slot(ev, "computed" if ev is not None else "missing", "EV = ΣPV(FCFF) + PV(TV)"),
        "net_debt": slot(net_debt, "computed" if net_debt is not None else "missing", "缺净债务（有息负债−现金）"),
        "equity_value": slot(_safe_div(ev - net_debt, 1) if (ev is not None and net_debt is not None) else None,
                             "computed" if (ev is not None and net_debt is not None) else "missing",
                             "权益价值 = EV − 净债务"),
    }

    missing = [k for k, v in inputs.items() if v["status"] == "missing"]
    status = "computed" if not missing else ("partial" if any(v["status"] == "computed" for v in inputs.values()) else "unavailable")
    m3_handoff = {
        "note": "DCF 输入缺失项留空，由事实锚 / 外部研究补齐后作为模型预测；本服务只留位，不计算。",
        "anchors": {
            "wacc": "需补锚：无风险利率 / β / 股权风险溢价 / 资本结构",
            "g": "需补锚：长期 GDP 增速 / 行业终局增速",
            "capex": "需补锚：资本开支计划 / 在建工程 / 产能扩张",
            "depreciation_amortization": "需补锚：固定资产/无形资产折旧摊销政策",
            "working_capital_change": "需补锚：应收/存货/应付周转预测（营运资本变动）",
            "tax_rate": "需补锚：有效税率",
            "net_debt": "需补锚：有息负债 − 货币资金",
        },
    }
    return {"status": status, "inputs": inputs, "outputs": outputs, "missing": missing, "m3_handoff": m3_handoff}


# --------------------------------------------------------------------------- #
# 3. 可比估值（PS + PE 两套并行）
# --------------------------------------------------------------------------- #

def compute_comparable(ps_ttm=None, ps_1y=None, pe_ttm=None, pe_1y=None, pre_money=None, base_revenue=None, base_net=None, pe_assumption=False):
    """可比估值：PS 与 PE 都算，缺哪套标 missing；pe_assumption=True 表示 PE 来自估值假设而非市场倍数。"""
    def series(vals):
        return [_num(v) for v in (vals or []) if _num(v) is not None]

    out = {}
    ps = series(ps_ttm)
    if ps:
        pm = _safe_div(pre_money, base_revenue)
        out["ps"] = {
            "status": "computed",
            "trimmed_mean": round(_trimmed_mean(ps), 4),
            "all_mean": round(sum(ps) / len(ps), 4),
            "1y_mean": round(sum(series(ps_1y)) / len(series(ps_1y)), 4) if series(ps_1y) else None,
            "pre_money_multiple": round(pm, 4) if pm is not None else None,
            "premium": round(pm / (sum(ps) / len(ps)), 4) if (pm is not None and sum(ps)) else None,
            "premium_basis": "投前倍数 / 可比全样本均值（本服务口径）",
        }
    else:
        out["ps"] = {"status": "missing", "note": "可比公司无 PS 倍数（或未提供）"}

    pe = series(pe_ttm)
    if pe:
        pm = _safe_div(pre_money, base_net)
        out["pe"] = {
            "status": "assumption" if pe_assumption else "computed",
            "trimmed_mean": round(_trimmed_mean(pe), 4),
            "all_mean": round(sum(pe) / len(pe), 4),
            "1y_mean": round(sum(series(pe_1y)) / len(series(pe_1y)), 4) if series(pe_1y) else None,
            "pre_money_multiple": round(pm, 4) if pm is not None else None,
            "premium": round(pm / (sum(pe) / len(pe)), 4) if (pm is not None and sum(pe)) else None,
            "premium_basis": "投前倍数 / 可比全样本均值（本服务口径）",
            "note": "PE 倍数来自估值假设（退出 PE 区间），非可比公司市场倍数" if pe_assumption else None,
        }
    else:
        out["pe"] = {"status": "missing", "note": "可比公司无 PE 倍数（或项目亏损，PE 不适用）"}
    return out


# --------------------------------------------------------------------------- #
# 4. IRR
# --------------------------------------------------------------------------- #

def compute_irr(invest, exit_value, years):
    """IRR = (exit/invest)^(1/years) − 1。"""
    inv, ev, y = _num(invest), _num(exit_value), _num(years)
    if not inv or not ev or not y or inv <= 0 or ev <= 0 or y <= 0:
        return None
    return (ev / inv) ** (1 / y) - 1


# --------------------------------------------------------------------------- #
# 5. 假设差异（材料 vs 模块）
# --------------------------------------------------------------------------- #

def assumption_diff(material_assumptions, module_actual):
    """逐字段比较材料假设 vs 模块按实际数据算出的假设。

    material_assumptions: {field: value}（02 预测参数）
    module_actual: {field: value}（01 实际/模块计算）
    返回 [{field, material, module, diff, note}]
    """
    out = []
    for field in sorted(set(material_assumptions) | set(module_actual)):
        m = _num(material_assumptions.get(field))
        a = _num(module_actual.get(field))
        if m is None and a is None:
            continue
        diff = (a - m) if (m is not None and a is not None) else None
        note = None
        if diff is not None and abs(diff) > 1e-6:
            note = "材料假设与模块实际值有差异（标记，待人工复核）"
        out.append({"field": field, "material": m, "module": a, "diff": round(diff, 6) if diff is not None else None, "note": note})
    return out
