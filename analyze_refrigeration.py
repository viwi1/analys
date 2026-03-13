#!/usr/bin/env python3
"""
Kylkompressoranalys – beräknar kylbelastning och tillgänglig kapacitet från IWMAC CSV-loggar.
Stödjer flera systemtyper: MT (C1–C8), Frys (F1–F8), Komfort (H1–H4).
Huvudmål: Ta reda på tillgänglig kylkapacitet (marginal för ny last).
"""

import argparse
import json
import re
from pathlib import Path

import matplotlib.pyplot as plt
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, PageBreak
import numpy as np
import pandas as pd

SYSTEM_PREFIX_MAP = {"C": "MT", "F": "Frys", "H": "Komfort"}
HOURS_PER_YEAR = 8760


def validate_config_files(config: dict, input_dir: Path) -> None:
    """Kontrollera att alla filer i config finns."""
    for c in config["compressors"]:
        filepath = input_dir / c["file"]
        if not filepath.exists():
            raise FileNotFoundError(f"Fil hittades inte: {filepath}")


def load_config(path: Path) -> dict:
    """Läs och validera config.json."""
    with open(path, "r", encoding="utf-8") as f:
        config = json.load(f)
    for c in config["compressors"]:
        if not all(k in c for k in ("name", "capacity_kw", "file")):
            raise ValueError(f"Kompressor saknar obligatoriskt fält (name, capacity_kw, file): {c}")
        if c["capacity_kw"] <= 0:
            raise ValueError(f"capacity_kw måste vara > 0 för {c['name']}: {c['capacity_kw']}")
        c_type = c.get("type", "auto")
        if c_type not in ("inverter", "onoff", "auto"):
            raise ValueError(f"Ogiltig typ för {c['name']}: {c_type}")
    return config


def group_compressors_by_system(config: dict) -> dict[str, list[dict]]:
    """Gruppera kompressorer per system (MT, Frys, Komfort) baserat på namnprefix."""
    systems: dict[str, list[dict]] = {}
    for c in config["compressors"]:
        m = re.match(r"^([CFH])(\d+)$", str(c["name"]).strip(), re.IGNORECASE)
        if m:
            prefix = m.group(1).upper()
            system = SYSTEM_PREFIX_MAP.get(prefix, prefix)
        else:
            system = "MT"
        systems.setdefault(system, []).append(c)
    def _sort_key(c):
        m = re.match(r"^[CFH]?(\d+)$", str(c["name"]), re.IGNORECASE)
        return int(m.group(1)) if m else 0

    for comps in systems.values():
        comps.sort(key=_sort_key)
    return systems


def _read_iwmac_csv(filepath: Path, skip_rows: int = 1) -> pd.DataFrame:
    """Läs IWMAC CSV med robust hantering av encoding och felaktiga rader."""
    encodings = ("utf-8", "utf-8-sig", "latin-1", "cp1252")
    last_err = None
    for enc in encodings:
        try:
            return pd.read_csv(
                filepath,
                header=None,
                skiprows=skip_rows,
                encoding=enc,
                dtype=str,
                on_bad_lines="skip",
                engine="python",
            )
        except (pd.errors.ParserError, UnicodeDecodeError) as e:
            last_err = e
            continue
    raise last_err or pd.errors.ParserError("Kunde inte läsa CSV")


def _load_inverter_series(filepath: Path, cols: tuple[int, int]) -> pd.Series:
    df = _read_iwmac_csv(filepath)
    ts_col, val_col = cols
    df = df.iloc[:, [ts_col, val_col]].copy()
    df.columns = ["ts", "val"]
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    df["val"] = pd.to_numeric(df["val"], errors="coerce")
    df = df.dropna().sort_values("ts").drop_duplicates(subset=["ts"], keep="last")
    return (df.set_index("ts")["val"] / 100.0)


def _load_onoff_series(filepath: Path, cols: tuple[int, int]) -> pd.Series:
    df = _read_iwmac_csv(filepath)
    ts_col, val_col = cols
    df = df.iloc[:, [ts_col, val_col]].copy()
    df.columns = ["ts", "val"]
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    df["val"] = df["val"].map({"0": 0.0, "1": 1.0})
    df = df.dropna().sort_values("ts").drop_duplicates(subset=["ts"], keep="last")
    return df.set_index("ts")["val"]


def _load_raw_series(filepath: Path, cols: tuple[int, int]) -> pd.Series:
    """Läs råa numeriska värden (för auto-typidentifiering)."""
    df = _read_iwmac_csv(filepath)
    ts_col, val_col = cols
    df = df.iloc[:, [ts_col, val_col]].copy()
    df.columns = ["ts", "val"]
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    df["val"] = pd.to_numeric(df["val"], errors="coerce")
    df = df.dropna().sort_values("ts").drop_duplicates(subset=["ts"], keep="last")
    return df.set_index("ts")["val"]


def _infer_compressor_type(s: pd.Series, name: str, lead_name: str) -> str:
    """Värden i {0,1} → onoff, annars inverter. C1/F1/H1 (lead) alltid inverter."""
    if name == lead_name:
        return "inverter"
    uniq = set(s.dropna().round(6).unique())
    if uniq.issubset({0.0, 1.0}):
        return "onoff"
    return "inverter"


def load_and_merge_csv(
    compressors: list[dict], input_dir: Path, resample_min: int = 1
) -> tuple[pd.DataFrame, list[dict]]:
    """Läs CSV för angivna kompressorer. Returnerar (df, comps med typ satt)."""
    series_by_name: dict[str, pd.Series] = {}
    comps_with_type: list[dict] = []
    lead_name = compressors[0]["name"] if compressors else None

    for comp in compressors:
        name = comp["name"]
        comp_type = comp.get("type", "auto")
        filepath = input_dir / comp["file"]
        cols = tuple(comp.get("columns", [0, 1]))

        if comp_type == "inverter":
            s = _load_inverter_series(filepath, cols)
        elif comp_type == "onoff":
            s = _load_onoff_series(filepath, cols)
        else:
            s_raw = _load_raw_series(filepath, cols)
            comp_type = _infer_compressor_type(s_raw, name, lead_name)
            s = (s_raw / 100.0) if (comp_type == "inverter" and s_raw.max() > 1.0) else s_raw

        s = s.resample(f"{resample_min}min").ffill()
        series_by_name[name] = s
        comps_with_type.append({**comp, "type": comp_type})

    global_min = min(s.index.min() for s in series_by_name.values())
    global_max = max(s.index.max() for s in series_by_name.values())
    time_index = pd.date_range(global_min, global_max, freq=f"{resample_min}min")

    for name in series_by_name:
        s = series_by_name[name].reindex(time_index, method="ffill").fillna(0)
        series_by_name[name] = s

    df = pd.DataFrame(series_by_name, index=time_index)
    df.index.name = "timestamp"
    return df.reset_index(), comps_with_type


def compute_load(
    df: pd.DataFrame, compressors: list[dict]
) -> tuple[pd.DataFrame, float]:
    """Beräkna load_kw, available_kw, load_fraction. Returnerar (df, q_max)."""
    q_max = sum(c["capacity_kw"] for c in compressors)
    load_kw = pd.Series(0.0, index=df.index)

    for comp in compressors:
        name = comp["name"]
        cap = comp["capacity_kw"]
        load_kw = load_kw + df[name] * cap

    df = df.copy()
    df["load_kw"] = load_kw
    df["available_kw"] = q_max - load_kw
    df["load_fraction"] = load_kw / q_max

    if (df["load_kw"] > q_max * 1.1).any():
        print("Varning: last över nominell kapacitet upptäckt (kontrollera config eller loggdata)")

    return df, q_max


def compute_compressor_stats(df: pd.DataFrame, compressors: list[dict]) -> dict:
    stats = {}
    dt_h = df["timestamp"].diff().dt.total_seconds() / 3600
    dt_h = dt_h.fillna(dt_h.median())

    for comp in compressors:
        name = comp["name"]
        s = df[name]
        prev = s.shift(1).fillna(0)
        if comp["type"] == "onoff":
            starts = ((prev == 0) & (s >= 1)).sum()
            runtime_h = (s >= 1).astype(float).mul(dt_h).sum()
        else:
            threshold = 0.05
            starts = ((prev < threshold) & (s >= threshold)).sum()
            runtime_h = (s > 0).astype(float).mul(dt_h).sum()
        stats[name] = {"starts": int(starts), "runtime_hours": float(runtime_h)}
    return stats


def compute_system_stats(df: pd.DataFrame, q_max: float) -> tuple[dict, float]:
    load = df["load_kw"]
    frac = df["load_fraction"]
    dt = df["timestamp"].diff().dt.total_seconds() / 3600

    load_95 = float(np.percentile(load, 95))
    load_99 = float(np.percentile(load, 99))
    data_hours = float(dt.sum())
    if data_hours <= 0:
        data_hours = 1.0

    hours_over_90 = float((frac >= 0.9).astype(float).mul(dt).sum())
    hours_over_80 = float((frac >= 0.8).astype(float).mul(dt).sum())
    hours_at_full = float((frac >= 0.99).astype(float).mul(dt).sum())
    factor_to_year = HOURS_PER_YEAR / data_hours

    stats = {
        "q_max": q_max,
        "mean_load": float(load.mean()),
        "max_load": float(load.max()),
        "p95_load": load_95,
        "p99_load": load_99,
        "mean_available": float(df["available_kw"].mean()),
        "practical_available": q_max - load_95,
        "hours_over_90pct": hours_over_90,
        "hours_over_80pct": hours_over_80,
        "hours_at_full_load": hours_at_full,
        "hours_at_full_load_per_year": hours_at_full * factor_to_year,
        "hours_over_90pct_per_year": hours_over_90 * factor_to_year,
        "data_hours": data_hours,
    }
    return stats, float(dt.mean())


def compute_per_step_capacity(
    df: pd.DataFrame, compressors: list[dict], q_max: float
) -> list[dict]:
    """Beräkna driftlägen: aktiv kapacitet och tillgänglig per steg (C1, C1+C2, ...)."""
    comp_names = [c["name"] for c in compressors]
    capacities = {c["name"]: c["capacity_kw"] for c in compressors}

    steps = []
    for k in range(1, len(comp_names) + 1):
        active = comp_names[:k]
        step_cap = sum(capacities[n] for n in active)
        steps.append({
            "step": "+".join(active),
            "active_kw": step_cap,
            "available_kw": q_max - step_cap,
        })

    return steps


def build_histogram(df: pd.DataFrame, bin_size: float = 10) -> pd.DataFrame:
    load = df["load_kw"]
    dt = df["timestamp"].diff().dt.total_seconds() / 3600
    dt = dt.fillna(dt.median())

    max_load = load.max()
    bins_ceil = int(np.ceil(max_load / bin_size)) * bin_size
    edges = np.arange(0, bins_ceil + bin_size, bin_size)

    bins = pd.cut(load, edges, right=False, labels=range(len(edges) - 1))
    hist_hours = dt.groupby(bins, observed=True).sum().reindex(range(len(edges) - 1), fill_value=0).values

    return pd.DataFrame({
        "bin_min": edges[:-1],
        "bin_max": edges[1:],
        "hours": hist_hours,
    })


def write_system_outputs(
    df: pd.DataFrame,
    hist_df: pd.DataFrame,
    sys_stats: dict,
    comp_stats: dict,
    per_step: list[dict],
    output_dir: Path,
    system_name: str,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    df.to_csv(output_dir / "timeseries_with_load.csv", index=False, encoding="utf-8")
    hist_df.to_csv(output_dir / "histogram_load.csv", index=False, encoding="utf-8")

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.bar(
        (hist_df["bin_min"] + hist_df["bin_max"]) / 2,
        hist_df["hours"],
        width=hist_df["bin_max"] - hist_df["bin_min"] - 0.5,
        align="center",
        edgecolor="black",
        linewidth=0.5,
    )
    ax.set_xlabel("Last (kW)")
    ax.set_ylabel("Timmar")
    ax.set_title(f"{system_name} – Lastfördelning (låg last = hög tillgänglig kapacitet)")
    fig.tight_layout()
    fig.savefig(output_dir / "histogram_load.png", dpi=150, bbox_inches="tight")
    plt.close()

    # Vecka vs max kW och vecka vs tillgänglig kW
    df_plot = df.copy()
    df_plot["timestamp"] = pd.to_datetime(df_plot["timestamp"])
    cal = df_plot["timestamp"].dt.isocalendar()
    df_plot["week_key"] = cal["year"].astype(str) + "-" + cal["week"].astype(str).str.zfill(2)
    df_plot["week_label"] = "v" + cal["week"].astype(str).str.zfill(2)
    def p95(x):
        return np.percentile(x, 95)

    def p05(x):
        return np.percentile(x, 5)

    weekly = (
        df_plot.groupby("week_key", sort=False, observed=True)
        .agg(
            p95_load_kw=("load_kw", p95),
            max_load_kw=("load_kw", "max"),
            p05_available_kw=("available_kw", p05),
            min_available_kw=("available_kw", "min"),
        )
        .reset_index()
    )
    weekly["week_label"] = "v" + weekly["week_key"].str.split("-").str[1]
    weekly = weekly.sort_values("week_key")
    q_max = sys_stats["q_max"]

    x = np.arange(len(weekly))
    width = 0.35

    fig, axes = plt.subplots(2, 1, figsize=(12, 8))
    # Last: 95-percentil (huvud) + max (tunn, visar spikar)
    axes[0].bar(x - width / 2, weekly["p95_load_kw"], width, label="95-percentil last", edgecolor="black", linewidth=0.5)
    axes[0].bar(x + width / 2, weekly["max_load_kw"], width * 0.5, alpha=0.5, label="Max last (spikar)", edgecolor="black", linewidth=0.3)
    axes[0].axhline(y=q_max, color="red", linestyle="--", linewidth=1.5, label=f"Installerad effekt ({q_max:.0f} kW)")
    axes[0].set_xticks(x)
    axes[0].set_xticklabels(weekly["week_label"], rotation=45, ha="right")
    axes[0].set_xlabel("Vecka")
    axes[0].set_ylabel("Last (kW)")
    axes[0].set_title(f"{system_name} – Last per vecka (95-percentil = typisk topp, max = eventuella spikar)")
    axes[0].legend()
    axes[0].set_xlim(-0.6, len(x) - 0.4)

    # Tillgänglig: p05 (huvud, typiskt lägsta) + min (tunn, visar spikar)
    axes[1].bar(x - width / 2, weekly["p05_available_kw"], width, label="5-percentil tillgänglig (typiskt lägsta)", edgecolor="black", linewidth=0.5)
    axes[1].bar(x + width / 2, weekly["min_available_kw"], width * 0.5, alpha=0.5, label="Min tillgänglig (spikar)", edgecolor="black", linewidth=0.3)
    axes[1].axhline(y=q_max, color="red", linestyle="--", linewidth=1.5, label=f"Installerad effekt ({q_max:.0f} kW)")
    axes[1].set_xticks(x)
    axes[1].set_xticklabels(weekly["week_label"], rotation=45, ha="right")
    axes[1].set_xlabel("Vecka")
    axes[1].set_ylabel("Tillgänglig kapacitet (kW)")
    axes[1].set_title(f"{system_name} – Tillgänglig kapacitet per vecka (5-percentil = typiskt lägsta, min = eventuella toppar)")
    axes[1].legend()
    axes[1].set_xlim(-0.6, len(x) - 0.4)

    fig.tight_layout()
    fig.savefig(output_dir / "weekly_load_available.png", dpi=150, bbox_inches="tight")
    plt.close()

    lines = [
        f"SYSTEM: {system_name}",
        "",
        f"Total kapacitet: {sys_stats['q_max']:.0f} kW",
        f"Medellast: {sys_stats['mean_load']:.0f} kW",
        f"95-percentil last: {sys_stats['p95_load']:.0f} kW",
        "",
        f"Uppskattad praktisk tillgänglig effekt: {sys_stats['practical_available']:.0f} kW",
        "",
        f"Tid över 90 % last: {sys_stats['hours_over_90pct']:.1f} timmar ({sys_stats['hours_over_90pct_per_year']:.0f} timmar/år)",
        f"Tid vid full last: {sys_stats['hours_at_full_load']:.1f} timmar ({sys_stats['hours_at_full_load_per_year']:.0f} timmar/år)",
        "",
        "----------------------------------------",
        "Tillgänglig kapacitet per steg",
        "----------------------------------------",
    ]
    for s in per_step:
        lines.append(f"  {s['step']}: aktiv {s['active_kw']:.0f} kW, tillgänglig {s['available_kw']:.0f} kW")
    lines.extend([
        "",
        "----------------------------------------",
        "Belastning",
        "----------------------------------------",
        f"Max last: {sys_stats['max_load']:.0f} kW",
        f"99-percentil: {sys_stats['p99_load']:.0f} kW",
        "",
        "Per kompressor:",
    ])
    for name, s in comp_stats.items():
        lines.append(f"  {name}: {s['starts']} starter, {s['runtime_hours']:.1f} h driftstid")

    (output_dir / "summary.txt").write_text("\n".join(lines), encoding="utf-8")


def write_pdf_report(output_dir: Path, system_dirs: list[tuple[str, Path]]) -> None:
    """Skapa en samlad PDF-rapport i output-mappen."""
    if not system_dirs:
        return
    pdf_path = output_dir / "rapport.pdf"
    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=A4,
        rightMargin=20 * mm,
        leftMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "CustomTitle",
        parent=styles["Heading1"],
        fontSize=16,
        spaceAfter=12,
    )
    body_style = styles["Normal"]
    body_style.fontSize = 10

    story = []
    story.append(Paragraph("Kylkompressoranalys – Rapport", title_style))
    story.append(Spacer(1, 10 * mm))

    for system_name, sys_path in system_dirs:
        story.append(Paragraph(f"<b>System: {system_name}</b>", title_style))
        summary_file = sys_path / "summary.txt"
        if summary_file.exists():
            text = summary_file.read_text(encoding="utf-8")
            for line in text.split("\n"):
                if line.strip():
                    escaped = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    story.append(Paragraph(escaped, body_style))
            story.append(Spacer(1, 5 * mm))

        hist_img = sys_path / "histogram_load.png"
        if hist_img.exists():
            story.append(Paragraph("Lastfördelning", styles["Heading2"]))
            story.append(Image(str(hist_img), width=170 * mm, height=85 * mm))
            story.append(Spacer(1, 5 * mm))

        weekly_img = sys_path / "weekly_load_available.png"
        if weekly_img.exists():
            story.append(Paragraph("Last och tillgänglig kapacitet per vecka", styles["Heading2"]))
            story.append(Image(str(weekly_img), width=170 * mm, height=113 * mm))

        story.append(PageBreak())

    doc.build(story)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analysera kylkompressordata (MT, Frys, Komfort) och beräkna tillgänglig kapacitet."
    )
    parser.add_argument("--input", "-i", type=str, default="input", help="Mapp med IWMAC CSV-filer")
    parser.add_argument("--output", "-o", type=str, default="output", help="Output-mapp")
    parser.add_argument("--config", "-c", type=str, default="config.json", help="Sökväg till config.json")
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    config_path = script_dir / args.config if not Path(args.config).is_absolute() else Path(args.config)
    input_dir = script_dir / args.input if not Path(args.input).is_absolute() else Path(args.input)
    output_dir = script_dir / args.output if not Path(args.output).is_absolute() else Path(args.output)

    config = load_config(config_path)
    validate_config_files(config, input_dir)
    systems = group_compressors_by_system(config)
    system_dirs: list[tuple[str, Path]] = []

    for system_name, compressors in systems.items():
        if not compressors:
            continue
        df, comps_typed = load_and_merge_csv(compressors, input_dir)
        df, q_max = compute_load(df, comps_typed)
        comp_stats = compute_compressor_stats(df, comps_typed)
        sys_stats, _ = compute_system_stats(df, q_max)
        hist_df = build_histogram(df, bin_size=10)
        per_step = compute_per_step_capacity(df, comps_typed, q_max)

        sys_output = output_dir / system_name
        write_system_outputs(df, hist_df, sys_stats, comp_stats, per_step, sys_output, system_name)
        system_dirs.append((system_name, sys_output))
        print(f"  {system_name}: {len(compressors)} kompressorer, {q_max:.0f} kW → {sys_output}")

    write_pdf_report(output_dir, system_dirs)
    print(f"Klar. Resultat sparade i {output_dir} (rapport.pdf)")


if __name__ == "__main__":
    main()
