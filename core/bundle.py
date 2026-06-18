# Positive and negative prompts travel together on one STRING wire so the
# upstream PromptChain compiler can split them at the CLIP boundary; the
# delimiter is a non-printable ASCII Unit Separator unlikely to clash with
# user prompt content.
BUNDLE_DELIM = "\x1FPROMPTCHAIN_NEG\x1F"


def make_bundle(pos: str, neg: str) -> str:
    if neg:
        return pos + BUNDLE_DELIM + neg
    return pos


def parse_bundle(value: str) -> tuple[str, str]:
    if not value:
        return "", ""
    if BUNDLE_DELIM in value:
        parts = value.split(BUNDLE_DELIM, 1)
        return parts[0], parts[1] if len(parts) > 1 else ""
    return value, ""
