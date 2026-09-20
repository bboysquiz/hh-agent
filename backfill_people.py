from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
from dataclasses import dataclass

from aiogram import Bot

from config import ConfigError, load_settings
from database import Database
from people_enrichment_strict import PeopleEnricher, format_people_messages
import people_search_compat  # noqa: F401  # sanitize DDGS backend names for current releases


logger = logging.getLogger("backfill_people")


@dataclass(frozen=True)
class VacancyRow:
    id: str
    title: str
    company: str
    status: str
    llm_decision: int | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Find public employee profiles/contacts for vacancies already stored in "
            "agent.db. This command does NOT open HH.ru and does NOT start a browser."
        )
    )
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument(
        "--job-id",
        help="Process one vacancy ID regardless of its LLM decision/status.",
    )
    selector.add_argument(
        "--company",
        help="Process stored vacancies whose company contains this text.",
    )
    selector.add_argument(
        "--all",
        action="store_true",
        help="Process every stored vacancy with a non-empty company name.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Send the people report even if this vacancy was already marked as sent.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum number of vacancies to process (0 = no limit).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run discovery and print a summary, but do not send Telegram messages.",
    )
    return parser.parse_args()


def load_rows(
    database: Database,
    *,
    job_id: str | None,
    company: str | None,
    all_vacancies: bool,
    limit: int,
) -> list[VacancyRow]:
    sql = (
        "SELECT id, title, company, status, llm_decision "
        "FROM vacancies "
    )
    params: list[object] = []

    if job_id:
        sql += "WHERE id = ? "
        params.append(job_id)
    elif company:
        sql += "WHERE company LIKE ? "
        params.append(f"%{company}%")
    elif all_vacancies:
        sql += "WHERE TRIM(company) <> '' "
    else:
        # Default: only vacancies which were positively assessed by the existing agent.
        sql += "WHERE llm_decision = 1 AND TRIM(company) <> '' "

    sql += "ORDER BY discovered_at ASC"
    if limit > 0:
        sql += " LIMIT ?"
        params.append(limit)

    with sqlite3.connect(database.path) as con:
        rows = con.execute(sql, params).fetchall()

    return [
        VacancyRow(
            id=str(row[0]),
            title=str(row[1] or ""),
            company=str(row[2] or ""),
            status=str(row[3] or ""),
            llm_decision=None if row[4] is None else int(row[4]),
        )
        for row in rows
    ]


async def run() -> int:
    args = parse_args()

    try:
        settings = load_settings()
    except ConfigError as exc:
        print("Configuration error:")
        print(exc)
        return 2

    database = Database(settings.database_path)
    database.init()

    rows = load_rows(
        database,
        job_id=args.job_id,
        company=args.company,
        all_vacancies=args.all,
        limit=max(0, args.limit),
    )

    if not rows:
        print("No matching vacancies found in agent.db")
        return 0

    print(f"Matching vacancies: {len(rows)}")
    print("HH/browser: NOT USED")

    enricher = PeopleEnricher(settings, database)
    if not enricher.available:
        print("People enrichment is disabled. Set PEOPLE_ENRICHMENT_ENABLED=true")
        await enricher.close()
        return 2

    bot: Bot | None = None
    if not args.dry_run:
        bot = Bot(token=settings.tg_bot_token)

    processed = 0
    skipped = 0
    failed = 0

    try:
        for index, vacancy in enumerate(rows, 1):
            if not args.force and enricher.report_was_sent(vacancy.id):
                skipped += 1
                print(
                    f"[{index}/{len(rows)}] SKIP already sent: "
                    f"{vacancy.id} | {vacancy.company} | {vacancy.title}"
                )
                continue

            print(
                f"[{index}/{len(rows)}] SEARCH: "
                f"{vacancy.id} | {vacancy.company} | {vacancy.title}"
            )

            try:
                people = await enricher.enrich_company(vacancy.company)
                messages = format_people_messages(
                    vacancy.title,
                    vacancy.company,
                    people,
                )

                if args.dry_run:
                    print(f"  people={len(people)} messages={len(messages)}")
                    for person in people:
                        tg = person.telegram or "-"
                        print(
                            f"  - {person.full_name} | {person.title} | "
                            f"{person.employment_status} | telegram={tg}"
                        )
                else:
                    assert bot is not None
                    for text in messages:
                        await bot.send_message(
                            chat_id=settings.tg_user_id,
                            text=text,
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                        )
                    enricher.mark_report_sent(vacancy.id)

                processed += 1
                print(f"  OK: people={len(people)}")
            except Exception as exc:
                failed += 1
                logger.exception(
                    "backfill_people_failed job_id=%s company=%r",
                    vacancy.id,
                    vacancy.company,
                )
                print(f"  ERROR: {type(exc).__name__}: {exc}")

    finally:
        await enricher.close()
        if bot is not None:
            await bot.session.close()

    print(
        f"Done. processed={processed} skipped={skipped} failed={failed}"
    )
    return 0 if failed == 0 else 1


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
