"""
Tests pour le stockage des clubs et athlètes.
Utilise PostgreSQL via testcontainers (voir conftest.py).
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.db import get_db_connection
from scraper.list_athletes import store_athletes, update_memberships
from scraper.list_clubs import store_clubs

# La fixture db_dsn est définie dans conftest.py et utilise PostgreSQL


def test_store_clubs(db_dsn):
    clubs = {
        "1": ("Club One", 2000, 2001),
        "2": ("Club Two", 2002, 2003),
    }
    store_clubs(clubs)
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT ffa_id, name, first_year, last_year FROM clubs ORDER BY ffa_id")
    rows = cur.fetchall()
    conn.close()
    assert len(rows) == 2
    assert rows[0][0] == "1"
    assert rows[0][1] == "Club One"
    assert rows[1][0] == "2"
    assert rows[1][1] == "Club Two"


def test_store_athletes(db_dsn):
    athletes = {
        "ath1": {
            "name": "Athlete One",
            "url": "http://example.com",
            "birth_date": "2000-01-01",
            "license_id": "L1",
            "sexe": "M",
            "nationality": "FR",
        }
    }
    store_athletes(athletes)
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT ffa_id, name, url, birth_date, license_id, sexe, nationality FROM athletes")
    rows = cur.fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == "ath1"
    assert rows[0][1] == "Athlete One"
    assert rows[0][4] == "L1"


def test_update_memberships_sets_club_and_category_of_known_athletes(db_dsn):
    athlete = {"url": "u", "birth_date": None, "license_id": None, "sexe": "F", "nationality": None}
    store_athletes({"a1": {"name": "One", **athlete}, "a2": {"name": "Two", **athlete}})

    assert update_memberships("033033", {"a1": "ES", "unknown": "SE"}) == 1
    # Rien n'a changé : la ligne n'est pas réécrite.
    assert update_memberships("033033", {"a1": "ES"}) == 0
    # Mutation et changement de catégorie.
    assert update_memberships("075001", {"a1": "SE"}) == 1

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT ffa_id, club_ffa_id, category FROM athletes ORDER BY ffa_id")
    rows = cur.fetchall()
    conn.close()
    assert rows == [("a1", "075001", "SE"), ("a2", None, None)]
