# SPDX-FileCopyrightText: 2026 Christian Faur
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tiny dependency-free vector PDF writer for Polyshell Builder assembly guides."""

from __future__ import annotations


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


class PDFPage:
    def __init__(self, width: float = 612.0, height: float = 792.0):
        self.width = width
        self.height = height
        self.commands: list[str] = []

    def text(
        self,
        x: float,
        y: float,
        text: str,
        size: float = 10.0,
        bold: bool = False,
        color: tuple[float, float, float] | None = None,
    ) -> None:
        font = "F2" if bold else "F1"
        fill = f"{color[0]:.3f} {color[1]:.3f} {color[2]:.3f} rg " if color else ""
        self.commands.append(
            f"q {fill}BT /{font} {size:.2f} Tf {x:.2f} {y:.2f} Td ({_escape(text)}) Tj ET Q"
        )

    def line(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        width: float = 0.6,
        gray: float = 0.25,
        color: tuple[float, float, float] | None = None,
    ) -> None:
        stroke = (
            f"{color[0]:.3f} {color[1]:.3f} {color[2]:.3f} RG " if color else f"{gray:.3f} G "
        )
        self.commands.append(
            f"q {stroke}{width:.2f} w {x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S Q"
        )

    def polygon(
        self,
        points: list[tuple[float, float]],
        fill: tuple[float, float, float] = (0.90, 0.92, 0.95),
        stroke: tuple[float, float, float] = (0.18, 0.20, 0.24),
        width: float = 0.55,
        opacity: float = 1.0,
    ) -> None:
        if len(points) < 3:
            return
        path = [f"{points[0][0]:.2f} {points[0][1]:.2f} m"]
        path.extend(f"{x:.2f} {y:.2f} l" for x, y in points[1:])
        path.append("h B")
        graphics_state = "/GS1 gs " if opacity < 0.999 else ""
        self.commands.append(
            f"q {graphics_state}{fill[0]:.3f} {fill[1]:.3f} {fill[2]:.3f} rg "
            f"{stroke[0]:.3f} {stroke[1]:.3f} {stroke[2]:.3f} RG {width:.2f} w " + " ".join(path) + " Q"
        )

    def rectangle(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
        fill: tuple[float, float, float] | None = None,
        stroke_gray: float = 0.75,
    ) -> None:
        if fill is None:
            self.commands.append(f"q {stroke_gray:.3f} G 0.5 w {x:.2f} {y:.2f} {width:.2f} {height:.2f} re S Q")
        else:
            self.commands.append(
                f"q {fill[0]:.3f} {fill[1]:.3f} {fill[2]:.3f} rg {stroke_gray:.3f} G 0.5 w "
                f"{x:.2f} {y:.2f} {width:.2f} {height:.2f} re B Q"
            )

    def stream(self) -> bytes:
        return ("\n".join(self.commands) + "\n").encode("latin-1", errors="replace")


class PDFDocument:
    def __init__(self):
        self.pages: list[PDFPage] = []

    def add_page(self, width: float = 612.0, height: float = 792.0) -> PDFPage:
        page = PDFPage(width, height)
        self.pages.append(page)
        return page

    def save(self, filepath: str) -> None:
        # IDs: 1 catalog, 2 page tree, 3 Helvetica, 4 Helvetica-Bold,
        # 5 translucent fill state, then alternating page/content objects.
        objects: dict[int, bytes] = {}
        objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
        objects[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
        objects[4] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>"
        objects[5] = b"<< /Type /ExtGState /ca 0.72 /CA 0.90 >>"
        kids: list[str] = []
        for index, page in enumerate(self.pages):
            page_id = 6 + index * 2
            content_id = page_id + 1
            kids.append(f"{page_id} 0 R")
            objects[page_id] = (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page.width:.0f} {page.height:.0f}] "
                f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> "
                f"/ExtGState << /GS1 5 0 R >> >> /Contents {content_id} 0 R >>"
            ).encode("ascii")
            stream = page.stream()
            objects[content_id] = f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"endstream"
        objects[2] = f"<< /Type /Pages /Count {len(self.pages)} /Kids [{' '.join(kids)}] >>".encode("ascii")

        maximum = max(objects)
        output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0] * (maximum + 1)
        for object_id in range(1, maximum + 1):
            offsets[object_id] = len(output)
            output.extend(f"{object_id} 0 obj\n".encode("ascii"))
            output.extend(objects[object_id])
            output.extend(b"\nendobj\n")
        xref = len(output)
        output.extend(f"xref\n0 {maximum + 1}\n".encode("ascii"))
        output.extend(b"0000000000 65535 f \n")
        for object_id in range(1, maximum + 1):
            output.extend(f"{offsets[object_id]:010d} 00000 n \n".encode("ascii"))
        output.extend(
            f"trailer\n<< /Size {maximum + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
        )
        with open(filepath, "wb") as handle:
            handle.write(output)
