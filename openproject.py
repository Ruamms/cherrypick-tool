"""Cliente somente-leitura da API v3 do OpenProject.

Autenticação por **token de API** (Minha conta -> Tokens de acesso), nunca por senha.

O token fica guardado só na sua máquina, no arquivo de estado em %APPDATA%, que
está fora da pasta do repositório - não há como subir por engano. Além disso ele
é cifrado com a DPAPI do Windows, amarrada à sua conta: copiar o arquivo para
outra máquina ou outro usuário não serve de nada. Nunca aparece no log.

Nada aqui escreve no OpenProject: só GET.
"""

import base64
import ctypes
import json
import re
import ssl
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from ctypes import wintypes

from cherrypick_tool import StepError

TEMPO_LIMITE = 30


# ---------------------------------------------------------------- cifra local

class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob_para_bytes(blob):
    return ctypes.string_at(blob.pbData, blob.cbData)


def proteger(texto):
    """Cifra com a DPAPI do usuário atual. Devolve base64, ou '' se falhar."""
    if not texto:
        return ""
    try:
        bruto = texto.encode("utf-8")
        entrada = _BLOB(len(bruto), ctypes.cast(ctypes.create_string_buffer(bruto),
                                                ctypes.POINTER(ctypes.c_char)))
        saida = _BLOB()
        if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(entrada), u"BackportCheck", None, None, None, 0, ctypes.byref(saida)
        ):
            return ""
        try:
            return base64.b64encode(_blob_para_bytes(saida)).decode("ascii")
        finally:
            ctypes.windll.kernel32.LocalFree(saida.pbData)
    except Exception:
        return ""


def desproteger(texto_base64):
    """Decifra o que `proteger` gerou. '' se não for desta máquina/usuário."""
    if not texto_base64:
        return ""
    try:
        bruto = base64.b64decode(texto_base64)
        entrada = _BLOB(len(bruto), ctypes.cast(ctypes.create_string_buffer(bruto),
                                                ctypes.POINTER(ctypes.c_char)))
        saida = _BLOB()
        if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(entrada), None, None, None, None, 0, ctypes.byref(saida)
        ):
            return ""
        try:
            return _blob_para_bytes(saida).decode("utf-8")
        finally:
            ctypes.windll.kernel32.LocalFree(saida.pbData)
    except Exception:
        return ""


def _mensagem_do_erro(exc):
    """Mensagem que a propria API mandou no corpo do erro, quando manda."""
    try:
        corpo = json.loads(exc.read().decode("utf-8", "replace"))
    except Exception:
        return ""
    return corpo.get("message") or corpo.get("errorIdentifier") or ""


class OpenProject(object):
    """GETs na API v3. Sem escrita, por decisão de projeto."""

    def __init__(self, base_url, token):
        if not base_url:
            raise StepError("Informe a URL do OpenProject.")
        if not token:
            raise StepError("Cole o token de API (Minha conta -> Tokens de acesso).")
        self.base = base_url.rstrip("/")
        self._token = token
        self._schemas = {}

    def __repr__(self):
        # nunca exibir o token, nem em traceback
        return "<OpenProject %s>" % urllib.parse.urlparse(self.base).netloc

    def _get(self, caminho):
        url = caminho if caminho.startswith("http") else self.base + caminho
        credencial = base64.b64encode(("apikey:%s" % self._token).encode("utf-8")).decode("ascii")
        req = urllib.request.Request(url, headers={
            "Authorization": "Basic " + credencial,
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=TEMPO_LIMITE,
                                        context=ssl.create_default_context()) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise StepError("Token recusado (401). Gere outro em Minha conta -> Tokens de acesso.")
            if exc.code == 403:
                raise StepError("Sem permissão (403) para esse recurso no OpenProject.")
            if exc.code == 404:
                raise StepError("Não encontrado (404): %s" % url)
            erro = StepError("OpenProject respondeu %s (%s) em %s"
                             % (exc.code, _mensagem_do_erro(exc) or "sem detalhe", url))
            erro.codigo_http = exc.code
            raise erro
        except urllib.error.URLError as exc:
            raise StepError("Não foi possível falar com o OpenProject (%s). "
                            "Confira a URL, a rede/VPN e o certificado." % exc.reason)

    # -------------------------------------------------------- leituras

    def eu(self):
        dados = self._get("/api/v3/users/me")
        return dados.get("name") or dados.get("login") or "?"

    def tipos(self):
        dados = self._get("/api/v3/types")
        return [t.get("name", "") for t in dados.get("_embedded", {}).get("elements", [])]

    def status_fechados(self):
        """Nomes dos status marcados como 'fechado' na própria instância.

        É o único sinal objetivo de 'a tarefa terminou' que a API oferece - melhor
        do que adivinhar pelo nome do status.
        """
        dados = self._get("/api/v3/statuses")
        return [s.get("name", "") for s in dados.get("_embedded", {}).get("elements", [])
                if s.get("isClosed")]

    def work_packages_da_query(self, query_id):
        """Resultados de uma query salva (o mesmo query_id da URL do navegador)."""
        dados = self._get("/api/v3/queries/%s" % query_id)
        resultados = dados.get("_embedded", {}).get("results", {})
        return resultados.get("_embedded", {}).get("elements", [])

    def _paginar(self, caminho, filtros, maximo_paginas=25):
        """Percorre uma coleção paginada da API v3 (offset = número da página).

        Devolve (elementos, truncou) - truncou avisa que o teto de páginas veio
        antes do fim, para o log poder dizer que a lista não está completa.
        """
        achados, pagina = [], 1
        while pagina <= maximo_paginas:
            consulta = urllib.parse.urlencode({
                "filters": filtros, "pageSize": "100", "offset": str(pagina),
            })
            dados = self._get("%s?%s" % (caminho, consulta))
            lote = dados.get("_embedded", {}).get("elements", [])
            achados.extend(lote)
            if not lote or len(achados) >= dados.get("total", 0):
                return achados, False
            pagina += 1
        return achados, True

    def work_packages_do_projeto(self, projeto, dias=180, maximo_paginas=25):
        """Tarefas do projeto, de QUALQUER status, mexidas nos últimos <dias>.

        Sem filtro explícito a API devolve só as abertas - e o caso que interessa
        aqui é exatamente a tarefa já fechada, cujo PR foi mergeado e apagado.
        Se a instância recusar o filtro de data, cai para 'sem filtro' (que
        também traz as fechadas) em vez de falhar a carga inteira.
        """
        caminho = "/api/v3/projects/%s/work_packages" % urllib.parse.quote(str(projeto))
        janela = json.dumps([{"updatedAt": {"operator": ">t-", "values": [str(int(dias))]}}])
        try:
            return self._paginar(caminho, janela, maximo_paginas)
        except StepError:
            return self._paginar(caminho, "[]", maximo_paginas)

    def work_packages_por_id(self, ids, tamanho_lote=100):
        """Busca work packages por uma lista de números, em lotes.

        Usa o filtro `id` da API v3, o que dispensa query salva e cobre tarefa
        de qualquer projeto - o que interessa é o número que veio do PR.

        Devolve (elementos, ignorados). O filtro `id` valida cada valor contra o
        que a SUA conta pode ver, então um único número apagado ou de projeto sem
        permissão reprova o lote inteiro com 400 - por isso o lote recusado é
        dividido até sobrar o culpado, que sai em `ignorados`.
        """
        achados, ignorados = [], []
        ids = [str(i) for i in ids if str(i).strip()]
        for inicio in range(0, len(ids), tamanho_lote):
            self._buscar_por_id(ids[inicio:inicio + tamanho_lote], achados, ignorados)
        return achados, sorted(ignorados, key=lambda n: (len(n), n))

    def _buscar_por_id(self, lote, achados, ignorados):
        if not lote:
            return
        filtros = json.dumps([{"id": {"operator": "=", "values": lote}}])
        consulta = urllib.parse.urlencode({
            "filters": filtros, "pageSize": str(len(lote)),
        })
        try:
            dados = self._get("/api/v3/work_packages?" + consulta)
        except StepError as exc:
            if getattr(exc, "codigo_http", None) != 400:
                raise
            if len(lote) == 1:
                ignorados.extend(lote)
                return
            meio = len(lote) // 2
            self._buscar_por_id(lote[:meio], achados, ignorados)
            self._buscar_por_id(lote[meio:], achados, ignorados)
            return
        achados.extend(dados.get("_embedded", {}).get("elements", []))

    def campos_customizados(self, wp, nomes):
        """{nome legível: valor} dos custom fields preenchidos.

        Valores simples ficam no corpo; listas e opções ficam em `_links`, com
        o texto em `title` - ler só o corpo perderia metade dos casos.
        """
        valores = {}
        for chave, bruto in wp.items():
            if not chave.startswith("customField"):
                continue
            valor = bruto.get("raw") if isinstance(bruto, dict) else bruto
            if isinstance(valor, str):
                valor = valor.strip()
            if valor in (None, "", [], {}):
                continue
            valores[nomes.get(chave, chave)] = valor
        for chave, no in (wp.get("_links") or {}).items():
            if not chave.startswith("customField"):
                continue
            if isinstance(no, list):
                titulos = [n.get("title") for n in no if isinstance(n, dict) and n.get("title")]
                if titulos:
                    valores[nomes.get(chave, chave)] = ", ".join(titulos)
            elif isinstance(no, dict) and no.get("title"):
                valores[nomes.get(chave, chave)] = no["title"]
        return valores

    def comentarios(self, wp_id):
        """Texto de todos os comentários/atividades de um work package."""
        dados = self._get("/api/v3/work_packages/%s/activities" % wp_id)
        textos = []
        for item in dados.get("_embedded", {}).get("elements", []):
            comentario = (item.get("comment") or {}).get("raw") or ""
            if comentario.strip():
                textos.append(comentario)
        return textos

    def nomes_de_campos(self, wp):
        """Mapa {chave customFieldN: nome legível}, lido do schema do work package.

        `customField42` é local ao schema (projeto + tipo): o campo 42 de um
        projeto pode ser outro campo em outro. Ler o schema de UMA tarefa e
        aplicar em todas troca o nome das demais, e o campo procurado pelo nome
        simplesmente não é achado. O cache é por href de schema, então continua
        sendo uma requisição por tipo/projeto, não por tarefa.
        """
        href = ((wp.get("_links") or {}).get("schema") or {}).get("href")
        if not href:
            return {}
        if href not in self._schemas:
            try:
                esquema = self._get(href)
            except StepError:
                self._schemas[href] = {}
                return {}
            self._schemas[href] = {
                chave: valor.get("name", chave)
                for chave, valor in esquema.items()
                if chave.startswith("customField") and isinstance(valor, dict)
            }
        return self._schemas[href]


# ---------------------------------------------------------------- extração

RE_PROJETO = re.compile(r"/projects/([^/?#]+)", re.I)


def separar_projeto(url):
    """Separa a URL da instância do identificador do projeto, se houver.

    A pessoa cola o que está na barra de endereços. Com projeto na URL a
    ferramenta passa a olhar as tarefas DO PROJETO, e não só as que têm PR
    aberto; sem projeto, o comportamento é o de antes.

        https://op.empresa.com.br            -> ('https://op.empresa.com.br', '')
        https://op.empresa.com.br/projects/x -> ('https://op.empresa.com.br', 'x')
    """
    url = (url or "").strip().rstrip("/")
    achado = RE_PROJETO.search(url)
    if not achado:
        return url, ""
    return url[:achado.start()].rstrip("/"), achado.group(1)


RE_PR = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)", re.I)


def prs_do_texto(textos):
    """Números de PR citados, na ordem de aparição, sem repetir."""
    achados = []
    for texto in textos:
        for _org, _repo, numero in RE_PR.findall(texto or ""):
            if numero not in achados:
                achados.append(numero)
    return achados


# nomes dos campos personalizados lidos da tarefa. Ficam aqui, com variantes,
# porque quem renomeia o campo no OpenProject não avisa ninguém.
CAMPO_ENTREGA = ("confirmar entrega ao cliente", "confirmar entrega")
CAMPO_RAMOS = ("ramos para disponibilizacao", "ramos para disponibilizacao dos ajustes")
PREFIXO_RAMOS = "ramos para disponibiliza"


def _chave_campo(nome):
    """Nome de campo comparável: sem acento, sem pontuação, minúsculo."""
    texto = unicodedata.normalize("NFKD", str(nome or ""))
    texto = "".join(c for c in texto if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", texto).split())


def campo_por_nome(campos, alvos, prefixo=""):
    """Valor do campo personalizado cujo nome casa com um dos alvos.

    Compara sem acento, pontuação nem caixa - o '?' de 'Confirmar entrega ao
    cliente?' não pode decidir se o campo é achado. Devolve None se nenhum casa,
    que é diferente de achar o campo vazio.
    """
    procurados = [_chave_campo(a) for a in alvos]
    achado = None
    for nome, valor in (campos or {}).items():
        chave = _chave_campo(nome)
        if chave in procurados:
            return valor
        if prefixo and achado is None and chave.startswith(_chave_campo(prefixo)):
            achado = valor
    return achado


def valor_do_campo(wp, chave):
    valor = wp.get(chave)
    if isinstance(valor, dict):
        return valor.get("raw") or valor.get("name") or ""
    return "" if valor is None else str(valor)


def titulo_do_link(wp, nome):
    return ((wp.get("_links") or {}).get(nome) or {}).get("title", "")
