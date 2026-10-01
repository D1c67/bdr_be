"""rfp_zip: the harvest's zip opener over generated archives (docs/RFP_HARVEST.md
2.5, "Zips"): name sanitising (traversal, drive letters, backslashes),
the silently dropped litter (__MACOSX, dotfiles, Thumbs.db, desktop.ini,
directories), every skipped_reason, the bomb ratio, the declared-total cap,
the member cap, and extract_member's inflated-byte cap with O_EXCL."""

from __future__ import annotations

import io
import zipfile

import pytest

from app.services import rfp_zip

PDF = b"%PDF-1.7 fake body\n" + b"x" * 500
IMAGES = {"jpg", "jpeg", "png", "gif"}


def _is_image(name: str) -> bool:
    return name.rsplit(".", 1)[-1].lower() in IMAGES if "." in name else False


def _build(tmp_path, entries, name="a.zip", compression=zipfile.ZIP_DEFLATED):
    """entries: (name, bytes) or (ZipInfo, bytes)."""
    path = tmp_path / name
    with zipfile.ZipFile(path, "w", compression=compression) as zf:
        for entry, data in entries:
            zf.writestr(entry, data)
    return path


def _mark_encrypted(path, name: str) -> None:
    """Set the encryption flag (general-purpose bit 0) on `name` in the
    written archive: writestr resets flag_bits, so the bytes are patched
    in both the local header and the central directory entry."""
    data = bytearray(path.read_bytes())
    encoded = name.encode()
    for sig, flag_at, name_at in ((b"PK\x03\x04", 6, 30), (b"PK\x01\x02", 8, 46)):
        pos = 0
        while (pos := data.find(sig, pos)) != -1:
            if data[pos + name_at: pos + name_at + len(encoded)] == encoded:
                data[pos + flag_at] |= 0x1
            pos += 4
    path.write_bytes(bytes(data))


def _inspect(path, **over):
    kw = dict(max_members=200, max_total_bytes=10 * 1024 * 1024, per_member_max_bytes=1024 * 1024,
              is_image_name=_is_image)
    kw.update(over)
    return rfp_zip.inspect(path, **kw)


# ── is_zip ───────────────────────────────────────────────────────────────


def test_is_zip_reads_the_magic(tmp_path):
    path = _build(tmp_path, [("a.pdf", PDF)])
    assert rfp_zip.is_zip(path)
    (tmp_path / "not.zip").write_bytes(b"%PDF-1.7 nope")
    assert not rfp_zip.is_zip(tmp_path / "not.zip")
    assert not rfp_zip.is_zip(tmp_path / "missing.zip")


# ── inspect ──────────────────────────────────────────────────────────────


def test_inspect_lists_members_in_order_with_nested_paths(tmp_path):
    path = _build(tmp_path, [
        ("Specs/Div 26.pdf", PDF),
        ("Drawings/", b""),                       # a directory entry
        ("Drawings/E-101.pdf", PDF + b"y" * 100),
    ])
    listing = _inspect(path)
    assert listing.error is None and listing.error_kind is None and not listing.truncated
    assert [(m.index, m.path, m.size, m.skipped_reason) for m in listing.members] == [
        (0, "Specs/Div 26.pdf", len(PDF), None),
        (2, "Drawings/E-101.pdf", len(PDF) + 100, None),
    ]


def test_inspect_drops_litter_silently(tmp_path):
    path = _build(tmp_path, [
        ("__MACOSX/._E-101.pdf", b"junk"),
        ("Drawings/.DS_Store", b"junk"),
        ("Thumbs.db", b"junk"),
        ("desktop.ini", b"junk"),
        ("Drawings/.hidden.pdf", PDF),
        ("E-101.pdf", PDF),
    ])
    listing = _inspect(path)
    assert [m.path for m in listing.members] == ["E-101.pdf"]


def test_inspect_sanitises_names(tmp_path):
    path = _build(tmp_path, [
        ("/abs/Spec.pdf", PDF),
        ("C:\\Users\\gc\\Plans\\E-101.pdf", PDF),
        ("./Addendum 1/./A-1.pdf", PDF),
        ("../../etc/passwd.pdf", PDF),
        ("Plans/../../evil.pdf", PDF),
    ])
    listing = _inspect(path)
    got = [(m.path, m.skipped_reason) for m in listing.members]
    assert got == [
        ("abs/Spec.pdf", None),
        ("Users/gc/Plans/E-101.pdf", None),
        ("Addendum 1/A-1.pdf", None),
        ("passwd.pdf", "unsafe_name"),
        ("evil.pdf", "unsafe_name"),
    ]
    assert all(not m.path.startswith("/") and ".." not in m.path for m in listing.members)


def test_inspect_flags_encrypted_members(tmp_path):
    path = _build(tmp_path, [("secret.pdf", PDF), ("open.pdf", PDF)])
    _mark_encrypted(path, "secret.pdf")
    listing = _inspect(path)
    assert [(m.path, m.skipped_reason) for m in listing.members] == [
        ("secret.pdf", "encrypted"), ("open.pdf", None),
    ]


def test_inspect_flags_nested_archives_by_extension_and_by_magic(tmp_path):
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr("inside.pdf", PDF)
    path = _build(tmp_path, [
        ("Bid Docs.zip", inner.getvalue()),
        ("mystery.bin", inner.getvalue()),        # a zip hiding behind another extension
        ("archive.7z", b"7z\xbc\xaf\x27\x1c" + b"x" * 40),
        ("plain.pdf", PDF),
        # Office documents are zip containers by design (seen live: a GC's
        # Q&A .docx inside a Dropbox folder zip); they are documents, not
        # nested archives, whatever their magic says.
        ("02 Addenda/Q  A 09-12-26.docx", inner.getvalue()),
        ("Bid Form.XLSX", inner.getvalue()),
    ])
    listing = _inspect(path)
    assert [(m.path, m.skipped_reason) for m in listing.members] == [
        ("Bid Docs.zip", "nested_zip"),
        ("mystery.bin", "nested_zip"),
        ("archive.7z", "nested_zip"),
        ("plain.pdf", None),
        ("02 Addenda/Q  A 09-12-26.docx", None),
        ("Bid Form.XLSX", None),
    ]


def test_inspect_applies_the_callers_image_policy(tmp_path):
    path = _build(tmp_path, [("Photos/site.JPG", b"\xff\xd8" * 50), ("E-101.pdf", PDF)])
    listing = _inspect(path)
    assert [(m.path, m.skipped_reason) for m in listing.members] == [
        ("Photos/site.JPG", "image"), ("E-101.pdf", None),
    ]
    # The policy is the caller's: without one, the photo is downloadable.
    assert [m.skipped_reason for m in _inspect(path, is_image_name=lambda n: False).members] == [None, None]


def test_inspect_flags_empty_and_oversize_members(tmp_path):
    path = _build(tmp_path, [
        ("empty.pdf", b""),
        ("big.pdf", b"x" * 2048),
        ("ok.pdf", PDF),
    ])
    listing = _inspect(path, per_member_max_bytes=1024)
    assert [(m.path, m.skipped_reason) for m in listing.members] == [
        ("empty.pdf", "empty"), ("big.pdf", "too_large"), ("ok.pdf", None),
    ]


def test_inspect_refuses_a_decompression_bomb(tmp_path):
    path = _build(tmp_path, [("zeros.bin", b"\0" * (2 * 1024 * 1024)), ("ok.pdf", PDF)])
    listing = _inspect(path, per_member_max_bytes=10 * 1024 * 1024)
    assert listing.members == [] and listing.error_kind == "bomb"
    assert listing.error == "The zip looks like a decompression bomb."


def test_inspect_tolerates_a_small_highly_compressible_member(tmp_path):
    # 200 KB of one repeated row compresses far past 200:1 but cannot hurt
    # anyone; only members from 1 MB up count for the ratio.
    path = _build(tmp_path, [("rows.csv", b"1,2,3,4,5,6\n" * 17000)])
    listing = _inspect(path)
    assert listing.error is None and listing.members[0].skipped_reason is None


def test_inspect_refuses_a_declared_total_over_the_cap(tmp_path):
    path = _build(tmp_path, [("a.pdf", b"a" * 600), ("b.pdf", b"b" * 600)])
    listing = _inspect(path, max_total_bytes=1000)
    assert listing.members == [] and listing.error_kind == "too_large"
    assert "more bytes than the harvest accepts" in listing.error
    # Skipped members do not count: the oversize one is skipped, the rest fits.
    listing = _inspect(path, max_total_bytes=1000, per_member_max_bytes=500)
    assert listing.error is None
    assert [m.skipped_reason for m in listing.members] == ["too_large", "too_large"]


def test_inspect_caps_the_member_count(tmp_path):
    path = _build(tmp_path, [(f"f{i}.pdf", PDF) for i in range(5)] + [
        (f"p{i}.jpg", b"\xff\xd8" * 10) for i in range(4)
    ])
    listing = _inspect(path, max_members=3)
    assert listing.truncated
    kept = [m.path for m in listing.members if m.skipped_reason is None]
    skipped = [m.path for m in listing.members if m.skipped_reason == "image"]
    # Downloadable and skipped members are capped separately, so photos can
    # never crowd out drawings.
    assert kept == ["f0.pdf", "f1.pdf", "f2.pdf"] and skipped == ["p0.jpg", "p1.jpg", "p2.jpg"]


def test_inspect_answers_an_error_for_a_non_zip(tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"%PDF-1.7 not a zip")
    listing = _inspect(bad)
    assert listing.members == [] and listing.error_kind == "not_zip" and listing.error


def test_inspect_answers_an_error_for_a_truncated_zip(tmp_path):
    path = _build(tmp_path, [("a.pdf", PDF)])
    data = path.read_bytes()
    cut = tmp_path / "cut.zip"
    cut.write_bytes(data[: len(data) // 2])
    listing = _inspect(cut)
    assert listing.members == [] and listing.error_kind == "not_zip"


# ── extract_member ───────────────────────────────────────────────────────


def test_extract_member_inflates_the_member(tmp_path):
    path = _build(tmp_path, [("Specs/Div 26.pdf", PDF), ("E-101.pdf", PDF * 3)])
    dest = tmp_path / "out.pdf"
    written = rfp_zip.extract_member(path, 1, dest, max_bytes=len(PDF) * 3)
    assert written == len(PDF) * 3 and dest.read_bytes() == PDF * 3


def test_extract_member_caps_on_inflated_bytes_and_unlinks(tmp_path):
    path = _build(tmp_path, [("zeros.bin", b"\0" * 300_000)])
    dest = tmp_path / "out.bin"
    with pytest.raises(rfp_zip.ZipTooLarge) as exc:
        rfp_zip.extract_member(path, 0, dest, max_bytes=100_000)
    assert "larger than the per-file limit" in str(exc.value)
    assert not dest.exists()


def test_extract_member_refuses_bad_members(tmp_path):
    path = _build(tmp_path, [("Drawings/", b""), ("secret.pdf", PDF), ("ok.pdf", PDF)])
    _mark_encrypted(path, "secret.pdf")
    for index in (0, 1, 7, -1):
        dest = tmp_path / f"out{index}.pdf"
        with pytest.raises(rfp_zip.ZipError):
            rfp_zip.extract_member(path, index, dest, max_bytes=10_000)
        assert not dest.exists()


def test_extract_member_reports_a_corrupt_member(tmp_path):
    path = _build(tmp_path, [("ok.pdf", PDF * 20)])
    data = bytearray(path.read_bytes())
    # Scribble over the deflated body (past the 30-byte local header + name).
    for i in range(40, 80):
        data[i] ^= 0xFF
    path.write_bytes(bytes(data))
    dest = tmp_path / "out.pdf"
    with pytest.raises(rfp_zip.ZipError):
        rfp_zip.extract_member(path, 0, dest, max_bytes=100_000)
    assert not dest.exists()


def test_extract_member_never_overwrites(tmp_path):
    path = _build(tmp_path, [("ok.pdf", PDF)])
    dest = tmp_path / "out.pdf"
    dest.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        rfp_zip.extract_member(path, 0, dest, max_bytes=10_000)
    assert dest.read_bytes() == b"keep"


def test_extract_member_validates_the_cap(tmp_path):
    path = _build(tmp_path, [("ok.pdf", PDF)])
    with pytest.raises(ValueError):
        rfp_zip.extract_member(path, 0, tmp_path / "x", max_bytes=0)
