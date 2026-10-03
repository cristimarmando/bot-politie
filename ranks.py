RANKS = [
    {"key": "detectiv_stagiar", "name": "Detectiv Stagiar", "prefix": "DS"},
    {"key": "detectiv", "name": "Detectiv", "prefix": "D"},
    {"key": "investigator_operativ", "name": "Investigator Operativ", "prefix": "IO"},
    {"key": "investigator_principal", "name": "Investigator Principal", "prefix": "IP"},
    {"key": "ofiter_coordonator_caz", "name": "Ofițer Coordonator de Caz", "prefix": "OCC"},
    {"key": "sef_serviciu_investigatii", "name": "Șef Serviciu Investigații", "prefix": "SSI"},
    {"key": "coordonator_operational", "name": "Coordonator Operațional", "prefix": "CO"},
    {"key": "comandant_operational", "name": "Comandant Operațional", "prefix": "CMD"},
    {"key": "director_adjunct", "name": "Director Adjunct C.C.O.", "prefix": "DA"},
    {"key": "director_general", "name": "Director General C.C.O.", "prefix": "DG"},
]

LEADERSHIP_MIN_INDEX = next(i for i, r in enumerate(RANKS) if r["key"] == "coordonator_operational")
SSI_INDEX = next(i for i, r in enumerate(RANKS) if r["key"] == "sef_serviciu_investigatii")


def rank_index(name):
    if not name:
        return -1
    for i, r in enumerate(RANKS):
        if r["name"] == name:
            return i
    return -1


def rank_by_name(name):
    return next((r for r in RANKS if r["name"] == name), None)


def rank_by_key(key):
    return next((r for r in RANKS if r["key"] == key), None)


def is_leadership_rank(name):
    return rank_index(name) >= LEADERSHIP_MIN_INDEX


def is_ssi_rank(name):
    return rank_index(name) == SSI_INDEX
