import ipaddress
import re

_UCS: str = (
    r"\u00a0-\ud7ff\uf900-\ufdcf\ufdf0-\uffef"
    r"\U00010000-\U0001fffd\U00020000-\U0002fffd"
    r"\U00030000-\U0003fffd\U00040000-\U0004fffd"
    r"\U00050000-\U0005fffd\U00060000-\U0006fffd"
    r"\U00070000-\U0007fffd\U00080000-\U0008fffd"
    r"\U00090000-\U0009fffd\U000a0000-\U000afffd"
    r"\U000b0000-\U000bfffd\U000c0000-\U000cfffd"
    r"\U000d0000-\U000dfffd\U000e1000-\U000efffd"
)
_PRIVATE: str = r"\ue000-\uf8ff\U000f0000-\U000ffffd\U00100000-\U0010fffd"
_ASCII: str = r"A-Za-z0-9._~!$&'()*+,;=\-"
_HOST: re.Pattern[str] = re.compile(f"[{_ASCII}{_UCS}%]+")
_PATH: re.Pattern[str] = re.compile(f"[{_ASCII}{_UCS}%:@/]*")
_QUERY: re.Pattern[str] = re.compile(f"[{_ASCII}{_UCS}{_PRIVATE}%:@/?]*")
_FRAGMENT: re.Pattern[str] = re.compile(f"[{_ASCII}{_UCS}%:@/?]*")
_FUTURE: re.Pattern[str] = re.compile(rf"[vV][0-9A-Fa-f]+[.][{_ASCII}:]+")
_IPV6: re.Pattern[str] = re.compile("[0-9A-Fa-f:.]+")
_PORT: re.Pattern[str] = re.compile("[0-9]*")
_ZEROS: re.Pattern[str] = re.compile("0*")
_BAD_ESCAPE: re.Pattern[str] = re.compile(r"%(?![0-9A-Fa-f]{2})")
_FORBIDDEN: re.Pattern[str] = re.compile(
    r"[\u0000-\u0020\u007f-\u009f\u00a0\u1680\u2000-\u200a"
    r"\u2028-\u2029\u202f\u205f\u3000\ufeff\\]"
)


def valid_port(value: str, start: int, end: int) -> bool:
    if _PORT.fullmatch(value, start, end) is None:
        return False
    zeros = _ZEROS.match(value, start, end)
    if zeros is None:
        raise RuntimeError("port scanner did not match")
    significant = zeros.end()
    width = end - significant
    return width < 5 or (width == 5 and value[significant:end] <= "65535")


def valid_ip_literal(value: str, start: int, end: int) -> bool:
    if _FUTURE.fullmatch(value, start, end) is not None:
        return True
    if end - start > 45 or _IPV6.fullmatch(value, start, end) is None:
        return False
    try:
        ipaddress.IPv6Address(value[start:end])
    except ipaddress.AddressValueError:
        return False
    return True


def valid_authority(value: str, start: int, end: int) -> bool:
    if start == end or value.find("@", start, end) != -1:
        return False
    if value[start] == "[":
        close = value.find("]", start, end)
        if close == -1 or not valid_ip_literal(value, start + 1, close):
            return False
        return close + 1 == end or (
            value[close + 1] == ":" and valid_port(value, close + 2, end)
        )
    colon = value.find(":", start, end)
    host_end = end if colon == -1 else colon
    return _HOST.fullmatch(value, start, host_end) is not None and (
        colon == -1 or valid_port(value, colon + 1, end)
    )


def valid_vcs_ref_url(value: str) -> bool:
    if value.startswith("https://"):
        start = 8
    elif value.startswith("http://"):
        start = 7
    else:
        return False
    if _FORBIDDEN.search(value) is not None or _BAD_ESCAPE.search(value) is not None:
        return False
    end = len(value)
    authority_end = min(
        (position for mark in "/?#" if (position := value.find(mark, start)) != -1),
        default=end,
    )
    if not valid_authority(value, start, authority_end):
        return False
    fragment = value.find("#", authority_end)
    content_end = end if fragment == -1 else fragment
    query = value.find("?", authority_end, content_end)
    path_end = content_end if query == -1 else query
    if _PATH.fullmatch(value, authority_end, path_end) is None:
        return False
    if query != -1 and _QUERY.fullmatch(value, query + 1, content_end) is None:
        return False
    return fragment == -1 or _FRAGMENT.fullmatch(value, fragment + 1, end) is not None
