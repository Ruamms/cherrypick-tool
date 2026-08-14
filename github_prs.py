"""PRs abertos do GitHub, autenticando com a credencial que o git ja tem.

Sem dependencia do `gh` CLI: quem devolve a credencial e o proprio git
(`git credential fill`), lendo o helper configurado na maquina - no Windows,
o Git Credential Manager que vem junto com o Git for Windows.

O token nao e digitado, nao e guardado por esta ferramenta e nunca vai para o log.
"""

import ctypes
import json
import re
import subprocess
import urllib.error
import urllib.request
from ctypes import wintypes

from cherrypick_tool import GIT, StepError

TEMPO_LIMITE = 40
API = "https://api.github.com"

AUTOMATICA = "(automatica pelo repositorio)"
PADRAO_GIT = "(padrao do git)"

CRED_TYPE_GENERIC = 1


class _CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)), ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
    ]


def contas_guardadas(filtro=u"git:https://github.com*"):
    """[(alvo, usuario)] das credenciais git do GitHub no Gerenciador do Windows."""
    quantidade = wintypes.DWORD()
    ponteiro = ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))()
    try:
        ok = ctypes.windll.advapi32.CredEnumerateW(
            filtro, 0, ctypes.byref(quantidade), ctypes.byref(ponteiro))
    except Exception:
        return []
    if not ok:
        return []
    achadas = []
    try:
        for i in range(quantidade.value):
            cred = ponteiro[i].contents
            if cred.UserName:
                achadas.append((cred.TargetName, cred.UserName))
    finally:
        ctypes.windll.advapi32.CredFree(ponteiro)
    return achadas


def segredo_da_conta(alvo):
    """Le o segredo de uma credencial guardada - a mesma que o git usaria."""
    ponteiro = ctypes.POINTER(_CREDENTIAL)()
    if not ctypes.windll.advapi32.CredReadW(alvo, CRED_TYPE_GENERIC, 0, ctypes.byref(ponteiro)):
        return ""
    try:
        cred = ponteiro.contents
        return ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize).decode(
            "utf-16-le", "ignore")
    finally:
        ctypes.windll.advapi32.CredFree(ponteiro)

RE_ORIGEM = re.compile(r"github\.com[:/]+([\w.-]+)/([\w.-]+?)(?:\.git)?$", re.I)


def repo_do_remote(url_remote):
    """'https://github.com/Org/repo.git' -> 'Org/repo'. '' se nao reconhecer."""
    achado = RE_ORIGEM.search((url_remote or "").strip())
    return "%s/%s" % (achado.group(1), achado.group(2)) if achado else ""


def credencial_escolhida(conta, caminho_repo, org_repo):
    """Devolve (usuario, segredo) conforme a conta escolhida na tela."""
    if conta and conta not in (AUTOMATICA, PADRAO_GIT):
        for alvo, usuario in contas_guardadas():
            if usuario == conta:
                segredo = segredo_da_conta(alvo)
                if segredo:
                    return usuario, segredo
        raise StepError("Nao consegui ler o segredo da conta '%s' no Gerenciador de "
                        "Credenciais. Escolha outra conta." % conta)
    if conta == PADRAO_GIT:
        return credencial_do_git(caminho_repo)
    # automatica: o caminho org/repo e o que faz o git escolher a conta certa
    return credencial_do_git(caminho_repo, org_repo=org_repo)


def credencial_do_git(caminho_repo=None, host="github.com", org_repo=""):
    """Pede ao git a credencial do host. Devolve (usuario, segredo) ou ('', '')."""
    pedido = "protocol=https\nhost=%s\n" % host
    if org_repo:
        pedido += "path=%s\n" % org_repo
    pedido += "\n"
    comando = [GIT]
    if caminho_repo:
        comando += ["-C", caminho_repo]
    comando += ["credential", "fill"]
    try:
        proc = subprocess.run(
            comando, input=pedido, capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        raise StepError("Nao foi possivel pedir a credencial ao git (%r)." % (exc,))
    usuario = segredo = ""
    for linha in (proc.stdout or "").splitlines():
        if linha.startswith("username="):
            usuario = linha.split("=", 1)[1]
        elif linha.startswith("password="):
            segredo = linha.split("=", 1)[1]
    return usuario, segredo


class GitHub(object):
    """Somente leitura da API REST do GitHub."""

    def __init__(self, token):
        if not token:
            raise StepError(
                "O git nao devolveu credencial para o github.com. Faca um `git fetch` "
                "no repositorio uma vez para o Gerenciador de Credenciais guardar o acesso."
            )
        self._token = token

    def __repr__(self):
        return "<GitHub>"

    def _get(self, url):
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + self._token,
            "Accept": "application/vnd.github+json",
            "User-Agent": "backport-check",
        })
        try:
            with urllib.request.urlopen(req, timeout=TEMPO_LIMITE) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise StepError("GitHub recusou a credencial do git (401).")
            if exc.code == 404:
                raise StepError(
                    "GitHub devolveu 404 em %s. A conta que o git usa nao enxerga esse "
                    "repositorio - confira se e a conta certa para essa organizacao." % url
                )
            if exc.code == 403:
                raise StepError("GitHub recusou (403): limite de requisicoes ou sem permissao.")
            raise StepError("GitHub respondeu %s em %s" % (exc.code, url))
        except urllib.error.URLError as exc:
            raise StepError("Nao foi possivel falar com a API do GitHub (%s)." % exc.reason)

    def usuario(self):
        return self._get(API + "/user").get("login", "?")

    def revisao(self, org_repo, numero):
        """'aprovado', 'ajustes', 'comentado' ou ''.

        Vale a ultima revisao de cada pessoa. 'comentado' existe porque nem todo
        time usa o botao de aprovar: saber que alguem ja olhou e comentou ainda
        distingue o PR revisado do PR em que ninguem encostou.
        """
        try:
            lista = self._get("%s/repos/%s/pulls/%s/reviews?per_page=100"
                              % (API, org_repo, numero))
        except StepError:
            return ""
        if not isinstance(lista, list):
            return ""
        por_pessoa = {}
        for revisao in lista:
            estado = (revisao.get("state") or "").upper()
            if estado in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED"):
                por_pessoa[((revisao.get("user") or {}).get("login") or "?")] = estado
        estados = set(por_pessoa.values())
        if "CHANGES_REQUESTED" in estados:
            return "ajustes"
        if "APPROVED" in estados:
            return "aprovado"
        if "COMMENTED" in estados:
            return "comentado"
        return ""

    def revisoes(self, prs, trabalhadores=8, progresso=None):
        """{(repo, numero): estado} para varios PRs, em paralelo."""
        import concurrent.futures

        resultado = {}
        if not prs:
            return resultado
        with concurrent.futures.ThreadPoolExecutor(max_workers=trabalhadores) as executor:
            futuros = {
                executor.submit(self.revisao, pr["repo"], pr["numero"]): (pr["repo"], pr["numero"])
                for pr in prs
            }
            for i, futuro in enumerate(concurrent.futures.as_completed(futuros), 1):
                chave = futuros[futuro]
                try:
                    resultado[chave] = futuro.result()
                except Exception:
                    resultado[chave] = ""
                if progresso and i % 25 == 0:
                    progresso(i, len(futuros))
        return resultado

    def prs_abertos(self, org_repo, maximo_paginas=6):
        """Lista simplificada dos PRs abertos de um repositorio."""
        saida, pagina = [], 1
        while pagina <= maximo_paginas:
            lote = self._get("%s/repos/%s/pulls?state=open&per_page=100&page=%d"
                             % (API, org_repo, pagina))
            if not isinstance(lote, list):
                break
            for pr in lote:
                saida.append({
                    "repo": org_repo,
                    "numero": pr.get("number"),
                    "titulo": pr.get("title") or "",
                    "base": ((pr.get("base") or {}).get("ref") or ""),
                    "head": ((pr.get("head") or {}).get("ref") or ""),
                    "autor": ((pr.get("user") or {}).get("login") or ""),
                    "criado": (pr.get("created_at") or "")[:10],
                    "atualizado": (pr.get("updated_at") or "")[:10],
                    "rascunho": bool(pr.get("draft")),
                    "url": pr.get("html_url") or "",
                })
            if len(lote) < 100:
                break
            pagina += 1
        return saida
