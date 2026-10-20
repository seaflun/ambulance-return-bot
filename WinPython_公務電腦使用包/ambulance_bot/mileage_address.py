"""救災里程的保守地址比對；案件、車輛及時間仍由呼叫端核對。"""

import re
import unicodedata


_DIGITS = {char: value for value, char in enumerate("零一二三四五六七八九")}
_DIGITS.update({"〇": 0, "兩": 2, "两": 2})
_UNITS = {"十": 10, "百": 100, "千": 1000}
_NUMERALS = "0-9零〇一二三四五六七八九兩两十百千"
_CITIES = {"台北市", "新北市", "桃園市", "台中市", "台南市", "高雄市", "基隆市", "新竹市", "嘉義市"}
_ADMINISTRATION = re.compile(r"^([^0-9區鄉鎮市縣路街巷弄號段]+[縣市區鄉鎮])")
_ROAD = re.compile(r"^(.+?(?:大道|路|街)(?:[東西南北](?=\d+段))?)(.*)$")
_COMPONENT = re.compile(r"^(\d+(?:之\d+)?)(段|巷|弄|號)")
_COMPONENT_ORDER = {"段": 0, "巷": 1, "弄": 2, "號": 3}


def _number(text: str) -> str:
    if not any(char in _UNITS for char in text):
        return str(int("".join(str(_DIGITS.get(char, char)) for char in text)))
    total = current = 0
    previous_unit = 10000
    for char in text:
        if char in _UNITS:
            if _UNITS[char] >= previous_unit:
                raise ValueError("ambiguous address numeral")
            previous_unit = _UNITS[char]
            total += (current or 1) * _UNITS[char]
            current = 0
        else:
            current = current * 10 + int(_DIGITS.get(char, char))
    return str(total + current)


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).translate(str.maketrans({
        "臺": "台", "‐": "-", "‑": "-", "–": "-", "—": "-"}))
    text = re.sub(r"\s+", "", text)
    punctuation = r"[,.;:、。()\[\]{}「」『』〈〉《》]"
    # Do not concatenate two separate numeric addresses such as 1、3號.
    text = re.sub(rf"(?<![{_NUMERALS}]){punctuation}|{punctuation}(?![{_NUMERALS}])", "", text)
    text = re.sub(rf"[{_NUMERALS}]+(?=路|街|段|巷|弄|號|之|-)",
                  lambda match: _number(match.group()), text)
    text = re.sub(rf"(?<=[之-])[{_NUMERALS}]+", lambda match: _number(match.group()), text)
    text = re.sub(r"(?<=\d)-(?=\d)", "之", text)
    return re.sub(r"(\d+)號之(\d+)", r"\1之\2號", text)


def _landmark(text: str) -> str:
    text = re.sub(r"^(?:內|口|附近|近)", "", text)
    while True:
        trimmed = re.sub(r"(?:對面|附近|旁邊|前方|後方|左側|右側|左邊|右邊|旁|前|後|內|口|邊)$", "", text)
        if trimmed == text:
            return text
        text = trimmed


def _parse(value: str) -> tuple[dict[str, str], str, dict[str, str], str] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        text = _normalize(value)
    except ValueError:
        return None
    administration = {}
    while match := _ADMINISTRATION.match(text):
        name = match.group(1)
        level = "city" if name.endswith("縣") or name in _CITIES else "district"
        if level in administration:
            return None
        administration[level] = name
        text = text[match.end():]
    road = _ROAD.match(text)
    if road is None:
        return None
    street, tail = road.groups()
    components = {}
    previous_order = -1
    while match := _COMPONENT.match(tail):
        number, kind = match.groups()
        if _COMPONENT_ORDER[kind] <= previous_order:
            return None
        components[kind] = number
        previous_order = _COMPONENT_ORDER[kind]
        tail = tail[match.end():]
    if re.search(r"\d+(?:之\d+)?[段巷弄號]", tail):
        return None
    return administration, street, components, _landmark(tail)


def disaster_mileage_addresses_match(left: str, right: str) -> bool:
    """依相同道路及巷弄／門牌判定；不推測不同行政區或衝突門牌。"""
    first, second = _parse(left), _parse(right)
    if first is None or second is None:
        return False
    first_admin, first_street, first_parts, first_landmark = first
    second_admin, second_street, second_parts, second_landmark = second
    if first_street != second_street:
        return False
    if any(first_admin[level] != second_admin[level] for level in first_admin.keys() & second_admin.keys()):
        return False
    if first_parts.get("段") != second_parts.get("段"):
        return False
    if any(first_parts[kind] != second_parts[kind] for kind in first_parts.keys() & second_parts.keys()):
        return False
    if not (first_parts.keys() <= second_parts.keys() or second_parts.keys() <= first_parts.keys()):
        return False
    if not any(kind in first_parts and kind in second_parts for kind in ("巷", "弄", "號")):
        return False
    if ("巷" in first_parts) != ("巷" in second_parts):
        return False
    if "號" in first_parts and "號" in second_parts and first_parts.get("弄") != second_parts.get("弄"):
        return False
    if first_landmark and second_landmark and first_landmark != second_landmark:
        return False
    return True
