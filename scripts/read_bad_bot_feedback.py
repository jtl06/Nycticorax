from __future__ import annotations

import argparse
import os

import psycopg


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read archived Nycti bad-bot feedback and optionally clear reviewed rows."
    )
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--full",
        action="store_true",
        help="Print complete bundles, including raw agent messages, schemas, and traces.",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Delete only the rows displayed by this run after printing them.",
    )
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error("--limit must be between 1 and 100")

    database_url = os.getenv("DATABASE_PUBLIC_URL") or os.getenv("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_PUBLIC_URL or DATABASE_URL is required")
    database_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT feedback_message_id, created_at, feedback_text, bundle
                FROM bad_bot_feedback
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (args.limit,),
            )
            rows = cursor.fetchall()
            if not rows:
                print("No archived bad-bot feedback.")
                return

            for feedback_message_id, created_at, feedback_text, bundle in rows:
                rendered_bundle = (
                    bundle
                    if args.full
                    else bundle.split("agent_messages_and_tool_results", 1)[0].rstrip()
                )
                print(
                    "\n".join(
                        (
                            "---BAD-BOT-FEEDBACK---",
                            f"feedback_message_id: {feedback_message_id}",
                            f"created_at: {created_at.isoformat()}",
                            f"feedback: {feedback_text}",
                            rendered_bundle,
                        )
                    )
                )

            if args.clear:
                reviewed_ids = [row[0] for row in rows]
                cursor.execute(
                    "DELETE FROM bad_bot_feedback WHERE feedback_message_id = ANY(%s)",
                    (reviewed_ids,),
                )
                print(f"Cleared {cursor.rowcount} reviewed row(s).")


if __name__ == "__main__":
    main()
