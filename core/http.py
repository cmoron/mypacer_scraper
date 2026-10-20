"""Session HTTP partagée vers athle.fr."""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = "MyPacer-scraper/1.0 (+https://mypacer.fr; contact@mypacer.fr)"


def make_session(pool_size: int = 24, backoff_factor: float = 2.0) -> requests.Session:
    """Session avec retries exponentiels sur timeouts et erreurs transitoires.

    athle.fr renvoie ponctuellement des 503 et des read timeouts : sans retry,
    une seule erreur suffisait à perdre la mise à jour hebdomadaire.
    """
    retry = Retry(
        total=4,
        backoff_factor=backoff_factor,  # 2s, 4s, 8s, 16s
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size, max_retries=retry)
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    session.mount("https://", adapter)
    return session
