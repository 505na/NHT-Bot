import logging
import os

import mariadb
from dotenv import load_dotenv

load_dotenv()

MARIADB_HOST = os.getenv("MARIADB_HOST", "127.0.0.1")
MARIADB_PORT = int(os.getenv("MARIADB_PORT", "3306"))
MARIADB_USER = os.getenv("MARIADB_USER", "")
MARIADB_PASSWORD = os.getenv("MARIADB_PASSWORD", "")
MARIADB_DATABASE = os.getenv("MARIADB_DATABASE", "")


def get_connection() -> mariadb.Connection:
    if not MARIADB_USER or not MARIADB_DATABASE:
        raise RuntimeError("MARIADB_USER i MARIADB_DATABASE muszą być ustawione w .env.")

    try:
        return mariadb.connect(
            user=MARIADB_USER,
            password=MARIADB_PASSWORD,
            host=MARIADB_HOST,
            port=MARIADB_PORT,
            database=MARIADB_DATABASE,
            connect_timeout=5,
        )
    except mariadb.Error:
        logging.exception("Nie udało się połączyć z MariaDB.")
        raise


def initialize_schema() -> None:
    connection = get_connection()
    cursor = None
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS ticket_questionnaires (
                channel_id BIGINT UNSIGNED PRIMARY KEY,
                member_id BIGINT UNSIGNED NOT NULL,
                answers LONGTEXT CHARACTER SET utf8mb4 NOT NULL
            ) ENGINE=InnoDB
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS ticket_status (
                channel_id BIGINT UNSIGNED PRIMARY KEY,
                is_closed BOOLEAN NOT NULL DEFAULT FALSE,
                transcript_sent BOOLEAN NOT NULL DEFAULT FALSE
            ) ENGINE=InnoDB
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS ticket_counter (
                counter_id TINYINT UNSIGNED PRIMARY KEY,
                last_number BIGINT UNSIGNED NOT NULL
            )
            ENGINE=InnoDB
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_schema_migrations (
                migration_key VARCHAR(100) PRIMARY KEY,
                applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            ENGINE=InnoDB
            """
        )
        cursor.execute(
            """
            INSERT INTO ticket_counter (counter_id, last_number)
            VALUES (1, 0)
            ON DUPLICATE KEY UPDATE counter_id = VALUES(counter_id)
            """
        )
        connection.commit()
        logging.info("Przygotowano tabele ticketów w MariaDB.")
    except mariadb.Error:
        connection.rollback()
        logging.exception("Nie udało się przygotować tabel ticketów w MariaDB.")
        raise
    finally:
        if cursor is not None:
            cursor.close()
        connection.close()
