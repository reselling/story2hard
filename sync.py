#!/usr/bin/env python3
"""
Storyteller → Hardcover one-way progress sync.

Polls Storyteller every 15 minutes and mirrors reading status to Hardcover:
  - Reading  → Currently Reading + sync % (only if ≥ 1% change)
  - To read  → Want to Read (no progress pushed)
  - Read     → Read / completed
Books with no status or zero progress are ignored.
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
        """
        Returns books from the v2 API, which includes an embedded `status`
        object (name: "Reading" | "To read" | "Read") and current `position`.
        Falls back to v1 if v2 is unavailable.
        """
        try:
            resp = self.session.get(f"{STORYTELLER_URL}/api/v2/books", timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            log.warning("v2 books endpoint unavailable, falling back to v1.")
            resp = self.session.get(f"{STORYTELLER_URL}/api/books", timeout=15)
            resp.raise_for_status()
            return resp.json()

    def get_progress(self, book_id) -> float | None:
        """
        Returns totalProgression (0.0–1.0) from the positions endpoint,
        or None if no position has been recorded yet.
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
        Returns the most popular English edition for page-based progress tracking.

        Hardcover computes the `progress` percentage (read by external APIs)
        from progress_pages / edition.pages. We pick the most-used English
        edition (by users_count) so the % matches Storyteller exactly and
        the edition shown is the recognisable English version.

        Falls back to the most popular edition regardless of language if no
        English edition with pages is found.
        """
        data = self._gql(
            """
            query BookData($bookId: Int!) {
              # Most popular English edition
              english: editions(
                where: {
                  book_id: {_eq: $bookId},
                  pages: {_gt: 0},
                  language: {language: {_eq: "English"}}
                },
                order_by: {users_count: desc},
                limit: 1
              ) {
                id
                pages
                users_count
                edition_format
              }
              # Fallback: any edition with pages
              fallback: editions(
                where: {book_id: {_eq: $bookId}, pages: {_gt: 0}},
                order_by: {users_count: desc},
                limit: 1
              ) {
                id
                pages
                users_count
              }
            }
            """,
            {"bookId": book_id},
        )
        english   = data.get("english", [])
        fallback  = data.get("fallback", [])
        edition   = (english or fallback or [None])[0]
        if not edition:
            return {"edition_id": None, "pages": None}
        return {"edition_id": edition["id"], "pages": edition["pages"]}

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

    def create_user_book(self, book_id: int, status_id: int, edition_id: int | None) -> int:
        """Creates a user_book with the given status. Returns id."""
        data = self._gql(
            """
            mutation CreateUserBook($bookId: Int!, $statusId: Int!, $editionId: Int) {
              insert_user_book(object: {
                book_id: $bookId,
                status_id: $statusId,
                edition_id: $editionId
              }) {
                id
              }
            }
            """,
            {"bookId": book_id, "statusId": status_id, "editionId": edition_id},
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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_status_and_progress(book: dict) -> tuple[str | None, float | None]:
    """
    Extract (status_name, progress) from a v2 book object.
    status_name is one of "Reading", "To read", "Read", or None.
    progress is 0.0–1.0 or None.
    """
    status_obj = book.get("status") or {}
    status_name = status_obj.get("name")  # "Reading" | "To read" | "Read" | None

    # v2 embeds position directly on the book object
    position = book.get("position") or {}
    locator = position.get("locator") or {}
    progress = locator.get("locations", {}).get("totalProgression")

    return status_name, progress


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

    # ── Determine status and progress from v2 data ────────────────────────────
    status_name, progress = _extract_status_and_progress(book)

    # If no embedded position (v1 fallback), fetch separately
    if progress is None and status_name == "Reading":
        progress = st.get_progress(book_id)

    # Books with no status and no progress are untouched — skip entirely
    if not status_name and (progress is None or progress == 0.0):
        return

    book_state = state["books"].get(book_key, {})

    # ── "To read" → Want to Read on Hardcover ────────────────────────────────
    if status_name == "To read":
        hc_book_id = book_state.get("hardcover_book_id")
        if not hc_book_id:
            hc_book_id = hc.search_book(title)
            if not hc_book_id:
                log.warning('  Could not find "%s" on Hardcover — skipping.', title)
                return

        user_book_id = book_state.get("hardcover_user_book_id")
        if not user_book_id:
            user_book = hc.get_user_book(hc_book_id)
            if user_book:
                user_book_id = user_book["id"]
                if user_book.get("status_id") != 1:
                    hc.set_status(user_book_id, 1)
                    log.info('"%s": To read → Want to Read on Hardcover.', title)
            else:
                ed = hc.get_book_edition_data(hc_book_id)
                user_book_id = hc.create_user_book(hc_book_id, 1, ed["edition_id"])
                log.info('"%s": Created as Want to Read on Hardcover.', title)
        elif book_state.get("hardcover_status_id") != 1:
            hc.set_status(user_book_id, 1)
            log.info('"%s": Updated to Want to Read on Hardcover.', title)

        state["books"][book_key] = {
            **book_state,
            "title":                  title,
            "hardcover_book_id":      hc_book_id,
            "hardcover_user_book_id": user_book_id,
            "hardcover_status_id":    1,
        }
        return

    # ── "Read" → Read/completed on Hardcover ─────────────────────────────────
    if status_name == "Read":
        hc_book_id = book_state.get("hardcover_book_id")
        if not hc_book_id:
            hc_book_id = hc.search_book(title)
            if not hc_book_id:
                log.warning('  Could not find "%s" on Hardcover — skipping.', title)
                return

        user_book_id = book_state.get("hardcover_user_book_id")
        if not user_book_id:
            user_book = hc.get_user_book(hc_book_id)
            if user_book:
                user_book_id = user_book["id"]

        if not user_book_id:
            ed = hc.get_book_edition_data(hc_book_id)
            user_book_id = hc.create_user_book(hc_book_id, 3, ed["edition_id"])
            log.info('"%s": Created as Read on Hardcover.', title)
        elif book_state.get("hardcover_status_id") != 3:
            hc.set_status(user_book_id, 3)
            log.info('"%s": Marked as Read on Hardcover.', title)

        state["books"][book_key] = {
            **book_state,
            "title":                  title,
            "hardcover_book_id":      hc_book_id,
            "hardcover_user_book_id": user_book_id,
            "hardcover_status_id":    3,
        }
        return

    # ── "Reading" → Currently Reading + sync progress ────────────────────────
    if status_name != "Reading":
        return  # Unknown status — skip

    if progress is None or progress == 0.0:
        return  # Never actually opened

    last_progress = book_state.get("last_synced_progress", 0.0)
    finished = progress >= 1.0

    if not finished and abs(progress - last_progress) < MIN_DELTA:
        return  # Change too small — skip

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
            user_book_id = hc.create_user_book(hc_book_id, 2, edition_id)
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

    # ── Persist state ─────────────────────────────────────────────────────────
    state["books"][book_key] = {
        "title":                   title,
        "last_synced_progress":    progress,
        "hardcover_book_id":       hc_book_id,
        "hardcover_user_book_id":  user_book_id,
        "hardcover_read_id":       read_id,
        "hardcover_status_id":     3 if finished else 2,
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
