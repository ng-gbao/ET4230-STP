"""Generate an SVG comparison and optionally render it as a PNG."""

import argparse
import json
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, indent, tostring


def comparison(results):
    height = 100 + 82 * len(results)
    svg = Element("svg", xmlns="http://www.w3.org/2000/svg",
                  width="900", height=str(height), viewBox=f"0 0 900 {height}",
                  role="img", **{"aria-label": "STP experiment comparison"})

    def label(x, y, value, size=13):
        SubElement(svg, "text", x=str(x), y=str(y), fill="#152332",
                   **{"font-family": "DejaVu Sans, sans-serif", "font-size": str(size)}).text = str(value)

    def bar(x, y, width, color):
        SubElement(svg, "rect", x=str(x), y=str(y), width=str(width), height="16", fill=color)

    SubElement(svg, "rect", width="900", height=str(height), fill="#f5f8fa")
    label(20, 32, "STP experiment comparison", 22)
    label(195, 62, "Simulated convergence (s)", 15)
    label(550, 62, "Ports after convergence", 15)
    series = [("forwarding", "#176456"), ("blocking", "#956019"), ("disabled", "#606b79")]
    time_scale = max(1, max((row["convergence_ms"] for row in results), default=0))
    port_scale = max(1, max((max(row["state_counts"].values()) for row in results), default=0))
    for i, row in enumerate(results):
        y = 80 + i * 82
        label(20, y + 13, row["experiment"].replace("_", " "))
        elapsed = row["convergence_ms"]
        width = 220 * elapsed / time_scale
        bar(195, y, width, "#356489")
        label(202 + width, y + 13, f"{elapsed / 1000:.3f}")
        for j, (state, color) in enumerate(series):
            count = row["state_counts"][state]
            width = 205 * count / port_scale
            bar(550, y + j * 21, width, color)
            label(557 + width, y + j * 21 + 13, f"{count} {state}")
    indent(svg)
    return tostring(svg, encoding="unicode") + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("results/summary.json"))
    parser.add_argument("--output", type=Path, default=Path("results/comparison.svg"))
    parser.add_argument("--png", type=Path, help="also render the chart as a PNG with CairoSVG")
    args = parser.parse_args()
    svg = comparison(json.loads(args.input.read_text(encoding="utf-8")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(svg, encoding="utf-8")
    if args.png:
        try:
            from cairosvg import svg2png
        except ImportError:
            parser.error("--png requires CairoSVG (see requirements.txt)")
        args.png.parent.mkdir(parents=True, exist_ok=True)
        svg2png(bytestring=svg.encode("utf-8"), write_to=str(args.png))


if __name__ == "__main__":
    main()
