def utf8_length(text: str) -> int | None:
    try:
        return len(text.encode("utf-8"))
    except UnicodeEncodeError:
        return None


def fits_utf8(text: str, limit: int) -> bool:
    length = utf8_length(text)
    return length is not None and length <= limit
