#!/usr/bin/env python3
"""
Storyteller → Hardcover one-way progress sync.

Polls Storyteller every 15 minutes and pushes any book progress
(≥ 1% change) to Hardcover. Books with zero progress are ignored.
100% progress marks the book as Read on Hardcover.
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
        Returns totalProgression (0.0–1.0) or None if no position recorded.
        For Read Aloud books this is the epub3 text position (synced with audio).
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
        """Search Hardcover by title. Returns top-match book_id or None."""
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
        doc = hits[0].get("document", hits[0])
        raw_id = doc.get("id")
        return int(raw_id) if raw_id is not None else None

    def get_book_edition_data(self, book_id: int) -> dict:
        """
        Returns the best edition for page-based progress tracking.

        Hardcover computes the `progress` percentage (used by external APIs)
        from progress_pages / edition.pages — NOT from progress_seconds.
        We therefore use a print/ebook edition and progress_pages so that
        the percentage shown in Hardcover matches Storyteller exactly.
        """
        data = self._gql(
            """
            query BookData($bookId: Int!) {
              books(where: {id: {_eq: $bookId}}) {
                pages
                editions(
                  where: {pages: {_gt: 0}},
                  limit: 1,
                  order_by: {pages: desc}
                ) {
                  id
                  pages
                }
              }
            }
            """,
            {"bookId": book_id},
        )
        books = data.get("books", [])
        if not books:
            return {"edition_id": None, "pages": None}
        book = books[0]
        editions = book.get("editions", [])
        if editions:
            return {"edition_id": editions[0]["id"], "pages": editions[0]["pages"]}
        # Fall back to book-level page count (no specific edition)
        return {"edition_id": None, "pages": book.get("pages")}

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

    def create_user_book(self, book_id: int, edition_id: int | None) -> int:
        """Creates a user_book with status Currently Reading. Returns id."""
        data = self._gql(
            """
            mutation CreateUserBook($bookId: Int!, $editionId: Int) {
              insert_user_book(object: {
                book_id: $bookId,
                status_id: 2,
                edition_id: $editionId
              }) {
                id
              }
            }
            """,
            {"bookId": book_id, "editionId": edition_id},
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

    def create_read_session(
        self,
        user_book_id: int,
        progress: float,
        pages: int | None,
        edition_id: int | None,
        finished: bool = False,
    ) -> int:
        prog_pages = round(progress * pages) if pages else None
        finished_at = str(date.today()) if finished else None
        data = self._gql(
            """
            mutation CreateRead(
              $userBookId: Int!,
              $startedAt: date!,
              $progressPages: Int,
              $editionId: Int,
              $finishedAt: date
            ) {
              insert_user_book_read(
                user_book_id: $userBookId,
                user_book_read: {
                  started_at: $startedAt,
                  progress_pages: $progressPages,
                  edition_id: $editionId,
                  finished_at: $finishedAt
                }
              ) {
                id
              }
            }
            """,
            {
                "userBookId": user_book_id,
                "startedAt": str(date.today()),
                "progressPages": prog_pages,
                "editionId": edition_id,
                "finishedAt": finished_at,
            },
        )
        return data["insert_user_book_read"]["id"]

    def update_read_session(
        self,
        read_id: int,
        progress: float,
        pages: int | None,
        edition_id: int | None,
        finished: bool = False,
    ) -> None:
        prog_pages = round(progress * pages) if pages else None
        finished_at = str(date.today()) if finished else None
        self._gql(
            """
            mutation UpdateRead(
              $id: Int!,
              $progressPages: Int,
              $editionId: Int,
              $finishedAt: date
            ) {
              update_user_book_read(id: $id, object: {
                progress_pages: $progressPages,
                edition_id: $editionId,
                finished_at: $finishedAt
              }) {
                id
              }
            }
            """,
            {
                "id": read_id,
                "progressPages": prog_pages,
                "editionId": edition_id,
                "finishedAt": finished_at,
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

    finished = progress >= 1.0
    log.info('"%s": %.1f%% → %.1f%%', title, last_progress * 100, progress * 100)

    # ── Find book on Hardcover ────────────────────────────────────────────────
    hc_book_id = book_state.get("hardcover_book_id")
    pages      = book_state.get("pages")
    edition_id = book_state.get("edition_id")

    if not hc_book_id:
        hc_book_id = hc.search_book(title)
        if not hc_book_id:
            log.warning('  Could not find "%s" on Hardcover — skipping.', title)
            return
        ed = hc.get_book_edition_data(hc_book_id)
        pages      = ed["pages"]
        edition_id = ed["edition_id"]
        log.info('  Edition %s — %s pages', edition_id, pages)

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
            if user_book.get("status_id") not in (2, 3):
                hc.set_status(user_book_id, 2)
                log.info('  Set to Currently Reading.')
        else:
            user_book_id = hc.create_user_book(hc_book_id, edition_id)
            log.info('  Created user_book on Hardcover.')

    # ── Push progress ─────────────────────────────────────────────────────────
    if read_id:
        try:
            hc.update_read_session(read_id, progress, pages, edition_id, finished)
        except Exception:
            read_id = hc.create_read_session(user_book_id, progress, pages, edition_id, finished)
    else:
        read_id = hc.create_read_session(user_book_id, progress, pages, edition_id, finished)

    # ── Mark as Read if completed ─────────────────────────────────────────────
    if finished:
        hc.set_status(user_book_id, 3)
        log.info('  Marked "%s" as Read on Hardcover.', title)
    elif book_state.get("hardcover_user_book_id") and not finished:
        # Ensure status stays Currently Reading (not already marked done)
        if book_state.get("last_synced_progress", 0) < 1.0:
            pass  # status already set correctly

    # ── Persist state ─────────────────────────────────────────────────────────
    state["books"][book_key] = {
        "title":                   title,
        "last_synced_progress":    progress,
        "hardcover_book_id":       hc_book_id,
        "hardcover_user_book_id":  user_book_id,
        "hardcover_read_id":       read_id,
        "pages":                   pages,
        "edition_id":              edition_id,
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
