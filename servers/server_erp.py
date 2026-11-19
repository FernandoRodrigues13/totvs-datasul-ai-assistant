import logging
import re
import ipaddress
import json
import os
import secrets
import unicodedata
import psycopg2
import pandas as pd
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)


class BearerTokenMiddleware:
    """Protege todas as rotas HTTP do transporte SSE sem interromper o stream."""

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode("utf-8")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        supplied = headers.get(b"authorization", b"")
        if not secrets.compare_digest(supplied, self.expected):
            body = b"Unauthorized"
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode())],
            })
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
load_dotenv(os.path.join(BASE_DIR, ".env"), override=False)

mcp = FastMCP("NexusDatasul", dependencies=["psycopg2"])

CFOPS_FATURAMENTO = (
    5933, 6101, 5101, 5124, 6107, 6933,
    6122, 5125, 6124, 5118, 6125, 6401,
)

CFOPS_DEVOLUCAO = (
    1201, 1202, 2201, 2202, 2410,
)

CFOPS_COMPRAS = (
    1101, 1122, 1124, 1252, 1302, 1303, 1352,
    1401, 1406, 1407, 1551, 1556, 1653, 1922,
    1924, 1932, 1933, 1949, 2101, 2116, 2122, 2124,
    2125, 2352, 2401, 2406, 2407, 2551, 2556,
    2922, 2924, 2932, 2933, 2949,
)

CFOPS_FATURAMENTO_SERVICOS = (5933, 6933)
CFOPS_COMPRAS_SERVICOS = (1933, 2933)

SAIDAS_COLUNAS = {
    "pedido": "pedido de venda",
    "cliente": "cliente",
    "nota_fiscal": "no nota fiscal",
    "emissao": "emissao",
    "valor": "valor",
    "vencimento": "vencimento",
    "tipo_nf": "tipo de nf",
    "saida": "saida",
    "dias_estoque": "dias em estoque",
    "status": "status de saida",
    "siare": "siare",
    "invoice_coupa": "invoice coupa",
    "previsao_saida_logistica": "data previsao de saida logistica",
    "chamado": "no do chamado",
    "observacao": "observacao obrigatorio para saidas 5 dias uteis",
}


def _normalizar_cabecalho(valor: object) -> str:
    texto = unicodedata.normalize("NFKD", str(valor or ""))
    texto = "".join(c for c in texto if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", texto).strip()


def _carregar_planilha_siare() -> tuple[pd.DataFrame, dict[str, str]]:
    caminho = (os.getenv("SAIDAS_XLSX_PATH") or "").strip().strip('"')
    if not caminho:
        raise RuntimeError("Configure SAIDAS_XLSX_PATH no arquivo .env.")
    if not os.path.isfile(caminho):
        raise FileNotFoundError("Planilha de saídas não acessível pelo processo do MCP.")
    if os.path.splitext(caminho)[1].lower() != ".xlsx":
        raise ValueError("A fonte de saídas deve ser um arquivo .xlsx.")

    frame = pd.read_excel(caminho, sheet_name="SIARE", engine="openpyxl")
    headers = {_normalizar_cabecalho(c): str(c) for c in frame.columns}
    resolved = {}
    for key, label in SAIDAS_COLUNAS.items():
        actual = headers.get(_normalizar_cabecalho(label))
        if actual:
            resolved[key] = actual
    required = {"cliente", "nota_fiscal", "emissao", "valor", "saida", "status"}
    if required.difference(resolved):
        raise ValueError("A aba SIARE não contém todas as colunas essenciais esperadas.")
    return frame, resolved


def _numero_brasileiro(valor: object) -> float:
    if pd.isna(valor):
        return 0.0
    if isinstance(valor, (int, float)):
        return float(valor)
    texto = re.sub(r"[^0-9,.-]", "", str(valor).strip())
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    try:
        return float(texto)
    except ValueError:
        return 0.0


def get_connection():
    user = (os.getenv("PG_READONLY_USER") or "").strip()
    password = (os.getenv("PG_READONLY_PASSWORD") or "").strip()
    if not user or not password:
        raise RuntimeError("Configure PG_READONLY_USER e PG_READONLY_PASSWORD no ambiente do servidor.")
    return psycopg2.connect(
        host=(os.getenv("PG_HOST") or "localhost").strip(),
        port=(os.getenv("PG_PORT") or "5432").strip(),
        dbname=(os.getenv("PG_DATABASE") or "nexus_datasul").strip(),
        user=user,
        password=password,
        connect_timeout=5,
    )


@mcp.tool()
def get_schema() -> str:
    """Retorna todas as tabelas, views e colunas do banco de dados nexus_datasul"""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
            ORDER BY table_name, ordinal_position;
        """)
        schema = {}
        for table_name, column_name, data_type in cursor.fetchall():
            if table_name not in schema:
                schema[table_name] = []
            schema[table_name].append({"column": column_name, "type": data_type})
        cursor.close()
        conn.close()
        return json.dumps({"schema": schema}, ensure_ascii=False, default=str)
    except Exception:
        logger.exception("Falha ao consultar o esquema do banco")
        return json.dumps({"error": "Falha ao consultar o esquema do banco."}, ensure_ascii=False)


@mcp.tool()
def resumo_oficial_nexus(ano: int, mes: int = 0) -> str:
    """
    Calcula os valores oficiais de Faturamento Bruto, Devoluções, Faturamento Líquido e Compras
    aplicando automaticamente a regra oficial de CFOPs da Nexus e filtrando apenas 'Documento Regular'.
    Informe o ano (ex: 2026) e opcionalmente o mês de 1 a 12 (use 0 para o ano inteiro).
    """
    try:
        if not 1900 <= int(ano) <= 2200:
            raise ValueError("Ano fora do intervalo permitido (1900–2200).")
        if int(mes) not in range(0, 13):
            raise ValueError("Mês inválido; informe 0 ou um valor de 1 a 12.")
        conn = get_connection()
        cursor = conn.cursor()

        tem_mes = bool(mes and 1 <= int(mes) <= 12)
        filtro_mes_docto = "AND EXTRACT(MONTH FROM emissao) = %s" if tem_mes else ""
        params_docto = (ano, int(mes)) if tem_mes else (ano,)

        # Faturamento (CFOPs oficiais, Documento Regular)
        cursor.execute(
            f"""
            SELECT
                COUNT(*) AS qtd_notas,
                COALESCE(SUM(vl_contabil), 0) AS faturamento_bruto,
                COALESCE(SUM(vl_icms_trib), 0) AS icms,
                COALESCE(SUM(vl_ipi), 0) AS ipi,
                COALESCE(SUM(vl_iss), 0) AS iss
            FROM docto_fiscal
            WHERE situacao_doc = 'Documento Regular'
              AND COALESCE(desconsiderar_metrica, FALSE) = FALSE
              AND NOT (cliente_fornec = 3335 AND doc_fisc = 2149 AND ser = '1' AND nat_oper = '1551OO')
              AND cfop IN {CFOPS_FATURAMENTO}
              AND EXTRACT(YEAR FROM emissao) = %s
              {filtro_mes_docto}
            """,
            params_docto,
        )
        fat = cursor.fetchone()

        cursor.execute(
            f"""
            SELECT
                COALESCE(SUM(i.vl_pis), 0) AS pis,
                COALESCE(SUM(i.vl_cofins), 0) AS cofins
            FROM itens i
            JOIN docto_fiscal d
              ON d.cliente_fornec = i.cliente_fornec
             AND d.doc_fisc = i.doc_fisc
             AND COALESCE(d.ser, '') = COALESCE(i.ser, '')
             AND d.nat_oper = i.nat_oper
            WHERE d.situacao_doc = 'Documento Regular'
              AND COALESCE(d.desconsiderar_metrica, FALSE) = FALSE
              AND NOT (d.cliente_fornec = 3335 AND d.doc_fisc = 2149 AND d.ser = '1' AND d.nat_oper = '1551OO')
              AND d.cfop IN {CFOPS_FATURAMENTO}
              AND EXTRACT(YEAR FROM d.emissao) = %s
              {filtro_mes_docto.replace('emissao', 'd.emissao')}
            """,
            params_docto,
        )
        fat_pc = cursor.fetchone()

        # Devoluções (CFOPs oficiais 1201, 1202, 2201, 2202, 2410, Documento Regular)
        cursor.execute(
            f"""
            SELECT
                COUNT(*) AS qtd_notas,
                COALESCE(SUM(vl_contabil), 0) AS total_devolucoes,
                COALESCE(SUM(vl_icms_trib), 0) AS icms,
                COALESCE(SUM(vl_ipi), 0) AS ipi
            FROM docto_fiscal
            WHERE situacao_doc = 'Documento Regular'
              AND COALESCE(desconsiderar_metrica, FALSE) = FALSE
              AND NOT (cliente_fornec = 3335 AND doc_fisc = 2149 AND ser = '1' AND nat_oper = '1551OO')
              AND cfop IN {CFOPS_DEVOLUCAO}
              AND EXTRACT(YEAR FROM emissao) = %s
              {filtro_mes_docto}
            """,
            params_docto,
        )
        dev = cursor.fetchone()

        cursor.execute(
            f"""
            SELECT
                COALESCE(SUM(i.vl_pis), 0) AS pis,
                COALESCE(SUM(i.vl_cofins), 0) AS cofins
            FROM itens i
            JOIN docto_fiscal d
              ON d.cliente_fornec = i.cliente_fornec
             AND d.doc_fisc = i.doc_fisc
             AND COALESCE(d.ser, '') = COALESCE(i.ser, '')
             AND d.nat_oper = i.nat_oper
            WHERE d.situacao_doc = 'Documento Regular'
              AND COALESCE(d.desconsiderar_metrica, FALSE) = FALSE
              AND NOT (d.cliente_fornec = 3335 AND d.doc_fisc = 2149 AND d.ser = '1' AND d.nat_oper = '1551OO')
              AND d.cfop IN {CFOPS_DEVOLUCAO}
              AND EXTRACT(YEAR FROM d.emissao) = %s
              {filtro_mes_docto.replace('emissao', 'd.emissao')}
            """,
            params_docto,
        )
        dev_pc = cursor.fetchone()

        # Compras (CFOPs oficiais de compras, Documento Regular)
        cursor.execute(
            f"""
            SELECT
                COUNT(*) AS qtd_notas,
                COALESCE(SUM(vl_contabil), 0) AS total_compras,
                COALESCE(SUM(vl_icms_trib), 0) AS icms,
                COALESCE(SUM(vl_ipi), 0) AS ipi,
                COALESCE(SUM(vl_iss), 0) AS iss
            FROM docto_fiscal
            WHERE situacao_doc = 'Documento Regular'
              AND COALESCE(desconsiderar_metrica, FALSE) = FALSE
              AND NOT (cliente_fornec = 3335 AND doc_fisc = 2149 AND ser = '1' AND nat_oper = '1551OO')
              AND cfop IN {CFOPS_COMPRAS}
              AND EXTRACT(YEAR FROM emissao) = %s
              {filtro_mes_docto}
            """,
            params_docto,
        )
        comp = cursor.fetchone()

        cursor.execute(
            f"""
            SELECT
                COALESCE(SUM(i.vl_pis), 0) AS pis,
                COALESCE(SUM(i.vl_cofins), 0) AS cofins
            FROM itens i
            JOIN docto_fiscal d
              ON d.cliente_fornec = i.cliente_fornec
             AND d.doc_fisc = i.doc_fisc
             AND COALESCE(d.ser, '') = COALESCE(i.ser, '')
             AND d.nat_oper = i.nat_oper
            WHERE d.situacao_doc = 'Documento Regular'
              AND COALESCE(d.desconsiderar_metrica, FALSE) = FALSE
              AND NOT (d.cliente_fornec = 3335 AND d.doc_fisc = 2149 AND d.ser = '1' AND d.nat_oper = '1551OO')
              AND d.cfop IN {CFOPS_COMPRAS}
              AND EXTRACT(YEAR FROM d.emissao) = %s
              {filtro_mes_docto.replace('emissao', 'd.emissao')}
            """,
            params_docto,
        )
        comp_pc = cursor.fetchone()

        # Participação do Grupo VALE S.A. no Faturamento
        cursor.execute(
            f"""
            SELECT
                COALESCE(SUM(CASE WHEN is_grupo_vale THEN vl_contabil ELSE 0 END), 0) AS fat_grupo_vale,
                COUNT(CASE WHEN is_grupo_vale THEN 1 END) AS qtd_notas_vale,
                COALESCE(SUM(CASE WHEN NOT is_grupo_vale THEN vl_contabil ELSE 0 END), 0) AS fat_outros_clientes,
                COUNT(CASE WHEN NOT is_grupo_vale THEN 1 END) AS qtd_notas_outros
            FROM docto_fiscal
            WHERE situacao_doc = 'Documento Regular'
              AND COALESCE(desconsiderar_metrica, FALSE) = FALSE
              AND NOT (cliente_fornec = 3335 AND doc_fisc = 2149 AND ser = '1' AND nat_oper = '1551OO')
              AND cfop IN {CFOPS_FATURAMENTO}
              AND EXTRACT(YEAR FROM emissao) = %s
              {filtro_mes_docto}
            """,
            params_docto,
        )
        vale_row = cursor.fetchone()

        cursor.close()
        conn.close()

        fat_bruto = float(fat[1] or 0)
        tot_dev = float(dev[1] or 0)
        fat_vale = float(vale_row[0] or 0) if vale_row else 0.0
        fat_outros = float(vale_row[2] or 0) if vale_row else 0.0
        pct_vale = round((fat_vale / fat_bruto * 100), 2) if fat_bruto > 0 else 0.0

        res = {
            "periodo": f"{int(mes):02d}/{ano}" if tem_mes else str(ano),
            "regra_situacao": "Documento Regular",
            "faturamento": {
                "qtd_notas": fat[0],
                "faturamento_bruto": fat_bruto,
                "devolucoes": tot_dev,
                "faturamento_liquido_sem_devolucao": fat_bruto - tot_dev,
                "icms": float(fat[2] or 0),
                "ipi": float(fat[3] or 0),
                "pis": float(fat_pc[0] or 0),
                "cofins": float(fat_pc[1] or 0),
                "iss": float(fat[4] or 0),
                "participacao_grupo_vale_sa": {
                    "faturamento_grupo_vale": fat_vale,
                    "qtd_notas_grupo_vale": vale_row[1] if vale_row else 0,
                    "percentual_sobre_faturamento_bruto": f"{pct_vale}%",
                    "faturamento_demais_clientes": fat_outros,
                    "qtd_notas_demais_clientes": vale_row[3] if vale_row else 0,
                },
            },
            "devolucoes_detalhe": {
                "qtd_notas": dev[0],
                "valor_total": tot_dev,
                "icms": float(dev[2] or 0),
                "ipi": float(dev[3] or 0),
                "pis": float(dev_pc[0] or 0),
                "cofins": float(dev_pc[1] or 0),
            },
            "compras": {
                "qtd_notas": comp[0],
                "valor_total": float(comp[1] or 0),
                "icms": float(comp[2] or 0),
                "ipi": float(comp[3] or 0),
                "pis": float(comp_pc[0] or 0),
                "cofins": float(comp_pc[1] or 0),
                "iss": float(comp[4] or 0),
            },
        }
        return json.dumps(res, ensure_ascii=False, default=str)
    except ValueError as e:
        return f"Parâmetro inválido: {e}"
    except Exception:
        logger.exception("Falha ao calcular resumo oficial")
        return "Erro ao calcular resumo oficial. Consulte os logs do servidor."


@mcp.tool()
def health_check() -> bool:
    """Verifica se a conexão com o PostgreSQL local nexus_datasul está ativa"""
    try:
        conn = get_connection()
        conn.close()
        return True
    except Exception:
        return False


@mcp.tool()
def consultar_saidas_siare(
    cliente: str = "",
    status: str = "",
    pedido: str = "",
    nota_fiscal: str = "",
    emissao_de: str = "",
    emissao_ate: str = "",
    limite: int = 100,
) -> str:
    """Consulta, sem alterar o arquivo, as saídas da aba SIARE da planilha configurada.

    Datas de emissão usam o formato AAAA-MM-DD. Os resultados são limitados a 250 linhas.
    """
    try:
        if not 1 <= int(limite) <= 250:
            return "Parâmetro inválido: limite deve estar entre 1 e 250."
        for data in (emissao_de, emissao_ate):
            if data and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", data):
                return "Parâmetro inválido: datas devem usar o formato AAAA-MM-DD."
        if emissao_de and emissao_ate and emissao_de > emissao_ate:
            return "Parâmetro inválido: emissao_de deve ser anterior ou igual a emissao_ate."

        frame, columns = _carregar_planilha_siare()
        filters = {
            "cliente": cliente,
            "status": status,
            "pedido": pedido,
            "nota_fiscal": nota_fiscal,
        }
        for key, value in filters.items():
            if value:
                if key not in columns:
                    return f"A planilha não possui a coluna necessária para filtrar por {key}."
                mask = frame[columns[key]].astype("string").str.contains(
                    value, case=False, regex=False, na=False
                )
                frame = frame[mask]

        if emissao_de or emissao_ate:
            dates = pd.to_datetime(frame[columns["emissao"]], errors="coerce", dayfirst=True)
            if emissao_de:
                frame = frame[dates >= pd.Timestamp(emissao_de)]
                dates = dates.loc[frame.index]
            if emissao_ate:
                frame = frame[dates < pd.Timestamp(emissao_ate) + pd.Timedelta(days=1)]

        total_rows = len(frame)
        valor_total = round(sum(_numero_brasileiro(v) for v in frame[columns["valor"]]), 2)
        selected = [key for key in SAIDAS_COLUNAS if key in columns]
        page = frame.head(int(limite))
        result_frame = page[[columns[key] for key in selected]].copy()
        result_frame.columns = selected
        records = json.loads(result_frame.to_json(orient="records", date_format="iso", force_ascii=False))
        result = {
            "fonte": "Planilha de controle de saídas",
            "aba": "SIARE",
            "total_encontrado": total_rows,
            "valor_total_encontrado": valor_total,
            "registros_retornados": len(records),
            "registros": records,
        }
        if total_rows > len(records):
            result["aviso"] = "Resultado parcial; refine os filtros para consultar as demais linhas."
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception:
        logger.exception("Falha ao consultar a planilha da aba SIARE")
        return "Não foi possível consultar a planilha SIARE. Verifique o caminho, a permissão de rede e o formato da aba."


@mcp.tool()
def query(sql: str) -> str:
    """Executa uma consulta SQL de leitura (SELECT/WITH) no banco nexus_datasul.
    
    REGRA OFICIAL OBRIGATÓRIA DA SAQUETTO: em consultas a docto_fiscal e itens, use SEMPRE
    situacao_doc = 'Documento Regular'.
    - Faturamento de Serviços: CFOPs 5933, 6933 (ou view vw_faturamento_servicos).
    - Compras de Serviços: CFOPs 1933, 2933 (ou view vw_compras_servicos).
    Nunca inclua documentos cancelados ou inutilizados.
    """
    try:
        statement = sql.strip()
        if not statement:
            return "Erro SQL: consulta vazia."
        # Permitimos apenas uma instrução SELECT/WITH. Transações read-only abaixo
        # fornecem uma segunda barreira contra CTEs que tentem alterar dados.
        if statement.endswith(";"):
            statement = statement[:-1].rstrip()
        if ";" in statement or not re.match(r"^(SELECT|WITH)\b", statement, re.IGNORECASE):
            return "Erro SQL: somente uma consulta SELECT ou WITH é permitida."
        normalized_for_check = statement.replace('"', "")
        if re.search(
            r"\b(pg_sleep|pg_read_file|pg_read_binary_file|pg_ls_dir|dblink|"
            r"lo_import|lo_export|set_config|pg_cancel_backend|pg_terminate_backend|"
            r"pg_reload_conf|pg_notify)\s*\(|\bCOPY\b",
            normalized_for_check,
            re.IGNORECASE,
        ):
            return "Erro SQL: a consulta contém uma função ou comando não permitido."
        conn = get_connection()
        try:
            conn.set_session(readonly=True)
            with conn.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '15s'")
                cursor.execute("SET LOCAL idle_in_transaction_session_timeout = '20s'")
                cursor.execute(statement)
                if cursor.description is None:
                    return "Erro SQL: a consulta não retornou um conjunto de resultados."
                rows = cursor.fetchmany(1001)
                truncated = len(rows) > 1000
                rows = rows[:1000]
                cols = [desc[0] for desc in cursor.description]
                result = [dict(zip(cols, row)) for row in rows]
        finally:
            conn.close()
        if truncated:
            result.append({"aviso": "Resultado limitado às primeiras 1000 linhas."})
        if not result:
            return json.dumps(
                [{"aviso": "Nenhum registro encontrado (0 linhas) para os filtros aplicados."}],
                ensure_ascii=False,
            )
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception:
        logger.exception("Falha ao executar consulta SQL")
        return "Erro ao executar consulta SQL. Verifique a consulta ou consulte os logs do servidor."


@mcp.tool()
def consultar_faturamento_datasul_online(data_inicio: str, data_fim: str) -> str:
    """Consulta notas fiscais e faturamento diretamente da API REST oficial do Datasul em tempo real (Porta 8080).
    Formato esperado das datas: DD/MM/AAAA (ex: '15/11/2026', '18/11/2026').
    Retorna as notas emitidas, clientes, valores faturados e itens diretamente do ERP.
    """
    try:
        from database.sincronizar_datasul_api import buscar_notas_datasul_api
        notas = buscar_notas_datasul_api(data_inicio.strip(), data_fim.strip())
        if not notas:
            return f"Nenhuma nota fiscal encontrada no Datasul entre {data_inicio} e {data_fim}."
        
        resultado = []
        tot_valor = 0.0
        for n in notas:
            vlr = float(n.get("invoiceAmount") or 0.0)
            tot_valor += vlr
            resultado.append({
                "numero_nf": n.get("invoiceNumber"),
                "serie": n.get("series"),
                "cliente": n.get("customerShortName"),
                "emissao": n.get("printDate"),
                "valor_total": vlr,
                "chave_acesso": n.get("eletronicInvoiceAccessKey"),
                "itens": [
                    {
                        "item": it.get("itemCode"),
                        "descricao": it.get("comments"),
                        "quantidade": it.get("internalQuantity"),
                        "valor": it.get("itemTotalAmount")
                    } for it in (n.get("invoiceItem") or [])
                ]
            })
        
        return json.dumps({
            "periodo": f"{data_inicio} a {data_fim}",
            "total_notas": len(notas),
            "valor_faturado_total": tot_valor,
            "notas": resultado
        }, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        logger.exception("Falha ao consultar API REST do Datasul")
        return f"Erro ao consultar API Datasul: {str(e)}"


@mcp.tool()
def consultar_compras_datasul_online(limite_notas: int = 50) -> str:
    """Consulta as notas fiscais de entrada / compras de matérias-primas e serviços diretamente da API do Datasul.
    Retorna os fornecedores, insumos adquiridos, CFOPs de entrada (1xxx e 2xxx) e valores.
    """
    try:
        from database.sincronizar_datasul_api import sincronizar_compras_datasul
        res = sincronizar_compras_datasul(limite_paginas=max(1, limite_notas // 50))
        return json.dumps({
            "status": "sucesso",
            "mensagem": f"Compras sincronizadas com sucesso do Datasul.",
            "compras_sincronizadas": res.get("compras_inseridas", 0),
            "itens_sincronizados": res.get("itens_inseridos", 0)
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("Falha ao consultar compras na API REST do Datasul")
        return f"Erro ao consultar compras na API Datasul: {str(e)}"


@mcp.tool()
def consultar_saldo_estoque_datasul(deposito: str = "") -> str:
    """Consulta o saldo de estoque atual nos depósitos do Datasul (ex: ALM para almoxarifado, PRO para produção).
    Retorna os itens com saldo disponível, alocado e grupo de estoque.
    """
    try:
        from database.importar_datasul import get_pg_config
        cfg = get_pg_config(read_only=True)
        conn = psycopg2.connect(**cfg)
        cur = conn.cursor()
        
        sql = """
            SELECT item, descricao_item, deposito, saldo_disponivel, qtd_alocada, grupo_estoque_desc
            FROM vw_saldo_estoque
            WHERE saldo_disponivel > 0
        """
        params = []
        if deposito.strip():
            sql += " AND deposito = %s"
            params.append(deposito.strip().upper())
        sql += " ORDER BY saldo_disponivel DESC LIMIT 100;"
        
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        cur.close()
        conn.close()
        
        resultado = [dict(zip(cols, r)) for r in rows]
        return json.dumps({
            "deposito_filtro": deposito or "TODOS",
            "total_itens_retornados": len(resultado),
            "itens": resultado
        }, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        logger.exception("Falha ao consultar saldo de estoque")
        return f"Erro ao consultar saldo de estoque: {str(e)}"


@mcp.tool()
def consultar_ordens_producao_datasul(estado: str = "", cliente: str = "") -> str:
    """Consulta as ordens de produção (OPs) do Datasul.
    Permite filtrar por estado (ex: 'Não Iniciada', 'Em Produção', 'Terminada') ou por cliente (ex: 'VALE', 'CVRD').
    """
    try:
        from database.importar_datasul import get_pg_config
        cfg = get_pg_config(read_only=True)
        conn = psycopg2.connect(**cfg)
        cur = conn.cursor()

        sql = """
            SELECT nr_ordem, item, descricao_item, quantidade, un, estado_desc, cliente, dt_inicio, dt_termino
            FROM vw_ordens_producao
            WHERE 1=1
        """
        params = []
        if estado.strip():
            sql += " AND UPPER(estado_desc) LIKE %s"
            params.append(f"%{estado.strip().upper()}%")
        if cliente.strip():
            sql += " AND UPPER(cliente) LIKE %s"
            params.append(f"%{cliente.strip().upper()}%")

        sql += " ORDER BY nr_ordem DESC LIMIT 100;"

        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        cur.close()
        conn.close()

        resultado = [dict(zip(cols, r)) for r in rows]
        return json.dumps({
            "filtros": {"estado": estado or "TODOS", "cliente": cliente or "TODOS"},
            "total_ordens_retornadas": len(resultado),
            "ordens": resultado
        }, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        logger.exception("Falha ao consultar ordens de produção")
        return f"Erro ao consultar ordens de produção: {str(e)}"


@mcp.tool()
def consultar_contas_a_pagar(somente_firmes: bool = True, fornecedor: str = "", limite: int = 100) -> str:
    """Consulta títulos em aberto de Contas a Pagar.
    REGRA OBRIGATÓRIA DA SAQUETTO: Por padrão (somente_firmes=True), títulos de PREVISÃO
    (espécies 'PR', 'PF', 'PV' e is_previsao=TRUE) são DESCONSIDERADOS, pois são provisões orçamentárias e não dívidas firmes.
    Permite filtrar por fornecedor (razão social ou nome abreviado).
    """
    try:
        from database.importar_datasul import get_pg_config
        cfg = get_pg_config(read_only=True)
        conn = psycopg2.connect(**cfg)
        cur = conn.cursor()

        tabela = "vw_titulos_abertos_pagar" if somente_firmes else "titulos_abertos_pagar"
        sql = f"""
            SELECT fornec, razao_social, nome_abreviado, cgc, vencto, esp, titulo, p, vl_original, valor_saldo, is_previsao
            FROM {tabela}
            WHERE 1=1
        """
        params = []
        if fornecedor.strip():
            sql += " AND (UPPER(razao_social) LIKE %s OR UPPER(nome_abreviado) LIKE %s)"
            termo = f"%{fornecedor.strip().upper()}%"
            params.extend([termo, termo])

        sql += f" ORDER BY vencto ASC LIMIT {max(1, min(limite, 500))};"

        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]

        # Totalizador
        sql_tot = f"SELECT COUNT(*), COALESCE(SUM(valor_saldo), 0) FROM {tabela} WHERE 1=1"
        if fornecedor.strip():
            sql_tot += " AND (UPPER(razao_social) LIKE %s OR UPPER(nome_abreviado) LIKE %s)"
            cur.execute(sql_tot, (f"%{fornecedor.strip().upper()}%", f"%{fornecedor.strip().upper()}%"))
        else:
            cur.execute(sql_tot)
        tot_titulos, tot_saldo = cur.fetchone()

        cur.close()
        conn.close()

        titulos = [dict(zip(cols, r)) for r in rows]
        return json.dumps({
            "regra": "Previsões excluídas (dívida firme)" if somente_firmes else "Todas as espécies incluídas (com provisões)",
            "fornecedor_filtro": fornecedor or "TODOS",
            "total_geral_titulos": tot_titulos,
            "saldo_total_aberto_reais": float(tot_saldo),
            "titulos_amostra": titulos
        }, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        logger.exception("Falha ao consultar contas a pagar")
        return f"Erro ao consultar contas a pagar: {str(e)}"


@mcp.tool()
def consultar_contas_a_receber(somente_firmes: bool = True, cliente: str = "", limite: int = 100) -> str:
    """Consulta títulos em aberto de Contas a Receber.
    REGRA OBRIGATÓRIA DA SAQUETTO: Por padrão (somente_firmes=True), títulos de PREVISÃO são DESCONSIDERADOS.
    Permite filtrar por cliente (nome abreviado, razão social ou grupo unificado como 'VALE').
    """
    try:
        from database.importar_datasul import get_pg_config
        cfg = get_pg_config(read_only=True)
        conn = psycopg2.connect(**cfg)
        cur = conn.cursor()

        tabela = "vw_titulos_abertos_receber" if somente_firmes else "titulos_abertos_receber"
        sql = f"""
            SELECT cliente, nome_abrev, grupo_cliente, esp, ser, titulo, p, emissao, vencto, saldo, dias, is_previsao
            FROM {tabela}
            WHERE 1=1
        """
        params = []
        if cliente.strip():
            sql += " AND (UPPER(nome_abrev) LIKE %s OR UPPER(grupo_cliente) LIKE %s)"
            termo = f"%{cliente.strip().upper()}%"
            params.extend([termo, termo])

        sql += f" ORDER BY vencto ASC LIMIT {max(1, min(limite, 500))};"

        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]

        # Totalizador
        sql_tot = f"SELECT COUNT(*), COALESCE(SUM(saldo), 0) FROM {tabela} WHERE 1=1"
        if cliente.strip():
            sql_tot += " AND (UPPER(nome_abrev) LIKE %s OR UPPER(grupo_cliente) LIKE %s)"
            cur.execute(sql_tot, (f"%{cliente.strip().upper()}%", f"%{cliente.strip().upper()}%"))
        else:
            cur.execute(sql_tot)
        tot_titulos, tot_saldo = cur.fetchone()

        cur.close()
        conn.close()

        titulos = [dict(zip(cols, r)) for r in rows]
        return json.dumps({
            "regra": "Previsões excluídas (recebimento firme)" if somente_firmes else "Todas as espécies incluídas",
            "cliente_filtro": cliente or "TODOS",
            "total_geral_titulos": tot_titulos,
            "saldo_total_aberto_reais": float(tot_saldo),
            "titulos_amostra": titulos
        }, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        logger.exception("Falha ao consultar contas a receber")
        return f"Erro ao consultar contas a receber: {str(e)}"



if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Servidor MCP Nexus Datasul")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Tipo de transporte: 'stdio' (padrão local) ou 'sse' (serviço de rede para compartilhar com outros funcionários)",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host para escutar no modo SSE (padrão: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Porta para o serviço SSE (padrão: 8000)",
    )
    parser.add_argument("--tls-certfile", help="Certificado TLS (obrigatório para escuta fora do localhost).")
    parser.add_argument("--tls-keyfile", help="Chave privada TLS (obrigatória para escuta fora do localhost).")

    args = parser.parse_args()

    if args.transport == "sse":
        token = (os.getenv("MCP_AUTH_TOKEN") or "").strip()
        if len(token) < 32:
            parser.error("Defina MCP_AUTH_TOKEN no .env com pelo menos 32 caracteres para habilitar SSE.")
        try:
            remote_bind = not ipaddress.ip_address(args.host).is_loopback
        except ValueError:
            remote_bind = args.host.lower() != "localhost"
        if remote_bind and not (args.tls_certfile and args.tls_keyfile):
            parser.error("Escuta na rede exige --tls-certfile e --tls-keyfile. Use localhost para acesso local sem TLS.")
        import uvicorn

        app = BearerTokenMiddleware(mcp.sse_app(), token)
        print("Servidor MCP Nexus SSE protegido por token.")
        scheme = "https" if args.tls_certfile else "http"
        print(f"📡 Endpoint MCP: {scheme}://{args.host}:{args.port}/sse")
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            log_level="info",
            ssl_certfile=args.tls_certfile,
            ssl_keyfile=args.tls_keyfile,
        )
    else:
        mcp.run(transport="stdio")
