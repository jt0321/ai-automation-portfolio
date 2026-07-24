"""
ingest_scores.py
----------------
Batch-ingests all Humdrum (.krn) files in ./data/ through the symbolic RAG pipeline:
  Humdrum (.krn) → MEI (via Verovio) → music21 analysis → pgvector (PostgreSQL)

Prerequisites:
    python download_beethoven_piano_sonatas.py  # fetch .krn files
    psql $DATABASE_URL -f db/schema.sql  # create tables (first time only)
"""

import re
import click
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from download_beethoven_piano_sonatas import DATA_DIR
from db.store import (
    upsert_work, store_asset, store_segments,
    clear_work_segments_and_assets
)
from analysis.analyzer import analyze_score
from pipeline.mei_converter import score_to_mei

# Sonata number -> (title, opus, key, nickname). Covers all 32 published
# Beethoven piano sonatas as catalogued in craigsapp/beethoven-piano-sonatas.
SONATA_CATALOG = {
    1:  ("Piano Sonata No. 1 in F minor",        "Op. 2 No. 1",  "F minor",  None),
    2:  ("Piano Sonata No. 2 in A major",         "Op. 2 No. 2",  "A major",  None),
    3:  ("Piano Sonata No. 3 in C major",         "Op. 2 No. 3",  "C major",  None),
    4:  ("Piano Sonata No. 4 in E-flat major",    "Op. 7",        "E-flat major", None),
    5:  ("Piano Sonata No. 5 in C minor",         "Op. 10 No. 1", "C minor",  None),
    6:  ("Piano Sonata No. 6 in F major",         "Op. 10 No. 2", "F major",  None),
    7:  ("Piano Sonata No. 7 in D major",         "Op. 10 No. 3", "D major",  None),
    8:  ("Piano Sonata No. 8 in C minor",         "Op. 13",       "C minor",  "Pathétique"),
    9:  ("Piano Sonata No. 9 in E major",         "Op. 14 No. 1", "E major",  None),
    10: ("Piano Sonata No. 10 in G major",        "Op. 14 No. 2", "G major",  None),
    11: ("Piano Sonata No. 11 in B-flat major",   "Op. 22",       "B-flat major", None),
    12: ("Piano Sonata No. 12 in A-flat major",   "Op. 26",       "A-flat major", None),
    13: ("Piano Sonata No. 13 in E-flat major",   "Op. 27 No. 1", "E-flat major", "Quasi una fantasia"),
    14: ("Piano Sonata No. 14 in C-sharp minor",  "Op. 27 No. 2", "C-sharp minor", "Moonlight"),
    15: ("Piano Sonata No. 15 in D major",        "Op. 28",       "D major",  "Pastoral"),
    16: ("Piano Sonata No. 16 in G major",        "Op. 31 No. 1", "G major",  None),
    17: ("Piano Sonata No. 17 in D minor",        "Op. 31 No. 2", "D minor",  "Tempest"),
    18: ("Piano Sonata No. 18 in E-flat major",   "Op. 31 No. 3", "E-flat major", "The Hunt"),
    19: ("Piano Sonata No. 19 in G minor",        "Op. 49 No. 1", "G minor",  None),
    20: ("Piano Sonata No. 20 in G major",        "Op. 49 No. 2", "G major",  None),
    21: ("Piano Sonata No. 21 in C major",        "Op. 53",       "C major",  "Waldstein"),
    22: ("Piano Sonata No. 22 in F major",        "Op. 54",       "F major",  None),
    23: ("Piano Sonata No. 23 in F minor",        "Op. 57",       "F minor",  "Appassionata"),
    24: ("Piano Sonata No. 24 in F-sharp major",  "Op. 78",       "F-sharp major", "à Thérèse"),
    25: ("Piano Sonata No. 25 in G major",        "Op. 79",       "G major",  None),
    26: ("Piano Sonata No. 26 in E-flat major",   "Op. 81a",      "E-flat major", "Les Adieux"),
    27: ("Piano Sonata No. 27 in E minor",        "Op. 90",       "E minor",  None),
    28: ("Piano Sonata No. 28 in A major",        "Op. 101",      "A major",  None),
    29: ("Piano Sonata No. 29 in B-flat major",   "Op. 106",      "B-flat major", "Hammerklavier"),
    30: ("Piano Sonata No. 30 in E major",        "Op. 109",      "E major",  None),
    31: ("Piano Sonata No. 31 in A-flat major",   "Op. 110",      "A-flat major", None),
    32: ("Piano Sonata No. 32 in C minor",        "Op. 111",      "C minor",  None),
}


def parse_krn_metadata(krn_path: Path) -> dict:
    """Parse standard Humdrum metadata headers for title, composer, opus, movement, etc."""
    meta = {
        "composer": "Ludwig van Beethoven",
        "title": "Piano Sonata",
        "opus": None,
        "nickname": None,
        "movement": "",
        "key": None,
        "year": None,
    }
    
    # Try parsing filename first to guess sonata number and movement
    # e.g., sonata32-1.krn
    match = re.search(r"sonata(\d+)-(\d+)", krn_path.name)
    sonata_num = None
    mvt_num = None
    if match:
        sonata_num = int(match.group(1))
        mvt_num = int(match.group(2))
        meta["movement"] = f"Mvt {mvt_num}"
        meta["title"] = f"Piano Sonata No. {sonata_num}"
        
    try:
        content = krn_path.read_text(encoding="utf-8")
        for line in content.splitlines():
            if not line.startswith("!!!"):
                continue
            
            # Composer
            if line.startswith("!!!COM:"):
                val = line.split(":", 1)[1].strip()
                # Reformat "Beethoven, Ludwig van" -> "Ludwig van Beethoven"
                if "," in val:
                    parts = [p.strip() for p in val.split(",", 1)]
                    meta["composer"] = f"{parts[1]} {parts[0]}"
                else:
                    meta["composer"] = val
            
            # Original Title (fallback if filename parsing didn't work)
            elif line.startswith("!!!OTL:"):
                val = line.split(":", 1)[1].strip()
                # Clean up title formatting if it has sonata info
                if not sonata_num:
                    meta["title"] = val
            
            # Opus
            elif line.startswith("!!!OPS:"):
                val = line.split(":", 1)[1].strip()
                if val.isdigit():
                    meta["opus"] = f"Op. {val}"
                elif not val.lower().startswith("op"):
                    meta["opus"] = f"Op. {val}"
                else:
                    meta["opus"] = val
                    
            # Composition Date / Year
            elif line.startswith("!!!ODT:"):
                val = line.split(":", 1)[1].strip()
                year_match = re.search(r"\b(1[789]\d{2})\b", val)
                if year_match:
                    meta["year"] = int(year_match.group(1))
    except Exception as e:
        click.echo(f"   ⚠ Metadata parsing error for {krn_path.name}: {e}")
        
    # Standardize title/opus/key from the known catalog when we recognize
    # the sonata number, regardless of what the Humdrum headers say.
    if sonata_num in SONATA_CATALOG:
        title, opus, key, nickname = SONATA_CATALOG[sonata_num]
        meta["title"] = f"{title} ({nickname})" if nickname else title
        meta["opus"] = opus
        meta["key"] = key
        meta["nickname"] = nickname

    return meta


@click.command()
@click.option("--window", default=4, type=int, show_default=True,
              help="Measures per analysis chunk")
def main(window: int):
    krns = sorted(DATA_DIR.glob("*.krn"))
    if not krns:
        click.echo(f"No Humdrum (.krn) files found in ./{DATA_DIR}/ — run download_beethoven_piano_sonatas.py first.")
        return

    click.echo(f"Found {len(krns)} Humdrum score(s) in ./{DATA_DIR}/\n")

    for krn in krns:
        meta = parse_krn_metadata(krn)
        mvt_suffix = f" ({meta['movement']})" if meta["movement"] else ""
        click.echo(f"▶  {meta['composer']} — {meta['title']}{mvt_suffix}")

        work_meta = dict(
            composer     = meta["composer"],
            title        = f"{meta['title']}{mvt_suffix}",
            opus         = meta["opus"],
            nickname     = meta["nickname"],
            key_signature= meta["key"],
            year_composed= meta["year"],
            imslp_url    = None,
        )
        work_id = upsert_work(work_meta)
        click.echo(f"   Work ID: {work_id}")

        # Clear existing assets/segments to avoid duplicates
        clear_work_segments_and_assets(work_id)

        # Save Humdrum (.krn) asset directly in DB
        store_asset(work_id, "krn", str(krn))

        # Generate MEI file dynamically using Verovio (needed for SVG rendering)
        mei_path = score_to_mei(str(krn))
        if mei_path:
            store_asset(work_id, "mei", str(mei_path))
            click.echo(f"   ✓ MEI file generated → {mei_path.name}")
        else:
            click.echo("   ✗ MEI generation failed.")

        # Run music21 analysis directly on .krn and segment score
        click.echo("   Analyzing musical features...")
        try:
            chunks, global_key = analyze_score(str(krn), window=window)
            click.echo(f"   ✓ {len(chunks)} chunks extracted (global key: {global_key})")
            store_segments(work_id, chunks)
            click.echo(f"   ✓ Done ingesting\n")
        except Exception as e:
            click.echo(f"   ✗ Analysis failed: {e}\n")
            continue

    click.echo("All scores ingested. Run `python server.py` or `streamlit run scorechat_app.py` to start the app.")


if __name__ == "__main__":
    main()
