#!/usr/bin/env python3
"""Release-id rules for the EN/JP production pack builder."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.build_production_en_jp_packs import (
    ReleaseIdError,
    build_parser,
    generate_release_id,
    main,
    resolve_catalogue_release_id,
    validate_explicit_release_id,
)

# Example of an explicit id. The builder must not fall back to this string.
_LEGACY_FIXED = "production-packs-en-jp-20260926"
_GENERATED = re.compile(r"^production-packs-en-jp-\d{8}t\d{6}z-[0-9a-f]{8}$")


def _write_manifest(directory: Path, release_id: str, *, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(
        json.dumps({"catalogueReleaseId": release_id}) + "\n",
        encoding="utf-8",
    )
    return path


class ReleaseIdTests(unittest.TestCase):
    def test_release_id_generated_when_omitted(self) -> None:
        args = build_parser().parse_args([])
        self.assertEqual(args.catalogue_release_id, "")
        self.assertFalse(args.allow_existing_release_id)

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                first = resolve_catalogue_release_id("", output_dir=out)
                second = resolve_catalogue_release_id("   ", output_dir=out)
                stamped = resolve_catalogue_release_id(
                    "",
                    output_dir=out,
                    content_hash="ABCDEF0123456789",
                    now=datetime(2026, 10, 21, 1, 5, 0, tzinfo=timezone.utc),
                )
                zoned = datetime(
                    2026, 10, 21, 11, 5, 0, tzinfo=timezone(timedelta(hours=10))
                )
                from_input_hash = resolve_catalogue_release_id(
                    "",
                    output_dir=out,
                    content_hash="abcdef0123456789",
                    now=zoned,
                )
                unhashed = resolve_catalogue_release_id(
                    "",
                    output_dir=out,
                    content_hash="not-a-hash",
                    now=datetime(2026, 10, 21, 1, 5, 0, tzinfo=timezone.utc),
                )
            logged = stderr.getvalue()
            first_run = self._run_omitted(out / "run-a", b"source-bytes-a")
            second_run = self._run_omitted(out / "run-b", b"source-bytes-b")

        self.assertRegex(first, _GENERATED)
        self.assertRegex(second, _GENERATED)
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, _LEGACY_FIXED)
        self.assertNotEqual(second, _LEGACY_FIXED)
        self.assertIn(f"generated catalogue release id: {first}", logged)
        self.assertIn(f"generated catalogue release id: {second}", logged)
        self.assertEqual(
            stamped,
            "production-packs-en-jp-20261021t010500z-abcdef01",
        )
        self.assertEqual(from_input_hash, stamped)
        self.assertRegex(unhashed, _GENERATED)
        self.assertNotEqual(unhashed.rsplit("-", 1)[-1], "not-a-ha")
        self.assertRegex(first_run, _GENERATED)
        self.assertRegex(second_run, _GENERATED)
        self.assertNotEqual(first_run, second_run)
        self.assertNotEqual(first_run, _LEGACY_FIXED)
        self.assertNotEqual(second_run, _LEGACY_FIXED)
        self.assertTrue(first_run.endswith(hashlib.sha256(b"source-bytes-a").hexdigest()[:8]))
        self.assertTrue(second_run.endswith(hashlib.sha256(b"source-bytes-b").hexdigest()[:8]))

    def _run_omitted(self, root: Path, source_bytes: bytes) -> str:
        """Run the CLI with no release id and return the id written to the manifest."""
        root.mkdir(parents=True, exist_ok=True)
        captured: dict[str, str] = {}

        def fake_convert(**kwargs: Any) -> dict[str, Any]:
            output_db = kwargs["output_db"]
            if not isinstance(output_db, Path):
                raise AssertionError("output_db must be a path")
            output_db.parent.mkdir(parents=True, exist_ok=True)
            output_db.write_bytes(source_bytes)
            return {"languages": ["en", "ja"], "languageCounts": {"en": 1, "ja": 1}}

        def fake_build(**kwargs: Any) -> dict[str, Any]:
            release_id = kwargs["catalogue_release_id"]
            if not isinstance(release_id, str):
                raise AssertionError("catalogue_release_id must be a string")
            captured["release_id"] = release_id
            return {
                "classification": "PASS",
                "catalogueReleaseId": release_id,
                "packs": [],
            }

        database = root / "v1.sqlite"
        database.write_bytes(b"v1-input")
        stderr = io.StringIO()
        with (
            patch(
                "tools.build_production_en_jp_packs.convert_v1_search_to_global_pack_source",
                fake_convert,
            ),
            patch(
                "tools.build_production_en_jp_packs.build_all_packs",
                fake_build,
            ),
            contextlib.redirect_stderr(stderr),
        ):
            code = main(
                [
                    "--output-dir",
                    str(root / "out"),
                    "--v1-database",
                    str(database),
                    "--config",
                    str(root / "missing.json"),
                ]
            )
        self.assertEqual(code, 0)
        release_id = captured["release_id"]
        self.assertIn(f"generated catalogue release id: {release_id}", stderr.getvalue())
        public = json.loads(
            (root / "out" / "catalogue.packs.manifest.public.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(public["catalogueReleaseId"], release_id)
        return release_id

    def test_generated_release_id_passes_explicit_validation(self) -> None:
        generated = generate_release_id(
            content_hash="ABCDEF0123456789",
            now=datetime(2026, 10, 21, 1, 5, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(validate_explicit_release_id(generated), generated)
        live = generate_release_id()
        self.assertEqual(validate_explicit_release_id(live), live)

    def test_release_id_refused_after_later_build_overwrites_root_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            self.assertEqual(self._run_explicit(out, "release-id-x"), 0)
            self.assertEqual(self._run_explicit(out, "release-id-y"), 0)
            root = json.loads(
                (out / "catalogue.packs.manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(root["catalogueReleaseId"], "release-id-y")
            durable = json.loads(
                (out / "release-id-x" / "catalogue.packs.manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(durable["catalogueReleaseId"], "release-id-x")

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = main(
                    [
                        "--catalogue-release-id",
                        "release-id-x",
                        "--output-dir",
                        str(out),
                        "--v1-database",
                        str(out / "absent.sqlite"),
                        "--config",
                        str(out / "missing.json"),
                    ]
                )
            self.assertEqual(code, 1)
            self.assertIn("already exists", stderr.getvalue())
            self.assertIn(str(out / "release-id-x" / "catalogue.packs.manifest.json"), stderr.getvalue())
            self.assertFalse((out / "absent.sqlite").exists())

    def _run_explicit(self, out: Path, release_id: str) -> int:
        def fake_convert(**kwargs: Any) -> dict[str, Any]:
            output_db = kwargs["output_db"]
            if not isinstance(output_db, Path):
                raise AssertionError("output_db must be a path")
            output_db.parent.mkdir(parents=True, exist_ok=True)
            output_db.write_bytes(b"source-bytes")
            return {"languages": ["en", "ja"], "languageCounts": {"en": 1, "ja": 1}}

        def fake_build(**kwargs: Any) -> dict[str, Any]:
            built_id = kwargs["catalogue_release_id"]
            output_dir = kwargs["output_dir"]
            if not isinstance(built_id, str) or not isinstance(output_dir, Path):
                raise AssertionError("build_all_packs arguments")
            manifest = {
                "classification": "PASS",
                "catalogueReleaseId": built_id,
                "packs": [],
            }
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "catalogue.packs.manifest.json").write_text(
                json.dumps(manifest) + "\n",
                encoding="utf-8",
            )
            return manifest

        database = out / "v1.sqlite"
        database.write_bytes(b"v1-input")
        with (
            patch(
                "tools.build_production_en_jp_packs.convert_v1_search_to_global_pack_source",
                fake_convert,
            ),
            patch(
                "tools.build_production_en_jp_packs.build_all_packs",
                fake_build,
            ),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return main(
                [
                    "--catalogue-release-id",
                    release_id,
                    "--output-dir",
                    str(out),
                    "--v1-database",
                    str(database),
                    "--config",
                    str(out / "missing.json"),
                ]
            )

    def test_explicit_release_id_is_used_verbatim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            _write_manifest(
                out,
                "other-release-id",
                name="catalogue.packs.manifest.json",
            )
            cases = (
                _LEGACY_FIXED,
                "abc",
                "en.jp_pack-2026",
                "a" + ("b" * 80),
            )
            for value in cases:
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    resolved = resolve_catalogue_release_id(value, output_dir=out)
                self.assertEqual(resolved, value)
                self.assertEqual(stderr.getvalue(), "")

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                padded = resolve_catalogue_release_id(
                    f"  {_LEGACY_FIXED}\n",
                    output_dir=out,
                )
            self.assertEqual(padded, _LEGACY_FIXED)
            self.assertEqual(stderr.getvalue(), "")

    def test_invalid_release_id_rejected(self) -> None:
        invalid = (
            "AB",
            "no",
            "-leading-hyphen",
            ".dots",
            "has space",
            "slash/id",
            "a" * 82,
            "ID.with.caps",
            "  Not-Valid  ",
        )
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            for value in invalid:
                with self.assertRaises(ReleaseIdError, msg=value) as ctx:
                    resolve_catalogue_release_id(value, output_dir=out)
                self.assertIn("invalid --catalogue-release-id", str(ctx.exception))

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = main(
                    [
                        "--catalogue-release-id",
                        "Not-Valid",
                        "--output-dir",
                        str(out),
                        "--v1-database",
                        str(out / "absent.sqlite"),
                    ]
                )
            self.assertEqual(code, 1)
            self.assertIn("invalid --catalogue-release-id", stderr.getvalue())
            self.assertFalse(out.exists())

    def test_existing_release_id_refused_without_flag(self) -> None:
        release_id = "already-published-id"
        locations = (
            ("catalogue.packs.manifest.json", False),
            ("catalogue.packs.manifest.public.json", False),
            ("catalogue.packs.manifest.json", True),
            ("catalogue.packs.manifest.public.json", True),
        )
        for name, nested in locations:
            with self.subTest(name=name, nested=nested):
                with tempfile.TemporaryDirectory() as tmp:
                    out = Path(tmp)
                    directory = out / release_id if nested else out
                    manifest = _write_manifest(directory, f"  {release_id}  ", name=name)
                    with self.assertRaises(ReleaseIdError) as ctx:
                        resolve_catalogue_release_id(
                            f"  {release_id}  ",
                            output_dir=out,
                        )
                    message = str(ctx.exception)
                    self.assertIn("already exists", message)
                    self.assertIn(str(manifest), message)

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            _write_manifest(
                out,
                release_id,
                name="catalogue.packs.manifest.json",
            )
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = main(
                    [
                        "--catalogue-release-id",
                        release_id,
                        "--output-dir",
                        str(out),
                        "--v1-database",
                        str(out / "absent.sqlite"),
                    ]
                )
            self.assertEqual(code, 1)
            self.assertIn("already exists", stderr.getvalue())
            self.assertIn("--allow-existing-release-id", stderr.getvalue())
            self.assertFalse((out / "_source").exists())
            self.assertFalse((out / "absent.sqlite").exists())

    def test_existing_release_id_allowed_with_flag(self) -> None:
        release_id = "already-published-id"
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            _write_manifest(
                out,
                release_id,
                name="catalogue.packs.manifest.json",
            )
            self.assertEqual(
                resolve_catalogue_release_id(
                    release_id,
                    output_dir=out,
                    allow_existing=True,
                ),
                release_id,
            )
            args = build_parser().parse_args(["--allow-existing-release-id"])
            self.assertTrue(args.allow_existing_release_id)

            reached = False

            def stop_before_conversion(*_args: object, **_kwargs: object) -> None:
                nonlocal reached
                reached = True
                raise RuntimeError("stop before conversion")

            with patch(
                "tools.build_production_en_jp_packs.convert_v1_search_to_global_pack_source",
                stop_before_conversion,
            ):
                with self.assertRaises(RuntimeError):
                    main(
                        [
                            "--catalogue-release-id",
                            release_id,
                            "--allow-existing-release-id",
                            "--output-dir",
                            str(out),
                        ]
                    )
            self.assertTrue(reached)


if __name__ == "__main__":
    unittest.main()
