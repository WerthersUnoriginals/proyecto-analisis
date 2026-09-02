import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv


# Localiza la raíz del proyecto independientemente
# de desde dónde se ejecute el programa.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Carga las variables privadas guardadas en .env.
load_dotenv(PROJECT_ROOT / ".env")


def get_connection():
    """
    Abre una conexión con la base de datos CAN SLIM.
    Las credenciales se obtienen de variables de entorno/.env.
    """

    required_variables = [
        "CANSLIM_DB_HOST",
        "CANSLIM_DB_PORT",
        "CANSLIM_DB_NAME",
        "CANSLIM_DB_USER",
        "CANSLIM_DB_PASSWORD",
    ]

    missing = [
        variable
        for variable in required_variables
        if not os.getenv(variable)
    ]

    if missing:
        raise RuntimeError(
            "Faltan variables de entorno para PostgreSQL: "
            + ", ".join(missing)
        )

    return psycopg.connect(
        host=os.getenv("CANSLIM_DB_HOST"),
        port=int(os.getenv("CANSLIM_DB_PORT")),
        dbname=os.getenv("CANSLIM_DB_NAME"),
        user=os.getenv("CANSLIM_DB_USER"),
        password=os.getenv("CANSLIM_DB_PASSWORD"),
    )