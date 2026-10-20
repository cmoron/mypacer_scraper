import pathlib
import sys

from bs4 import BeautifulSoup

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))

import scraper.list_athletes as list_athletes
import scraper.list_clubs as list_clubs

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def test_extract_clubs_from_page():
    html = (FIXTURES / "clubs.html").read_text()
    soup = BeautifulSoup(html, "lxml")
    clubs = list_clubs.extract_clubs_from_page(soup)
    assert clubs == {"1234": "Club Name", "5678": "Second Club"}


def test_extract_athlete_data_parallel(monkeypatch):
    html = (FIXTURES / "club_athletes.html").read_text()
    soup = BeautifulSoup(html, "lxml")

    def fake_extract(url):
        return "2004", "2387169", "F", "FRA"

    def fake_get_existing(athlete_ids):
        # Mock: retourne un set vide (aucun athlète existant)
        return set()

    monkeypatch.setattr(list_athletes, "extract_birth_date_and_license", fake_extract)
    monkeypatch.setattr(list_athletes, "get_existing_athlete_ids", fake_get_existing)
    athletes = list_athletes.extract_athlete_data_parallel({}, soup)
    from pprint import pprint

    pprint(athletes)
    expected_url_1 = list_athletes.ATHLETE_BASE_URL.format(athlete_id="974476")
    expected_url_2 = list_athletes.ATHLETE_BASE_URL.format(athlete_id="974477")

    from pprint import pprint

    pprint(athletes)

    assert athletes == {
        "974476": {
            "id": "974476",
            "name": "DOE Jane",
            "url": expected_url_1,
            "birth_date": "2004",
            "license_id": "2387169",
            "sexe": "F",
            "nationality": "FRA",
        },
        "974477": {
            "id": "974477",
            "name": "DOE John",
            "url": expected_url_2,
            "birth_date": "2004",
            "license_id": "2387169",
            "sexe": "F",
            "nationality": "FRA",
        },
    }


def test_extract_birth_date_and_license(monkeypatch):
    html = (FIXTURES / "athlete.html").read_text()
    soup = BeautifulSoup(html, "lxml")

    def fake_fetch(url):
        return soup

    monkeypatch.setattr(list_athletes, "fetch_and_parse_html", fake_fetch)
    birth_date, license_id, sexe, nationality = list_athletes.extract_birth_date_and_license(
        "dummy"
    )
    assert birth_date == "2004"
    assert license_id == "2387169"
    assert sexe == "F"
    assert nationality == "FRA"


def test_extract_athlete_data_parallel_skips_non_athlete_links_and_failed_pages(monkeypatch):
    """Lien de menu « /athletes-accompagnement » ignoré ; fiche en échec non stockée."""
    html = (
        (FIXTURES / "club_athletes.html")
        .read_text()
        .replace(
            "<body>", '<body><a href="/athletes-accompagnement">Accompagnement des athlètes</a>'
        )
    )
    soup = BeautifulSoup(html, "lxml")

    def fake_extract(url):
        # 974477 : athle.fr indisponible, la fiche doit être retentée au prochain run.
        return None if url.endswith("974477") else ("2004", "2387169", "F", "FRA")

    monkeypatch.setattr(list_athletes, "extract_birth_date_and_license", fake_extract)
    monkeypatch.setattr(list_athletes, "get_existing_athlete_ids", lambda ids: set())
    athletes = list_athletes.extract_athlete_data_parallel({}, soup)

    assert list(athletes) == ["974476"]


def test_extract_birth_date_and_license_returns_none_when_fetch_fails(monkeypatch):
    monkeypatch.setattr(list_athletes, "fetch_and_parse_html", lambda url: None)
    assert list_athletes.extract_birth_date_and_license("dummy") is None
