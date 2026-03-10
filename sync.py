#!/usr/bin/env python3
"""
Storyteller → Hardcover one-way progress sync.

Polls Storyteller every 15 minutes and pushes any book progress
(≥ 1% change) to Hardcover. Books with zero progress are ignored.
"""

import json
import logging
import os
import time
from datetime import date
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # In Docker, env vars come from compose — dotenv not needed

# ── Configuration ─────────────────────────────────────────────────────────────

STORYTELLER_URL      = os.environ["STORYTELLER_URL"].rstrip("/")
STORYTELLER_USERNAME = os.environ["STORYTELLER_USERNAME"]
STORYTELLER_PASSWORD = os.environ["STORYTELLER_PASSWORD"]
HARDCOVER_TOKEN      = os.environ["HARDCOVER_TOKEN"]

SYNC_INTERVAL  = int(os.environ.get("SYNC_INTERVAL_MINUTES", "15")) * 60
MIN_DELTA      = float(os.environ.get("MIN_PROGRESS_DELTA", "0.01"))  # 1 %
STATE_FILE     = Path(os.environ.get("STATE_FILE", "state.json"))
HARDCOVER_API  = "https://api.hardcover.app/v1/graphql"

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── State helpers ─────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"books": {}}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Storyteller client ────────────────────────────────────────────────────────

class StorytellerClient:
    def __init__(self):
        self.session = requests.Session()

    def authenticate(self) -> None:
        resp = self.session.post(
            f"{STORYTELLER_URL}/api/token",
            data={"username": STORYTELLER_USERNAME, "password": STORYTELLER_PASSWORD},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        resp.raise_for_status()
        token = resp.json()["access_token"]
        self.session.headers["Authorization"] = f"Bearer {token}"
        log.info("Authenticated with Storyteller.")

    def get_books(self) -> list[dict]:
        resp = self.session.get(f"{STORYTELLER_URL}/api/books", timeout=15)
        resp.raise_for_status()
        return resp.json()

    def get_progress(self, book_id) -> float | None:
        """
        Returns totalProgression (0.0–1.0) or None if no position has
        been recorded yet.
        """
        resp = self.session.get(
            f"{STORYTELLER_URL}/api/books/{book_id}/positions",
            timeout=15,
        )
        if resp.status_code in (404, 204):
            return None
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return None
        locator = data.get("locator") or {}
        return locator.get("locations", {}).get("totalProgression")


# ── Hardcover client ──────────────────────────────────────────────────────────

class HardcoverClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {HARDCOVER_TOKEN}",
            "Content-Type": "application/json",
        })
        self.user_id: int | None = None

    def _gql(self, query: str, variables: dict | None = None) -> dict:
        payload: dict = {"query": query}
        if variables:
            payload["variables"] = variables
        resp = self.session.post(HARDCOVER_API, json=payload, timeout=20)
        resp.raise_for_status()
        body = resp.json()
        if "errors" in body:
            raise RuntimeError(f"GraphQL errors: {body['errors']}")
        return body["data"]

    def get_user_id(self) -> int:
        data = self._gql("query { me { id } }")
        self.user_id = data["me"][0]["id"]
        return self.user_id

    def search_book(self, title: str) -> int | None:
        """
        Search Hardcover by title. Returns the top-match book_id or None.
        """
        data = self._gql(
            """
            query Search($q: String!) {
              search(query: $q, query_type: "Book", per_page: 5, page: 1) {
                results
              }
            }
            """,
            {"q": title},
        )
        results = data.get("search", {}).get("results", {})
        if isinstance(results, str):
            results = json.loads(results)
        hits = results.get("hits", [])
        if not hits:
            return None
        # Hardcover search results nest the book data under "document"
        doc = hits[0].get("document", hits[0])
        raw_id = doc.get("id")
        return int(raw_id) if raw_id is not None else None

    def get_book_edition_data(self, book_id: int) -> dict:
        """Return audio_seconds and pages for a book (used to calculate progress input)."""
        data = self._gql(
            """
            query BookData($bookId: Int!) {
              books(where: {id: {_eq: $bookId}}) {
                pages
                editions(
                  where: {audio_seconds: {_gt: 0}},
                  limit: 1,
                  order_by: {audio_seconds: desc}
                ) {
                  audio_seconds
                }
              }
            }
            """,
            {"bookId": book_id},
        )
        books = data.get("books", [])
        if not books:
            return {"pages": None, "audio_seconds": None}
        book = books[0]
        editions = book.get("editions", [])
        audio_seconds = editions[0]["audio_seconds"] if editions else None
        return {"pages": book.get("pages"), "audio_seconds": audio_seconds}

    def get_user_book(self, book_id: int) -> dict | None:
        data = self._gql(
            """
            query GetUserBook($userId: Int!, $bookId: Int!) {
              user_books(where: {
                user_id: {_eq: $userId},
                book_id: {_eq: $bookId}
              }) {
                id
                status_id
                user_book_reads(order_by: {id: desc}, limit: 1) {
                  id
                }
              }
            }
            """,
            {"userId": self.user_id, "bookId": book_id},
        )
        books = data.get("user_books", [])
        return books[0] if books else None

    def create_user_book(self, book_id: int) -> int:
        """Creates a user_book with status 'Currently Reading'. Returns id."""
        data = self._gql(
            """
            mutation CreateUserBook($bookId: Int!) {
              insert_user_book(object: {book_id: $bookId, status_id: 2}) {
                id
              }
            }
            """,
            {"bookId": book_id},
        )
        return data["insert_user_book"]["id"]

    def set_status(self, user_book_id: int, status_id: int) -> None:
        self._gql(
            """
            mutation SetStatus($id: Int!, $statusId: Int!) {
              update_user_book(id: $id, object: {status_id: $statusId}) {
                id
              }
            }
            """,
            {"id": user_book_id, "statusId": status_id},
        )

    @staticmethod
    def _progress_vars(
        progress: float,
        audio_seconds: int | None,
        pages: int | None,
    ) -> tuple[int | None, int | None]:
        """Return (progress_seconds, progress_pages) — one will be set, one None."""
        if audio_seconds:
            return round(progress * audio_seconds), None
        if pages:
            return None, round(progress * pages)
        return None, None

    def create_read_session(
        self,
        user_book_id: int,
        progress: float,
        audio_seconds: int | None,
        pages: int | None,
    ) -> int:
        prog_secs, prog_pages = self._progress_vars(progress, audio_seconds, pages)
        data = self._gql(
            """
            mutation CreateRead(
              $userBookId: Int!,
              $startedAt: date!,
              $progressSeconds: Int,
              $progressPages: Int
            ) {
              insert_user_book_read(
                user_book_id: $userBookId,
                user_book_read: {
                  started_at: $startedAt,
                  progress_seconds: $progressSeconds,
                  progress_pages: $progressPages
                }
              ) {
                id
              }
            }
            """,
            {
                "userBookId": user_book_id,
                "startedAt": str(date.today()),
                "progressSeconds": prog_secs,
                "progressPages": prog_pages,
            },
        )
        return data["insert_user_book_read"]["id"]

    def update_read_session(
        self,
        read_id: int,
        progress: float,
        audio_seconds: int | None,
        pages: int | None,
    ) -> None:
        prog_secs, prog_pages = self._progress_vars(progress, audio_seconds, pages)
        self._gql(
            """
            mutation UpdateRead(
              $id: Int!,
              $progressSeconds: Int,
              $progressPages: Int
            ) {
              update_user_book_read(id: $id, object: {
                progress_seconds: $progressSeconds,
                progress_pages: $progressPages
              }) {
                id
              }
            }
            """,
            {
                "id": read_id,
                "progressSeconds": prog_secs,
                "progressPages": prog_pages,
            },
        )


# ── Per-book sync ─────────────────────────────────────────────────────────────

def sync_book(
    st: StorytellerClient,
    hc: HardcoverClient,
    book: dict,
    state: dict,
) -> None:
    book_id  = book["id"]
    title    = book.get("title", f"book-{book_id}")
    book_key = str(book.get("uuid", book_id))

    progress = st.get_progress(book_id)

    # Ignore books that haven't been started
    if progress is None or progress == 0.0:
        return

    book_state    = state["books"].get(book_key, {})
    last_progress = book_state.get("last_synced_progress", -1.0)

    if abs(progress - last_progress) < MIN_DELTA:
        return  # Change too small — skip

    log.info('"%s": %.1f%% → %.1f%%', title, last_progress * 100, progress * 100)

    # ── Find book on Hardcover ────────────────────────────────────────────────
    hc_book_id    = book_state.get("hardcover_book_id")
    audio_seconds = book_state.get("audio_seconds")
    pages         = book_state.get("pages")

    if not hc_book_id:
        hc_book_id = hc.search_book(title)
        if not hc_book_id:
            log.warning('  Could not find "%s" on Hardcover — skipping.', title)
            return
        edition = hc.get_book_edition_data(hc_book_id)
        audio_seconds = edition["audio_seconds"]
        pages         = edition["pages"]
        log.info('  Edition data — audio_seconds: %s, pages: %s', audio_seconds, pages)

    # ── Resolve or create user_book record ───────────────────────────────────
    user_book_id = book_state.get("hardcover_user_book_id")
    read_id      = book_state.get("hardcover_read_id")

    if not user_book_id:
        user_book = hc.get_user_book(hc_book_id)
        if user_book:
            user_book_id = user_book["id"]
            reads = user_book.get("user_book_reads", [])
            if reads:
                read_id = reads[0]["id"]
            if user_book.get("status_id") != 2:
                hc.set_status(user_book_id, 2)
                log.info('  Set "%s" to Currently Reading on Hardcover.', title)
        else:
            user_book_id = hc.create_user_book(hc_book_id)
            log.info('  Created user_book for "%s" on Hardcover.', title)

    # ── Push progress ─────────────────────────────────────────────────────────
    if read_id:
        try:
            hc.update_read_session(read_id, progress, audio_seconds, pages)
        except Exception:
            # Read session may have been deleted — create a new one
            read_id = hc.create_read_session(user_book_id, progress, audio_seconds, pages)
    else:
        read_id = hc.create_read_session(user_book_id, progress, audio_seconds, pages)

    # ── Persist state ─────────────────────────────────────────────────────────
    state["books"][book_key] = {
        "title":                   title,
        "last_synced_progress":    progress,
        "hardcover_book_id":       hc_book_id,
        "hardcover_user_book_id":  user_book_id,
        "hardcover_read_id":       read_id,
        "audio_seconds":           audio_seconds,
        "pages":                   pages,
    }
    log.info('  ✓ Synced "%s" at %.1f%%', title, progress * 100)


# ── Main sync cycle ───────────────────────────────────────────────────────────

def run_sync(st: StorytellerClient, hc: HardcoverClient) -> None:
    log.info("── Sync start ──")
    state = load_state()

    st.authenticate()
    books = st.get_books()
    log.info("Found %d book(s) in Storyteller.", len(books))

    for book in books:
        try:
            sync_book(st, hc, book, state)
        except Exception as exc:
            log.error('Error syncing "%s": %s', book.get("title", book.get("id")), exc)

    save_state(state)
    log.info("── Sync complete ──\n")


def main() -> None:
    st = StorytellerClient()
    hc = HardcoverClient()

    hc.get_user_id()
    log.info("Hardcover user ID: %s", hc.user_id)
    log.info("Sync interval: %d minutes | Min delta: %.0f%%",
             SYNC_INTERVAL // 60, MIN_DELTA * 100)

    while True:
        try:
            run_sync(st, hc)
        except Exception as exc:
            log.error("Sync cycle failed: %s", exc)

        log.info("Next sync in %d minutes.", SYNC_INTERVAL // 60)
        time.sleep(SYNC_INTERVAL)


if __name__ == "__main__":
    main()
