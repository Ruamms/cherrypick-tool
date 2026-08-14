"""Cliente somente-leitura da API v3 do OpenProject.

Autenticacao por **token de API** (Minha conta -> Tokens de acesso), nunca por senha.

O token fica guardado so na sua maquina, no arquivo de estado em %APPDATA%, que
esta fora da pasta do repositorio - nao ha como subir por engano. Alem disso ele
e cifrado com a DPAPI do Windows, amarrada a sua conta: copiar o arquivo para
outra maquina ou outro usuario nao serve de nada. Nunca aparece no log.

Nada aqui escreve no OpenProject: so GET.
"""

import base64
import ctypes
import json
import re
import ssl
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
    """Cifra com a DPAPI do usuario atual. Devolve base64, ou '' se falhar."""
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
    """Decifra o que `proteger` gerou. '' se nao for desta maquina/usuario."""
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


class OpenProject(object):
    """GETs na API v3. Sem escrita, por decisao de projeto."""

    def __init__(self, base_url, token):
        if not base_url:
            raise StepError("Informe a URL do OpenProject.")
        if not token:
            raise StepError("Cole o token de API (Minha conta -> Tokens de acesso).")
        self.base = base_url.rstrip("/")
        self._token = token

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
                raise StepError("Sem permissao (403) para esse recurso no OpenProject.")
            if exc.code == 404:
                raise StepError("Nao encontrado (404): %s" % url)
            raise StepError("OpenProject respondeu %s em %s" % (exc.code, url))
        except urllib.error.URLError as exc:
            raise StepError("Nao foi possivel falar com o OpenProject (%s). "
                            "Confira a URL, a rede/VPN e o certificado." % exc.reason)

    # -------------------------------------------------------- leituras

    def eu(self):
        dados = self._get("/api/v3/users/me")
        return dados.get("name") or dados.get("login") or "?"

    def tipos(self):
        dados = self._get("/api/v3/types")
        return [t.get("name", "") for t in dados.get("_embedded", {}).get("elements", [])]

    def work_packages_da_query(self, query_id):
        """Resultados de uma query salva (o mesmo query_id da URL do navegador)."""
        dados = self._get("/api/v3/queries/%s" % query_id)
        resultados = dados.get("_embedded", {}).get("results", {})
        return resultados.get("_embedded", {}).get("elements", [])

    def comentarios(self, wp_id):
        """Texto de todos os comentarios/atividades de um work package."""
        dados = self._get("/api/v3/work_packages/%s/activities" % wp_id)
        textos = []
        for item in dados.get("_embedded", {}).get("elements", []):
            comentario = (item.get("comment") or {}).get("raw") or ""
            if comentario.strip():
                textos.append(comentario)
        return textos

    def nomes_de_campos(self, wp):
        """Mapa {chave customFieldN: nome legivel}, lido do schema do work package."""
        href = ((wp.get("_links") or {}).get("schema") or {}).get("href")
        if not href:
            return {}
        try:
            esquema = self._get(href)
        except StepError:
            return {}
        nomes = {}
        for chave, valor in esquema.items():
            if chave.startswith("customField") and isinstance(valor, dict):
                nomes[chave] = valor.get("name", chave)
        return nomes


# ---------------------------------------------------------------- extracao

RE_PR = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)", re.I)


def prs_do_texto(textos):
    """Numeros de PR citados, na ordem de aparicao, sem repetir."""
    achados = []
    for texto in textos:
        for _org, _repo, numero in RE_PR.findall(texto or ""):
            if numero not in achados:
                achados.append(numero)
    return achados


def valor_do_campo(wp, chave):
    valor = wp.get(chave)
    if isinstance(valor, dict):
        return valor.get("raw") or valor.get("name") or ""
    return "" if valor is None else str(valor)


def titulo_do_link(wp, nome):
    return ((wp.get("_links") or {}).get(nome) or {}).get("title", "")
