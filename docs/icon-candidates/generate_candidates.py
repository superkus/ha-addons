#!/usr/bin/env python3
"""Generate original, non-deployed Remote Energy Meter branding candidates."""

from __future__ import annotations

import hashlib
import json
import math
import textwrap
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any, Final

from PIL import Image, ImageDraw, ImageFont

OUTPUT_ROOT: Final = Path(__file__).resolve().parent
PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
NAVY: Final = "#10233F"
NAVY_LIGHT: Final = "#18385D"
WHITE: Final = "#F7FAFC"
SHEET_LIGHT: Final = "#EEF3F7"
SHEET_DARK: Final = "#111827"
SUPERSAMPLE: Final = 4

Shape = dict[str, Any]
Scene = list[Shape]


@dataclass(frozen=True, slots=True)
class Concept:
    number: int
    slug: str
    name: str
    rationale: str
    primary: str
    secondary: str


CONCEPTS: Final = (
    Concept(
        1,
        "pulse-dial",
        "Pulse Dial",
        "A bright energy bolt inside a calibrated dial reads instantly as measured electrical power.",
        "#27C2D1",
        "#FFC857",
    ),
    Concept(
        2,
        "server-current",
        "Server Current",
        "A three-bay server stack crossed by a live power trace emphasizes remote infrastructure metering.",
        "#39D98A",
        "#4DB8FF",
    ),
    Concept(
        3,
        "orbit-meter",
        "Orbit Meter",
        "Network nodes orbit a central gauge to express one meter observing multiple remote endpoints.",
        "#9D7CFF",
        "#31D6C4",
    ),
    Concept(
        4,
        "watt-link",
        "Watt Link",
        "A connected waveform inside a diamond combines telemetry, networking, and energy flow.",
        "#FF8A4C",
        "#58A6FF",
    ),
)

PIXEL_FONT: Final = {
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "G": ("01110", "10001", "10000", "10111", "10001", "10001", "01110"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
}


def rounded_rect(
    box: tuple[float, float, float, float],
    radius: float,
    fill: str,
    *,
    stroke: str | None = None,
    width: float = 0,
) -> Shape:
    return {"kind": "rounded_rect", "box": box, "radius": radius, "fill": fill, "stroke": stroke, "width": width}


def ellipse(
    box: tuple[float, float, float, float],
    fill: str | None,
    *,
    stroke: str | None = None,
    width: float = 0,
) -> Shape:
    return {"kind": "ellipse", "box": box, "fill": fill, "stroke": stroke, "width": width}


def line(points: list[tuple[float, float]], stroke: str, width: float) -> Shape:
    return {"kind": "line", "points": points, "stroke": stroke, "width": width}


def polygon(
    points: list[tuple[float, float]],
    fill: str,
    *,
    stroke: str | None = None,
    width: float = 0,
) -> Shape:
    return {"kind": "polygon", "points": points, "fill": fill, "stroke": stroke, "width": width}


def arc(box: tuple[float, float, float, float], start: float, end: float, stroke: str, width: float) -> Shape:
    return {"kind": "arc", "box": box, "start": start, "end": end, "stroke": stroke, "width": width}


def _icon_scene(concept: Concept) -> Scene:
    if concept.number == 1:
        return [
            ellipse((34, 34, 478, 478), NAVY, stroke=concept.primary, width=18),
            arc((78, 78, 434, 434), 145, 395, concept.primary, 18),
            line([(126, 318), (96, 336)], WHITE, 13),
            line([(146, 190), (116, 164)], WHITE, 13),
            line([(256, 145), (256, 106)], WHITE, 13),
            line([(366, 190), (396, 164)], WHITE, 13),
            line([(386, 318), (416, 336)], WHITE, 13),
            polygon(
                [(278, 98), (176, 278), (241, 278), (216, 414), (341, 220), (274, 220)],
                concept.secondary,
            ),
            ellipse((238, 260, 274, 296), concept.primary),
        ]
    if concept.number == 2:
        scene: Scene = [rounded_rect((34, 34, 478, 478), 92, NAVY, stroke=concept.primary, width=18)]
        for top in (92, 224, 356):
            scene.extend(
                [
                    rounded_rect((86, top, 426, top + 94), 25, NAVY_LIGHT, stroke=concept.primary, width=10),
                    ellipse((112, top + 31, 144, top + 63), concept.secondary),
                    line([(166, top + 47), (216, top + 47)], WHITE, 11),
                    line([(360, top + 47), (390, top + 47)], concept.primary, 11),
                ]
            )
        scene.extend(
            [
                line(
                    [(174, 271), (224, 271), (253, 230), (289, 318), (322, 271), (392, 271)],
                    concept.secondary,
                    18,
                ),
                ellipse((243, 260, 265, 282), WHITE),
            ]
        )
        return scene
    if concept.number == 3:
        return [
            polygon(
                [(256, 31), (449, 143), (449, 369), (256, 481), (63, 369), (63, 143)],
                NAVY,
                stroke=concept.primary,
                width=18,
            ),
            ellipse((83, 151, 429, 361), None, stroke=concept.secondary, width=11),
            ellipse((159, 76, 353, 436), None, stroke=concept.primary, width=11),
            line([(108, 256), (205, 256)], concept.secondary, 11),
            line([(307, 205), (394, 176)], concept.primary, 11),
            line([(307, 307), (394, 336)], concept.secondary, 11),
            ellipse((171, 171, 341, 341), NAVY_LIGHT, stroke=WHITE, width=11),
            arc((194, 194, 318, 318), 145, 395, concept.primary, 12),
            line([(256, 271), (306, 211)], concept.secondary, 18),
            ellipse((240, 255, 272, 287), WHITE),
            ellipse((86, 234, 130, 278), concept.secondary),
            ellipse((372, 154, 416, 198), concept.primary),
            ellipse((372, 314, 416, 358), concept.secondary),
        ]
    return [
        polygon(
            [(256, 31), (481, 256), (256, 481), (31, 256)],
            NAVY,
            stroke=concept.primary,
            width=18,
        ),
        arc((54, 217, 150, 313), 115, 245, concept.secondary, 12),
        arc((362, 217, 458, 313), -65, 65, concept.secondary, 12),
        line(
            [(91, 302), (156, 302), (207, 222), (258, 365), (315, 151), (360, 302), (421, 302)],
            concept.secondary,
            22,
        ),
        ellipse((70, 281, 112, 323), concept.primary, stroke=WHITE, width=7),
        ellipse((400, 281, 442, 323), concept.primary, stroke=WHITE, width=7),
        ellipse((244, 349, 272, 377), concept.primary),
        ellipse((301, 137, 329, 165), concept.primary),
    ]


def _transform_shape(shape: Shape, scale: float, dx: float, dy: float) -> Shape:
    transformed = dict(shape)
    if "box" in shape:
        x1, y1, x2, y2 = shape["box"]
        transformed["box"] = (x1 * scale + dx, y1 * scale + dy, x2 * scale + dx, y2 * scale + dy)
    if "points" in shape:
        transformed["points"] = [(x * scale + dx, y * scale + dy) for x, y in shape["points"]]
    if "radius" in shape:
        transformed["radius"] = shape["radius"] * scale
    if "width" in shape:
        transformed["width"] = shape["width"] * scale
    return transformed


def _pixel_word(word: str, x: float, y: float, cell: float, color: str) -> Scene:
    scene: Scene = []
    cursor = x
    for character in word:
        glyph = PIXEL_FONT[character]
        for row, values in enumerate(glyph):
            for column, value in enumerate(values):
                if value == "1":
                    inset = cell * 0.08
                    scene.append(
                        rounded_rect(
                            (
                                cursor + column * cell + inset,
                                y + row * cell + inset,
                                cursor + (column + 1) * cell - inset,
                                y + (row + 1) * cell - inset,
                            ),
                            cell * 0.12,
                            color,
                        )
                    )
        cursor += cell * 6
    return scene


def _logo_scene(concept: Concept) -> Scene:
    scene: Scene = [rounded_rect((8, 36, 1272, 476), 108, NAVY, stroke=concept.primary, width=16)]
    scene.extend(_transform_shape(shape, 0.76, 30, 61) for shape in _icon_scene(concept))
    scene.append(line([(448, 102), (448, 410)], concept.secondary, 10))
    scene.extend(_pixel_word("REMOTE", 520, 70, 16, concept.primary))
    scene.extend(_pixel_word("ENERGY", 520, 200, 16, WHITE))
    scene.extend(_pixel_word("METER", 520, 330, 16, concept.secondary))
    scene.extend(
        [
            rounded_rect((1126, 320, 1154, 412), 8, concept.primary),
            rounded_rect((1170, 252, 1198, 412), 8, concept.secondary),
            rounded_rect((1214, 174, 1242, 412), 8, WHITE),
        ]
    )
    return scene


def _svg_number(value: float) -> str:
    rounded = round(value, 3)
    return str(int(rounded)) if rounded.is_integer() else f"{rounded:g}"


def _svg_attributes(shape: Shape) -> str:
    attributes = [f'fill="{shape.get("fill") or "none"}"']
    if shape.get("stroke"):
        attributes.extend(
            [
                f'stroke="{shape["stroke"]}"',
                f'stroke-width="{_svg_number(shape["width"])}"',
                'stroke-linecap="round"',
                'stroke-linejoin="round"',
            ]
        )
    return " ".join(attributes)


def _shape_svg(shape: Shape) -> str:
    kind = shape["kind"]
    attrs = _svg_attributes(shape)
    if kind == "rounded_rect":
        x1, y1, x2, y2 = shape["box"]
        return (
            f'<rect x="{_svg_number(x1)}" y="{_svg_number(y1)}" '
            f'width="{_svg_number(x2 - x1)}" height="{_svg_number(y2 - y1)}" '
            f'rx="{_svg_number(shape["radius"])}" {attrs}/>'
        )
    if kind == "ellipse":
        x1, y1, x2, y2 = shape["box"]
        return (
            f'<ellipse cx="{_svg_number((x1 + x2) / 2)}" cy="{_svg_number((y1 + y2) / 2)}" '
            f'rx="{_svg_number((x2 - x1) / 2)}" ry="{_svg_number((y2 - y1) / 2)}" {attrs}/>'
        )
    if kind in {"line", "polygon"}:
        points = " ".join(f"{_svg_number(x)},{_svg_number(y)}" for x, y in shape["points"])
        element = "polyline" if kind == "line" else "polygon"
        return f'<{element} points="{points}" {attrs}/>'
    if kind == "arc":
        x1, y1, x2, y2 = shape["box"]
        rx = (x2 - x1) / 2
        ry = (y2 - y1) / 2
        cx = (x1 + x2) / 2
        cy = (y1 + y2) / 2
        start = shape["start"]
        end = shape["end"]
        delta = end - start
        start_radians = math.radians(start)
        end_radians = math.radians(end)
        start_point = (cx + rx * math.cos(start_radians), cy + ry * math.sin(start_radians))
        end_point = (cx + rx * math.cos(end_radians), cy + ry * math.sin(end_radians))
        large_arc = 1 if delta > 180 else 0
        path = (
            f"M {_svg_number(start_point[0])} {_svg_number(start_point[1])} "
            f"A {_svg_number(rx)} {_svg_number(ry)} 0 {large_arc} 1 "
            f"{_svg_number(end_point[0])} {_svg_number(end_point[1])}"
        )
        return (
            f'<path d="{path}" fill="none" stroke="{shape["stroke"]}" '
            f'stroke-width="{_svg_number(shape["width"])}" stroke-linecap="round"/>'
        )
    raise ValueError(f"unsupported shape kind: {kind}")


def _write_svg(path: Path, scene: Scene, width: int, height: int, title: str) -> None:
    body = "\n  ".join(_shape_svg(shape) for shape in scene)
    document = f'''<?xml version="1.0" encoding="UTF-8"?>
<!-- Original artwork created for Remote Energy Meter. No third-party marks or artwork. -->
<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img">
  <title>{escape(title)}</title>
  {body}
</svg>
'''
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8", newline="\n")


def _scaled_box(box: tuple[float, float, float, float], sx: float, sy: float) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = box
    return (round(x1 * sx), round(y1 * sy), round(x2 * sx), round(y2 * sy))


def _render_scene(scene: Scene, scene_width: int, scene_height: int, width: int, height: int) -> Image.Image:
    render_width = width * SUPERSAMPLE
    render_height = height * SUPERSAMPLE
    sx = render_width / scene_width
    sy = render_height / scene_height
    image = Image.new("RGBA", (render_width, render_height), (0, 0, 0, 0))
    drawing = ImageDraw.Draw(image)
    for shape in scene:
        kind = shape["kind"]
        stroke_width = max(1, round(shape.get("width", 0) * (sx + sy) / 2))
        if kind == "rounded_rect":
            drawing.rounded_rectangle(
                _scaled_box(shape["box"], sx, sy),
                radius=round(shape["radius"] * (sx + sy) / 2),
                fill=shape["fill"],
                outline=shape.get("stroke"),
                width=stroke_width,
            )
        elif kind == "ellipse":
            drawing.ellipse(
                _scaled_box(shape["box"], sx, sy),
                fill=shape.get("fill"),
                outline=shape.get("stroke"),
                width=stroke_width,
            )
        elif kind == "line":
            points = [(round(x * sx), round(y * sy)) for x, y in shape["points"]]
            drawing.line(points, fill=shape["stroke"], width=stroke_width, joint="curve")
            radius = stroke_width // 2
            for x, y in points:
                drawing.ellipse((x - radius, y - radius, x + radius, y + radius), fill=shape["stroke"])
        elif kind == "polygon":
            points = [(round(x * sx), round(y * sy)) for x, y in shape["points"]]
            drawing.polygon(points, fill=shape["fill"])
            if shape.get("stroke"):
                drawing.line([*points, points[0]], fill=shape["stroke"], width=stroke_width, joint="curve")
        elif kind == "arc":
            drawing.arc(
                _scaled_box(shape["box"], sx, sy),
                start=shape["start"],
                end=shape["end"],
                fill=shape["stroke"],
                width=stroke_width,
            )
        else:
            raise ValueError(f"unsupported shape kind: {kind}")
    return image.resize((width, height), Image.Resampling.LANCZOS)


def _save_png(image: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG", optimize=True, compress_level=9, interlace=True)


def _generate_concept(concept: Concept) -> None:
    root = OUTPUT_ROOT / f"{concept.number:02d}-{concept.slug}"
    icon_scene = _icon_scene(concept)
    logo_scene = _logo_scene(concept)
    _write_svg(root / "source/icon.svg", icon_scene, 512, 512, f"{concept.name} icon")
    _write_svg(root / "source/logo.svg", logo_scene, 1280, 512, f"{concept.name} logo")

    outputs = (
        (root / "addon/icon.png", icon_scene, 512, 512, 128, 128),
        (root / "addon/logo.png", logo_scene, 1280, 512, 250, 100),
        (root / "integration/brand/icon.png", icon_scene, 512, 512, 256, 256),
        (root / "integration/brand/icon@2x.png", icon_scene, 512, 512, 512, 512),
        (root / "integration/brand/logo.png", logo_scene, 1280, 512, 640, 256),
        (root / "integration/brand/logo@2x.png", logo_scene, 1280, 512, 1280, 512),
    )
    for path, scene, scene_width, scene_height, width, height in outputs:
        _save_png(_render_scene(scene, scene_width, scene_height, width, height), path)


def _font(size: int) -> ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


def _paste_centered(canvas: Image.Image, image: Image.Image, box: tuple[int, int, int, int]) -> None:
    x1, y1, x2, y2 = box
    x = x1 + (x2 - x1 - image.width) // 2
    y = y1 + (y2 - y1 - image.height) // 2
    canvas.alpha_composite(image, (x, y))


def _contact_sheet() -> Path:
    width, height = 1500, 1280
    sheet = Image.new("RGBA", (width, height), SHEET_LIGHT)
    drawing = ImageDraw.Draw(sheet)
    drawing.text((32, 20), "Remote Energy Meter icon candidates", fill=NAVY, font=_font(34))
    drawing.text(
        (34, 62),
        "Original artwork. Transparent PNG deliverables. Light and dark context previews",
        fill="#41566F",
        font=_font(19),
    )

    for index, concept in enumerate(CONCEPTS):
        top = 100 + index * 285
        drawing.rounded_rectangle((20, top, 1480, top + 265), radius=26, fill="#FFFFFF", outline="#CED8E2", width=2)
        drawing.text((42, top + 24), f"{concept.number}. {concept.name}", fill=NAVY, font=_font(27))
        rationale = textwrap.wrap(concept.rationale, width=32)
        drawing.multiline_text((42, top + 68), "\n".join(rationale), fill="#41566F", font=_font(17), spacing=5)
        drawing.rounded_rectangle((44, top + 185, 86, top + 227), radius=9, fill=concept.primary)
        drawing.rounded_rectangle((96, top + 185, 138, top + 227), radius=9, fill=concept.secondary)
        drawing.text((148, top + 191), "candidate palette", fill="#60758D", font=_font(15))

        icon = Image.open(OUTPUT_ROOT / f"{concept.number:02d}-{concept.slug}/addon/icon.png").convert("RGBA")
        logo = Image.open(OUTPUT_ROOT / f"{concept.number:02d}-{concept.slug}/addon/logo.png").convert("RGBA")

        light_card = (330, top + 34, 482, top + 186)
        dark_card = (500, top + 34, 652, top + 186)
        drawing.rounded_rectangle(light_card, radius=20, fill="#F8FAFC", outline="#D5DEE7", width=2)
        drawing.rounded_rectangle(dark_card, radius=20, fill=SHEET_DARK)
        _paste_centered(sheet, icon, light_card)
        _paste_centered(sheet, icon, dark_card)
        drawing.text((352, top + 201), "128 px light", fill="#60758D", font=_font(14))
        drawing.text((518, top + 201), "128 px dark", fill="#60758D", font=_font(14))

        icon64 = icon.resize((64, 64), Image.Resampling.LANCZOS)
        icon32 = icon.resize((32, 32), Image.Resampling.LANCZOS)
        _paste_centered(sheet, icon64, (675, top + 48, 755, top + 128))
        _paste_centered(sheet, icon32, (689, top + 142, 741, top + 194))
        drawing.text((685, top + 202), "64 and 32 px", fill="#60758D", font=_font(14))

        light_logo_card = (780, top + 35, 1090, top + 175)
        dark_logo_card = (1120, top + 35, 1430, top + 175)
        drawing.rounded_rectangle(light_logo_card, radius=18, fill="#F8FAFC", outline="#D5DEE7", width=2)
        drawing.rounded_rectangle(dark_logo_card, radius=18, fill=SHEET_DARK)
        _paste_centered(sheet, logo, light_logo_card)
        _paste_centered(sheet, logo, dark_logo_card)
        drawing.text((858, top + 201), "250 by 100 light", fill="#60758D", font=_font(14))
        drawing.text((1196, top + 201), "250 by 100 dark", fill="#60758D", font=_font(14))

    drawing.text(
        (32, 1246),
        "Selection sheet only. None of these files are installed in the add-on or custom integration.",
        fill="#526A83",
        font=_font(16),
    )
    path = OUTPUT_ROOT / "contact-sheet.png"
    _save_png(sheet, path)
    return path


def _luminance_span(image: Image.Image, background: tuple[int, int, int, int]) -> float:
    backdrop = Image.new("RGBA", image.size, background)
    backdrop.alpha_composite(image)
    values = []
    for red, green, blue, _alpha in backdrop.get_flattened_data():
        values.append(0.2126 * red + 0.7152 * green + 0.0722 * blue)
    return max(values) - min(values)


def _validate_png(path: Path, expected_size: tuple[int, int], *, icon: bool) -> dict[str, object]:
    content = path.read_bytes()
    if not content.startswith(PNG_SIGNATURE):
        raise ValueError(f"invalid PNG signature: {path}")
    with Image.open(path) as opened:
        image = opened.convert("RGBA")
        if opened.size != expected_size:
            raise ValueError(f"unexpected dimensions for {path}: {opened.size}")
        if opened.mode != "RGBA":
            raise ValueError(f"unexpected color mode for {path}: {opened.mode}")
    alpha = image.getchannel("A")
    alpha_range = alpha.getextrema()
    if alpha_range != (0, 255):
        raise ValueError(f"PNG does not contain transparent and opaque pixels: {path}: {alpha_range}")
    bbox = alpha.getbbox()
    if bbox is None:
        raise ValueError(f"PNG is fully transparent: {path}")
    entry: dict[str, object] = {
        "path": path.relative_to(OUTPUT_ROOT).as_posix(),
        "dimensions": list(expected_size),
        "mode": "RGBA",
        "alpha_range": list(alpha_range),
        "alpha_bbox": list(bbox),
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "png_interlace_method": content[28],
    }
    if icon:
        small = image.resize((32, 32), Image.Resampling.LANCZOS)
        small_bbox = small.getchannel("A").getbbox()
        if small_bbox is None or small_bbox[2] - small_bbox[0] < 25 or small_bbox[3] - small_bbox[1] < 25:
            raise ValueError(f"32px silhouette is too small: {path}: {small_bbox}")
        light_span = _luminance_span(small, (248, 250, 252, 255))
        dark_span = _luminance_span(small, (17, 24, 39, 255))
        if light_span < 70 or dark_span < 70:
            raise ValueError(f"32px contrast span is too low: {path}: {light_span:.1f}/{dark_span:.1f}")
        entry.update(
            {
                "downscaled_32_alpha_bbox": list(small_bbox),
                "downscaled_32_light_luminance_span": round(light_span, 1),
                "downscaled_32_dark_luminance_span": round(dark_span, 1),
            }
        )
    return entry


def _validate_assets(contact_sheet: Path) -> Path:
    png_results: list[dict[str, object]] = []
    svg_results: list[dict[str, object]] = []
    for concept in CONCEPTS:
        root = OUTPUT_ROOT / f"{concept.number:02d}-{concept.slug}"
        expected = (
            (root / "addon/icon.png", (128, 128), True),
            (root / "addon/logo.png", (250, 100), False),
            (root / "integration/brand/icon.png", (256, 256), True),
            (root / "integration/brand/icon@2x.png", (512, 512), True),
            (root / "integration/brand/logo.png", (640, 256), False),
            (root / "integration/brand/logo@2x.png", (1280, 512), False),
        )
        png_results.extend(_validate_png(path, size, icon=is_icon) for path, size, is_icon in expected)
        for path, view_box in (
            (root / "source/icon.svg", "0 0 512 512"),
            (root / "source/logo.svg", "0 0 1280 512"),
        ):
            root_element = ET.parse(path).getroot()
            if root_element.tag != "{http://www.w3.org/2000/svg}svg" or root_element.attrib.get("viewBox") != view_box:
                raise ValueError(f"invalid SVG root or viewBox: {path}")
            svg_results.append(
                {
                    "path": path.relative_to(OUTPUT_ROOT).as_posix(),
                    "view_box": view_box,
                    "bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )

    sheet_content = contact_sheet.read_bytes()
    with Image.open(contact_sheet) as sheet:
        if not sheet_content.startswith(PNG_SIGNATURE) or sheet.size != (1500, 1280) or sheet.mode != "RGBA":
            raise ValueError("contact sheet failed PNG, dimension, or mode validation")
    report = {
        "result": "pass",
        "artwork_origin": "Original artwork created for Remote Energy Meter",
        "candidate_count": len(CONCEPTS),
        "deployment_assets_per_candidate": 6,
        "editable_sources_per_candidate": 2,
        "dark_variants": "not needed; opaque high-contrast plates were validated on light and dark backgrounds",
        "png": png_results,
        "svg": svg_results,
        "contact_sheet": {
            "path": contact_sheet.relative_to(OUTPUT_ROOT).as_posix(),
            "dimensions": [1500, 1280],
            "mode": "RGBA",
            "bytes": len(sheet_content),
            "sha256": hashlib.sha256(sheet_content).hexdigest(),
        },
    }
    report_path = OUTPUT_ROOT / "validation-report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    return report_path


def main() -> int:
    for concept in CONCEPTS:
        _generate_concept(concept)
    contact_sheet = _contact_sheet()
    report = _validate_assets(contact_sheet)
    print(f"Generated {len(CONCEPTS)} candidate sets in {OUTPUT_ROOT}")
    print(f"Contact sheet: {contact_sheet}")
    print(f"Validation report: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
