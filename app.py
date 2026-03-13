#!/usr/bin/env python3
"""
Streamlit-app fÃ¶r kylkompressoranalys.
AnvÃ¤nder analyze_refrigeration fÃ¶r att berÃ¤kna last och tillgÃ¤nglig kapacitet.
"""

import sys
import tempfile
from pathlib import Path

import streamlit as st

# LÃ¤gg till script-mappen i path sÃ¥ vi kan importera analyze_refrigeration
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
    load_config_from_json,
    validate_config_files,
    write_pdf_report,
    write_system_outputs,
)

st.set_page_config(
    page_title="Kylkompressoranalys",
    page_icon="â„ï¸",
    layout="wide",
)

st.title("â„ï¸ Kylkompressoranalys")
st.markdown(
    "Analysera kylbelastning och tillgÃ¤nglig kapacitet frÃ¥n IWMAC CSV-loggar. "
    "StÃ¶djer MT (C1â€“C8), Frys (F1â€“F8), Komfort (H1â€“H4)."
)

script_dir = Path(__file__).resolve().parent
input_dir = script_dir / "input"
output_dir = script_dir / "output"
config_path = script_dir / "config.json"

# === Config ===
st.subheader("âš™ï¸ Konfiguration")
config_uploaded = st.file_uploader(
    "Ladda upp config.json (valfritt â€“ annars anvÃ¤nds standard)",
    type=["json"],
    key="config_upload",
)
if config_uploaded:
    try:
        config = load_config_from_json(config_uploaded.getvalue())
        st.success("Config inlÃ¤st frÃ¥n uppladdad fil.")
    except Exception as e:
        st.error(f"Ogiltig config: {e}")
        st.stop()
elif config_path.exists():
    try:
        config = load_config(config_path)
    except Exception as e:
        st.error(f"Config-fel: {e}")
        st.stop()
else:
    st.error("Ingen config. Ladda upp config.json eller lÃ¤gg config.json i projektmappen.")
    st.stop()

# === Filuppladdning per system ===
st.subheader("ðŸ“¤ Ladda upp indatafiler")
st.caption(
    "Ladda upp CSV-filer fÃ¶r respektive system. Om du inte laddar upp anvÃ¤nds filer frÃ¥n mappen "
    "(lokal kÃ¶rning) eller tidigare uppladdade filer."
)

systems = group_compressors_by_system(config)


def _unique_files_per_system(systems_dict):
    """Returnera per system: lista av (filnamn, [kompressornamn som anvÃ¤nder den])."""
    result = {}
    for system_name, compressors in systems_dict.items():
        file_to_comps = {}
        for c in compressors:
            f = c["file"]
            file_to_comps.setdefault(f, []).append(c["name"])
        result[system_name] = [(f, comps) for f, comps in file_to_comps.items()]
    return result


file_groups = _unique_files_per_system(systems)
uploaded_files = {}

for system_name, files_and_comps in file_groups.items():
    comps_str = ", ".join(c for _, comps in files_and_comps for c in comps)
    with st.expander(f"**{system_name}** ({comps_str})", expanded=True):
        for idx, (filename, comp_names) in enumerate(files_and_comps):
            label = f"{', '.join(comp_names)} â€“ {filename}"
            f = st.file_uploader(
                label,
                type=["csv"],
                key=f"upload_{system_name}_{idx}",
            )
            if f is not None:
                uploaded_files[(system_name, filename)] = f

st.divider()

# === InstÃ¤llningar ===
with st.sidebar:
    st.header("InstÃ¤llningar")
    use_custom_input = st.checkbox("Anpassad input-mapp", value=False)
    custom_input = ""
    if use_custom_input:
        custom_input = st.text_input("Input-mapp", value="input")
    inp_dir = script_dir / (custom_input if custom_input else "input")

    resample_min = st.slider("Samplingsintervall (minuter)", 1, 60, 1)
    bin_size = st.slider("Histogram intervall (kW)", 5, 50, 10)

    st.divider()
    st.caption("Standard: input/, output/, config.json")

# BestÃ¤m input-kÃ¤lla
use_upload = len(uploaded_files) > 0
if not use_upload:
    if not inp_dir.exists():
        st.error(
            f"Input-mapp saknas: {inp_dir}. Ladda upp CSV-filer ovan eller anvÃ¤nd en befintlig mapp."
        )
        st.stop()
    try:
        validate_config_files(config, inp_dir)
    except Exception as e:
        st.error(f"Config-fel: {e}")
        st.stop()

# KÃ¶r analys
if st.button("KÃ¶r analys", type="primary"):
    if use_upload:
        work_dir = Path(tempfile.mkdtemp())
        if inp_dir.exists():
            for f in inp_dir.glob("*.csv"):
                if f.name not in [fn for (_, fn) in uploaded_files.keys()]:
                    (work_dir / f.name).write_bytes(f.read_bytes())
        for (_, filename), uploaded in uploaded_files.items():
            (work_dir / filename).write_bytes(uploaded.getvalue())
        try:
            validate_config_files(config, work_dir)
        except FileNotFoundError as e:
            st.error(f"Saknade filer: {e}")
            st.stop()
    else:
        work_dir = inp_dir
    system_dirs = []
    progress = st.progress(0, text="Analyserar...")

    for i, (system_name, compressors) in enumerate(systems.items()):
        if not compressors:
            continue
        progress.progress((i + 1) / max(len(systems), 1), text=f"Analyserar {system_name}...")
        try:
            df, comps_typed = load_and_merge_csv(compressors, work_dir, resample_min=resample_min)
            df, q_max = compute_load(df, comps_typed)
            comp_stats = compute_compressor_stats(df, comps_typed)
            sys_stats, _ = compute_system_stats(df, q_max)
            hist_df = build_histogram(df, bin_size=bin_size)
            per_step = compute_per_step_capacity(df, comps_typed, q_max)

            sys_output = output_dir / system_name
            sys_output.mkdir(parents=True, exist_ok=True)
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
    st.rerun()

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
                st.info("Inga data fÃ¶r detta system.")
                continue

            summary_file = sys_path / "summary.txt"
            if summary_file.exists():
                st.code(summary_file.read_text(encoding="utf-8"), language=None)

            col1, col2 = st.columns(2)
            with col1:
                hist_img = sys_path / "histogram_load.png"
                if hist_img.exists():
                    st.image(str(hist_img), caption="LastfÃ¶rdelning")
            with col2:
                weekly_img = sys_path / "weekly_load_available.png"
                if weekly_img.exists():
                    st.image(str(weekly_img), caption="Last och tillgÃ¤nglig kapacitet per vecka")

    pdf_path = output_dir / "rapport.pdf"
    if pdf_path.exists():
        with open(pdf_path, "rb") as f:
            st.download_button("Ladda ner PDF-rapport", f, file_name="rapport.pdf", mime="application/pdf")
