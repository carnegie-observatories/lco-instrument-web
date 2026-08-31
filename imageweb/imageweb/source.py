"""The instrument frame source: event-triggered, lazily decoded, latest frame kept.

``announce(data)`` (called by the control client on ``exposure_complete``)
records the event's ``fits_path`` — local and absolute by contract
(docs/plans/image-viewer-plan.md § Decided). The FITS is opened and decoded
only while viewers are connected; with zero clients the handler just records
the path, so a bridge nobody is looking at does no work. The source keeps
exactly **one** decoded frame (``--keep N`` history is explicitly deferred);
``get(last_seq)`` implements the shared transport's seq-keyed delivery, so a
late joiner is replayed the newest frame immediately.

Every non-structural FITS header card is forwarded verbatim with its comment
(``fits.cards`` / ``fits.comments`` in the frame header) — display verbatim,
derive nothing.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time

import numpy as np
from astropy.io import fits as pyfits

from chz1.stream import Frame

log = logging.getLogger("imageweb.source")

# Cards that describe the file rather than the instrument; everything else is forwarded.
STRUCTURAL_CARDS = {"SIMPLE", "BITPIX", "NAXIS", "NAXIS1", "NAXIS2", "BZERO", "BSCALE",
                    "EXTEND", "END", ""}


def jsonable(v):
    """A FITS card value as JSON, verbatim. Integers beyond 2**53 become strings
    so JavaScript cannot round them; NaN/Undefined -> null."""
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return str(v) if abs(v) > 2**53 else v
    if isinstance(v, float):
        return None if math.isnan(v) or math.isinf(v) else v
    if isinstance(v, str) or v is None:
        return v
    try:  # numpy scalars
        return jsonable(v.item())
    except AttributeError:
        return None


class InstrumentSource:
    """One instrument's newest science frame, decoded from the announced FITS."""

    def __init__(self, name: str):
        self.name = name
        self._cond = asyncio.Condition()
        self._frame: Frame | None = None
        self._n = 0                        # decoded-frame seq (monotonic)
        self._pending: dict | None = None  # newest announce not yet decoded
        self._decoding = False
        self._clients = 0
        self.last: dict | None = None      # {"id", "path", "at"} of the newest decoded frame
        self.error: str | None = None      # last decode failure, shown on /status

    # -- clients --------------------------------------------------------------

    def add_client(self) -> None:
        self._clients += 1
        self._kick()

    def remove_client(self) -> None:
        self._clients = max(0, self._clients - 1)

    # -- the trigger ----------------------------------------------------------

    def announce(self, data: dict) -> None:
        """An ``exposure_complete`` arrived. Record it; decode only if watched."""
        path = data.get("fits_path")
        if not path:
            log.warning("%s: exposure_complete without fits_path: %s", self.name, data)
            return
        self._pending = dict(data)
        if self._clients:
            self._kick()
        else:
            log.info("%s: recorded %s (no viewers; decode deferred)", self.name, path)

    def _kick(self) -> None:
        if self._pending is not None and not self._decoding:
            self._decoding = True
            asyncio.get_running_loop().create_task(self._decode())

    async def _decode(self) -> None:
        """Decode announces until none are pending. One at a time; a newer
        announce during a decode simply replaces the frame right after."""
        loop = asyncio.get_running_loop()
        try:
            while self._pending is not None:
                meta, self._pending = self._pending, None
                path = meta["fits_path"]
                try:
                    frame = await loop.run_in_executor(None, self._load, path, meta)
                except Exception as e:
                    self.error = f"{path}: {e}"
                    log.exception("%s: failed to load %s", self.name, path)
                    continue
                self.error = None
                async with self._cond:
                    self._frame = frame
                    self._n += 1
                    self.last = {"id": meta.get("id"), "path": path, "at": time.time()}
                    self._cond.notify_all()
                h, w = frame.data.shape
                log.info("%s: frame #%d %dx%d from %s (read %.0f ms)",
                         self.name, self._n, w, h, path, frame.read_ms)
        finally:
            self._decoding = False
            if self._pending is not None and self._clients:  # arrived during the finally
                self._kick()

    # -- the FrameSource protocol ---------------------------------------------

    async def get(self, last_seq: int) -> tuple[int, Frame]:
        async with self._cond:
            await self._cond.wait_for(lambda: self._frame is not None and self._n > last_seq)
            return self._n, self._frame

    def status(self) -> dict:
        last = self.last
        return {
            "name": self.name,
            "clients": self._clients,
            "last_seq": self._n if self._n else None,
            "image_id": last["id"] if last else None,
            "fits_path": last["path"] if last else None,
            "age_s": round(time.time() - last["at"], 1) if last else None,
            "pending": self._pending is not None or self._decoding,
            "error": self.error,
        }

    # -- the file -------------------------------------------------------------

    def _load(self, path: str, meta: dict) -> Frame:
        """FITS bytes -> Frame with the header cards as ``extra``. astropy returns
        uint16 for BITPIX=16/BZERO=32768 (what the LCO writers produce); anything
        else is clipped into uint16 — the encoder eats 16-bit unsigned only."""
        t0 = time.perf_counter()
        cards: dict = {}
        comments: dict = {}
        with pyfits.open(path) as hl:
            hdu = hl[0]
            data = hdu.data
            if data is None or data.ndim != 2:
                raise ValueError(f"no 2-D primary image (ndim={getattr(data, 'ndim', None)})")
            if data.dtype != np.uint16:
                data = np.clip(data, 0, 65535).astype(np.uint16)
            data = np.ascontiguousarray(data)
            for key in hdu.header:
                if key in STRUCTURAL_CARDS:
                    continue
                if key in ("COMMENT", "HISTORY"):  # multi-valued; join verbatim
                    cards[key] = "\n".join(str(v) for v in hdu.header[key])
                    continue
                cards[key] = jsonable(hdu.header[key])
                if hdu.header.comments[key]:
                    comments[key] = hdu.header.comments[key]
        image_id = meta.get("id")
        return Frame(
            name=f"{self.name} #{image_id}" if image_id is not None else os.path.basename(path),
            data=data,
            read_ms=(time.perf_counter() - t0) * 1e3,
            extra={
                "fits": {
                    "id": jsonable(image_id),
                    "path": path,
                    "cards": cards,
                    "comments": comments,
                },
            },
        )
