"""Kommandozeile: python -m snapmem <befehl> [optionen]"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from snapmem import __version__
from snapmem.config import Settings
from snapmem.logging_setup import setup_logging

log = logging.getLogger("snapmem")


def _settings(args) -> Settings:
    s = Settings.from_base(
        Path(args.base),
        input_dir=args.input, output_dir=args.output, work_dir=args.work,
        workers=getattr(args, "workers", None), video_workers=getattr(args, "video_workers", None),
        exiftool=args.exiftool, default_tz=args.default_tz,
    )
    s.ensure_dirs()
    return s


def _load_or_build(s: Settings):
    """Baut den Katalog immer neu auf (dank Probe-Cache schnell) – so greifen neue Entscheidungen sofort."""
    from snapmem.catalog import build_catalog
    from snapmem.report import print_stats, write_catalog_reports

    cat = build_catalog(s)
    cat.save(s.catalog_path)
    write_catalog_reports(cat, s.reports_dir)
    print_stats(cat)
    return cat


def cmd_check(args) -> int:
    from snapmem.tools import environment_report

    s = _settings(args)
    ok = True
    for k, v in environment_report(s.exiftool).items():
        log.info("%-18s %s", k, v)
        ok &= not v.startswith("FEHLT")
    try:
        from snapmem.export_reader import list_zips
        log.info("%-18s %d Zip-Dateien in %s", "input", len(list_zips(s.input_dir)), s.input_dir)
    except Exception as exc:
        log.error("input: %s", exc)
        ok = False
    return 0 if ok else 1


def cmd_scan(args) -> int:
    s = _settings(args)
    cat = _load_or_build(s)
    pend = cat.stats["pending_conflicts"]
    if pend:
        log.info("Offene Konflikte -> `python -m snapmem review` starten, um sie zu entscheiden.")
    log.info("Katalog: %s | Reports: %s", s.catalog_path, s.reports_dir)
    return 0


def cmd_review(args) -> int:
    from snapmem.review.server import serve

    s = _settings(args)
    cat = _load_or_build(s)
    return serve(s, cat, port=args.port, open_browser=not args.no_browser)


def cmd_process(args) -> int:
    from snapmem.pipeline import process

    s = _settings(args)
    cat = _load_or_build(s)
    return process(s, cat, limit=args.limit, only=args.only, sample=args.sample,
                   use_defaults=args.use_defaults, force=args.force, dry_run=args.dry_run)


def cmd_verify(args) -> int:
    from snapmem.verify import verify

    s = _settings(args)
    from snapmem.catalog import Catalog
    if not s.catalog_path.exists():
        log.error("Kein Katalog – zuerst scan/process ausführen")
        return 1
    return verify(s, Catalog.load(s.catalog_path), limit=args.limit)


def cmd_run(args) -> int:
    from snapmem.pipeline import process

    s = _settings(args)
    cat = _load_or_build(s)
    if cat.stats["pending_conflicts"] and not args.use_defaults:
        log.warning("Es gibt offene Konflikte %s. Bitte zuerst `python -m snapmem review` ausführen "
                    "oder mit --use-defaults fortfahren. Konfliktfreie Memories werden trotzdem verarbeitet.",
                    cat.stats["pending_conflicts"])
    rc = process(s, cat, use_defaults=args.use_defaults, force=args.force)
    if rc == 0:
        from snapmem.verify import verify
        from snapmem.catalog import Catalog
        rc = verify(s, Catalog.load(s.catalog_path), limit=args.verify_limit)
    return rc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="snapmem", description="Snapchat-Memories-Export für iCloud & Co. aufbereiten")
    p.add_argument("--version", action="version", version=f"snapmem {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--base", default=".", help="Projektordner (Standard: aktuelles Verzeichnis)")
    common.add_argument("--input", help="Ordner mit den Zip-Dateien (Standard: <base>/input)")
    common.add_argument("--output", help="Ausgabeordner (Standard: <base>/output)")
    common.add_argument("--work", help="Arbeitsordner für Cache/Status (Standard: <base>/work)")
    common.add_argument("--exiftool", help="Pfad zu exiftool.exe")
    common.add_argument("--default-tz", default=None, help="Zeitzone ohne GPS (Standard: Europe/Berlin)")
    common.add_argument("-v", "--verbose", action="store_true", help="Debug-Ausgabe auf der Konsole")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("check", parents=[common], help="Umgebung prüfen (ffmpeg, exiftool, NVENC, Input)")
    sub.add_parser("scan", parents=[common], help="Export analysieren, Katalog + Reports erstellen")

    r = sub.add_parser("review", parents=[common], help="Web-Oberfläche zum Lösen von Konflikten")
    r.add_argument("--port", type=int, default=8765)
    r.add_argument("--no-browser", action="store_true")

    for name, hlp in (("process", "Medien zusammenfügen + Metadaten schreiben"),
                      ("run", "scan + process + verify in einem Schritt")):
        q = sub.add_parser(name, parents=[common], help=hlp)
        q.add_argument("--workers", type=int, help="Parallele Arbeiter (Standard 6)")
        q.add_argument("--video-workers", type=int, help="Parallele Video-Encodes (Standard 2)")
        q.add_argument("--use-defaults", action="store_true",
                       help="Offene Konflikte mit Standardwerten lösen (kein GPS / Letterbox)")
        q.add_argument("--force", action="store_true", help="Bereits verarbeitete Memories erneut verarbeiten")
        if name == "process":
            q.add_argument("--limit", type=int, help="Nur die ersten N Memories")
            q.add_argument("--only", nargs="+", help="Nur diese Memory-IDs (z. B. 2025-10-25_<uuid>)")
            q.add_argument("--sample", action="store_true",
                           help="Je ein Beispiel pro Szenario verarbeiten (zum Testen)")
            q.add_argument("--dry-run", action="store_true", help="Nur anzeigen, was passieren würde")
        else:
            q.add_argument("--verify-limit", type=int, default=0, help="Nur N Dateien prüfen (0 = alle)")

    v = sub.add_parser("verify", parents=[common], help="Metadaten der Ausgabe mit exiftool prüfen")
    v.add_argument("--limit", type=int, default=0, help="Nur N Dateien prüfen (0 = alle)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base = Path(args.base).resolve()
    logs_dir = base / "logs"
    log_file = setup_logging(logs_dir, args.command, args.verbose)
    log.info("snapmem %s – Befehl '%s' – Log: %s", __version__, args.command, log_file)
    handler = {"check": cmd_check, "scan": cmd_scan, "review": cmd_review, "process": cmd_process,
               "run": cmd_run, "verify": cmd_verify}[args.command]
    try:
        return handler(args)
    except KeyboardInterrupt:
        log.warning("Abgebrochen (Strg+C). Fortsetzen ist jederzeit möglich.")
        return 130
    except Exception:
        log.exception("Unerwarteter Fehler")
        return 1


if __name__ == "__main__":
    sys.exit(main())
