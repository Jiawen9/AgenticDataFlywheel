from __future__ import annotations

import json
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageDraw, ImageFont


BOUNDS_RE = re.compile(r"\[(?P<x1>-?\d+),(?P<y1>-?\d+)\]\[(?P<x2>-?\d+),(?P<y2>-?\d+)\]")


@dataclass
class UINode:
    bounds: tuple[int, int, int, int]
    attrs: dict[str, str]

    @property
    def area(self) -> int:
        x1, y1, x2, y2 = self.bounds
        return max(0, x2 - x1) * max(0, y2 - y1)


@dataclass
class BoxResult:
    bbox: tuple[int, int, int, int]
    source: str
    confidence: float
    target: dict[str, str]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["bbox"] = list(self.bbox)
        return value


def parse_bounds(value: str) -> tuple[int, int, int, int] | None:
    match = BOUNDS_RE.fullmatch(value.strip())
    if not match:
        return None
    result = tuple(int(match.group(name)) for name in ("x1", "y1", "x2", "y2"))
    if result[2] <= result[0] or result[3] <= result[1]:
        return None
    return result


def parse_ui(xml_text: str) -> list[UINode]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    nodes: list[UINode] = []
    for element in root.iter("node"):
        bounds = parse_bounds(element.attrib.get("bounds", ""))
        if bounds:
            nodes.append(UINode(bounds=bounds, attrs=dict(element.attrib)))
    return nodes


def screen_size(nodes: Iterable[UINode], image_size: tuple[int, int]) -> tuple[int, int]:
    nodes = list(nodes)
    if not nodes:
        return image_size
    return (
        max(node.bounds[2] for node in nodes),
        max(node.bounds[3] for node in nodes),
    )


def contains(bounds: tuple[int, int, int, int], point: tuple[float, float]) -> bool:
    x1, y1, x2, y2 = bounds
    return x1 <= point[0] <= x2 and y1 <= point[1] <= y2


def distance_to_rect(point: tuple[float, float], bounds: tuple[int, int, int, int]) -> float:
    x1, y1, x2, y2 = bounds
    dx = max(x1 - point[0], 0, point[0] - x2)
    dy = max(y1 - point[1], 0, point[1] - y2)
    return math.hypot(dx, dy)


def is_usable(node: UINode) -> bool:
    attrs = node.attrs
    return attrs.get("enabled", "true") == "true" and attrs.get("visible-to-user", "true") == "true"


def target_metadata(node: UINode | None) -> dict[str, str]:
    if node is None:
        return {}
    attrs = node.attrs
    return {
        key: attrs.get(key, "")
        for key in ("resource-id", "text", "content-desc", "class", "clickable", "scrollable", "focused")
        if attrs.get(key, "")
    }


def shrink_box(bounds: tuple[int, int, int, int], ratio: float, cap: int) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = bounds
    width, height = x2 - x1, y2 - y1
    inset_x = min(cap, round(width * ratio)) if width >= 48 else 0
    inset_y = min(cap, round(height * ratio)) if height >= 48 else 0
    return x1 + inset_x, y1 + inset_y, x2 - inset_x, y2 - inset_y


def include_point(
    bounds: tuple[int, int, int, int], point: tuple[float, float], margin: int = 4
) -> tuple[int, int, int, int]:
    """Keep the observed successful action point inside a shrunken safe box."""
    x1, y1, x2, y2 = bounds
    return (
        min(x1, math.floor(point[0] - margin)),
        min(y1, math.floor(point[1] - margin)),
        max(x2, math.ceil(point[0] + margin)),
        max(y2, math.ceil(point[1] + margin)),
    )


def choose_click_candidate(candidates: list[UINode]) -> UINode:
    node = min(candidates, key=lambda item: item.area)
    node_class = node.attrs.get("class", "")
    # Some apps expose a clickable TextView inside the actual clickable search field.
    # Promote only to a nearby container with a comparable area; this avoids selecting
    # a page-sized clickable ancestor for ordinary icon/text buttons.
    if "TextView" in node_class or "ImageView" in node_class:
        containers = [
            candidate
            for candidate in candidates
            if candidate is not node
            and candidate.area <= node.area * 8
            and contains(candidate.bounds, (node.bounds[0], node.bounds[1]))
            and contains(candidate.bounds, (node.bounds[2], node.bounds[3]))
            and "TextView" not in candidate.attrs.get("class", "")
            and "ImageView" not in candidate.attrs.get("class", "")
        ]
        if containers:
            node = min(containers, key=lambda item: item.area)
    return node


def clamp_box(bounds: tuple[float, float, float, float], size: tuple[int, int]) -> tuple[int, int, int, int]:
    width, height = size
    x1, y1, x2, y2 = bounds
    x1 = max(0, min(width - 2, round(x1)))
    y1 = max(0, min(height - 2, round(y1)))
    x2 = max(x1 + 1, min(width - 1, round(x2)))
    y2 = max(y1 + 1, min(height - 1, round(y2)))
    return x1, y1, x2, y2


def scale_box(
    bounds: tuple[int, int, int, int],
    from_size: tuple[int, int],
    to_size: tuple[int, int],
) -> tuple[int, int, int, int]:
    sx = to_size[0] / max(1, from_size[0])
    sy = to_size[1] / max(1, from_size[1])
    x1, y1, x2, y2 = bounds
    return clamp_box((x1 * sx, y1 * sy, x2 * sx, y2 * sy), to_size)


def infer_click(
    action: dict[str, Any], nodes: list[UINode], image_size: tuple[int, int]
) -> BoxResult:
    raw = action.get("coordinate", [])
    if not isinstance(raw, list) or len(raw) < 2:
        return fallback_center(image_size, "click_without_coordinate")
    point = float(raw[0]), float(raw[1])
    xml_size = screen_size(nodes, image_size)
    clickable = [n for n in nodes if is_usable(n) and n.attrs.get("clickable") == "true"]
    candidates = [n for n in clickable if contains(n.bounds, point)]
    if candidates:
        node = choose_click_candidate(candidates)
        box = include_point(shrink_box(node.bounds, 0.06, 24), point)
        return BoxResult(
            scale_box(box, xml_size, image_size),
            "ui_clickable",
            0.96,
            target_metadata(node),
            "smallest enabled clickable UI node containing the action point",
        )

    if clickable:
        node = min(clickable, key=lambda item: (distance_to_rect(point, item.bounds), item.area))
        distance = distance_to_rect(point, node.bounds)
        if distance <= max(80, math.hypot(*xml_size) * 0.05):
            box = include_point(shrink_box(node.bounds, 0.04, 18), point)
            return BoxResult(
                scale_box(box, xml_size, image_size),
                "nearest_ui_clickable",
                0.72,
                target_metadata(node),
                f"nearest enabled clickable node; point-to-box distance={distance:.1f}",
            )

    sx = image_size[0] / max(1, xml_size[0])
    sy = image_size[1] / max(1, xml_size[1])
    px, py = point[0] * sx, point[1] * sy
    radius = max(28, round(min(image_size) * 0.035))
    return BoxResult(
        clamp_box((px - radius, py - radius, px + radius, py + radius), image_size),
        "point_fallback",
        0.42,
        {},
        "no usable clickable node; fixed safe region around observed point",
    )


def infer_swipe(
    action: dict[str, Any], nodes: list[UINode], image_size: tuple[int, int]
) -> BoxResult:
    start = action.get("start_coordinate", [])
    end = action.get("end_coordinate", [])
    if not isinstance(start, list) or len(start) < 2 or not isinstance(end, list) or len(end) < 2:
        return fallback_center(image_size, "swipe_without_endpoints")
    p1 = float(start[0]), float(start[1])
    p2 = float(end[0]), float(end[1])
    xml_size = screen_size(nodes, image_size)
    scrollable = [n for n in nodes if is_usable(n) and n.attrs.get("scrollable") == "true"]
    both = [n for n in scrollable if contains(n.bounds, p1) and contains(n.bounds, p2)]
    if both:
        node = min(both, key=lambda item: item.area)
        return BoxResult(
            scale_box(shrink_box(node.bounds, 0.04, 28), xml_size, image_size),
            "ui_scrollable",
            0.94,
            target_metadata(node),
            "smallest enabled scrollable UI node containing both swipe endpoints",
        )

    midpoint = ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2)
    partial = [n for n in scrollable if contains(n.bounds, p1) or contains(n.bounds, midpoint)]
    if partial:
        node = min(partial, key=lambda item: item.area)
        return BoxResult(
            scale_box(shrink_box(node.bounds, 0.035, 24), xml_size, image_size),
            "ui_scrollable_partial",
            0.75,
            target_metadata(node),
            "scrollable UI node containing the start point or gesture midpoint",
        )

    sx = image_size[0] / max(1, xml_size[0])
    sy = image_size[1] / max(1, xml_size[1])
    x1, x2 = sorted((p1[0] * sx, p2[0] * sx))
    y1, y2 = sorted((p1[1] * sy, p2[1] * sy))
    pad_x = max(80, (x2 - x1) * 0.35)
    pad_y = max(80, (y2 - y1) * 0.18)
    return BoxResult(
        clamp_box((x1 - pad_x, y1 - pad_y, x2 + pad_x, y2 + pad_y), image_size),
        "gesture_envelope_fallback",
        0.48,
        {},
        "no scrollable node; expanded bounding region around the swipe path",
    )


def infer_type(nodes: list[UINode], image_size: tuple[int, int]) -> BoxResult:
    xml_size = screen_size(nodes, image_size)
    focused = [n for n in nodes if is_usable(n) and n.attrs.get("focused") == "true"]
    editable = [
        n
        for n in nodes
        if is_usable(n)
        and ("EditText" in n.attrs.get("class", "") or n.attrs.get("password") == "true")
    ]
    candidates = focused or editable
    if candidates:
        node = min(candidates, key=lambda item: item.area)
        return BoxResult(
            scale_box(shrink_box(node.bounds, 0.04, 18), xml_size, image_size),
            "focused_input" if focused else "editable_input",
            0.90 if focused else 0.70,
            target_metadata(node),
            "focused/editable UI field receiving the type action",
        )
    return fallback_center(image_size, "type_without_input_node")


def fallback_center(image_size: tuple[int, int], reason: str) -> BoxResult:
    width, height = image_size
    return BoxResult(
        clamp_box((width * 0.15, height * 0.25, width * 0.85, height * 0.75), image_size),
        "screen_content_fallback",
        0.25,
        {},
        reason,
    )


def infer_box(action: dict[str, Any], xml_text: str, image_size: tuple[int, int]) -> BoxResult:
    nodes = parse_ui(xml_text) if xml_text else []
    kind = str(action.get("action", "unknown"))
    if kind in {"click", "long_press"}:
        return infer_click(action, nodes, image_size)
    if kind == "swipe":
        return infer_swipe(action, nodes, image_size)
    if kind == "type":
        return infer_type(nodes, image_size)
    return fallback_center(image_size, f"unsupported_action:{kind}")


def annotation_color(action_kind: str, confidence: float) -> tuple[int, int, int]:
    if confidence < 0.5:
        return 255, 165, 0
    if action_kind == "swipe":
        return 0, 170, 255
    if action_kind == "type":
        return 170, 80, 255
    return 255, 55, 48


def annotate_image(source: Path, destination: Path, action: dict[str, Any], result: BoxResult) -> None:
    with Image.open(source) as opened:
        image = opened.convert("RGB")
    draw = ImageDraw.Draw(image)
    color = annotation_color(str(action.get("action", "unknown")), result.confidence)
    width = max(5, round(min(image.size) / 180))
    draw.rectangle(result.bbox, outline=color, width=width)
    label = f"{action.get('action', 'unknown')}  conf={result.confidence:.2f}  {result.source}"
    try:
        font = ImageFont.truetype("arial.ttf", 24)
    except OSError:
        font = ImageFont.load_default()
    x1, y1, _, _ = result.bbox
    text_y = max(2, y1 - 32)
    draw.text((x1 + 2, text_y), label, fill=color, font=font, stroke_width=3, stroke_fill=(0, 0, 0))
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, quality=94, subsampling=0)


def load_trajectory_actions(evaluation_file: Path) -> list[dict[str, Any]]:
    data = json.loads(evaluation_file.read_text(encoding="utf-8"))
    return list(data.get("actions_flat", []))
