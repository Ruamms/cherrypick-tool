"""PRs abertos do GitHub, autenticando com a credencial que o git ja tem.

Sem dependencia do `gh` CLI: quem devolve a credencial e o proprio git
(`git credential fill`), lendo o helper configurado na maquina - no Windows,
o Git Credential Manager que vem junto com o Git for Windows.

O token nao e digitado, nao e guardado por esta ferramenta e nunca vai para o log.
"""

import json
import re
import subprocess
import urllib.error
import urllib.request

from cherrypick_tool import GIT, StepError

TEMPO_LIMITE = 40
API = "https://api.github.com"

RE_ORIGEM = re.compile(r"github\.com[:/]+([\w.-]+)/([\w.-]+?)(?:\.git)?$", re.I)


def repo_do_remote(url_remote):
    """'https://github.com/Org/repo.git' -> 'Org/repo'. '' se nao reconhecer."""
    achado = RE_ORIGEM.search((url_remote or "").strip())
    return "%s/%s" % (achado.group(1), achado.group(2)) if achado else ""


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
