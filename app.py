#!/usr/bin/env python3
"""
Streamlit-app för kylkompressoranalys.
Använder analyze_refrigeration för att beräkna last och tillgänglig kapacitet.
"""

import sys
from pathlib import Path

import streamlit as st

# Lägg till script-mappen i path så vi kan importera analyze_refrigeration
script_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(script_dir))

from analyze_refrigeration import (
    build_histogram,
    compute_compressor_stats,
    compute_load,
    compute_per_step_capacity,
    compute_system_stats,
    group_compressors_by_system,
    load_and_merge_csv,
    load_config,
    validate_config_files,
    write_pdf_report,
    write_system_outputs,
)

st.set_page_config(
    page_title="Kylkompressoranalys",
    page_icon="❄️",
    layout="wide",
)

st.title("❄️ Kylkompressoranalys")
st.markdown(
    "Analysera kylbelastning och tillgänglig kapacitet från IWMAC CSV-loggar. "
    "Stödjer MT (C1–C8), Frys (F1–F8), Komfort (H1–H4)."
)

script_dir = Path(__file__).resolve().parent
input_dir = script_dir / "input"
output_dir = script_dir / "output"
config_path = script_dir / "config.json"

# Sidofält för inställningar
with st.sidebar:
    st.header("Inställningar")
    use_custom_input = st.checkbox("Anpassad input-mapp", value=False)
    custom_input = ""
    if use_custom_input:
        custom_input = st.text_input("Input-mapp", value="input")
    inp_dir = script_dir / (custom_input if custom_input else "input")

    resample_min = st.slider("Samplingsintervall (minuter)", 1, 60, 1)
    bin_size = st.slider("Histogram intervall (kW)", 5, 50, 10)

    st.divider()
    st.caption("Standard: input/, output/, config.json")

if not config_path.exists():
    st.error(f"Config fil saknas: {config_path}")
    st.stop()

if not inp_dir.exists():
    st.error(f"Input-mapp saknas: {inp_dir}")
    st.stop()

try:
    config = load_config(config_path)
    validate_config_files(config, inp_dir)
except Exception as e:
    st.error(f"Config-fel: {e}")
    st.stop()

systems = group_compressors_by_system(config)

if st.button("Kör analys", type="primary"):
    system_dirs = []
    progress = st.progress(0, text="Analyserar...")

    for i, (system_name, compressors) in enumerate(systems.items()):
        if not compressors:
            continue
        progress.progress((i + 1) / max(len(systems), 1), text=f"Analyserar {system_name}...")
        try:
            df, comps_typed = load_and_merge_csv(compressors, inp_dir, resample_min=resample_min)
            df, q_max = compute_load(df, comps_typed)
            comp_stats = compute_compressor_stats(df, comps_typed)
            sys_stats, _ = compute_system_stats(df, q_max)
            hist_df = build_histogram(df, bin_size=bin_size)
            per_step = compute_per_step_capacity(df, comps_typed, q_max)

            sys_output = output_dir / system_name
            write_system_outputs(
                df, hist_df, sys_stats, comp_stats, per_step, sys_output, system_name
            )
            system_dirs.append((system_name, sys_output))
        except Exception as e:
            st.error(f"Fel vid analys av {system_name}: {e}")
            raise

    write_pdf_report(output_dir, system_dirs)
    progress.progress(1.0, text="Klar!")
    st.success(f"Analys klar. Resultat sparade i {output_dir}")

# Visa sammanfattning om output finns
output_exists = output_dir.exists() and any(output_dir.iterdir())
if output_exists:
    st.divider()
    st.subheader("Senaste resultat")

    tabs = st.tabs(list(systems.keys()))
    for tab, (system_name, compressors) in zip(tabs, systems.items()):
        with tab:
            sys_path = output_dir / system_name
            if not sys_path.exists():
                st.info("Inga data för detta system.")
                continue

            summary_file = sys_path / "summary.txt"
            if summary_file.exists():
                st.code(summary_file.read_text(encoding="utf-8"), language=None)

            col1, col2 = st.columns(2)
            with col1:
                hist_img = sys_path / "histogram_load.png"
                if hist_img.exists():
                    st.image(str(hist_img), caption="Lastfördelning")
            with col2:
                weekly_img = sys_path / "weekly_load_available.png"
                if weekly_img.exists():
                    st.image(str(weekly_img), caption="Last och tillgänglig kapacitet per vecka")

    pdf_path = output_dir / "rapport.pdf"
    if pdf_path.exists():
        with open(pdf_path, "rb") as f:
            st.download_button("Ladda ner PDF-rapport", f, file_name="rapport.pdf", mime="application/pdf")
