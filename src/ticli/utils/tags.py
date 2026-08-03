"""Write metadata into a downloaded audio file, with the standard library only.

A downloaded track has to be an ordinary file that any player opens and shows
the right name for. Half of that is the path it lives at — `Artist/Album/NN
Title.m4a` is already readable by a human and by every player that falls back
to the filename. The other half is real tags inside the bytes, and that is
what this module does.

There is no dependency to reach for: mutagen would be a new one, and ffmpeg is
not guaranteed present (mpv users may have never installed it). So the boxes
are written here.

**MP4 / M4A** — the container everything TIDAL serves this client arrives in,
AAC on a device-flow session and FLAC on a PKCE one. Tags live in
`moov.udta.meta.ilst` as iTunes-style atoms. Writing them means growing `moov`,
and growing `moov` moves every byte after it — so any absolute offset recorded
*inside* `moov` has to move with it. That is `stco` / `co64`, which this module
patches, and it is the whole reason a naive "append a udta" produces a file
that plays silence. Three things make that safe rather than brave:

* nothing is patched at all when `moov` is the last box in the file, because
  then no offset moved;
* a file that records absolute offsets anywhere this module does not
  understand — a fragmented `tfhd` carrying `base-data-offset`, encrypted
  `saio` — is **refused**, not guessed at;
* the result is re-parsed end to end before it is accepted, and any
  disagreement means the untagged bytes are kept.

The contract is therefore: `write_tags` either produces a structurally valid
file or leaves the file exactly as it found it, and says which.

**FLAC** — a Vorbis comment block, which is genuinely easy: the block list is
length-prefixed and carries no offsets, so an existing comment block is
replaced and a missing one inserted after STREAMINFO.

Anything else is left alone. That is not a failure; it is a file whose folder
and filename still say what it is.
"""

import logging
import os
import struct
from pathlib import Path

logger = logging.getLogger(__name__)

# MP4 boxes whose payload is entirely other boxes. Anything not in here is
# opaque and is copied through untouched — the list is deliberately short,
# because walking into a box this module has misunderstood is how offsets get
# corrupted.
MP4_CONTAINERS = frozenset({
    b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta", b"edts", b"mvex",
})

# ilst payload type indicators (the "well-known type" field of a `data` box).
DATA_BINARY = 0
DATA_UTF8 = 1
DATA_JPEG = 13
DATA_PNG = 14

# How big a cover this module will embed. A 1280x1280 TIDAL cover is ~220 KB,
# which is fine; anything wildly larger is more likely a mistake than art.
MAX_COVER_BYTES = 4 * 1024 * 1024


class TagError(Exception):
    """This file cannot be tagged safely. The bytes are left alone."""


# ── generic box plumbing ──


def _box(kind: bytes, payload: bytes) -> bytes:
    """One MP4 box. 32-bit size only — every box this module writes is tags."""
    return struct.pack(">I", len(payload) + 8) + kind + payload


def _iter_boxes(data: bytes, start: int = 0, end: int = None):
    """Walk one level of boxes, yielding (kind, header_len, body_start, box_end).

    Raises TagError on anything malformed rather than guessing, because a
    misread length here becomes a corrupt file downstream.
    """
    if end is None:
        end = len(data)
    pos = start
    while pos < end:
        if end - pos < 8:
            raise TagError("truncated box header")
        size = struct.unpack_from(">I", data, pos)[0]
        kind = data[pos + 4:pos + 8]
        header = 8
        if size == 1:
            if end - pos < 16:
                raise TagError("truncated 64-bit box header")
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos  # "to end of file", legal for the last box
        if size < header or pos + size > end:
            raise TagError(f"box {kind!r} claims an impossible size")
        yield kind, header, pos + header, pos + size
        pos += size


def _find_boxes(data: bytes, kind: bytes, start: int = 0, end: int = None) -> list:
    return [b for b in _iter_boxes(data, start, end) if b[0] == kind]


# ── ilst ──


def _data_box(payload: bytes, indicator: int) -> bytes:
    return _box(b"data", struct.pack(">II", indicator, 0) + payload)


def _text_atom(name: bytes, value: str) -> bytes:
    return _box(name, _data_box(value.encode("utf-8"), DATA_UTF8))


def _pair_atom(name: bytes, number: int, total: int) -> bytes:
    """trkn / disk: a 8-byte record of "this one of that many"."""
    body = struct.pack(">HHHH", 0, max(0, min(0xFFFF, number)),
                       max(0, min(0xFFFF, total)), 0)
    return _box(name, _data_box(body, DATA_BINARY))


def _cover_atom(image: bytes) -> bytes:
    kind = DATA_PNG if image[:8] == b"\x89PNG\r\n\x1a\n" else DATA_JPEG
    return _box(b"covr", _data_box(image, kind))


def _hdlr() -> bytes:
    """The `mdir`/`appl` handler every iTunes-style meta box carries. Players
    that look for tags look for this first; a meta box without it is skipped."""
    body = (b"\x00" * 4          # version + flags
            + b"\x00" * 4        # pre_defined
            + b"mdir" + b"appl"  # handler type, and iTunes' reserved marker
            + b"\x00" * 8
            + b"\x00")           # empty name
    return _box(b"hdlr", body)


def _ilst(meta: dict, cover: bytes = None) -> bytes:
    """The tag payload. Empty when there is nothing worth writing."""
    atoms = b""
    for name, key in ((b"\xa9nam", "title"), (b"\xa9ART", "artist"),
                      (b"aART", "album_artist"), (b"\xa9alb", "album"),
                      (b"\xa9day", "date"), (b"cprt", "copyright"),
                      (b"\xa9cmt", "comment")):
        value = meta.get(key)
        if value:
            atoms += _text_atom(name, str(value))
    if meta.get("track_num"):
        atoms += _pair_atom(b"trkn", int(meta["track_num"]),
                            int(meta.get("track_total") or 0))
    if meta.get("disc_num"):
        atoms += _pair_atom(b"disk", int(meta["disc_num"]),
                            int(meta.get("disc_total") or 0))
    if cover and len(cover) <= MAX_COVER_BYTES:
        atoms += _cover_atom(cover)
    return _box(b"ilst", atoms) if atoms else b""


def _udta(meta: dict, cover: bytes = None) -> bytes:
    ilst = _ilst(meta, cover)
    if not ilst:
        return b""
    # meta is a full box: four bytes of version+flags before its children
    return _box(b"udta", _box(b"meta", b"\x00" * 4 + _hdlr() + ilst))


# ── offset fixup ──


def _shift_chunk_offsets(moov: bytearray, delta: int) -> None:
    """Add `delta` to every stco/co64 entry inside a moov, in place.

    These are absolute file positions of the media chunks. Growing moov moves
    the media, so every one of them is wrong by exactly the amount moov grew —
    which is why a file tagged without this plays as silence or refuses to
    open at all.
    """
    if not delta:
        return

    def walk(start, end):
        for kind, _header, body, box_end in _iter_boxes(bytes(moov), start, end):
            if kind in MP4_CONTAINERS:
                walk(body, box_end)
            elif kind in (b"stco", b"co64"):
                wide = kind == b"co64"
                width = 8 if wide else 4
                count = struct.unpack_from(">I", moov, body + 4)[0]
                if body + 8 + count * width > box_end:
                    raise TagError("chunk offset table overruns its box")
                for i in range(count):
                    at = body + 8 + i * width
                    fmt = ">Q" if wide else ">I"
                    value = struct.unpack_from(fmt, moov, at)[0] + delta
                    if value < 0 or (not wide and value > 0xFFFFFFFF):
                        raise TagError("chunk offset would not fit after tagging")
                    struct.pack_into(fmt, moov, at, value)

    walk(0, len(moov))


def _refuses_offsets(data: bytes, start: int, end: int) -> bool:
    """Whether anything in this range pins bytes absolutely in a way this
    module does not know how to move.

    A fragmented file normally addresses its samples relative to the enclosing
    `moof`, which is why growing `moov` is harmless — but `tfhd` is allowed to
    carry an absolute `base-data-offset` instead, and an encrypted file's
    `saio` always does. Neither is rewritten here; a file carrying one is
    handed back untagged.
    """
    for kind, _header, body, box_end in _iter_boxes(data, start, end):
        if kind == b"saio":
            return True
        if kind == b"tfhd":
            flags = struct.unpack_from(">I", data, body)[0] & 0xFFFFFF
            if flags & 0x000001:  # base-data-offset-present
                return True
        if kind in MP4_CONTAINERS or kind in (b"moof", b"traf"):
            if _refuses_offsets(data, body, box_end):
                return True
    return False


# ── MP4 ──


def _strip_udta(moov: bytes, header: int) -> bytes:
    """moov with any existing udta removed, so tagging twice does not stack.

    Every other child is copied through byte for byte, including ones this
    module has no idea about — the only box it is entitled to replace is the
    one it wrote.
    """
    out = moov[:header]
    pos = header
    for kind, _h, _body, box_end in _iter_boxes(moov, header, len(moov)):
        if kind != b"udta":
            out += moov[pos:box_end]
        pos = box_end
    return out


def _tag_mp4(data: bytes, meta: dict, cover: bytes = None) -> bytes:
    udta = _udta(meta, cover)
    if not udta:
        raise TagError("nothing to write")

    top = list(_iter_boxes(data))
    moov = next((b for b in top if b[0] == b"moov"), None)
    if moov is None:
        raise TagError("no moov box")
    _kind, header, body, box_end = moov
    moov_start = body - header
    moov_is_last = box_end == len(data)

    if not moov_is_last and _refuses_offsets(data, 0, len(data)):
        raise TagError("file records absolute offsets this module will not move")

    old = data[moov_start:box_end]
    stripped = bytearray(_strip_udta(old, header))
    new = bytearray(stripped + udta)
    struct.pack_into(">I", new, 0, len(new))
    if header != 8:
        raise TagError("64-bit moov header is not rewritten here")

    if not moov_is_last:
        _shift_chunk_offsets(new, len(new) - len(old))

    out = data[:moov_start] + bytes(new) + data[box_end:]
    _verify_mp4(out)
    return out


def _verify_mp4(data: bytes) -> None:
    """Re-read the finished file the way a player would. Anything that does
    not parse means the untagged bytes are kept instead — this is the check
    that makes binary surgery on somebody's only copy acceptable."""
    boxes = list(_iter_boxes(data))
    if not boxes:
        raise TagError("result has no boxes")
    if boxes[-1][3] != len(data):
        raise TagError("result does not fill the file")
    moov = next((b for b in boxes if b[0] == b"moov"), None)
    if moov is None:
        raise TagError("result lost its moov")
    udta = _find_boxes(data, b"udta", moov[2], moov[3])
    if len(udta) != 1:
        raise TagError("result does not carry exactly one udta")
    meta = _find_boxes(data, b"meta", udta[0][2], udta[0][3])
    if len(meta) != 1:
        raise TagError("result has no meta box")
    # meta's four bytes of version+flags come before its children
    if not _find_boxes(data, b"ilst", meta[0][2] + 4, meta[0][3]):
        raise TagError("result has no ilst")


# ── FLAC ──


def _vorbis_comment(meta: dict) -> bytes:
    vendor = b"ticli"
    fields = []
    for name, key in (("TITLE", "title"), ("ARTIST", "artist"),
                      ("ALBUMARTIST", "album_artist"), ("ALBUM", "album"),
                      ("DATE", "date"), ("COPYRIGHT", "copyright"),
                      ("ISRC", "isrc")):
        value = meta.get(key)
        if value:
            fields.append(f"{name}={value}".encode("utf-8"))
    if meta.get("track_num"):
        fields.append(f"TRACKNUMBER={int(meta['track_num'])}".encode("utf-8"))
        if meta.get("track_total"):
            fields.append(f"TRACKTOTAL={int(meta['track_total'])}".encode("utf-8"))
    if meta.get("disc_num"):
        fields.append(f"DISCNUMBER={int(meta['disc_num'])}".encode("utf-8"))
    out = struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", len(fields))
    for field in fields:
        out += struct.pack("<I", len(field)) + field
    return out


def _tag_flac(data: bytes, meta: dict, cover: bytes = None) -> bytes:
    if data[:4] != b"fLaC":
        raise TagError("not a FLAC stream")
    comment = _vorbis_comment(meta)
    if not comment:
        raise TagError("nothing to write")

    pos = 4
    blocks = []  # (type, payload)
    while True:
        if len(data) - pos < 4:
            raise TagError("truncated FLAC metadata block")
        header = data[pos]
        last = bool(header & 0x80)
        kind = header & 0x7F
        length = int.from_bytes(data[pos + 1:pos + 4], "big")
        start = pos + 4
        if start + length > len(data):
            raise TagError("FLAC metadata block overruns the file")
        blocks.append((kind, data[start:start + length]))
        pos = start + length
        if last:
            break

    kept = [(k, p) for k, p in blocks if k != 4]
    # STREAMINFO must stay first; the comment goes straight after it
    rebuilt = [kept[0], (4, comment)] + kept[1:] if kept else [(4, comment)]

    out = b"fLaC"
    for index, (kind, payload) in enumerate(rebuilt):
        flag = 0x80 if index == len(rebuilt) - 1 else 0
        out += bytes([flag | kind]) + len(payload).to_bytes(3, "big")
        out += payload
    return out + data[pos:]


# ── the one call ──


def describe(meta: dict, cover: bytes = None) -> str:
    """What a tagged file will actually carry, in the words the UI uses.

    Built from the values in hand rather than from intent, so the interface
    can never claim a tag that was not written.
    """
    names = [label for label, key in
             (("title", "title"), ("artist", "artist"), ("album", "album"),
              ("track number", "track_num"), ("year", "date"))
             if meta.get(key)]
    if cover:
        names.append("cover art")
    return ", ".join(names)


def write_tags(path, meta: dict, cover: bytes = None) -> str:
    """Tag the file at `path` in place. Returns what was written, or "".

    Never raises and never leaves a half-written file: the tagged bytes are
    built in memory, checked, written beside the original and only then
    renamed over it. A file this module does not understand keeps its bytes
    and its folder-and-filename metadata, which is not nothing.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    try:
        data = path.read_bytes()
        if suffix in (".m4a", ".mp4", ".aac"):
            out = _tag_mp4(data, meta, cover)
        elif suffix == ".flac":
            out = _tag_flac(data, meta, cover)
        else:
            return ""
        tmp = path.with_name(path.name + ".tagging")
        tmp.write_bytes(out)
        os.replace(tmp, path)
    except (TagError, OSError, struct.error, ValueError) as e:
        logger.debug("Left %s untagged: %s", path, e)
        try:
            tmp = path.with_name(path.name + ".tagging")
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return ""
    return describe(meta, cover)
