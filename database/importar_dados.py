import os
import re
import sys
import unicodedata
import pandas as pd
import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_values
from dotenv import load_dotenv

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
load_dotenv(os.path.join(BASE_DIR, ".env"), override=False)


def get_pg_config(read_only: bool = False):
    user_key = "PG_READONLY_USER" if read_only else "PG_USER"
    password_key = "PG_READONLY_PASSWORD" if read_only else "PG_PASSWORD"
    user = (os.getenv(user_key) or "").strip()
    password = (os.getenv(password_key) or "").strip()
    if not user or not password:
        raise RuntimeError(f"Configure {user_key} e {password_key} no ambiente do PostgreSQL.")
    return {
        "host": (os.getenv("PG_HOST") or "localhost").strip(),
        "port": (os.getenv("PG_PORT") or "5432").strip(),
        "dbname": (os.getenv("PG_DATABASE") or "nexus_datasul").strip(),
        "user": user,
        "password": password,
    }


def normalize_identifier(name: str, fallback: str = "col") -> str:
    text = str(name or "").strip()
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    if not text:
        text = fallback
    if text[0].isdigit():
        text = f"c_{text}"
    return text[:60]


def ensure_database_exists():
    cfg = get_pg_config()
    target_db = cfg["dbname"]
    conn = psycopg2.connect(
        host=cfg["host"],
        port=cfg["port"],
        dbname="postgres",
        user=cfg["user"],
        password=cfg["password"],
    )
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target_db,))
    exists = cur.fetchone() is not None
    if not exists:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target_db)))
        print(f"Banco de dados '{target_db}' criado com sucesso no PostgreSQL!")
    cur.close()
    conn.close()


TEXT_ONLY_COLS = {
    "chave_acesso",
    "chave_nfe_substituido",
    "nat_oper",
    "ser",
    "cest",
    "inscr_estadual",
    "ie_transp",
    "placa_1",
    "placa_2",
    "placa_3",
    "emitente",
    "uf",
    "cidade",
    "bairro",
    "endereco",
    "pais",
    "situacao_doc",
    "descricao_item_xml",
    "obs_2",
}


def coerce_dataframe_types(df: pd.DataFrame) -> pd.DataFrame:
    for col in df.columns:
        s = df[col]
        if pd.api.types.is_object_dtype(s) or pd.api.types.is_string_dtype(s):
            # Remove espaços em branco nas pontas e transforma strings vazias em NaN
            cleaned = s.apply(lambda x: x.strip() if isinstance(x, str) else x)
            cleaned = cleaned.replace({"": None, "None": None, "nan": None})
            df[col] = cleaned

            if col in TEXT_ONLY_COLS:
                continue

            non_null = cleaned.dropna()
            if len(non_null) == 0:
                continue

            # Tenta converter colunas numéricas que vieram como texto no Excel (ex: '0131395', '01', '98', '1.234,56')
            def try_parse_num(val):
                if isinstance(val, (int, float)):
                    return val
                if not isinstance(val, str):
                    return val
                v = val.strip()
                # Formato brasileiro 1.234,56
                if "," in v and "." in v and v.rfind(",") > v.rfind("."):
                    v = v.replace(".", "").replace(",", ".")
                elif "," in v and "." not in v:
                    v = v.replace(",", ".")
                return v

            candidate = pd.to_numeric(non_null.map(try_parse_num), errors="coerce")
            if candidate.notna().sum() == len(non_null):
                full_num = pd.to_numeric(
                    cleaned.map(lambda x: try_parse_num(x) if x is not None else None),
                    errors="coerce",
                )
                try:
                    if (full_num.dropna() % 1 == 0).all() and (full_num.dropna().abs() < 9_000_000_000_000_000).all():
                        df[col] = full_num.astype("Int64")
                    else:
                        df[col] = full_num.astype("float64")
                except Exception:
                    df[col] = full_num.astype("float64")
    return df


def infer_pg_type(series: pd.Series, col_name: str = "") -> str:
    if series.isna().all():
        if col_name.startswith(("vl_", "base_", "aliq_", "percentual_", "qtd_", "quantidade", "peso_")):
            return "NUMERIC"
        if col_name.startswith(("dt_", "emissao", "data_")):
            return "TIMESTAMP"
        return "TEXT"
    if pd.api.types.is_integer_dtype(series):
        return "BIGINT"
    if pd.api.types.is_float_dtype(series):
        return "NUMERIC"
    if pd.api.types.is_bool_dtype(series):
        return "BOOLEAN"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "TIMESTAMP"
    non_null = series.dropna()
    if len(non_null) > 0 and all(hasattr(x, "year") and hasattr(x, "month") and hasattr(x, "day") for x in non_null):
        return "TIMESTAMP"
    return "TEXT"


def clean_cell(val, pg_type: str = "TEXT"):
    if pd.isna(val):
        return None
    if hasattr(val, "to_pydatetime"):
        res = val.to_pydatetime()
        return str(res) if pg_type == "TEXT" else res
    if hasattr(val, "item"):
        res = val.item()
        return str(res) if pg_type == "TEXT" and not isinstance(res, str) else res
    if pg_type == "TEXT" and not isinstance(val, str):
        return str(val)
    return val


def import_dataframe_to_pg(df: pd.DataFrame, table_name: str, conn):
    # Remove apenas linhas 100% vazias e colunas sem nome ("Unnamed: ...") que estejam vazias,
    # preservando TODAS as colunas nomeadas do relatório bruto mesmo que estejam vazias hoje.
    df = df.dropna(how="all").copy()
    cols_to_keep = [
        c for c in df.columns
        if not (str(c).strip().lower().startswith("unnamed:") and df[c].isna().all())
    ]
    df = df[cols_to_keep].copy()
    if df.empty and len(df.columns) == 0:
        return 0

    seen = {}
    clean_cols = []
    for idx, col in enumerate(df.columns):
        base = normalize_identifier(col, f"col_{idx+1}")
        count = seen.get(base, 0)
        seen[base] = count + 1
        clean_cols.append(base if count == 0 else f"{base}_{count+1}")
    df.columns = clean_cols
    df = coerce_dataframe_types(df)

    col_defs = []
    pg_types = []
    for col in df.columns:
        pg_type = infer_pg_type(df[col], col)
        pg_types.append(pg_type)
        col_defs.append(sql.SQL("{} {}").format(sql.Identifier(col), sql.SQL(pg_type)))

    cur = conn.cursor()
    cur.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(sql.Identifier(table_name)))
    create_stmt = sql.SQL("CREATE TABLE {} ({})").format(
        sql.Identifier(table_name),
        sql.SQL(", ").join(col_defs),
    )
    cur.execute(create_stmt)

    records = [
        tuple(clean_cell(val, pg_types[idx]) for idx, val in enumerate(row))
        for row in df.to_numpy()
    ]

    insert_stmt = sql.SQL("INSERT INTO {} ({}) VALUES %s").format(
        sql.Identifier(table_name),
        sql.SQL(", ").join(sql.Identifier(c) for c in df.columns),
    )
    execute_values(cur, insert_stmt.as_string(conn), records, page_size=2000)

    # Cria índices automáticos nas colunas de ligação e filtro mais usadas
    for index_col in ("doc_fisc", "cliente_fornec", "emitente", "cfop", "dt_docto", "emissao", "item"):
        if index_col in df.columns:
            idx_name = f"idx_{table_name}_{index_col}"[:60]
            cur.execute(
                sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} ({})").format(
                    sql.Identifier(idx_name),
                    sql.Identifier(table_name),
                    sql.Identifier(index_col),
                )
            )

    cur.close()
    return len(records)


EMPRESAS_GRUPO_VALE = (
    "CVRD ALMOX",
    "CVRD AREA",
    "CVRD BRUCUTU",
    "CVRD CARAJAS",
    "CVRD ITABIRA",
    "CVRD M AGUDO",
    "CVRD MANGARA",
    "CVRD MUTUCA",
    "CVRD P MADEI",
    "CVRD PELOTIZ",
    "CVRD PICO",
    "CVRDRIOPEIXE",
    "CVRDUNIFBH",
    "FERROUS VIGA",
    "MINA DE ALEG",
    "MINA DE CAPA",
    "MINA SOSSEG",
    "SALOBOMETAIS",
    "VALE - 040",
    "VALE  0V1",
    "VALE 081",
    "VALE C TECNO",
    "VALE CARAJAS",
    "VALE CTBC",
    "VALE EST FER",
    "VALE GOV VAL",
    "VALE M. S11D",
    "VALE OW1",
    "Vale OZ1",
    "VALE S.A",
    "VALE STACRUZ",
)


def create_official_views(conn):
    cur = conn.cursor()
    cur.execute(
        f"""
        DROP VIEW IF EXISTS vw_faturamento CASCADE;
        DROP VIEW IF EXISTS vw_devolucoes CASCADE;
        DROP VIEW IF EXISTS vw_compras CASCADE;
        DROP VIEW IF EXISTS vw_faturamento_servicos CASCADE;
        DROP VIEW IF EXISTS vw_compras_servicos CASCADE;
        DROP VIEW IF EXISTS vw_itens_grupos CASCADE;
        DROP VIEW IF EXISTS vw_estoque_grupos CASCADE;

        ALTER TABLE docto_fiscal ADD COLUMN IF NOT EXISTS is_grupo_vale BOOLEAN DEFAULT FALSE;
        ALTER TABLE docto_fiscal ADD COLUMN IF NOT EXISTS grupo_cliente TEXT;
        ALTER TABLE docto_fiscal ADD COLUMN IF NOT EXISTS desconsiderar_metrica BOOLEAN DEFAULT FALSE;
        ALTER TABLE itens ADD COLUMN IF NOT EXISTS desconsiderar_metrica BOOLEAN DEFAULT FALSE;

        UPDATE docto_fiscal
        SET is_grupo_vale = CASE WHEN emitente IN {EMPRESAS_GRUPO_VALE} THEN TRUE ELSE FALSE END,
            grupo_cliente = CASE WHEN emitente IN {EMPRESAS_GRUPO_VALE} THEN 'VALE S.A.' ELSE emitente END;

        -- Regra definitiva Nexus: Exclusão pontual e exclusiva da NF 2149 STK CABINES Jan/2026
        UPDATE docto_fiscal
        SET desconsiderar_metrica = TRUE
        WHERE cliente_fornec = 3335 AND doc_fisc = 2149 AND ser = '1' AND nat_oper = '1551OO';

        UPDATE itens
        SET desconsiderar_metrica = TRUE
        WHERE cliente_fornec = 3335 AND doc_fisc = 2149 AND ser = '1' AND nat_oper = '1551OO';

        CREATE INDEX IF NOT EXISTS idx_docto_fiscal_is_grupo_vale ON docto_fiscal (is_grupo_vale);
        CREATE INDEX IF NOT EXISTS idx_docto_fiscal_grupo_cliente ON docto_fiscal (grupo_cliente);

        CREATE OR REPLACE VIEW vw_faturamento AS
        SELECT * FROM docto_fiscal
        WHERE situacao_doc = 'Documento Regular'
          AND COALESCE(desconsiderar_metrica, FALSE) = FALSE
          AND NOT (cliente_fornec = 3335 AND doc_fisc = 2149 AND ser = '1' AND nat_oper = '1551OO')
          AND cfop IN (5933, 6101, 5101, 5124, 6107, 6933, 6122, 5125, 6124, 5118, 6125, 6401);

        CREATE OR REPLACE VIEW vw_devolucoes AS
        SELECT * FROM docto_fiscal
        WHERE situacao_doc = 'Documento Regular'
          AND COALESCE(desconsiderar_metrica, FALSE) = FALSE
          AND NOT (cliente_fornec = 3335 AND doc_fisc = 2149 AND ser = '1' AND nat_oper = '1551OO')
          AND cfop IN (1201, 1202, 2201, 2202, 2410);

        CREATE OR REPLACE VIEW vw_compras AS
        SELECT * FROM docto_fiscal
        WHERE situacao_doc = 'Documento Regular'
          AND COALESCE(desconsiderar_metrica, FALSE) = FALSE
          AND NOT (cliente_fornec = 3335 AND doc_fisc = 2149 AND ser = '1' AND nat_oper = '1551OO')
          AND cfop IN (
              1101, 1122, 1124, 1252, 1302, 1303, 1352,
              1401, 1406, 1407, 1551, 1556, 1653, 1922,
              1924, 1932, 1933, 1949, 2101, 2116, 2122, 2124,
              2125, 2352, 2401, 2406, 2407, 2551, 2556,
              2922, 2924, 2932, 2933, 2949
          );

        CREATE OR REPLACE VIEW vw_faturamento_servicos AS
        SELECT * FROM docto_fiscal
        WHERE situacao_doc = 'Documento Regular'
          AND COALESCE(desconsiderar_metrica, FALSE) = FALSE
          AND NOT (cliente_fornec = 3335 AND doc_fisc = 2149 AND ser = '1' AND nat_oper = '1551OO')
          AND cfop IN (5933, 6933);

        CREATE OR REPLACE VIEW vw_compras_servicos AS
        SELECT * FROM docto_fiscal
        WHERE situacao_doc = 'Documento Regular'
          AND COALESCE(desconsiderar_metrica, FALSE) = FALSE
          AND NOT (cliente_fornec = 3335 AND doc_fisc = 2149 AND ser = '1' AND nat_oper = '1551OO')
          AND cfop IN (1933, 2933);

        CREATE TABLE IF NOT EXISTS grupo_estoque (
            codigo VARCHAR(5) PRIMARY KEY,
            descricao VARCHAR(100) NOT NULL
        );

        INSERT INTO grupo_estoque (codigo, descricao) VALUES
        ('10', 'Material Auxiliar'),
        ('11', 'Material de uso e consumo'),
        ('20', 'Materia prima'),
        ('30', 'Material de terceiros'),
        ('40', 'Embalagem'),
        ('50', 'Produto em processo'),
        ('51', 'Produto em processo de terceiro'),
        ('60', 'Manutenção maq e equipamentos'),
        ('70', 'Manutenção predial'),
        ('80', 'Produto acabado'),
        ('81', 'Produto acabado de terceiro'),
        ('90', 'Infraestrutura e pessoal'),
        ('98', 'Materiais proprios'),
        ('99', 'Debito direto')
        ON CONFLICT (codigo) DO UPDATE SET descricao = EXCLUDED.descricao;

        CREATE OR REPLACE VIEW vw_estoque_grupos AS
        SELECT 
            d.*,
            g.codigo AS grupo_estoque_cod,
            g.descricao AS grupo_estoque_desc
        FROM diario_auxiliar_estoque d
        LEFT JOIN grupo_estoque g ON SUBSTRING(TRUNC(d.item)::text FROM 1 FOR 2) = g.codigo;

        CREATE OR REPLACE VIEW vw_itens_grupos AS
        SELECT 
            i.*,
            g.codigo AS grupo_estoque_cod,
            g.descricao AS grupo_estoque_desc
        FROM itens i
        LEFT JOIN grupo_estoque g ON SUBSTRING(TRUNC(i.item)::text FROM 1 FOR 2) = g.codigo
        WHERE COALESCE(i.desconsiderar_metrica, FALSE) = FALSE
          AND NOT (i.cliente_fornec = 3335 AND i.doc_fisc = 2149 AND i.ser = '1' AND i.nat_oper = '1551OO');

        CREATE OR REPLACE VIEW vw_atestados_competencia AS
        SELECT 
            matricula,
            nome,
            cargo,
            centro_custo,
            desc_c_custo,
            st,
            descricao_st,
            cid,
            dd_sit as dd_sit_total,
            dt_inicio::date as dt_inicio,
            dt_termino::date as dt_termino
        FROM historico_sit_funcionario
        WHERE st = 10 AND dd_sit <= 15;
        """
    )
    # Unifica VALE S.A. também nas tabelas de Contas a Receber, se existirem
    cur.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'titulos_abertos_receber') THEN
                ALTER TABLE titulos_abertos_receber ADD COLUMN IF NOT EXISTS is_grupo_vale BOOLEAN DEFAULT FALSE;
                ALTER TABLE titulos_abertos_receber ADD COLUMN IF NOT EXISTS grupo_cliente TEXT;
                UPDATE titulos_abertos_receber
                SET is_grupo_vale = (nome_abrev IN {EMPRESAS_GRUPO_VALE} OR nome_cliente ILIKE '%%VALE S.A%%'),
                    grupo_cliente = CASE WHEN (nome_abrev IN {EMPRESAS_GRUPO_VALE} OR nome_cliente ILIKE '%%VALE S.A%%') THEN 'VALE S.A.' ELSE nome_cliente END;
            END IF;

            IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'titulos_liq_contas_receber') THEN
                ALTER TABLE titulos_liq_contas_receber ADD COLUMN IF NOT EXISTS is_grupo_vale BOOLEAN DEFAULT FALSE;
                ALTER TABLE titulos_liq_contas_receber ADD COLUMN IF NOT EXISTS cliente_unificado TEXT;
                UPDATE titulos_liq_contas_receber
                SET is_grupo_vale = (nome_abreviado IN {EMPRESAS_GRUPO_VALE} OR nome ILIKE '%%VALE S.A%%'),
                    cliente_unificado = CASE WHEN (nome_abreviado IN {EMPRESAS_GRUPO_VALE} OR nome ILIKE '%%VALE S.A%%') THEN 'VALE S.A.' ELSE nome END;
            END IF;
        END $$;
        """
    )
    conn.commit()
    cur.close()


def import_excel_or_csv(file_path: str) -> dict[str, int]:
    ensure_database_exists()
    cfg = get_pg_config()
    conn = psycopg2.connect(**cfg)
    results = {}
    try:
        ext = os.path.splitext(file_path)[1].lower()
        if ext in (".xlsx", ".xls", ".xlsm"):
            xls = pd.ExcelFile(file_path)
            base_file_name = normalize_identifier(os.path.splitext(os.path.basename(file_path))[0], "relatorio")
            for sheet in xls.sheet_names:
                print(f"Lendo aba '{sheet}'...")
                df = pd.read_excel(xls, sheet_name=sheet)
                norm_sheet = normalize_identifier(sheet, "aba")
                if norm_sheet == "docto_fiscais":
                    table_name = "docto_fiscal"
                elif len(xls.sheet_names) == 1:
                    table_name = base_file_name
                else:
                    table_name = norm_sheet
                rows = import_dataframe_to_pg(df, table_name, conn)
                results[table_name] = rows
                print(f"Aba '{sheet}' -> tabela '{table_name}' ({rows} linhas importadas)")
        elif ext == ".csv":
            try:
                df = pd.read_csv(file_path, sep=None, engine="python", encoding="utf-8")
            except UnicodeDecodeError:
                df = pd.read_csv(file_path, sep=None, engine="python", encoding="latin1")
            base_name = os.path.splitext(os.path.basename(file_path))[0]
            table_name = normalize_identifier(base_name, "dados")
            rows = import_dataframe_to_pg(df, table_name, conn)
            results[table_name] = rows
            print(f"Arquivo CSV -> tabela '{table_name}' ({rows} linhas importadas)")
        else:
            raise ValueError(f"Formato de arquivo não suportado: {ext}")
        create_official_views(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return results


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target_file = sys.argv[1]
        import_excel_or_csv(target_file)
    else:
        dados_dir = os.path.join(BASE_DIR, "dados_demonstracao")
        files = [
            os.path.join(dados_dir, f)
            for f in os.listdir(dados_dir)
            if f.lower().endswith((".xlsx", ".xls", ".csv")) and not f.startswith("~$")
        ]
        if not files:
            print(f"Nenhuma planilha encontrada em {dados_dir}.")
        for f in files:
            print(f"Importando {f}...")
            import_excel_or_csv(f)
