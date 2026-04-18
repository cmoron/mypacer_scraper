#!/usr/bin/env python3
"""
List athletes from a PostgreSQL database containing club IDs and store them in the same database.
"""

import argparse
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from time import time

import psycopg2
import requests
from bs4 import BeautifulSoup

from core.config import get_logger, setup_logging
from core.db import DatabaseConnectionError, create_database, get_db_connection

logger = get_logger(__name__)

# URL of the club
CLUB_URL = "https://www.athle.fr/bases/liste.aspx?frmbase=cclubs&frmmode=2&frmespace=&frmtypeclub=M&frmsaison={year}&frmnclub={club_id}&frmposition={page}"
ATHLETE_BASE_URL = "https://www.athle.fr/athletes/{athlete_id}"
SESSION = requests.Session()
adapter = HTTPAdapter = requests.adapters.HTTPAdapter(
    pool_connections=24,
    pool_maxsize=24,
)
SESSION.mount("https://", adapter)

# First year of the database
FIRST_YEAR = 2004

total_athletes = 0


def generate_club_url(year: int, club_id: str, page: int = 0) -> str:
    """
    Generate the URL for a club ID
    Args:
        year (int): The year
        club_id (str): The club ID
        page (int): The page number
    Returns:
        str: The URL for the club
    """
    return CLUB_URL.format(year=year, club_id=club_id, page=page)


def fetch_and_parse_html(url: str, max_retries: int = 3) -> BeautifulSoup | None:
    """
    Fetch and parse the HTML content of a URL with retry logic
    Args:
        url (str): The URL to fetch
        max_retries (int): Maximum number of retry attempts
    Returns:
        BeautifulSoup: The parsed HTML content, or None if all retries failed
    """
    for attempt in range(max_retries):
        try:
            response = SESSION.get(url, timeout=20)
            response.raise_for_status()
            return BeautifulSoup(response.text, "lxml")
        except requests.Timeout:
            logger.warning("Timeout fetching %s (attempt %d/%d)", url, attempt + 1, max_retries)
            if attempt == max_retries - 1:
                logger.error("Failed to fetch %s after %d attempts (timeout)", url, max_retries)
                return None
        except requests.HTTPError as e:
            logger.error("HTTP error fetching %s: %s", url, e)
            return None
        except requests.RequestException as e:
            logger.warning(
                "Error fetching %s: %s (attempt %d/%d)", url, e, attempt + 1, max_retries
            )
            if attempt == max_retries - 1:
                logger.error("Failed to fetch %s after %d attempts", url, max_retries)
                return None
    return None


def get_max_pages(soup: BeautifulSoup) -> int:
    """
    Get the number of club pages for a given year
    Args:
        soup (BeautifulSoup): The BeautifulSoup object
    Returns:
        int: Number of club pages
    """
    max_pages = 0
    if soup:
        select_element = soup.find("select", class_="barSelect")
        if select_element:
            max_pages = len(select_element.find_all("option"))
    return max_pages


def athlete_exists(athlete_ffa_id: str) -> bool:
    """
    Check if an athlete already exists in the PostgreSQL database.
    Args:
        athlete_ffa_id (str): The FFA ID of the athlete
    Returns:
        bool: True if the athlete exists, False otherwise
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    exists = False
    try:
        cursor.execute("SELECT 1 FROM athletes WHERE ffa_id = %s", (athlete_ffa_id,))
        exists = cursor.fetchone() is not None
    except psycopg2.Error as e:
        logger.error("Error: %s", e)
        raise
    finally:
        cursor.close()
        conn.close()
    return exists


def get_existing_athlete_ids(athlete_ids: list[str]) -> set[str]:
    """
    Get all existing athlete FFA IDs from the database in a single query.
    This is much more efficient than calling athlete_exists() for each ID.

    Args:
        athlete_ids (list[str]): List of FFA IDs to check
    Returns:
        set[str]: Set of FFA IDs that already exist in the database
    """
    if not athlete_ids:
        return set()

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # Use IN clause to check all IDs at once
        cursor.execute("SELECT ffa_id FROM athletes WHERE ffa_id = ANY(%s)", (athlete_ids,))
        existing_ids = {row[0] for row in cursor.fetchall()}
        logger.debug(
            "Found %d existing athletes out of %d checked", len(existing_ids), len(athlete_ids)
        )
        return existing_ids
    except psycopg2.Error as e:
        logger.error("Error checking existing athletes: %s", e)
        raise
    finally:
        cursor.close()
        conn.close()


def extract_athlete_data(athletes: dict, soup: BeautifulSoup) -> dict:
    """
    Extract athlete data from a BeautifulSoup object
    Args:
        athletes (dict): The athletes
        soup (BeautifulSoup): The BeautifulSoup object
    Returns:
        dict: The athletes
    """
    if soup:
        athlete_links = soup.find_all("a", href=lambda x: x and "javascript:bddThrowAthlete" in x)
        for link in athlete_links:
            id_athlete = link["href"].split(",")[1].strip("'").strip()
            if id_athlete not in athletes:
                name_athlete = link.get_text(strip=True)
                # format BASE_URL with athlete_id
                url = ATHLETE_BASE_URL.format(athlete_id=id_athlete)
                birth_date, license_id, sexe, nationality = extract_birth_date_and_license(url)
                athletes[id_athlete] = {
                    "name": name_athlete,
                    "url": url,
                    "birth_date": birth_date,
                    "license_id": license_id,
                    "sexe": sexe,
                    "nationality": nationality,
                }
    return athletes


def extract_athlete_data_parallel(athletes: dict, soup: BeautifulSoup) -> dict:
    """
    Extract athlete data from a BeautifulSoup object using parallel requests.
    Optimized to check existing athletes in bulk before making HTTP requests.

    Args:
        athletes (dict): The athletes
        soup (BeautifulSoup): The BeautifulSoup object
    Returns:
        dict: The athletes
    """
    if soup:
        athlete_links = soup.find_all("a", href=lambda x: x and "athletes" in x)

        # Extract all athlete IDs first
        athlete_ids = []
        link_by_id = {}
        for link in athlete_links:
            id_athlete = link["href"].split("/")[2].strip()
            athlete_ids.append(id_athlete)
            link_by_id[id_athlete] = link

        # Check which athletes already exist in DB (single query)
        existing_ids = get_existing_athlete_ids(athlete_ids)

        # Filter out athletes that already exist
        new_links = [
            link_by_id[id_athlete]
            for id_athlete in athlete_ids
            if id_athlete not in existing_ids and id_athlete not in athletes
        ]

        logger.info(
            "Found %d athletes, %d already in DB, %d new to fetch",
            len(athlete_ids),
            len(existing_ids),
            len(new_links),
        )

        # Only process new athletes
        if new_links:
            with ThreadPoolExecutor(max_workers=24) as executor:
                future_to_athlete = {
                    executor.submit(fetch_and_extract_athlete_data, link): link
                    for link in new_links
                }
                for future in as_completed(future_to_athlete):
                    athlete_data = future.result()
                    if athlete_data:
                        id_athlete = athlete_data["id"]
                        if id_athlete not in athletes:
                            athletes[id_athlete] = athlete_data
    return athletes


def fetch_and_extract_athlete_data(link):
    """
    Fetch and extract athlete data from an individual athlete link.
    Note: This function assumes the athlete doesn't exist in DB (filtering done upstream).

    Args:
        link (bs4.element.Tag): A BeautifulSoup tag containing the athlete link
    Returns:
        dict: Extracted data for one athlete
    """
    id_athlete = link["href"].split("/")[2].strip()

    logger.debug("Processing athlete ID: %s", id_athlete)

    name_athlete = link.get_text(strip=True)
    url = ATHLETE_BASE_URL.format(athlete_id=id_athlete)

    # Fetch athlete details from their page
    birth_date, license_id, sexe, nationality = extract_birth_date_and_license(url)

    return {
        "id": id_athlete,
        "name": name_athlete,
        "url": url,
        "birth_date": birth_date,
        "license_id": license_id,
        "sexe": sexe,
        "nationality": nationality,
    }


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


def store_athletes(athletes: dict):
    """
    Store the athletes in the PostgreSQL database using the new schema.
    Uses ffa_id as the unique FFA identifier and lets PostgreSQL handle:
    - Auto-generated internal id (SERIAL)
    - normalized_name via trigger (or manual for SQLite)
    - Duplicate license_id via partial unique constraint

    Args:
        athletes (dict): Dictionary with ffa_id as key and athlete info as value
    """
    global total_athletes
    conn = get_db_connection()
    cursor = conn.cursor()

    inserted = 0

    try:
        for ffa_id, info in athletes.items():
            normalized = normalize_name(info["name"])

            # Insert or update based on ffa_id
            # Include normalized_name for compatibility with SQLite (PostgreSQL trigger will override)
            cursor.execute(
                """
                INSERT INTO athletes (ffa_id, name, normalized_name, url, birth_date, license_id, sexe, nationality)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (ffa_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    normalized_name = EXCLUDED.normalized_name,
                    url = EXCLUDED.url,
                    birth_date = COALESCE(EXCLUDED.birth_date, athletes.birth_date),
                    license_id = COALESCE(EXCLUDED.license_id, athletes.license_id),
                    sexe = COALESCE(EXCLUDED.sexe, athletes.sexe),
                    nationality = COALESCE(EXCLUDED.nationality, athletes.nationality)
            """,
                (
                    ffa_id,
                    info["name"],
                    normalized,
                    info["url"],
                    info["birth_date"],
                    info.get("license_id"),
                    info["sexe"],
                    info["nationality"],
                ),
            )

            if cursor.rowcount > 0:
                inserted += 1

        conn.commit()
        logger.info("Athletes stored: %d athletes", inserted)
        total_athletes += inserted
    except psycopg2.Error as e:
        logger.error("Error storing athletes: %s", e)
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def ensure_schema_exists():
    """
    Ensure the database schema exists.
    If tables don't exist, create them using the schema from core.schema
    """
    from core.schema import create_tables

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Check if athletes table exists with the new schema
        cursor.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'athletes' AND column_name = 'ffa_id'
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


def retrieve_clubs(club_id: str, year: int) -> dict:
    """
    Retrieve the clubs from the PostgreSQL database only if the last year is greater or equal to the given year
    Args:
        club_id (str): The club FFA ID
        year (int): The year
    Returns:
        dict: Dictionary with ffa_id as key and name as value
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    res = {}
    try:
        if club_id:
            cursor.execute("SELECT ffa_id, name FROM clubs WHERE ffa_id = %s", (club_id,))
        else:
            cursor.execute(
                "SELECT ffa_id, name FROM clubs WHERE first_year <= %s AND last_year >= %s",
                (year, year),
            )
        res = dict(cursor.fetchall())
    except psycopg2.Error as e:
        logger.error("Error: %s", e)
        raise
    finally:
        cursor.close()
        conn.close()
    return res


def extract_athletes_from_club(year: int, club_id: str) -> dict:
    """
    Extract athletes from a club
    Args:
        year (int): The year
        club_id (str): The club ID
    Returns:
        dict: The athletes
    """
    athletes = {}
    url = generate_club_url(year, club_id)
    soup = fetch_and_parse_html(url)
    if soup:
        return extract_athlete_data_parallel(athletes, soup)
    return athletes


def extract_birth_date_and_license(url: str) -> dict:
    """
    Extract birth date and license number from the athlete's page
    Args:
        url (str): The URL of the athlete

    Returns:
        dict: Dictionary containing 'birth_date' and 'license_number'
    """
    birth_date = None
    license_number = None
    sexe = None
    nationality = None

    soup = fetch_and_parse_html(url)

    # Normaliser tous les <p class="text-white"> en une liste de lignes lisibles
    lines = []
    if soup:
        lines = [p.get_text(" ", strip=True) for p in soup.select("p.text-white")]

    for line in lines:
        # Année de naissance: "Né(e) en : 2004"
        if line.startswith("Né(e) en"):
            m = re.search(r"\b(19|20)\d{2}\b", line)
            if m:
                birth_date = m.group(0)
            continue

        # Catégorie / Nationalité : "ES / F / FRA"
        if line.startswith("Catégorie / Nationalité"):
            # On récupère les trois champs séparés par "/"
            # ex: "Catégorie / Nationalité : ES / F / FRA"
            # -> cat='ES', sex='F', nat='FRA'
            parts = [x.strip() for x in line.split(": ", 1)[1].split("/")]
            if len(parts) >= 3:
                sex = parts[1]
                nat = parts[2]
                # Nettoyage simple
                sex = re.sub(r"[^A-Za-z]", "", sex)[:1] or None
                nat = re.sub(r"[^A-Za-z]", "", nat)[:3].upper() or None
                sexe = sex
                nationality = nat
            continue

        # N° de licence : "N° de licence : 2387169 - COMP (maj le ...)"
        if line.startswith("N° de licence"):
            m = re.search(r"\b\d{5,}\b", line)  # n° numérique
            if m:
                license_number = m.group(0)
            continue

    return birth_date, license_number, sexe, nationality


def update_athletes_info():
    """
    Update missing information for all athletes in the PostgreSQL database.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    # Sélectionner tous les athlètes qui ont des informations manquantes
    cursor.execute("SELECT ffa_id, url FROM athletes WHERE url IS NULL OR url = ''")
    athletes_to_update = cursor.fetchall()
    logger.info("Updating information for %s athletes", len(athletes_to_update))

    cpt = 0
    # Utiliser ThreadPoolExecutor pour paralléliser les mises à jour
    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_id = {
            executor.submit(fetch_and_update_athlete, ffa_id): ffa_id
            for (ffa_id, _) in athletes_to_update
        }
        for future in as_completed(future_to_id):
            try:
                cpt += 1
                logger.info(
                    "%s / %s - Updated athlete %s",
                    cpt,
                    len(athletes_to_update),
                    future_to_id[future],
                )
                future.result()  # Just to catch any exceptions that might have been thrown
            except Exception as e:
                logger.error("Failed to update athlete %s: %s", future_to_id[future], str(e))
                raise
    conn.close()


def fetch_and_update_athlete(ffa_id):
    """
    Fetch and update athlete data for a given FFA ID in the PostgreSQL database.
    Args:
        ffa_id (str): The FFA ID of the athlete
    """
    url = ATHLETE_BASE_URL.format(athlete_id=ffa_id)
    birth_date, license_id, sexe, nationality = extract_birth_date_and_license(url)

    # Mettre à jour la base de données
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE athletes SET url = %s, birth_date = %s, license_id = %s, sexe = %s, nationality = %s
        WHERE ffa_id = %s
    """,
        (url, birth_date, license_id, sexe, nationality, ffa_id),
    )
    conn.commit()
    conn.close()


def process_clubs_and_athletes(first_year: int, last_year: int, club_id: str) -> None:
    """
    Process the clubs and athletes and store them in the PostgreSQL database
    Args:
        first_year (int): The first year
        last_year (int): The last year
        club_id (str): The club FFA ID
    """
    start_time = time()
    try:
        ensure_schema_exists()
        logger.info("Processing years from %s to %s", first_year, last_year)
        for year in range(first_year, last_year + 1):
            clubs = retrieve_clubs(club_id, year)
            nb_clubs = len(clubs)
            logger.info("Year %s: Found %s clubs to process", year, nb_clubs)
            cpt = 0

            for club_ffa_id in clubs:
                cpt += 1
                try:
                    logger.info(
                        "%s / %s - Processing club %s for year %s",
                        cpt,
                        nb_clubs,
                        clubs[club_ffa_id],
                        year,
                    )
                except UnicodeEncodeError:
                    logger.error("UnicodeEncodeError for %s", club_ffa_id)
                    raise
                athletes = extract_athletes_from_club(year, club_ffa_id)
                store_athletes(athletes)

        # Print final summary
        duration = time() - start_time
        hours = int(duration // 3600)
        minutes = int((duration % 3600) // 60)
        seconds = int(duration % 60)

        logger.info("=" * 80)
        logger.info("✅ SCRAPING TERMINÉ")
        logger.info("=" * 80)
        logger.info("📊 Athlètes ajoutés : %s", f"{total_athletes:,}".replace(",", " "))
        logger.info("📅 Période traitée : %s - %s", first_year, last_year)
        logger.info("⏱️  Durée : %dh %02dmin %02ds", hours, minutes, seconds)
        logger.info("=" * 80)

    except KeyboardInterrupt:
        logger.error("Interrupted by user")
        raise
    except requests.RequestException as e:
        logger.error("Error: %s", e)
        raise


def main():
    """
    Main function
    """
    parser = argparse.ArgumentParser(
        description="List athletes from a PostgreSQL database containing club IDs."
    )
    parser.add_argument(
        "--first-year", type=int, default=FIRST_YEAR, help="First year of the database."
    )
    parser.add_argument(
        "--last-year", type=int, default=datetime.now().year, help="Last year of the database."
    )
    parser.add_argument("--club-id", type=str, help="Club ID to extract athletes from.")
    parser.add_argument(
        "--update", action="store_true", help="Update missing information for all athletes"
    )
    args = parser.parse_args()
    first_year = args.first_year
    last_year = max(first_year, args.last_year)

    logger.info("Start scrapping")

    try:
        create_database()
    except DatabaseConnectionError as e:
        logger.error(str(e))
        sys.exit(1)

    if args.update:
        update_athletes_info()
    else:
        process_clubs_and_athletes(first_year, last_year, args.club_id)


if __name__ == "__main__":
    setup_logging("list_athletes")
    logger.info("Script started with args: %s", " ".join(sys.argv))
    main()
