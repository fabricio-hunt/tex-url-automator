"""
URL Processing Tool
===================
GUI application to process CSV/TXT files containing URLs.

Author  : Fabricio Barauna
Version : 2.2.0
Python  : 3.11 (64-bit) — recommended

Changelog v2.2.0:
  - Fix: tb.Style(master=) removido — compatível com todas as versões do ttkbootstrap
  - Fix: normalize_columns chamada na main thread (evita Toplevel fora da thread Tk)
  - _worker agora retorna df bruto; normalize_columns + tratar_urls rodam na main thread
"""

from __future__ import annotations

import logging
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import chardet
import pandas as pd
import ttkbootstrap as tb
from ttkbootstrap.constants import *
from tkinterdnd2 import TkinterDnD, DND_FILES

from backend import tratar_urls


# =============================================================================
# Logging
# =============================================================================

logging.basicConfig(
    filename="url_processing.log",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    encoding="utf-8",
)
log = logging.getLogger(__name__)


# =============================================================================
# Constants
# =============================================================================

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({".csv", ".txt"})
OUTPUT_SEP = ";"
OUTPUT_ENCODING = "utf-8"
APP_TITLE = "URL Processing Tool"
APP_GEOMETRY = "460x300"


# =============================================================================
# File I/O helpers
# =============================================================================

def detect_encoding(path: Path) -> str:
    """Detect file encoding using chardet; fallback to utf-8."""
    with open(path, "rb") as fh:
        raw = fh.read()
    result = chardet.detect(raw)
    encoding = result.get("encoding") or "utf-8"
    log.debug("Detected encoding '%s' for '%s'", encoding, path.name)
    return encoding


def read_txt(path: Path) -> pd.DataFrame:
    """Read a TXT file (one URL per line) into a DataFrame."""
    encoding = detect_encoding(path)
    with open(path, encoding=encoding, errors="ignore") as fh:
        urls = [line.strip() for line in fh if line.strip()]
    if not urls:
        raise ValueError("TXT file is empty or contains no valid lines.")
    log.info("Read %d URLs from '%s'", len(urls), path.name)
    return pd.DataFrame({"url": urls})


def read_csv(path: Path) -> pd.DataFrame:
    """Read a CSV file with automatic encoding detection."""
    encoding = detect_encoding(path)
    try:
        df = pd.read_csv(path, encoding=encoding)
    except Exception:
        log.warning("Encoding '%s' failed for '%s', retrying with utf-8", encoding, path.name)
        df = pd.read_csv(path)
    if df.empty:
        raise ValueError("CSV file is empty.")
    log.info("Read %d rows from '%s'", len(df), path.name)
    return df


# =============================================================================
# Validation
# =============================================================================

def validate_path(path: Path) -> None:
    """Raise descriptive errors for invalid paths."""
    if not path.exists():
        raise FileNotFoundError(f"File not found:\n{path}")
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported format: '{path.suffix}'\n"
            f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )
    if path.stat().st_size == 0:
        raise ValueError("The selected file is empty.")


# =============================================================================
# Path normalisation (drag-and-drop)
# =============================================================================

def normalize_dnd_path(raw: str) -> Path:
    """
    Normalise a drag-and-drop path string delivered by tkinterdnd2.
    Multiple files come separated by spaces wrapped in {}.
    Only the first file is taken.
    """
    first = raw.strip().split("} {")[0]
    cleaned = first.strip("{}").strip('"').strip("'")
    return Path(cleaned)


# =============================================================================
# Column normalisation  — MUST run on main thread (opens Toplevel if needed)
# =============================================================================

def _ask_url_column(columns: list[str]) -> str | None:
    """
    Modal dialog asking the user to pick which column contains URLs.
    Returns the selected column name, or None if cancelled.
    """
    result: list[str] = []

    win = tk.Toplevel()
    win.title("Map URL column")
    win.geometry("340x170")
    win.resizable(False, False)
    win.grab_set()

    tk.Label(
        win,
        text="Column 'url' not found in this file.\nWhich column contains the URLs?",
        pady=12,
        justify="center",
    ).pack()

    var = tk.StringVar(value=columns[0])
    combo = ttk.Combobox(
        win, textvariable=var, values=columns, state="readonly", width=32
    )
    combo.pack(pady=4)

    def confirm() -> None:
        result.append(var.get())
        win.destroy()

    def cancel() -> None:
        win.destroy()

    btn_frame = tk.Frame(win)
    btn_frame.pack(pady=10)
    tk.Button(btn_frame, text="Confirm", command=confirm, width=12).pack(side="left", padx=6)
    tk.Button(btn_frame, text="Cancel",  command=cancel,  width=12).pack(side="left", padx=6)

    win.wait_window()
    return result[0] if result else None


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Ensure the DataFrame has a column named exactly 'url'.

    Resolution order:
      1. Column already named 'url' (any case) → rename to lowercase.
      2. Single column present → treat it as the URL column.
      3. Multiple columns, none matching → show picker dialog.
    """
    df.columns = df.columns.str.strip()

    match = next((c for c in df.columns if c.lower() == "url"), None)
    if match:
        if match != "url":
            log.info("Column '%s' renamed to 'url'", match)
            return df.rename(columns={match: "url"})
        return df

    if len(df.columns) == 1:
        original = df.columns[0]
        log.info("Single column '%s' renamed to 'url'", original)
        return df.rename(columns={original: "url"})

    log.warning("No 'url' column found. Available: %s — asking user.", list(df.columns))
    chosen = _ask_url_column(list(df.columns))
    if not chosen:
        raise InterruptedError("No URL column selected — processing cancelled.")
    log.info("User mapped column '%s' → 'url'", chosen)
    return df.rename(columns={chosen: "url"})


# =============================================================================
# Core pipeline
# =============================================================================

def load_file(path: Path) -> pd.DataFrame:
    """Read the file and return a raw DataFrame (no column normalisation yet)."""
    ext = path.suffix.lower()
    if ext == ".txt":
        return read_txt(path)
    if ext == ".csv":
        return read_csv(path)
    raise ValueError(f"Cannot load file with extension '{ext}'.")


def save_output(df: pd.DataFrame, source_path: Path) -> Path:
    """Prompt the user for a save location and persist the DataFrame."""
    default_name = f"{source_path.stem}_clean.csv"
    dest = filedialog.asksaveasfilename(
        title="Save processed file as…",
        defaultextension=".csv",
        initialfile=default_name,
        initialdir=str(source_path.parent),
        filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
    )
    if not dest:
        raise InterruptedError("Save cancelled by user.")
    output_path = Path(dest)
    df.to_csv(output_path, index=False, sep=OUTPUT_SEP, encoding=OUTPUT_ENCODING)
    log.info("Output saved to '%s' (%d rows)", output_path, len(df))
    return output_path


# =============================================================================
# Background I/O worker  (only file reading — no Tkinter calls)
# =============================================================================

def _io_worker(path: Path, app: TkinterDnD.Tk, ctx: dict) -> None:
    """
    Runs in a daemon thread.
    Only does file I/O — hands the raw DataFrame back to the main thread
    so that normalize_columns (which may open a Toplevel) and tratar_urls
    are always called on the Tk main thread.
    """
    try:
        df_raw = load_file(path)
        app.after(0, lambda: _process_on_main(df_raw, path, app, ctx))
    except Exception as exc:
        log.exception("I/O failed for '%s'", path.name)
        app.after(0, lambda: _on_error(exc, ctx))


# =============================================================================
# Main-thread processing  (normalize → tratar_urls → save → feedback)
# =============================================================================

def _process_on_main(
    df_raw: pd.DataFrame, path: Path, app: TkinterDnD.Tk, ctx: dict
) -> None:
    """Column normalisation, backend call and save — all on the main thread."""
    try:
        df = normalize_columns(df_raw)          # may open Toplevel dialog
        df_processed = tratar_urls(df)          # business logic
        output_path = save_output(df_processed, path)  # save dialog
        _on_success(output_path, ctx)
    except InterruptedError:
        _reset_ui(ctx, message=None)
    except Exception as exc:
        log.exception("Processing failed for '%s'", path.name)
        _on_error(exc, ctx)


def _on_success(output_path: Path, ctx: dict) -> None:
    _reset_ui(ctx, message=None)
    messagebox.showinfo(
        "Success",
        f"Processing completed!\n\nSaved as:\n{output_path}",
    )


def _on_error(exc: Exception, ctx: dict) -> None:
    _reset_ui(ctx, message=None)
    messagebox.showerror("Error", f"Processing failed:\n{exc}")


def _reset_ui(ctx: dict, *, message: str | None) -> None:
    ctx["drop_label"].config(text=message or "Drop file here")
    ctx["btn"].config(state="normal")


# =============================================================================
# Entry point for processing (triggered by UI events)
# =============================================================================

def process_file(path_str: str, app: TkinterDnD.Tk, ctx: dict) -> None:
    """Validate path, lock UI, then dispatch I/O to background thread."""
    if not path_str:
        return

    try:
        path = normalize_dnd_path(path_str)
        validate_path(path)
    except (FileNotFoundError, ValueError) as exc:
        messagebox.showerror("Error", str(exc))
        return

    log.info("Started processing '%s'", path.name)
    ctx["drop_label"].config(text=f"Processing: {path.name}…")
    ctx["btn"].config(state="disabled")

    threading.Thread(
        target=_io_worker,
        args=(path, app, ctx),
        daemon=True,
        name=f"io-{path.stem}",
    ).start()


# =============================================================================
# GUI
# =============================================================================

def create_app() -> None:
    # 1. DnD window first — required before Style or any widget
    app = TkinterDnD.Tk()

    # 2. Theme applied after window exists, without master= (all ttkbootstrap versions)
    tb.Style(theme="flatly")

    app.title(APP_TITLE)
    app.geometry(APP_GEOMETRY)
    app.resizable(False, False)

    # ── Header ───────────────────────────────────────────────────────────────
    tb.Label(app, text=APP_TITLE, font=("Segoe UI", 14, "bold")).pack(pady=(18, 4))
    tb.Label(
        app,
        text="Drag & drop a CSV or TXT file, or click 'Select file'",
        font=("Segoe UI", 9),
        foreground="gray",
    ).pack()

    # ── Drop zone ─────────────────────────────────────────────────────────────
    drop_label = tb.Label(
        app,
        text="Drop file here",
        bootstyle=INFO,
        relief="ridge",
        padding=20,
        font=("Segoe UI", 10),
        anchor="center",
    )
    drop_label.pack(padx=40, pady=18, fill="both")

    # ── Select file button ────────────────────────────────────────────────────
    btn = tb.Button(app, text="Select file", bootstyle=PRIMARY, width=20)
    btn.pack(pady=(0, 18))

    # ── Shared UI context ────────────────────────────────────────────────────
    ctx: dict = {"drop_label": drop_label, "btn": btn}

    # ── Event bindings ────────────────────────────────────────────────────────
    drop_label.drop_target_register(DND_FILES)  # type: ignore[attr-defined]
    drop_label.dnd_bind(                         # type: ignore[attr-defined]
        "<<Drop>>",
        lambda e: (
            log.info("DnD event received: %s", e.data),
            process_file(e.data, app, ctx),
        ),
    )

    btn.config(
        command=lambda: process_file(
            filedialog.askopenfilename(
                title="Choose a file",
                filetypes=[
                    ("CSV files", "*.csv"),
                    ("Text files", "*.txt"),
                    ("All files", "*.*"),
                ],
            ),
            app,
            ctx,
        )
    )

    app.mainloop()


# =============================================================================
# Entry point
# =============================================================================

if __name__ == "__main__":
    log.info("Application started")
    create_app()
    log.info("Application closed")