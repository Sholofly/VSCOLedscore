"""Berichtsjablonen: standaardteksten, plaatsaanduidingen, validatie en invullen."""

import re
from collections.abc import Mapping
from typing import Any

# Eén regel per aanduiding: wat je in een sjabloon als {naam} kunt gebruiken.
PLACEHOLDERS: dict[str, str] = {
    "minuut": "Speelminuut, bij het einde ook met blessuretijd (bv. 90+2). Is er geen minuut "
    "(Voetbalnederland: begin, rust, tweede helft, einde), dan valt \"'{minuut} \" vanzelf weg",
    "thuis": "Naam van de thuisploeg",
    "uit": "Naam van de uitploeg",
    "eigen": "Naam van de eigen club (thuis of uit)",
    "tegenstander": "Naam van de tegenstander",
    "stand": "Stand in de volgorde thuis-uit, bv. 2-1",
    "thuis_score": "Doelpunten thuis",
    "uit_score": "Doelpunten uit",
    "hashtag": "Hashtag van de wedstrijd: de eerste drie letters van de thuisploeg en van de uitploeg "
    "(bv. #vscvsw). Per wedstrijd aan te passen onder Hashtag op de pagina Sjablonen",
    "speler": "Naam van de doelpuntmaker (alleen bij een eigen doelpunt)",
    "erin": "Naam van de speler die erin komt (alleen bij een wissel; bij meer spelers: A en B)",
    "eruit": "Naam van de speler die eruit gaat (alleen bij een wissel; bij meer spelers: A en B)",
    "blessuretijd": "Aantal minuten blessuretijd (alleen bij het einde)",
    "afsluiting": "De afsluitregel (sjabloon 'afsluiting')",
}

# (omschrijving, extra toegestane aanduidingen naast de basisaanduidingen)
TEMPLATE_INFO: dict[str, tuple[str, set[str]]] = {
    "start": ("Wedstrijd gestart", set()),
    "goal_home": ("Eigen doelpunt, met doelpuntmaker", {"speler"}),
    "goal_home_no_scorer": ("Eigen doelpunt, zonder doelpuntmaker", set()),
    "goal_away": ("Doelpunt tegenstander", set()),
    "rest": ("Rust", set()),
    "second_half": ("Begin tweede helft", set()),
    "end_win": ("Einde wedstrijd, winst", {"blessuretijd"}),
    "end_other": ("Einde wedstrijd, gelijkspel of verlies", {"blessuretijd"}),
    "substitution": ("Wissel eigen club (alleen LED Scores)", {"erin", "eruit"}),
    "substitution_alt": ("Wissel eigen club, alternatief", {"erin", "eruit"}),
    "afsluiting": ("Afsluiting onder elk bericht", set()),
}

DEFAULT_TEMPLATES: dict[str, str] = {
    "start": "'{minuut} De wedstrijd is begonnen! {afsluiting}",
    "goal_home": "'{minuut} DOELPUNT {eigen}! {speler}! {afsluiting}",
    "goal_home_no_scorer": "'{minuut} DOELPUNT {eigen}! {afsluiting}",
    "goal_away": "'{minuut} Doelpunt {tegenstander}. {afsluiting}",
    "rest": "'{minuut} Rust. {afsluiting}",
    "second_half": "'{minuut} De tweede helft is begonnen! {afsluiting}",
    "end_win": "'{minuut} AFGELOPEN! {afsluiting}",
    "end_other": "'{minuut} Afgelopen! {afsluiting}",
    "substitution": "'{minuut} Wissel bij {eigen}: {erin} vervangt {eruit}. {afsluiting}",
    "substitution_alt": "'{minuut} Wissel bij {eigen}: {eruit} verlaat het veld voor {erin}. {afsluiting}",
    "afsluiting": "{thuis} - {uit} {stand}. {hashtag}",
}

SAMPLE_CONTEXT: dict[str, Any] = {
    "minuut": "12",
    "thuis": "VSCO'61",
    "uit": "VSW 1",
    "eigen": "VSCO'61",
    "tegenstander": "VSW 1",
    "stand": "2-1",
    "thuis_score": 2,
    "uit_score": 1,
    "hashtag": "#vscvsw",
    "speler": "Voorbeeld Speler",
    "erin": "Speler In",
    "eruit": "Speler Uit",
    "blessuretijd": 2,
}

MAX_HASHTAG_LENGTH = 30


def hashtag_part(name: str) -> str:
    """De eerste drie letters van een clubnaam, klein en zonder cijfers of leestekens
    (VSCO'61 wordt vsc, 1e Hattem wordt hat)."""
    return "".join(char for char in str(name) if char.isalpha())[:3].lower()


def default_hashtag(home: str, away: str) -> str:
    """Standaardhashtag van een wedstrijd: de eerste drie letters van de thuis- en van de
    uitploeg (VSCO'61 - VSW 1 wordt #vscvsw)."""
    return f"#{hashtag_part(home)}{hashtag_part(away)}"


def validate_hashtag(value: object) -> str:
    """Maak van een zelfgekozen hashtag `#naam`; leeg betekent: de standaard gebruiken."""
    text = str(value).strip().lstrip("#").strip()
    if not text:
        return ""
    if len(text) > MAX_HASHTAG_LENGTH or not re.fullmatch(r"\w+", text):
        raise ValueError(
            f"Gebruik alleen letters, cijfers en een underscore, zonder spaties (maximaal {MAX_HASHTAG_LENGTH} tekens)."
        )
    return f"#{text}"


class Message(str):
    """Berichttekst met de soort (sjabloonnaam, bv. "start" of "goal_home") en eventueel
    de URL van een foto; gedraagt zich verder als tekst."""

    image: str | None
    kind: str | None

    def __new__(cls, text: str, image: str | None = None, kind: str | None = None) -> "Message":
        message = super().__new__(cls, text)
        message.image = image
        message.kind = kind
        return message


MAX_TEMPLATE_LENGTH = 500
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
# "'{minuut} " aan het begin (of los in de tekst) weghalen als er geen minuut is.
_NO_MINUTE = re.compile(r"'?\{minuut\}\s*")
_BASE = set(PLACEHOLDERS) - {"speler", "erin", "eruit", "blessuretijd", "afsluiting"}


def allowed_placeholders(key: str) -> set[str]:
    allowed = _BASE | TEMPLATE_INFO[key][1]
    if key != "afsluiting":
        allowed.add("afsluiting")
    return allowed


def validate_template(key: str, text: str) -> str | None:
    """Geef een foutmelding terug, of None als het sjabloon geldig is."""
    if key not in TEMPLATE_INFO:
        return f"Onbekend sjabloon: {key}"
    if len(text) > MAX_TEMPLATE_LENGTH:
        return f"Maximaal {MAX_TEMPLATE_LENGTH} tekens."
    if "\n" in text or "\r" in text:
        return "Een sjabloon mag maar één regel zijn."
    unknown = sorted(set(_PLACEHOLDER.findall(text)) - allowed_placeholders(key))
    if unknown:
        names = ", ".join("{" + name + "}" for name in unknown)
        return f"Niet beschikbaar in dit sjabloon: {names}"
    return None


def render(template: str, context: Mapping[str, Any]) -> str:
    """Vul {aanduidingen} in één keer in; waarden worden niet opnieuw uitgebreid."""
    return _PLACEHOLDER.sub(
        lambda match: str(context.get(match.group(1), match.group(0))), template
    )


def render_message(
    templates: Mapping[str, str], key: str, context: Mapping[str, Any]
) -> str:
    """Render een bericht inclusief afsluiting. Leeg sjabloon geeft een lege tekst."""
    template = templates.get(key, DEFAULT_TEMPLATES[key])
    values = dict(context)
    if values.get("minuut") in ("", None):
        template = _NO_MINUTE.sub("", template)
    trailer = templates.get("afsluiting", DEFAULT_TEMPLATES["afsluiting"])
    values["afsluiting"] = render(trailer, values).strip()
    return render(template, values).strip()
