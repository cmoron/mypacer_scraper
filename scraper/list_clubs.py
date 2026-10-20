#!/usr/bin/env python3
"""
Récupère les données des clubs d'athlétisme pour une année donnée et les stocke dans une base de données PostgreSQL.
"""

import argparse
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from core.config import get_logger, setup_logging
from core.db import create_database, get_db_connection
from core.http import make_session

# URL de la base de données des clubs d'athlétisme
FIRST_YEAR = 2004
BASES_ATHLE_URL = "https://www.athle.fr/bases/"
SESSION = make_session()

logger = get_logger(__name__)


def get_max_club_pages(year: int) -> int:
    """
    Récupère le nombre de pages de clubs pour une année donnée
    Args:
        year (int): Année pour laquelle récupérer les données

    Returns:
        int: Nombre de pages de clubs
    """
    max_pages = 0
    club_base_url = (
        BASES_ATHLE_URL
        + f"/liste.aspx?frmpostback=true&frmbase=cclubs&frmmode=1&frmespace=0&frmsaison={year}&frmsexe=&frmligue=&frmdepartement=&frmnclub=&frmruptures="
    )

    response = SESSION.get(club_base_url, timeout=20)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "lxml")
    select_element = soup.find("div", id="optionsPagination")

    if select_element:
        max_pages = len(select_element.find_all("div", class_="select-option"))

    return max_pages


def fetch_club_page(url: str) -> BeautifulSoup:
    """
    Fetch and parse the HTML content of a URL
    Args:
        url (str): The URL to fetch
    Returns:
        BeautifulSoup: The parsed HTML content
    """
    try:
        response = SESSION.get(url, timeout=20)
        response.raise_for_status()
        return BeautifulSoup(response.text, "lxml")
    except requests.RequestException as e:
        logger.error("Error fetching %s: %s", url, e)
        return None


def extract_clubs_from_page(soup: BeautifulSoup) -> dict:
    """
    Extract clubs from a BeautifulSoup object
    Args:
        soup (BeautifulSoup): The BeautifulSoup object
    Returns:
        dict: Dictionary of clubs
    """
    tbody = soup.find("tbody", class_="text-blue-primary") or soup

    clubs: dict[str, str] = {}

    # Les lignes utiles ont exactement 7 <td> au niveau racine (pas les detail-row).
    for tr in tbody.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) != 7:
            continue

        name_td = tds[2]  # colonne "Club" (contient un <a>)
        id_td = tds[3]  # colonne "N°Club"

        a = name_td.find("a")
        if not a:
            continue

        club_name = " ".join(a.get_text(strip=True).split())
        club_name = re.sub(r"\*+$", "", club_name).strip()  # retire les astérisques finaux
        club_id = id_td.get_text(strip=True)

        if club_id and club_name:
            clubs[club_id] = club_name

    return clubs


def extract_clubs(clubs: dict, year: int) -> dict:
    """
    Récupère les clubs d'athlétisme pour une année donnée
    Args:
        year (int): Année pour laquelle récupérer les données
    Returns:
        dict: Dictionnaire des clubs d'athlétisme avec leur ID comme clé
              et leur nom et année comme valeur
    """
    max_club_pages = get_max_club_pages(year)
    club_base_url = (
        BASES_ATHLE_URL
        + f"/liste.aspx?frmpostback=true&frmbase=cclubs&frmmode=1&frmespace=0&frmsaison={year}&frmsexe=&frmligue=&frmdepartement=&frmnclub=&frmruptures=&frmposition="
    )
    urls = [club_base_url + str(page) for page in range(max_club_pages)]

    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_url = {executor.submit(fetch_club_page, url): url for url in urls}
        for future in as_completed(future_to_url):
            try:
                soup = future.result()
                if soup:
                    page_clubs = extract_clubs_from_page(soup)
                    for club_id, club_name in page_clubs.items():
                        if club_id not in clubs:
                            clubs[club_id] = (club_name, year, year)
                        else:
                            clubs[club_id] = (
                                club_name,
                                min(clubs[club_id][1], year),
                                max(clubs[club_id][2], year),
                            )
                        logger.debug("Fetched club %s: %s", club_id, clubs[club_id])
            except requests.RequestException as e:
                logger.error("Request error for URL %s: %s", future_to_url[future], e)

    return clubs


def ensure_schema_exists():
    """
    Ensure the database schema exists.
    If tables don't exist, create them using the schema from core.schema
    """
    import psycopg2

    from core.schema import create_tables

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Check if clubs table exists with the new schema
        cursor.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'clubs' AND column_name = 'ffa_id'
        """
        )

        if cursor.fetchone() is None:
            logger.info("Schema not found or outdated, creating new schema...")
            cursor.close()
            conn.close()
            create_tables()
            logger.info("Schema created successfully")
        else:
            logger.debug("Schema already exists")
    except psycopg2.Error as e:
        logger.error("Error checking schema: %s", e)
        raise
    finally:
        if not cursor.closed:
            cursor.close()
        if not conn.closed:
            conn.close()


def normalize_name(name: str) -> str:
    """
    Normalize a name for searching (lowercase, no accents, clean spaces).
    This replicates the PostgreSQL normalize_text() function.
    """
    try:
        from unidecode import unidecode

        normalized = unidecode(name.lower())
        # Clean multiple spaces
        normalized = " ".join(normalized.split())
        return normalized
    except ImportError:
        # Fallback if unidecode is not available
        return " ".join(name.lower().split())


def store_clubs(clubs: dict):
    """
    Stocke les clubs dans une base de données PostgreSQL using the new schema.
    Uses ffa_id as the unique FFA identifier and lets PostgreSQL handle:
    - Auto-generated internal id (SERIAL)
    - normalized_name via trigger (or manual for SQLite)

    Args:
        clubs (dict): Dictionary with club_ffa_id as key and (name, first_year, last_year) as value
    """
    import psycopg2

    conn = get_db_connection()
    cursor = conn.cursor()

    inserted = 0
    updated = 0

    try:
        for club_ffa_id, club in clubs.items():
            name = club[0]
            first_year = club[1]
            last_year = club[2]
            normalized = normalize_name(name)

            # Insert or update based on ffa_id
            # Include normalized_name for compatibility with SQLite (PostgreSQL trigger will override)
            cursor.execute(
                """
                INSERT INTO clubs (ffa_id, name, normalized_name, first_year, last_year)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (ffa_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    normalized_name = EXCLUDED.normalized_name,
                    first_year = LEAST(clubs.first_year, EXCLUDED.first_year),
                    last_year = GREATEST(clubs.last_year, EXCLUDED.last_year)
            """,
                (club_ffa_id, name, normalized, first_year, last_year),
            )

            if cursor.rowcount > 0:
                inserted += 1

        conn.commit()
        logger.info("Clubs stored: %d clubs", inserted)

    except psycopg2.Error as e:
        logger.error("Error storing clubs: %s", e)
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def main():
    """
    Fonction principale
    """
    parser = argparse.ArgumentParser(
        description="Récupère les données des clubs d'athlétisme FFA sur bases.athle"
    )
    parser.add_argument(
        "--first-year",
        type=int,
        default=FIRST_YEAR,
        help=f"Année de départ pour l'extraction (défaut : {FIRST_YEAR})",
    )
    args = parser.parse_args()

    # Calcul de la saison actuelle : si on est à partir de septembre, on prend année+1
    now = datetime.now()
    current_season = now.year + 1 if now.month >= 9 else now.year

    logger.info("Début de l'extraction des clubs")

    try:
        create_database()
        ensure_schema_exists()

        # for each year from FIRST_YEAR to current year
        clubs = {}
        for year in range(args.first_year, current_season + 1):
            clubs = extract_clubs(clubs, year)

        logger.info("Extraction terminée : %s clubs", len(clubs))

        store_clubs(clubs)
    except requests.RequestException as e:
        # Sortie non nulle : sinon le cron marque le run comme réussi.
        logger.error("Erreur lors de la requête : %s", e)
        sys.exit(1)


if __name__ == "__main__":
    setup_logging("list_clubs")
    main()
