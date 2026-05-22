from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

ROTATION_IDS = {"US": "711-53200-19255-0"}

def build_epn_link(target_url: str, campid: str, *, custom_id: str|None=None,
                   marketplace: str="US", tool_id: int=10001, channel_id: int=1,
                   mkevt: int=1, rotation_id: str|None=None) -> str:
    rot = rotation_id or ROTATION_IDS.get(marketplace.upper(), ROTATION_IDS["US"])
    parsed = urlparse(target_url)
    base_q = dict(parse_qsl(parsed.query, keep_blank_values=True))
    tracking_params = {
        "mkevt": str(mkevt),
        "mkcid": str(channel_id),
        "mkrid": rot,
        "campid": str(campid),
        "toolid": str(tool_id),
    }
    if custom_id:
        tracking_params["customid"] = custom_id
    new_q = base_q | tracking_params
    return urlunparse(parsed._replace(query=urlencode(new_q, doseq=True)))

def make_custom_id(*, season=None, post_slug=None, placement=None, ab=None, extra=None) -> str:
    parts = []
    if season:    parts.append(f"seas:{season}")
    if post_slug: parts.append(f"post:{post_slug[:60]}")
    if placement: parts.append(f"pos:{placement}")
    if ab:        parts.append(f"ab:{ab}")
    if extra:     parts.append(f"x:{extra[:40]}")
    return "|".join(parts)[:256]
