"""Reject releases whose user-facing update page is missing or stale."""
import argparse
from html.parser import HTMLParser
from pathlib import Path
import re
import sys
import zipfile


class UpdateCards(HTMLParser):
    def __init__(self):
        super().__init__()
        self.cards = []
        self.current = None
        self.in_item = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "article" and "data-release-version" in attrs:
            self.current = {"version": attrs["data-release-version"], "text": "", "items": []}
        elif self.current is not None and tag == "li":
            self.in_item = True
            self.current["items"].append("")

    def handle_data(self, data):
        if self.current is not None:
            self.current["text"] += data
            if self.in_item:
                self.current["items"][-1] += data

    def handle_endtag(self, tag):
        if tag == "li":
            self.in_item = False
        elif tag == "article" and self.current is not None:
            self.cards.append(self.current)
            self.current = None


def verify_notes(text, version):
    parser = UpdateCards()
    parser.feed(text)
    if not parser.cards or parser.cards[0]["version"] != version:
        raise ValueError("Add the current release as the first versioned update card.")
    versions = [card["version"] for card in parser.cards]
    if len(versions) != len(set(versions)):
        raise ValueError("Duplicate update versions.")
    for card in parser.cards:
        if (not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}\.\d{4}", card["version"])
                or card["version"] not in card["text"]
                or not card["items"] or not all(item.strip() for item in card["items"])):
            raise ValueError("Each update needs a visible version and nonempty description items.")


def main():
    runtime = Path(__file__).resolve().parents[1] / "WinPython_公務電腦使用包"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version")
    parser.add_argument("--template", type=Path, default=runtime / "templates/system_updates.html")
    parser.add_argument("--package", type=Path)
    args = parser.parse_args()
    version = args.version or (runtime / "VERSION.txt").read_text(encoding="utf-8-sig").strip()
    try:
        source = args.template.read_bytes()
        verify_notes(source.decode("utf-8-sig"), version)
        if args.package:
            with zipfile.ZipFile(args.package) as archive:
                prefix = "WinPython_公務電腦使用包/"
                if archive.read(prefix + "VERSION.txt").decode("utf-8-sig").strip() != version:
                    raise ValueError("Packaged version does not match release version.")
                if archive.read(prefix + "templates/system_updates.html") != source:
                    raise ValueError("Packaged updates page does not match the reviewed source.")
    except (ValueError, OSError, KeyError, zipfile.BadZipFile) as error:
        print(f"System updates check failed: {error}", file=sys.stderr)
        return 1
    print(f"System updates verified: {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
