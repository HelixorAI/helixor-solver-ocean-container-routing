"""Render the manifest's declared visualizations as one self-contained HTML page.

The page is driven by `ui.visualizations` in the pack manifest, not by this
domain: each declaration binds to the request's records (entities, groups) and
the plan's placements, and a KPI names a rule and is evaluated from that rule's
own contract and parameters. Optional replay sections show the continuous
planning summary and the pinning scenarios when their JSON files exist.

    python -m ocean_routing report out/plan-adopted.json -o out/report.html
"""
from __future__ import annotations

import html
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

from .model import Plan

MANIFEST = Path(__file__).resolve().parent.parent / "pack/manifests/ocean_container_routing.yaml"


# -- binding the request to the manifest's names -------------------------------

class Bound:
    """The request's records under the manifest's decision names."""

    def __init__(self, manifest: Dict[str, Any], problem: Dict[str, Any], plan: Plan):
        decision = manifest["decision"]
        self.manifest = manifest
        for side in ("entities", "groups"):
            if decision[side].get("identity", "id") != "id":
                raise ValueError(f"report supports identity 'id' only, {side} declare {decision[side]['identity']!r}")
        self.entities: List[Dict[str, Any]] = problem[decision["entities"]["source"]]
        self.groups: List[Dict[str, Any]] = problem[decision["groups"]["source"]]
        self.group_by_id = {g["id"]: g for g in self.groups}
        self.plan = plan
        self.members: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for e in self.entities:
            if e["id"] in plan:
                self.members[plan[e["id"]]].append(e)

    def rows(self, name: str) -> List[Dict[str, Any]]:
        if name not in ("entities", "groups"):
            raise ValueError(f"visualization rows must be 'entities' or 'groups', got {name!r}")
        return self.entities if name == "entities" else self.groups

    def collect(self, group: Dict[str, Any], value: Dict[str, Any]) -> float:
        if value.get("collect") != "sum":
            raise ValueError(f"unsupported collect {value.get('collect')!r}")
        return float(sum(e[value["entity_attribute"]] for e in self.members.get(group["id"], [])))

    def field(self, entity: Dict[str, Any], path: str) -> Any:
        if path.startswith("group."):
            group = self.group_by_id.get(self.plan.get(entity["id"], ""))
            return None if group is None else group.get(path[len("group."):])
        return entity.get(path)


# -- rule measures: evaluated from the rule's contract, not its name -----------

def _measure(b: Bound, rule_id: str, measure: str) -> float:
    rule = b.manifest["constraints"][rule_id]
    contract, p = rule["contract"], rule.get("parameters") or {}
    if contract == "assigned_in_entity_set":
        ok = [b.field(e, f"group.{p['group_attribute']}") in (e.get(p["entity_attribute"]) or []) for e in b.entities]
        if measure == "satisfied_share":
            return sum(ok) / len(ok) if ok else 1.0
        if measure == "violated_count":
            return float(len(ok) - sum(ok))
    elif contract == "aggregate":
        value = {"collect": p["collect"], "entity_attribute": p["entity_attribute"]}
        pairs = [(b.collect(g, value), float(g[p["group_attribute"]])) for g in b.groups
                 if b.members.get(g["id"]) or p.get("empty_groups") == "judge"]
        if measure == "fill_share" and p["compare"] == ">=":
            total = sum(limit for _, limit in pairs)
            return sum(min(got, limit) for got, limit in pairs) / total if total else 1.0
        if measure == "excess":
            if p["compare"] == "<=":
                return sum(max(0.0, got - limit) for got, limit in pairs)
            return sum(max(0.0, limit - got) for got, limit in pairs)
    elif contract == "group_fits" and measure == "excess":
        return float(sum(max(0, b.field(e, f"group.{p['group_attribute']}") - e[p["entity_attribute"]])
                         for e in b.entities if e["id"] in b.plan))
    raise ValueError(f"measure {measure!r} is not defined for contract {contract!r} (rule {rule_id})")


def _violates(b: Bound, rule_id: str, entity: Dict[str, Any]) -> bool:
    rule = b.manifest["constraints"][rule_id]
    p = rule.get("parameters") or {}
    if rule["contract"] != "assigned_in_entity_set":
        raise ValueError(f"row filter by rule needs a per-entity contract, {rule_id} is {rule['contract']}")
    return b.field(entity, f"group.{p['group_attribute']}") not in (entity.get(p["entity_attribute"]) or [])


# -- rendering -----------------------------------------------------------------

def _e(v: Any) -> str:
    return html.escape(str(v))


def _num(v: float, fmt: Optional[str] = None, unit: Optional[str] = None) -> str:
    if fmt == "percent":
        return f"{v * 100:.1f}%"
    text = f"{v:,.0f}" if abs(v - round(v)) < 1e-9 else f"{v:,.1f}"
    return f"{text} {unit}" if unit else text


def _table(headers: List[str], rows: List[List[Any]], caption: str = "Table view") -> str:
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{_e(c)}</td>" for c in r) + "</tr>" for r in rows)
    return (f"<details class='tv'><summary>{_e(caption)}</summary><div class='scroll'>"
            f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div></details>")


def render_kpi(b: Bound, viz: Dict[str, Any]) -> str:
    tiles = []
    for m in viz["metrics"]:
        v = _measure(b, m["rule"], m["measure"])
        status = m.get("status") if v > 0 else None
        icon = "<span class='icon' aria-hidden='true'>!</span>" if status else ""
        rule = b.manifest["constraints"][m["rule"]]["name"]
        tiles.append(f"<div class='tile {status or ''}'><div class='label'>{_e(m['label'])}</div>"
                     f"<div class='value'>{icon}{_e(_num(v, m.get('format'), m.get('unit')))}</div>"
                     f"<div class='hint'>rule: {_e(rule)}</div></div>")
    return f"<div class='tiles'>{''.join(tiles)}</div>"


def render_stacked_bar(b: Bound, viz: Dict[str, Any]) -> str:
    rows = sorted(b.rows(viz["rows"]), key=lambda g: (g[viz["group_by"]], g[viz.get("sort_by", "id")]))
    scale = max([float(g[viz["limit"]]) for g in rows] + [b.collect(g, viz["value"]) for g in rows] + [1.0])
    bar_h, gap, label_w, width = 16, 6, 150, 560
    px = lambda v: v / scale * (width - label_w - 60)
    y, parts, lane, table_rows = 0, [], None, []
    for g in rows:
        if g[viz["group_by"]] != lane:
            lane = g[viz["group_by"]]
            y += 8
            parts.append(f"<text x='0' y='{y + 12}' class='grp'>{_e(lane)}</text>")
            y += 20
        load, alloc, limit = b.collect(g, viz["value"]), float(g[viz["allocation"]]), float(g[viz["limit"]])
        confirmed = float(g[viz["confirmed"]]) if viz.get("confirmed") else limit
        used = min(load, alloc)
        extra_ok = max(0.0, min(load, confirmed) - alloc)          # beyond allocation, acknowledged
        extra_unack = max(0.0, load - max(alloc, confirmed))        # beyond what the carrier acknowledged
        unused = max(0.0, alloc - load)
        held = max(0.0, confirmed - max(load, alloc))                # acknowledged, idle, kept
        spot = extra_ok + extra_unack
        tip = (f"{g[viz['row_label']]}: {load:g} FEU booked, allocation {alloc:g}, acknowledged {confirmed:g}, "
               f"space on sale {limit:g}" + (f", {extra_unack:g} FEU not yet acknowledged" if extra_unack else "")
               + (f", {held:g} FEU held for later" if held else ""))
        x0 = label_w
        segs = []
        cursor = x0
        for amount, cls, solid in ((used, "s-used", True), (extra_ok, "s-spot", True), (extra_unack, "s-unack", True),
                                   (unused, "s-unused", False), (held, "s-held", False)):
            if amount <= 0:
                continue
            w = max(px(amount) - (2 if solid else 1), 1)
            y0, h = (y, bar_h) if solid else (y + 0.5, bar_h - 1)
            segs.append(f"<rect x='{cursor:.1f}' y='{y0}' width='{w:.1f}' height='{h}' rx='3' class='{cls}'/>")
            cursor += px(amount)
        segs.append(f"<line x1='{x0 + px(limit):.1f}' x2='{x0 + px(limit):.1f}' y1='{y - 2}' y2='{y + bar_h + 2}' class='limit'/>")
        parts.append(f"<g class='hit' data-tip='{_e(tip)}'><rect x='0' y='{y - gap / 2}' width='{width}' height='{bar_h + gap}' class='hitbox'/>"
                     f"<text x='{label_w - 8}' y='{y + 12}' class='lbl' text-anchor='end'>{_e(g[viz['row_label']])}</text>"
                     f"{''.join(segs)}<text x='{x0 + px(max(load, alloc, limit)) + 6:.1f}' y='{y + 12}' class='val'>{load:g}/{alloc:g}</text></g>")
        table_rows.append([g[viz["row_label"]], lane, load, alloc, confirmed, limit, spot, extra_unack, unused, held])
        y += bar_h + gap
    legend = ("<div class='legend'><span><i class='k s-used'></i>Allocation used</span>"
              "<span><i class='k s-spot'></i>Beyond allocation, acknowledged</span>"
              "<span><i class='k s-unack'></i>Beyond allocation, not yet acknowledged</span>"
              "<span><i class='k s-unused'></i>Allocation left unused</span>"
              "<span><i class='k s-held'></i>Acknowledged space held for later</span>"
              "<span><i class='k limit-k'></i>Space on sale</span></div>")
    hatch = ("<defs><pattern id='hatch' width='5' height='5' patternUnits='userSpaceOnUse' patternTransform='rotate(45)'>"
             "<rect width='2' height='5' class='s-spot'/></pattern></defs>")
    svg = f"<svg viewBox='0 0 {width} {y + 4}' role='img' aria-label='{_e(viz['name'])}'>{hatch}{''.join(parts)}</svg>"
    return legend + svg + _table(["Sailing", "Lane", "Booked FEU", "Allocation", "Acknowledged", "Space on sale",
                                  "Beyond allocation", "Not acknowledged", "Unused allocation", "Held idle"], table_rows)


def render_bar(b: Bound, viz: Dict[str, Any]) -> str:
    used: Dict[Tuple[Any, Any], float] = defaultdict(float)
    alloc: Dict[Tuple[Any, Any], float] = defaultdict(float)
    for g in b.rows(viz["rows"]):
        key = (g[viz["x_axis"]], g[viz["group_by"]])
        cap = float(g[viz["value"]["cap"]])
        used[key] += min(b.collect(g, viz["value"]), cap)
        alloc[key] += float(g[viz["reference"]])
    xs = sorted({k[0] for k in alloc if alloc[k] > 0})
    series = sorted({k[1] for k in alloc if alloc[k] > 0})
    width, height, left, bottom = 560, 200, 36, 28
    slot = (width - left) / max(len(xs), 1)
    bw = min(18.0, (slot - 12) / max(len(series), 1))
    parts = [f"<line x1='{left}' x2='{width}' y1='{height - bottom - (height - bottom - 10) * t}' "
             f"y2='{height - bottom - (height - bottom - 10) * t}' class='grid'/>"
             f"<text x='{left - 6}' y='{height - bottom - (height - bottom - 10) * t + 4}' class='axis' text-anchor='end'>{int(t * 100)}%</text>"
             for t in (0, 0.5, 1)]
    rows = []
    for i, x in enumerate(xs):
        x0 = left + i * slot + (slot - bw * len(series)) / 2
        for j, s in enumerate(series):
            if not alloc.get((x, s)):
                continue
            share = used[(x, s)] / alloc[(x, s)]
            h = (height - bottom - 10) * share
            tip = f"week {x}, {s}: {used[(x, s)]:g} of {alloc[(x, s)]:g} FEU allocated used ({share * 100:.0f}%)"
            parts.append(f"<g class='hit' data-tip='{_e(tip)}'><rect x='{x0 + j * bw:.1f}' y='{height - bottom - h:.1f}' "
                         f"width='{bw - 2:.1f}' height='{max(h, 1):.1f}' rx='3' class='c{j + 1}'/></g>")
            rows.append([x, s, used[(x, s)], alloc[(x, s)], f"{share * 100:.1f}%"])
        parts.append(f"<text x='{left + i * slot + slot / 2:.1f}' y='{height - 8}' class='axis' text-anchor='middle'>week {x}</text>")
    legend = "<div class='legend'>" + "".join(f"<span><i class='k c{j + 1}'></i>{_e(s)}</span>" for j, s in enumerate(series)) + "</div>"
    svg = f"<svg viewBox='0 0 {width} {height}' role='img' aria-label='{_e(viz['name'])}'>{''.join(parts)}</svg>"
    return legend + svg + _table(["Week", viz["group_by"].title(), "Allocation used", "Allocated", "Share"], rows)


def render_gantt(b: Bound, viz: Dict[str, Any]) -> str:
    rows = b.rows(viz["rows"])
    lanes = sorted({g[viz["lanes"]] for g in rows})
    lo = min(g[viz["start"]] for g in rows)
    hi = max(g[viz["end"]] for g in rows)
    width, left, lane_h = 560, 110, 46
    px = lambda d: left + (d - lo) / max(hi - lo, 1) * (width - left - 10)
    parts, table_rows = [], []
    step = 7 if hi - lo <= 42 else 14
    for d in range(lo - lo % step + step, hi + 1, step):
        parts.append(f"<line x1='{px(d):.1f}' x2='{px(d):.1f}' y1='0' y2='{len(lanes) * lane_h}' class='grid'/>"
                     f"<text x='{px(d):.1f}' y='{len(lanes) * lane_h + 14}' class='axis' text-anchor='middle'>day {d}</text>")
    for i, lane in enumerate(lanes):
        on_lane = sorted((g for g in rows if g[viz["lanes"]] == lane), key=lambda g: g[viz["start"]])
        parts.append(f"<text x='0' y='{i * lane_h + 26}' class='lbl'>{_e(lane)}</text>")
        tracks: List[int] = []
        for g in on_lane:
            t = next((k for k, end in enumerate(tracks) if end < g[viz["start"]]), len(tracks))
            if t == len(tracks):
                tracks.append(0)
            tracks[t] = g[viz["end"]]
            y = i * lane_h + 6 + (t % 4) * 9
            load = b.collect(g, viz["value"])
            cls = "s-used" if load else "s-empty"
            tip = f"{g[viz['label']]}: departs day {g[viz['start']]}, arrives day {g[viz['end']]}, {load:g} FEU booked"
            parts.append(f"<g class='hit' data-tip='{_e(tip)}'><rect x='{px(g[viz['start']]):.1f}' y='{y}' "
                         f"width='{max(px(g[viz['end']]) - px(g[viz['start']]) - 2, 2):.1f}' height='7' rx='3' class='{cls}'/></g>")
            table_rows.append([g[viz["label"]], lane, g[viz["start"]], g[viz["end"]], load])
    legend = "<div class='legend'><span><i class='k s-used'></i>Sailing with bookings</span><span><i class='k s-empty'></i>No bookings</span></div>"
    svg = f"<svg viewBox='0 0 {width} {len(lanes) * lane_h + 20}' role='img' aria-label='{_e(viz['name'])}'>{''.join(parts)}</svg>"
    return legend + svg + _table(["Sailing", "Lane", "Departs", "Arrives", "Booked FEU"], table_rows)


def render_table(b: Bound, viz: Dict[str, Any]) -> str:
    rows = b.rows(viz["rows"])
    flt = viz.get("filter")
    if flt:
        rows = [r for r in rows if _violates(b, flt["rule"], r) == flt.get("violated", True)]
    cols = viz["columns"]
    body = [[b.field(r, c) for c in cols] for r in rows]
    head = "".join(f"<th>{_e(c)}</th>" for c in cols)
    trs = "".join("<tr>" + "".join(f"<td>{_e(v)}</td>" for v in r) + "</tr>" for r in body)
    return (f"<p class='hint'>{len(body)} rows</p><div class='scroll'><table><thead><tr>{head}</tr></thead>"
            f"<tbody>{trs}</tbody></table></div>")


RENDERERS: Dict[str, Callable[[Bound, Dict[str, Any]], str]] = {
    "kpi": render_kpi, "stacked_bar": render_stacked_bar, "bar": render_bar,
    "gantt": render_gantt, "table": render_table,
}


def render_visualizations(manifest: Dict[str, Any], problem: Dict[str, Any], plan: Plan) -> List[Tuple[Dict[str, Any], str]]:
    b = Bound(manifest, problem, plan)
    out = []
    for viz in (manifest.get("ui") or {}).get("visualizations") or []:
        renderer = RENDERERS.get(viz["type"])
        if renderer is None:
            raise ValueError(f"visualization {viz.get('id')}: no renderer for type {viz['type']!r}")
        out.append((viz, renderer(b, viz)))
    return out


# -- replay sections (outputs of the continuous and pinning commands) ----------

def _continuous_section(summary: List[Dict[str, Any]]) -> str:
    engines = []
    for c in summary:
        for r in c["rows"]:
            if r["solver"] not in engines:
                engines.append(r["solver"])
    charts = []
    for metric, label, fmt in (("penalty", "Penalty (lower is better)", "{:,.0f}"), ("moved", "Containers moved from the previous plan", "{}")):
        values = [r[metric] for c in summary for r in c["rows"] if r.get(metric) is not None]
        top = max(values + [1])
        width, height, left, bottom = 560, 180, 10, 26
        slot = (width - left) / len(summary)
        bw = min(22.0, (slot - 16) / len(engines))
        parts = [f"<line x1='{left}' x2='{width}' y1='{height - bottom}' y2='{height - bottom}' class='grid'/>"]
        for i, c in enumerate(summary):
            x0 = left + i * slot + (slot - bw * len(engines)) / 2
            for r in c["rows"]:
                j = engines.index(r["solver"])
                v = r.get(metric)
                if v is None:
                    continue
                h = (height - bottom - 16) * v / top
                tip = f"cycle {c['cycle']} (day {c['today']}), {r['solver']}: {fmt.format(v)}"
                parts.append(f"<g class='hit' data-tip='{_e(tip)}'><rect x='{x0 + j * bw:.1f}' y='{height - bottom - h:.1f}' "
                             f"width='{bw - 2:.1f}' height='{max(h, 1):.1f}' rx='3' class='c{j + 1}'/></g>")
            parts.append(f"<text x='{left + i * slot + slot / 2:.1f}' y='{height - 8}' class='axis' text-anchor='middle'>cycle {c['cycle']}, day {c['today']}</text>")
        charts.append(f"<h3>{_e(label)}</h3><svg viewBox='0 0 {width} {height}' role='img' aria-label='{_e(label)}'>{''.join(parts)}</svg>")
    space_rows = [[c["cycle"], c["today"], f"{c['space']['confirmed_feu']:g}", f"{c['space']['rejected_feu']:g}",
                   f"{c['space']['requested_feu']:g}", f"{c['space']['withdrawn_feu'] + c['space']['released_feu']:g}",
                   f"{c['space']['held_idle_feu']:g}"] for c in summary if c.get("space")]
    space = ""
    if space_rows:
        head = "".join(f"<th>{h}</th>" for h in ["Cycle", "Day", "Carrier confirmed", "Carrier rejected",
                                                    "We requested", "We gave back", "Held idle for later"])
        body = "".join("<tr>" + "".join(f"<td>{_e(v)}</td>" for v in r) + "</tr>" for r in space_rows)
        space = (f"<h3>Space beyond the allocation (FEU)</h3><div class='scroll'><table><thead><tr>{head}</tr></thead>"
                 f"<tbody>{body}</tbody></table></div>")
    legend = "<div class='legend'>" + "".join(f"<span><i class='k c{j + 1}'></i>{_e(e)}</span>" for j, e in enumerate(engines)) + "</div>"
    rows = [[c["cycle"], c["today"], r["solver"], f"{r['penalty']:,.0f}", f"{r['late']} ({r['days_late']})",
             f"{r['utilization_pct']}%", r["spot_feu"], r["moved"], r["seconds"]] for c in summary for r in c["rows"]]
    return space + legend + "".join(charts) + _table(["Cycle", "Day", "Engine", "Penalty", "Late (days)", "Allocation used", "Spot FEU", "Moved", "Seconds"], rows)


def _pinning_section(rows: List[Dict[str, Any]]) -> str:
    out = [[r["case"], ", ".join(f"{n} {m}" for m, n in sorted(r["pins"].items())) or "none", r["outcome"],
            "" if r["penalty"] is None else f"{r['penalty']:,.0f}", "" if r["moved"] is None else r["moved"],
            "" if r.get("optimum_penalty") is None else f"{r['optimum_penalty']:,.0f}"] for r in rows]
    head = "".join(f"<th>{h}</th>" for h in ["Scenario", "Pins", "Outcome", "Penalty", "Moved", "Optimum, same pins"])
    body = "".join("<tr>" + "".join(f"<td>{_e(v)}</td>" for v in r) + "</tr>" for r in out)
    return f"<div class='scroll'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


CSS = """
:root{--surface:#fcfcfb;--panel:#ffffff;--border:#e4e3df;--text:#0b0b0b;--text2:#52514e;--muted:#7a7974;
--c1:#2a78d6;--c2:#eb6834;--c3:#1baf7a;--c4:#eda100;--critical:#c62828;--grid:#e4e3df;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--surface:#1a1a19;--panel:#222221;--border:#3a3a38;
--text:#f0efec;--text2:#c3c2b7;--muted:#9a998f;--c1:#3987e5;--c2:#d95926;--c3:#199e70;--c4:#c98500;--critical:#ef6461;--grid:#3a3a38;color-scheme:dark}}
:root[data-theme="dark"]{--surface:#1a1a19;--panel:#222221;--border:#3a3a38;--text:#f0efec;--text2:#c3c2b7;--muted:#9a998f;
--c1:#3987e5;--c2:#d95926;--c3:#199e70;--c4:#c98500;--critical:#ef6461;--grid:#3a3a38;color-scheme:dark}
*{box-sizing:border-box}body{margin:0;background:var(--surface);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:980px;margin:0 auto;padding:24px 16px 64px}h1{font-size:26px;margin:0 0 4px}h2{font-size:19px;margin:0 0 4px}h3{font-size:15px;margin:14px 0 4px;color:var(--text2)}
.sub,.hint{color:var(--text2);font-size:13px;margin:0 0 10px}section{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:16px;margin:16px 0}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}.tile{border:1px solid var(--border);border-radius:8px;padding:10px 12px}
.tile .label{color:var(--text2);font-size:13px}.tile .value{font-size:24px;font-weight:600}.tile.critical .value{color:var(--critical)}
.icon{display:inline-grid;place-items:center;width:20px;height:20px;border-radius:50%;background:var(--critical);color:#fff;font-size:13px;margin-right:6px;vertical-align:3px}
svg{width:100%;height:auto;display:block;overflow:visible}text{font-size:11px;fill:var(--text2)}text.grp{font-weight:600;fill:var(--text)}text.val{fill:var(--muted)}
.s-used,.c1{fill:var(--c1)}.s-spot,.c2{fill:var(--c2)}.c3{fill:var(--c3)}.c4{fill:var(--c4)}.s-unused{fill:none;stroke:var(--muted);stroke-dasharray:3 2}
.s-empty{fill:none;stroke:var(--muted)}.s-unack{fill:url(#hatch);stroke:var(--c2);stroke-width:1}.s-held{fill:none;stroke:var(--c2);stroke-dasharray:3 2}.limit{stroke:var(--text);stroke-width:2}.grid{stroke:var(--grid)}.hitbox{fill:transparent}.hit:hover rect:not(.hitbox){opacity:.8}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:13px;color:var(--text2);margin:6px 0 8px}.legend i.k{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:6px;vertical-align:-1px}
i.k.s-used,i.k.c1{background:var(--c1)}i.k.s-spot,i.k.c2{background:var(--c2)}i.k.c3{background:var(--c3)}i.k.c4{background:var(--c4)}
i.k.s-unused,i.k.s-empty{border:1px dashed var(--muted)}i.k.s-held{border:1px dashed var(--c2)}
i.k.s-unack{background:repeating-linear-gradient(45deg,var(--c2) 0 2px,transparent 2px 5px);border:1px solid var(--c2)}i.k.limit-k{width:3px;background:var(--text)}
.scroll{overflow:auto;max-height:420px}table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:4px 8px;border-bottom:1px solid var(--border);white-space:nowrap}
th{color:var(--text2);font-weight:600}details.tv{margin-top:8px}summary{cursor:pointer;color:var(--text2);font-size:13px}
#tip{position:fixed;pointer-events:none;background:var(--text);color:var(--surface);padding:5px 8px;border-radius:6px;font-size:12px;display:none;max-width:320px;z-index:9}
"""

JS = """
const tip=document.getElementById('tip');
document.querySelectorAll('.hit').forEach(g=>{g.addEventListener('mousemove',e=>{tip.textContent=g.dataset.tip;tip.style.display='block';
tip.style.left=Math.min(e.clientX+12,innerWidth-330)+'px';tip.style.top=(e.clientY+12)+'px'});g.addEventListener('mouseleave',()=>tip.style.display='none')});
"""


def build_report(problem: Dict[str, Any], plan: Plan, *, title: str, subtitle: str,
                 continuous: Optional[List[Dict[str, Any]]] = None,
                 pinning: Optional[List[Dict[str, Any]]] = None,
                 manifest: Optional[Dict[str, Any]] = None) -> str:
    manifest = manifest or yaml.safe_load(MANIFEST.read_text())
    sections = [f"<section><h2>{_e(v['name'])}</h2>"
                f"{'<p class=sub>' + _e(v['description']) + '</p>' if v.get('description') else ''}{body}</section>"
                for v, body in render_visualizations(manifest, problem, plan)]
    if continuous:
        sections.append("<section><h2>Continuous planning replay</h2><p class='sub'>Each cycle repairs the previous plan "
                        "after its events. The same request goes to every engine; the from-scratch run ignores the prior plan.</p>"
                        + _continuous_section(continuous) + "</section>")
    if pinning:
        sections.append("<section><h2>Pinning scenarios</h2><p class='sub'>One disruption, one base plan, one pin choice per run.</p>"
                        + _pinning_section(pinning) + "</section>")
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Ocean Routing Plan</title><style>{CSS}</style></head><body><main><h1>{_e(title)}</h1>"
            f"<p class='sub'>{_e(subtitle)}</p>{''.join(sections)}</main><div id='tip' role='tooltip'></div><script>{JS}</script></body></html>")


def load_json(path: Path) -> Optional[Any]:
    return json.loads(path.read_text()) if path.is_file() else None
